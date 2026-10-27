"""The WoW plugin page: NSRT notes, settings, commands, roles and the page's files.

Commands are typed through a fake keyboard and clipboard: nothing here ever
presses a key or touches the PC's clipboard.
"""
import copy
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wow_addons
from wow_addons import CommandError, WowAddons
from wow_addons.modules import bigwigs, nsrt

ROOT = Path(__file__).resolve().parent.parent
WOW_STATIC = ROOT / "static" / "wow"

NOTE = """EncounterID:3306;Difficulty:Mythic;Name:Dimensius;
time:13;ph:1;tag:Magicbane;spellid:101643;text:Personal Reminder
time:1:05;ph:2;tag:ranged;text:Spread; then stack
time:40;ph:1;tag:Healer1 Healer2;spellid:740;text:Tranquility
Bloodlust on pull
Note: use a defensive at 2 stacks
"""


class FakeKeys:
    def __init__(self):
        self.pressed = []

    def __call__(self, *keys):
        self.pressed.append(keys)


class FakeClip:
    def __init__(self, text="what the player had copied", works=True):
        self.text = text
        self.works = works
        self.history = []

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.history.append(text)
        if self.works:
            self.text = text
        return self.works


def make(settings=None, clip=None, ready=True, **kw):
    keys = FakeKeys()
    core = WowAddons(wow_addons.discover(), send_keys=keys, keys_ready=lambda: ready,
                     clipboard=clip or FakeClip(), sleep=lambda s: None, **kw)
    core.configure(settings or {})
    return core, keys


CHAT = [("KEY_ENTER",), ("KEY_LEFTCTRL", "KEY_V"), ("KEY_ENTER",)]


def nsrt_msgs(core):
    return [m for m in core.update_msgs() if m["type"] == "wow_nsrt_update"]


# ── NSRT notes ────────────────────────────────────────────────────────────────

def test_note_is_split_into_personal_shared_and_notes():
    parsed = nsrt.parse_note(NOTE)
    assert (parsed["encounterName"], parsed["encounterId"], parsed["difficulty"]) == ("Dimensius", 3306, "Mythic")
    out = nsrt.classify(parsed, "magicbane", "ranged")
    assert [(e["time"], e["phase"], e["text"]) for e in out["personal"]] == [
        ("00:13", 1, "Personal Reminder"), ("01:05", 2, "Spread; then stack")]   # text keeps its ";"
    assert out["personal"][0]["spell"] == 101643
    assert [(e["time"], e["tags"]) for e in out["shared"]] == [("00:40", ["Healer1", "Healer2"])]
    # A line naming none of the note's fields is a note, colon or not.
    assert [n["text"] for n in out["notes"]] == ["Bloodlust on pull", "Note: use a defensive at 2 stacks"]


def test_without_a_character_every_timed_entry_is_shared():
    out = nsrt.classify(nsrt.parse_note(NOTE), "", "")
    assert out["personal"] == [] and len(out["shared"]) == 3


def test_the_one_line_example_is_a_header_and_an_entry():
    parsed = nsrt.parse_note("EncounterID:3306;Difficulty:Mythic;Name:BossName; time:13;ph:1;tag:Magicbane;"
                             "spellid:101643;text:Personal Reminder")
    assert parsed["encounterName"] == "BossName"
    assert nsrt.classify(parsed, "Magicbane", "")["personal"][0]["time"] == "00:13"


def test_text_that_is_not_a_note_is_never_shown():
    # "Paste note" reads the PC's clipboard: a password or a link must not become a note.
    for text in ("hunter2", "user:secret", "https://example.com/a:b", "", "time:soon;text:x"):
        assert nsrt.parse_note(text) is None, text


def test_wow_codes_and_unsafe_characters_are_dropped():
    parsed = nsrt.parse_note('time:5;tag:x;text:|cffff0000Soak|r the "orb" |Hspell:1|h[link]|h')
    assert parsed["timed"][0]["text"] == "Soak the orb [link]"


def test_saved_variables_offer_every_note_and_the_active_one_first():
    lua = ('NSRT = {\n'
           '\t["Reminders"] = {\n'
           '\t\t["Boss A"] = "EncounterID:1;Name:Caf\\195\\169 Boss;\\ntime:10;tag:x;text:A \\"quoted\\" one",\n'
           '\t\t["Boss B"] = "EncounterID:2;Name:B;\\ntime:20;text:B",\n'
           '\t},\n'
           '\t["ActiveReminder"] = "Boss A",\n'
           '\t["Other"] = "not a note",\n'
           '\t"time:1;text:array part", -- [1]\n'
           '}\n')
    data = nsrt.load_saved_variables(lua.encode("utf-8").decode("latin-1"))
    names = [n["name"] for n in data["notes"]]
    assert names == ["Café Boss", "B", "Note 3"]
    assert data["active"] == data["notes"][0]["id"]
    assert data["notes"][0]["parsed"]["timed"][0]["text"] == "A quoted one"
    # Ids stay the same when the file is read again.
    assert nsrt.load_saved_variables(lua)["notes"][1]["id"] == data["notes"][1]["id"]


# ── settings ──────────────────────────────────────────────────────────────────

def test_every_module_is_found_with_unique_commands():
    mods = wow_addons.discover()
    assert {m.id for m in mods} == {"nsrt", "bigwigs", "ellesmere", "combatlog", "utility", "char"}
    ids = [c.id for m in mods for c in m.commands]
    assert len(ids) == len(set(ids))


def test_settings_are_cleaned_and_defaults_filled_in():
    core, _ = make()
    s = core.clean_settings({
        "modules": {"nsrt": False, "ghost": True},
        "character": ' Magic"bane|\n ', "role": "ranged",
        "account": "../../etc", "wow_path": "~/Games/wow\0",
        "keybinds": {"F1": "open_nsrt", "F2": "open_nsrt", "F3": "bw_pull", "F13": "bw_version", "F4": "rm -rf"},
        "slash": {"bw_pull": "/pull {seconds} now", "nsrt_test": "/ns test {x}", "open_nsrt": "ns", "cwm_clear": "/cwm\nclear"},
        "bigwigs": {"pull_s": 99, "break_m": "3"},
        "ellesmere": {"profiles": ["Raid", "Raid", ' M"+ ', 5, ""]},
    })
    assert s["modules"] == {"nsrt": False, "bigwigs": True, "ellesmere": True, "combatlog": True, "utility": True,
                            "char": True}
    assert (s["character"], s["role"], s["account"], s["wow_path"]) == ("Magicbane", "ranged", "", "")
    # One slot per command, only commands without arguments, only F1-F12.
    assert s["keybinds"] == {"F1": "open_nsrt"}
    # A command's own text names only its arguments and starts with "/".
    assert s["slash"] == {"bw_pull": "/pull {seconds} now"}
    assert s["bigwigs"] == {"pull_s": 60, "break_m": 3}
    assert s["ellesmere"] == {"profiles": ["Raid", "M+"]}
    assert s["combatlog"] == {"wcl_enabled": True, "wcl_key": "", "wcl_app": ""}


# ── commands ──────────────────────────────────────────────────────────────────

def test_a_command_is_typed_into_chat_and_the_clipboard_put_back():
    clip = FakeClip()
    core, keys = make({"bigwigs": {"pull_s": 12}}, clip=clip)
    assert core.run("bw_pull") == "bigwigs"
    assert clip.history == ["/pull 12", "what the player had copied"]
    assert keys.pressed == CHAT
    keys.pressed.clear()
    core.run("bw_pull", {"seconds": 99})       # clamped to the command's range
    assert clip.history[-2] == "/pull 60"


def test_a_command_with_a_slot_presses_its_key():
    clip = FakeClip()
    core, keys = make({"keybinds": {"F7": "open_nsrt"}}, clip=clip)
    core.run("open_nsrt")
    assert keys.pressed == [("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTSHIFT", "KEY_F7")]
    assert clip.history == []


def test_bar_labels_cannot_break_out_of_the_command():
    clip = FakeClip()
    core, _ = make(clip=clip)
    core.run("bw_raidbar", {"seconds": 20, "label": 'Adds"; /script x()\n|cff'})
    assert clip.history[0] == '/raidbar 20 "Adds; /script x() cff"'


def test_bad_commands_and_arguments_are_refused():
    core, keys = make({"modules": {"utility": False}, "ellesmere": {"profiles": ["Raid"]}})
    cases = [
        ("format_c", None, "unknown"), (None, None, "unknown"),
        ("cwm_clear", None, "disabled"),
        ("bw_pull", {"seconds": True}, "invalid"),
        ("bw_pull", {"seconds": 10, "extra": 1}, "invalid"),
        ("bw_pull", ["seconds"], "invalid"),
        ("eui_profile", {"name": "Not mine"}, "invalid"),
        ("eui_buff", {"buff": "flask", "on": "yes"}, "invalid"),
        ("nsrt_pick", {"id": "../x"}, "invalid"),
    ]
    for command, args, reason in cases:
        with pytest.raises(CommandError) as err:
            core.run(command, args)
        assert err.value.reason == reason, (command, args)
    assert keys.pressed == []


def test_no_keyboard_or_clipboard_is_reported():
    core, _ = make(ready=False)
    with pytest.raises(CommandError, match="no_keyboard"):
        core.run("bw_version")
    clip = FakeClip(works=False)
    core, keys = make(clip=clip)
    with pytest.raises(CommandError, match="no_clipboard"):
        core.run("bw_version")
    assert keys.pressed == []


def test_timers_are_shared_with_the_seconds_left():
    now = [100.0]
    core, _ = make(clock=lambda: now[0])
    core.run("bw_break", {"minutes": 2})
    now[0] += 30
    timer = core.state_msg(False)["data"]["state"]["bigwigs"]["timer"]
    assert (timer["kind"], timer["duration"], timer["remaining"]) == ("break", 120, 90)
    now[0] += 91
    assert core.state_msg(False)["data"]["state"]["bigwigs"]["timer"] is None


def test_going_live_turns_the_combat_log_on_first():
    clip = FakeClip()
    launched = []
    core, keys = make({"combatlog": {"wcl_app": "warcraftlogs"}}, clip=clip, launch=launched.append)
    core.run("wcl_live_stream")
    assert clip.history[0] == "/combatlog" and launched == ["warcraftlogs"]
    assert core.module_state("combatlog") == {"combatlog": True, "wcl_live": True}
    keys.pressed.clear()
    core.run("wcl_live_stream")                 # off again: the log keeps running, nothing typed
    assert keys.pressed == [] and core.module_state("combatlog") == {"combatlog": True, "wcl_live": False}
    core, _ = make({"combatlog": {"wcl_enabled": False}})
    with pytest.raises(CommandError, match="disabled"):
        core.run("wcl_live_stream")


def test_buff_check_and_arcane_intellect():
    clip = FakeClip()
    core, _ = make(clip=clip)
    core.run("eui_buff", {"buff": "flask", "on": True})
    core.run("cast_intellect")
    assert clip.history[0] == "/cast Arcane Intellect"
    assert core.module_state("ellesmere")["buffs"] == {"flask": True, "food": False, "oil": False, "intellect": True}
    core.run("eui_buffs_clear")
    assert not any(core.module_state("ellesmere")["buffs"].values())


def test_pasting_a_note_from_the_pc_clipboard():
    core, _ = make({"character": "Magicbane"}, clip=FakeClip(NOTE))
    core.run("nsrt_paste")
    view = nsrt_msgs(core)[0]
    data = view["data"]
    assert (data["encounterName"], data["source"], data["active"]) == ("Dimensius", "paste", "pasted")
    assert len(data["personal"]) == 1 and len(data["shared"]) == 2
    core, _ = make(clip=FakeClip("my password"))
    with pytest.raises(CommandError, match="empty"):
        core.run("nsrt_paste")


def test_devices_get_no_paths_or_private_settings():
    core, _ = make({"wow_path": "/home/me/wow", "account": "ACC", "slash": {"open_nsrt": "/ns hub"},
                    "combatlog": {"wcl_key": "secret-key", "wcl_app": "/opt/uploader"}})
    device = core.state_msg(False)["data"]
    raw = repr(device)
    for value in ("/home/me/wow", "secret-key", "/opt/uploader", "ACC"):
        assert value not in raw
    assert "files" not in device
    # The device still sees the text a key types.
    hub = next(c for m in device["modules"] for c in m["commands"] if c["id"] == "open_nsrt")
    assert hub["template"] == ["/ns hub"]
    admin = core.state_msg(True)["data"]
    assert admin["settings"]["combatlog"]["wcl_key"] == "secret-key" and "files" in admin


# ── watched files ─────────────────────────────────────────────────────────────

def test_saved_variables_are_read_when_they_change(tmp_path):
    folder = tmp_path / "_retail_" / "WTF" / "Account" / "ACC" / "SavedVariables"
    folder.mkdir(parents=True)
    file = folder / nsrt.NOTE_FILE
    file.write_text('NSRT = { ["Note"] = "EncounterID:1;Name:First;\\ntime:5;tag:me;text:Go" }\n')
    core, _ = make({"wow_path": str(tmp_path), "character": "me"})
    assert core.poll() == {"nsrt"}
    assert core.poll() == set()                  # unchanged: not read again
    assert nsrt_msgs(core)[0]["data"]["personal"][0]["text"] == "Go"
    assert core.state_msg(True)["data"]["files"]["nsrt"] == str(file)

    file.write_text('NSRT = { ["Note"] = "EncounterID:2;Name:Second;\\ntime:6;text:Again" }\n')
    os.utime(file, (time.time() + 5, time.time() + 5))
    assert core.poll() == {"nsrt"}
    assert nsrt_msgs(core)[0]["data"]["encounterName"] == "Second"

    core.configure({"wow_path": str(tmp_path), "modules": {"nsrt": False}})
    assert core.poll() == {"nsrt"} and nsrt_msgs(core) == []
    core.configure({"wow_path": str(tmp_path), "account": "OTHER"})
    core.poll()
    assert nsrt_msgs(core)[0]["data"]["file"] is False


# ── over the WebSocket ────────────────────────────────────────────────────────

from test_integration import mock_hardware  # noqa: E402,F401
from test_security import _admin, _device, _paired_token, app  # noqa: E402,F401
from test_integration import _receive_until  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def wow(app, monkeypatch):
    """The server's plugin with a fake keyboard and clipboard."""
    keys, clip = FakeKeys(), FakeClip()
    settings = copy.deepcopy(server.wow.settings())
    state = copy.deepcopy(server.wow._state)
    monkeypatch.setattr(server.wow, "send_keys", keys)
    monkeypatch.setattr(server.wow, "keys_ready", lambda: True)
    monkeypatch.setattr(server.wow, "clipboard", clip)
    monkeypatch.setattr(server.wow, "sleep", lambda s: None)
    yield keys, clip
    server.wow.configure(settings)
    server.wow._state = state


def test_devices_may_send_commands_and_get_an_answer(app, wow):
    keys, clip = wow
    with _device(app, _paired_token()) as ws:
        assert _receive_until(ws, "wow_state")["data"]["modules"]
        ws.send_json({"type": "wow_addon_action", "command": "bw_pull", "args": {"seconds": 15}})
        assert _receive_until(ws, "wow_cmd_ack")["data"] == {"command": "bw_pull", "status": "sent"}
        assert _receive_until(ws, "wow_state")["data"]["state"]["bigwigs"]["timer"]["duration"] == 15
        ws.send_json({"type": "wow_addon_action", "command": "rm", "args": {}})
        assert _receive_until(ws, "wow_cmd_ack")["data"] == {"command": "rm", "status": "error", "reason": "unknown"}
    assert clip.history[0] == "/pull 15"


def test_only_the_pc_saves_wow_settings(app, wow):
    with _admin(app) as ws:
        _receive_until(ws, "wow_state")
        ws.send_json({"type": "wow_settings_save", "wow_addons": {"character": "Magicbane", "modules": {"utility": False}}})
        state = _receive_until(ws, "wow_state")["data"]
    assert state["settings"]["character"] == "Magicbane"
    assert not next(m for m in state["modules"] if m["id"] == "utility")["enabled"]
    assert server.config["wow_addons"]["character"] == "Magicbane"
    # Saved settings never reach a tablet through config_sync.
    with _device(app, _paired_token()) as ws:
        sync = _receive_until(ws, "config_sync")
    assert "wow_addons" not in sync["data"]["cfg"]


# ── the page's files ──────────────────────────────────────────────────────────

def _page_files():
    return sorted(WOW_STATIC.rglob("*.css")) + sorted(WOW_STATIC.rglob("*.js"))


def test_every_addon_has_a_page_side_and_back():
    backend = {m.id for m in wow_addons.discover()}
    page = {p.stem for p in (WOW_STATIC / "modules").glob("*.js")}
    assert backend == page
    for p in (WOW_STATIC / "modules").glob("*.js"):
        assert f"id: '{p.stem}'" in p.read_text(encoding="utf-8"), p.name


def test_page_files_use_theme_tokens_only():
    for path in _page_files():
        code = path.read_text(encoding="utf-8")
        assert not re.findall(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b(?![\w-])", code), path.name
        assert not re.findall(r"\b(?:rgba?|hsla?)\(", code), path.name


def test_page_files_follow_the_template_rules():
    for path in _page_files():
        code = path.read_text(encoding="utf-8")
        faded = [item.strip() for value in re.findall(r"transition(?:-property)?\s*:\s*([^;]+);", code)
                 for item in value.split(",") if item.strip().startswith("--")]
        assert faded == [], path.name
        assert "corner-shape" not in code, path.name   # squircles stay scoped in the template
        assert "backdrop-filter" not in code, path.name
        if path.suffix == ".js":
            stripped = re.sub(r"/\*[\s\S]*?\*/|(?<![:'\"])//[^\n]*", "", code)
            assert not re.findall(r"(?<![\w.$])(?:window\.)?(alert|confirm|prompt)\s*\(", stripped), path.name
            assert "createIcons" not in code, path.name


def test_page_phone_query_is_the_dashboards():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    js = re.search(r"const PHONE_LAYOUT = matchMedia\('([^']+)'\)", html).group(1)
    css = re.search(r"── Phones ─[\s\S]*?@media ([^{]+?)\s*\{", (WOW_STATIC / "wow.css").read_text()).group(1)
    assert css == js


def test_the_page_stays_under_the_lock_screen_and_dialogs():
    css = (WOW_STATIC / "wow.css").read_text(encoding="utf-8")
    z = int(re.search(r"#wow-view\s*\{[^}]*z-index:\s*(\d+)", css).group(1))
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    lock = int(re.search(r"#lock-overlay\s*\{[^}]*?z-index:\s*(\d+)", html).group(1))
    assert z < lock and z < 99999
