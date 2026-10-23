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


def _z_index(selector: str) -> int:
    """From the rule that sets it (others, like the ambient light's, share the selector)."""
    css = TEMPLATE.read_text(encoding="utf-8")
    for block in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", css):
        z = re.search(r"z-index:\s*(\d+)", block.group(1))
        if z:
            return int(z.group(1))
    raise AssertionError(f"no z-index for {selector}")


def test_full_screen_clock_stays_under_the_lock_screen():
    """The clock is dismissed with a tap; the lock screen must cover it and cannot be."""
    assert _z_index("#clock-view") < _z_index("#lock-overlay")


def test_no_custom_property_transitions():
    """A transitioned custom property is animated on the main thread, repainting
    all that paints it on every frame: the ambient light's 1.4 s fade on body
    did, the whole page (1.2 s of raster per new track, traced as a tablet).
    Cross-fade layers by opacity instead, as .ambient does."""
    css = TEMPLATE.read_text(encoding="utf-8")
    faded = [item.strip() for value in re.findall(r"transition(?:-property)?\s*:\s*([^;]+);", css)
             for item in value.split(",") if item.strip().startswith("--")]
    assert faded == []


def test_screen_care_choices_match_what_the_server_accepts():
    """Settings → General → Screen care offers exactly the minutes save_config keeps."""
    import server
    html = TEMPLATE.read_text(encoding="utf-8")
    for picker_id, key in (("cfg-idle-dim", "idle_dim_min"), ("cfg-idle-clock", "idle_clock_min")):
        block = re.search(r'id="' + picker_id + r'"[^>]*>([\s\S]*?)</div>', html).group(1)
        offered = tuple(int(v) for v in re.findall(r'data-v="(\d+)"', block))
        assert offered == server.IDLE_CHOICES[key], key
        assert server.DEFAULT_CONFIG[key] in offered


def test_screen_share_key_is_a_push_key():
    """Discord does not report whether a screen share is running, so the key
    never shows a state: each press sinks and springs back (pressKey)."""
    code = _script()
    assert not re.search(r"btnDiscScreen[^;\n]*vid-active", code)
    assert "pressKey(dom.btnDiscScreen)" in code
