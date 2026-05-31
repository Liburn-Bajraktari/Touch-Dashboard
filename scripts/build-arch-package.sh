#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKGVER="$(grep '^pkgver=' "$ROOT_DIR/PKGBUILD" | cut -d= -f2)"
ARCHIVE="$ROOT_DIR/v${PKGVER}.tar.gz"
TMP_DIR="$(mktemp -d)"

if command -v paru >/dev/null 2>&1; then
  export PACMAN="paru"
  echo "==> Using paru as AUR helper"
elif command -v yay >/dev/null 2>&1; then
  export PACMAN="yay"
  echo "==> Using yay as AUR helper"
fi

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$TMP_DIR/touch-dashboard"
cp "$ROOT_DIR/server.py" "$TMP_DIR/touch-dashboard/"
cp "$ROOT_DIR/touch-dashboard.desktop" "$TMP_DIR/touch-dashboard/"
cp -r "$ROOT_DIR/templates" "$TMP_DIR/touch-dashboard/"
cp -r "$ROOT_DIR/static" "$TMP_DIR/touch-dashboard/"

echo "==> Creating local source archive..."
tar -C "$TMP_DIR" -czf "$ARCHIVE" "touch-dashboard"
cd "$ROOT_DIR"

echo "==> Updating local checksums..."
updpkgsums

makepkg -f "$@"
