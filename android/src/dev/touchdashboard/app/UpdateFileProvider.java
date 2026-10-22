package dev.touchdashboard.app;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;
import java.util.regex.Pattern;

/**
 * Shares a downloaded update with Android's package installer, read-only.
 *
 * Not exported: the installer reaches a file only through the read grant on
 * AppUpdater's install intent, and only a release APK in cache/updates exists
 * here (content://dev.touchdashboard.app.updates/TouchDashboard-vX.Y.Z.apk).
 */
public class UpdateFileProvider extends ContentProvider {

    static final String AUTHORITY = "dev.touchdashboard.app.updates";
    static final String APK_TYPE = "application/vnd.android.package-archive";
    private static final Pattern NAME = Pattern.compile("TouchDashboard-v\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.apk");

    static Uri uriFor(File apk) {
        return new Uri.Builder().scheme("content").authority(AUTHORITY).appendPath(apk.getName()).build();
    }

    private File fileFor(Uri uri) throws FileNotFoundException {
        String name = uri.getLastPathSegment();
        if (uri.getPathSegments().size() != 1 || name == null || !NAME.matcher(name).matches()) {
            throw new FileNotFoundException("not an update: " + uri);
        }
        File file = new File(new File(getContext().getCacheDir(), "updates"), name);
        if (!file.isFile()) throw new FileNotFoundException(name);
        return file;
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public String getType(Uri uri) {
        return APK_TYPE;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"r".equals(mode)) throw new SecurityException("read-only");
        return ParcelFileDescriptor.open(fileFor(uri), ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        File file;
        try {
            file = fileFor(uri);
        } catch (FileNotFoundException e) {
            return null;
        }
        MatrixCursor cursor = new MatrixCursor(new String[] {OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        cursor.addRow(new Object[] {file.getName(), file.length()});
        return cursor;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("read-only");
    }
}
