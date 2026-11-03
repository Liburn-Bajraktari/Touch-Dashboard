"""Live link frames in Python, cell for cell as TouchDashboard.lua's `symbols`
draws them, for places where no Lua runs: the fake WoW window on Windows
(fake_wow_window.py, scripts/wow_probe_win.py). test_wow_companion checks it
against the addon's own cells."""
import struct

from wow_addons.link import CALIB, CELL, HEADER, MAGIC, PROTOCOL, fletcher16


def frame_symbols(payload: bytes, *, seq: int = 1, page: int = 1, pages: int = 1, tick: int = 0) -> list[int]:
    """A page's cells: the calibration colours 0-7, then its bytes 3 bits a cell, high bits first."""
    data = MAGIC + bytes((PROTOCOL,)) + struct.pack(">HBBIH", seq, page, pages, tick, len(payload)) + payload
    assert len(data) == HEADER + len(payload)
    data += struct.pack(">H", fletcher16(data))
    out, acc, bits = list(range(CALIB)), 0, 0
    for byte in data:
        acc, bits = acc << 8 | byte, bits + 8
        while bits >= 3:
            bits -= 3
            out.append(acc >> bits & 7)
            acc &= (1 << bits) - 1
    if bits:
        out.append(acc << (3 - bits) & 7)
    return out


def strip_row(symbols: list[int], width: int, start: int) -> bytes:
    """One row of `width` RGB pixels with the cells drawn from `start`, CELL pixels each."""
    row = bytearray(width * 3)
    for i, sym in enumerate(symbols):
        rgb = bytes(255 if sym & bit else 0 for bit in (4, 2, 1))
        for x in range(start + i * CELL, min(width, start + (i + 1) * CELL)):
            row[x * 3:x * 3 + 3] = rgb
    return bytes(row)
