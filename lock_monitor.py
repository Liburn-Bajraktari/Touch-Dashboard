"""
lock_monitor.py — PC session lock-state monitor.

Provides `LockMonitor`, a lightweight object with:
  • `get_state() -> bool`   — True when the host PC's session is locked.
  • `on_lock_change`        — callable(is_locked: bool) fired on every transition.
  • `start() / stop()`

Platform behaviour
──────────────────
Linux  : Event-driven D-Bus listener running a GLib.MainLoop on a daemon thread.

  KEY DESIGN NOTE: dbus-python's DBusGMainLoop dispatches signals on the
  *default* GLib main context. We must NOT use a private GLib.MainContext
  (as mouse_battery.py does) because that breaks session-bus signal delivery.
  Instead we run GLib.MainLoop() (default context) on a dedicated daemon
  thread. This is safe: we use a threading.Event to signal shutdown.

  Two buses are opened:
    SESSION bus — org.freedesktop.ScreenSaver.ActiveChanged
      Catches KDE Plasma, GNOME, Cinnamon, XFCE — all emit on this interface.
      path/bus_name left as None so both /ScreenSaver and
      /org/freedesktop/ScreenSaver paths are matched (KDE sends on both).

    SYSTEM bus — org.freedesktop.login1.Session LockedHint
      Works on i3/openbox and any DE that calls loginctl lock-session.
      On KDE Wayland this is NOT set, so it's supplementary only.

  OR rule: locked = screensaver_active OR logind_locked_hint.

  Confirmed on:
    • KDE Plasma 6 (Wayland) — session bus ActiveChanged ✓
    • KDE Plasma 5 (X11)     — session bus ActiveChanged ✓
    • GNOME (Wayland/X11)    — session bus ActiveChanged ✓
    • Cinnamon / XFCE        — session bus ActiveChanged ✓
    • i3 / tty (systemd)     — system bus LockedHint ✓

Windows: 2 Hz ctypes poll of WTSQuerySessionInformationW (no extra deps).
"""
from __future__ import annotations

import logging
import sys
import threading

logger = logging.getLogger(__name__)


# ─── Shared base ───────────────────────────────────────────────────────────────

class LockMonitor:
    """Abstract lock monitor — platform subclass is selected by `create()`."""

    def __init__(self) -> None:
        self._locked = False
        self._lock   = threading.Lock()
        self.on_lock_change: "callable[[bool], None] | None" = None

    def get_state(self) -> bool:
        with self._lock:
            return self._locked

    def _set_locked(self, value: bool) -> None:
        with self._lock:
            if value == self._locked:
                return
            self._locked = value
        cb = self.on_lock_change
        if cb is not None:
            try:
                cb(value)
            except Exception:
                logger.exception("lock_monitor: on_lock_change callback raised")

    def start(self) -> None:  # noqa: B027
        pass

    def stop(self) -> None:
        pass


# ─── Linux ─────────────────────────────────────────────────────────────────────

if sys.platform.startswith("linux"):

    _SS_IFACE     = "org.freedesktop.ScreenSaver"
    _LOGIND_BUS   = "org.freedesktop.login1"
    _LOGIND_PATH  = "/org/freedesktop/login1/session/auto"
    _LOGIND_IFACE = "org.freedesktop.login1.Session"
    _PROPS_IFACE  = "org.freedesktop.DBus.Properties"

    class LinuxLockMonitor(LockMonitor):
        """
        Runs GLib.MainLoop() (default context) on a dedicated daemon thread.
        Subscribes to:
          - org.freedesktop.ScreenSaver.ActiveChanged  (SESSION bus) — KDE/GNOME/…
          - org.freedesktop.login1 LockedHint           (SYSTEM bus)  — i3/openbox
        """

        def __init__(self) -> None:
            super().__init__()
            self._thread: threading.Thread | None = None
            self._loop         = None   # GLib.MainLoop
            self._stop_evt     = threading.Event()
            self._session_bus  = None
            self._system_bus   = None
            # Per-source state; combined with OR.
            self._ss_locked     = False
            self._logind_locked = False

        def start(self) -> None:
            self._thread = threading.Thread(
                target=self._run,
                name="lock-monitor",
                daemon=True,
            )
            self._thread.start()

        def stop(self) -> None:
            self._stop_evt.set()
            if self._loop is not None:
                try:
                    self._loop.quit()
                except Exception:
                    pass

        def _run(self) -> None:
            try:
                import dbus                       # type: ignore[import-untyped]
                import dbus.mainloop.glib         # type: ignore[import-untyped]
                from gi.repository import GLib   # type: ignore[import-untyped]

                context = GLib.MainContext.new()
                context.push_thread_default()

                # DBusGMainLoop MUST be installed before any Bus() call.
                dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

                self._session_bus = dbus.SessionBus()
                self._system_bus  = dbus.SystemBus()

                # Read current state before subscribing (avoids missing a
                # lock that happened before we started).
                self._read_initial_state()

                # Subscribe to signals.
                self._subscribe_screensaver()
                self._subscribe_logind()

                # Run the thread-default GLib main loop on this thread.
                self._loop = GLib.MainLoop.new(context, False)
                logger.info("lock-monitor: started (session+system D-Bus, thread-default GLib loop)")
                self._loop.run()
                context.pop_thread_default()

            except Exception:
                logger.exception(
                    "lock-monitor: D-Bus thread failed — lock detection unavailable"
                )

        # ── initial state ─────────────────────────────────────────────────────

        def _read_initial_state(self) -> None:
            self._read_screensaver_state()
            self._read_logind_state()

        def _read_screensaver_state(self) -> None:
            try:
                import dbus  # type: ignore[import-untyped]
                obj    = self._session_bus.get_object(_SS_IFACE, "/ScreenSaver")
                iface  = dbus.Interface(obj, _SS_IFACE)
                active = bool(iface.GetActive())
                self._ss_locked = active
                self._update(active, "screensaver/init")
            except Exception as exc:
                logger.debug("lock-monitor: screensaver GetActive failed: %s", exc)

        def _read_logind_state(self) -> None:
            try:
                import dbus  # type: ignore[import-untyped]
                obj   = self._system_bus.get_object(_LOGIND_BUS, _LOGIND_PATH)
                props = dbus.Interface(obj, _PROPS_IFACE)
                locked = bool(props.Get(_LOGIND_IFACE, "LockedHint"))
                self._logind_locked = locked
                self._update(locked, "logind/init")
            except Exception as exc:
                logger.debug("lock-monitor: logind LockedHint init failed: %s", exc)

        # ── subscriptions ─────────────────────────────────────────────────────

        def _subscribe_screensaver(self) -> None:
            """
            Subscribe to org.freedesktop.ScreenSaver.ActiveChanged on the session bus.
            path=None, bus_name=None → match any sender, any path.
            This catches KDE (/ScreenSaver and /org/freedesktop/ScreenSaver),
            GNOME (/org/gnome/ScreenSaver), Cinnamon, XFCE, etc.
            """
            try:
                self._session_bus.add_signal_receiver(
                    handler_function=self._on_screensaver_active,
                    signal_name="ActiveChanged",
                    dbus_interface=_SS_IFACE,
                    bus_name=None,
                    path=None,
                )
                logger.debug("lock-monitor: subscribed to ScreenSaver.ActiveChanged (session bus)")
            except Exception as exc:
                logger.warning("lock-monitor: screensaver subscription failed: %s", exc)

        def _subscribe_logind(self) -> None:
            try:
                self._system_bus.add_signal_receiver(
                    handler_function=self._on_logind_props_changed,
                    signal_name="PropertiesChanged",
                    dbus_interface=_PROPS_IFACE,
                    bus_name=_LOGIND_BUS,
                    path=_LOGIND_PATH,
                )
                logger.debug("lock-monitor: subscribed to logind LockedHint (system bus)")
            except Exception as exc:
                logger.warning("lock-monitor: logind subscription failed: %s", exc)

        # ── signal handlers ───────────────────────────────────────────────────

        def _on_screensaver_active(self, is_active: bool, *args, **kwargs) -> None:
            self._ss_locked = bool(is_active)
            self._update(self._ss_locked, "screensaver")

        def _on_logind_props_changed(
            self, interface: str, changed: dict, invalidated: list
        ) -> None:
            if "LockedHint" in changed:
                self._logind_locked = bool(changed["LockedHint"])
                self._update(self._logind_locked, "logind")

        # ── combine sources ───────────────────────────────────────────────────

        def _update(self, value: bool, source: str) -> None:
            combined = self._ss_locked or self._logind_locked
            logger.debug("lock-monitor: %s=%s → combined=%s", source, value, combined)
            self._set_locked(combined)


# ─── Windows ───────────────────────────────────────────────────────────────────

elif sys.platform.startswith("win"):
    import ctypes
    import ctypes.wintypes as _wt

    _WTS_SESSIONSTATE_LOCK = 0x00000001

    _WTSAPI32 = None
    try:
        _WTSAPI32 = ctypes.windll.wtsapi32  # type: ignore[attr-defined]
    except OSError:
        logger.warning("lock-monitor: wtsapi32.dll not available")

    class _WTSINFOEX_LEVEL1(ctypes.Structure):
        _fields_ = [
            ("SessionId",       _wt.ULONG),
            ("SessionFlags",    _wt.ULONG),
            ("WTSSessionState", _wt.ULONG),
            ("SessionName",     ctypes.c_wchar * 33),
        ]

    class _WTSINFOEX(ctypes.Structure):
        _fields_ = [
            ("Level", _wt.ULONG),
            ("Data",  _WTSINFOEX_LEVEL1),
        ]

    _WTSSessionInfoEx = 25

    def _query_locked() -> bool:
        if _WTSAPI32 is None:
            return False
        buf    = ctypes.c_void_p()
        bytes_ = _wt.DWORD()
        try:
            ok = _WTSAPI32.WTSQuerySessionInformationW(
                None,
                ctypes.c_ulong(0xFFFFFFFF),
                _WTSSessionInfoEx,
                ctypes.byref(buf),
                ctypes.byref(bytes_),
            )
            if not ok or not buf.value:
                return False
            info = ctypes.cast(buf, ctypes.POINTER(_WTSINFOEX)).contents
            return bool(info.Data.SessionFlags & _WTS_SESSIONSTATE_LOCK)
        except Exception as exc:
            logger.debug("lock-monitor: WTSQuerySessionInformation error: %s", exc)
            return False
        finally:
            try:
                if buf.value:
                    _WTSAPI32.WTSFreeMemory(buf)
            except Exception:
                pass

    class WindowsLockMonitor(LockMonitor):
        def __init__(self) -> None:
            super().__init__()
            self._stop_evt = threading.Event()
            self._thread: threading.Thread | None = None

        def start(self) -> None:
            self._locked = _query_locked()
            self._thread = threading.Thread(
                target=self._run, name="lock-monitor", daemon=True,
            )
            self._thread.start()
            logger.info("lock-monitor: Windows WTS lock poller started (2 Hz)")

        def stop(self) -> None:
            self._stop_evt.set()

        def _run(self) -> None:
            while not self._stop_evt.wait(timeout=0.5):
                try:
                    self._set_locked(_query_locked())
                except Exception:
                    logger.exception("lock-monitor: poll iteration failed")


# ─── Factory ───────────────────────────────────────────────────────────────────

def create() -> LockMonitor:
    if sys.platform.startswith("linux"):
        return LinuxLockMonitor()    # type: ignore[name-defined]
    elif sys.platform.startswith("win"):
        return WindowsLockMonitor()  # type: ignore[name-defined]
    else:
        logger.info("lock-monitor: unsupported platform — lock detection disabled")
        return LockMonitor()
