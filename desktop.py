"""
desktop.py — Desktop window and system tray for Touch Dashboard.

Uses pywebview (WebKit2GTK on Linux, edgechromium/WebView2 on Windows) for the
frameless dashboard window, and pystray for the system tray icon.

Designed to be imported by server.py; has no imports from server.py to avoid
circular dependencies — all server-side callbacks are passed as parameters.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import webbrowser
from typing import Callable

logger = logging.getLogger(__name__)

# Module-level window reference so /api/wakeup can bring it forward.
_active_window = None


def wakeup() -> None:
    """Show and focus the window (called by the /api/wakeup HTTP endpoint)."""
    if _active_window is not None:
        try:
            _active_window.show()
        except Exception as exc:
            logger.debug("wakeup show: %s", exc)


# ─── Window API (exposed to JS as window.pywebview.api.*) ──────────────────────

class DesktopApi:
    """
    Methods on this class are exposed to JS via window.pywebview.api.*.

    pywebview wraps every public method in a Promise on the JS side, so all
    methods can be called with .then() even if they return a plain value.
    """

    def __init__(self, local_ip: str, quit_fn: Callable[[], None]) -> None:
        self._local_ip = local_ip
        self._quit_fn = quit_fn
        self._minimize_to_tray = True
        self._window = None         # bound after create_window()
        self._tray: TrayManager | None = None

    def _bind(self, window, tray: TrayManager) -> None:
        """Called after window and tray are created."""
        self._window = window
        self._tray = tray

    # ── JS-callable methods ────────────────────────────────────────────────────

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def maximize(self) -> None:
        """Toggle fullscreen (matches existing HTML button behaviour)."""
        if self._window:
            self._window.toggle_fullscreen()

    def close(self) -> None:
        """
        Called when the user presses the custom close button in the HTML.
        Either hides to tray or fully quits, depending on the user's setting.
        """
        if self._minimize_to_tray and self._window:
            self._window.hide()
            if self._tray:
                self._tray.set_visible(False)
        else:
            self._quit_fn()

    def set_minimize_to_tray(self, val: bool) -> None:
        self._minimize_to_tray = bool(val)

    def get_local_ip(self) -> str:
        return self._local_ip

    def open_url(self, url: str) -> None:
        """Open a URL in the system default browser."""
        try:
            webbrowser.open(url)
        except Exception as exc:
            logger.warning("open_url failed for '%s': %s", url, exc)

    def start_drag(self) -> None:
        """
        No-op stub kept for HTML API compatibility.
        pywebview handles window dragging natively via easy_drag=True and the
        .pywebview-drag-region CSS class.
        """


# ─── System tray ──────────────────────────────────────────────────────────────

class TrayManager:
    """pystray-based system tray icon with show/hide/quit menu."""

    def __init__(
        self,
        icon_path: str,
        local_ip: str,
        show_fn: Callable[[], None],
        hide_fn: Callable[[], None],
        quit_fn: Callable[[], None],
    ) -> None:
        self._icon_path = icon_path
        self._local_ip = local_ip
        self._show = show_fn
        self._hide = hide_fn
        self._quit = quit_fn
        self._visible = True
        self._icon = None

    def set_visible(self, visible: bool) -> None:
        """Update internal state when window is hidden/shown programmatically."""
        self._visible = visible

    def show_window(self) -> None:
        self._show()
        self._visible = True

    def _toggle(self) -> None:
        if self._visible:
            self._hide()
            self._visible = False
        else:
            self._show()
            self._visible = True

    def _make_icon_image(self):
        from PIL import Image
        if os.path.exists(self._icon_path):
            try:
                return Image.open(self._icon_path).convert("RGBA")
            except Exception:
                pass
        # Fallback: accent-colour square
        return Image.new("RGBA", (64, 64), (14, 165, 233, 255))

    def _build_menu(self):
        import pystray

        def _toggle_label(item) -> str:
            return "Hide Dashboard" if self._visible else "Show Dashboard"

        return pystray.Menu(
            pystray.MenuItem(_toggle_label, lambda: self._toggle()),
            pystray.MenuItem(f"IP: {self._local_ip}", None, enabled=False),
            pystray.MenuItem("Quit", lambda: self._quit()),
        )

    def start(self) -> None:
        import pystray
        self._icon = pystray.Icon(
            "touch-dashboard",
            self._make_icon_image(),
            "Touch Dashboard",
            self._build_menu(),
        )
        self._icon.run_detached()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception as exc:
                logger.debug("Tray stop: %s", exc)
            self._icon = None


# ─── Windows helpers ───────────────────────────────────────────────────────────

def configure_windows_app_identity(app_user_model_id: str) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            app_user_model_id
        )
    except Exception as exc:
        logger.debug("AppUserModelID: %s", exc)





# ─── Single-instance guard ─────────────────────────────────────────────────────

_lock_file_handle = None


def _acquire_linux_lock(data_dir: str, port: int) -> bool:
    """
    Try to acquire the single-instance lockfile on Linux.
    Returns True if this process is the first instance.
    """
    global _lock_file_handle
    import fcntl
    lock_path = os.path.join(data_dir, f"server_{port}.lock")
    try:
        _lock_file_handle = open(lock_path, "w")
        fcntl.flock(_lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file_handle.write(str(os.getpid()))
        _lock_file_handle.flush()
        return True
    except OSError:
        return False


def _wake_existing_instance(port: int) -> None:
    """Ask an already-running instance to show its window via HTTP."""
    import urllib.request
    for _ in range(10):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/wakeup", timeout=0.5
            )
            return
        except Exception:
            time.sleep(0.5)


# ─── Main entry point ──────────────────────────────────────────────────────────

def launch_desktop(
    host: str,
    port: int,
    icon_path: str,
    data_dir: str,
    run_server_fn: Callable,
    stop_server_fn: Callable,
    wait_server_fn: Callable[[int, float], bool],
    get_ip_fn: Callable[[], str],
    app_user_model_id: str = "TouchDashboard.TouchDashboard",
) -> None:
    """
    Start the pywebview desktop window + pystray tray icon.
    Blocks the calling thread until the window is destroyed.

    Parameters
    ----------
    host / port          : uvicorn bind address
    icon_path            : absolute path to .ico / .png icon file
    data_dir             : user data directory (used for lockfile + storage)
    run_server_fn        : function(host, port) that starts uvicorn (blocking)
    stop_server_fn       : function() that signals uvicorn to exit
    wait_server_fn       : function(port, timeout) -> bool
    get_ip_fn            : function() -> LAN IP string
    app_user_model_id    : Windows AUMID for taskbar grouping
    """
    import webview

    global _active_window

    # ── Windows: single-instance via named mutex ───────────────────────────────
    if sys.platform.startswith("win"):
        import ctypes
        ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED = 183, 5
        mutex_name = f"Global\\TouchDashboard_Mutex_{port}"
        try:
            kernel32 = ctypes.windll.kernel32
            _mutex = kernel32.CreateMutexW(None, False, mutex_name)
            last_err = kernel32.GetLastError()
            if last_err in (ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED) or not _mutex:
                _wake_existing_instance(port)
                os._exit(0)
        except Exception as exc:
            logger.warning("Mutex creation failed: %s. Assuming duplicate.", exc)
            os._exit(0)

    # ── Linux: single-instance via lockfile ────────────────────────────────────
    elif sys.platform.startswith("linux"):
        if not _acquire_linux_lock(data_dir, port):
            _wake_existing_instance(port)
            os._exit(0)

    configure_windows_app_identity(app_user_model_id)

    # ── Start FastAPI server, then open window ─────────────────────────────────
    srv_thread = threading.Thread(
        target=run_server_fn,
        kwargs={"host": host, "port": port},
        daemon=True,
        name="fastapi",
    )
    srv_thread.start()
    if not wait_server_fn(port, 10.0):
        logger.error(
            "FastAPI did not start on port %d; window may show blank page.", port
        )

    local_ip = get_ip_fn()

    # ── Quit callback (idempotent via threading.Event) ─────────────────────────
    _quit_called = threading.Event()

    def _do_quit() -> None:
        if _quit_called.is_set():
            return
        _quit_called.set()
        stop_server_fn()
        try:
            window.destroy()
        except Exception:
            pass

    # ── Build API and window ───────────────────────────────────────────────────
    api = DesktopApi(local_ip=local_ip, quit_fn=_do_quit)

    window = webview.create_window(
        title=f"Touch Dashboard \u2014 {local_ip}",
        url=f"http://127.0.0.1:{port}",
        js_api=api,
        width=1280,
        height=800,
        frameless=True,
        transparent=True,
        easy_drag=True,
        background_color="#070b12",
    )
    _active_window = window

    # ── System tray ────────────────────────────────────────────────────────────
    tray = TrayManager(
        icon_path=icon_path,
        local_ip=local_ip,
        show_fn=window.show,
        hide_fn=window.hide,
        quit_fn=_do_quit,
    )
    api._bind(window=window, tray=tray)
    tray.start()

    # ── Start WebKit2GTK / WebView2 main loop (blocks until all windows gone) ──
    # Force GTK backend on Linux — pywebview auto-selects Qt on KDE which would
    # bring back Chromium via QWebEngineView, defeating the whole point.
    gui: str | None = "gtk" if sys.platform.startswith("linux") else None
    webview.start(gui=gui, private_mode=False, storage_path=data_dir, icon=icon_path)

    # ── Cleanup after GTK loop exits ───────────────────────────────────────────
    _active_window = None
    tray.stop()
    stop_server_fn()  # idempotent — safe to call again
