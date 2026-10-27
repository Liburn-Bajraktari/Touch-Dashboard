"""
clipboard.py — the PC's clipboard as text, without a new dependency.

Linux: wl-copy / wl-paste under Wayland, else xclip (X11). Windows: the Win32
clipboard through ctypes (untested on real Windows here). Each call returns
False / None when the clipboard cannot be used, never raises.
"""
from __future__ import annotations

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


def _win_api():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    return ctypes, user32, kernel32


def _win_open(user32) -> bool:
    # Another program may hold it for a moment.
    for _ in range(10):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def _win_set(text: str) -> bool:
    try:
        ctypes, user32, kernel32 = _win_api()
        data = text.encode("utf-16-le") + b"\0\0"
        if not _win_open(user32):
            return False
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)   # still ours: the clipboard did not take it
                return False
            return True
        finally:
            user32.CloseClipboard()
    except (OSError, AttributeError, ValueError) as e:
        logger.warning("Could not write the clipboard: %s", e)
        return False


def _win_get() -> str | None:
    try:
        ctypes, user32, kernel32 = _win_api()
        if not _win_open(user32):
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)[:MAX_READ]
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except (OSError, AttributeError, ValueError):
        return None
