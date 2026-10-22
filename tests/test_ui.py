"""Page-level rules that are easy to break and hard to see in review."""
import re
from pathlib import Path

import pytest

import macro_system

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "index.html"


def _script() -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    return "\n".join(re.findall(r"<script>([\s\S]*?)</script>", html))


def test_no_native_dialogs():
    """alert/confirm/prompt draw unthemed system windows in the desktop window
    and the Android WebView; every question goes through tdConfirm/tdNotice."""
    code = re.sub(r"/\*[\s\S]*?\*/|(?<![:'\"])//[^\n]*", "", _script())
    calls = re.findall(r"(?<![\w.$])(?:window\.)?(alert|confirm|prompt)\s*\(", code)
    assert calls == []


def _recorded_key_names() -> set[str]:
    """Every name the key editor's recorder can save (CODE_TO_EVDEV + the patterns in evdevName)."""
    script = _script()
    table = script[script.index("const CODE_TO_EVDEV = {"):script.index("function evdevName")]
    names = {"KEY_" + v for v in re.findall(r":\s*'([A-Z0-9]+)'", table)}
    names |= {f"KEY_{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
    names |= {f"KEY_KP{n}" for n in range(10)} | {f"KEY_F{n}" for n in range(1, 25)}
    return names


def test_recorded_keys_can_be_pressed_on_windows():
    missing = sorted(n for n in _recorded_key_names() if n not in macro_system._WIN_VK_MAP)
    assert missing == []


def test_recorded_keys_can_be_pressed_on_linux():
    evdev = pytest.importorskip("evdev")
    names = _recorded_key_names() | set(macro_system._KEY_ALIASES.values())
    assert sorted(n for n in names if not hasattr(evdev.ecodes, n)) == []


def test_old_browser_key_names_still_work():
    """Shortcuts recorded before the recorder used evdev names keep working."""
    assert macro_system.key_name("KEY_METALEFT") == "KEY_LEFTMETA"
    assert macro_system.key_name("key_arrowup") == "KEY_UP"
    assert macro_system.key_name("KEY_NUMPAD7") == "KEY_KP7"
    assert macro_system.key_name("KEY_L") == "KEY_L"
