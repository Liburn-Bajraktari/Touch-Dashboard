"""WoW's window on Windows, with screen.py's contract: is it in front, is the
companion's box open (its beacon), and the top rows of its picture for the
live link.

The picture comes from Windows.Graphics.Capture (WGC, through pywinrt): the
window's own frames even while another window covers it (not while it is
minimised: Windows stops drawing it), without the yellow capture border on
Windows 11. Only the rows asked for leave the GPU: each read copies the client
area's top rows of the newest frame into a small staging texture (Direct3D 11
through ctypes, its methods called by vtable index as audio_system.py calls
IPolicyConfig). Where WGC cannot start, GDI reads the same rows off the
screen, which is right only while that edge of WoW is visible.

A WGC pool that is full drops new frames, not old ones: after a quiet second
its newest frame can be a second old. So each read empties the pool and, when
what is left is old, waits a moment for WoW's next frame; the beacon, which
lets keys through, is never read from an old one.

Coordinates are physical pixels. pywebview makes this process system-DPI-aware
(webview/platforms/winforms.py) and sizes its window on that, so the process
stays so; the window's geometry is read per-monitor-aware on this thread only,
or a monitor at another scale would hand back scaled rectangles and the
strip's 3-pixel cells would not line up.
"""
from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import threading
import time
import uuid

from .screen import CELL_PX, PROCESSES, RECONNECT_S, TITLES, bgra_to_rgb, find_beacon

logger = logging.getLogger(__name__)

CLASS = "GxWindowClass"         # WoW's window class, retail and classic
FRESH_S = 0.05                  # a frame this young is WoW's picture now
WAIT_S = 0.1                    # how long the beacon's read waits for a fresh one
LINK_WAIT_S = 0.02              # ... and the live link's: about a frame at 50 fps, then what there is
POLL_S = 0.004
BEACON_AGE_S = 0.15             # the beacon lets keys through: never read from an older frame
STALE_S = 1.0                   # the live link may use one this old (its pages carry WoW's clock)
RETRY_WGC_S = 60.0              # on GDI after WGC failed: try WGC again this often
FORCE = "TD_WOW_CAPTURE"        # "gdi": skip WGC (the probe compares the two)


def client_offset(client: tuple[int, int, int, int],
                  bounds: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """Where the client area (x, y, width, height on screen) starts in a
    capture of the window, whose frame is `bounds` (left, top, right, bottom):
    about (1, 31) for a window with a title bar, (0, 0) borderless. None when
    it does not fit inside, which no real window does."""
    x, y, w, h = client
    left, top, right, bottom = bounds
    ox, oy = x - left, y - top
    if w <= 0 or h <= 0 or ox < 0 or oy < 0 or ox + w > right - left or oy + h > bottom - top:
        return None
    return ox, oy


def rows_rgb(raw: bytes, pitch: int, width: int, rows: int) -> bytes:
    """`rows` rows of `width` B G R X pixels, `pitch` bytes apart, as packed R G B."""
    return b"".join(bgra_to_rgb(raw[r * pitch:r * pitch + width * 4]) for r in range(rows))


class WinScreen:
    """`api`: the window calls (_Win32Api); `capture(hwnd, api)` and
    `fallback(hwnd, api)` make what reads its picture (WGC, GDI). The tests
    pass stand-ins for all three."""

    def __init__(self, api=None, capture=None, fallback=None, clock=time.monotonic):
        self._api = api
        self._make = capture if capture is not None else _wgc_capture
        self._fallback = fallback if fallback is not None else _GdiCapture
        self._clock = clock
        self._lock = threading.Lock()
        self._hwnd = 0
        self._cap = None
        self._cap_until = None              # when a fallback capture gives WGC another try
        self._next_try = 0.0
        self._told: set[str] = set()

    def _tell(self, key: str, msg: str, *args) -> None:
        if key not in self._told:           # once a run: these repeat ten times a second
            self._told.add(key)
            logger.warning(msg, *args)

    def _win32(self):
        if self._api is None:
            self._api = _Win32Api()
        return self._api

    def _close(self) -> None:
        if self._cap is not None:
            try:
                self._cap.close()
            except OSError:
                pass
            self._cap = None

    def _find(self, api) -> int:
        """WoW's window: the one in front when that is WoW (several clients),
        else the one seen last, else the first there is."""
        fg = api.foreground()
        if fg and api.is_wow(fg):
            hwnd = fg
        elif self._hwnd and api.is_wow(self._hwnd):
            hwnd = self._hwnd
        else:
            hwnd = api.find()
        if hwnd != self._hwnd:
            self._close()
            self._hwnd = hwnd
            self._next_try = 0.0
        return hwnd

    def _geometry(self, api, hwnd: int) -> tuple[int, int, int, int] | None:
        """The client area's offset in the captured frame and its size."""
        client, bounds = api.client(hwnd), api.bounds(hwnd)
        off = client_offset(client, bounds) if client and bounds else None
        if off is None:
            self._tell("geometry", "WoW's client area %s is not inside its window %s", client, bounds)
            return None
        return off[0], off[1], client[2], client[3]

    def _capture(self, hwnd: int):
        if self._cap is not None and self._cap_until is not None and self._clock() >= self._cap_until:
            self._close()                    # WGC may have failed for a moment only
        if self._cap is None and self._clock() >= self._next_try:
            for make in (self._make, self._fallback):
                try:
                    self._cap = make(hwnd, self._win32())
                    self._cap_until = None if make is self._make else self._clock() + RETRY_WGC_S
                    break
                except (OSError, ImportError, RuntimeError, ValueError, AttributeError) as e:
                    self._tell(getattr(make, "__name__", "capture"), "WoW's window: %s cannot read it: %s",
                               getattr(make, "__name__", "capture"), e)
            if self._cap is None:
                self._next_try = self._clock() + RECONNECT_S
        return self._cap

    def _read(self, rows: int, max_age: float, x: int = 0, y: int = 0,
              width: int | None = None) -> tuple[bytes, int] | None:
        """RGB rows of the client area from (x, y), and the client's width."""
        api = self._win32()
        wow = self._find(api)
        if not wow or api.iconic(wow):
            return None
        geo = self._geometry(api, wow)
        if geo is None:
            return None
        ox, oy, w, h = geo
        width = w - x if width is None else width
        if x < 0 or y < 0 or width <= 0 or rows <= 0 or x + width > w or y + rows > h:
            return None
        cap = self._capture(wow)
        if cap is None:
            return None
        got = cap.read(ox + x, oy + y, width, rows, max_age)
        if cap.broken:                       # the GPU reset, the window went: start over later
            self._close()
            self._next_try = self._clock() + RECONNECT_S
        if got is None:
            return None
        raw, pitch = got
        return rows_rgb(raw, pitch, width, rows), w

    def _guarded(self, what: str, fn, default=None):
        with self._lock:
            try:
                return fn()
            except (OSError, ValueError, AttributeError, ctypes.ArgumentError) as e:
                self._tell(what, "Could not %s: %s", what, e)
                self._close()
                return default

    def front(self) -> bool | None:
        """True when WoW is the active window, False when another one is (or
        WoW is not running), None when this cannot be told."""
        def run():
            api = self._win32()
            wow = self._find(api)
            if not wow:
                return None if api.running() else False
            return api.foreground() == wow
        return self._guarded("tell which window is in front", run)

    def beacon(self) -> bool | None:
        """True while the companion's box shows its beacon, None when WoW's
        window cannot be seen (or not freshly enough to let keys through)."""
        def run():
            got = self._read(CELL_PX, BEACON_AGE_S)
            return None if got is None else find_beacon(*got) is not None
        return self._guarded("read WoW's window", run)

    def size(self) -> tuple[int, int] | None:
        """WoW's client area in pixels, or None when it cannot be seen."""
        def run():
            api = self._win32()
            wow = self._find(api)
            if not wow or api.iconic(wow):
                return None
            geo = self._geometry(api, wow)
            return (geo[2], geo[3]) if geo else None
        return self._guarded("read WoW's window", run)

    def grab(self, x: int, y: int, width: int, height: int) -> bytes | None:
        """RGB rows of a rectangle of WoW's client area, or None."""
        def run():
            got = self._read(height, STALE_S, x, y, width)
            return got[0] if got else None
        return self._guarded("read WoW's window", run)

    def release(self) -> None:
        """Let WoW's window go until the next read: the capture closes, so
        Windows stops copying each frame WoW draws for us."""
        with self._lock:
            self._close()

    def keys_blocked(self) -> bool:
        """WoW runs elevated (as administrator): Windows drops our keys on the way in."""
        def run():
            api = self._win32()
            wow = self._find(api)
            return bool(wow) and api.keys_blocked(wow)
        return self._guarded("tell whether WoW runs as administrator", run, False)


# ── Windows.Graphics.Capture ─────────────────────────────────────────────────

class _Frames:
    """The newest frame a WGC pool holds, and the rows copied from it.

    `pool`: next() → a frame (size, time, surface, close()) or None,
    recreate(size), size. `gpu`: copy(surface, x, y, w, rows) → (bytes, pitch).
    `now`: the clock frames are stamped with (the performance counter)."""

    def __init__(self, pool, gpu, now, sleep=time.sleep):
        self.pool, self.gpu, self.now, self.sleep = pool, gpu, now, sleep
        self.size = tuple(pool.size)
        self.broken = False
        self._rows = None                   # (x, y, w, n, raw, pitch, frame time) of the last copy

    def _newest(self):
        newest = None
        while (f := self.pool.next()) is not None:
            if newest is not None:
                newest.close()
            newest = f
        return newest

    def _age(self, frame, box) -> float:
        """How old the picture a read would get is: the frame's, else that of
        the rows copied last (when they hold this box)."""
        if frame is not None:
            return self.now() - frame.time
        c = self._rows
        if c is not None and c[:3] == box[:3] and c[3] >= box[3]:
            return self.now() - c[6]
        return float("inf")

    def _fresh(self, box, wait: float):
        """The newest frame, waited for (up to `wait`) while what a read would
        get is older than FRESH_S."""
        frame = self._newest()
        deadline = self.now() + wait
        while self._age(frame, box) > FRESH_S and self.now() < deadline:
            self.sleep(POLL_S)
            f = self._newest()
            if f is not None:
                if frame is not None:
                    frame.close()
                frame = f
        return frame

    def _take(self, frame, x: int, y: int, w: int, rows: int) -> None:
        try:
            size = tuple(frame.size)
            if size != self.size:            # the window was resized: this frame is the old size
                self.pool.recreate(size)
                self.size = size
                return
            n = max(rows, CELL_PX)           # the beacon's rows too, for the next read
            if x + w > size[0] or y + n > size[1]:
                n = rows
            if x + w > size[0] or y + n > size[1]:
                return                       # an invalid box would be ignored, not refused
            raw, pitch = self.gpu.copy(frame.surface, x, y, w, n)
            self._rows = (x, y, w, n, raw, pitch, frame.time)
        finally:
            frame.close()                    # after the copy is read back: the buffer goes to the next frame

    def read(self, x: int, y: int, w: int, rows: int, max_age: float):
        # The pool holds the first two frames after the last read and drops the
        # rest, so its newest can be a read's interval old. The beacon's read
        # (it lets keys through) waits for WoW's picture of now; the live link
        # waits only about a frame: waiting as long held the window's lock
        # 25-70 ms a read while WoW drew slowly in the background, and a
        # command's beacon check queued behind it.
        wait = WAIT_S if max_age <= BEACON_AGE_S else LINK_WAIT_S
        try:
            frame = self._fresh((x, y, w, rows), wait)
            if frame is not None:
                self._take(frame, x, y, w, rows)
        except OSError as e:
            self.broken = True
            logger.warning("WoW's window: the capture stopped (%s)", e)
            return None
        c = self._rows
        if c is None or c[:3] != (x, y, w) or c[3] < rows or self.now() - c[6] > max_age:
            return None
        return c[4], c[5]

    def close(self) -> None:
        for part in (self.pool, self.gpu):
            try:
                part.close()
            except OSError:
                pass


def _wgc_capture(hwnd: int, api) -> _Frames:
    if os.environ.get(FORCE, "").lower() == "gdi":
        raise RuntimeError(f"{FORCE}=gdi")
    gpu = _D3D11()
    try:
        pool = _WinrtPool(hwnd, gpu.winrt)
    except BaseException:
        gpu.close()
        raise
    return _Frames(pool, gpu, _qpc_seconds)


def _qpc_seconds() -> float:
    """The performance counter in seconds: the clock WGC stamps frames with."""
    count, freq = ctypes.c_int64(), ctypes.c_int64()
    k32 = ctypes.windll.kernel32
    k32.QueryPerformanceCounter(ctypes.byref(count))
    k32.QueryPerformanceFrequency(ctypes.byref(freq))
    return count.value / freq.value


class _WinrtFrame:
    __slots__ = ("_f", "size", "time", "surface")

    def __init__(self, f):
        self._f = f
        s = f.content_size
        self.size = (s.width, s.height)
        self.time = f.system_relative_time.total_seconds()
        self.surface = f.surface

    def close(self) -> None:
        self._f.close()


class _WinrtPool:
    """A free-threaded WGC frame pool on WoW's window: no dispatcher queue,
    and no callback into Python (frames are taken when a read wants one)."""

    def __init__(self, hwnd: int, device):
        from winrt.windows.graphics.capture import Direct3D11CaptureFramePool, GraphicsCaptureSession
        from winrt.windows.graphics.capture.interop import create_for_window
        from winrt.windows.graphics.directx import DirectXPixelFormat
        if not GraphicsCaptureSession.is_supported():
            raise OSError("Windows.Graphics.Capture is not available")
        self._device = device
        self._format = DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
        self._item = create_for_window(hwnd)
        size = self._item.size
        self.size = (size.width, size.height)
        self._pool = Direct3D11CaptureFramePool.create_free_threaded(device, self._format, 2, size)
        self._session = self._pool.create_capture_session(self._item)
        # No cursor over the strip; no yellow border around WoW (Windows 11).
        for name in ("is_cursor_capture_enabled", "is_border_required"):
            try:
                setattr(self._session, name, False)
            except (AttributeError, OSError) as e:
                logger.info("WoW's window: the capture keeps %s (%s)", name, e)
        self._session.start_capture()

    def next(self):
        f = self._pool.try_get_next_frame()
        return None if f is None else _WinrtFrame(f)

    def recreate(self, size: tuple[int, int]) -> None:
        self._pool.recreate(self._device, self._format, 2, size)

    def close(self) -> None:
        for part in (self._session, self._pool):
            try:
                part.close()
            except OSError:
                pass


_IID_MULTITHREAD = "9b7e4e00-342c-4106-a19f-4f2704f689f0"   # ID3D10Multithread
_IID_DXGI_DEVICE = "54ec77fa-1377-44e6-8c32-88fd5f44c84c"   # IDXGIDevice
_IID_TEXTURE2D = "6f15aaf2-d208-4e89-9ab4-489535d34f9c"     # ID3D11Texture2D
# Vtable slots (d3d11.h, d3d10.h): IUnknown 0-2, then each interface's own in order.
_QI, _RELEASE = 0, 2
_DEV_CREATE_TEXTURE2D, _DEV_REMOVED_REASON = 5, 39
_CTX_MAP, _CTX_UNMAP, _CTX_COPY_REGION = 14, 15, 46
_MT_SET_PROTECTED = 5
_DEVICE_GONE = {0x887A0005, 0x887A0006, 0x887A0007}          # removed, hung, reset


class _D3D11:
    """A Direct3D 11 device for WGC, and a copy of a frame's rows to the CPU."""

    def __init__(self):
        from ctypes import POINTER, byref, c_int, c_uint, c_void_p
        c = ctypes
        self._staging, self._staging_size = None, None
        self.dev = self.ctx = None
        self.winrt = None

        class Desc(c.Structure):
            _fields_ = [(n, c_uint) for n in ("Width", "Height", "MipLevels", "ArraySize", "Format",
                                             "SampleCount", "SampleQuality", "Usage", "BindFlags",
                                             "CPUAccessFlags", "MiscFlags")]

        class Box(c.Structure):
            _fields_ = [(n, c_uint) for n in ("left", "top", "front", "right", "bottom", "back")]

        class Mapped(c.Structure):
            _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]

        self._Desc, self._Box, self._Mapped = Desc, Box, Mapped
        d3d11 = c.WinDLL("d3d11")
        d3d11.D3D11CreateDevice.argtypes = [c_void_p, c_int, c_void_p, c_uint, c_void_p, c_uint, c_uint,
                                            POINTER(c_void_p), POINTER(c_uint), POINTER(c_void_p)]
        d3d11.D3D11CreateDevice.restype = c.c_long
        dev, ctx, level = c_void_p(), c_void_p(), c_uint()
        hr = -1
        for driver in (1, 5):            # the GPU, else WARP (no GPU: a VM, a CI runner)
            hr = d3d11.D3D11CreateDevice(None, driver, None, 0x20, None, 0, 7,
                                         byref(dev), byref(level), byref(ctx))   # 0x20: BGRA, which WGC needs
            if hr >= 0:
                break
        if hr < 0:
            raise OSError(f"D3D11CreateDevice failed (0x{hr & 0xFFFFFFFF:08X})")
        self.dev, self.ctx = dev.value, ctx.value
        try:
            # WGC uses the device from its own thread while ours copies from it.
            mt = self._qi(self.dev, _IID_MULTITHREAD)
            self._fn(mt, _MT_SET_PROTECTED, c_int, c_int)(mt, 1)
            self._release(mt)
            dxgi = self._qi(self.dev, _IID_DXGI_DEVICE)
            try:
                from winrt.windows.graphics.directx.direct3d11.interop import \
                    create_direct3d11_device_from_dxgi_device
                self.winrt = create_direct3d11_device_from_dxgi_device(dxgi)
            finally:
                self._release(dxgi)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _fn(ptr: int, slot: int, restype, *argtypes):
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
        fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[slot]
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn)

    def _qi(self, ptr: int, iid: str) -> int:
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(iid).bytes_le)
        out = ctypes.c_void_p()
        hr = self._fn(ptr, _QI, ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            ptr, ctypes.byref(guid), ctypes.byref(out))
        self._check(hr, f"QueryInterface {iid}")
        return out.value

    def _release(self, ptr: int | None) -> None:
        if ptr:
            self._fn(ptr, _RELEASE, ctypes.c_ulong)(ptr)

    def _check(self, hr: int, what: str) -> None:
        if hr >= 0:
            return
        code = hr & 0xFFFFFFFF
        if code in _DEVICE_GONE and self.dev:
            why = self._fn(self.dev, _DEV_REMOVED_REASON, ctypes.c_long)(self.dev) & 0xFFFFFFFF
            raise OSError(f"{what}: the GPU device is gone (0x{code:08X}, reason 0x{why:08X})")
        raise OSError(f"{what} failed (0x{code:08X})")

    def _staging_for(self, w: int, h: int) -> int:
        if self._staging_size != (w, h):
            self._release(self._staging)
            self._staging, self._staging_size = None, None
            # B8G8R8A8 (87), staging (3), readable by the CPU (0x20000)
            desc = self._Desc(w, h, 1, 1, 87, 1, 0, 3, 0, 0x20000, 0)
            out = ctypes.c_void_p()
            hr = self._fn(self.dev, _DEV_CREATE_TEXTURE2D, ctypes.c_long, ctypes.POINTER(self._Desc),
                          ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                self.dev, ctypes.byref(desc), None, ctypes.byref(out))
            self._check(hr, "CreateTexture2D")
            self._staging, self._staging_size = out.value, (w, h)
        return self._staging

    def copy(self, surface, x: int, y: int, w: int, rows: int) -> tuple[bytes, int]:
        from winrt.windows.graphics.directx.direct3d11.interop import get_dxgi_surface_from_object
        c = ctypes
        dxgi = get_dxgi_surface_from_object(surface)      # ours to release
        try:
            tex = self._qi(dxgi, _IID_TEXTURE2D)
        finally:
            self._release(dxgi)
        try:
            staging = self._staging_for(w, rows)
            box = self._Box(x, y, 0, x + w, y + rows, 1)
            self._fn(self.ctx, _CTX_COPY_REGION, None, c.c_void_p, c.c_uint, c.c_uint, c.c_uint, c.c_uint,
                     c.c_void_p, c.c_uint, c.POINTER(self._Box))(
                self.ctx, staging, 0, 0, 0, 0, tex, 0, c.byref(box))
            mapped = self._Mapped()
            hr = self._fn(self.ctx, _CTX_MAP, c.c_long, c.c_void_p, c.c_uint, c.c_uint, c.c_uint,
                          c.POINTER(self._Mapped))(self.ctx, staging, 0, 1, 0, c.byref(mapped))   # 1: read
            self._check(hr, "Map")
            try:
                pitch = mapped.RowPitch
                raw = c.string_at(mapped.pData, pitch * (rows - 1) + w * 4)
            finally:
                self._fn(self.ctx, _CTX_UNMAP, None, c.c_void_p, c.c_uint)(self.ctx, staging, 0)
            return raw, pitch
        finally:
            self._release(tex)

    def close(self) -> None:
        if self.winrt is not None:
            try:
                self.winrt.close()
            except OSError:
                pass
            self.winrt = None
        for name in ("_staging", "ctx", "dev"):
            self._release(getattr(self, name))
            setattr(self, name, None)


# ── GDI, where WGC cannot start ──────────────────────────────────────────────

class _GdiCapture:
    """The client area's rows as the screen shows them: right only while that
    edge of WoW is visible (another window over it, and the checksum and the
    beacon simply fail to match)."""

    broken = False

    def __init__(self, hwnd: int, api):
        from ctypes import wintypes as wt
        c = ctypes
        self._hwnd, self._api = hwnd, api
        self._u = c.WinDLL("user32", use_last_error=True)
        self._g = c.WinDLL("gdi32", use_last_error=True)
        self._u.GetDC.argtypes = [wt.HWND]
        self._u.GetDC.restype = wt.HDC
        self._u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        self._g.CreateCompatibleDC.argtypes = [wt.HDC]
        self._g.CreateCompatibleDC.restype = wt.HDC
        self._g.CreateDIBSection.argtypes = [wt.HDC, c.c_void_p, wt.UINT, c.POINTER(c.c_void_p),
                                             wt.HANDLE, wt.DWORD]
        self._g.CreateDIBSection.restype = wt.HBITMAP
        self._g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        self._g.SelectObject.restype = wt.HGDIOBJ
        self._g.BitBlt.argtypes = [wt.HDC, c.c_int, c.c_int, c.c_int, c.c_int, wt.HDC, c.c_int, c.c_int,
                                   wt.DWORD]
        self._g.DeleteObject.argtypes = [wt.HGDIOBJ]
        self._g.DeleteDC.argtypes = [wt.HDC]

    def read(self, x: int, y: int, w: int, rows: int, max_age: float):
        c = ctypes
        bounds = self._api.bounds(self._hwnd)
        if not bounds:
            return None
        header = (c.c_uint32 * 10)(40, w, c.c_uint32(-rows).value, 1 | 32 << 16, 0, 0, 0, 0, 0, 0)
        bits = c.c_void_p()
        with self._api.physical():
            screen = self._u.GetDC(None)
            if not screen:
                return None
            mem = self._g.CreateCompatibleDC(screen)
            dib = self._g.CreateDIBSection(screen, header, 0, c.byref(bits), None, 0) if mem else None
            try:
                if not dib:
                    return None
                old = self._g.SelectObject(mem, dib)
                ok = self._g.BitBlt(mem, 0, 0, w, rows, screen, bounds[0] + x, bounds[1] + y, 0x00CC0020)
                self._g.SelectObject(mem, old)
                # top-down 32-bit rows, no padding
                return (c.string_at(bits, w * rows * 4), w * 4) if ok else None
            finally:
                if dib:
                    self._g.DeleteObject(dib)
                if mem:
                    self._g.DeleteDC(mem)
                self._u.ReleaseDC(None, screen)

    def close(self) -> None:
        pass


# ── The window ───────────────────────────────────────────────────────────────

class _Win32Api:
    """user32, dwmapi and the process's token, every coordinate in physical pixels."""

    def __init__(self):
        from ctypes import POINTER, c_int, c_void_p
        from ctypes import wintypes as wt
        c = ctypes
        u = c.WinDLL("user32", use_last_error=True)
        for name, args, res in (
                ("FindWindowExW", [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR], wt.HWND),
                ("GetClassNameW", [wt.HWND, wt.LPWSTR, c_int], c_int),
                ("GetWindowTextW", [wt.HWND, wt.LPWSTR, c_int], c_int),
                ("IsWindow", [wt.HWND], wt.BOOL),
                ("IsIconic", [wt.HWND], wt.BOOL),
                ("GetForegroundWindow", [], wt.HWND),
                ("GetAncestor", [wt.HWND, wt.UINT], wt.HWND),
                ("GetClientRect", [wt.HWND, POINTER(wt.RECT)], wt.BOOL),
                ("ClientToScreen", [wt.HWND, POINTER(wt.POINT)], wt.BOOL),
                ("GetWindowRect", [wt.HWND, POINTER(wt.RECT)], wt.BOOL),
                ("GetWindowThreadProcessId", [wt.HWND, POINTER(wt.DWORD)], wt.DWORD),
                ("SetThreadDpiAwarenessContext", [c_void_p], c_void_p)):
            fn = getattr(u, name)
            fn.argtypes, fn.restype = args, res
        dwm = c.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, c_void_p, wt.DWORD]
        dwm.DwmGetWindowAttribute.restype = c.c_long
        k32 = c.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.OpenProcess.restype = wt.HANDLE
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.CloseHandle.argtypes = [wt.HANDLE]
        adv = c.WinDLL("advapi32", use_last_error=True)
        adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, POINTER(wt.HANDLE)]
        adv.GetTokenInformation.argtypes = [wt.HANDLE, c_int, c_void_p, wt.DWORD, POINTER(wt.DWORD)]
        adv.GetSidSubAuthorityCount.argtypes = [c_void_p]
        adv.GetSidSubAuthorityCount.restype = POINTER(c.c_ubyte)
        adv.GetSidSubAuthority.argtypes = [c_void_p, wt.DWORD]
        adv.GetSidSubAuthority.restype = POINTER(wt.DWORD)
        self._u, self._dwm, self._k32, self._adv, self._wt = u, dwm, k32, adv, wt
        self._told = False

    @contextlib.contextmanager
    def physical(self):
        """Per-monitor DPI awareness for this thread while inside: rectangles in physical pixels."""
        old = self._u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
        try:
            yield
        finally:
            if old:
                self._u.SetThreadDpiAwarenessContext(old)

    def _text(self, fn, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        fn(hwnd, buf, 256)
        return buf.value

    def _process(self, hwnd: int) -> str:
        pid = self._wt.DWORD()
        self._u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            import psutil
            return psutil.Process(pid.value).name().lower()
        except Exception:          # psutil's NoSuchProcess / AccessDenied, or no psutil
            return ""

    def is_wow(self, hwnd: int) -> bool:
        if not hwnd or not self._u.IsWindow(hwnd) or self._text(self._u.GetWindowTextW, hwnd) not in TITLES:
            return False
        return self._text(self._u.GetClassNameW, hwnd) == CLASS or self._process(hwnd) in PROCESSES

    def find(self) -> int:
        """The first top-level window that is WoW's: by its class, else by its
        title and process (should the class ever change)."""
        for cls, title in ((CLASS, None), (None, TITLES[0])):
            h = None
            while h := self._u.FindWindowExW(None, h, cls, title):
                if self.is_wow(h):
                    return h
        return 0

    def foreground(self) -> int:
        fg = self._u.GetForegroundWindow()
        return (self._u.GetAncestor(fg, 2) if fg else 0) or 0       # 2: GA_ROOT

    def iconic(self, hwnd: int) -> bool:
        return bool(self._u.IsIconic(hwnd))

    def client(self, hwnd: int) -> tuple[int, int, int, int] | None:
        rect, pt = self._wt.RECT(), self._wt.POINT(0, 0)
        with self.physical():
            if not (self._u.GetClientRect(hwnd, ctypes.byref(rect))
                    and self._u.ClientToScreen(hwnd, ctypes.byref(pt))):
                return None
        return pt.x, pt.y, rect.right - rect.left, rect.bottom - rect.top

    def bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        """The window's frame as a capture of it covers it (physical pixels whatever the thread's DPI mode)."""
        rect = self._wt.RECT()
        if self._dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) < 0:  # 9: EXTENDED_FRAME_BOUNDS
            with self.physical():
                if not self._u.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return None
        return rect.left, rect.top, rect.right, rect.bottom

    def running(self) -> bool:
        try:
            import psutil
        except ImportError:
            return False
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() in PROCESSES:
                return True
        return False

    def _integrity(self, pid: int | None) -> int | None:
        """The process's mandatory integrity level (0x2000 medium, 0x3000 high: elevated)."""
        k32, adv, wt = self._k32, self._adv, self._wt
        proc = k32.GetCurrentProcess() if pid is None else k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not proc:
            return None
        token = wt.HANDLE()
        try:
            if not adv.OpenProcessToken(proc, 0x8, ctypes.byref(token)):                         # TOKEN_QUERY
                return None
            try:
                need = wt.DWORD()
                adv.GetTokenInformation(token, 25, None, 0, ctypes.byref(need))                   # TokenIntegrityLevel
                buf = ctypes.create_string_buffer(need.value)
                if not need.value or not adv.GetTokenInformation(token, 25, buf, need, ctypes.byref(need)):
                    return None
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]   # TOKEN_MANDATORY_LABEL.Label.Sid
                return adv.GetSidSubAuthority(sid, adv.GetSidSubAuthorityCount(sid)[0] - 1)[0]
            finally:
                k32.CloseHandle(token)
        finally:
            if pid is not None:
                k32.CloseHandle(proc)

    def keys_blocked(self, hwnd: int) -> bool:
        pid = self._wt.DWORD()
        self._u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        ours, theirs = self._integrity(None), self._integrity(pid.value)
        if ours is None or theirs is None:
            # Not knowing must not stop every key: the worst case is today's,
            # keys that silently do nothing.
            if not self._told:
                self._told = True
                logger.info("Could not compare WoW's integrity level with ours")
            return False
        return theirs > ours
