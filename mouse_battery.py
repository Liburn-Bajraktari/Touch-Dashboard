"""
mouse_battery.py — Event-driven UPower D-Bus mouse battery monitor (Linux only).

Architecture:
  Runs a GLib.MainLoop on a daemon thread named "upower-monitor".
  Subscribes to org.freedesktop.UPower DeviceAdded/DeviceRemoved signals for hotplug.
  For each discovered Type==5 (Mouse) device, subscribes to
  org.freedesktop.DBus.Properties.PropertiesChanged for zero-CPU-idle battery tracking.
  Calls on_battery_change() → main_event_loop.call_soon_threadsafe(trigger.set)
  so the FastAPI hardware_loop is woken on real change events, not polling.

Multi-device election:
  Lightspeed/HID++ mice can appear as two simultaneous UPower Type==5 nodes:
    • The wired USB-C path  (State: Charging, Pct: real value)
    • The Lightspeed dongle (State: Unknown,  Pct: 0)
  _best_state() uses a priority score so the most informative device always wins,
  preventing the idle dongle from shadowing the real charging reading.

State integer mapping (org.freedesktop.UPower.Device):
  0=Unknown  1=Charging  2=Discharging  3=Empty  4=FullyCharged
  5=PendingCharge  6=PendingDischarge

Usage:
  mon = UPowerMouseMonitor()
  mon.on_battery_change = lambda pct, state: ...
  mon.start()
  ...
  state = mon.get_state()   # {"pct": 73.0, "state": "discharging", "model": ..., "vendor": ...}
  mon.stop()
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# D-Bus / GLib constants
_UPOWER_BUS_NAME    = "org.freedesktop.UPower"
_UPOWER_PATH        = "/org/freedesktop/UPower"
_UPOWER_IFACE       = "org.freedesktop.UPower"
_UPOWER_DEV_IFACE   = "org.freedesktop.UPower.Device"
_PROPS_IFACE        = "org.freedesktop.DBus.Properties"

# UPower device type for Mouse
_TYPE_MOUSE = 5

# UPower State integer → human label mapping
_STATE_LABELS: dict[int, str] = {
    0: "unknown",
    1: "charging",
    2: "discharging",
    3: "empty",
    4: "full",
    5: "pending-charge",
    6: "pending-discharge",
}

# Priority score for state election — higher wins.
# Charging is most informative; unknown/zero-pct dongle scores lowest.
_STATE_PRIORITY: dict[str, int] = {
    "charging":          5,
    "discharging":       4,
    "full":              3,
    "pending-charge":    2,
    "pending-discharge": 1,
    "empty":             1,
    "unknown":           0,
}


def _device_score(dev: dict) -> tuple[int, float]:
    """
    Election key for _best_state().

    Primary:   state priority (charging > discharging > full > … > unknown)
    Secondary: percentage (higher is better — breaks ties between two
               discharging nodes where one is the real mouse and one is stale)
    """
    state_score = _STATE_PRIORITY.get(dev.get("state", "unknown"), 0)
    pct         = dev.get("pct") or 0.0
    return (state_score, pct)


def _infer_connection_type(native_path: str) -> str:
    """
    Infer connection type from the UPower NativePath.
    UPower has no dedicated ConnectionType property, so we derive it from
    the OS-level path.

    Examples:
      /sys/devices/.../hci0/dev_AA_BB_CC  → "bluetooth"
      /sys/devices/.../usb1/...           → "usb"
      /dev/input/mouseN / hid-N           → "hid"
      (anything else)                     → "unknown"
    """
    p = native_path.lower()
    if "hci" in p or "bluetooth" in p or "dev_" in p:
        return "bluetooth"
    if "usb" in p:
        return "usb"
    if "hid" in p or "input" in p:
        return "hid"
    return "unknown"


class UPowerMouseMonitor:
    """
    Event-driven UPower D-Bus mouse battery monitor.

    Discovers Type==5 (Mouse) devices on startup and via hotplug signals.
    Tracks all discovered mice in _device_states keyed by D-Bus object path.
    get_state() and _on_props_changed both elect the best device via _best_state()
    so that a Lightspeed dongle (State=unknown, Pct=0) never shadows the real
    wired-charging reading.
    """

    def __init__(self) -> None:
        # Public callback — set before calling start()
        self.on_battery_change: Callable[[float, int], None] | None = None

        self._lock          = threading.Lock()
        # Per-device state: {dbus_path: {pct, state, model, vendor, connection_type}}
        self._device_states: dict[str, dict] = {}
        self._last_pct: float | None         = None
        self._loop          = None           # GLib.MainLoop
        self._thread        = None           # daemon thread
        self._bus           = None           # dbus.SystemBus
        self._prop_sigs: dict[str, object] = {}  # path → Signal object

    # ── Public API ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Initialise D-Bus subscriptions and start the GLib main loop thread."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="upower-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the GLib main loop and clean up signal subscriptions."""
        if self._loop and self._loop.is_running():
            self._loop.quit()

    def get_state(self) -> dict:
        """Thread-safe snapshot of the best-candidate device state."""
        with self._lock:
            return self._best_state()

    # ── Internal ────────────────────────────────────────────────────────────────

    def _best_state(self) -> dict:
        """Elect the most informative device state."""
        if not self._device_states:
            if sys.platform.startswith("linux"):
                wired_model = _check_wired_fallback()
                if wired_model:
                    return {
                        "pct": self._last_pct,
                        "state": "charging",
                        "model": wired_model,
                        "vendor": "Logitech",
                        "connection_type": "usb",
                    }
            return _make_empty_state()

        winner = dict(max(self._device_states.values(), key=_device_score))
        if winner.get("pct") is not None:
            self._last_pct = winner["pct"]
        return winner

    def _run(self) -> None:
        """Entry point for the daemon thread. Sets up D-Bus and runs the GLib loop."""
        try:
            # GLib mainloop MUST be set as the dbus default BEFORE any SystemBus()
            # call on this thread. We use a thread-default context so we don't
            # crash GTK's g_application_run() which acquires the global default context.
            import dbus  # type: ignore[import-untyped]
            import dbus.mainloop.glib  # type: ignore[import-untyped]
            from gi.repository import GLib  # type: ignore[import-untyped]  # noqa: PLC0415

            context = GLib.MainContext.new()
            context.push_thread_default()

            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

            self._bus  = dbus.SystemBus()
            self._loop = GLib.MainLoop.new(context, False)

            self._subscribe_hotplug()
            self._scan_existing_devices()

            logger.info("UPower mouse monitor started")
            self._loop.run()
            context.pop_thread_default()

        except Exception as exc:
            logger.error("UPower monitor error: %s", exc, exc_info=True)

    def _subscribe_hotplug(self) -> None:
        """Subscribe to UPower DeviceAdded / DeviceRemoved signals."""
        self._bus.add_signal_receiver(  # type: ignore[union-attr]
            handler_function=self._on_device_added,
            signal_name="DeviceAdded",
            dbus_interface=_UPOWER_IFACE,
            bus_name=_UPOWER_BUS_NAME,
            path=_UPOWER_PATH,
        )
        self._bus.add_signal_receiver(  # type: ignore[union-attr]
            handler_function=self._on_device_removed,
            signal_name="DeviceRemoved",
            dbus_interface=_UPOWER_IFACE,
            bus_name=_UPOWER_BUS_NAME,
            path=_UPOWER_PATH,
        )

    def _scan_existing_devices(self) -> None:
        """Enumerate all UPower devices and track any Type==5 (Mouse) ones."""
        import dbus  # type: ignore[import-untyped]

        try:
            upower_obj   = self._bus.get_object(_UPOWER_BUS_NAME, _UPOWER_PATH)  # type: ignore[union-attr]
            upower_iface = dbus.Interface(upower_obj, _UPOWER_IFACE)
            paths        = upower_iface.EnumerateDevices()
        except Exception as exc:
            logger.warning("UPower EnumerateDevices failed: %s", exc)
            return

        for path in paths:
            self._inspect_device(str(path))

    def _inspect_device(self, path: str) -> None:
        """
        Inspect a UPower device path. If it is a mouse (Type==5),
        log its metadata and begin property tracking.
        """
        import dbus  # type: ignore[import-untyped]

        try:
            dev_obj  = self._bus.get_object(_UPOWER_BUS_NAME, path)  # type: ignore[union-attr]
            props    = dbus.Interface(dev_obj, _PROPS_IFACE)
            dev_type = int(props.Get(_UPOWER_DEV_IFACE, "Type"))
        except Exception as exc:
            logger.debug("Cannot read UPower device %s: %s", path, exc)
            return

        if dev_type != _TYPE_MOUSE:
            return

        try:
            model      = str(props.Get(_UPOWER_DEV_IFACE, "Model"))
            vendor     = str(props.Get(_UPOWER_DEV_IFACE, "Vendor"))
            percentage = float(props.Get(_UPOWER_DEV_IFACE, "Percentage"))
            state_int  = int(props.Get(_UPOWER_DEV_IFACE, "State"))
            native     = str(props.Get(_UPOWER_DEV_IFACE, "NativePath"))
        except Exception as exc:
            logger.warning("Cannot read UPower mouse properties at %s: %s", path, exc)
            return

        conn_type   = _infer_connection_type(native)
        state_label = _STATE_LABELS.get(state_int, "unknown")

        logger.info(
            "UPower mouse discovered — Model=%r Vendor=%r Path=%s NativePath=%r "
            "ConnectionType=%s Pct=%.0f%% State=%s",
            model, vendor, path, native, conn_type, percentage, state_label,
        )

        dev_state = {
            "pct":             percentage,
            "state":           state_label,
            "model":           model,
            "vendor":          vendor,
            "connection_type": conn_type,
        }

        with self._lock:
            self._device_states[path] = dev_state
            if len(self._device_states) > 1:
                # Log which device wins the election so multi-device scenarios
                # are visible in the server log.
                winner = self._best_state()
                logger.info(
                    "UPower multi-device election: %d mice visible, "
                    "elected %r (State=%s Pct=%.0f%%)",
                    len(self._device_states),
                    winner.get("model", "?"),
                    winner.get("state", "?"),
                    winner.get("pct") or 0,
                )

        # Subscribe to property changes on this device
        self._subscribe_props(path, model, vendor)

        # Notify so hardware_loop broadcasts the elected best state
        self._fire_callback(percentage, state_int)

    def _subscribe_props(self, path: str, model: str, vendor: str) -> None:
        """Subscribe to PropertiesChanged on a specific device object path."""
        if path in self._prop_sigs:
            return  # already subscribed

        def _handler(iface, changed, invalidated, sender=None):
            self._on_props_changed(path, model, vendor, iface, changed, invalidated)

        sig = self._bus.add_signal_receiver(  # type: ignore[union-attr]
            handler_function=_handler,
            signal_name="PropertiesChanged",
            dbus_interface=_PROPS_IFACE,
            bus_name=_UPOWER_BUS_NAME,
            path=path,
        )
        self._prop_sigs[path] = sig

    def _unsubscribe_props(self, path: str) -> None:
        sig = self._prop_sigs.pop(path, None)
        if sig is not None:
            try:
                sig.remove()  # type: ignore[union-attr]
            except Exception:
                pass

    def _on_device_added(self, path) -> None:
        """Handle UPower DeviceAdded signal — inspect and optionally track."""
        logger.debug("UPower DeviceAdded: %s", path)
        self._inspect_device(str(path))

    def _on_device_removed(self, path) -> None:
        """Handle UPower DeviceRemoved signal — drop from per-device store."""
        path = str(path)
        with self._lock:
            was_mouse = path in self._device_states
            if was_mouse:
                self._device_states.pop(path, None)

        if was_mouse:
            logger.info("UPower mouse removed: %s", path)
            self._unsubscribe_props(path)
            # Re-elect from remaining devices (if any) and broadcast the result.
            with self._lock:
                elected = self._best_state()
            elected_pct       = elected.get("pct")
            elected_state_str = elected.get("state", "unknown")
            elected_state_int = next(
                (k for k, v in _STATE_LABELS.items() if v == elected_state_str), 0
            )
            self._fire_callback(elected_pct, elected_state_int)

    def _on_props_changed(
        self, path: str, model: str, vendor: str,
        iface: str, changed: dict, invalidated,
    ) -> None:
        """Handle PropertiesChanged on a tracked mouse device."""
        if iface != _UPOWER_DEV_IFACE:
            return

        pct       = changed.get("Percentage")
        state_int = changed.get("State")

        if pct is None and state_int is None:
            return  # irrelevant property

        with self._lock:
            dev = self._device_states.get(path)
            if dev is None:
                return  # spurious signal for an untracked path
            if pct is not None:
                dev["pct"]   = float(pct)
            if state_int is not None:
                dev["state"] = _STATE_LABELS.get(int(state_int), "unknown")

            # Re-elect best device after applying the update.
            elected           = self._best_state()
            elected_pct       = elected.get("pct")
            elected_state_str = elected.get("state", "unknown")

        elected_state_int = next(
            (k for k, v in _STATE_LABELS.items() if v == elected_state_str), 0
        )

        logger.debug(
            "UPower mouse %r update: %.0f%% %s  (elected from %d device(s))",
            model, pct or 0, elected_state_str, len(self._device_states),
        )
        self._fire_callback(elected_pct, elected_state_int)

    def _fire_callback(self, pct, state_int) -> None:
        cb = self.on_battery_change
        if cb is not None:
            try:
                cb(pct, state_int)
            except Exception as exc:
                logger.error("on_battery_change callback error: %s", exc)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _make_empty_state() -> dict:
    return {"pct": None, "state": "unknown", "model": "", "vendor": "",
            "connection_type": "unknown"}

def _check_wired_fallback() -> str | None:
    """Check sysfs for a Logitech mouse if UPower drops the device entirely."""
    try:
        import os
        base = "/sys/bus/usb/devices"
        if not os.path.exists(base):
            return None
        for dev in os.listdir(base):
            if ":" in dev: 
                continue
            prod_path = os.path.join(base, dev, "product")
            if os.path.exists(prod_path):
                try:
                    with open(prod_path) as f:
                        prod = f.read().strip()
                        if "G502" in prod or "Logitech Mouse" in prod:
                            return prod
                except Exception:
                    pass
    except Exception:
        pass
    return None
