# Android Release Signing Guide

Touch Dashboard's release APK must be signed before it can be installed on Android devices or published to the Play Store. The debug APK is auto-signed by Gradle and works for development and sideloading.

## Quick start

### 1. Generate a keystore (one-time setup)

```bash
keytool -genkeypair \
  -alias touch-dashboard \
  -keyalg RSA \
  -keysize 2048 \
  -validity 10000 \
  -keystore touch-dashboard-release.jks
```

Keep `touch-dashboard-release.jks` **secret** and **backed up**. Loss of the keystore means you cannot publish updates to the same app listing.

### 2. Set environment variables before building

```bash
export KEYSTORE_PATH=/absolute/path/to/touch-dashboard-release.jks
export KEYSTORE_PASS=your_keystore_password
export KEY_ALIAS=touch-dashboard
export KEY_PASS=your_key_password

./scripts/build-android.sh release
```

### 3. CI / secrets management

In CI pipelines (e.g. Woodpecker, GitHub Actions) store the keystore and passwords as **secrets**, not in the repository. Decode the base64-encoded keystore at build time:

```bash
echo "$KEYSTORE_BASE64" | base64 -d > release.jks
export KEYSTORE_PATH="$(pwd)/release.jks"
```

## Gradle integration (advanced)

If you prefer to configure signing permanently in Gradle, edit `android/app/build.gradle`:

```groovy
android {
    signingConfigs {
        release {
            storeFile     file(System.getenv("KEYSTORE_PATH") ?: "release.jks")
            storePassword System.getenv("KEYSTORE_PASS") ?: ""
            keyAlias      System.getenv("KEY_ALIAS")     ?: "touch-dashboard"
            keyPassword   System.getenv("KEY_PASS")      ?: ""
        }
    }
    buildTypes {
        release {
            signingConfig signingConfigs.release
            minifyEnabled false
            shrinkResources false
        }
    }
}
```

## Verify the signed APK

```bash
apksigner verify --verbose android/app/build/outputs/apk/release/app-release.apk
```

## Install on a device over USB (sideload)

```bash
adb install android/app/build/outputs/apk/release/app-release.apk
```
