package dev.touchdashboard.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;

/**
 * PackageInstaller's answer to a committed update session. Not exported: it is
 * reached only through the PendingIntent AppUpdater hands to the session.
 */
public class InstallResultReceiver extends BroadcastReceiver {
    @SuppressWarnings("deprecation")
    @Override
    public void onReceive(Context context, Intent intent) {
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        String message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
        Intent confirm = status == PackageInstaller.STATUS_PENDING_USER_ACTION
                ? (Intent) intent.getParcelableExtra(Intent.EXTRA_INTENT) : null;
        AppUpdater.onInstallStatus(context, status, message, confirm);
    }
}
