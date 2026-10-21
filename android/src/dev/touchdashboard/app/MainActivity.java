package dev.touchdashboard.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONException;
import org.json.JSONObject;

/**
 * A full-screen WebView around the bundled dashboard.
 *
 * The page stays at file:///android_asset/www/index.html: its localStorage
 * (the PC's address, the pairing token) belongs to that origin, so it must not
 * change between app versions. The page reaches the PC over ws:// and http://,
 * which is why file:// pages get universal access. The TDAndroid bridge is the
 * page's way to the updater; only the bundled page may use it.
 */
public class MainActivity extends Activity implements AppUpdater.Listener {

    static final String PAGE = "file:///android_asset/www/index.html";
    private static final String ASSET_PREFIX = "file:///android_asset/";

    private WebView webView;
    private AppUpdater updater;
    /** Updated on the UI thread; the bridge (a binder thread) only reads it. */
    private volatile String currentUrl = "";

    // File-URL access is deprecated, but the page must stay a file:// page (see
    // above) and reach the PC from there; an https asset origin would make
    // every ws:// connection to the PC mixed content.
    @SuppressWarnings("deprecation")
    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        updater = new AppUpdater(this, this);

        webView = new WebView(this);
        webView.setBackgroundColor(0xFF000000);
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(false);
        // The file:// page talks to the PC (ws://, http:// cover art).
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportMultipleWindows(false);
        // The page reads its own version from here (showApkUpdateIfNeeded).
        s.setUserAgentString(s.getUserAgentString() + " TouchDashboard/" + versionName());

        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new ShellClient());
        webView.addJavascriptInterface(new Bridge(), "TDAndroid");
        setContentView(webView);

        if (savedInstanceState == null || webView.restoreState(savedInstanceState) == null) {
            webView.loadUrl(PAGE);
        }
        currentUrl = PAGE;
        enterFullScreen();
    }

    @Override
    protected void onResume() {
        super.onResume();
        webView.onResume();
        updater.onHostResumed();
    }

    @Override
    protected void onPause() {
        webView.onPause();
        super.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        webView.saveState(outState);
    }

    @Override
    protected void onDestroy() {
        updater.detach();
        webView.removeJavascriptInterface("TDAndroid");
        webView.destroy();
        super.onDestroy();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) enterFullScreen();
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    /** AppUpdater.Listener: forward an update event to the page. */
    @Override
    public void onUpdateEvent(JSONObject event) {
        runOnUiThread(() -> {
            if (webView == null) return;
            webView.evaluateJavascript(
                    "window.tdAppUpdate && window.tdAppUpdate(" + event + ")", null);
        });
    }

    @SuppressWarnings("deprecation")
    private void enterFullScreen() {
        Window w = getWindow();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            w.getAttributes().layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            w.setDecorFitsSystemWindows(false);
            WindowInsetsController c = w.getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            w.getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                            | View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
    }

    String versionName() {
        try {
            PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            return info.versionName != null ? info.versionName : "0";
        } catch (PackageManager.NameNotFoundException e) {
            return "0";
        }
    }

    /** Only the bundled page stays in the app; other links open in the browser. */
    private final class ShellClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            String url = request.getUrl().toString();
            if (url.startsWith(ASSET_PREFIX)) return false;
            String scheme = request.getUrl().getScheme();
            if ("http".equals(scheme) || "https".equals(scheme)) {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl())
                            .addCategory(Intent.CATEGORY_BROWSABLE));
                } catch (ActivityNotFoundException ignored) {
                    // No browser: stay on the dashboard.
                }
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
            currentUrl = url != null ? url : "";
        }
    }

    /** window.TDAndroid. Called on a binder thread; refuses pages outside the app. */
    private final class Bridge {
        private boolean trusted() {
            return currentUrl.startsWith(ASSET_PREFIX);
        }

        /** {"version": "0.11.1", "versionCode": 11001, "sdk": 34, "selfUpdate": true} */
        @JavascriptInterface
        public String info() {
            JSONObject o = new JSONObject();
            try {
                o.put("version", versionName());
                o.put("versionCode", AppUpdater.ownVersionCode(MainActivity.this));
                o.put("sdk", Build.VERSION.SDK_INT);
                o.put("selfUpdate", true);
            } catch (JSONException ignored) {
                // Plain values only.
            }
            return o.toString();
        }

        /** Download, check and install release `tag` (vX.Y.Z) of this app. */
        @JavascriptInterface
        public void update(String tag) {
            if (!trusted()) return;
            updater.start(tag);
        }

        /** Open this app's "Install unknown apps" switch (the first update asks for it). */
        @JavascriptInterface
        public void openInstallSettings() {
            if (!trusted()) return;
            runOnUiThread(() -> updater.openInstallPermissionSettings());
        }

        @JavascriptInterface
        public void openUrl(String url) {
            if (!trusted() || url == null || !(url.startsWith("https://") || url.startsWith("http://"))) return;
            runOnUiThread(() -> {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (ActivityNotFoundException ignored) {
                    // No browser installed.
                }
            });
        }
    }
}
