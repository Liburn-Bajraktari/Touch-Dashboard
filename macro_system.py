"""
macro_system.py — Keyboard macro injection and application enumeration.

Linux:  Uses evdev / UInput for key injection.
Windows: Uses ctypes SendInput (Win32 API) for key injection.
Both:   Application discovery via .desktop files (Linux) or
        PowerShell Get-StartApps (Windows).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


# ─── Windows virtual-key map ───────────────────────────────────────────────────

_WIN_VK_MAP: dict[str, int] = {
    **{f"KEY_{c}": 0x41 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
    **{f"KEY_{n}": 0x30 + i for i, n in enumerate("0123456789")},
    "KEY_LEFTCTRL": 0xA2, "KEY_RIGHTCTRL": 0xA3,
    "KEY_LEFTSHIFT": 0xA0, "KEY_RIGHTSHIFT": 0xA1,
    "KEY_LEFTALT": 0xA4, "KEY_RIGHTALT": 0xA5,
    "KEY_ENTER": 0x0D, "KEY_ESC": 0x1B,
    "KEY_BACKSPACE": 0x08, "KEY_TAB": 0x09,
    "KEY_SPACE": 0x20, "KEY_PAUSE": 0x13,
    **{f"KEY_F{n}": 0x6F + n for n in range(1, 13)},
    "KEY_MUTE": 0xAD, "KEY_VOLUMEDOWN": 0xAE, "KEY_VOLUMEUP": 0xAF,
    "KEY_NEXTSONG": 0xB0, "KEY_PREVIOUSSONG": 0xB1,
    "KEY_STOPCD": 0xB2, "KEY_PLAYPAUSE": 0xB3,
    "KEY_LEFTMETA": 0x5B, "KEY_RIGHTMETA": 0x5C,
    "KEY_PRINT": 0x2C, "KEY_SCROLLLOCK": 0x91, "KEY_NUMLOCK": 0x90,
    "KEY_INSERT": 0x2D, "KEY_DELETE": 0x2E, "KEY_HOME": 0x24,
    "KEY_END": 0x23, "KEY_PAGEUP": 0x21, "KEY_PAGEDOWN": 0x22,
    "KEY_UP": 0x26, "KEY_DOWN": 0x28, "KEY_LEFT": 0x25, "KEY_RIGHT": 0x27,
    **{f"KEY_F{n}": 0x7B + (n - 12) for n in range(13, 25)},
    **{f"KEY_KP{n}": 0x60 + n for n in range(10)},
    "KEY_KPASTERISK": 0x6A, "KEY_KPPLUS": 0x6B, "KEY_KPMINUS": 0x6D,
    "KEY_KPDOT": 0x6E, "KEY_KPSLASH": 0x6F, "KEY_KPENTER": 0x0D,
    "KEY_SYSRQ": 0x2C, "KEY_CAPSLOCK": 0x14, "KEY_COMPOSE": 0x5D,
    "KEY_MINUS": 0xBD, "KEY_EQUAL": 0xBB, "KEY_COMMA": 0xBC, "KEY_DOT": 0xBE,
    "KEY_SLASH": 0xBF, "KEY_SEMICOLON": 0xBA, "KEY_APOSTROPHE": 0xDE, "KEY_GRAVE": 0xC0,
    "KEY_LEFTBRACE": 0xDB, "KEY_BACKSLASH": 0xDC, "KEY_RIGHTBRACE": 0xDD, "KEY_102ND": 0xE2,
}

# Shortcuts recorded before v0.11.4 took the browser's key names
# ("KEY_METALEFT" for KeyboardEvent.code "MetaLeft"), which no keyboard has.
# They mean these keys.
_KEY_ALIASES: dict[str, str] = {
    "KEY_CONTROLLEFT": "KEY_LEFTCTRL", "KEY_CONTROLRIGHT": "KEY_RIGHTCTRL",
    "KEY_SHIFTLEFT": "KEY_LEFTSHIFT", "KEY_SHIFTRIGHT": "KEY_RIGHTSHIFT",
    "KEY_ALTLEFT": "KEY_LEFTALT", "KEY_ALTRIGHT": "KEY_RIGHTALT",
    "KEY_METALEFT": "KEY_LEFTMETA", "KEY_METARIGHT": "KEY_RIGHTMETA",
    "KEY_OSLEFT": "KEY_LEFTMETA", "KEY_OSRIGHT": "KEY_RIGHTMETA",
    "KEY_ESCAPE": "KEY_ESC",
    "KEY_ARROWUP": "KEY_UP", "KEY_ARROWDOWN": "KEY_DOWN",
    "KEY_ARROWLEFT": "KEY_LEFT", "KEY_ARROWRIGHT": "KEY_RIGHT",
    "KEY_BRACKETLEFT": "KEY_LEFTBRACE", "KEY_BRACKETRIGHT": "KEY_RIGHTBRACE",
    "KEY_QUOTE": "KEY_APOSTROPHE", "KEY_BACKQUOTE": "KEY_GRAVE", "KEY_PERIOD": "KEY_DOT",
    "KEY_INTLBACKSLASH": "KEY_102ND", "KEY_PRINTSCREEN": "KEY_SYSRQ", "KEY_CONTEXTMENU": "KEY_COMPOSE",
    "KEY_NUMPADDIVIDE": "KEY_KPSLASH", "KEY_NUMPADMULTIPLY": "KEY_KPASTERISK",
    "KEY_NUMPADSUBTRACT": "KEY_KPMINUS", "KEY_NUMPADADD": "KEY_KPPLUS",
    "KEY_NUMPADENTER": "KEY_KPENTER", "KEY_NUMPADDECIMAL": "KEY_KPDOT",
    **{f"KEY_NUMPAD{n}": f"KEY_KP{n}" for n in range(10)},
    "KEY_AUDIOVOLUMEMUTE": "KEY_MUTE", "KEY_AUDIOVOLUMEDOWN": "KEY_VOLUMEDOWN",
    "KEY_AUDIOVOLUMEUP": "KEY_VOLUMEUP", "KEY_MEDIAPLAYPAUSE": "KEY_PLAYPAUSE",
    "KEY_MEDIATRACKNEXT": "KEY_NEXTSONG", "KEY_MEDIATRACKPREVIOUS": "KEY_PREVIOUSSONG",
    "KEY_MEDIASTOP": "KEY_STOPCD",
}


def key_name(key: str) -> str:
    """The evdev name for a recorded key, old browser-style names included."""
    name = str(key).upper()
    return _KEY_ALIASES.get(name, name)


# Keys a keyboard sends with an E0 scan code prefix that MapVirtualKey does not
# always report from their virtual key (keypad Enter shares VK_RETURN with
# Enter). Everything else comes from MapVirtualKeyW.
_WIN_SCAN: dict[str, tuple[int, bool]] = {
    "KEY_KPENTER": (0x1C, True), "KEY_KPSLASH": (0x35, True), "KEY_NUMLOCK": (0x45, True),
    "KEY_RIGHTCTRL": (0x1D, True), "KEY_RIGHTALT": (0x38, True),
}
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2


def _win_chord(keys, scan_of) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """A chord's key-downs and key-ups as (virtual key, scan code, flags).

    Each key carries its scan code and, for the navigation block (Home, End,
    the arrows…), the extended flag, as a keyboard sends them: with a virtual
    key alone a game that reads scan codes sees nothing, or Numpad 7 for Home.
    `scan_of(vk)` → (scan code, extended). A key with no Windows code refuses
    the whole chord: pressing the rest would send bare modifiers."""
    down = []
    for k in keys:
        if isinstance(k, int):
            vk, name = k, ""
        else:
            name = key_name(k)
            vk = _WIN_VK_MAP.get(name, 0)
            if not vk:
                raise ValueError(f"no Windows key for {k!r}")
        scan, extended = _WIN_SCAN.get(name) or scan_of(vk)
        down.append((vk, scan, KEYEVENTF_EXTENDEDKEY if extended else 0))
    return down, [(vk, scan, flags | KEYEVENTF_KEYUP) for vk, scan, flags in reversed(down)]


_win = None


def _win_api():
    global _win
    if _win is None:
        import ctypes
        from ctypes import wintypes as wt

        class KeybdInput(ctypes.Structure):
            _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MouseInput(ctypes.Structure):         # the union's largest member: INPUT's size
            _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class Union(ctypes.Union):
            _fields_ = [("ki", KeybdInput), ("mi", MouseInput)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wt.DWORD), ("u", Union)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(Input), ctypes.c_int]
        user32.SendInput.restype = wt.UINT
        user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
        user32.MapVirtualKeyW.restype = wt.UINT
        _win = (ctypes, user32, Input)
    return _win


def _win_send_keys(*keys) -> None:
    """Press and release a chord on Windows through SendInput. OSError when
    Windows refused it (the secure desktop, or a window running as administrator)."""
    ctypes, user32, Input = _win_api()

    def scan_of(vk: int) -> tuple[int, bool]:
        sc = user32.MapVirtualKeyW(vk, 4)            # MAPVK_VK_TO_VSC_EX: E0/E1 in the high byte
        return sc & 0xFF, (sc >> 8) in (0xE0, 0xE1)

    down, up = _win_chord(keys, scan_of)
    if not down:
        return

    def send(events) -> int:
        arr = (Input * len(events))()
        for i, (vk, scan, flags) in enumerate(events):
            arr[i].type = 1                          # INPUT_KEYBOARD
            arr[i].u.ki.wVk, arr[i].u.ki.wScan, arr[i].u.ki.dwFlags = vk, scan, flags
        return user32.SendInput(len(events), arr, ctypes.sizeof(Input))

    sent = send(down)
    time.sleep(0.05)
    released = send(up)                              # always: a modifier left down would stick
    if sent != len(down) or released != len(up):
        raise OSError(ctypes.get_last_error(), "Windows did not take the key presses")




# ─── MacroSystem ───────────────────────────────────────────────────────────────

class MacroSystem:
    """Cross-platform keyboard macro injection."""

    _ui: Any = None          # evdev.UInput (Linux only)
    _init_lock = threading.Lock()

    @classmethod
    def _init_linux(cls) -> None:
        with cls._init_lock:
            if cls._ui is not None:
                return
            try:
                import evdev  # type: ignore[import]
                cls._ui = evdev.UInput()
            except Exception as e:
                logger.error(f"evdev UInput init failed: {e}")

    @classmethod
    def available(cls) -> bool:
        """Whether keys can be pressed: on Linux, whether the uinput keyboard exists
        (send_keys only logs when it cannot be created, e.g. no /dev/uinput access)."""
        if sys.platform.startswith("win"):
            return True
        try:
            import evdev  # type: ignore[import]  # noqa: F401
        except ImportError:
            return False
        cls._init_linux()
        return cls._ui is not None

    @classmethod
    def press(cls, *keys) -> None:
        """send_keys that raises when Windows refuses the keys (the WoW page says why)."""
        if sys.platform.startswith("win"):
            _win_send_keys(*keys)
        else:
            cls.send_keys(*keys)

    @classmethod
    def send_keys(cls, *keys) -> None:
        """
        Send a key-down + key-up chord.
        Accepts evdev keycode strings (e.g. 'KEY_LEFTCTRL', 'KEY_C')
        or raw integer keycodes.
        """
        if sys.platform.startswith("win"):
            try:
                _win_send_keys(*keys)
            except Exception as e:
                logger.error(f"Windows SendInput failed: {e}")
            return

        # Linux path
        try:
            import evdev  # type: ignore[import]
            cls._init_linux()
            if not cls._ui:
                return
            ecodes = evdev.ecodes
            resolved = []
            for k in keys:
                if isinstance(k, int):
                    resolved.append(k)
                else:
                    ec = getattr(ecodes, key_name(k), None)
                    if ec is not None:
                        resolved.append(ec)

            for ec in resolved:
                cls._ui.write(ecodes.EV_KEY, ec, 1)
            cls._ui.syn()
            for ec in reversed(resolved):
                cls._ui.write(ecodes.EV_KEY, ec, 0)
            cls._ui.syn()
        except ImportError:
            logger.warning("evdev not available; macro ignored")
        except Exception as e:
            logger.error(f"Linux macro send_keys: {e}")


# ─── AppEnumerator ─────────────────────────────────────────────────────────────

class AppEnumerator:
    """Discovers installed applications on the current platform."""

    _cached: list[dict] | None = None
    _lock = threading.Lock()

    @classmethod
    def get_apps(cls) -> list[dict]:
        with cls._lock:
            if cls._cached is not None:
                return cls._cached
            cls._cached = cls._build_list()
            return cls._cached

    @classmethod
    def invalidate_cache(cls) -> None:
        with cls._lock:
            cls._cached = None

    @staticmethod
    def _build_list() -> list[dict]:
        apps: list[dict] = []

        if sys.platform.startswith("win"):
            try:
                ps = (
                    # Windows PowerShell pipes in the OEM code page unless told:
                    # accented app names came back garbled.
                    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
                    "$apps = Get-StartApps | Select-Object Name, AppID;"
                    "$apps | ForEach-Object {"
                    "  [PSCustomObject]@{name=$_.Name; exec=$_.AppID}"
                    "} | ConvertTo-Json -Compress"
                )
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", ps],
                    stderr=subprocess.DEVNULL, timeout=12,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),   # no console flashing up
                ).decode("utf-8", errors="replace").strip()
                if out:
                    raw = __import__("json").loads(out)
                    if isinstance(raw, dict):
                        raw = [raw]
                    for d in raw:
                        name = (d.get("name") or "").strip()
                        exec_ = (d.get("exec") or "").strip()
                        if name and exec_:
                            apps.append({"name": name, "exec": exec_, "icon": ""})
            except Exception as e:
                logger.error(f"Windows app enumeration: {e}")

        else:
            search_paths = [
                os.path.expanduser("~/.local/share/applications"),
                "/usr/share/applications",
            ]
            seen_names: set[str] = set()
            for base in search_paths:
                if not os.path.isdir(base):
                    continue
                for root, _dirs, files in os.walk(base):
                    for fname in files:
                        if not fname.endswith(".desktop"):
                            continue
                        fpath = os.path.join(root, fname)
                        try:
                            with open(fpath, encoding="utf-8", errors="ignore") as f:
                                content = f.read()
                            if "NoDisplay=true" in content:
                                continue
                            name = exec_cmd = icon = ""
                            in_entry = False
                            for line in content.splitlines():
                                line = line.strip()
                                if line == "[Desktop Entry]":
                                    in_entry = True
                                elif line.startswith("[") and line != "[Desktop Entry]":
                                    in_entry = False
                                if in_entry:
                                    if line.startswith("Name=") and not name:
                                        name = line.split("=", 1)[1]
                                    elif line.startswith("Exec=") and not exec_cmd:
                                        exec_cmd = line.split("=", 1)[1]
                                        # Strip field codes (%f %F %u %U etc.)
                                        for code in ("%f", "%F", "%u", "%U", "%c", "%k", "%i"):
                                            exec_cmd = exec_cmd.replace(code, "")
                                        exec_cmd = exec_cmd.strip()
                                    elif line.startswith("Icon=") and not icon:
                                        icon = line.split("=", 1)[1]
                            if name and exec_cmd and name not in seen_names:
                                seen_names.add(name)
                                apps.append({"name": name, "exec": exec_cmd, "icon": icon})
                        except Exception:
                            pass

        return sorted(apps, key=lambda x: (x["name"] or "").lower())

    @staticmethod
    def launch_app(exec_cmd: str) -> None:
        if sys.platform.startswith("win"):
            try:
                subprocess.Popen(
                    ["explorer.exe", f"shell:AppsFolder\\{exec_cmd}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.error(f"Windows app launch '{exec_cmd}': {e}")
        else:
            try:
                import shlex
                args = shlex.split(exec_cmd)
                subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                logger.error(f"Linux app launch '{exec_cmd}': {e}")
