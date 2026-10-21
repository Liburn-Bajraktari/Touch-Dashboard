#!/usr/bin/env bash
# Build the Android app from android/ with the SDK's own tools (no Gradle):
# aapt2 (resources + manifest) → javac → d8 → zipalign → apksigner.
#
#   ./scripts/build-android.sh v1.2.3 [out.apk]
#
# The APK bundles templates/index.html and static/ as assets/www, stamped
# with the version. versionCode is derived from the tag (major*1000000 +
# minor*1000 + patch), so every release installs over the one before it.
#
# Needs: JDK (javac), the Android SDK with platforms;android-35 and
# build-tools;35.0.0 (ANDROID_SDK, default ~/Android/Sdk), and the release key
# described by ANDROID_SIGNING (default ~/.config/touch-dashboard/android-release.properties:
# storeFile, storePassword, keyAlias, keyPassword). The key never enters the repo;
# losing it means every tablet has to uninstall and reinstall the app.

set -euo pipefail

VERSION="${1:-}"
if [[ ! "$VERSION" =~ ^v([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    echo "usage: $0 vMAJOR.MINOR.PATCH [out.apk]" >&2
    exit 1
fi
VERSION_CODE=$(( 10#${BASH_REMATCH[1]} * 1000000 + 10#${BASH_REMATCH[2]} * 1000 + 10#${BASH_REMATCH[3]} ))
VERSION_NAME="${VERSION#v}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(realpath -m "${2:-$ROOT/TouchDashboard-$VERSION.apk}")"
SDK="${ANDROID_SDK:-${ANDROID_HOME:-$HOME/Android/Sdk}}"
BUILD_TOOLS="$SDK/build-tools/35.0.0"
ANDROID_JAR="$SDK/platforms/android-35/android.jar"
SIGNING="${ANDROID_SIGNING:-$HOME/.config/touch-dashboard/android-release.properties}"

fail() { echo "build-android: $*" >&2; exit 1; }
for tool in aapt2 d8 zipalign apksigner; do
    [ -x "$BUILD_TOOLS/$tool" ] || fail "missing $BUILD_TOOLS/$tool (install build-tools;35.0.0)"
done
[ -f "$ANDROID_JAR" ] || fail "missing $ANDROID_JAR (install platforms;android-35)"
command -v javac >/dev/null || fail "javac not found (install a JDK)"
[ -f "$SIGNING" ] || fail "missing signing properties $SIGNING"

prop() { sed -n "s/^$1=//p" "$SIGNING" | head -n 1; }
# Newer JDKs warn that apksigner's crypto library loads native code; noise only.
quiet_jdk() { grep -v -e '^WARNING: .*restricted method' -e '^WARNING: java.lang.System::loadLibrary' \
    -e '^WARNING: Use --enable-native-access' -e '^WARNING: Restricted methods will be blocked' -e '^$' || true; }
STORE_FILE="$(prop storeFile)"; KEY_ALIAS="$(prop keyAlias)"
[ -f "$STORE_FILE" ] || fail "keystore not found: $STORE_FILE"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
cd "$ROOT"

echo "Building TouchDashboard $VERSION (versionCode $VERSION_CODE)..."

# 1. The dashboard, as the app's assets.
mkdir -p "$BUILD/assets/www"
cp templates/index.html "$BUILD/assets/www/index.html"
cp -r static "$BUILD/assets/www/"
sed -i "s/const APK_VERSION = \"dev\";/const APK_VERSION = \"$VERSION\";/" "$BUILD/assets/www/index.html"
grep -q "const APK_VERSION = \"$VERSION\";" "$BUILD/assets/www/index.html" \
    || fail "could not stamp APK_VERSION into index.html"

# 2. Resources and manifest.
"$BUILD_TOOLS/aapt2" compile --dir android/res -o "$BUILD/res.zip"
"$BUILD_TOOLS/aapt2" link -o "$BUILD/unsigned.apk" \
    -I "$ANDROID_JAR" \
    --manifest android/AndroidManifest.xml \
    --min-sdk-version 24 --target-sdk-version 35 \
    --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" \
    -A "$BUILD/assets" \
    "$BUILD/res.zip"

# 3. Code: Java 11 bytecode against android.jar, dexed for API 24+.
mkdir -p "$BUILD/classes" "$BUILD/dex"
javac -encoding UTF-8 --release 11 -nowarn -Xlint:-options \
    -classpath "$ANDROID_JAR" -d "$BUILD/classes" \
    $(find android/src -name '*.java')
"$BUILD_TOOLS/d8" --release --min-api 24 --lib "$ANDROID_JAR" \
    --output "$BUILD/dex" $(find "$BUILD/classes" -name '*.class')
(cd "$BUILD/dex" && zip -q -X "$BUILD/unsigned.apk" classes.dex)

# 4. Align, sign (v2 + v3; minSdk 24 needs no v1), verify.
"$BUILD_TOOLS/zipalign" -p -f 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"
TD_KS_PASS="$(prop storePassword)" TD_KEY_PASS="$(prop keyPassword)" \
    "$BUILD_TOOLS/apksigner" sign \
        --ks "$STORE_FILE" --ks-key-alias "$KEY_ALIAS" \
        --ks-pass env:TD_KS_PASS --key-pass env:TD_KEY_PASS \
        --v1-signing-enabled false --v2-signing-enabled true --v3-signing-enabled true \
        --out "$OUT" "$BUILD/aligned.apk" 2>&1 | quiet_jdk
"$BUILD_TOOLS/apksigner" verify "$OUT" 2>&1 | quiet_jdk

echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
