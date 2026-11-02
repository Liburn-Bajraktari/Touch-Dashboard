"""The WoW page's layout, keys, custom commands and routes, the game state the
companion reports, the WoW Token, and who may change what over the WebSocket."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wow_addons
from wow_addons import CommandError, WowAddons
from wow_addons.armory import Armory
from wow_addons.modules import game

from test_wow import CHAT, FakeClip, FakeKeys, make


def cells(layout, col):
    return [c["w"] for c in layout["columns"][col]]


# ── the layout ────────────────────────────────────────────────────────────────

def test_the_first_layout_is_built_from_the_modules():
    core, _ = make()
    layout = core.layout()
    assert cells(layout, 0)[:2] == ["nsrt.note", "game.buffs"]
    assert cells(layout, 1) == ["char.plate", "raid.timers", "markers.world"]
    assert cells(layout, 2)[0] == "keys" and layout["columns"][2][0]["title"] == "Tools"
    assert core.state_msg(False)["data"]["settings"]["layout_default"] is True


def test_a_saved_pull_and_break_plate_is_raid_and_dungeons_now():
    core, _ = make()
    s = core.configure({"layout": {"columns": [[], [{"w": "bigwigs.timers"}, {"w": "raid.timers"}], []]}})
    assert s["layout"]["columns"][1] == [{"w": "raid.timers", "size": "full"}]     # once


def test_templates_start_a_keys_plate():
    core, _ = make()
    tpl = {t["id"]: t for t in core.state_msg(False)["data"]["templates"]}
    world = tpl["markers.world"]
    assert [k["label"] for k in world["keys"][:8]] == ["Star", "Circle", "Diamond", "Triangle", "Moon", "Square",
                                                       "Cross", "Skull"]
    # /wm counts them its own way: the star is 5, the square 1
    assert world["keys"][0]["args"] == {"marker": 5} and world["keys"][5]["args"] == {"marker": 1}
    assert world["keys"][8] == {"cmd": "wow_cwm"}
    assert tpl["markers.target"]["keys"][0]["args"] == {"marker": 1}
    assert {"raid.lead", "wowgame.callouts", "nsrt.tools", "utility.tools"} <= set(tpl)
    core.configure({"modules": {"markers": False}})
    assert "markers.world" not in {t["id"] for t in core.state_msg(False)["data"]["templates"]}


def test_a_templates_addon_keys_need_the_addon(tmp_path):
    addons = tmp_path / "_retail_" / "Interface" / "AddOns"
    for name in ("Details", "MythicDungeonTools"):
        (addons / name).mkdir(parents=True)
    core, _ = make({"wow_path": str(tmp_path)})
    core.poll()
    panels = next(t for t in core.state_msg(False)["data"]["templates"] if t["id"] == "addons.panels")
    assert [k["cmd"] for k in panels["keys"]] == ["det_toggle", "det_reset", "mdt_open"]


def test_a_saved_layout_is_cleaned():
    core, _ = make()
    s = core.configure({"layout": {"columns": [
        [{"w": "nsrt.note", "size": "half"},          # NSRT's note is full width only
         {"w": "nsrt.note"},                          # each widget once
         {"w": "ghost.widget"}, {"w": None, "size": "half"}, "junk"],
        [{"w": "keys", "id": "raid", "title": 'Raid"|', "keys": [
            {"cmd": "bw_pull", "args": {"seconds": 99}, "label": "Pull!", "glyph": "timer"},
            {"cmd": "bw_pull", "args": {"seconds": "x"}},            # bad arguments: the defaults
            {"cmd": "rm -rf"}, None,
            {"cmd": "wow_say", "glyph": "../evil", "label": "x" * 80}]},
         {"w": "keys", "id": "raid"}],                                # a second "raid" gets its own id
    ]}})
    cols = s["layout"]["columns"]
    assert cols[0] == [{"w": "nsrt.note", "size": "full"}, {"w": None, "size": "half"}]
    keys = cols[1][0]
    assert keys["title"] == "Raid"
    assert keys["keys"][0] == {"cmd": "bw_pull", "args": {"seconds": 60}, "label": "Pull!", "glyph": "timer"}
    assert keys["keys"][1:4] == [{"cmd": "bw_pull"}, None, None]
    assert keys["keys"][4] == {"cmd": "wow_say", "label": "x" * 24}
    assert cols[1][1]["id"] != "raid" and cols[2] == []
    assert core.configure({"layout": None})["layout"] is None   # back to the first layout


# ── custom commands, slots and routes ─────────────────────────────────────────

def test_custom_commands_are_cleaned_and_run():
    clip = FakeClip()
    core, keys = make({"custom": [
        {"id": "c_det", "label": "Details", "glyph": "bar-chart-3", "lines": ["/details toggle", "not a command", 5]},
        {"id": "c_det", "label": "Again", "lines": ["/x"]},                  # an id once
        {"id": "bad id", "label": "Bad", "lines": ["/x"]},
        {"id": "c_empty", "label": "Empty", "lines": ["no slash"]},
        {"id": "c_ctrl", "lines": ["/rw hi\x07"]},
    ]}, clip=clip)
    assert [c["id"] for c in core.settings()["custom"]] == ["c_det"]
    assert core.run("c_det") == "custom"
    assert clip.history[0] == "/details toggle" and keys.pressed == CHAT
    custom = next(m for m in core.state_msg(False)["data"]["modules"] if m["id"] == "custom")
    assert custom["commands"][0]["via"] == "addon"


def test_routes_come_from_the_first_word():
    core, _ = make({"custom": [{"id": "c_a", "label": "A", "lines": ["/cast Flash Heal"]},
                               {"id": "c_b", "label": "B", "lines": ["/mrt note"]},
                               {"id": "c_c", "label": "C", "lines": ["/2 LFM"], "via": "auto"},
                               {"id": "c_d", "label": "D", "lines": ["/mrt"], "via": "game"}]})
    route = lambda cid: core.route(core._lookup(cid)[1])  # noqa: E731
    assert [route(c) for c in ("c_a", "c_b", "c_c", "c_d")] == ["game", "addon", "game", "game"]
    assert route("wow_readycheck") == "game" and route("det_toggle") == "addon"


def test_a_slot_holds_a_command_with_its_arguments():
    core, keys = make({"keybinds": {"F2": {"cmd": "bw_pull", "args": {"seconds": 15}},
                                    "F3": {"cmd": "wow_rw", "args": {"text": "Bloodlust now"}},
                                    "F4": {"cmd": "wow_rw"},                    # no text: nothing to type
                                    "F5": {"cmd": "nsrt_pick", "args": {"id": "x"}}}})   # types nothing
    assert set(core.settings()["keybinds"]) == {"F2", "F3"}
    core.run("bw_pull", {"seconds": 15})
    assert keys.pressed[-1] == ("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTSHIFT", "KEY_F2")
    core.run("bw_pull", {"seconds": 20})                      # another length is typed
    assert keys.pressed[-3:] == CHAT
    assert core.slot_payload() == "2=/pull 15;3=/rw Bloodlust now"
    assert core.state_msg(True)["data"]["slot_text"] == {"F2": "/pull 15", "F3": "/rw Bloodlust now"}
    assert "slot_text" not in core.state_msg(False)["data"]


def test_the_companion_is_used_only_once_installed(tmp_path):
    root = tmp_path / "_retail_" / "Interface" / "AddOns"
    root.mkdir(parents=True)
    core, keys = make({"wow_path": str(tmp_path)})
    core.poll()
    assert core.companion_status()["installed"] is None
    core.run("bw_version")
    assert keys.pressed == CHAT                                # typed: no addon
    assert core.install_companion() == wow_addons.bundled_version()
    assert (root / "TouchDashboard" / "TouchDashboard.lua").is_file()
    assert core.state_msg(False)["data"]["companion"]["installed"] == wow_addons.bundled_version()
    core.configure({"wow_path": str(tmp_path), "companion": False})
    keys.pressed.clear()
    core.run("bw_version")
    assert keys.pressed == CHAT                                # switched off: typed
    core, _ = make({})
    with pytest.raises(CommandError, match="no_folder"):
        core.install_companion()


# ── the game's state ──────────────────────────────────────────────────────────

SV = '''
TouchDashboardDB = {
	["chars"] = {
		["Thrall-Draenor"] = {
			["name"] = "Thrall", ["realm"] = "Draenor", ["class"] = "WARRIOR", ["level"] = 80,
			["ilvl"] = 640.5, ["gold"] = 12345678, ["updated"] = 1700000000,
			["key"] = { ["level"] = 10, ["name"] = "Ara-Kara" },
			["vault"] = { { ["t"] = 1, ["p"] = 4, ["th"] = 1 }, { ["t"] = 1, ["p"] = 4, ["th"] = 4 }, { ["t"] = 3, ["p"] = 1, ["th"] = 2 } },
			["items"] = { ["bags"] = { { ["id"] = 191, ["n"] = 5, ["q"] = 1, ["name"] = "Phial of Truth" } },
			              ["bank"] = { { ["id"] = 192, ["n"] = 2, ["q"] = 3, ["name"] = "Flask of Tempered Swiftness" } } },
		},
		["Jaina-Draenor"] = {
			["name"] = "Jaina", ["class"] = "MAGE", ["level"] = 70, ["gold"] = 50000,
			["items"] = { ["bags"] = { { ["id"] = 192, ["n"] = 1, ["q"] = 3, ["name"] = "Flask of Tempered Swiftness" } } },
		},
	},
	["warband"] = { ["gold"] = 10000000, ["items"] = { { ["id"] = 192, ["n"] = 4, ["q"] = 3, ["name"] = "Flask of Tempered Swiftness" } } },
}
'''

SNAP = {"v": 1, "sh": 1, "combat": False, "char": {"name": "Thrall", "realm": "Draenor", "class": "MAGE", "level": 80,
                                                 "ilvl": 650.1},
        "gold": 98765432, "bags": {"free": 12, "total": 100}, "dur": {"low": 35, "broken": 1}, "zone": "Dornogal",
        "key": {"level": 12, "name": "Ara-Kara"}, "weapon": {"mh": 1800, "oh": False},
        "buffs": [{"id": 1459, "name": "Arcane Intellect", "rem": 3000}, {"id": 1, "name": "Well Fed", "rem": 600},
                  {"id": 2, "name": "Flask of Tempered Swiftness", "rem": 3200}],
        "bars": [{"label": "Pull", "rem": 9.5}]}


def game_view(core):
    return next(m for m in core.update_msgs() if m["type"] == "wow_game_update")["data"]


def test_saved_characters_and_the_live_one_are_merged(tmp_path):
    folder = tmp_path / "WTF" / "Account" / "ACC" / "SavedVariables"
    folder.mkdir(parents=True)
    (folder / game.SV_FILE).write_text(SV)
    core, _ = make({"wow_path": str(tmp_path)})
    core.poll()
    v = game_view(core)
    assert [c["name"] for c in v["chars"]] == ["Thrall", "Jaina"]
    thrall = v["chars"][0]
    assert thrall["gold"] == 1234 and thrall["vault"] == {"dungeons": 2, "raid": 0} and thrall["live"] is False
    assert v["totals"] == {"gold": 1234 + 5 + 1000, "warband": 1000, "chars": 2}
    core._took(copy.deepcopy(SNAP))
    v = game_view(core)
    live = v["live"]
    assert v["chars"][0]["live"] and v["chars"][0]["gold"] == 9876 and v["chars"][0]["ilvl"] == 650.1
    assert live["dur"] == {"low": 35, "broken": 1} and live["bars"] == [{"label": "Pull", "rem": 9.5, "dur": None}]
    checks = {c["id"]: c for c in live["checks"]}
    assert checks["intellect"]["ok"] and checks["intellect"]["cast"] == "cast_intellect"   # a mage casts it
    assert checks["flask"]["ok"] and checks["food"]["ok"] and not checks["rune"]["ok"]
    assert checks["weapon"] == {"id": "weapon", "name": "Weapon oil", "ok": True, "rem": 1800, "kind": "consumable"}
    assert "shout" not in checks                      # solo: no warrior to ask for Battle Shout


def checks_in(group, buffs=None, cls="MAGE"):
    core, _ = make({})
    snap = copy.deepcopy(SNAP) | {"group": group}
    snap["char"]["class"] = cls
    if buffs is not None:
        snap["buffs"] = buffs
    core._took(snap)
    return {c["id"]: c for c in game_view(core)["live"]["checks"]}


def test_the_buff_check_asks_for_what_is_yours_and_what_your_group_can_give():
    solo = checks_in({"n": 0, "raid": False, "classes": ["MAGE"]}, buffs=[])
    assert list(solo) == ["intellect", "flask", "food", "rune", "weapon"]          # yours to see to
    assert solo["intellect"]["cast"] == "cast_intellect" and not solo["intellect"]["ok"]
    party = checks_in({"n": 3, "raid": False, "classes": ["MAGE", "PRIEST", "WARRIOR"]}, buffs=[])
    assert list(party) == ["intellect", "fortitude", "shout", "flask", "food", "rune", "weapon"]
    assert party["shout"]["cast"] is None and not party["shout"]["ok"]
    given = checks_in({"n": 0, "raid": False}, buffs=[{"id": 21562, "name": "Power Word: Fortitude", "rem": 3000}])
    assert given["fortitude"]["ok"] and "shout" not in given                      # on you anyway: shown
    older = checks_in({"n": 5, "raid": False}, buffs=[])                           # an addon that does not say who
    assert {"fortitude", "shout", "wild", "bronze", "skyfury"} <= set(older)
    warrior = checks_in({"n": 0, "raid": False}, buffs=[], cls="WARRIOR")
    assert list(warrior) == ["shout", "flask", "food", "rune", "weapon"]


def test_midnights_augment_rune_is_found_by_its_spell():
    # Its buff is called just "Void-Touched"; the live link names it, an older addon sends only the id.
    for name in ("Void-Touched", None):
        rune = checks_in({"n": 0, "raid": False}, buffs=[{"id": 1264426, "name": name, "rem": 206}])["rune"]
        assert rune["ok"] and rune["rem"] == 206


def test_a_character_saved_without_its_realm_is_left_out(tmp_path):
    folder = tmp_path / "SavedVariables"
    folder.mkdir()
    (folder / game.SV_FILE).write_text(SV.replace('["Jaina-Draenor"]', '["Thrall-"] = { ["name"] = "Thrall", '
                                                  '["gold"] = 0, ["ilvl"] = 0 },\n\t\t["Jaina-Draenor"]'))
    core, _ = make({"wow_path": str(folder)})
    core.poll()
    assert [c["key"] for c in game_view(core)["chars"]] == ["Thrall-Draenor", "Jaina-Draenor"]


def test_items_are_found_on_every_character(tmp_path):
    folder = tmp_path / "SavedVariables"
    folder.mkdir()
    (folder / game.SV_FILE).write_text(SV)
    core, _ = make({"wow_path": str(folder)})
    core.poll()
    _, reply = core.run_ex("game_find", {"q": "flask"})
    hit = reply["items"][0]
    assert hit["name"] == "Flask of Tempered Swiftness" and hit["n"] == 7
    assert {(w["who"], w["loc"]) for w in hit["where"]} == {("Thrall", "bank"), ("Jaina", "bags"),
                                                            ("Warband", "warband bank")}
    assert core.run_ex("game_find", {"q": "nothing like it"})[1]["items"] == []


def test_a_broken_saved_variables_file_reads_as_nothing():
    assert game.load('TouchDashboardDB = { ["chars"] = { "unfinished') == {"chars": {}, "warband": None}


def test_a_snapshot_must_be_the_companions():
    parse = WowAddons._parse_snapshot
    assert parse('TDS1 ab12 {"gold": 1}', "ab12") == {"gold": 1}
    assert parse('TDS1 ab12 {"gold": 1}', "ffff") is None       # another exchange's
    assert parse('TDS1 - {"gold": 1}', None) == {"gold": 1}      # the box's first, before a nonce
    assert parse("TDX ab12", None) is None and parse("TDS1 - [1]", None) is None
    assert parse("TDS1 - " + "{" * 10, None) is None and parse(None, None) is None


# ── the WoW Token ─────────────────────────────────────────────────────────────

class TokenSession:
    def __init__(self):
        self.prices = []

    def post(self, url, **kw):
        return Resp({"access_token": "t", "expires_in": 86400})

    def get(self, url, params=None, headers=None, timeout=None):
        assert url.endswith("/data/wow/token/index") and params["namespace"] == "dynamic-eu"
        return Resp(self.prices.pop(0))


class Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_the_token_price_and_its_week_are_kept(tmp_path):
    now = [1_700_000_000.0]
    session = TokenSession()
    arm = Armory(cache_file=str(tmp_path / "cache.json"), session=session, clock=lambda: now[0])
    assert arm.token("eu")["state"] == "no_key" and not arm.token_due("eu")
    arm.configure("id", "secret")
    assert arm.token_due("eu")
    for hours, gold in ((30, 400_000), (20, 410_000), (0, 425_000)):
        session.prices.append({"price": gold * 10000, "last_updated_timestamp": int((now[0] - hours * 3600) * 1000)})
        assert arm.fetch_token("eu")
    t = arm.token("eu")
    assert (t["price"], t["change"], t["low"], t["high"]) == (425_000, 25_000, 400_000, 425_000)
    assert len(t["points"]) == 3 and not arm.token_due("eu")
    now[0] += 21 * 60
    assert arm.token_due("eu")
    again = Armory(cache_file=str(tmp_path / "cache.json"), session=session, clock=lambda: now[0])
    assert again.token("eu")["state"] in ("no_key", "ok")
    again.configure("id", "secret")
    assert again.token("eu")["price"] == 425_000          # kept across restarts


# ── over the WebSocket ────────────────────────────────────────────────────────

from test_integration import mock_hardware  # noqa: E402,F401
from test_security import _admin, _device, _paired_token, app  # noqa: E402,F401
from test_integration import _receive_until  # noqa: E402
from test_wow import wow  # noqa: E402,F401

import server  # noqa: E402


def test_a_tablet_may_arrange_the_page_but_not_write_commands(app, wow):
    with _device(app, _paired_token()) as ws:
        _receive_until(ws, "wow_state")
        ws.send_json({"type": "wow_layout_save", "layout": {"columns": [[{"w": "nsrt.note"}], [], []]}})
        state = _receive_until(ws, "wow_state")["data"]
        assert state["settings"]["layout"]["columns"][0] == [{"w": "nsrt.note", "size": "full"}]
        ws.send_json({"type": "wow_settings_save", "wow_addons": {"custom": [{"id": "c_x", "label": "X",
                                                                             "lines": ["/run x()"]}]}})
        assert _receive_until(ws, "refused")["data"] == {"action": "wow_settings_save", "reason": "admin_only"}
        ws.send_json({"type": "wow_companion_install"})
        assert _receive_until(ws, "refused")["data"]["reason"] == "admin_only"
    assert server.wow.settings()["custom"] == []


def test_a_command_answer_carries_its_data(app, wow):
    with _admin(app) as ws:
        _receive_until(ws, "wow_state")
        ws.send_json({"type": "wow_addon_action", "command": "game_find", "args": {"q": "flask"}})
        ack = _receive_until(ws, "wow_cmd_ack")["data"]
    assert ack["status"] == "sent" and ack["data"] == {"q": "flask", "items": [], "more": 0}


def old_companion(tmp_path, version="1.0.0"):
    target = tmp_path / "_retail_" / "Interface" / "AddOns" / wow_addons.COMPANION
    target.mkdir(parents=True)
    (target / f"{wow_addons.COMPANION}.toc").write_text(f"## Title: Touch Dashboard\n## Version: {version}\n")
    return target


def test_an_older_companion_in_the_game_folder_is_updated(tmp_path):
    target = old_companion(tmp_path)
    core, _ = make({"wow_path": str(tmp_path)})
    core.poll()
    assert core.companion_status()["installed"] == wow_addons.bundled_version()
    assert (target / f"{wow_addons.COMPANION}.lua").read_text() == (
        Path(wow_addons.COMPANION_DIR) / f"{wow_addons.COMPANION}.lua").read_text()


def test_the_companion_is_never_installed_or_updated_unasked(tmp_path):
    (tmp_path / "_retail_" / "Interface" / "AddOns").mkdir(parents=True)
    core, _ = make({"wow_path": str(tmp_path)})
    core.poll()                                         # not installed: stays so
    assert core.companion_status()["installed"] is None
    old = tmp_path / "off"
    old_companion(old)
    core, _ = make({"wow_path": str(old), "companion": False})
    core.poll()                                         # switched off in WoW settings: left as it is
    assert core.companion_status()["installed"] == "1.0.0"
    newer = tmp_path / "newer"
    old_companion(newer, "99.0.0")
    core, _ = make({"wow_path": str(newer)})
    core.poll()                                         # newer than this install's: never downgraded
    assert core.companion_status()["installed"] == "99.0.0"


def test_in_a_key_buffs_not_seen_are_hidden_not_missing():
    key = {"state": 1, "map": 503, "name": "Ara-Kara", "level": 12, "limit": 1800, "elapsed": 600}
    core, _ = make({})
    core._took(copy.deepcopy(SNAP) | {"buffs": [], "mplus": key})
    checks = {c["id"]: c for c in game_view(core)["live"]["checks"]}
    assert checks["intellect"]["hidden"] and checks["intellect"]["cast"] is None     # no Cast key on a guess
    assert checks["flask"]["hidden"] and not checks["flask"]["ok"]
    assert checks["weapon"]["ok"]                     # not an aura: the game shows it
    core._took(copy.deepcopy(SNAP) | {"buffs": []})   # out of the key: missing again
    checks = {c["id"]: c for c in game_view(core)["live"]["checks"]}
    assert not checks["flask"]["hidden"] and checks["intellect"]["cast"] == "cast_intellect"



def test_the_companions_word_on_hidden_auras_wins_over_the_guess():
    key = {"state": 1, "map": 503, "name": "Ara-Kara", "level": 12, "limit": 1800, "elapsed": 600}
    core, _ = make({})
    core._took(copy.deepcopy(SNAP) | {"buffs": [], "mplus": key, "aura_secret": False})   # the game says: readable
    checks = {c["id"]: c for c in game_view(core)["live"]["checks"]}
    assert not checks["flask"]["hidden"] and checks["intellect"]["cast"] == "cast_intellect"
    core._took(copy.deepcopy(SNAP) | {"buffs": [], "aura_secret": True, "pull_hidden": True})   # an encounter
    live = game_view(core)["live"]
    checks = {c["id"]: c for c in live["checks"]}
    assert checks["flask"]["hidden"] and checks["intellect"]["cast"] is None
    assert live["pull_hidden"] is True

def test_boss_names_lose_the_games_defeated():
    key = {"state": 1, "map": 1, "name": "Den of Nalorakk", "level": 16, "limit": 1920, "elapsed": 1066, "bosses": [
        {"name": "The Hoardmonger defeated", "done": True, "at": 501},
        {"name": "Sentinel of Winter defea", "done": False},          # cut at 24 bytes by addon 2.5.0
        {"name": "Nalorakk defeated", "done": False}, {"name": "Lord Dee", "done": False}]}
    core, _ = make({})
    core._took(copy.deepcopy(SNAP) | {"mplus": key})
    assert [b["name"] for b in game_view(core)["live"]["mplus"]["bosses"]] == [
        "The Hoardmonger", "Sentinel of Winter", "Nalorakk", "Lord Dee"]


@pytest.mark.parametrize("where", ["World of Warcraft", "World of Warcraft/_retail_",
                                   "World of Warcraft/_retail_/WTF/Account/ACC/SavedVariables", "~"])
def test_the_addons_folder_is_found_from_the_folder_set_in_settings(tmp_path, monkeypatch, where):
    # Nothing is assumed about where WoW lives: the game folder, its _retail_ or a
    # SavedVariables folder under it, as set in WoW settings ("~" for the home folder).
    retail = tmp_path / "World of Warcraft" / "_retail_"
    (retail / "Interface" / "AddOns" / "BigWigs").mkdir(parents=True)
    (retail / "WTF" / "Account" / "ACC" / "SavedVariables").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = "~/World of Warcraft/_retail_" if where == "~" else str(tmp_path / where)
    core, _ = make({"wow_path": path})
    core.poll()
    assert core.has_addon("BigWigs") is True and core.has_addon("DBM-Core") is False


def test_without_a_game_folder_no_addon_is_known(tmp_path):
    core, _ = make({})
    core.poll()
    assert core.has_addon("BigWigs") is None          # unknown, not missing
    core, _ = make({"wow_path": str(tmp_path)})       # a folder with no Interface/AddOns in or above it
    core.poll()
    assert core.has_addon("BigWigs") is None


def test_the_newest_released_companion_is_read_from_githubs_release_list():
    def rel(tag, *assets, **kw):
        return {"tag_name": tag, "assets": [{"name": a} for a in assets], **kw}
    releases = [rel("v0.18.0", "TouchDashboard-addon-2.10.0.zip", prerelease=True),   # never offered
                rel("v0.17.0", "TouchDashboard-addon-2.7.0.zip", draft=True),
                rel("v0.16.1", "touch-dashboard-v0.16.1.zip", "TouchDashboard-addon-2.6.0.zip", "SHA256SUMS"),
                rel("v0.16.0", "TouchDashboard-addon-2.5.0.zip"), rel("v0.11.0", "TouchDashboard-v0.11.0.apk")]
    assert wow_addons.latest_companion(releases) == "2.6.0"
    assert wow_addons.latest_companion([rel("v0.11.0", "TouchDashboard-v0.11.0.apk")]) is None
    assert wow_addons.latest_companion({"message": "API rate limit exceeded"}) is None


def test_a_newer_released_companion_is_reported_until_this_install_has_it(tmp_path):
    core, _ = make({})
    assert core.companion_status()["release"] is None
    core.set_latest_companion(wow_addons.bundled_version())       # this install brings it already
    assert core.companion_status()["release"] is None
    core.set_latest_companion("99.0.0")                            # the dashboard's own update brings it
    assert core.companion_status()["release"] == "99.0.0"
    core.set_latest_companion("99.0.0; rm -rf ~")                  # anything else is no version
    assert core.companion_status()["release"] is None
