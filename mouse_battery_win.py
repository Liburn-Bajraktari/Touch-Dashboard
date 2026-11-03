"""
mouse_battery_win.py — the mouse's battery on Windows, read from the mouse itself.

Windows has no UPower. Up to v0.16.4 the dashboard read a number from
%TEMP%\\g502_battery.txt, written by a script of the user's own. Now it asks
the mouse, with nothing to install, through Windows' own HID and device APIs
(ctypes):

  • Logitech (Unifying, Lightspeed and Bolt receivers, a cable, Bluetooth):
    HID++ 2.0, the protocol Logitech's software and Solaar speak. The battery
    features UNIFIED_BATTERY (0x1004), BATTERY_STATUS (0x1000) and
    BATTERY_VOLTAGE (0x1001, G-series mice: a voltage, turned into a share by
    a Li-Po curve). Through the receiver's vendor collection (usage page
    0xFF00), which Windows lets a normal program open, with a software id of
    our own so replies meant for G HUB are not taken for ours.
  • Razer: the 90-byte feature report Synapse uses, class 0x07 command 0x80
    (level, 0-255) and 0x84 (charging), as openrazer documents it. A mouse's
    own collection opened with no access rights, which is enough for feature
    reports.
  • Bluetooth, any brand: the battery level Windows itself reads from a
    Bluetooth mouse (the device property its Settings show), matched to a
    mouse through the device container.

The file stays the last fallback. Read once a minute on a thread of its own:
a battery changes slowly, and every request wakes a sleeping mouse.

The protocol parts are pure and tested in tests/test_mouse_battery_win.py;
the Windows calls are not tested on a real PC yet.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import uuid

logger = logging.getLogger(__name__)

POLL_S = 60.0
LOGITECH, RAZER = 0x046D, 0x1532


def _state(pct, state: str, model: str, vendor: str, connection: str) -> dict:
    """The shape mouse_battery.py's UPower monitor reports."""
    return {"pct": None if pct is None else float(pct), "state": state, "model": model, "vendor": vendor,
            "connection_type": connection}


# ── Logitech: HID++ 2.0 ─────────────────────────────────────────────────────

HIDPP_LONG, HIDPP_LONG_LEN = 0x11, 20
SW_ID = 0x0B
ROOT, DEVICE_NAME = 0x0000, 0x0005
UNIFIED_BATTERY, BATTERY_STATUS, BATTERY_VOLTAGE = 0x1004, 0x1000, 0x1001
MOUSE_KINDS = {3, 4, 5}          # DEVICE_NAME's type: mouse, trackpad, trackball
RECEIVER_SLOTS = range(1, 7)
DIRECT = 0xFF                    # the device itself: on its cable, or Bluetooth
# A Li-Po cell's voltage against its charge (Solaar's table for Logitech's mice)
VOLTAGE_CURVE = ((4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60), (3811, 50), (3778, 40),
                 (3751, 30), (3717, 20), (3671, 10), (3646, 5), (3579, 2), (3500, 0))


def voltage_pct(mv: int) -> float:
    if mv >= VOLTAGE_CURVE[0][0]:
        return 100.0
    for (v1, p1), (v0, p0) in zip(VOLTAGE_CURVE, VOLTAGE_CURVE[1:]):
        if mv >= v0:
            return round(p0 + (p1 - p0) * (mv - v0) / (v1 - v0), 1)
    return 0.0


def unified_battery(data: bytes) -> tuple[float, str]:
    """UNIFIED_BATTERY get_status: charge %, level flags, charging status, external power."""
    return float(data[0]), {0: "discharging", 1: "charging", 2: "charging", 3: "full"}.get(data[2], "unknown")


def battery_status(data: bytes) -> tuple[float, str]:
    """BATTERY_STATUS: charge %, next level, status."""
    return float(data[0]), {0: "discharging", 1: "charging", 2: "charging", 3: "full",
                            4: "charging"}.get(data[2], "unknown")


def battery_voltage(data: bytes) -> tuple[float, str]:
    """BATTERY_VOLTAGE: millivolts (big-endian), then flags: 0x80 on external
    power, its low bits whether the charge is done."""
    mv, flags = data[0] << 8 | data[1], data[2]
    if flags & 0x80:
        return voltage_pct(mv), "full" if flags & 0x07 in (1, 2) else "charging"
    return voltage_pct(mv), "discharging"


class Hidpp:
    """A HID++ 2.0 device at `index` behind `io` (write(report), read(timeout) → report or None)."""

    def __init__(self, io, index: int, timeout: float = 0.5):
        self.io, self.index, self.timeout = io, index, timeout
        self._features: dict[int, int | None] = {}

    def call(self, feature_index: int, function: int, *params: int) -> bytes | None:
        """The reply's parameters, None for an error or no reply."""
        fn = function << 4 | SW_ID
        self.io.write(bytes((HIDPP_LONG, self.index, feature_index, fn, *params)).ljust(HIDPP_LONG_LEN, b"\0"))
        deadline = time.monotonic() + self.timeout
        while (left := deadline - time.monotonic()) > 0:
            r = self.io.read(left)
            if not r:
                return None
            if len(r) < 5 or r[1] != self.index:
                continue                     # another device's, or another program's
            if r[2] in (0x8F, 0xFF) and r[3] == feature_index and r[4] == fn:
                return None                  # an error (HID++ 1.0 or 2.0) for this request
            if r[2] == feature_index and r[3] == fn:
                return bytes(r[4:])
        return None

    def ping(self) -> bool:
        r = self.call(0, 1, 0, 0, 0xAA)
        return r is not None and len(r) >= 3 and r[2] == 0xAA

    def feature(self, feature_id: int) -> int | None:
        if feature_id not in self._features:
            r = self.call(0, 0, feature_id >> 8, feature_id & 0xFF)
            self._features[feature_id] = r[0] if r and r[0] else None
        return self._features[feature_id]

    def kind(self) -> int | None:
        i = self.feature(DEVICE_NAME)
        r = self.call(i, 2) if i else None
        return r[0] if r else None

    def name(self) -> str:
        i = self.feature(DEVICE_NAME)
        r = self.call(i, 0) if i else None
        if not r:
            return ""
        count, out = r[0], b""
        while len(out) < count:
            chunk = self.call(i, 1, len(out))
            if not chunk:
                break
            out += chunk[:count - len(out)]
        return out.decode("utf-8", errors="replace").strip("\0 ")

    def battery(self) -> tuple[float, str] | None:
        for fid, function, parse in ((UNIFIED_BATTERY, 1, unified_battery), (BATTERY_STATUS, 0, battery_status),
                                     (BATTERY_VOLTAGE, 0, battery_voltage)):
            i = self.feature(fid)
            r = self.call(i, function) if i else None
            if r and len(r) >= 3:
                return parse(r)
        return None


def logitech_mouse(io, direct: bool, connection: str) -> dict | None:
    """The first mouse with a battery behind one HID++ collection."""
    for index in (DIRECT,) if direct else RECEIVER_SLOTS:
        dev = Hidpp(io, index)
        if not dev.ping():
            continue                         # no device in that slot, or asleep
        if dev.kind() not in MOUSE_KINDS:
            continue
        got = dev.battery()
        if got:
            return _state(got[0], got[1], dev.name() or "Logitech mouse", "Logitech", connection)
    return None


# ── Razer ─────────────────────────────────────────────────────────────────────

RAZER_REPORT_LEN = 90
RAZER_TRANSACTIONS = (0x1F, 0x3F, 0xFF)      # differs by model: tried in turn
RAZER_OK = 0x02


def razer_request(command_class: int, command_id: int, size: int, transaction: int) -> bytes:
    """A feature report (report id 0, then 90 bytes): status, transaction id,
    remaining packets, protocol, data size, class, id, 80 argument bytes,
    then the XOR of bytes 2-87."""
    r = bytearray(RAZER_REPORT_LEN)
    r[1], r[5], r[6], r[7] = transaction, size, command_class, command_id
    crc = 0
    for b in r[2:88]:
        crc ^= b
    r[88] = crc
    return b"\0" + bytes(r)


def razer_reply(raw: bytes, command_class: int, command_id: int) -> bytes | None:
    """The reply's argument bytes, None unless it is a success for that command."""
    r = raw[1:] if len(raw) == RAZER_REPORT_LEN + 1 else raw
    if len(r) < RAZER_REPORT_LEN or r[0] != RAZER_OK or r[6] != command_class or r[7] != command_id:
        return None
    return bytes(r[8:88])


def razer_mouse(io, model: str) -> dict | None:
    """`io`: set_feature(report), get_feature(length) → bytes."""
    for transaction in RAZER_TRANSACTIONS:
        level = _razer_ask(io, 0x07, 0x80, transaction)
        if level is None:
            continue
        charging = _razer_ask(io, 0x07, 0x84, transaction)
        state = "charging" if charging and charging[1] else "discharging"
        return _state(round(level[1] * 100 / 255, 1), state, model or "Razer mouse", "Razer", "usb")
    return None


def _razer_ask(io, command_class: int, command_id: int, transaction: int) -> bytes | None:
    if not io.set_feature(razer_request(command_class, command_id, 0x02, transaction)):
        return None
    time.sleep(0.02)                         # the mouse needs a moment before its answer is there
    raw = io.get_feature(RAZER_REPORT_LEN + 1)
    return razer_reply(raw, command_class, command_id) if raw else None


# ── Windows: HID devices ────────────────────────────────────────────────────

class _Hid:
    """One HID collection, opened with overlapped I/O (reads with a timeout)."""

    def __init__(self, api, path: str, access: int):
        self.api, self.path = api, path
        self.handle = api.open(path, access)
        if not self.handle:
            raise OSError(f"cannot open {path}")

    def write(self, report: bytes) -> bool:
        return self.api.write(self.handle, report)

    def read(self, timeout: float) -> bytes | None:
        return self.api.read(self.handle, 64, timeout)

    def set_feature(self, report: bytes) -> bool:
        return self.api.set_feature(self.handle, report)

    def get_feature(self, length: int) -> bytes | None:
        return self.api.get_feature(self.handle, length)

    def close(self) -> None:
        self.api.close(self.handle)


class _Win32Hid:
    """hid.dll, setupapi.dll and kernel32 through ctypes."""

    def __init__(self):
        from ctypes import wintypes as wt
        c = ctypes
        self._wt = wt
        self.hid = c.WinDLL("hid")
        self.setup = c.WinDLL("setupapi", use_last_error=True)
        self.k32 = c.WinDLL("kernel32", use_last_error=True)

        class GUID(c.Structure):
            _fields_ = [("bytes", c.c_ubyte * 16)]

        class IfaceData(c.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("guid", GUID), ("Flags", wt.DWORD), ("Reserved", c.c_size_t)]

        class Attributes(c.Structure):
            _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT), ("ProductID", wt.USHORT),
                        ("VersionNumber", wt.USHORT)]

        class Caps(c.Structure):
            _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT), ("InputReportByteLength", wt.USHORT),
                        ("OutputReportByteLength", wt.USHORT), ("FeatureReportByteLength", wt.USHORT),
                        ("Reserved", wt.USHORT * 17), ("Counts", wt.USHORT * 10)]

        class Overlapped(c.Structure):
            _fields_ = [("Internal", c.c_size_t), ("InternalHigh", c.c_size_t), ("Offset", wt.DWORD),
                        ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]

        self.GUID, self.IfaceData, self.Attributes, self.Caps, self.Overlapped = \
            GUID, IfaceData, Attributes, Caps, Overlapped
        self.setup.SetupDiGetClassDevsW.restype = wt.HANDLE
        self.setup.SetupDiGetClassDevsW.argtypes = [c.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
        self.setup.SetupDiEnumDeviceInterfaces.argtypes = [wt.HANDLE, c.c_void_p, c.c_void_p, wt.DWORD, c.c_void_p]
        self.setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [wt.HANDLE, c.c_void_p, c.c_void_p, wt.DWORD,
                                                                c.POINTER(wt.DWORD), c.c_void_p]
        self.setup.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
        self.k32.CreateFileW.restype = wt.HANDLE
        self.k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, c.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
        self.k32.CreateEventW.restype = wt.HANDLE
        self.k32.CreateEventW.argtypes = [c.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
        for name in ("ReadFile", "WriteFile"):
            getattr(self.k32, name).argtypes = [wt.HANDLE, c.c_void_p, wt.DWORD, c.POINTER(wt.DWORD), c.c_void_p]
        self.k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        self.k32.GetOverlappedResult.argtypes = [wt.HANDLE, c.c_void_p, c.POINTER(wt.DWORD), wt.BOOL]
        self.k32.CancelIo.argtypes = [wt.HANDLE]
        self.k32.CloseHandle.argtypes = [wt.HANDLE]
        for name in ("HidD_GetAttributes", "HidD_GetPreparsedData", "HidD_SetFeature", "HidD_GetFeature",
                     "HidD_GetProductString"):
            getattr(self.hid, name).restype = wt.BOOLEAN
        self.hid.HidD_GetAttributes.argtypes = [wt.HANDLE, c.c_void_p]
        self.hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, c.POINTER(c.c_void_p)]
        self.hid.HidD_FreePreparsedData.argtypes = [c.c_void_p]
        self.hid.HidP_GetCaps.argtypes = [c.c_void_p, c.c_void_p]
        self.hid.HidP_GetCaps.restype = c.c_long
        for name in ("HidD_SetFeature", "HidD_GetFeature", "HidD_GetProductString"):
            getattr(self.hid, name).argtypes = [wt.HANDLE, c.c_void_p, wt.ULONG]

    def devices(self) -> list[dict]:
        """Every HID collection: path, vid, pid, usage page, usage, report lengths, product."""
        c, wt = ctypes, self._wt
        guid = self.GUID()
        self.hid.HidD_GetHidGuid(c.byref(guid))
        info = self.setup.SetupDiGetClassDevsW(c.byref(guid), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
        if not info or info == c.c_void_p(-1).value:
            return []
        out = []
        try:
            i = 0
            while True:
                iface = self.IfaceData()
                iface.cbSize = c.sizeof(iface)
                if not self.setup.SetupDiEnumDeviceInterfaces(info, None, c.byref(guid), i, c.byref(iface)):
                    break
                i += 1
                need = wt.DWORD()
                self.setup.SetupDiGetDeviceInterfaceDetailW(info, c.byref(iface), None, 0, c.byref(need), None)
                buf = c.create_string_buffer(need.value + 2)
                c.cast(buf, c.POINTER(wt.DWORD))[0] = 8 if c.sizeof(c.c_void_p) == 8 else 6   # cbSize
                if not self.setup.SetupDiGetDeviceInterfaceDetailW(info, c.byref(iface), buf, need, None, None):
                    continue
                path = c.wstring_at(c.addressof(buf) + 4)
                described = self.describe(path)
                if described:
                    out.append(described)
        finally:
            self.setup.SetupDiDestroyDeviceInfoList(info)
        return out

    def describe(self, path: str) -> dict | None:
        c = ctypes
        h = self.open(path, 0)                   # no access: enough to ask what it is, even of a mouse
        if not h:
            return None
        try:
            attrs = self.Attributes()
            attrs.Size = c.sizeof(attrs)
            if not self.hid.HidD_GetAttributes(h, c.byref(attrs)):
                return None
            pre = c.c_void_p()
            caps = self.Caps()
            if not self.hid.HidD_GetPreparsedData(h, c.byref(pre)):
                return None
            try:
                if self.hid.HidP_GetCaps(pre, c.byref(caps)) != 0x00110000:     # HIDP_STATUS_SUCCESS
                    return None
            finally:
                self.hid.HidD_FreePreparsedData(pre)
            name = c.create_unicode_buffer(128)
            product = name.value if self.hid.HidD_GetProductString(h, name, c.sizeof(name)) else ""
            return {"path": path, "vid": attrs.VendorID, "pid": attrs.ProductID, "page": caps.UsagePage,
                    "usage": caps.Usage, "in": caps.InputReportByteLength, "out": caps.OutputReportByteLength,
                    "feature": caps.FeatureReportByteLength, "product": product}
        finally:
            self.close(h)

    def open(self, path: str, access: int):
        h = self.k32.CreateFileW(path, access, 3, None, 3, 0x40000000, None)  # share rw, OPEN_EXISTING, OVERLAPPED
        return None if not h or h == ctypes.c_void_p(-1).value else h

    def close(self, handle) -> None:
        self.k32.CloseHandle(handle)

    def _io(self, fn, handle, buf, timeout: float) -> int | None:
        c, wt = ctypes, self._wt
        ov = self.Overlapped()
        ov.hEvent = self.k32.CreateEventW(None, True, False, None)
        try:
            done = wt.DWORD()
            if not fn(handle, buf, len(buf), None, c.byref(ov)) and c.get_last_error() != 997:   # IO_PENDING
                return None
            if self.k32.WaitForSingleObject(ov.hEvent, int(timeout * 1000)) != 0:
                self.k32.CancelIo(handle)
                self.k32.GetOverlappedResult(handle, c.byref(ov), c.byref(done), True)
                return None
            if not self.k32.GetOverlappedResult(handle, c.byref(ov), c.byref(done), False):
                return None
            return done.value
        finally:
            self.k32.CloseHandle(ov.hEvent)

    def write(self, handle, report: bytes) -> bool:
        buf = ctypes.create_string_buffer(report, len(report))
        return bool(self._io(self.k32.WriteFile, handle, buf, 1.0))

    def read(self, handle, size: int, timeout: float) -> bytes | None:
        buf = ctypes.create_string_buffer(size)
        n = self._io(self.k32.ReadFile, handle, buf, timeout)
        return buf.raw[:n] if n else None

    def set_feature(self, handle, report: bytes) -> bool:
        buf = ctypes.create_string_buffer(report, len(report))
        return bool(self.hid.HidD_SetFeature(handle, buf, len(report)))

    def get_feature(self, handle, length: int) -> bytes | None:
        buf = ctypes.create_string_buffer(length)                # report id 0 in its first byte
        return buf.raw if self.hid.HidD_GetFeature(handle, buf, length) else None


def read_hid_mice(api) -> list[dict]:
    """Every mouse battery the HID protocols above can read."""
    found = []
    seen_razer = set()
    for d in api.devices():
        try:
            if d["vid"] == LOGITECH and d["page"] == 0xFF00 and d["usage"] == 0x0002 and d["in"] >= HIDPP_LONG_LEN:
                # The long-report collection: requests and replies are 20 bytes
                io = _Hid(api, d["path"], 0xC0000000)                 # read and write
                try:
                    direct = "receiver" not in d["product"].lower()
                    got = logitech_mouse(io, direct, "usb" if direct else "wireless")
                finally:
                    io.close()
                if got:
                    found.append(got)
            elif d["vid"] == RAZER and d["feature"] >= RAZER_REPORT_LEN + 1 and d["pid"] not in seen_razer:
                io = _Hid(api, d["path"], 0)
                try:
                    got = razer_mouse(io, d["product"])
                finally:
                    io.close()
                if got:
                    seen_razer.add(d["pid"])
                    found.append(got)
        except OSError as e:
            logger.debug("mouse battery: %s: %s", d.get("product") or d.get("path"), e)
    return found


# ── Windows: Bluetooth ──────────────────────────────────────────────────────

# The battery level Windows reads from a Bluetooth device ({104EA319-…}, 2: a byte)
BT_BATTERY = ("104ea319-6ee2-4701-bd47-8ddbf425bbe5", 2)
CONTAINER_ID = ("8c7ed206-3f8a-4827-b3ab-ae9e1faefc6c", 2)
FRIENDLY_NAME = ("a45c254e-df1c-4efd-8020-67d146a850e0", 14)
MOUSE_CLASS = uuid.UUID("4d36e96f-e325-11ce-bfc1-08002be10318")


def bluetooth_mice() -> list[dict]:
    """Bluetooth mice whose battery Windows knows: devices with that property
    whose container also holds a device of the Mouse class."""
    from ctypes import wintypes as wt
    c = ctypes
    setup = c.WinDLL("setupapi", use_last_error=True)

    class DevInfo(c.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("ClassGuid", c.c_ubyte * 16), ("DevInst", wt.DWORD),
                    ("Reserved", c.c_size_t)]

    class PropKey(c.Structure):
        _fields_ = [("fmtid", c.c_ubyte * 16), ("pid", wt.DWORD)]

    setup.SetupDiGetClassDevsW.restype = wt.HANDLE
    setup.SetupDiGetClassDevsW.argtypes = [c.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
    setup.SetupDiEnumDeviceInfo.argtypes = [wt.HANDLE, wt.DWORD, c.c_void_p]
    setup.SetupDiGetDevicePropertyW.argtypes = [wt.HANDLE, c.c_void_p, c.c_void_p, c.POINTER(wt.ULONG),
                                                c.c_void_p, wt.DWORD, c.POINTER(wt.DWORD), wt.DWORD]
    setup.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]

    def key(k):
        return PropKey((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(k[0]).bytes_le), k[1])

    def prop(info, dev, k, size=512) -> bytes | None:
        kind, buf = wt.ULONG(), c.create_string_buffer(size)
        if not setup.SetupDiGetDevicePropertyW(info, c.byref(dev), c.byref(key(k)), c.byref(kind), buf, size,
                                               None, 0):
            return None
        return buf.raw

    info = setup.SetupDiGetClassDevsW(None, None, None, 0x6)              # PRESENT | ALLCLASSES
    if not info or info == c.c_void_p(-1).value:
        return []
    batteries, mice = [], set()
    try:
        i = 0
        while True:
            dev = DevInfo()
            dev.cbSize = c.sizeof(dev)
            if not setup.SetupDiEnumDeviceInfo(info, i, c.byref(dev)):
                break
            i += 1
            container = prop(info, dev, CONTAINER_ID, 16)
            if uuid.UUID(bytes_le=bytes(dev.ClassGuid)) == MOUSE_CLASS and container:
                mice.add(container)
            level = prop(info, dev, BT_BATTERY, 1)
            if level is not None and container:
                name = prop(info, dev, FRIENDLY_NAME) or b""
                batteries.append((container, level[0], name.decode("utf-16-le", errors="replace").split("\0")[0]))
    finally:
        setup.SetupDiDestroyDeviceInfoList(info)
    return [_state(pct, "discharging", name or "Bluetooth mouse", "", "bluetooth")
            for container, pct, name in batteries if container in mice and 0 <= pct <= 100]


# ── the monitor ───────────────────────────────────────────────────────────────

_STATE_RANK = {"charging": 3, "discharging": 2, "full": 1}


class WindowsMouseBattery:
    """get_state() as mouse_battery.UPowerMouseMonitor's, read every POLL_S
    on a thread. `read()` → readings (tests pass their own)."""

    def __init__(self, read=None, interval: float = POLL_S):
        self._read = read or _read_all
        self._interval = interval
        self._state: dict | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.on_battery_change = None

    def get_state(self) -> dict | None:
        """The best reading, None when no mouse was read (the server's file fallback then)."""
        with self._lock:
            return dict(self._state) if self._state else None

    def poll(self) -> None:
        try:
            found = self._read()
        except (OSError, AttributeError, ValueError) as e:        # ctypes, a DLL without the call
            logger.debug("mouse battery: %s", e)
            found = []
        best = max(found, key=lambda s: (_STATE_RANK.get(s["state"], 0), s["pct"] or 0), default=None)
        with self._lock:
            changed, self._state = best != self._state, best
        if changed and self.on_battery_change:
            self.on_battery_change()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self._interval)

    def start(self) -> None:
        threading.Thread(target=self._run, name="mouse-battery", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def _read_all() -> list[dict]:
    if not sys.platform.startswith("win"):
        return []
    found = read_hid_mice(_Win32Hid())
    try:
        found += bluetooth_mice()
    except OSError as e:
        logger.debug("mouse battery (Bluetooth): %s", e)
    return found
