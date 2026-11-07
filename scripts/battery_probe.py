"""Run mouse_battery_win's HID++ code against a real Logitech mouse on Linux,
over hidraw, to check the protocol layer without Windows.

    python scripts/battery_probe.py [-v]

Read-only: it asks each Logitech receiver or mouse for its devices' type, name
and battery, nothing else. Needs read and write access to /dev/hidraw* (Solaar's
udev rules give it to the logged-in user; else run it with sudo). -v prints
every report written and read.

The mice Linux's hid-logitech-dj driver makes behind a receiver are left out:
Windows has no such node, only the receiver's own collection, which is what
mouse_battery_win reads there."""
import glob
import os
import re
import select
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mouse_battery_win as mb   # noqa: E402


HID_DEVICE = re.compile(r"^[0-9A-F]{4}:[0-9A-F]{4}:[0-9A-F]{4}\.[0-9A-F]{4}$")


class Hidraw:
    def __init__(self, path, verbose=False):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.verbose = verbose

    def write(self, report):
        if self.verbose:
            print(f"  > {report.hex(' ')}")
        os.write(self.fd, report)

    def read(self, timeout):
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        r = os.read(self.fd, 64) if ready else None
        if self.verbose:
            print(f"  < {r.hex(' ') if r else '(nothing)'}")
        return r

    def close(self):
        os.close(self.fd)


def logitech_nodes():
    """hidraw nodes of Logitech devices whose report descriptor has HID++'s long report (0x11)."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        parent = os.path.basename(os.path.dirname(os.path.realpath(os.path.join(node, "device"))))
        if HID_DEVICE.match(parent.upper()):
            continue                          # a mouse hid-logitech-dj made behind its receiver
        uevent = Path(node, "device", "uevent").read_text(errors="replace")
        if ":0000046D:" not in uevent.upper():
            continue
        desc = Path(node, "device", "report_descriptor").read_bytes()
        if b"\x85\x11" in desc and b"\x06\x00\xff" in desc:     # report id 0x11 on vendor page 0xFF00
            name = next((line.split("=", 1)[1] for line in uevent.splitlines() if line.startswith("HID_NAME=")), "")
            yield "/dev/" + os.path.basename(node), name


def main():
    if not sys.platform.startswith("linux"):
        sys.exit("Linux only (hidraw)")         # elsewhere no node is found: it would say no mouse answered
    verbose = "-v" in sys.argv[1:]
    found = False
    for path, name in logitech_nodes():
        print(f"{path}: {name}")
        try:
            io = Hidraw(path, verbose)
        except PermissionError:
            print("  no access (Solaar's udev rules, or sudo)")
            continue
        try:
            direct = "receiver" not in name.lower()
            got = mb.logitech_mouse(io, direct, "usb" if direct else "wireless")
        finally:
            io.close()
        print(f"  {got}")
        found = found or bool(got)
    if not found:
        print("No Logitech mouse answered.")


if __name__ == "__main__":
    main()
