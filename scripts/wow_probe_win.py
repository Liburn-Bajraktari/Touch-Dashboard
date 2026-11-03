"""What Touch Dashboard sees of WoW's window on this Windows PC: run it with
WoW open (or --fake for a stand-in window), the companion's live link on.

    python scripts/wow_probe_win.py [--fake] [--reads 30] [--png top.png]

It prints the window it found, where its client area sits in a capture, the
DPI mode, whether WoW runs elevated, then reads the top rows through Windows
Graphics Capture and through GDI: whether the live link decodes, whether the
beacon shows, how long a read takes and how old the frames are. Nothing is
pressed and nothing is written but the optional PNG of the top rows."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wow_addons import link, screen, screen_win as sw   # noqa: E402


def probe(via: str, reads: int, png: str | None) -> dict:
    if via == "gdi":
        os.environ[sw.FORCE] = "gdi"
    else:
        os.environ.pop(sw.FORCE, None)
    s = sw.WinScreen()
    out: dict = {"via": via}
    api = s._win32()
    hwnd = s._find(api)
    if not hwnd:
        return {**out, "error": "no WoW window", "wow_running": api.running()}
    client, bounds = api.client(hwnd), api.bounds(hwnd)
    out.update(hwnd=hwnd, client=client, frame=bounds, offset=sw.client_offset(client, bounds),
               minimised=api.iconic(hwnd), in_front=s.front(), elevated=s.keys_blocked())
    times, decoded, beacons = [], 0, []
    rgb = None
    for _ in range(reads):
        t = time.perf_counter()
        size = s.size()
        rgb = s.grab(0, 0, size[0], screen.CELL_PX) if size else None
        times.append((time.perf_counter() - t) * 1000)
        if rgb:
            row = rgb[:size[0] * 3]
            decoded += any(link.read_row(row, size[0], x) for x in link.locate(row, size[0]))
        beacons.append(s.beacon())
        time.sleep(0.1)
    cap = s._cap
    out.update(capture=type(cap).__name__ if cap else None, reads=reads, decoded=decoded,
               beacon=sorted({str(b) for b in beacons}),
               read_ms={"median": sorted(times)[len(times) // 2], "max": max(times)})
    if isinstance(cap, sw._Frames) and cap._rows:
        out["last_frame_age_ms"] = round((sw._qpc_seconds() - cap._rows[6]) * 1000, 1)
    if png and rgb:
        from PIL import Image
        w = len(rgb) // 3 // screen.CELL_PX
        Image.frombytes("RGB", (w, screen.CELL_PX), rgb).resize((w, screen.CELL_PX * 8)).save(f"{via}-{png}")
        out["png"] = f"{via}-{png}"
    return out


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Windows only")
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="open a stand-in WoW window first")
    ap.add_argument("--reads", type=int, default=30)
    ap.add_argument("--png", help="save the top rows, 8× tall, as <via>-<name>")
    args = ap.parse_args()
    fake = None
    if args.fake:
        fake = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_wow_window.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        print(fake.stdout.readline().strip())
    try:
        for via in ("wgc", "gdi"):
            print(json.dumps(probe(via, args.reads, args.png), indent=2, default=str))
    finally:
        if fake:
            fake.stdin.write("quit\n")
            fake.stdin.flush()
            fake.wait(timeout=5)


if __name__ == "__main__":
    main()
