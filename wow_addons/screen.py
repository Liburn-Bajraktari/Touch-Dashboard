"""WoW's window as this PC sees it: is it in front, and is the companion's box open.

The companion addon lights a beacon along the window's top edge while its
input box has the keyboard: one row of BEACON cells, CELL_PX physical pixels
each, just left of the live link's strip (a fifth of the way across). Not in
the corner: overlays draw there into the game's own image (Steam's FPS
counter covered v2.1.0's beacon, and every Read answered no_box), so
`find_beacon` looks for it along the whole edge. Ctrl+C, Ctrl+V and Enter go
to the box only while the beacon shows; without the box they would reach the
game's own bindings (a player may have Bloodlust on Ctrl+C).

Linux: WoW runs through Wine or Proton under XWayland. Its window is found by
its title in the window manager's client list and read with GetImage, through
libxcb and ctypes (no new dependency; well under a millisecond for the top
rows, and the window's
own pixels even when another window covers it). xcb hands each error back
with its reply. Xlib would send it to one handler for the whole process, which
GTK owns in the desktop window, and which ends the process.

The same read carries the companion's live link (link.py): `size()` and
`grab()` give link.py the strip along the window's top edge.

Elsewhere (Windows, a Wayland-native Wine) nothing is checked: front(),
beacon(), size() and grab() answer None.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import re
import struct
import sys
import threading
import time

from .link import symbols

logger = logging.getLogger(__name__)

TITLES = ("World of Warcraft",)
PROCESSES = ("wow.exe", "wowclassic.exe", "wowt.exe", "wowb.exe")
# Left to right, each channel lit (1) or dark (0). TouchDashboard.lua draws the same.
BEACON = ((1, 0, 1), (0, 1, 0), (1, 1, 1), (0, 0, 0), (0, 1, 1), (1, 1, 0))
CELL_PX = 4
HIGH, LOW = 192, 63          # a lit channel reads at least HIGH, a dark one at most LOW
RECONNECT_S = 5.0
# The beacon's bottom row, a pixel at a time. The bottom row because the live
# link's cells are a pixel shorter: its data cannot pass for the beacon there.
_BEACON_RUN = re.compile(b"".join(bytes([r * 4 + g * 2 + b]) + b"{3,5}" for r, g, b in BEACON))


def beacon_seen(rgb: bytes, width: int, x: int = 0) -> bool:
    """`rgb`: rows of `width` RGB pixels from the window's top, at least one
    cell high. True when the centre of every cell from `x` on shows its colour."""
    c = CELL_PX // 2
    for i, cell in enumerate(BEACON):
        o = (c * width + x + i * CELL_PX + c) * 3
        px = rgb[o:o + 3]
        if len(px) < 3 or x + i * CELL_PX + c >= width:
            return False
        for v, lit in zip(px, cell):
            if (v < HIGH) if lit else (v > LOW):
                return False
    return True


def find_beacon(rgb: bytes, width: int) -> int | None:
    """Where the beacon is along the window's top edge (`rgb`: CELL_PX rows of
    `width` RGB pixels), or None."""
    y = CELL_PX - 1
    for m in _BEACON_RUN.finditer(symbols(rgb[y * width * 3:(y + 1) * width * 3])):
        if beacon_seen(rgb, width, m.start()):
            return m.start()
    return None


def wow_running() -> bool:
    """A WoW client among this user's processes (Wine names them after the .exe)."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                if f.read().strip().lower() in PROCESSES:
                    return True
        except OSError:
            continue
    return False


class NoScreen:
    """Nothing can be checked here."""

    def front(self) -> bool | None:
        return None

    def beacon(self) -> bool | None:
        return None

    def size(self) -> tuple[int, int] | None:
        return None

    def grab(self, x: int, y: int, width: int, height: int) -> bytes | None:
        return None


class _Cookie(ctypes.Structure):
    _fields_ = [("sequence", ctypes.c_uint)]


class _ScreenIter(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("rem", ctypes.c_int), ("index", ctypes.c_int)]


class _AtomReply(ctypes.Structure):
    _fields_ = [("response_type", ctypes.c_uint8), ("pad0", ctypes.c_uint8), ("sequence", ctypes.c_uint16),
                ("length", ctypes.c_uint32), ("atom", ctypes.c_uint32)]


class _Geometry(ctypes.Structure):
    _fields_ = [("response_type", ctypes.c_uint8), ("depth", ctypes.c_uint8), ("sequence", ctypes.c_uint16),
                ("length", ctypes.c_uint32), ("root", ctypes.c_uint32), ("x", ctypes.c_int16),
                ("y", ctypes.c_int16), ("width", ctypes.c_uint16), ("height", ctypes.c_uint16),
                ("border_width", ctypes.c_uint16)]


_SETUP_IMAGE_BYTE_ORDER = 30     # offset of image_byte_order in xcb_setup_t; 0 = LSBFirst (BGRX)
_Z_PIXMAP = 2
_ANY = 0


class X11Screen:
    """WoW's window under X11 (XWayland), through libxcb."""

    def __init__(self, display: str | None = None):
        self._display = display
        self._lock = threading.Lock()
        self._xcb = None
        self._free = None
        self._c = None
        self._root = 0
        self._lsb = True
        self._atoms: dict[str, int] = {}
        self._wow = 0
        self._next_try = 0.0

    # ── the connection ───────────────────────────────────────────────────

    def _lib(self) -> bool:
        if self._xcb is not None:
            return True
        name = ctypes.util.find_library("xcb")
        if not name:
            return False
        x = ctypes.CDLL(name)
        vp, u8, u16, u32, i16 = ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_int16
        err = ctypes.POINTER(ctypes.c_void_p)
        for fn, res, args in (
            ("xcb_connect", vp, [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]),
            ("xcb_connection_has_error", ctypes.c_int, [vp]),
            ("xcb_disconnect", None, [vp]),
            ("xcb_get_setup", vp, [vp]),
            ("xcb_setup_roots_iterator", _ScreenIter, [vp]),
            ("xcb_screen_next", None, [ctypes.POINTER(_ScreenIter)]),
            ("xcb_intern_atom", _Cookie, [vp, u8, u16, ctypes.c_char_p]),
            ("xcb_intern_atom_reply", vp, [vp, _Cookie, err]),
            ("xcb_get_property", _Cookie, [vp, u8, u32, u32, u32, u32, u32]),
            ("xcb_get_property_reply", vp, [vp, _Cookie, err]),
            ("xcb_get_property_value", vp, [vp]),
            ("xcb_get_property_value_length", ctypes.c_int, [vp]),
            ("xcb_get_image", _Cookie, [vp, u8, u32, i16, i16, u16, u16, u32]),
            ("xcb_get_image_reply", vp, [vp, _Cookie, err]),
            ("xcb_get_image_data", vp, [vp]),
            ("xcb_get_image_data_length", ctypes.c_int, [vp]),
            ("xcb_get_geometry", _Cookie, [vp, u32]),
            ("xcb_get_geometry_reply", vp, [vp, _Cookie, err]),
        ):
            f = getattr(x, fn)
            f.restype, f.argtypes = res, args
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        libc.free.argtypes, libc.free.restype = [ctypes.c_void_p], None
        self._xcb, self._free = x, libc.free
        return True

    def _drop(self) -> None:
        if self._c:
            self._xcb.xcb_disconnect(self._c)
        self._c, self._atoms, self._wow = None, {}, 0

    def _connect(self) -> bool:
        if self._c and not self._xcb.xcb_connection_has_error(self._c):
            return True
        self._drop()
        if time.monotonic() < self._next_try:
            return False
        self._next_try = time.monotonic() + RECONNECT_S
        display = self._display or os.environ.get("DISPLAY")
        if not display or not self._lib():
            return False
        n = ctypes.c_int(0)
        c = self._xcb.xcb_connect(display.encode(), ctypes.byref(n))
        if not c or self._xcb.xcb_connection_has_error(c):
            if c:
                self._xcb.xcb_disconnect(c)
            return False
        setup = self._xcb.xcb_get_setup(c)
        it = self._xcb.xcb_setup_roots_iterator(setup)
        for _ in range(max(0, n.value)):
            self._xcb.xcb_screen_next(ctypes.byref(it))
        if not it.data:
            self._xcb.xcb_disconnect(c)
            return False
        self._c = c
        self._root = ctypes.c_uint32.from_address(it.data).value
        self._lsb = ctypes.c_uint8.from_address(setup + _SETUP_IMAGE_BYTE_ORDER).value == 0
        return True

    def _reply(self, fn, cookie) -> int | None:
        e = ctypes.c_void_p(0)
        r = fn(self._c, cookie, ctypes.byref(e))
        if e.value:
            self._free(e.value)
        if not r or e.value:
            if r:
                self._free(r)
            return None
        return r

    def _atom(self, name: str) -> int:
        a = self._atoms.get(name)
        if a is None:
            r = self._reply(self._xcb.xcb_intern_atom_reply,
                            self._xcb.xcb_intern_atom(self._c, 0, len(name), name.encode()))
            a = ctypes.cast(r, ctypes.POINTER(_AtomReply)).contents.atom if r else 0
            if r:
                self._free(r)
            self._atoms[name] = a
        return a

    def _prop(self, window: int, name: str, longs: int) -> bytes | None:
        r = self._reply(self._xcb.xcb_get_property_reply,
                        self._xcb.xcb_get_property(self._c, 0, window, self._atom(name), _ANY, 0, longs))
        if not r:
            return None
        try:
            n = self._xcb.xcb_get_property_value_length(r)
            return ctypes.string_at(self._xcb.xcb_get_property_value(r), n) if n > 0 else b""
        finally:
            self._free(r)

    # ── WoW's window ─────────────────────────────────────────────────────

    def _title(self, window: int) -> str:
        raw = self._prop(window, "_NET_WM_NAME", 64) or self._prop(window, "WM_NAME", 64) or b""
        return raw.decode("utf-8", "replace")

    def _find(self) -> int:
        if self._wow and self._title(self._wow) in TITLES:
            return self._wow
        self._wow = 0
        data = self._prop(self._root, "_NET_CLIENT_LIST", 4096) or b""
        for w in struct.unpack(f"={len(data) // 4}I", data[:len(data) // 4 * 4]):
            if self._title(w) in TITLES:
                self._wow = w
                break
        return self._wow

    def _grab(self, window: int, x: int, y: int, width: int, height: int) -> bytes | None:
        """A rectangle of the window as RGB rows, or None (minimised, gone, outside)."""
        r = self._reply(self._xcb.xcb_get_image_reply,
                        self._xcb.xcb_get_image(self._c, _Z_PIXMAP, window, x, y, width, height, 0xFFFFFFFF))
        if not r:
            return None
        try:
            n = self._xcb.xcb_get_image_data_length(r)
            if n != width * height * 4:     # 24/32-bit visuals only: what WoW gets
                return None
            raw = ctypes.string_at(self._xcb.xcb_get_image_data(r), n)
        finally:
            self._free(r)
        out = bytearray(width * height * 3)
        if self._lsb:                       # B G R X
            out[0::3], out[1::3], out[2::3] = raw[2::4], raw[1::4], raw[0::4]
        else:                               # X R G B
            out[0::3], out[1::3], out[2::3] = raw[1::4], raw[2::4], raw[3::4]
        return bytes(out)

    def _size(self, window: int) -> tuple[int, int] | None:
        r = self._reply(self._xcb.xcb_get_geometry_reply, self._xcb.xcb_get_geometry(self._c, window))
        if not r:
            return None
        g = ctypes.cast(r, ctypes.POINTER(_Geometry)).contents
        out = (g.width, g.height)
        self._free(r)
        return out

    def front(self) -> bool | None:
        """True when WoW is the active window, False when another one is (or
        WoW is not running), None when this cannot be told."""
        with self._lock:
            try:
                if not self._connect():
                    return None
                wow = self._find()
                if not wow:
                    return None if wow_running() else False
                active = self._prop(self._root, "_NET_ACTIVE_WINDOW", 1)
                if active is None or len(active) < 4:
                    return None
                return struct.unpack("=I", active[:4])[0] == wow
            except (OSError, ValueError, struct.error):
                logger.exception("Could not tell which window is in front")
                self._drop()
                return None

    def beacon(self) -> bool | None:
        """True while the companion's box shows its beacon, None when WoW's
        window cannot be seen."""
        with self._lock:
            try:
                if not self._connect():
                    return None
                wow = self._find()
                size = self._size(wow) if wow else None
                if not size:
                    return None
                rgb = self._grab(wow, 0, 0, size[0], CELL_PX)
                return rgb is not None and find_beacon(rgb, size[0]) is not None
            except (OSError, ValueError, struct.error):
                logger.exception("Could not read WoW's window")
                self._drop()
                return None

    def size(self) -> tuple[int, int] | None:
        """WoW's window in pixels, or None when it cannot be seen."""
        with self._lock:
            try:
                if not self._connect():
                    return None
                wow = self._find()
                return self._size(wow) if wow else None
            except (OSError, ValueError, struct.error):
                logger.exception("Could not read WoW's window")
                self._drop()
                return None

    def grab(self, x: int, y: int, width: int, height: int) -> bytes | None:
        """RGB rows of a rectangle of WoW's window, or None."""
        with self._lock:
            try:
                if not self._connect():
                    return None
                wow = self._find()
                return self._grab(wow, x, y, width, height) if wow else None
            except (OSError, ValueError, struct.error):
                logger.exception("Could not read WoW's window")
                self._drop()
                return None


def for_platform() -> X11Screen | NoScreen:
    if sys.platform.startswith("linux") and ctypes.util.find_library("xcb"):
        return X11Screen()
    return NoScreen()
