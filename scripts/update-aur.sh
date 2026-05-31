#!/usr/bin/env bash
set -euo pipefail

# This script prepares the AUR metadata (checksums and .SRCINFO)

echo "==> Updating checksums in PKGBUILD..."
# updpkgsums automatically downloads the sources in PKGBUILD and updates sha256sums
updpkgsums

echo "==> Generating .SRCINFO..."
makepkg --printsrcinfo > .SRCINFO

echo "==> AUR metadata updated successfully!"
