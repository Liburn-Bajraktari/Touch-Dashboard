"""Renders of the WoW page for docs/WOW.md (and the README's one), from the
fake-data demo.

    .venv/bin/python scripts/wow_demo.py &               # fictional data on 127.0.0.1:8899
    /usr/bin/python3 scripts/wow_docs_render.py [--url http://127.0.0.1:8899] [--out docs/images/wow] [name…]

Needs WebKitGTK 4.1, PyGObject and pycairo: the desktop window's own engine,
from the system's packages (not the venv). Each shot loads the page offscreen
at the tablet's size (the Redmi Pad 2's 1138 × 711 CSS pixels) drawn at 2×
(GDK_SCALE=2, set below: a 2× page zoom resolves rem in container queries
otherwise, and plates take their narrow layouts),
as the tablet draws it (the rich effects tier, round corners), opens the WoW
page, sets up what the shot shows, waits, and saves the screen or one plate
of it. Shots that arrange the page save their layout on the demo server, so
the next shot puts back the one it needs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

os.environ.setdefault("GDK_BACKEND", "x11")             # GDK_SCALE is ignored on Wayland
os.environ.setdefault("GDK_SCALE", "2")                 # before GTK starts: draw at 2× without zooming the page
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

W, H, ZOOM = 1138, 711, 2

# The demo's first layout (scripts/wow_demo.py), a raid night; one with the
# other plates; one for the Mythic+ moment.
MAIN = {"columns": [
    [{"w": "nsrt.note", "size": "full"}, {"w": "game.buffs", "size": "full"}],
    [{"w": "char.plate", "size": "full"}, {"w": "raid.timers", "size": "full"}, {"w": "raid.next", "size": "full"}],
    [{"w": "markers.world", "size": "full"}, {"w": "game.keystone", "size": "full"},
     {"w": "token.price", "size": "half"}, {"w": "game.wallet", "size": "half"}],
]}
KEY = {"columns": [[{"w": "raid.mplus", "size": "full"}], [{"w": "game.buffs", "size": "full"}], [{"w": "raid.next", "size": "full"}]]}


def callout(text, label, glyph):
    return {"cmd": "wow_rw", "args": {"text": text}, "label": label, "glyph": glyph}


MORE = {"columns": [
    [{"w": "nsrt.alerts", "size": "full"}, {"w": "game.bags", "size": "full"}],
    [{"w": "raid.timers", "size": "full"},
     {"w": "keys", "size": "full", "id": "callouts", "title": "Raid callouts", "keys": [
         callout("Stack!", "Stack", "users"), callout("Spread!", "Spread", "move"),
         callout("Defensives now", "Defensives", "shield"), callout("Bloodlust now", "Bloodlust", "flame"),
         callout("Wipe, run back", "Wipe", "skull"), callout("Pull in 10", "Pull soon", "timer")]}],
    [{"w": "game.keystone", "size": "full"}, {"w": "game.alts", "size": "full"}, {"w": "combatlog.log", "size": "full"}],
]}

# Run in the page before each shot: the tablet's tier and corners, a theme,
# the shot's layout, the WoW page open.
PRELUDE = """
void (async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  window.__rect = undefined;
  document.documentElement.dataset.fx = 'rich';
  document.documentElement.dataset.corners = 'round';
  try { localStorage.setItem('td_wow_raid_lengths', %(lengths)s ? '1' : '0'); } catch (e) {}
  TDTheme.apply(%(theme)s);
  socket.send(JSON.stringify({ type: 'wow_layout_save', layout: %(layout)s }));
  await sleep(700);
  window.TDWow.open();
  await sleep(1300);
  const v = document.querySelector('#wow-view');
  const plate = (name) => v.querySelector(`.wow-cell[aria-label="${name}"]`);
  const pick = async (name) => {
    const row = [...document.querySelectorAll('#wow-picker .wow-pick-row')]
      .find(r => r.querySelector('.wow-pick-name').textContent === name);
    row.click(); await sleep(400);
  };
  let crop = null;
  %(setup)s
  if (crop) {
    const r = crop.getBoundingClientRect();
    window.__rect = { x: r.left, y: r.top, w: r.width, h: r.height };
  } else {
    window.__rect = null;
  }
})();
"""

# name: (layout, theme, Lengths open, setup JS (may set `crop` to an element), seconds to wait)
SHOTS = {
    "wow-page": (MAIN, "graphite", False, "", 1.5),
    "wow-page-daylight": (MAIN, "daylight", False, "", 1.5),
    "wow-edit": (MAIN, "graphite", False, """
        v.querySelector('.wow-edit').click(); await sleep(400);
        plate('World markers').querySelector('[data-act="remove"]').click(); await sleep(400);""", 1.5),
    "wow-templates": (MAIN, "graphite", False, """
        v.querySelector('.wow-edit').click(); await sleep(400);
        [...v.querySelectorAll('.wow-col')][1].querySelector(':scope > .wow-add-tile').click(); await sleep(400);
        await pick('Keys');""", 1.5),
    "wow-key-editor": (MORE, "graphite", False, """
        v.querySelector('.wow-edit').click(); await sleep(400);
        plate('Raid callouts').querySelector('.wow-key').click(); await sleep(600);""", 1.5),
    "wow-settings": (MAIN, "graphite", False, """
        v.querySelector('.wow-gear').click(); await sleep(600);""", 1.5),
    "plate-note": (MAIN, "graphite", False, "crop = plate('NSRT note');", 1.0),
    "plate-buffs": (MAIN, "graphite", False, "crop = plate('Buff check');", 1.0),
    "plate-character": (MAIN, "graphite", False, "crop = plate('Character');", 1.0),
    "plate-raid": (MAIN, "graphite", False, "crop = plate('Raid & dungeons');", 1.0),
    "plate-markers": (MAIN, "graphite", False, "crop = plate('World markers');", 1.0),
    "plate-mplus": (KEY, "graphite", False, "crop = plate('Mythic+');", 1.0),
    "plate-buffs-key": (KEY, "graphite", False, "crop = plate('Buff check');", 1.0),
    "plate-next": (MAIN, "graphite", False, "crop = plate('Next alert');", 1.0),
    "plate-next-key": (KEY, "graphite", False, "crop = plate('Next alert');", 1.0),
    "plate-token": (MAIN, "graphite", False, "crop = plate('WoW Token');", 1.0),
    "plate-gold": (MAIN, "graphite", False, "crop = plate('Gold');", 1.0),
    "plate-raid-lengths": (MORE, "graphite", True, "crop = plate('Raid & dungeons');", 1.0),
    "plate-alerts": (MORE, "graphite", False, "crop = plate('Encounter alerts');", 1.0),
    "plate-callouts": (MORE, "graphite", False, "crop = plate('Raid callouts');", 1.0),
    "plate-bags": (MORE, "graphite", False, "crop = plate('Bags');", 1.0),
    "plate-keystone": (MORE, "graphite", False, "crop = plate('Keystone & vault');", 1.0),
    "plate-characters": (MORE, "graphite", False, "crop = plate('Characters');", 1.0),
    "plate-combatlog": (MORE, "graphite", False, "crop = plate('Combat log');", 1.0),
}

# The demo's moment each shot shows (wow_demo.py): a raid night unless named here.
MOMENTS = {"plate-mplus": "key", "plate-buffs-key": "key", "plate-next-key": "key"}

parser = argparse.ArgumentParser(description="Renders of the WoW page for the docs")
parser.add_argument("--url", default="http://127.0.0.1:8899")
parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                   "docs", "images", "wow"))
parser.add_argument("names", nargs="*", help="only these shots")
opts = parser.parse_args()
os.makedirs(opts.out, exist_ok=True)
queue = [n for n in SHOTS if not opts.names or n in opts.names]

win = Gtk.OffscreenWindow()
view = WebKit2.WebView()
view.set_size_request(W, H)
win.add(view)
win.show_all()
current = {"name": None}


def js(script, then=None):
    def done(v, res):
        value = v.evaluate_javascript_finish(res)
        if then:
            then(value.to_string() if value else "null")
    view.evaluate_javascript(script, -1, None, None, None, done)


def save(name, rect_json):
    def done(v, res):
        surface = v.get_snapshot_finish(res)
        path = os.path.join(opts.out, f"{name}.png")
        rect = json.loads(rect_json) if rect_json not in ("null", "undefined") else None
        if rect:
            pad = 10
            x, y = max(0, int((rect["x"] - pad) * ZOOM)), max(0, int((rect["y"] - pad) * ZOOM))
            w = min(W * ZOOM - x, int((rect["w"] + 2 * pad) * ZOOM))
            h = min(H * ZOOM - y, int((rect["h"] + 2 * pad) * ZOOM))
            out = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
            ctx = cairo.Context(out)
            ctx.set_source_surface(surface, -x, -y)
            ctx.paint()
            out.write_to_png(path)
        else:
            surface.write_to_png(path)
        print(f"{name}: {path}", flush=True)
        next_shot()
    view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, done)


def run_shot():
    layout, theme, lengths, setup, wait = SHOTS[current["name"]]
    js(PRELUDE % {"layout": json.dumps(layout), "theme": json.dumps(theme), "lengths": json.dumps(lengths),
                  "setup": setup})
    GLib.timeout_add(int((3.0 + wait) * 1000), lambda: js("JSON.stringify(window.__rect === undefined ? null : window.__rect)",
                                                          lambda r: save(current["name"], r)) and False)
    return False


def loaded(v, event):
    if event == WebKit2.LoadEvent.FINISHED and current["name"]:
        GLib.timeout_add(2500, run_shot)


def next_shot():
    if not queue:
        Gtk.main_quit()
        return
    current["name"] = queue.pop(0)
    with urllib.request.urlopen(f"{opts.url}/demo/moment/{MOMENTS.get(current['name'], 'raid')}", timeout=5) as r:
        if not json.load(r).get("ok"):
            sys.exit(f"{opts.url} is not scripts/wow_demo.py")
    view.load_uri(opts.url)


view.connect("load-changed", loaded)
GLib.idle_add(lambda: next_shot() and False)
GLib.timeout_add(15 * 60 * 1000, Gtk.main_quit)
Gtk.main()
sys.exit(0)
