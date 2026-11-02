"""
The game as the companion addon (addon/TouchDashboard) shows it: your buffs
before a pull, gold, bags, durability, keystone and Great Vault, and every
character it has seen, plus a search through all their bags.

Two sources. Live: the addon's live link, read from WoW's window several
times a second while it shows (link.py), or else the snapshot its box hands
over with each command sent through it and with the Read key (WoW must be in
front). Saved: its SavedVariables (TouchDashboard.lua), which WoW writes on
/reload, logout and exit: every character as of then. In combat, encounters
and M+ the game keeps buffs secret from addons, so the buff check is for
before the pull.

Consumables are told by their English names (Flask, Phial, Well Fed,
Augment) or their spell ids (Midnight's augment rune buff is called just
"Void-Touched"); raid buffs by spell id, in any language.

The live state also carries the last ready check's answers, NSRT's alerts
for the phase that runs and the Mythic+ run: Raid & dungeons' plates and the
NSRT alert plates show them.
"""
from __future__ import annotations

import re
from typing import Any

from wow_addons import AddonModule, Arg, Command, CommandError, Context, Watch, Widget, chat_text
from wow_addons.lua import LuaError, items, parse_saved_variables, table

SV_FILE = "TouchDashboard.lua"
FIND_MAX = 40
BUFFS_MAX = 60
CHARS_MAX = 60

# id, name, spell ids it shows as, the class that casts it, its cast command
RAID_BUFFS = (
    ("intellect", "Arcane Intellect", (1459, 432778), "MAGE", "cast_intellect"),
    ("fortitude", "Power Word: Fortitude", (21562,), "PRIEST", "cast_fortitude"),
    ("shout", "Battle Shout", (6673,), "WARRIOR", "cast_shout"),
    ("wild", "Mark of the Wild", (1126, 432661), "DRUID", "cast_wild"),
    ("bronze", "Blessing of the Bronze", (381748, 381732, 381741, 381746, 381749, 381750, 381751, 381752,
                                          381753, 381754, 381756, 381757, 381758), "EVOKER", "cast_bronze"),
    ("skyfury", "Skyfury", (462854,), "SHAMAN", "cast_skyfury"),
)
# id, name, words in the buff's English name, its spell ids
CONSUMABLES = (
    ("flask", "Flask", ("Flask", "Phial"), ()),
    ("food", "Food", ("Well Fed", "Hearty Well Fed"), ()),
    ("rune", "Augment rune", ("Augment",), (1264426,)),
)
ALERTS_MAX = 8
_DEFEATED = re.compile(r"\s+defeated$", re.I)
_DEFEATED_CUT = re.compile(r"\s+d(?:e(?:f(?:e(?:a(?:t(?:e(?:d)?)?)?)?)?)?)?$", re.I)   # cut short at 24 bytes
CHESTS = (0.6, 0.8, 1.0)       # +3, +2, +1: the share of the key's time each needs (EllesmereUI's timer, Midnight)
VAULT_KINDS = {1: "dungeons", 3: "raid", 6: "world", 2: "pvp"}


def _num(v: Any) -> int | float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v: Any, n: int = 64) -> str | None:
    return v[:n] if isinstance(v, str) and v else None


def _items(raw: Any) -> list[dict]:
    out = []
    for e in items(raw)[:2000]:
        e = table(e)
        iid, n = _num(e.get("id")), _num(e.get("n"))
        if iid and n:
            out.append({"id": int(iid), "n": int(n), "q": int(_num(e.get("q")) or 0), "name": _text(e.get("name"))})
    return out


def _vault(raw: Any) -> dict:
    """{dungeons, raid, world, pvp}: slots unlocked of each kind."""
    out: dict[str, int] = {}
    for a in items(raw):
        a = table(a)
        kind = VAULT_KINDS.get(_num(a.get("t")))
        if kind:
            p, th = _num(a.get("p")) or 0, _num(a.get("th")) or 0
            out[kind] = out.get(kind, 0) + (1 if th and p >= th else 0)
    return out


def _char(key: str, c: dict) -> dict:
    bags, dur, ks = table(c.get("bags")), table(c.get("dur")), table(c.get("key"))
    stored = table(c.get("items"))
    return {
        "key": key[:80], "name": _text(c.get("name"), 48) or key.split("-")[0], "realm": _text(c.get("realm"), 64),
        "class": _text(c.get("class"), 16), "level": _num(c.get("level")), "ilvl": _num(c.get("ilvl")),
        "gold": int(_num(c.get("gold")) or 0) // 10000,
        "bags": {"free": _num(bags.get("free")), "total": _num(bags.get("total"))} if bags else None,
        "dur": _num(dur.get("low")),
        "keystone": {"level": _num(ks.get("level")), "name": _text(ks.get("name"))} if _num(ks.get("level")) else None,
        "vault": _vault(c.get("vault")),
        "updated": _num(c.get("updated")),
        "items": {"bags": _items(stored.get("bags")), "bank": _items(stored.get("bank"))},
    }


def load(text: str) -> dict:
    """TouchDashboard.lua → {chars: {key: …}, warband: {gold, items}}."""
    try:
        db = table(parse_saved_variables(text).get("TouchDashboardDB"))
    except LuaError:
        return {"chars": {}, "warband": None}
    chars = {}
    for key, c in list(table(db.get("chars")).items())[:CHARS_MAX]:
        # Not one saved before the game knew the realm ("Name-": the companion before 2.3.0)
        if isinstance(key, str) and isinstance(c, dict) and not key.endswith("-"):
            chars[key] = _char(key, c)
    wb = table(db.get("warband"))
    warband = {"gold": int(_num(wb.get("gold")) or 0) // 10000, "items": _items(wb.get("items"))} if wb else None
    return {"chars": chars, "warband": warband}


def _live_key(snap: dict) -> str | None:
    c = table(snap.get("char"))
    return f"{c['name']}-{c['realm']}" if isinstance(c.get("name"), str) and isinstance(c.get("realm"), str) else None


def _buffs_hidden(snap: dict) -> bool:
    """The game keeps auras from addons now, as the companion says (Blizzard's
    C_Secrets, since 2.6.0: combat, an encounter, a key, a PvP match); from an
    older one, guessed: in combat, chat lockdown or a Mythic+ key."""
    said = snap.get("aura_secret")
    if isinstance(said, bool):
        return said
    return snap.get("combat") is True or snap.get("lockdown") is True or table(snap.get("mplus")).get("state") == 1


def _checks(snap: dict, cls: str | None) -> list[dict]:
    """The pre-pull checklist from the live buffs: what is yours to see to
    (consumables, weapon oil, your own class's raid buff) always; another
    class's raid buff when it is on you, or in a group with that class in it
    (any class, from an addon that does not say which are there). While the
    game hides auras a buff not seen is `hidden`, not missing; weapon oil is
    not an aura and always shows."""
    hidden = _buffs_hidden(snap)
    buffs = [table(b) for b in items(snap.get("buffs"))]
    by_id = {_num(b.get("id")): b for b in buffs}
    group = table(snap.get("group"))
    grouped = (_num(group.get("n")) or 0) > 1
    here = {c for c in items(group.get("classes")) if isinstance(c, str)} if "classes" in group else None
    out = []
    for bid, name, ids, caster, cast in RAID_BUFFS:
        hit = next((by_id[i] for i in ids if i in by_id), None) \
            or next((b for b in buffs if b.get("name") == name), None)
        if hit is None and cls != caster and not (grouped and (here is None or caster in here)):
            continue                               # no one here to cast it: not missing
        out.append({"id": bid, "name": name, "ok": hit is not None, "rem": _num(hit.get("rem")) if hit else None,
                    "cast": cast if cls == caster and not hidden else None, "kind": "raid",
                    "hidden": hit is None and hidden})
    for cid, name, words, ids in CONSUMABLES:
        hit = next((by_id[i] for i in ids if i in by_id), None) or next(
            (b for b in buffs if isinstance(b.get("name"), str) and any(w in b["name"] for w in words)), None)
        out.append({"id": cid, "name": name, "ok": hit is not None, "rem": _num(hit.get("rem")) if hit else None,
                    "label": _text(hit.get("name")) if hit else None, "kind": "consumable",
                    "hidden": hit is None and hidden})
    weapon = table(snap.get("weapon"))
    mh = _num(weapon.get("mh"))
    out.append({"id": "weapon", "name": "Weapon oil", "ok": bool(mh), "rem": mh, "kind": "consumable"})
    return out


def _ready_check(raw: Any) -> dict | None:
    """The last ready check: running (rem: seconds left) or over (rem: how long ago, negative)."""
    rc = table(raw)
    state = _num(rc.get("state"))
    if state not in (1, 2):
        return None
    missing = [{"name": _text(table(m).get("name"), 24), "a": _num(table(m).get("a"))}
               for m in items(rc.get("missing"))[:8] if _text(table(m).get("name"))]
    return {"state": "running" if state == 1 else "over", "rem": _num(rc.get("rem")),
            "total": _num(rc.get("total")) or 0, "ready": _num(rc.get("ready")) or 0, "missing": missing}


def _alerts(raw: Any) -> dict | None:
    """NSRT's alerts still to come in this phase, next first."""
    a = table(raw)
    phase = _num(a.get("phase"))
    if not phase:
        return None
    upcoming = [{"text": _text(table(x).get("text"), 80), "rem": _num(table(x).get("rem")),
                 "spell": _num(table(x).get("spell"))} for x in items(a.get("list"))[:ALERTS_MAX]]
    return {"phase": None if phase == 255 else phase, "test": phase == 255, "enc": _text(a.get("enc"), 48),
            "list": [x for x in upcoming if x["text"] and x["rem"] is not None]}


def _boss(name: Any) -> str | None:
    """A boss as the key's criteria name it, less the game's " defeated" (2.5.0 sent it, cut at 24 bytes)."""
    text = _text(name, 48)
    if not text:
        return None
    cut = _DEFEATED_CUT if len(text.encode()) == 24 else _DEFEATED
    return cut.sub("", text) or text


def _mplus(raw: Any) -> dict | None:
    """The Mythic+ run: in a key (elapsed counts on from here once the timer
    runs) or over (its result), with the times its chests need."""
    m = table(raw)
    state = _num(m.get("state"))
    if state not in (1, 2):
        return None
    limit = _num(m.get("limit")) or 0
    elapsed = _num(m.get("elapsed")) or 0
    bosses = [{"name": _boss(table(b).get("name")), "done": table(b).get("done") is True,
               "at": _num(table(b).get("at"))} for b in items(m.get("bosses"))[:8] if _boss(table(b).get("name"))]
    return {"state": "running" if state == 1 else "over", "name": _text(m.get("name"), 64),
            "level": _num(m.get("level")) or 0, "limit": limit, "elapsed": elapsed,
            "counting": state == 1 and elapsed > 0, "chests": [round(limit * f) for f in CHESTS] if limit else [],
            "onTime": m.get("onTime") is True if state == 2 else None,
            "upgrades": (_num(m.get("upgrades")) or 0) if state == 2 else None,
            "deaths": _num(m.get("deaths")) or 0, "lost": _num(m.get("lost")) or 0,
            "forces": min(100, max(0, _num(m.get("forces")) or 0)), "bosses": bosses}


def view(ctx: Context) -> dict:
    live = ctx.game()
    saved = ctx.data or {"chars": {}, "warband": None}
    snap = live["data"] if live else {}
    current = _live_key(snap) if live else None
    chars = []
    for key, c in saved["chars"].items():
        chars.append({k: v for k, v in c.items() if k != "items"} | {"current": key == current, "live": False})
    if live and current:
        c = table(snap.get("char"))
        entry = next((x for x in chars if x["key"] == current), None)
        if entry is None:
            entry = {"key": current, "name": c.get("name"), "realm": c.get("realm"), "vault": {}, "current": True}
            chars.append(entry)
        bags, dur, ks = table(snap.get("bags")), table(snap.get("dur")), table(snap.get("key"))
        entry.update(live=True, current=True, cls=None, level=_num(c.get("level")), ilvl=_num(c.get("ilvl")),
                     gold=int(_num(snap.get("gold")) or 0) // 10000,
                     bags={"free": _num(bags.get("free")), "total": _num(bags.get("total"))} if bags else None,
                     dur=_num(dur.get("low")),
                     keystone={"level": _num(ks.get("level")), "name": _text(ks.get("name"))} if ks else None)
        entry["class"] = _text(c.get("class"), 16)
        entry.pop("cls", None)
        if snap.get("vault") is not None:
            entry["vault"] = _vault(snap.get("vault"))
    chars.sort(key=lambda x: (not x.get("current"), -(x.get("level") or 0), -(x.get("ilvl") or 0), x.get("name") or ""))
    warband = saved.get("warband")
    out: dict[str, Any] = {
        "companion": ctx.companion(),
        "live": None,
        "chars": chars,
        "totals": {"gold": sum(c.get("gold") or 0 for c in chars) + ((warband or {}).get("gold") or 0),
                   "warband": (warband or {}).get("gold"), "chars": len(chars)},
    }
    if live:
        c = table(snap.get("char"))
        dur = table(snap.get("dur"))
        out["live"] = {
            "age": round(live["age"]), "link": bool(live.get("link")),
            "combat": snap.get("combat") is True, "lockdown": snap.get("lockdown") is True,
            "pull_hidden": snap.get("pull_hidden") is True,
            "name": _text(c.get("name"), 48), "class": _text(c.get("class"), 16), "spec": _text(c.get("spec"), 32),
            "zone": _text(snap.get("zone")), "gold": int(_num(snap.get("gold")) or 0) // 10000,
            "bags": next((x["bags"] for x in chars if x.get("current")), None),
            "dur": {"low": _num(dur.get("low")), "broken": _num(dur.get("broken"))} if dur else None,
            "buffs": [{"id": _num(b.get("id")), "name": _text(b.get("name")), "rem": _num(b.get("rem"))}
                      for b in (table(x) for x in items(snap.get("buffs"))[:BUFFS_MAX])],
            "checks": _checks(snap, _text(c.get("class"), 16)),
            "bars": [{"label": _text(table(b).get("label")), "rem": _num(table(b).get("rem")),
                      "dur": _num(table(b).get("dur"))}
                     for b in items(snap.get("bars"))[:20] if _text(table(b).get("label"))],
            "version": _text(snap.get("ver"), 16),
            "rc": _ready_check(snap.get("rc")),
            "alerts": _alerts(snap.get("alerts")),
            "mplus": _mplus(snap.get("mplus")),
        }
    return out


def _find(ctx: Context, args: dict) -> None:
    """Search every character's bags and bank, the warband bank and the live bags."""
    q = (args.get("q") or "").lower()
    saved = ctx.data or {"chars": {}, "warband": None}
    live = ctx.game()
    snap = live["data"] if live else {}
    current = _live_key(snap) if live else None
    found: dict[int, dict] = {}

    def add(entries, who, where):
        for e in entries:
            if q and q not in (e.get("name") or "").lower():
                continue
            hit = found.setdefault(e["id"], {"id": e["id"], "name": e.get("name"), "q": e.get("q", 0), "n": 0,
                                             "where": []})
            hit["n"] += e["n"]
            hit["where"].append({"who": who, "loc": where, "n": e["n"]})

    live_bags = _items(snap.get("items")) if snap.get("items") is not None else None
    for key, c in saved["chars"].items():
        bags = live_bags if key == current and live_bags is not None else c["items"]["bags"]
        if q or key == current:
            add(bags, c["name"], "bags")
        if q:
            add(c["items"]["bank"], c["name"], "bank")
    if current and current not in saved["chars"] and live_bags is not None:
        add(live_bags, table(snap.get("char")).get("name") or "", "bags")
    if q and saved.get("warband"):
        add(saved["warband"]["items"], "Warband", "warband bank")
    hits = sorted(found.values(), key=lambda h: (-(h["q"] or 0), -h["n"], h["name"] or ""))
    ctx.reply({"q": args.get("q") or "", "items": hits[:FIND_MAX], "more": max(0, len(hits) - FIND_MAX)})


def _sync(ctx: Context, args: dict) -> None:
    ctx.sync(full=True)


MODULE = AddonModule(
    id="game",
    name="Game state",
    description="Buffs, gold, bags, keystone and vault from the Touch Dashboard addon, for every character",
    order=3,
    commands=(
        Command("game_sync", "Read the game", run=_sync, glyph="refresh-ccw", key="tools",
                description="Reads your buffs, gold, bags and keystone from the game now (WoW must be in front)"),
        Command("game_find", "Find an item", args={"q": Arg("text", max_len=40, optional=True, label="Item")},
                run=_find),
    ),
    watch=Watch(SV_FILE, load),
    view=view,
    snapshot=True,
    addon="TouchDashboard",
    widgets=(
        Widget("buffs", "Buff check", "Raid buffs, flask, food, rune and weapon oil, from the game",
               sizes=("full", "half"), column=0, order=20),
        Widget("wallet", "Gold", "Gold on this character, on all of them and in the warband bank",
               sizes=("half", "full"), column=2, order=40),
        Widget("bags", "Bags", "Free slots and durability, and a search through every character's bags",
               sizes=("half", "full")),
        Widget("keystone", "Keystone & vault", "Your keystone and this week's Great Vault, for each character",
               sizes=("half", "full")),
        Widget("alts", "Characters", "Every character the addon has seen: level, item level, gold, keystone",
               grow=True),
    ),
)
