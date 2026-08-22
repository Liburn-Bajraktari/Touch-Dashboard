# Maintainer: Your Name <you@example.com>
pkgname=touch-dashboard
pkgver=0.1.2
pkgrel=1
pkgdesc="Touch-friendly FastAPI system dashboard with a PyWebView desktop wrapper"
arch=('any')
url="https://codeberg.org/liburnb/Touch-Dashboard"
license=('MIT')
depends=(
  'python'
  'python-fastapi'
  'uvicorn'
  'python-requests'
  'python-psutil'
  'python-spotipy'
  'python-pywebview'
  'python-pystray'
  'python-pillow'
  'python-evdev'
  'python-gobject'
  'webkit2gtk-4.1'
  'playerctl'
  'pipewire'
  'wireplumber'
  'upower'
)
optdepends=(
  'python-nvidia-ml-py: NVIDIA GPU telemetry'
  'speedtest-cli: dashboard speed test action'
  'pulseaudio: pactl compatibility command'
  'appmenu-gtk-module: suppress pywebview GTK menu warning'
)
source=("https://codeberg.org/liburnb/Touch-Dashboard/archive/v${pkgver}.tar.gz")
sha256sums=('3687056be13f67a441834aa067c747a3a27c8bd7d3abdb07e6eab440a8bcc933')

package() {
  cd "$srcdir/touch-dashboard"

  install -dm755 "$pkgdir/opt/touch-dashboard"
  install -Dm755 *.py "$pkgdir/opt/touch-dashboard/"
  cp -r templates static docs "$pkgdir/opt/touch-dashboard/"

  install -dm755 "$pkgdir/usr/bin"
  cat > "$pkgdir/usr/bin/touch-dashboard" <<'EOF'
#!/usr/bin/env bash
cd /opt/touch-dashboard
exec python /opt/touch-dashboard/server.py "$@"
EOF
  chmod 755 "$pkgdir/usr/bin/touch-dashboard"

  install -Dm644 touch-dashboard.desktop "$pkgdir/usr/share/applications/touch-dashboard.desktop"
  install -Dm644 static/icon-512.png "$pkgdir/usr/share/icons/hicolor/512x512/apps/touch-dashboard.png"
}
