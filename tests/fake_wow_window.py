"""A stand-in for WoW's window on Windows, for the live tests and the probe:
a top-level window of WoW's class and title whose client area's top rows
show the companion's beacon and a live link frame, drawn with GDI as the
addon's textures would be.

    python tests/fake_wow_window.py [--width W] [--height H] [--payload TEXT] [--no-beacon]
                                    [--title T --cls C --topmost --at X Y]   (a window to cover it with)

It prints "ready <hwnd>" once shown, then takes lines on stdin: "beacon 0|1",
"payload TEXT", "quit". Windows only."""
import argparse
import ctypes
import sys
import threading
from ctypes import wintypes as wt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from link_frames import frame_symbols, strip_row   # noqa: E402
from wow_addons import screen                      # noqa: E402

WM_DESTROY, WM_PAINT, WM_CLOSE, WM_APP = 0x0002, 0x000F, 0x0010, 0x8000
WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_EX_TOPMOST = 0x00CF0000, 0x10000000, 0x00000008


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT), ("fRestore", wt.BOOL),
                ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


def top_rows(width: int, payload: bytes, beacon: bool) -> bytes:
    """The client area's top CELL_PX rows as RGB: the strip from a fifth of the
    width on its top row, the beacon 8 pixels left of it (TouchDashboard.lua)."""
    start = width // 5
    rows = [bytearray(width * 3) for _ in range(screen.CELL_PX)]
    rows[0][:] = strip_row(frame_symbols(payload, tick=1), width, start)
    if beacon:
        x0 = start - 8 - len(screen.BEACON) * screen.CELL_PX
        for i, cell in enumerate(screen.BEACON):
            rgb = bytes(255 if v else 0 for v in cell)
            for row in rows:
                for x in range(x0 + i * screen.CELL_PX, x0 + (i + 1) * screen.CELL_PX):
                    row[x * 3:x * 3 + 3] = rgb
    return b"".join(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--payload", default="Touch Dashboard probe")
    ap.add_argument("--no-beacon", action="store_true")
    ap.add_argument("--title", default="World of Warcraft")
    ap.add_argument("--cls", default="GxWindowClass")
    ap.add_argument("--topmost", action="store_true")
    ap.add_argument("--at", type=int, nargs=2, default=(100, 100))
    args = ap.parse_args()
    state = {"payload": args.payload.encode(), "beacon": not args.no_beacon}

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.AdjustWindowRectEx.argtypes = [ctypes.POINTER(wt.RECT), wt.DWORD, wt.BOOL, wt.DWORD]
    user32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.BeginPaint.restype = wt.HDC
    user32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.InvalidateRect.argtypes = [wt.HWND, wt.LPVOID, wt.BOOL]
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    gdi32.GetStockObject.restype = wt.HGDIOBJ
    gdi32.SetDIBitsToDevice.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD, wt.DWORD, ctypes.c_int,
                                        ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
    kernel32.GetModuleHandleW.restype = wt.HMODULE

    def paint(hwnd):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rect = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        width, rows = rect.right, screen.CELL_PX
        if args.cls == "GxWindowClass" and width > 0:
            rgb = top_rows(width, state["payload"], state["beacon"])
            bgrx = bytearray(len(rgb) // 3 * 4)
            bgrx[0::4], bgrx[1::4], bgrx[2::4] = rgb[2::3], rgb[1::3], rgb[0::3]
            header = (ctypes.c_uint32 * 10)(40, width, ctypes.c_uint32(-rows).value, 1 | 32 << 16, 0, 0, 0, 0, 0, 0)
            buf = (ctypes.c_ubyte * len(bgrx)).from_buffer(bgrx)
            gdi32.SetDIBitsToDevice(hdc, 0, 0, width, rows, 0, 0, 0, rows, buf, header, 0)
        user32.EndPaint(hwnd, ctypes.byref(ps))

    @WNDPROC
    def proc(hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            paint(hwnd)
            return 0
        if msg == WM_APP:
            user32.InvalidateRect(hwnd, None, False)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    inst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, proc, 0, 0, inst, None, None,
                     gdi32.GetStockObject(4), None, args.cls, None)          # 4: BLACK_BRUSH
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        raise OSError(ctypes.get_last_error(), "RegisterClassExW")
    rect = wt.RECT(0, 0, args.width, args.height)
    ex = WS_EX_TOPMOST if args.topmost else 0
    user32.AdjustWindowRectEx(ctypes.byref(rect), WS_OVERLAPPEDWINDOW, False, ex)
    hwnd = user32.CreateWindowExW(ex, args.cls, args.title, WS_OVERLAPPEDWINDOW | WS_VISIBLE, args.at[0], args.at[1],
                                  rect.right - rect.left, rect.bottom - rect.top, None, None, inst, None)
    if not hwnd:
        raise OSError(ctypes.get_last_error(), "CreateWindowExW")
    user32.SetForegroundWindow(hwnd)

    def commands():
        for line in sys.stdin:
            word, _, rest = line.strip().partition(" ")
            if word == "beacon":
                state["beacon"] = rest == "1"
            elif word == "payload":
                state["payload"] = rest.encode()
            elif word == "quit":
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return
            user32.PostMessageW(hwnd, WM_APP, 0, 0)
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    threading.Thread(target=commands, daemon=True).start()
    print(f"ready {hwnd}", flush=True)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
