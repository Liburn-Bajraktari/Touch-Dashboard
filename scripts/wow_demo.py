"""The WoW page with fictional data, for screenshots and trying the page out.

    .venv/bin/python scripts/wow_demo.py [--port 8899] [--home DIR]

A server like the real one on 127.0.0.1:<port>, with its own HOME (a new
temporary folder unless --home is given): your config, game folder, keyboard
and clipboard are never touched. It has a fake game folder with the companion
and a few addons "installed", SavedVariables for three characters and an NSRT
note, Battle.net and Raider.IO answered from the test fixtures (names changed),
an emulated companion behind a keyboard and clipboard that go nowhere, and the
live link faked above its pixels: one moment kept as it is, so screenshots come
out the same every time (the page still counts its timers). Two moments: a raid
night (the default) and a Mythic+ key, where the game hides buffs; GET
/demo/moment/raid or /demo/moment/key switches (a demo-only route), and a page
loaded after it shows that moment.

scripts/wow_docs_render.py takes the renders in docs/WOW.md from it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--port", type=int, default=8899)
parser.add_argument("--home", help="the demo's HOME (default: a new temporary folder)")
opts = parser.parse_args()

HOME = opts.home or tempfile.mkdtemp(prefix="td-wow-demo-")
os.environ["HOME"] = HOME
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

NAME, REALM, REALM_SLUG, GUILD = "Aelwyn", "Silvermoon", "silvermoon", "Ember Court"

data_dir = os.path.join(HOME, ".config", "touch-dashboard")
os.makedirs(data_dir, exist_ok=True)
wow_root = os.path.join(HOME, "wow", "_retail_")
addons = os.path.join(wow_root, "Interface", "AddOns")
for folder in ("BigWigs", "NorthernSkyRaidTools", "Details", "WeakAuras", "Plater", "MythicDungeonTools",
               "SimulationCraft"):
    os.makedirs(os.path.join(addons, folder), exist_ok=True)
sv_dir = os.path.join(wow_root, "WTF", "Account", "DEMO", "SavedVariables")
os.makedirs(sv_dir, exist_ok=True)
now = time.time()


def lua_items(items):
    return "{ " + ", ".join(f'{{ ["id"] = {i}, ["n"] = {n}, ["q"] = {q}, ["name"] = "{name}" }}'
                            for i, n, q, name in items) + " }"


FLASK = (241325, 18, 3, "Flask of the Blood Knights")
with open(os.path.join(sv_dir, "TouchDashboard.lua"), "w") as f:
    f.write(f'''
TouchDashboardDB = {{
	["chars"] = {{
		["{NAME}-{REALM}"] = {{ ["name"] = "{NAME}", ["realm"] = "{REALM}", ["class"] = "MAGE", ["level"] = 90,
			["ilvl"] = 671.4, ["gold"] = 4831244211, ["updated"] = {int(now - 3600)},
			["key"] = {{ ["level"] = 12, ["name"] = "Ara-Kara, City of Echoes" }},
			["vault"] = {{ {{ ["t"] = 1, ["p"] = 8, ["th"] = 1 }}, {{ ["t"] = 1, ["p"] = 8, ["th"] = 4 }}, {{ ["t"] = 1, ["p"] = 8, ["th"] = 8 }},
			              {{ ["t"] = 3, ["p"] = 4, ["th"] = 2 }}, {{ ["t"] = 3, ["p"] = 4, ["th"] = 4 }}, {{ ["t"] = 6, ["p"] = 1, ["th"] = 2 }} }},
			["items"] = {{ ["bags"] = {lua_items([FLASK, (242300, 40, 3, "Hearty Royal Roast"), (259085, 6, 4, "Void-Touched Augment Rune")])},
			              ["bank"] = {lua_items([(FLASK[0], 40, 3, FLASK[3])])} }} }},
		["Steelhide-{REALM}"] = {{ ["name"] = "Steelhide", ["realm"] = "{REALM}", ["class"] = "WARRIOR", ["level"] = 90,
			["ilvl"] = 659.1, ["gold"] = 120044500, ["updated"] = {int(now - 3 * 86400)},
			["key"] = {{ ["level"] = 10, ["name"] = "The Dawnbreaker" }},
			["vault"] = {{ {{ ["t"] = 1, ["p"] = 4, ["th"] = 1 }}, {{ ["t"] = 1, ["p"] = 4, ["th"] = 4 }} }},
			["items"] = {{ ["bags"] = {lua_items([(FLASK[0], 2, 3, FLASK[3])])} }} }},
		["Leafwhisper-{REALM}"] = {{ ["name"] = "Leafwhisper", ["realm"] = "{REALM}", ["class"] = "DRUID", ["level"] = 82,
			["ilvl"] = 610, ["gold"] = 9870000, ["updated"] = {int(now - 12 * 86400)} }},
	}},
	["warband"] = {{ ["gold"] = 25000000000, ["items"] = {lua_items([(FLASK[0], 60, 3, FLASK[3])])} }},
}}
''')
from wow_addons.modules import nsrt  # noqa: E402
with open(os.path.join(sv_dir, nsrt.NOTE_FILE), "w") as f:
    f.write('NSRT = { ["ActiveReminder"] = "Dimensius", ["Note"] = "EncounterID:3306;Difficulty:Mythic;Name:Dimensius;\\n'
            f'time:13;ph:1;tag:{NAME};spellid:45438;text:Ice Block the soak\\n'
            'time:1:05;ph:2;tag:ranged;text:Spread; then stack\\n'
            'time:40;ph:1;tag:Healer1 Healer2;spellid:740;text:Tranquility\\nBloodlust on pull" }\n')

# A week of WoW Token prices for its trace.
week = [[int(now - h * 3600), int(410_000 + 18_000 * math.sin(h / 9) + 300 * (168 - h))] for h in range(168, -1, -2)]
with open(os.path.join(data_dir, "wow_spells.json"), "w") as f:
    json.dump({"spells": {}, "token": {"eu": week}}, f)

# The plates as the renders show them first (WoW settings → the pencil arranges them).
LAYOUT = {"columns": [
    [{"w": "nsrt.note", "size": "full"}, {"w": "game.buffs", "size": "full"}],
    [{"w": "char.plate", "size": "full"}, {"w": "raid.timers", "size": "full"}, {"w": "raid.next", "size": "full"}],
    [{"w": "markers.world", "size": "full"}, {"w": "game.keystone", "size": "full"},
     {"w": "token.price", "size": "half"}, {"w": "game.wallet", "size": "half"}],
]}
with open(os.path.join(data_dir, "config.json"), "w") as f:
    json.dump({"disc_enabled": False, "disc_vc_enabled": False, "bnet_id": "demo", "bnet_secret": "demo",
               "theme": "graphite", "wow_addons": {
                   "wow_path": wow_root, "character": NAME, "realm": REALM, "region": "eu", "role": "ranged",
                   "keybinds": {"F1": {"cmd": "raid_pull", "args": {"seconds": 10}}},
                   "custom": [{"id": "c_demo1", "label": "MRT note", "glyph": "clipboard-list", "lines": ["/mrt note"],
                               "via": "auto"}],
                   "layout": LAYOUT}}, f)

import server  # noqa: E402
import test_armory  # noqa: E402

# Battle.net and Raider.IO from the test fixtures, under the demo's names.
NAMES = (("Magicbane", NAME), ("magicbane", NAME.lower()), ("Twisting Nether", REALM),
         ("twisting-nether", REALM_SLUG), ("Northern Sky", GUILD))
routes = json.dumps(test_armory.routes(**{"/data/wow/token/index": (200, {
    "price": int(week[-1][1] * 10000), "last_updated_timestamp": int(now * 1000)})}))
for old, new in NAMES:
    routes = routes.replace(old, new)
server.armory._session = test_armory.FakeSession({k: tuple(v) for k, v in json.loads(routes).items()})
server.armory.configure("demo", "demo")


class Clip:
    """A clipboard that is not the PC's."""
    text = ""

    def get_text(self):
        return self.text

    def set_text(self, t):
        self.text = t
        return True


SNAP = {"v": 1, "ver": server.wow_addons.bundled_version(), "combat": False, "lockdown": False, "link": True,
        "char": {"name": NAME, "realm": REALM, "class": "MAGE", "spec": "Frost", "level": 90, "ilvl": 671.4},
        "gold": 4831990211, "bags": {"free": 23, "total": 142}, "dur": {"low": 71, "broken": 0},
        "zone": "Manaforge Omega", "key": {"map": 503, "level": 12, "name": "Ara-Kara, City of Echoes"},
        "weapon": {"mh": 3540, "oh": False},
        "buffs": [{"id": 1459, "name": "Arcane Intellect", "rem": 3200},
                  {"id": 21562, "name": "Power Word: Fortitude", "rem": 2900},
                  {"id": 1126, "name": "Mark of the Wild", "rem": 3300}, {"id": 1, "name": "Well Fed", "rem": 2400},
                  {"id": 2, "name": "Flask of the Blood Knights", "rem": 3500},
                  {"id": 1264426, "name": "Void-Touched", "rem": 1700}],
        "bars": [{"label": "Pull", "rem": 8.4, "dur": 10.0}, {"label": "Cosmic Collapse", "rem": 31.0, "dur": 45.0}],
        "group": {"n": 20, "raid": True, "classes": ["MAGE", "PRIEST", "DRUID", "WARRIOR", "EVOKER"]},
        "rc": {"state": 1, "rem": 24.0, "total": 20, "ready": 17,
               "missing": [{"name": "Steelhide", "a": 0}, {"name": "Leafwhisper", "a": 2}, {"name": "Brannoc", "a": 0}]},
        "alerts": {"phase": 1, "enc": "Dimensius", "list": [
            {"text": "Ice Block the {skull} soak", "rem": 9.0, "spell": None},
            {"text": "Spread {star} {circle}", "rem": 24.0, "spell": None},
            {"text": "Tranquility", "rem": 41.0, "spell": 740},
            {"text": "Stack on {moon} for the add", "rem": 66.0, "spell": None},
            {"text": "Bloodlust", "rem": 95.0, "spell": None}]}}

# A Mythic+ key: a party of five, the timer running; the game hides buffs from addons.
# In a key the boss's bars come from Blizzard's timeline through BigWigs, their names
# secret (no label); NSRT runs no reminders in dungeons.
KEY = dict(SNAP, buffs=[], bars=[{"label": None, "rem": 9.0, "dur": 20.0}, {"label": None, "rem": 24.0, "dur": 30.0}],
           rc={"state": 0}, alerts={"phase": 0, "list": []}, zone="Ara-Kara, City of Echoes",
           group={"n": 5, "raid": False, "classes": ["MAGE", "PRIEST", "DRUID", "WARRIOR", "EVOKER"]},
           mplus={"state": 1, "map": 503, "name": "Ara-Kara, City of Echoes", "level": 12, "limit": 1800,
                  "elapsed": 1045.0, "running": True, "deaths": 3, "lost": 45, "forces": 72.4,
                  "bosses": [{"name": "Avanoxx", "done": True, "at": 371},
                             {"name": "Anub'zekt", "done": True, "at": 842},
                             {"name": "Ki'katal the Harvester", "done": False, "at": None}]})
MOMENTS = {"raid": SNAP, "key": KEY}
moment = {"now": "raid"}


@server._app.get("/demo/moment/{name}")
async def demo_moment(name: str):
    """The demo only: which moment the live link shows."""
    if name not in MOMENTS:
        return {"ok": False, "moments": list(MOMENTS)}
    moment["now"] = name
    return {"ok": True, "moment": name}


clip = Clip()
box = {"on": False, "text": ""}


def keys(*k):
    """The companion addon, emulated: its box answers the chord, Ctrl+C, Ctrl+V
    and Enter; everything else goes nowhere."""
    if k == server.wow_addons.FOCUS_KEYS:
        box.update(on=True, text="TDS1 - " + json.dumps(MOMENTS[moment["now"]]))
    elif k == ("KEY_LEFTCTRL", "KEY_C") and box["on"]:
        clip.text = box["text"]
        if not box["text"].startswith("TDS1 - "):   # its answer copied: the box lets go, as the addon's does
            box["on"] = False
    elif k == ("KEY_LEFTCTRL", "KEY_V") and box["on"]:
        box["text"] = clip.text
    elif k == ("KEY_ENTER",) and box["on"]:
        parts = (box["text"] or "").split(" ", 3)
        if parts[0] == "TD1":
            result = "loaded" if "b" in parts[2] else "ok"
            box["text"] = f"TDS1 {parts[1]} " + json.dumps(
                {**MOMENTS[moment["now"]], "sh": server.wow_addons.adler32(server.wow.slot_payload()), "result": result})
            box["on"] = "r" in parts[2]
        else:
            box["on"] = False


class Screen:
    """WoW in front, its box's beacon showing while the box is open."""

    def front(self):
        return True

    def beacon(self):
        return box["on"]

    def size(self):
        return None

    def grab(self, *a):
        return None


server.wow.send_keys = keys
server.wow.keys_ready = lambda: True
server.wow.clipboard = clip
server.wow.screen = Screen()
server.wow.poll()
server.wow.install_companion()


def hold():
    """The live link, faked above its pixels: the same moment, always fresh."""
    while True:
        with server.wow._lock:
            server.wow._link_at = server.wow.clock()
            server.wow._snapshot = dict(MOMENTS[moment["now"]])
            server.wow._snapshot_at = server.wow.clock()
        time.sleep(0.5)


threading.Thread(target=hold, daemon=True).start()
print(f"WoW page demo: http://127.0.0.1:{opts.port} (HOME {HOME})", flush=True)
server.run_fastapi_server(host="127.0.0.1", port=opts.port)
