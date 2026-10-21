# Building and signing the Android app

The Android app lives in `android/`: a manifest, resources and a few Java classes (`MainActivity`, the WebView shell; `AppUpdater` and `InstallResultReceiver`, the self-updater). `scripts/build-android.sh` builds it with the Android SDK's own tools, without Gradle, and `scripts/release.sh --apk` runs that script for every release.

## What you need

- A JDK (`javac`); any recent one works, the code is compiled to Java 11 bytecode.
- The Android SDK with two packages, under `~/Android/Sdk` (or set `ANDROID_SDK`):
  - `platforms;android-35` (`platform-35_r02.zip`)
  - `build-tools;35.0.0` (`build-tools_r35_linux.zip`)

  With `sdkmanager`: `sdkmanager "platforms;android-35" "build-tools;35.0.0"`. Without it, unpack the two zips from `https://dl.google.com/android/repository/` into `~/Android/Sdk/platforms/android-35` and `~/Android/Sdk/build-tools/35.0.0`. Both are covered by the Android SDK License Agreement.
- The release key (below).

## Build

```bash
./scripts/build-android.sh v1.2.3                  # writes TouchDashboard-v1.2.3.apk
./scripts/build-android.sh v1.2.3 /tmp/test.apk    # somewhere else
```

The script bundles `templates/index.html` and `static/` as `assets/www`, stamps the version into the page, sets `versionName` to `1.2.3` and `versionCode` to `major * 1000000 + minor * 1000 + patch` (v1.2.3 → 1002003), then compiles, dexes, aligns, signs (APK Signature Scheme v2 and v3) and verifies the APK.

Check a build:

```bash
~/Android/Sdk/build-tools/35.0.0/aapt2 dump badging TouchDashboard-v1.2.3.apk | head -3
~/Android/Sdk/build-tools/35.0.0/apksigner verify --print-certs TouchDashboard-v1.2.3.apk
```

## The release key

Every APK is signed with one private key. Android installs an update only if it carries the same key, and the app's updater checks it too, so **this key decides who can ship updates to every tablet.**

- It lives outside the repo, in `~/.config/touch-dashboard/`:
  - `android-release.jks`: the keystore (PKCS12, alias `touch-dashboard`, RSA 4096, valid until 2056).
  - `android-release.properties`: `storeFile`, `storePassword`, `keyAlias`, `keyPassword`. Both files are mode 600.
- `scripts/build-android.sh` reads the properties file (or the one named by `ANDROID_SIGNING`).
- **Back up both files** somewhere safe and offline, for example an encrypted password manager entry or an encrypted USB drive. Never commit them or attach them anywhere public.
- **If the key is lost**, no further update can be installed over the existing app: every tablet has to uninstall it, install a build signed with a new key, and pair again. That is exactly what happened once, in the move away from the old shared debug key.
- If it **leaks**, anyone can build an APK Android accepts as an update. Make a new key, release with it, and treat it like a lost key.

The certificate's SHA-256 fingerprint, to compare against `apksigner verify --print-certs`:

```bash
keytool -list -v -keystore ~/.config/touch-dashboard/android-release.jks | grep SHA256
```

### Making a new key

Only when starting over (it forces the reinstall described above):

```bash
keytool -genkeypair -keystore ~/.config/touch-dashboard/android-release.jks -storetype PKCS12 \
  -alias touch-dashboard -keyalg RSA -keysize 4096 -validity 11000 -dname "CN=Touch Dashboard"
```

Then write `~/.config/touch-dashboard/android-release.properties` with the four keys above and `chmod 600` both files.

## How self-update works

1. The PC's `config_sync` carries `update_apk_url` (the newest published, non-pre-release release with an APK) and `req_apk_version`.
2. The page compares them with the app's version (`window.TDAndroid.info()`, also in the user agent as `TouchDashboard/x.y.z`) and offers or requires the update.
3. `TDAndroid.update(tag)` hands only the tag to the app. `AppUpdater` downloads `SHA256SUMS` and `TouchDashboard-<tag>.apk` from this repository's release over HTTPS, checks the checksum, the package name, that the version is newer and that the signing certificate matches its own.
4. If Android does not yet let the app install packages, it opens that switch ("Install unknown apps") and waits for the user to come back.
5. It writes the APK into a `PackageInstaller` session and commits it; Android shows its update dialog (always: a silent update would close the dashboard mid-use), installs the update and closes the app.

## Testing on a device

```bash
adb install -r TouchDashboard-v1.2.3.apk        # same key: installs over the existing app
adb logcat -s AndroidRuntime PackageInstaller   # if something fails
```

An app signed with the old debug key must be uninstalled first (`adb uninstall dev.touchdashboard.app`), which also deletes its pairing.
