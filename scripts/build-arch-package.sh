#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKGVER="$(grep '^pkgver=' "$ROOT_DIR/PKGBUILD" | cut -d= -f2)"
ARCHIVE="$ROOT_DIR/touch-dashboard-$PKGVER.tar.gz"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$TMP_DIR/touch-dashboard-$PKGVER"
cp "$ROOT_DIR/server.py" "$TMP_DIR/touch-dashboard-$PKGVER/"
cp "$ROOT_DIR/touch-dashboard.desktop" "$TMP_DIR/touch-dashboard-$PKGVER/"
cp -r "$ROOT_DIR/templates" "$TMP_DIR/touch-dashboard-$PKGVER/"
cp -r "$ROOT_DIR/static" "$TMP_DIR/touch-dashboard-$PKGVER/"

tar -C "$TMP_DIR" -czf "$ARCHIVE" "touch-dashboard-$PKGVER"
cd "$ROOT_DIR"
makepkg -f "$@"
