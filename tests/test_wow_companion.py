"""The companion addon and the server's side of it, end to end: the real
addon/TouchDashboard/TouchDashboard.lua runs in Lua 5.1 against a stand-in of
the WoW API (tests/wow_companion_harness.lua), and the keys the core presses
are played into it as WoW reads them: chords in its ALT-CTRL-SHIFT order, an
unbound chord falling through to the plain key, the player's own bindings
(Bloodlust on Ctrl+C). WoW's window is a stand-in that paints the companion's
beacon from the cells the addon draws."""
import json
import os
import shutil
import subprocess

import pytest

import wow_addons
from wow_addons import CommandError, WowAddons, adler32, link, screen
from wow_addons.lua import parse_saved_variables

LUA = shutil.which("lua5.1") or shutil.which("luajit")
HERE = os.path.dirname(__file__)
HARNESS = os.path.join(HERE, "wow_companion_harness.lua")
ADDON = os.path.join(os.path.dirname(HERE), "addon", "TouchDashboard", "TouchDashboard.lua")

pytestmark = pytest.mark.skipif(LUA is None, reason="needs Lua 5.1 to run the addon")


class Game:
    """The harness: one command a line, one answer a line."""

    def __init__(self):
        self.p = subprocess.Popen([LUA, HARNESS, ADDON], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, cwd=HERE)
        self.cmd("event PLAYER_LOGIN")

    def cmd(self, line: str) -> str:
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        out = self.p.stdout.readline()
        assert out, f"the harness stopped at {line!r}"
        return out.rstrip("\n")

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)


class Clip:
    def __init__(self, text="the player's own text"):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text
        return True


ROWS = screen.CELL_PX               # the window's top rows the fakes draw: the beacon's height


def paint(row, x, w, rgb, width):
    for px in range(max(0, round(x)), min(width, round(x + w))):
        row[px * 3:px * 3 + 3] = rgb


def paint_strip(d, width):
    """The live link's rows as WoW would draw them from the addon's cells (as
    tall as they are; below them, down to the beacon's height, the game's black)."""
    row = bytearray(width * 3)
    tall = 0
    if d["shown"]:
        for sym, x, w, h in zip(d["syms"], d["xs"], d["ws"], d["hs"]):
            if sym >= 0:
                paint(row, x, w, bytes(255 if sym & bit else 0 for bit in (4, 2, 1)), width)
                tall = max(tall, round(h))
    return bytes(row) * tall + bytes(width * 3) * (ROWS - tall)


class Window:
    """WoW's window: in front or not, its top edge as the addon draws it (the
    beacon, the live link's strip, a frame (1/60 s) for each read) under an
    overlay in the corner (Steam's FPS counter, 52 pixels wide)."""

    def __init__(self, game):
        self.game = game
        self.in_front = True
        self.width = 2560
        self.frames = 0
        self.tweak = None          # the pixels as the game's settings draw them
        self.overlay = 52          # the counter's width in the top-left corner, 0 for none

    def size(self):
        return self.width, 1440

    def top(self, link_frame=True):
        """The window's top ROWS rows as RGB, the strip a frame on (or as it is)."""
        d = json.loads(self.game.cmd("link" if link_frame else "link 0"))
        self.frames += link_frame
        rows = bytearray(paint_strip(d, self.width))
        b = json.loads(self.game.cmd("beacon"))
        for y in range(ROWS):
            row = bytearray(rows[y * self.width * 3:(y + 1) * self.width * 3])
            if b["shown"]:
                for c in b["cells"]:
                    if y < round(c["h"]):
                        paint(row, c["x"], c["w"], bytes(round(v * 255) for v in c["rgb"]), self.width)
            dim = bytes(v // 10 for v in row[:self.overlay * 3])           # dark glass over the corner
            row[:self.overlay * 3] = dim
            rows[y * self.width * 3:(y + 1) * self.width * 3] = row
        return self.tweak(bytes(rows)) if self.tweak else bytes(rows)

    def grab(self, x, y, width, height):
        assert y == 0 and height <= ROWS and x + width <= self.width
        rows = self.top(link_frame=height == link.ROWS)
        return b"".join(rows[(r * self.width + x) * 3:(r * self.width + x + width) * 3] for r in range(height))

    def front(self):
        return self.in_front

    def beacon(self):
        return screen.find_beacon(self.top(link_frame=False), self.width) is not None


MODS = {"KEY_LEFTCTRL": "CTRL", "KEY_LEFTALT": "ALT", "KEY_LEFTSHIFT": "SHIFT"}


@pytest.fixture
def game():
    g = Game()
    yield g
    g.close()


def make(game, tmp_path, settings=None, *, loaded=True, window=True):
    """A core whose keys go into the harness. `loaded=False`: the addon is
    installed but not running in game. `window=False`: WoW's window cannot be
    seen (Windows, a Wayland-native Wine)."""
    clip = Clip()
    pressed = []

    def keys(*k):
        pressed.append(k)
        if k == ("KEY_LEFTCTRL", "KEY_V"):
            game.cmd("clip " + json.dumps(clip.text))
        res = json.loads(game.cmd("press " + " ".join(MODS.get(x, x[4:]) for x in k)))
        if isinstance(res, dict) and "copied" in res:
            clip.text = res["copied"]
        if isinstance(res, dict) and "slot" in res:
            pressed[-1] = ("slot", res["slot"])

    if not loaded:
        game.cmd("unload")
    root = tmp_path / "World of Warcraft" / "_retail_"
    (root / "Interface" / "AddOns").mkdir(parents=True)
    win = Window(game) if window else None
    # Our clock runs with the strip's frames, 4000 s ahead of WoW's GetTime().
    core = WowAddons(wow_addons.discover(), send_keys=keys, clipboard=clip, sleep=lambda s: None,
                     screen=win, clock=lambda: 5000 + (win.frames / 60 if win else 0))
    core.configure({"wow_path": str(root), **(settings or {})})
    assert core.install_companion() == wow_addons.bundled_version() == "2.6.0"
    return core, pressed, clip


def actions(game):
    return json.loads(game.cmd("actions"))


def said(game):
    return json.loads(game.cmd("said"))


def calls(game):
    return json.loads(game.cmd("calls"))


def test_an_addon_command_runs_in_the_hidden_box_and_the_state_comes_back(game, tmp_path):
    core, pressed, clip = make(game, tmp_path)
    assert core.run("bw_pull", {"seconds": 10}) == "bigwigs"
    assert calls(game) == [{"cmd": "/pull", "msg": "10"}]
    assert said(game) == [] and actions(game) == []   # chat never opened, no binding fired
    assert game.cmd("focused") == "false"            # the box gave the keyboard back
    assert clip.text == "the player's own text"       # the clipboard is put back
    snap = core.snapshot()["data"]
    assert snap["result"] == "ok" and snap["gold"] == 123456789012
    assert snap["bags"] == {"free": 25, "total": 100} and snap["dur"] == {"low": 40, "broken": 0}
    assert snap["char"]["name"] == "Thrall" and snap["char"]["spec"] == "Arms"
    assert snap["key"] == {"map": 503, "level": 12, "name": "Ara-Kara"}
    assert [b["name"] for b in snap["buffs"]] == ["Arcane Intellect", "Well Fed"]
    assert snap["weapon"] == {"mh": 1800, "oh": False}


def test_secret_values_never_leave_the_game(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    game.cmd("secret 1")
    core.sync()
    assert [b["name"] for b in core.snapshot()["data"]["buffs"]] == ["Arcane Intellect"]


def test_boss_mod_bars_come_back_with_the_time_left(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    game.cmd("bigwigs Pull 10")
    core.sync()
    assert core.snapshot()["data"]["bars"] == [{"label": "Pull", "rem": 10}]


def test_blizzards_own_commands_go_through_the_run_button(game, tmp_path):
    custom = [{"id": "c_rc", "label": "Ready", "lines": ["/readycheck"]},
              {"id": "c_forced", "label": "Forced", "lines": ["/readycheck"], "via": "addon"},
              {"id": "c_new", "label": "New", "lines": ["/mystery go"]}]
    core, pressed, clip = make(game, tmp_path, {"custom": custom})
    core.run("c_rc")                                  # Blizzard's: loaded into the run button, then its chord
    assert ("slot", "/readycheck") in pressed and pressed[-1] == ("slot", "/readycheck")
    assert said(game) == [] and calls(game) == []     # never typed into chat, never run by the addon
    assert json.loads(game.cmd("key ALT-CTRL-SHIFT-END")) == ""      # emptied once clicked
    pressed.clear()
    core.run("c_forced")                              # the addon will not run Blizzard's command
    assert calls(game) == [] and said(game)[-1] == "/readycheck"
    assert pressed.count(("KEY_ENTER",)) == 3         # its box, then chat's Enter … Enter
    core.run("c_new")                                 # unknown to the addon: learned as Blizzard's, typed
    assert core.route(core._lookup("c_new")[1]) == "game"
    pressed.clear()
    core.run("c_new")                                 # from then on through the run button
    assert pressed[-1] == ("slot", "/mystery go") and len(said(game)) == 2


def test_in_combat_blizzards_commands_are_typed(game, tmp_path):
    custom = [{"id": "c_rc", "label": "Ready", "lines": ["/readycheck"]}]
    core, pressed, _ = make(game, tmp_path, {"custom": custom})
    game.cmd("combat 1")                              # the PC has not seen it yet: the addon says so
    core.run("c_rc")
    assert wow_addons.FOCUS_KEYS in pressed and said(game) == ["/readycheck"]
    assert json.loads(game.cmd("key ALT-CTRL-SHIFT-END")) == ""      # nothing loaded
    frames(core, 20)                                  # the live link says it: the box is not even tried
    pressed.clear()
    core.run("c_rc")
    assert wow_addons.FOCUS_KEYS not in pressed and said(game) == ["/readycheck", "/readycheck"]


def test_key_slots_are_loaded_into_secure_buttons_once(game, tmp_path):
    core, pressed, _ = make(game, tmp_path, {"keybinds": {"F3": {"cmd": "bw_pull", "args": {"seconds": 10}}}})
    core.run("bw_pull", {"seconds": 10})
    assert ("slot", "/pull 10") in pressed
    assert core.snapshot()["data"]["sh"] == adler32(core.slot_payload())
    pressed.clear()
    core.run("bw_pull", {"seconds": 10})              # loaded: one key press, nothing else
    assert pressed == [("slot", "/pull 10")]
    core.run("bw_pull", {"seconds": 12})              # another length has no slot: the box
    assert calls(game)[-1] == {"cmd": "/pull", "msg": "12"}


def test_slots_changed_in_combat_wait_for_it_to_end(game, tmp_path):
    core, pressed, _ = make(game, tmp_path, {"keybinds": {"F1": {"cmd": "bw_version"}}})
    game.cmd("combat 1")
    with pytest.raises(CommandError) as e:
        core.run("bw_version")
    assert e.value.reason == "in_combat"
    assert game.cmd("key ALT-CTRL-SHIFT-F1") == "unbound"
    game.cmd("combat 0")
    game.cmd("event PLAYER_REGEN_ENABLED")
    assert json.loads(game.cmd("key ALT-CTRL-SHIFT-F1")) == "/bwv"


def test_an_addon_that_is_not_running_is_skipped_for_a_while(game, tmp_path):
    core, pressed, _ = make(game, tmp_path, loaded=False)
    core.run("bw_version")                            # its box never opened: typed into chat
    assert wow_addons.FOCUS_KEYS in pressed and said(game) == ["/bwv"]
    pressed.clear()
    core.run("bw_version")                            # not tried again for a while
    assert wow_addons.FOCUS_KEYS not in pressed


def test_ctrl_c_is_never_pressed_without_the_box(game, tmp_path):
    """The chord falls through to the plain key (Home: the camera) when the
    addon is not listening; the Ctrl+C that read its box would cast Bloodlust."""
    core, pressed, _ = make(game, tmp_path, loaded=False)
    with pytest.raises(CommandError) as e:
        core.sync()
    assert e.value.reason == "no_box"
    assert pressed == [wow_addons.FOCUS_KEYS]
    assert actions(game) == ["PREVVIEW"]              # not BLOODLUST


def test_the_chords_are_bound_as_wow_writes_them(game, tmp_path):
    core, pressed, _ = make(game, tmp_path, {"keybinds": {"F2": {"cmd": "bw_version"}}})
    core.sync()                                       # Ctrl+Alt+Shift+Home reached the box
    core.run("bw_version")
    assert pressed[-1] == ("slot", "/bwv")           # and Ctrl+Alt+Shift+F2 its button
    assert actions(game) == []


def test_the_beacon_is_four_pixels_a_cell_beside_the_strip(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    assert json.loads(game.cmd("beacon"))["shown"] is False
    game.cmd("press CTRL ALT SHIFT HOME")
    b = json.loads(game.cmd("beacon"))
    assert b["shown"] is True
    assert [c["rgb"] for c in b["cells"]] == [list(c) for c in screen.BEACON]
    start = 2560 // 5 - 8 - len(screen.BEACON) * screen.CELL_PX     # 8 pixels left of the strip, out of the corner
    for i, c in enumerate(b["cells"]):                # under a 0.71 UI scale on a 1440-pixel window
        assert c["x"] == pytest.approx(start + i * screen.CELL_PX) and c["w"] == pytest.approx(screen.CELL_PX)
        assert c["h"] == pytest.approx(screen.CELL_PX)
    assert screen.find_beacon(core.screen.top(), core.screen.width) == start
    game.cmd("timers 2")                              # the box closes by itself, and the beacon with it
    assert json.loads(game.cmd("beacon"))["shown"] is False
    assert core.screen.beacon() is False


@pytest.mark.parametrize("overlay", [0, 52, 400])     # none, Steam's FPS counter, a MangoHud box
def test_an_overlay_in_the_corner_hides_neither_the_box_nor_the_strip(game, tmp_path, overlay):
    core, pressed, _ = make(game, tmp_path)
    core.screen.overlay = overlay
    assert frames(core, 2) == {"game"} and core.snapshot()["link"] is True
    core.sync(full=True)                              # the box opened, and was seen to
    assert core.snapshot()["data"]["items"]
    assert ("KEY_LEFTCTRL", "KEY_C") in pressed


def test_nothing_is_pressed_while_wow_is_not_in_front(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    core.screen.in_front = False
    for run in (lambda: core.run("bw_pull", {"seconds": 10}), core.sync, lambda: core.run("wow_reload")):
        with pytest.raises(CommandError) as e:
            run()
        assert e.value.reason == "not_front"
    assert pressed == []


def test_without_a_view_of_wow_its_box_is_never_used(game, tmp_path):
    core, pressed, _ = make(game, tmp_path, window=False)
    with pytest.raises(CommandError) as e:
        core.sync()
    assert e.value.reason == "no_check"
    core.run("bw_version")                            # typed into chat instead
    assert wow_addons.FOCUS_KEYS not in pressed and said(game) == ["/bwv"]


def test_once_it_answered_silence_is_reported_not_typed(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    core.sync()
    game.cmd("unload")                                # the addon stopped (a /reload with an error)
    pressed.clear()
    with pytest.raises(CommandError) as e:
        core.run("bw_version")
    assert e.value.reason == "no_box"
    assert pressed == [wow_addons.FOCUS_KEYS] and said(game) == []


def test_each_character_is_saved_for_the_dashboard(game, tmp_path):
    game.cmd("event BANKFRAME_OPENED")
    game.cmd("event PLAYER_LOGOUT")
    sv = parse_saved_variables(json.loads(game.cmd("sv")))
    char = sv["TouchDashboardDB"]["chars"]["Thrall-Draenor"]
    assert char["gold"] == 123456789012 and char["class"] == "WARRIOR" and char["ilvl"] == 648.2
    assert char["items"]["bags"] == [{"id": 191, "n": 100, "q": 1, "name": "Phial of Truth"}]
    assert char["items"]["bank"][0]["id"] == 291
    assert char["vault"] == [{"t": 1, "i": 1, "p": 3, "th": 1, "l": 12}]
    assert sv["TouchDashboardDB"]["warband"]["gold"] == 5000000


def frames(core, n=1):
    """Read the live link n times, a frame apart: the modules it changed."""
    out = set()
    for _ in range(n):
        out |= core.read_link()
    return out


def test_the_live_link_carries_the_games_state_without_a_key(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    assert frames(core, 2) == {"game"}
    snap = core.snapshot()
    assert snap["link"] is True and core.companion_status()["link"] is True
    d = snap["data"]
    assert d["char"] == {"name": "Thrall", "realm": "Draenor", "class": "WARRIOR", "spec": "Arms",
                         "level": 80, "ilvl": 648.2}
    assert d["gold"] == 123456789012 and d["bags"] == {"free": 25, "total": 100}
    assert d["dur"] == {"low": 40, "broken": 0} and d["zone"] == "Dornogal"
    assert d["key"] == {"map": 503, "level": 12, "name": "Ara-Kara"}
    assert [(b["id"], b["name"]) for b in d["buffs"]] == [(1459, None), (2, "Well Fed")]   # consumables named
    assert d["buffs"][0]["rem"] == pytest.approx(3000, abs=1) and d["buffs"][1]["rem"] == pytest.approx(60, abs=1)
    assert d["weapon"]["mh"] == pytest.approx(1800, abs=1) and d["weapon"]["oh"] is False
    assert d["group"]["n"] == 5 and d["group"]["raid"] is False
    assert sorted(d["group"]["classes"]) == ["DRUID", "EVOKER", "MAGE", "PRIEST", "WARRIOR"]   # you included
    assert d["combat"] is False and d["lockdown"] is False
    assert d["sh"] == adler32("") and d["ver"] == "2.0.0"
    assert pressed == []
    assert frames(core, 10) == set()                  # nothing changed in game: nothing to tell


def test_a_pull_timer_shows_as_it_starts(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    frames(core, 70)                                  # a whole second turns: WoW's clock is known
    game.cmd("bigwigs Pull 10")
    assert frames(core, 1) == {"game"}                # on the next frame
    frames(core, 15)
    snap = core.snapshot()
    (bar,) = snap["data"]["bars"]
    assert bar["label"] == "Pull" and bar["dur"] == 10
    assert bar["rem"] - snap["age"] == pytest.approx(10 - 16 / 60, abs=0.05)   # as a screen counts it
    assert pressed == []


def test_a_countdown_anyone_starts_is_a_pull_bar(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    frames(core, 2)
    game.cmd("event START_PLAYER_COUNTDOWN Player-1-0A 12 12")       # the raid leader's, no boss mod involved
    frames(core, 20)
    (bar,) = core.snapshot()["data"]["bars"]
    assert bar["label"] == "Pull" and bar["dur"] == 12
    game.cmd("event CANCEL_PLAYER_COUNTDOWN Player-1-0A")
    frames(core, 20)
    assert core.snapshot()["data"]["bars"] == []
    game.cmd("event START_PLAYER_COUNTDOWN Player-1-0A 10 10")
    game.cmd("bigwigs Pull 10")                      # BigWigs shows the same countdown: still one bar
    frames(core, 20)
    assert [b["label"] for b in core.snapshot()["data"]["bars"]] == ["Pull"]
    assert pressed == []


def test_the_group_comes_with_its_classes(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    game.cmd("group raid SHAMAN WARLOCK")
    frames(core, 20)
    g = core.snapshot()["data"]["group"]
    assert g["raid"] is True and g["n"] == 3 and sorted(g["classes"]) == ["SHAMAN", "WARLOCK", "WARRIOR"]
    game.cmd("group solo")
    core.sync()                                       # the box's snapshot says the same
    g = core.snapshot()["data"]["group"]
    assert g == {"n": 0, "raid": False, "classes": ["WARRIOR"]}


def test_secret_values_stay_out_of_the_link(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    game.cmd("secret 1")
    frames(core, 2)
    assert [b["id"] for b in core.snapshot()["data"]["buffs"]] == [1459]


def test_short_buffs_stay_off_the_strip(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    game.cmd("proc 1")                                # a 40 s Bloodlust
    frames(core, 2)
    assert [b["id"] for b in core.snapshot()["data"]["buffs"]] == [1459, 2]
    core.sync()                                       # the box still hands every buff over
    assert [b["id"] for b in core.snapshot()["data"]["buffs"]] == [1459, 2, 2825]


def test_the_strip_is_a_hairline(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    d = json.loads(game.cmd("link 2"))
    heights = {round(h, 3) for h, sym in zip(d["hs"], d["syms"]) if sym >= 0}
    assert heights == {1.0}                            # one physical pixel tall, three wide
    assert {round(w, 3) for w, sym in zip(d["ws"], d["syms"]) if sym >= 0} == {3.0}
    rows = core.screen.top()
    assert rows[core.screen.width * 3:core.screen.width * 3 * 2] == bytes(core.screen.width * 3)   # under it: the game's
    assert frames(core, 2) == {"game"}


def test_the_strip_reads_through_the_games_gamma(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    core.screen.tweak = lambda rgb: bytes(int(28 + v * 0.7) for v in rgb)     # brightness up, contrast down
    assert frames(core, 2) == {"game"}
    assert core.snapshot()["data"]["gold"] == 123456789012


def test_a_damaged_frame_is_refused_not_misread(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    width = core.screen.width
    rgb = core.screen.grab(0, 0, width, link.ROWS)
    (start,) = link.locate(rgb, width)
    assert start == width // 5 and link.read_row(rgb, width, start) is not None
    for cell in (60, 200):                           # a header cell, a payload cell
        bad = bytearray(rgb)
        y, x = link.ROWS // 2, start + cell * link.CELL + link.CELL // 2
        o = (y * width + x) * 3
        bad[o:o + 3] = bytes(255 - v for v in bad[o:o + 3])
        assert link.read_row(bytes(bad), width, start) is None
    assert link.read_row(bytes(len(rgb)), width, start) is None              # no strip: the game's black
    assert link.locate(bytes(len(rgb)), width) == []
    flat = bytes(v // 8 + 100 for v in rgb)                                   # too washed out to tell
    assert all(link.read_row(flat, width, x) is None for x in link.locate(flat, width))


def test_a_long_state_comes_in_pages(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    game.cmd("screen 800 600")
    core.screen.width = 800                           # 160 cells: 42 bytes a page
    frames(core, 20)
    labels = [f"Phase {i} adds and a long label" for i in range(6)]
    for i, label in enumerate(labels):
        game.cmd(f"bigwigs {label} {30 + i}")
    frames(core, 1)
    # The rest is read frame by frame: at a steady 10 Hz a game drawing 30 fps
    # could show the same page at every read
    assert core.link_interval() == wow_addons.LINK_PAGE_S
    frames(core, 20)
    assert [b["label"] for b in core.snapshot()["data"]["bars"]] == labels
    assert core.link_interval() == wow_addons.LINK_FAST_S
    rgb = core.screen.grab(0, 0, 800, link.ROWS)
    assert link.read_row(rgb, 800, link.locate(rgb, 800)[0]).pages >= 3


def test_the_link_switched_off_in_game_is_told_once(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    frames(core, 2)
    game.cmd("slash link off")
    assert frames(core, 60) == set()                  # still up for a moment
    assert frames(core, 90) == {"game"}               # then down, told once
    assert core.companion_status()["link"] is False and core.link_interval() == wow_addons.LINK_SLOW_S
    game.cmd("slash link on")
    assert frames(core, 2) == {"game"} and core.companion_status()["link"] is True
    assert core.link_interval() == wow_addons.LINK_FAST_S


def test_with_the_link_up_a_command_skips_the_first_read(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    frames(core, 2)
    pressed.clear()
    core.run("bw_pull", {"seconds": 10})
    assert calls(game)[-1] == {"cmd": "/pull", "msg": "10"}
    assert pressed.count(("KEY_LEFTCTRL", "KEY_C")) == 1      # only for its answer


def test_a_full_read_keeps_its_bags_while_the_link_runs(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    core.sync(full=True)
    assert core.snapshot()["data"]["items"]
    frames(core, 2)
    d = core.snapshot()["data"]
    assert d["link"] is True and d["items"] and d["vault"]


def game_live(core):
    """The game module's live view, as the page gets it."""
    return next(m for m in core.update_msgs() if m["type"] == "wow_game_update")["data"]["live"]


def test_the_augment_rune_is_known_by_its_spell(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    game.cmd("rune 1")                                 # Midnight's rune buff: "Void-Touched"
    frames(core, 20)
    assert {"id": 1264426, "name": "Void-Touched", "rem": 2999} in [
        dict(b, rem=round(b["rem"], -1) - 1) for b in core.snapshot()["data"]["buffs"]]
    checks = {c["id"]: c for c in game_live(core)["checks"]}
    assert checks["rune"]["ok"] and checks["rune"]["label"] == "Void-Touched"


def test_no_character_is_saved_before_the_game_knows_its_realm(game, tmp_path):
    game.cmd("early 1")                                # the first moments of a login: no realm yet
    game.cmd("event PLAYER_LOGOUT")
    chars = parse_saved_variables(json.loads(game.cmd("sv")))["TouchDashboardDB"]["chars"]
    assert list(chars) == ["Thrall-Draenor"]           # no "Thrall-" beside it
    game.cmd("stray")                                  # what 2.1.0 left: "Thrall-", no realm, no gold
    game.cmd("early 0")
    game.cmd("event PLAYER_LOGOUT")
    chars = parse_saved_variables(json.loads(game.cmd("sv")))["TouchDashboardDB"]["chars"]
    assert list(chars) == ["Thrall-Draenor"] and chars["Thrall-Draenor"]["gold"] == 123456789012


def test_a_ready_check_says_who_is_not_ready(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    frames(core, 2)
    game.cmd("event READY_CHECK Thrall 35")            # you asked: you are ready
    game.cmd("event READY_CHECK_CONFIRM party1 true")
    game.cmd("event READY_CHECK_CONFIRM party2 false")
    frames(core, 20)
    rc = core.snapshot()["data"]["rc"]
    assert (rc["state"], rc["total"], rc["ready"]) == (1, 5, 2) and 30 < rc["rem"] <= 35
    assert rc["missing"] == [{"name": "Mage", "a": 2}, {"name": "Druid", "a": 0}, {"name": "Evoker", "a": 0}]
    game.cmd("event READY_CHECK_FINISHED false")
    frames(core, 20)
    live = game_live(core)["rc"]
    assert live["state"] == "over" and live["ready"] == 2
    assert [(m["name"], m["a"]) for m in live["missing"]] == [("Mage", 2), ("Druid", 3), ("Evoker", 3)]
    assert pressed == []


def test_nsrts_alerts_for_the_phase_come_through(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    frames(core, 70)                                   # a whole second turns: WoW's clock is known
    game.cmd("event ENCOUNTER_START 3306 Dimensius 16 20")
    game.cmd("nsrt phase 1 3306")
    frames(core, 20)
    snap = core.snapshot()
    alerts = snap["data"]["alerts"]
    assert (alerts["phase"], alerts["enc"]) == (1, "Dimensius")
    # In order, plain text: the raid mark's texture is {skull}, colour codes go, a spell names itself
    assert [(a["text"], a["spell"]) for a in alerts["list"]] == [
        ("Ice Block the {skull} soak", None), ("Spread", None), ("Tranquility", 740)]
    assert alerts["list"][0]["rem"] - snap["age"] == pytest.approx(13 - 20 / 60, abs=0.2)   # as a screen counts it
    game.cmd("nsrt delay 5")                           # a boss's cast moved the rest of the phase
    frames(core, 20)
    snap = core.snapshot()
    assert snap["data"]["alerts"]["list"][0]["rem"] - snap["age"] == pytest.approx(18 - 40 / 60, abs=0.2)
    game.cmd("nsrt phase 2 3306")
    frames(core, 20)
    assert [a["text"] for a in core.snapshot()["data"]["alerts"]["list"]] == ["Stack"]
    view = game_live(core)["alerts"]
    assert (view["phase"], view["test"], view["enc"]) == (2, False, "Dimensius")
    game.cmd("nsrt hide")                              # the encounter is over
    frames(core, 20)
    assert core.snapshot()["data"]["alerts"]["list"] == [] and game_live(core)["alerts"] is None


def test_nsrts_test_run_starts_from_a_key(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    frames(core, 70)
    core.run("nsrt_test")                              # /tdc nstest through the box: NSRT's own test of phase 1
    frames(core, 20)
    view = game_live(core)["alerts"]
    assert view["test"] is True and view["phase"] is None
    assert [a["text"] for a in view["list"]] == ["Other boss", "Ice Block the {skull} soak", "Spread", "Tranquility"]


def test_a_mythic_plus_run_comes_through(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    frames(core, 70)                                   # a whole second turns: WoW's clock is known
    game.cmd("mplus start 12")                         # in the key, the countdown before the timer
    frames(core, 20)
    m = core.snapshot()["data"]["mplus"]
    assert (m["state"], m["running"], m["level"], m["name"], m["limit"]) == (1, False, 12, "Ara-Kara", 1800)
    assert [b["name"] for b in m["bosses"]] == ["Avanoxx", "Anub'zekt", "Ki'katal the Harvester"]   # no ✓, no "defeated"
    assert game_live(core)["mplus"]["chests"] == [1080, 1440, 1800]                                  # +3, +2, +1
    game.cmd("mplus go")
    frames(core, 125)
    game.cmd("mplus kill 1")
    game.cmd("mplus forces 230")                       # 230 of 460: half
    game.cmd("mplus death")
    frames(core, 20)
    snap = core.snapshot()
    m = snap["data"]["mplus"]
    assert m["running"] and m["elapsed"] - snap["age"] == pytest.approx(145 / 60, abs=0.6)   # counted from its start
    assert m["bosses"][0] == {"name": "Avanoxx", "done": True, "at": 2} and not m["bosses"][1]["done"]
    assert (m["forces"], m["deaths"], m["lost"]) == (50.0, 1, 15)
    live = game_live(core)["mplus"]
    assert live["state"] == "running" and live["counting"] is True
    game.cmd("mplus done 1500000 1 2")                 # timed: 25:00, two chests
    frames(core, 20)
    m = core.snapshot()["data"]["mplus"]
    assert (m["state"], m["elapsed"], m["onTime"], m["upgrades"]) == (2, 1500.0, True, 2)
    assert game_live(core)["mplus"]["state"] == "over"
    game.cmd("mplus reset")
    frames(core, 20)
    assert core.snapshot()["data"]["mplus"] is None and game_live(core)["mplus"] is None
    assert pressed == []


def test_wow_is_told_once_when_a_newer_addon_is_installed(game, tmp_path):
    core, _, _ = make(game, tmp_path)                 # the folder has this install's; WoW runs "2.0.0"
    frames(core, 2)                                   # the live link says which runs
    assert core.companion_status()["running"] == "2.0.0"
    core.run("bw_version")
    core.run("bw_version")
    told = [p for p in json.loads(game.cmd("printed")) if "/reload" in p]
    assert len(told) == 1 and f"{wow_addons.bundled_version()} is installed (this is 2.0.0)" in told[0]


def test_a_pull_whose_numbers_the_game_hides_is_still_seen(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    frames(core, 2)
    game.cmd("event START_PLAYER_COUNTDOWN Player-1-0A SECRET SECRET")   # in a raid: the leader's pull, numbers secret
    frames(core, 20)
    snap = core.snapshot()["data"]
    assert snap["pull_hidden"] is True and snap["bars"] == []
    assert game_live(core)["pull_hidden"] is True
    game.cmd("event PLAYER_REGEN_DISABLED")             # combat began: that was the pull
    frames(core, 20)
    assert core.snapshot()["data"]["pull_hidden"] is False
    game.cmd("event START_PLAYER_COUNTDOWN Player-1-0A SECRET SECRET")
    game.cmd("event CANCEL_PLAYER_COUNTDOWN Player-1-0A")
    frames(core, 20)
    assert core.snapshot()["data"]["pull_hidden"] is False
    game.cmd("event START_PLAYER_COUNTDOWN Player-1-0A SECRET SECRET")
    frames(core, 20)
    game.cmd("timers 61")                               # nobody pulled: a minute on, it is forgotten
    frames(core, 20)
    assert core.snapshot()["data"]["pull_hidden"] is False
    assert pressed == []


def test_a_ready_check_whose_asker_the_game_hides(game, tmp_path):
    core, pressed, _ = make(game, tmp_path)
    frames(core, 2)
    game.cmd("rcstatus party1 ready")                   # the Priest asked: the game counts them ready
    game.cmd("event READY_CHECK SECRET SECRET")         # in a dungeon or raid: who and how long are secret
    game.cmd("event READY_CHECK_CONFIRM party2 true")
    frames(core, 20)
    rc = core.snapshot()["data"]["rc"]
    assert (rc["state"], rc["total"], rc["ready"]) == (1, 5, 2) and 30 < rc["rem"] <= 35   # Blizzard's 35 s
    game.cmd("event READY_CHECK_FINISHED false")
    frames(core, 20)
    live = game_live(core)["rc"]
    assert [(m["name"], m["a"]) for m in live["missing"]] == [("Thrall", 3), ("Druid", 3), ("Evoker", 3)]
    assert pressed == []


def test_the_game_says_when_it_hides_auras(game, tmp_path):
    core, _, _ = make(game, tmp_path)
    frames(core, 2)
    assert core.snapshot()["data"]["aura_secret"] is False
    checks = {c["id"]: c for c in game_live(core)["checks"]}
    assert not checks["flask"]["hidden"]                # out of a key: missing is missing
    game.cmd("restrict 1")                              # a key started: auras secret out of combat too
    game.cmd("secret 1")                                # Well Fed now reads as secret; Arcane Intellect does not
    game.cmd("event ADDON_RESTRICTION_STATE_CHANGED 2 2")
    frames(core, 20)
    assert core.snapshot()["data"]["aura_secret"] is True
    checks = {c["id"]: c for c in game_live(core)["checks"]}
    assert checks["food"]["hidden"] and checks["flask"]["hidden"]
    assert checks["intellect"]["ok"]                    # a buff the game still lets through shows as it is
