"""
lock_monitor.py — PC session lock-state monitor.

Provides `LockMonitor`, a lightweight object with:
  • `get_state() -> bool`   — True when the host PC's session is locked.
  • `on_lock_change`        — callable(is_locked: bool) fired on every transition.
  • `start() / stop()`

Platform behaviour
──────────────────
Linux  : Event-driven D-Bus listener on a private GLib.MainContext daemon thread.
         Primary source  : org.freedesktop.login1 Session LockedHint
         Fallback source : org.gnome.ScreenSaver  ActiveChanged  (GNOME, Cinnamon)
                           org.kde.screensaver    ActiveChanged  (Plasma)
         All three are subscribed simultaneously; the OR of their signals wins.
         Zero CPU at idle — purely signal-driven.

Windows: Lightweight 2 Hz ctypes poll of WTSQuerySessionInformationW.
         Detects WTS_SESSIONSTATE_LOCK flag in WTSSessionInfoEx (no extra deps).
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
        self._locked  = False
        self._lock    = threading.Lock()
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


# ─── Linux — D-Bus (org.freedesktop.login1 + ScreenSaver fallbacks) ───────────

if sys.platform.startswith("linux"):

    _LOGIND_BUS   = "org.freedesktop.login1"
    _LOGIND_PATH  = "/org/freedesktop/login1/session/auto"
    _LOGIND_IFACE = "org.freedesktop.login1.Session"
    _PROPS_IFACE  = "org.freedesktop.DBus.Properties"

    _GNOME_BUS    = "org.gnome.ScreenSaver"
    _GNOME_PATH   = "/org/gnome/ScreenSaver"
    _GNOME_IFACE  = "org.gnome.ScreenSaver"

    _KDE_BUS      = "org.kde.screensaver"
    _KDE_PATH     = "/ScreenSaver"
    _KDE_IFACE    = "org.freedesktop.ScreenSaver"

    class LinuxLockMonitor(LockMonitor):
        """
        Subscribes to loginctl LockedHint + GNOME/KDE screensaver signals
        on a private GLib.MainContext daemon thread.
        """

        def __init__(self) -> None:
            super().__init__()
            self._thread: threading.Thread | None = None
            self._loop   = None  # GLib.MainLoop
            self._bus    = None  # dbus.SystemBus

        def start(self) -> None:
            self._thread = threading.Thread(
                target=self._run,
                name="lock-monitor",
                daemon=True,
            )
            self._thread.start()

        def stop(self) -> None:
            if self._loop is not None:
                try:
                    self._loop.quit()
                except Exception:
                    pass

        def _run(self) -> None:
            """Entry point for the daemon thread."""
            try:
                import dbus                         # type: ignore[import-untyped]
                import dbus.mainloop.glib           # type: ignore[import-untyped]
                from gi.repository import GLib     # type: ignore[import-untyped]

                context = GLib.MainContext.new()
                context.push_thread_default()

                dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

                self._bus  = dbus.SystemBus()
                self._loop = GLib.MainLoop.new(context, False)

                self._read_initial_state()
                self._subscribe_logind()
                self._subscribe_screensaver_signals()

                logger.info("lock-monitor: D-Bus session lock monitor started")
                self._loop.run()
                context.pop_thread_default()

            except Exception:
                logger.exception("lock-monitor: D-Bus thread failed — lock detection unavailable")

        def _read_initial_state(self) -> None:
            """Synchronously read LockedHint before subscribing to signals."""
            try:
                import dbus  # type: ignore[import-untyped]
                obj   = self._bus.get_object(_LOGIND_BUS, _LOGIND_PATH)
                props = dbus.Interface(obj, _PROPS_IFACE)
                locked = bool(props.Get(_LOGIND_IFACE, "LockedHint"))
                self._set_locked(locked)
                logger.debug("lock-monitor: initial LockedHint=%s", locked)
            except Exception as exc:
                logger.debug("lock-monitor: cannot read initial LockedHint: %s", exc)

        def _subscribe_logind(self) -> None:
            """Subscribe to PropertiesChanged on the loginctl Session object."""
            try:
                self._bus.add_signal_receiver(
                    handler_function=self._on_logind_props_changed,
                    signal_name="PropertiesChanged",
                    dbus_interface=_PROPS_IFACE,
                    bus_name=_LOGIND_BUS,
                    path=_LOGIND_PATH,
                )
                logger.debug("lock-monitor: subscribed to loginctl LockedHint")
            except Exception as exc:
                logger.warning("lock-monitor: failed to subscribe to loginctl: %s", exc)

        def _subscribe_screensaver_signals(self) -> None:
            """Subscribe to GNOME and KDE screensaver ActiveChanged signals."""
            for (bus_name, path, iface, label) in [
                (_GNOME_BUS, _GNOME_PATH, _GNOME_IFACE, "GNOME"),
                (_KDE_BUS,   _KDE_PATH,   _KDE_IFACE,   "KDE"),
            ]:
                try:
                    self._bus.add_signal_receiver(
                        handler_function=self._on_screensaver_active,
                        signal_name="ActiveChanged",
                        dbus_interface=iface,
                        bus_name=bus_name,
                        path=path,
                    )
                    logger.debug("lock-monitor: subscribed to %s screensaver", label)
                except Exception as exc:
                    logger.debug("lock-monitor: %s screensaver unavailable: %s", label, exc)

        def _on_logind_props_changed(
            self, interface: str, changed: dict, invalidated: list
        ) -> None:
            if "LockedHint" in changed:
                locked = bool(changed["LockedHint"])
                logger.debug("lock-monitor: loginctl LockedHint → %s", locked)
                self._set_locked(locked)

        def _on_screensaver_active(self, is_active: bool) -> None:
            locked = bool(is_active)
            logger.debug("lock-monitor: screensaver ActiveChanged → %s", locked)
            self._set_locked(locked)


# ─── Windows — WTS session query (ctypes, 2 Hz poll) ──────────────────────────

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

    _WTSSessionInfoEx = 25  # WTS_INFO_CLASS enum value

    def _query_locked() -> bool:
        """Return True if the current Windows session is locked."""
        if _WTSAPI32 is None:
            return False
        buf    = ctypes.c_void_p()
        bytes_ = _wt.DWORD()
        try:
            ok = _WTSAPI32.WTSQuerySessionInformationW(
                None,
                ctypes.c_ulong(0xFFFFFFFF),  # WTS_CURRENT_SESSION
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
        """
        Polls WTSQuerySessionInformationW at 2 Hz in a daemon thread.
        No message loop required; no extra dependencies beyond ctypes.
        """

        def __init__(self) -> None:
            super().__init__()
            self._stop_evt = threading.Event()
            self._thread: threading.Thread | None = None

        def start(self) -> None:
            self._locked = _query_locked()
            self._thread = threading.Thread(
                target=self._run,
                name="lock-monitor",
                daemon=True,
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
    """Return the appropriate LockMonitor for the current platform."""
    if sys.platform.startswith("linux"):
        return LinuxLockMonitor()   # type: ignore[name-defined]
    elif sys.platform.startswith("win"):
        return WindowsLockMonitor()  # type: ignore[name-defined]
    else:
        logger.info("lock-monitor: unsupported platform — lock detection disabled")
        return LockMonitor()
