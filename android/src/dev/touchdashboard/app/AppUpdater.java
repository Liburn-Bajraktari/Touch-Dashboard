package dev.touchdashboard.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Self-update: download a release of this app from its GitHub releases, check
 * it, and hand it to Android's package installer.
 *
 * The page only names a tag; the download address is fixed here, so a device
 * on the LAN cannot point the tablet at another file. The APK must match the
 * release's SHA256SUMS (fetched over HTTPS), carry this package name, be newer
 * than the installed version and be signed by the same certificate. Android
 * also refuses an update signed by another key, whatever this class decides.
 *
 * Events go to the page as {"state": ..., ...}: checking, downloading
 * (progress 0..1), verifying, permission, confirm, installed, cancelled,
 * failed (error, message).
 */
final class AppUpdater {

    interface Listener {
        void onUpdateEvent(JSONObject event);
    }

    static final String RELEASES = "https://github.com/Liburn-Bajraktari/Touch-Dashboard/releases/download/";
    private static final Pattern TAG = Pattern.compile("v\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}");
    private static final Pattern SUM_LINE = Pattern.compile("(?m)^([0-9a-fA-F]{64})\\s+\\*?(\\S+)\\s*$");
    private static final long MAX_APK_BYTES = 64L * 1024 * 1024;
    private static final int MAX_SUMS_BYTES = 64 * 1024;
    private static final long PROGRESS_EVERY_MS = 200;


    private final Activity activity;
    private volatile Listener listener;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final AtomicBoolean busy = new AtomicBoolean();
    /** A checked APK waiting for the install permission, and its tag. */
    private volatile File readyApk;
    private volatile String readyTag;
    private volatile boolean awaitingPermission;
    /** The installer is open; seeing the activity again means no update happened. */
    private volatile boolean awaitingInstall;

    AppUpdater(Activity activity, Listener listener) {
        this.activity = activity;
        this.listener = listener;
        // A successful install replaces the process before it can tidy up.
        worker.execute(() -> clearDir(new File(activity.getCacheDir(), "updates")));
    }

    void detach() {
        listener = null;
    }

    /** Start (or resume, after the permission screen) an update to `tag`. */
    void start(String tag) {
        if (tag == null || !TAG.matcher(tag).matches()) {
            fail("bad_tag", "Not a release tag: " + tag);
            return;
        }
        if (readyApk != null && tag.equals(readyTag)) {
            activity.runOnUiThread(this::installOrAskPermission);
            return;
        }
        if (!busy.compareAndSet(false, true)) return;   // one update at a time
        worker.execute(() -> {
            try {
                File apk = downloadAndCheck(tag);
                readyApk = apk;
                readyTag = tag;
                activity.runOnUiThread(this::installOrAskPermission);
            } catch (UpdateError e) {
                fail(e.code, e.getMessage());
            } catch (IOException e) {
                fail("network", e.getClass().getSimpleName() + ": " + e.getMessage());
            } catch (RuntimeException e) {
                fail("internal", e.toString());
            }
        });
    }

    /**
     * The activity is back in front: from the "Install unknown apps" screen
     * (install if it was allowed), or from the installer, which means the
     * update was cancelled or failed (a successful one replaces this process).
     */
    void onHostResumed() {
        if (awaitingInstall) {
            awaitingInstall = false;
            PackageReplacedReceiver.forgetUpdate(activity);
            finish("cancelled", null, "The update was not installed");
            return;
        }
        if (!awaitingPermission || readyApk == null) return;
        if (canInstall()) {
            awaitingPermission = false;
            launchInstaller();
        } else {
            emit("permission");
        }
    }

    void openInstallPermissionSettings() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        try {
            activity.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + activity.getPackageName())));
        } catch (ActivityNotFoundException e) {
            try {
                activity.startActivity(new Intent(Settings.ACTION_SECURITY_SETTINGS));
            } catch (ActivityNotFoundException ignored) {
                // No settings screen to open: the page still explains what to allow.
            }
        }
    }

    private boolean canInstall() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.O
                || activity.getPackageManager().canRequestPackageInstalls();
    }

    private void installOrAskPermission() {
        if (readyApk == null) return;
        if (!canInstall()) {
            awaitingPermission = true;
            emit("permission");
            openInstallPermissionSettings();
            return;
        }
        awaitingPermission = false;
        launchInstaller();
    }

    // ── Download and checks ─────────────────────────────────────────────────

    private File downloadAndCheck(String tag) throws IOException, UpdateError {
        String apkName = "TouchDashboard-" + tag + ".apk";
        File dir = new File(activity.getCacheDir(), "updates");
        clearDir(dir);
        if (!dir.mkdirs() && !dir.isDirectory()) throw new IOException("cannot create " + dir);

        emit("checking");
        String sums = new String(fetchBytes(RELEASES + tag + "/SHA256SUMS", MAX_SUMS_BYTES),
                StandardCharsets.UTF_8);
        String expected = null;
        Matcher m = SUM_LINE.matcher(sums);
        while (m.find()) {
            if (m.group(2).equals(apkName)) expected = m.group(1).toLowerCase();
        }
        if (expected == null) throw new UpdateError("no_checksum", "SHA256SUMS lists no " + apkName);

        File apk = new File(dir, apkName);
        String actual = download(RELEASES + tag + "/" + apkName, apk);
        if (!actual.equals(expected)) {
            apk.delete();
            throw new UpdateError("checksum_mismatch", "The download does not match SHA256SUMS");
        }

        emit("verifying");
        checkArchive(apk);
        return apk;
    }

    @SuppressWarnings("deprecation")
    private void checkArchive(File apk) throws UpdateError {
        PackageManager pm = activity.getPackageManager();
        int flags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? PackageManager.GET_SIGNING_CERTIFICATES : PackageManager.GET_SIGNATURES;
        PackageInfo archive = pm.getPackageArchiveInfo(apk.getPath(), flags);
        if (archive == null) throw new UpdateError("invalid_apk", "Not a readable APK");
        if (!activity.getPackageName().equals(archive.packageName)) {
            throw new UpdateError("wrong_package", "The APK is " + archive.packageName);
        }
        if (versionCode(archive) <= ownVersionCode(activity)) {
            throw new UpdateError("not_newer", "This version is already installed");
        }
        try {
            PackageInfo own = pm.getPackageInfo(activity.getPackageName(), flags);
            Set<String> mine = signerHashes(own), theirs = signerHashes(archive);
            // Some Android versions omit an archive's signers; the system still
            // checks them when it installs, so only a known mismatch stops here.
            if (!theirs.isEmpty() && !theirs.equals(mine)) {
                throw new UpdateError("wrong_signer", "The APK is signed by another key");
            }
        } catch (PackageManager.NameNotFoundException e) {
            throw new UpdateError("internal", "Own package not found");
        }
    }

    // ── Installing ──────────────────────────────────────────────────────────

    /**
     * Hand the checked APK to Android's package installer, as a sideloaded
     * file. Its screens end with "App installed" and an Open button, which a
     * PackageInstaller session never shows: there the dialog just closes and
     * the dashboard is gone. The APK is shared read-only through
     * UpdateFileProvider. Android kills this process to replace the app; if
     * the activity comes back instead, the update did not happen.
     */
    @SuppressWarnings("deprecation")
    private void launchInstaller() {
        File apk = readyApk;
        if (apk == null) return;
        Uri uri = UpdateFileProvider.uriFor(apk);
        // INSTALL_PACKAGE goes to the system installer itself (VIEW could offer
        // other APK handlers); deprecated in API 29 but still the sideload path.
        Intent install = new Intent(Intent.ACTION_INSTALL_PACKAGE)
                .setDataAndType(uri, UpdateFileProvider.APK_TYPE)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                .putExtra(Intent.EXTRA_RETURN_RESULT, false);
        PackageReplacedReceiver.expectUpdate(activity);
        try {
            activity.startActivity(install);
        } catch (ActivityNotFoundException e) {
            try {
                activity.startActivity(new Intent(Intent.ACTION_VIEW)
                        .setDataAndType(uri, UpdateFileProvider.APK_TYPE)
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
            } catch (ActivityNotFoundException none) {
                PackageReplacedReceiver.forgetUpdate(activity);
                fail("install_failed", "No package installer on this device");
                return;
            }
        }
        awaitingInstall = true;
        emit("confirm");
    }

    // ── HTTP ────────────────────────────────────────────────────────────────

    private HttpURLConnection open(String url) throws IOException, UpdateError {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setInstanceFollowRedirects(true);   // https → https only (HttpURLConnection never downgrades)
        c.setConnectTimeout(15000);
        c.setReadTimeout(30000);
        c.setRequestProperty("User-Agent", "TouchDashboard-Android");
        int code = c.getResponseCode();
        if (code == 404) throw new UpdateError("not_found", "Release file not found: " + url);
        if (code != 200) throw new UpdateError("network", "HTTP " + code + " for " + url);
        if (!"https".equals(c.getURL().getProtocol())) throw new UpdateError("network", "Refused a non-HTTPS download");
        return c;
    }

    private byte[] fetchBytes(String url, int max) throws IOException, UpdateError {
        HttpURLConnection c = open(url);
        try (InputStream in = c.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            for (int n; (n = in.read(buf)) > 0; ) {
                out.write(buf, 0, n);
                if (out.size() > max) throw new UpdateError("too_large", url + " is too large");
            }
            return out.toByteArray();
        } finally {
            c.disconnect();
        }
    }

    /** Stream url into file; returns its SHA-256 (hex). */
    private String download(String url, File file) throws IOException, UpdateError {
        HttpURLConnection c = open(url);
        long total = c.getContentLengthLong();
        if (total > MAX_APK_BYTES) throw new UpdateError("too_large", "The APK is too large");
        MessageDigest sha;
        try {
            sha = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new UpdateError("internal", "No SHA-256");
        }
        long done = 0, lastEmit = 0;
        try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(file)) {
            byte[] buf = new byte[64 * 1024];
            for (int n; (n = in.read(buf)) > 0; ) {
                done += n;
                if (done > MAX_APK_BYTES) throw new UpdateError("too_large", "The APK is too large");
                sha.update(buf, 0, n);
                out.write(buf, 0, n);
                long now = System.currentTimeMillis();
                if (now - lastEmit >= PROGRESS_EVERY_MS) {
                    lastEmit = now;
                    emitProgress(done, total);
                }
            }
        } finally {
            c.disconnect();
        }
        emitProgress(done, total > 0 ? total : done);
        return hex(sha.digest());
    }

    // ── Helpers ─────────────────────────────────────────────────────────────

    @SuppressWarnings("deprecation")
    static long ownVersionCode(Context context) {
        try {
            return versionCode(context.getPackageManager().getPackageInfo(context.getPackageName(), 0));
        } catch (PackageManager.NameNotFoundException e) {
            return 0;
        }
    }

    @SuppressWarnings("deprecation")
    private static long versionCode(PackageInfo info) {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? info.getLongVersionCode() : info.versionCode;
    }

    @SuppressWarnings("deprecation")
    private static Set<String> signerHashes(PackageInfo info) {
        Signature[] sigs;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            sigs = info.signingInfo != null ? info.signingInfo.getApkContentsSigners() : null;
        } else {
            sigs = info.signatures;
        }
        Set<String> out = new HashSet<>();
        if (sigs == null) return out;
        try {
            MessageDigest sha = MessageDigest.getInstance("SHA-256");
            for (Signature s : sigs) out.add(hex(sha.digest(s.toByteArray())));
        } catch (NoSuchAlgorithmException ignored) {
            // Every Android has SHA-256.
        }
        return out;
    }

    private static String hex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static void clearDir(File dir) {
        File[] files = dir.listFiles();
        if (files == null) return;
        for (File f : files) f.delete();
    }

    private void emit(String state) {
        send(event(state));
    }

    private void emitProgress(long done, long total) {
        JSONObject e = event("downloading");
        try {
            e.put("bytes", done);
            e.put("total", total);
            if (total > 0) e.put("progress", Math.min(1.0, (double) done / total));
        } catch (JSONException ignored) {
            // Plain values only.
        }
        send(e);
    }

    private void fail(String code, String message) {
        finish("failed", code, message);
    }

    /** End the update (installed, cancelled or failed) and let the next one start. */
    private void finish(String state, String code, String message) {
        File apk = readyApk;
        readyApk = null;
        readyTag = null;
        awaitingPermission = false;
        busy.set(false);
        if (apk != null && !"installed".equals(state)) apk.delete();
        JSONObject e = event(state);
        try {
            if (code != null) e.put("error", code);
            if (message != null) e.put("message", message);
        } catch (JSONException ignored) {
            // Plain values only.
        }
        send(e);
    }

    private static JSONObject event(String state) {
        JSONObject e = new JSONObject();
        try {
            e.put("state", state);
        } catch (JSONException ignored) {
            // Plain values only.
        }
        return e;
    }

    private void send(JSONObject e) {
        Listener l = listener;
        if (l != null) l.onUpdateEvent(e);
    }

    private static final class UpdateError extends Exception {
        private static final long serialVersionUID = 1L;
        final String code;

        UpdateError(String code, String message) {
            super(message);
            this.code = code;
        }
    }
}
