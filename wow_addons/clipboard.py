"""
clipboard.py — the PC's clipboard as text, without a new dependency.

Linux: wl-copy / wl-paste under Wayland, else xclip (X11). Windows: the Win32
clipboard through ctypes, every write kept out of the clipboard's history.
Each call returns False / None when the clipboard cannot be used, never raises.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

TIMEOUT_S = 2
MAX_READ = 1024 * 1024      # a note or a SimC string is far smaller

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _linux_tools() -> tuple[list[str], list[str]] | None:
    """(copy, paste) commands for this session, or None without a tool."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy") and shutil.which("wl-paste"):
        return ["wl-copy", "--type", "text/plain;charset=utf-8"], ["wl-paste", "--no-newline", "--type", "text"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-in"], ["xclip", "-selection", "clipboard", "-out"]
    return None


def set_text(text: str) -> bool:
    if sys.platform.startswith("win"):
        return _win_set(text)
    tools = _linux_tools()
    if tools is None:
        logger.warning("No clipboard tool: install wl-clipboard (Wayland) or xclip (X11)")
        return False
    try:
        # Both tools stay behind to serve the selection: no pipes held open to them.
        subprocess.run(tools[0], input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=TIMEOUT_S, check=True)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Could not write the clipboard: %s", e)
        return False


def get_text() -> str | None:
    """The clipboard's text, or None when it is empty, not text or unreadable."""
    if sys.platform.startswith("win"):
        return _win_get()
    tools = _linux_tools()
    if tools is None:
        return None
    try:
        out = subprocess.run(tools[1], capture_output=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None   # empty, or holds something other than text
    return out.stdout[:MAX_READ].decode("utf-8", errors="replace")


# Registered formats that keep a write out of Windows' clipboard history
# (Win+V), the cloud clipboard and clipboard managers: the companion's
# sentinel and envelopes are no one's to keep. WoW's own copies (its answers)
# cannot be marked: WoW writes those.
_PRIVATE_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory",
                    "CanUploadToCloudClipboard", "Clipboard Viewer Ignore")
_HWND_MESSAGE = -3
_api = None


def _win_api():
    global _api
    if _api is None:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        private = [f for f in map(user32.RegisterClipboardFormatW, _PRIVATE_FORMATS) if f]
        _api = (user32, kernel32, private)
    return _api


def _win_open(user32):
    """Open the clipboard, owned by a message-only window made for this call
    (with no owner, EmptyClipboard leaves SetClipboardData nothing to give the
    data to). The window, or None."""
    hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, _HWND_MESSAGE, None, None, None)
    if not hwnd:
        return None
    for _ in range(10):         # another program may hold it for a moment
        if user32.OpenClipboard(hwnd):
            return hwnd
        time.sleep(0.02)
    user32.DestroyWindow(hwnd)
    return None


def _win_close(user32, hwnd) -> None:
    user32.CloseClipboard()
    user32.DestroyWindow(hwnd)  # the data stays: it was handed over, not promised


def _win_put(user32, kernel32, fmt: int, data: bytes) -> bool:
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)   # still ours: the clipboard did not take it
        return False
    return True


def _win_set(text: str) -> bool:
    try:
        user32, kernel32, private = _win_api()
        hwnd = _win_open(user32)
        if not hwnd:
            return False
        try:
            if not user32.EmptyClipboard():
                return False
            if not _win_put(user32, kernel32, CF_UNICODETEXT, text.encode("utf-16-le") + b"\0\0"):
                return False
            for fmt in private:
                _win_put(user32, kernel32, fmt, b"\0\0\0\0")    # a DWORD 0: "no"
            return True
        finally:
            _win_close(user32, hwnd)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError) as e:
        logger.warning("Could not write the clipboard: %s", e)
        return False


def _win_get() -> str | None:
    try:
        user32, kernel32, _ = _win_api()
        hwnd = _win_open(user32)
        if not hwnd:
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                # UTF-16 up to its NUL, read by the block's size: never past it
                raw = ctypes.string_at(ptr, min(kernel32.GlobalSize(handle), MAX_READ * 2 + 2) // 2 * 2)
                return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0][:MAX_READ]
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            _win_close(user32, hwnd)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return None
