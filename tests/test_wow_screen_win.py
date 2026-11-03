"""WoW's window on Windows (wow_addons/screen_win.py), without Windows: where
the client area sits in a capture, what a capture's frames are taken from,
what happens when a capture fails, and the keys and clipboard Windows gets.
The companion's own tests run the whole path through WinScreen as well."""
import ctypes
import logging

import pytest

import macro_system
from wow_addons import clipboard, screen
from wow_addons import screen_win as sw
from wow_addons.screen_win import WinScreen, client_offset, rows_rgb


# ── geometry and pixels ──────────────────────────────────────────────────────

def test_the_client_area_is_found_inside_the_window_frame():
    assert client_offset((101, 81, 1920, 1080), (100, 50, 2022, 1162)) == (1, 31)   # windowed: title bar, border
    assert client_offset((0, 0, 2560, 1440), (0, 0, 2560, 1440)) == (0, 0)          # borderless
    assert client_offset((-1912, 31, 1920, 1080), (-1920, 0, 16, 1142)) == (8, 31)  # on a monitor to the left
    assert client_offset((90, 81, 1920, 1080), (100, 50, 2022, 1162)) is None       # left of its own frame
    assert client_offset((101, 81, 2560, 1080), (100, 50, 2022, 1162)) is None      # a scaled rect: wider than it
    assert client_offset((101, 81, 0, 0), (100, 50, 2022, 1162)) is None            # minimised to nothing


def test_rows_come_back_as_rgb_whatever_the_row_pitch():
    bgrx = bytes((3, 2, 1, 255, 6, 5, 4, 255)) + b"\xee" * 8      # 2 pixels, padded to 16 bytes
    assert rows_rgb(bgrx * 2, 16, 2, 2) == bytes((1, 2, 3, 4, 5, 6)) * 2
    assert screen.bgra_to_rgb(bytes((30, 20, 10, 0))) == bytes((10, 20, 30))


# ── WinScreen ────────────────────────────────────────────────────────────────

class Api:
    """A desktop with WoW at 1920×1080 in a window with a title bar."""

    def __init__(self):
        self.wows = [0x10]
        self.front_hwnd = 0x10
        self.minimised = False
        self.blocked = False
        self.process = True
        self.client_rect = (101, 81, 1920, 1080)

    def foreground(self):
        return self.front_hwnd

    def is_wow(self, hwnd):
        return hwnd in self.wows

    def find(self):
        return self.wows[0] if self.wows else 0

    def iconic(self, hwnd):
        return self.minimised

    def client(self, hwnd):
        return self.client_rect

    def bounds(self, hwnd):
        return 100, 50, 2022, 1162

    def running(self):
        return self.process

    def keys_blocked(self, hwnd):
        return self.blocked


class Capture:
    """Frame rows of one colour per window, so a read says which window it came from."""

    def __init__(self, hwnd, log):
        self.hwnd, self.log, self.broken, self.closed = hwnd, log, False, False

    def read(self, x, y, w, rows, max_age):
        self.log.append((self.hwnd, x, y, w, rows, max_age))
        return bytes((self.hwnd & 0xFF, 0, 0, 255)) * w * rows, w * 4

    def close(self):
        self.closed = True


def make(api=None, *, capture=None, fallback=None, clock=None):
    log = []
    made = []

    def default(hwnd, _api):
        c = Capture(hwnd, log)
        made.append(c)
        return c
    s = WinScreen(api=api or Api(), capture=capture or default, fallback=fallback or default,
                  clock=clock or (lambda: 0.0))
    return s, log, made


def test_wow_in_front_is_told_from_another_window_and_from_no_wow_at_all():
    api = Api()
    s, _, _ = make(api)
    assert s.front() is True
    api.front_hwnd = 0x99
    assert s.front() is False
    api.wows = []
    assert s.front() is None                     # its process runs, its window cannot be found
    api.process = False
    assert s.front() is False                    # no WoW at all


def test_the_wow_window_in_front_is_the_one_read_when_there_are_several():
    api = Api()
    api.wows = [0x10, 0x20]
    api.front_hwnd = 0x20
    s, log, made = make(api)
    assert s.grab(0, 0, 4, 1) == bytes((0, 0, 0x20)) * 4
    api.front_hwnd = 0x99                        # another program in front: stay with the one seen last
    assert s.grab(0, 0, 4, 1) == bytes((0, 0, 0x20)) * 4
    api.front_hwnd = 0x10                        # the other client
    assert s.grab(0, 0, 4, 1) == bytes((0, 0, 0x10)) * 4
    assert made[0].closed and not made[1].closed  # its capture closed with the switch


def test_reads_are_of_the_client_area_not_the_title_bar():
    s, log, _ = make()
    assert s.size() == (1920, 1080)
    s.grab(0, 0, 1920, 1)
    s.beacon()
    (hwnd, x, y, w, rows, age), (_, bx, by, bw, brows, bage) = log
    assert (x, y, w, rows) == (1, 31, 1920, 1) and age == sw.STALE_S
    assert (bx, by, bw, brows) == (1, 31, 1920, screen.CELL_PX) and bage == sw.BEACON_AGE_S
    assert s.grab(1900, 0, 40, 1) is None        # past the client's right edge: not the border's pixels


def test_a_minimised_wow_cannot_be_seen_but_can_be_told_from_the_front(caplog):
    api = Api()
    api.minimised = True
    s, log, _ = make(api)
    assert s.beacon() is None and s.size() is None and s.grab(0, 0, 4, 1) is None
    assert s.front() is True and log == []


def test_a_client_area_outside_its_frame_is_refused_and_said_once(caplog):
    api = Api()
    api.client_rect = (101, 81, 2560, 1440)      # what a scaled, system-aware rectangle looks like
    s, log, _ = make(api)
    with caplog.at_level(logging.WARNING):
        assert s.beacon() is None and s.grab(0, 0, 4, 1) is None and s.size() is None
    assert log == [] and len([r for r in caplog.records if "not inside" in r.message]) == 1


def test_without_wgc_gdi_reads_it_and_wgc_is_tried_again_later(caplog):
    now = [0.0]
    tries = []

    def wgc(hwnd, api):
        tries.append(now[0])
        raise OSError("Windows.Graphics.Capture is not available")
    s, log, made = make(capture=wgc, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING):
        assert s.grab(0, 0, 4, 1) is not None    # through the fallback
        assert s.grab(0, 0, 4, 1) is not None
    assert tries == [0.0] and len(made) == 1
    assert len([r for r in caplog.records if "not available" in r.message]) == 1   # said once
    now[0] = sw.RETRY_WGC_S
    s.grab(0, 0, 4, 1)
    assert tries == [0.0, sw.RETRY_WGC_S] and made[0].closed


def test_nothing_that_can_read_waits_before_trying_again():
    now = [0.0]
    attempts = []

    def fails(hwnd, api):
        attempts.append(now[0])
        raise OSError("no")
    s, _, _ = make(capture=fails, fallback=fails, clock=lambda: now[0])
    assert s.beacon() is None and s.beacon() is None
    assert attempts == [0.0, 0.0]                # both, once
    now[0] = screen.RECONNECT_S
    s.beacon()
    assert len(attempts) == 4


def test_a_broken_capture_is_closed_and_made_again_later():
    now = [0.0]
    s, log, made = make(clock=lambda: now[0])
    s.grab(0, 0, 4, 1)
    made[0].broken = True                        # the GPU reset under it
    s.grab(0, 0, 4, 1)
    assert made[0].closed
    assert s.grab(0, 0, 4, 1) is None and len(made) == 1
    now[0] = screen.RECONNECT_S
    assert s.grab(0, 0, 4, 1) is not None and len(made) == 2


def test_an_elevated_wow_is_told_and_any_failure_reads_as_not():
    api = Api()
    s, _, _ = make(api)
    assert s.keys_blocked() is False
    api.blocked = True
    assert s.keys_blocked() is True

    def broken(hwnd):
        raise OSError("access denied")
    api.keys_blocked = broken
    assert s.keys_blocked() is False


def test_a_failing_call_answers_none_and_is_logged_once(caplog):
    api = Api()

    def gone():
        raise OSError("the desktop went away")
    api.foreground = gone
    s, _, _ = make(api)
    with caplog.at_level(logging.WARNING):
        assert s.front() is None and s.front() is None and s.beacon() is None
    assert len([r for r in caplog.records if "desktop went away" in r.message]) == 2   # front once, read once


# ── the WGC pool ─────────────────────────────────────────────────────────────

class Frame:
    def __init__(self, t, size=(100, 40), name=""):
        self.time, self.size, self.surface, self.closed = t, size, name, False

    def close(self):
        self.closed = True


class Pool:
    def __init__(self, size=(100, 40)):
        self.size = size
        self.queue = []
        self.recreated = []
        self.closed = False
        self.arrive = {}                 # clock value → a frame that arrives then

    def next(self):
        return self.queue.pop(0) if self.queue else None

    def recreate(self, size):
        self.recreated.append(size)

    def close(self):
        self.closed = True


class Gpu:
    def __init__(self):
        self.copies = []
        self.fail = None

    def copy(self, surface, x, y, w, rows):
        if self.fail:
            raise self.fail
        self.copies.append((surface, x, y, w, rows))
        return surface.encode() * (w * rows), w * 4

    def close(self):
        pass


class Clock:
    def __init__(self, pool, t=10.0):
        self.t, self.pool = t, pool

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t = round(self.t + s, 6)
        for at in [a for a in self.pool.arrive if a <= self.t]:
            self.pool.queue.append(self.pool.arrive.pop(at))


def frames(size=(100, 40)):
    pool, gpu = Pool(size), Gpu()
    clock = Clock(pool)
    return sw._Frames(pool, gpu, clock, clock.sleep), pool, gpu, clock


def test_a_read_takes_the_newest_frame_and_lets_the_rest_go():
    f, pool, gpu, clock = frames()
    old, older, newest = Frame(9.90, name="a"), Frame(9.95, name="b"), Frame(9.99, name="c")
    pool.queue = [old, older, newest]
    raw, pitch = f.read(1, 2, 10, 1, sw.STALE_S)
    assert raw[:1] == b"c" and pitch == 40
    assert old.closed and older.closed and newest.closed          # each buffer back to the pool
    assert gpu.copies == [("c", 1, 2, 10, screen.CELL_PX)]        # the beacon's rows too, for the next read


def test_an_old_frame_waits_for_the_next_one():
    f, pool, gpu, clock = frames()
    stale = Frame(9.0, name="o")                 # captured just after the last read, a second ago
    pool.queue = [stale]
    pool.arrive = {10.02: Frame(10.016, name="n")}
    raw, _ = f.read(0, 0, 10, 4, sw.BEACON_AGE_S)
    assert raw[:1] == b"n" and stale.closed and clock.t <= 10.0 + sw.WAIT_S


def test_with_no_fresh_frame_the_beacon_is_not_read_but_the_link_may_use_it():
    f, pool, gpu, clock = frames()
    pool.queue = [Frame(9.5, name="o")]          # WoW drew nothing new for half a second
    assert f.read(0, 0, 10, 4, sw.BEACON_AGE_S) is None
    assert f.read(0, 0, 10, 1, sw.STALE_S)[0][:1] == b"o"        # the rows copied from it
    clock.t = 11.0
    assert f.read(0, 0, 10, 1, sw.STALE_S) is None               # older than the link takes
    assert f.read(5, 0, 10, 1, sw.STALE_S) is None               # another rectangle: never copied


def test_the_link_waits_about_a_frame_and_the_beacon_for_a_fresh_one():
    f, pool, gpu, clock = frames()
    pool.queue = [Frame(9.6, name="o")]          # WoW in the background, drawing slowly
    pool.arrive = {10.05: Frame(10.05, name="n")}
    assert f.read(0, 0, 10, 1, sw.STALE_S)[0][:1] == b"o" and clock.t == 10.0 + sw.LINK_WAIT_S
    assert f.read(0, 0, 10, 4, sw.BEACON_AGE_S)[0][:1] == b"n" and clock.t == 10.052
    t = clock.t
    assert f.read(0, 0, 10, 1, sw.STALE_S)[0][:1] == b"n" and clock.t == t     # a fresh copy: no wait


def test_the_beacon_takes_the_links_copy_while_it_is_fresh():
    f, pool, gpu, clock = frames()
    pool.queue = [Frame(9.99, name="l")]
    f.read(0, 0, 10, 1, sw.STALE_S)              # the link's read copies the beacon's rows too
    clock.t = 10.03
    assert f.read(0, 0, 10, 4, sw.BEACON_AGE_S)[0][:1] == b"l" and clock.t == 10.03
    assert len(gpu.copies) == 1
    clock.t = 10.1                               # older than FRESH_S: the beacon waits for a frame
    assert f.read(0, 0, 10, 4, sw.BEACON_AGE_S) is None and clock.t == 10.1 + sw.WAIT_S


def test_a_resized_window_recreates_the_pool_and_skips_that_frame():
    f, pool, gpu, clock = frames()
    big = Frame(9.99, size=(200, 80))
    pool.queue = [big]
    assert f.read(0, 0, 10, 1, sw.STALE_S) is None
    assert pool.recreated == [(200, 80)] and big.closed and gpu.copies == []
    pool.queue = [Frame(10.0, size=(200, 80), name="r")]
    assert f.read(0, 0, 150, 1, sw.STALE_S)[0][:1] == b"r"       # wider than before: the new size


def test_a_box_outside_the_frame_is_not_copied():
    f, pool, gpu, clock = frames(size=(100, 2))
    pool.queue = [Frame(9.99, size=(100, 2), name="x")]
    assert f.read(0, 0, 10, 1, sw.STALE_S)[0][:1] == b"x"         # 1 row fits, 4 do not
    assert gpu.copies == [("x", 0, 0, 10, 1)]
    pool.queue = [Frame(9.99, size=(100, 2), name="y")]
    assert f.read(95, 0, 10, 1, sw.STALE_S) is None and gpu.copies[-1][0] == "x"


def test_a_gpu_that_fails_breaks_the_capture_and_still_lets_the_frame_go(caplog):
    f, pool, gpu, clock = frames()
    frame = Frame(9.99)
    pool.queue = [frame]
    gpu.fail = OSError("Map: the GPU device is gone")
    with caplog.at_level(logging.WARNING):
        assert f.read(0, 0, 10, 1, sw.STALE_S) is None
    assert f.broken and frame.closed and "device is gone" in caplog.text


def test_wgc_can_be_skipped_for_gdi(monkeypatch):
    monkeypatch.setenv(sw.FORCE, "gdi")
    with pytest.raises(RuntimeError):
        sw._wgc_capture(0x10, None)


# ── keys ─────────────────────────────────────────────────────────────────────

def scan_of(vk):
    # MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX) on a US keyboard
    return {0xA2: (0x1D, False), 0xA4: (0x38, False), 0xA0: (0x2A, False), 0x24: (0x47, True),
            0x23: (0x4F, True), 0x43: (0x2E, False), 0x0D: (0x1C, False), 0x70: (0x3B, False)}[vk]


def test_the_focus_chord_goes_out_as_a_keyboard_sends_it():
    down, up = macro_system._win_chord(("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTSHIFT", "KEY_HOME"), scan_of)
    assert down == [(0xA2, 0x1D, 0), (0xA4, 0x38, 0), (0xA0, 0x2A, 0), (0x24, 0x47, 1)]   # Home: extended
    assert up == [(0x24, 0x47, 3), (0xA0, 0x2A, 2), (0xA4, 0x38, 2), (0xA2, 0x1D, 2)]     # released in reverse


def test_keypad_enter_is_not_enter_and_an_unknown_key_refuses_the_chord():
    assert macro_system._win_chord(("KEY_KPENTER",), scan_of)[0] == [(0x0D, 0x1C, 1)]
    assert macro_system._win_chord(("KEY_ENTER",), scan_of)[0] == [(0x0D, 0x1C, 0)]
    with pytest.raises(ValueError):
        macro_system._win_chord(("KEY_LEFTCTRL", "KEY_NOSUCHKEY"), scan_of)


# ── the clipboard ────────────────────────────────────────────────────────────

class Win32Clipboard:
    """user32 and kernel32 as clipboard.py calls them, with real memory behind GlobalLock."""

    HWND = 0x77

    def __init__(self):
        self.mem, self.data, self.owner, self.open_by = {}, {}, None, None
        self.windows, self.next = set(), 1

    # user32
    def CreateWindowExW(self, ex, cls, title, style, x, y, w, h, parent, menu, inst, param):
        assert cls == "STATIC" and parent == clipboard._HWND_MESSAGE
        self.windows.add(self.HWND)
        return self.HWND

    def DestroyWindow(self, hwnd):
        self.windows.discard(hwnd)

    def OpenClipboard(self, hwnd):
        self.open_by = hwnd
        return 1

    def CloseClipboard(self):
        self.open_by = None
        return 1

    def EmptyClipboard(self):
        self.data.clear()
        self.owner = self.open_by          # with no owner, SetClipboardData would have nowhere to go
        return 1

    def SetClipboardData(self, fmt, handle):
        if not self.owner:
            return None
        self.data[fmt] = bytes(self.mem[handle])
        return handle

    def GetClipboardData(self, fmt):
        if fmt not in self.data:
            return None
        return self.GlobalAlloc(0, len(self.data[fmt]), self.data[fmt])

    # kernel32
    def GlobalAlloc(self, flags, size, init=None):
        h, self.next = self.next, self.next + 1
        self.mem[h] = ctypes.create_string_buffer(init or b"", size)
        return h

    def GlobalLock(self, h):
        return ctypes.addressof(self.mem[h])

    def GlobalUnlock(self, h):
        return 1

    def GlobalFree(self, h):
        self.mem.pop(h, None)

    def GlobalSize(self, h):
        return len(self.mem[h])


def test_a_write_is_owned_and_kept_out_of_clipboard_history(monkeypatch):
    fake = Win32Clipboard()
    private = [0xC001, 0xC002, 0xC003, 0xC004]
    monkeypatch.setattr(clipboard, "_api", (fake, fake, private))
    assert clipboard._win_set("TDX 1a2b ünïcode") is True
    assert fake.data[clipboard.CF_UNICODETEXT] == "TDX 1a2b ünïcode".encode("utf-16-le") + b"\0\0"
    assert all(fake.data[f] == b"\0\0\0\0" for f in private)       # history, cloud, managers: no
    assert fake.open_by is None and fake.windows == set()            # closed, its window gone
    assert clipboard._win_get() == "TDX 1a2b ünïcode"
