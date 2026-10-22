package dev.touchdashboard.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import java.io.File;
import java.io.IOException;

/**
 * Reopens the dashboard after an update the app started itself.
 *
 * Android delivers MY_PACKAGE_REPLACED to the new version once it is
 * installed. Starting an activity from here works where background starts are
 * allowed (Android 9 and older, or an exemption the device grants); elsewhere
 * Android refuses it quietly and the installer's own "Open" button is the way
 * back. A marker written just before the installer opens keeps installs from
 * anywhere else (adb, a file manager) from launching the app.
 */
public class PackageReplacedReceiver extends BroadcastReceiver {

    private static final String MARKER = "update-started";
    private static final long MARKER_MAX_AGE_MS = 15 * 60 * 1000;

    static void expectUpdate(Context context) {
        File marker = marker(context);
        try {
            if (!marker.createNewFile()) marker.setLastModified(System.currentTimeMillis());
        } catch (IOException ignored) {
            // No marker, no automatic reopen: the installer's Open button remains.
        }
    }

    static void forgetUpdate(Context context) {
        marker(context).delete();
    }

    private static File marker(Context context) {
        return new File(context.getNoBackupFilesDir(), MARKER);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction())) return;
        File marker = marker(context);
        boolean ours = marker.isFile() && System.currentTimeMillis() - marker.lastModified() < MARKER_MAX_AGE_MS;
        marker.delete();
        if (!ours) return;
        try {
            context.startActivity(new Intent(context, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP));
        } catch (RuntimeException ignored) {
            // A refused background start: the user opens the app from the installer.
        }
    }
}
