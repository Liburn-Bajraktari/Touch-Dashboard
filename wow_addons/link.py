"""The companion's live link: the game's state painted into one row of cells
along the top edge of WoW's window (TouchDashboard.lua, "the live link"), read
from the window several times a second (screen.py) and turned back into the
snapshot the companion's box hands over (TDS1), so the views need no second
shape.

The addon draws the row in the middle of the top edge (from a fifth of the
width to four fifths), away from the corners where overlays draw (Steam's
FPS counter, MangoHud): drawn into the game's own image, they cover the strip
for this reader too. The reader does not rely on where it is: `locate` finds
the calibration cells along the row.

A frame, left to right, CELL pixels a cell and ROWS tall (one: a hairline
along the top, the least the game can show; up to addon 2.5.0 three, whose
top row reads the same): CALIB cells in colours 0-7 (red 4,
green 2, blue 1; each channel lit or dark), then bytes as 3-bit cells, high
bits first:

    "TL" protocol(1) seq(2) page(1) pages(1) tick(4) len(2) payload(len) fletcher16(2)

The calibration cells give each channel's cut as this screen draws it (the
game's gamma, brightness and contrast). tick is WoW's GetTime() in whole
seconds; times in the payload are tenths of GetTime(), so a running timer
leaves the strip as it is. A payload longer than a row comes in pages, one a
frame, all with the same seq.

Pure: no I/O.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

CELL, ROWS, CALIB, HEADER = 3, 1, 8, 13
MAGIC, PROTOCOL = b"TL", 4
MIN_SPREAD = 48              # a channel's lit and dark calibration cells at least this far apart
HEADER_SYMBOLS = -(-HEADER * 8 // 3)
CLOCK_SLIP_S = 3.0           # WoW's clock this far off ours: WoW restarted, measure again
CUTS = (128, 96, 160)        # where `locate` tries the line between lit and dark
# The calibration cells, a pixel at a time: colour 0 then 1-7, each about a cell wide.
_CALIB_RUN = re.compile(rb"\x00(\x01{2,4})\x02{2,4}\x03{2,4}\x04{2,4}\x05{2,4}\x06{2,4}\x07{2,4}")
# WoW's class ids (UnitClass's third value), for the group's classes.
CLASSES = ("WARRIOR", "PALADIN", "HUNTER", "ROGUE", "PRIEST", "DEATHKNIGHT", "SHAMAN", "MAGE", "WARLOCK", "MONK",
           "DRUID", "DEMONHUNTER", "EVOKER")


@dataclass(frozen=True)
class Page:
    seq: int
    page: int
    pages: int
    tick: int
    payload: bytes


def fletcher16(data: bytes) -> int:
    a = b = 0
    for x in data:
        a = (a + x) % 255
        b = (b + a) % 255
    return b << 8 | a


_LIT: dict[int, tuple[bytes, bytes, bytes]] = {}


def symbols(row: bytes, cut: int = 128) -> bytes:
    """Each RGB pixel of `row` as a colour 0-7 (red 4, green 2, blue 1), a
    channel lit above `cut`."""
    if cut not in _LIT:
        _LIT[cut] = tuple(bytes(w if v > cut else 0 for v in range(256)) for w in (4, 2, 1))
    r, g, b = _LIT[cut]
    n = len(row) // 3
    # Three planes of 0/4, 0/2 and 0/1 bytes add up without a carry: one sum for the whole row.
    total = sum(int.from_bytes(row[ch::3][:n].translate(t), "big") for ch, t in enumerate((r, g, b)))
    return total.to_bytes(n, "big")


def locate(rgb: bytes, width: int) -> list[int]:
    """Where along the rows (`width` RGB pixels each, at least ROWS of them)
    the strip may start: the calibration cells, as `read_row` takes them."""
    y = ROWS // 2
    row = rgb[y * width * 3:(y + 1) * width * 3]
    out: list[int] = []
    for cut in CUTS:
        for m in _CALIB_RUN.finditer(symbols(row, cut)):
            x = m.start(1) - CELL
            if x >= 0 and x not in out:
                out.append(x)
    return out


def read_row(rgb: bytes, width: int, x: int = 0) -> Page | None:
    """A frame from the strip's pixels (`width` RGB pixels a row, at least
    ROWS rows from the top of the strip, the strip starting `x` pixels in),
    or None: no strip, unreadable, torn."""
    y = ROWS // 2
    row = rgb[(y * width + x) * 3:(y + 1) * width * 3]
    n = len(row) // 3 // CELL
    if n < CALIB + HEADER_SYMBOLS:
        return None

    def centre(i: int) -> bytes:
        o = (i * CELL + CELL // 2) * 3
        return row[o:o + 3]

    calib = [centre(i) for i in range(CALIB)]
    cuts = []
    for ch in range(3):
        bit = 4 >> ch
        lit = [calib[s][ch] for s in range(CALIB) if s & bit]
        dark = [calib[s][ch] for s in range(CALIB) if not s & bit]
        if min(lit) - max(dark) < MIN_SPREAD:
            return None
        cuts.append((min(lit) + max(dark)) / 2)
    r_cut, g_cut, b_cut = cuts

    def unpack(symbols: int) -> bytes:
        out, acc, bits = bytearray(), 0, 0
        for i in range(CALIB, CALIB + symbols):
            r, g, b = centre(i)
            acc = acc << 3 | (r > r_cut) << 2 | (g > g_cut) << 1 | (b > b_cut)
            bits += 3
            if bits >= 8:
                bits -= 8
                out.append(acc >> bits & 0xFF)
                acc &= (1 << bits) - 1
        return bytes(out)

    head = unpack(HEADER_SYMBOLS)[:HEADER]
    if head[:2] != MAGIC or head[2] != PROTOCOL:
        return None
    seq, page, pages, tick, length = struct.unpack(">HBBIH", head[3:HEADER])
    total = HEADER + length + 2
    symbols = -(-total * 8 // 3)
    if CALIB + symbols > n or not 1 <= page <= pages:
        return None
    data = unpack(symbols)[:total]
    if len(data) < total or fletcher16(data[:-2]) != struct.unpack(">H", data[-2:])[0]:
        return None
    return Page(seq, page, pages, tick, data[HEADER:HEADER + length])


class Link:
    """A payload from its pages, and WoW's clock against ours (monotonic)."""

    def __init__(self):
        self._seq: int | None = None
        self.pending = False                # some pages of the newest payload are still to come
        self._pages: dict[int, tuple[int, bytes]] = {}
        self.payload: bytes | None = None
        self._tick: int | None = None
        self._offset: float | None = None   # ours minus WoW's GetTime()
        self._exact = False

    def feed(self, page: Page, now: float) -> bytes | None:
        """The payload when this page completed a new one."""
        self._clock(page.tick, now)
        if page.seq != self._seq:
            self._seq, self._pages = page.seq, {}
        self._pages[page.page] = (page.pages, page.payload)
        if any(n != page.pages for n, _ in self._pages.values()):
            self._pages = {page.page: (page.pages, page.payload)}
        self.pending = set(self._pages) != set(range(1, page.pages + 1))
        if self.pending:
            return None
        payload = b"".join(self._pages[i][1] for i in range(1, page.pages + 1))
        if payload == self.payload:
            return None
        self.payload = payload
        return payload

    def _clock(self, tick: int, now: float) -> None:
        # A tick seen to turn gives our time at that whole second, late by at
        # most one read: the earliest such sighting is the closest.
        if self._offset is not None and not -1 <= (now - self._offset) - tick <= CLOCK_SLIP_S:
            self._offset, self._exact, self._tick = None, False, None
        if self._tick is not None and tick == self._tick + 1:
            seen = now - tick
            self._offset = seen if not self._exact else min(self._offset, seen)
            self._exact = True
        elif self._offset is None:
            self._offset = now - tick - 0.5
        self._tick = tick

    def wow_time(self, now: float) -> float:
        return now - (self._offset or 0.0)


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.i = data, 0

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.data):
            raise ValueError("short payload")
        out = self.data[self.i:self.i + n]
        self.i += n
        return out

    def uint(self, n: int) -> int:
        return int.from_bytes(self.take(n), "big")

    def text(self) -> str:
        return self.take(self.uint(1)).decode("utf-8", "replace")


def parse(payload: bytes, wow_now: float) -> dict:
    """The payload as the companion's box would hand it over (TDS1), times left
    counted from `wow_now` (WoW's GetTime() now). ValueError when it is short."""
    r = _Reader(payload)
    flags, members, classes = r.uint(1), r.uint(1), r.uint(2)
    name, realm, cls, spec = r.text(), r.text(), r.text(), r.text()
    level, ilvl = r.uint(1), r.uint(2) / 10
    gold = r.uint(6)
    free, total = r.uint(2), r.uint(2)
    low, broken = r.uint(1), r.uint(1)
    mh, oh = r.uint(4), r.uint(4)
    key_level, key_map, key_name = r.uint(1), r.uint(2), r.text()
    zone = r.text()
    slots_hash = r.uint(4)
    version = r.text()

    def left(tenths: int) -> float:
        return tenths / 10 - wow_now

    buffs = []
    for _ in range(r.uint(1)):
        spell, end, kind = r.uint(3), r.uint(4), r.uint(1)
        label = r.text() if kind else None
        buffs.append({"id": spell, "name": label, "rem": max(0, int(left(end))) if end else -1})
    bars = []
    for _ in range(r.uint(1)):
        label, end, length = r.text(), r.uint(4), r.uint(2) / 10
        rem = round(left(end), 1)
        if rem > 0:
            bars.append({"label": label, "rem": rem, "dur": length})
    # The last ready check (state 0 none, 1 running, 2 over; who is not
    # ready: 0 not answered yet, 2 not ready, 3 no answer), then NSRT's alerts.
    rc_state, rc_end = r.uint(1), r.uint(4)
    rc_total, rc_ready = r.uint(1), r.uint(1)
    missing = []
    for _ in range(r.uint(1)):
        answer = r.uint(1)
        missing.append({"name": r.text(), "a": answer})
    phase, encounter = r.uint(1), r.text()
    alerts = []
    for _ in range(r.uint(1)):
        text, end, spell = r.text(), r.uint(4), r.uint(3)
        alerts.append({"text": text, "rem": round(left(end), 1), "spell": spell or None})
    # The Mythic+ run: 1 in a key (its timer's start, 0 before it runs), 2 over (its final time)
    mplus = None
    run_state = r.uint(1)
    if run_state:
        map_id, dungeon, level, limit, when = r.uint(2), r.text(), r.uint(1), r.uint(2), r.uint(4)
        flags, deaths, lost, forces = r.uint(1), r.uint(1), r.uint(2), r.uint(2) / 10
        bosses = []
        for _ in range(r.uint(1)):
            name, done, at = r.text(), r.uint(1), r.uint(2)
            bosses.append({"name": name, "done": bool(done), "at": at if done and at else None})
        elapsed = when / 10 if run_state == 2 else (round(wow_now - when / 10, 1) if when else 0)
        mplus = {"state": run_state, "map": map_id, "name": dungeon or None, "level": level, "limit": limit,
                 "elapsed": max(0, elapsed), "running": run_state == 1 and when > 0,
                 "onTime": bool(flags & 1), "upgrades": flags >> 1, "deaths": deaths, "lost": lost,
                 "forces": forces, "bosses": bosses}
    return {
        "v": 1, "ver": version, "sh": slots_hash, "link": True,
        "combat": bool(flags & 1), "lockdown": bool(flags & 2),
        "char": {"name": name or None, "realm": realm or None, "class": cls or None, "spec": spec or None,
                 "level": level or None, "ilvl": ilvl or None},
        "gold": gold,
        "bags": {"free": free, "total": total} if total else None,
        "dur": {"low": low, "broken": broken} if low != 255 else None,
        "zone": zone or None,
        "key": {"map": key_map, "level": key_level, "name": key_name or None} if key_level else None,
        "weapon": {"mh": max(0, int(left(mh))) if mh else False, "oh": max(0, int(left(oh))) if oh else False},
        "buffs": buffs,
        "bars": bars,
        "rc": {"state": rc_state, "rem": round(left(rc_end), 1) if rc_end else 0, "total": rc_total,
               "ready": rc_ready, "missing": missing},
        "alerts": {"phase": phase, "enc": encounter or None, "list": alerts},
        "mplus": mplus,
        "group": {"n": members, "raid": bool(flags & 4),
                  "classes": [c for i, c in enumerate(CLASSES) if classes >> i & 1]},
    }
