#!/usr/bin/env bash
# build-android.sh
# Touch Dashboard — Android APK build script
#
# Usage:
#   ./scripts/build-android.sh [debug|release]
#
# Builds the Capacitor Android APK.
# The backend (server.py) must still run on the desktop; the APK is client-only.
#
# Release signing:
#   To build a signed release APK, set the following environment variables
#   OR create android/local.properties with the values:
#     KEYSTORE_PATH  — absolute path to your .jks / .keystore file
#     KEYSTORE_PASS  — keystore password
#     KEY_ALIAS      — key alias inside the keystore
#     KEY_PASS       — key password
#   If these are not set, assembleRelease will produce an unsigned APK.
#   See docs/android-signing.md for a full setup guide.

set -euo pipefail

BUILD_TYPE="${1:-debug}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}") /.." && pwd)"
cd "$ROOT_DIR"

# ── Validate tools ──────────────────────────────────────────────────────────

echo "=== Checking prerequisites ==="

# Java (required by Gradle)
if ! command -v java &>/dev/null; then
    echo "ERROR: Java not found. Install JDK 17 or 21 (required by Gradle)." >&2
    exit 1
fi
echo "  Java:    $(java -version 2>&1 | head -1)"

# ── Prepare web assets ─────────────────────────────────────────────────────

echo ""
echo "=== Preparing web assets ==="

# 1. Rebuild www/ directory
rm -rf www
mkdir -p www
cp templates/index.html www/
cp -r static www/

# 2. Android network security config
NET_SEC_DIR="android/app/src/main/res/xml"
mkdir -p "$NET_SEC_DIR"
cat << 'EOF' > "$NET_SEC_DIR/network_security_config.xml"
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="true">
        <trust-anchors>
            <certificates src="system"/>
        </trust-anchors>
    </base-config>
</network-security-config>
EOF
echo "✓ Wrote network_security_config.xml"

echo ""
echo "=== Syncing web assets to Android ==="
ASSETS_DIR="android/app/src/main/assets/www"
mkdir -p "$ASSETS_DIR"
cp -r www/* "$ASSETS_DIR"/

# ── Configure release signing if env vars are set ─────────────────────────

if [ "$BUILD_TYPE" = "release" ]; then
    SIGNING_PROPS="android/signing.properties"
    if [ -n "${KEYSTORE_PATH:-}" ] && [ -n "${KEYSTORE_PASS:-}" ] && \
       [ -n "${KEY_ALIAS:-}" ] && [ -n "${KEY_PASS:-}" ]; then
        echo ""
        echo "=== Writing signing.properties ==="
        cat > "$SIGNING_PROPS" <<EOF
storeFile=${KEYSTORE_PATH}
storePassword=${KEYSTORE_PASS}
keyAlias=${KEY_ALIAS}
keyPassword=${KEY_PASS}
EOF
        echo "  Signing config written to $SIGNING_PROPS"
    else
        echo ""
        echo "WARNING: Release signing env vars not set (KEYSTORE_PATH, KEYSTORE_PASS, KEY_ALIAS, KEY_PASS)."
        echo "         The release APK will be unsigned and cannot be installed on devices directly."
        echo "         See docs/android-signing.md for the signing setup guide."
    fi
fi

# ── Build APK ──────────────────────────────────────────────────────────────

echo ""
echo "=== Building $BUILD_TYPE APK ==="
cd android

if [ "$BUILD_TYPE" = "release" ]; then
    ./gradlew assembleRelease --no-daemon --quiet
    APK_PATH="app/build/outputs/apk/release/app-release.apk"
    APK_UNSIGNED="app/build/outputs/apk/release/app-release-unsigned.apk"
else
    ./gradlew assembleDebug --no-daemon --quiet
    APK_PATH="app/build/outputs/apk/debug/app-debug.apk"
    APK_UNSIGNED=""
fi

cd "$ROOT_DIR"

# ── Report output ──────────────────────────────────────────────────────────

echo ""
echo "=== Build complete ==="
FULL_PATH="android/$APK_PATH"
if [ -f "$FULL_PATH" ]; then
    SIZE=$(du -h "$FULL_PATH" | cut -f1)
    echo "  APK: $FULL_PATH  ($SIZE)"
elif [ -n "$APK_UNSIGNED" ] && [ -f "android/$APK_UNSIGNED" ]; then
    SIZE=$(du -h "android/$APK_UNSIGNED" | cut -f1)
    echo "  APK (unsigned): android/$APK_UNSIGNED  ($SIZE)"
    echo "  Sign with: apksigner sign --ks <keystore> android/$APK_UNSIGNED"
else
    echo "  APK output not found at expected path. Check Gradle output above."
fi
