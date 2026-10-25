"""
mpris_monitor.py — event-driven MPRIS (Linux media players) for Touch Dashboard.

Replaces a `playerctl` process every second with D-Bus signals: players are
found through NameOwnerChanged, their state follows PropertiesChanged and
Seeked, and nothing runs while nothing changes. Commands (Next, SetPosition…)
go straight to the player's bus name.

  KEY DESIGN NOTE: this uses Gio's GDBus, not dbus-python. A private session
  bus connection on this thread's own GLib.MainContext delivers every signal
  here and shares nothing with lock_monitor's dbus-python SessionBus (dbus-python
  hands out one shared connection per process, tied to whichever loop made it).
  GDBusConnection.call_sync() is thread-safe, so the server sends commands
  from its worker threads without touching this loop.

  MPRIS does not signal Position: it is extrapolated from the last reading
  (and Rate) and read again on a seek, a status or track change, and every
  POSITION_RESYNC_S while playing.

If GDBus or the loop cannot start (no session bus, a broken PyGObject), start()
returns False and the server falls back to polling playerctl (media.py).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH   = "/org/mpris/MediaPlayer2"
ROOT_IFACE   = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE  = "org.freedesktop.DBus.Properties"
# playerctld re-publishes whichever player was active last: a copy of another.
IGNORED_NAMES = frozenset({MPRIS_PREFIX + "playerctld"})
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

CALL_TIMEOUT_MS   = 1000
START_TIMEOUT_S   = 3.0
POSITION_RESYNC_S = 10
# Leaving Playing shows only once it has lasted this long. Firefox reports a
# pause of ~100 ms every few seconds while a video buffers (measured: at 1, 3.4,
# 8.5, 13.8 s...); each one flipped every screen's play key twice, repainted the
# whole Linux window both times, and its resume re-stamped "started", so the
# player could take the card from another one that was playing.
PAUSE_SETTLE_S = 0.6


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v)
    return str(value) if value is not None else ""


def _number(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _pid(value) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def parse_metadata(md: dict) -> dict:
    """An unpacked MPRIS Metadata dict → the fields the dashboard uses."""
    length = _number(md.get("mpris:length"))
    return {
        "title":   _text(md.get("xesam:title")),
        "artist":  _text(md.get("xesam:artist")),
        "album":   _text(md.get("xesam:album")),
        "url":     _text(md.get("xesam:url")),
        "art":     _text(md.get("mpris:artUrl")),
        "trackid": _text(md.get("mpris:trackid")),
        "length":  round(length / 1e6, 1) if length else None,
        # KDE's browser bridge names the browser process whose tab it shows.
        "media_pid": _pid(md.get("kde:pid")),
    }


class MprisMonitor:
    """All MPRIS players on the session bus, kept current by signals."""

    def __init__(self) -> None:
        # Called on the monitor thread after any change; set before start().
        self.on_change: Callable[[], None] | None = None
        self.running = False

        self._lock    = threading.Lock()
        self._players: dict[str, dict] = {}   # bus name → state
        self._owners:  dict[str, str]  = {}   # unique name (":1.42") → bus name
        self._conn    = None                  # Gio.DBusConnection (private)
        self._loop    = None                  # GLib.MainLoop
        self._context = None                  # GLib.MainContext
        self._thread: threading.Thread | None = None
        self._ready   = threading.Event()

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the loop thread; True once it is listening."""
        if self._thread and self._thread.is_alive():
            return self.running
        self._thread = threading.Thread(target=self._run, name="mpris-monitor", daemon=True)
        self._thread.start()
        self._ready.wait(START_TIMEOUT_S)
        return self.running

    def stop(self) -> None:
        self.running = False
        if self._loop is not None and self._context is not None:
            # quit() must run on the loop's own context.
            from gi.repository import GLib  # type: ignore[import-untyped]
            src = GLib.idle_source_new()
            src.set_callback(lambda *_: (self._loop.quit(), False)[1])
            src.attach(self._context)

    def players(self) -> list[dict]:
        """A snapshot of every player, position extrapolated to now."""
        now = time.monotonic()
        out = []
        with self._lock:
            for bus, p in self._players.items():
                pos = p["position"]
                if pos is not None and p["status"] == "Playing":
                    pos += (now - p["position_at"]) * p["rate"]
                length = p["meta"]["length"]
                if pos is not None and length:
                    pos = min(pos, length)
                out.append({
                    **p["meta"],
                    "bus": bus,
                    "identity": p["identity"],
                    "status": p["status"],
                    "position": round(pos, 1) if pos is not None else None,
                    "caps": dict(p["caps"]),
                    "started": p["started"],
                    "pid": p["pid"],
                })
        return out

    def position(self, bus: str) -> float | None:
        """Read Position now (seconds); None when the player does not say."""
        value = self._get(bus, PLAYER_IFACE, "Position")
        n = _number(value)
        if n is None:
            return None
        with self._lock:
            p = self._players.get(bus)
            if p is not None:
                p["position"], p["position_at"] = n / 1e6, time.monotonic()
        return round(n / 1e6, 1)

    def call(self, bus: str, method: str, signature: str | None = None, *args) -> bool:
        """Call a Player method (Next, PlayPause, Seek…); thread-safe."""
        if self._conn is None:
            return False
        try:
            from gi.repository import Gio, GLib  # type: ignore[import-untyped]
            params = GLib.Variant(f"({signature})", args) if signature else None
            self._conn.call_sync(bus, MPRIS_PATH, PLAYER_IFACE, method, params, None,
                                 Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)
            return True
        except Exception as exc:   # GLib.Error: the player left, or refused
            logger.debug("mpris %s.%s failed: %s", bus, method, exc)
            return False

    def set_position(self, bus: str, seconds: float, trackid: str, current: float | None) -> bool:
        """Absolute seek: SetPosition when the track has an id, else Seek by the difference."""
        us = int(max(0.0, seconds) * 1e6)
        if trackid and trackid != NO_TRACK:
            return self.call(bus, "SetPosition", "ox", trackid, us)
        if current is None:
            return False
        return self.call(bus, "Seek", "x", us - int(current * 1e6))

    # ── Loop thread ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import gi  # type: ignore[import-untyped]
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib  # type: ignore[import-untyped]

            self._context = GLib.MainContext.new()
            self._context.push_thread_default()
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self._conn = Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
            # Signals are delivered on the context that was thread-default here.
            self._conn.signal_subscribe(
                "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                "/org/freedesktop/DBus", "org.mpris.MediaPlayer2",
                Gio.DBusSignalFlags.MATCH_ARG0_NAMESPACE, self._on_owner_changed)
            self._conn.signal_subscribe(
                None, PROPS_IFACE, "PropertiesChanged", MPRIS_PATH, None,
                Gio.DBusSignalFlags.NONE, self._on_properties_changed)
            self._conn.signal_subscribe(
                None, PLAYER_IFACE, "Seeked", MPRIS_PATH, None,
                Gio.DBusSignalFlags.NONE, self._on_seeked)

            names = self._conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "ListNames", None, GLib.VariantType.new("(as)"),
                Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None).unpack()[0]
            for name in names:
                if name.startswith(MPRIS_PREFIX) and name not in IGNORED_NAMES:
                    self._add_player(name, None)

            resync = GLib.timeout_source_new_seconds(POSITION_RESYNC_S)
            resync.set_callback(self._resync_positions)
            resync.attach(self._context)

            self._loop = GLib.MainLoop.new(self._context, False)
            self.running = True
            self._ready.set()
            logger.info("MPRIS monitor started (%d players)", len(self._players))
            self._loop.run()
            self._context.pop_thread_default()
        except Exception:
            logger.exception("MPRIS monitor failed; polling playerctl instead")
        finally:
            self.running = False
            self._ready.set()
            if self._conn is not None:
                try:
                    self._conn.close_sync(None)
                except Exception:
                    pass
                self._conn = None

    def _get(self, bus: str, iface: str, prop: str):
        if self._conn is None:
            return None
        try:
            from gi.repository import Gio, GLib  # type: ignore[import-untyped]
            reply = self._conn.call_sync(
                bus, MPRIS_PATH, PROPS_IFACE, "Get", GLib.Variant("(ss)", (iface, prop)),
                GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)
            return reply.unpack()[0]
        except Exception:
            return None

    def _get_all(self, bus: str, iface: str) -> dict:
        if self._conn is None:
            return {}
        try:
            from gi.repository import Gio, GLib  # type: ignore[import-untyped]
            reply = self._conn.call_sync(
                bus, MPRIS_PATH, PROPS_IFACE, "GetAll", GLib.Variant("(s)", (iface,)),
                GLib.VariantType.new("(a{sv})"), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)
            return reply.unpack()[0]
        except Exception as exc:
            logger.debug("mpris GetAll %s failed: %s", bus, exc)
            return {}

    def _pid_of(self, owner: str) -> int | None:
        """The process behind a bus connection."""
        if self._conn is None:
            return None
        try:
            from gi.repository import Gio, GLib  # type: ignore[import-untyped]
            reply = self._conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "GetConnectionUnixProcessID", GLib.Variant("(s)", (owner,)),
                GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)
            return _pid(reply.unpack()[0])
        except Exception:
            return None

    def _notify(self) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb()
            except Exception as exc:
                logger.error("MPRIS on_change callback error: %s", exc)

    def _add_player(self, bus: str, owner: str | None) -> None:
        if owner is None:
            try:
                from gi.repository import Gio, GLib  # type: ignore[import-untyped]
                owner = self._conn.call_sync(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                    "GetNameOwner", GLib.Variant("(s)", (bus,)), GLib.VariantType.new("(s)"),
                    Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None).unpack()[0]
            except Exception:
                return
        props = self._get_all(bus, PLAYER_IFACE)
        root  = self._get_all(bus, ROOT_IFACE)
        now   = time.monotonic()
        status = str(props.get("PlaybackStatus") or "Stopped")
        position = _number(props.get("Position"))
        state = {
            "identity": _text(root.get("Identity")),
            "status": status,
            "meta": parse_metadata(props.get("Metadata") or {}),
            "position": position / 1e6 if position is not None else None,
            "position_at": now,
            "rate": float(props.get("Rate") or 1.0) or 1.0,
            "caps": {
                "prev": bool(props.get("CanGoPrevious", True)),
                "next": bool(props.get("CanGoNext", True)),
                "seek": bool(props.get("CanSeek", True)),
            },
            # Found playing: as if it started now, after any found earlier.
            "started": now if status == "Playing" else 0.0,
            "pid": self._pid_of(owner),
            "held": None,     # a status that left Playing, not shown yet (PAUSE_SETTLE_S)
            "held_at": 0.0,
        }
        with self._lock:
            self._owners[owner] = bus
            self._players[bus] = state
        self._notify()

    def _remove_player(self, bus: str) -> None:
        with self._lock:
            self._players.pop(bus, None)
            for unique in [u for u, b in self._owners.items() if b == bus]:
                del self._owners[unique]
        self._notify()

    # ── Signal handlers (monitor thread) ─────────────────────────────────────

    def _on_owner_changed(self, _conn, _sender, _path, _iface, _signal, params, *_user) -> None:
        name, old, new = params.unpack()
        if not name.startswith(MPRIS_PREFIX) or name in IGNORED_NAMES:
            return
        if old:
            self._remove_player(name)
        if new:
            self._add_player(name, new)

    def _on_properties_changed(self, _conn, sender, _path, _iface, _signal, params, *_user) -> None:
        iface, changed, invalidated = params.unpack()
        with self._lock:
            bus = self._owners.get(sender)
        if bus is None:
            return
        if iface == ROOT_IFACE:
            if "Identity" in changed:
                with self._lock:
                    if bus in self._players:
                        self._players[bus]["identity"] = _text(changed["Identity"])
                self._notify()
            return
        if iface != PLAYER_IFACE:
            return
        # Invalidated properties carry no value: read them.
        for prop in invalidated:
            value = self._get(bus, PLAYER_IFACE, prop)
            if value is not None:
                changed[prop] = value
        reread_position = False
        now = time.monotonic()
        with self._lock:
            p = self._players.get(bus)
            if p is None:
                return
            if "PlaybackStatus" in changed:
                status = str(changed["PlaybackStatus"])
                if p["status"] == "Playing" and status != "Playing":
                    # Shown as still playing until it lasts (_settle).
                    if p["held"] is None:
                        p["held_at"] = now
                        self._later(PAUSE_SETTLE_S, self._settle, bus, now)
                    p["held"] = status
                elif status == "Playing" and p["held"] is not None:
                    p["held"] = None        # a blip: it never stopped, nor started again
                    reread_position = True  # it may have jumped meanwhile
                elif status != p["status"]:
                    if status == "Playing":
                        p["started"] = now
                    p["status"] = status
                    reread_position = True
            if "Metadata" in changed:
                meta = parse_metadata(changed["Metadata"] or {})
                if (meta["trackid"], meta["title"]) != (p["meta"]["trackid"], p["meta"]["title"]):
                    p["position"], p["position_at"] = 0.0, now
                    reread_position = True
                p["meta"] = meta
            if "Rate" in changed:
                p["rate"] = float(changed["Rate"] or 1.0) or 1.0
            for prop, key in (("CanGoPrevious", "prev"), ("CanGoNext", "next"), ("CanSeek", "seek")):
                if prop in changed:
                    p["caps"][key] = bool(changed[prop])
        if reread_position:
            self.position(bus)
        self._notify()

    def _later(self, seconds: float, fn, *args) -> None:
        """Run fn(*args) on the monitor thread after `seconds`."""
        from gi.repository import GLib  # type: ignore[import-untyped]
        src = GLib.timeout_source_new(int(seconds * 1000))
        src.set_callback(lambda *_: (fn(*args), False)[1])
        src.attach(self._context)

    def _settle(self, bus: str, held_at: float) -> None:
        """A status that left Playing at held_at and has lasted: show it."""
        with self._lock:
            p = self._players.get(bus)
            if p is None or p["held"] is None or p["held_at"] != held_at:
                return   # gone, back to playing, or a later pause holds its own timer
            if p["position"] is not None:
                # Freeze the extrapolated position where playback stopped.
                p["position"] += (held_at - p["position_at"]) * p["rate"]
                p["position_at"] = held_at
            p["status"], p["held"] = p["held"], None
        self.position(bus)
        self._notify()

    def _on_seeked(self, _conn, sender, _path, _iface, _signal, params, *_user) -> None:
        (us,) = params.unpack()
        with self._lock:
            bus = self._owners.get(sender)
            p = self._players.get(bus) if bus else None
            if p is None:
                return
            p["position"], p["position_at"] = max(0, us) / 1e6, time.monotonic()
        self._notify()

    def _resync_positions(self, *_args) -> bool:
        with self._lock:
            playing = [b for b, p in self._players.items() if p["status"] == "Playing"]
        for bus in playing:
            self.position(bus)
        return True   # keep the timer
