"""The mouse's battery on Windows (mouse_battery_win.py) without Windows:
HID++ 2.0 and Razer's reports through stand-in devices, the voltage curve,
and the monitor's choice between mice."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mouse_battery_win as mb


class Logitech:
    """A receiver's long-report collection with devices in some slots, each a
    dict: kind, name, and battery features {feature id: reply bytes}."""

    FEATURES = (mb.ROOT, mb.DEVICE_NAME, mb.UNIFIED_BATTERY, mb.BATTERY_STATUS, mb.BATTERY_VOLTAGE)

    def __init__(self, slots, noise=()):
        self.slots, self.queue, self.writes = slots, list(noise), []

    def write(self, r):
        self.writes.append(r)
        assert len(r) == mb.HIDPP_LONG_LEN and r[0] == mb.HIDPP_LONG
        idx, feat, fn = r[1], r[2], r[3]
        dev = self.slots.get(idx)
        if dev is None:
            self.queue.append(bytes((0x10, idx, 0x8F, feat, fn, 0x09, 0)))     # HID++ 1.0: unknown device
            return
        func = fn >> 4
        offered = [f for f in self.FEATURES if f == mb.ROOT or f == mb.DEVICE_NAME or f in dev["battery"]]

        def reply(data):
            self.queue.append(bytes((0x11, idx, feat, fn, *data)).ljust(20, b"\0"))
        if feat == 0 and func == 1:
            reply((4, 2, r[6]))                                    # ping: protocol 4.2, the ping byte back
        elif feat == 0 and func == 0:
            fid = r[4] << 8 | r[5]
            reply((offered.index(fid),) if fid in offered and fid != mb.ROOT else (0,))
        elif offered[feat] == mb.DEVICE_NAME:
            name = dev["name"].encode()
            reply((len(name),) if func == 0 else name[r[4]:r[4] + 16] if func == 1 else (dev["kind"],))
        elif offered[feat] in dev["battery"]:
            reply(dev["battery"][offered[feat]])
        else:
            self.queue.append(bytes((0x11, idx, 0xFF, feat, fn, 0x02)).ljust(20, b"\0"))   # 2.0 error

    def read(self, timeout):
        return self.queue.pop(0) if self.queue else None


G502 = {"kind": 3, "name": "G502 LIGHTSPEED Wireless Gaming Mouse",
        "battery": {mb.BATTERY_VOLTAGE: (0x0F, 0x50, 0x00)}}          # 3920 mV, on battery


def test_a_lightspeed_mouse_is_found_behind_its_receiver_and_read_by_voltage():
    rx = Logitech({1: G502})
    got = mb.logitech_mouse(rx, direct=False, connection="wireless")
    assert got == {"pct": mb.voltage_pct(3920), "state": "discharging",
                   "model": "G502 LIGHTSPEED Wireless Gaming Mouse", "vendor": "Logitech",
                   "connection_type": "wireless"}
    assert 69 < got["pct"] < 70
    assert all(w[3] & 0x0F == mb.SW_ID for w in rx.writes)        # our own software id on every request


def test_a_keyboard_in_another_slot_is_not_the_mouse():
    keyboard = {"kind": 0, "name": "G915", "battery": {mb.UNIFIED_BATTERY: (80, 8, 0, 0)}}
    mouse = {"kind": 3, "name": "MX Master 3S", "battery": {mb.UNIFIED_BATTERY: (55, 4, 1, 1)}}
    got = mb.logitech_mouse(Logitech({1: keyboard, 2: mouse}), direct=False, connection="wireless")
    assert (got["model"], got["pct"], got["state"]) == ("MX Master 3S", 55.0, "charging")


def test_a_mouse_on_its_cable_is_the_device_itself():
    wired = dict(G502, battery={mb.BATTERY_VOLTAGE: (0x10, 0x4A, 0x81)})   # 4170 mV, on power, done
    got = mb.logitech_mouse(Logitech({mb.DIRECT: wired}), direct=True, connection="usb")
    assert got["state"] == "full" and got["pct"] > 98


def test_another_programs_replies_are_not_taken_for_ours():
    g_hub = bytes((0x11, 1, 0x00, 0x1A, 4, 2, 0xAA)).ljust(20, b"\0")   # G HUB's ping reply: its own sw id
    rx = Logitech({1: G502}, noise=[g_hub, bytes((0x11, 2, 0, 0x1B, 0, 0, 0)).ljust(20, b"\0")])
    assert mb.logitech_mouse(rx, direct=False, connection="wireless")["pct"] > 0


class Replay:
    """A conversation recorded from a real device, as (written, reply) hex:
    each write must be the one recorded, and gets the reply the device gave."""

    def __init__(self, pairs):
        self.pairs = [tuple(bytes.fromhex(x).ljust(mb.HIDPP_LONG_LEN, b"\0") for x in p) for p in pairs]
        self.reply = None

    def write(self, r):
        sent, self.reply = self.pairs.pop(0)
        assert r == sent

    def read(self, timeout):
        r, self.reply = self.reply, None
        return r


# A G502 X PLUS behind its Lightspeed receiver (USB C547), read on Linux over
# hidraw by scripts/battery_probe.py on 2026-11-03; UPower said 63 %, discharging.
G502_X_PLUS = [
    ("11 01 00 1b 00 00 aa", "11 01 00 1b 04 02 aa"),        # ping: HID++ 4.2
    ("11 01 00 0b 00 05", "11 01 00 0b 03"),                 # DEVICE_NAME is feature 3
    ("11 01 03 2b", "11 01 03 2b 03"),                       # its type: a mouse
    ("11 01 00 0b 10 04", "11 01 00 0b 06 00 03"),           # UNIFIED_BATTERY is feature 6
    ("11 01 06 1b", "11 01 06 1b 3f 04 00 00"),              # 63 %, level good, discharging
    ("11 01 03 0b", "11 01 03 0b 0b"),                       # an 11-byte name
    ("11 01 03 1b", "11 01 03 1b 47 35 30 32 20 58 20 50 4c 55 53"),
]


def test_a_real_g502_x_plus_behind_its_lightspeed_receiver():
    rx = Replay(G502_X_PLUS)
    got = mb.logitech_mouse(rx, direct=False, connection="wireless")
    assert got == {"pct": 63.0, "state": "discharging", "model": "G502 X PLUS", "vendor": "Logitech",
                   "connection_type": "wireless"}
    assert rx.pairs == []                                       # nothing more asked of it


def test_battery_formats():
    assert mb.unified_battery(bytes((72, 4, 0, 0))) == (72.0, "discharging")
    assert mb.unified_battery(bytes((100, 8, 3, 1))) == (100.0, "full")
    assert mb.battery_status(bytes((40, 30, 1))) == (40.0, "charging")
    assert mb.voltage_pct(4300) == 100.0 and mb.voltage_pct(3400) == 0.0
    assert mb.voltage_pct(3811) == 50.0 and 50 < mb.voltage_pct(3830) < 60


class Razer:
    """A Razer mouse's control interface: a feature report in, its answer out."""

    def __init__(self, level=191, charging=0, transaction=0x1F):
        self.level, self.charging, self.transaction, self.last = level, charging, transaction, None
        self.sent = []

    def set_feature(self, report):
        assert len(report) == mb.RAZER_REPORT_LEN + 1 and report[0] == 0
        self.sent.append(report)
        self.last = report
        return True

    def get_feature(self, length):
        r = bytearray(self.last[1:])
        crc = 0
        for b in r[2:88]:
            crc ^= b
        assert r[88] == crc                                     # the mouse ignores a bad checksum
        if r[1] != self.transaction:
            r[0] = 0x05                                         # not supported: another model's id
        else:
            r[0] = mb.RAZER_OK
            r[9] = self.level if r[7] == 0x80 else self.charging
        return b"\0" + bytes(r)


def test_a_razer_mouse_reports_its_level_out_of_255_and_whether_it_charges():
    got = mb.razer_mouse(Razer(level=191), "Razer Viper Ultimate")
    assert got == {"pct": 74.9, "state": "discharging", "model": "Razer Viper Ultimate", "vendor": "Razer",
                   "connection_type": "wireless"}
    assert mb.razer_mouse(Razer(level=255, charging=1), "")["state"] == "charging"


def test_each_razer_transaction_id_is_tried_in_turn():
    mouse = Razer(transaction=0x3F)
    assert mb.razer_mouse(mouse, "Razer Basilisk")["pct"] == 74.9
    assert [r[2] for r in mouse.sent[:2]] == [0x1F, 0x3F]


def test_the_monitor_keeps_the_best_mouse_and_says_when_it_changed():
    readings = [[]]
    told = []
    m = mb.WindowsMouseBattery(read=lambda: readings[0])
    m.on_battery_change = lambda: told.append(1)
    m.poll()
    assert m.get_state() is None and told == []                 # nothing read: the server's file fallback
    readings[0] = [mb._state(40, "discharging", "Bluetooth mouse", "", "bluetooth"),
                   mb._state(80, "charging", "G502", "Logitech", "usb")]
    m.poll()
    assert m.get_state()["model"] == "G502" and told == [1]
    m.poll()
    assert told == [1]                                          # the same: not told again

    def broken():
        raise OSError("hid.dll")
    m._read = broken
    m.poll()
    assert m.get_state() is None
