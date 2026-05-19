#!/usr/bin/env bash
set -euo pipefail

BUILD_TYPE="${1:-debug}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d node_modules ]; then
  npm install
fi

npm run prepare:capacitor

if [ ! -d android ]; then
  npx cap add android
fi

npx cap sync android

cd android
if [ "$BUILD_TYPE" = "release" ]; then
  ./gradlew assembleRelease
else
  ./gradlew assembleDebug
fi
