"""
Northern Sky Raid Tools (NSRT): the live note viewer and NSRT's commands.

Notes are WoWUtils strings, one entry per line, fields separated by ";":

    EncounterID:3306;Difficulty:Mythic;Name:BossName;
    time:13;ph:1;tag:Magicbane;spellid:101643;text:Personal Reminder

A line with time is a timed reminder: personal when its tag names the
player's character or role tag (Settings), shared otherwise. Lines without a
time are notes. Everything after "text:" is the text, semicolons included.

The notes come from NSRT's SavedVariables (written by WoW on /reload, logout
and exit) or from the PC's clipboard ("Paste note"). NSRT's file layout is not
documented, so every string in it that reads as a note is offered; the one
under a key naming the active note is shown first, and the page can switch.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from wow_addons import AddonModule, Arg, CommandError, Command, Context, Watch, chat_text

NOTE_FILE = "NorthernSkyRaidTools.lua"
MAX_ENTRIES = 400          # per list
TEXT_MAX = 240
PASTED_ID = "pasted"

# ["key"] = "value", key = "value", [1] = "value", or a bare "value" (array part).
_ENTRY = re.compile(
    r'(?:\[\s*"(?P<qkey>(?:[^"\\\n]|\\.)*)"\s*\]\s*=\s*|(?P<key>\b[A-Za-z_]\w*)\s*=\s*)?'
    r'"(?P<val>(?:[^"\\\n]|\\.)*)"')
_ESCAPE = re.compile(r'\\(\d{1,3}|x[0-9a-fA-F]{2}|z\s*|.)', re.S)
_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
           "\\": "\\", '"': '"', "'": "'", "\n": "\n"}
_ACTIVE_KEY = re.compile(r"active|current|selected|loaded", re.I)
_TAG_SPLIT = re.compile(r"[\s,]+")
# WoW's inline codes: colours, textures, atlases, links, line breaks.
_WOW_CODES = re.compile(r"\|c[0-9a-fA-F]{8}|\|r|\|T[^|]*\|t|\|A[^|]*\|a|\|H[^|]*\|h|\|h|\|n")
FIELDS = frozenset({"encounterid", "difficulty", "name", "time", "ph", "phase", "tag", "spellid", "text"})


def lua_unescape(raw: str) -> str:
    """A Lua string literal's body (the file read as latin-1, so one char per
    byte) as text: escapes resolved to bytes, then decoded as UTF-8."""
    def sub(m: re.Match) -> str:
        e = m.group(1)
        if e[0].isdigit():
            return chr(min(int(e), 255))
        if e[0] == "x":
            return chr(int(e[1:], 16))
        if e[0] == "z":
            return ""
        return _SIMPLE.get(e, e)
    return _ESCAPE.sub(sub, raw).encode("latin-1", errors="replace").decode("utf-8", errors="replace")


def _text(value: str | None, max_len: int) -> str:
    return chat_text(_WOW_CODES.sub("", value or ""), max_len)


def _fields(line: str) -> dict[str, str]:
    """key:value pairs of one line; keys lower case. text: takes the rest. A
    line naming none of the note's fields ("Note: use a defensive") is text."""
    out: dict[str, str] = {}
    rest = line
    while rest:
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9_]*)\s*:", rest)
        if not m:
            break
        key = m.group(1).lower()
        rest = rest[m.end():]
        if key == "text":
            out[key] = rest.strip()
            break
        value, _, rest = rest.partition(";")
        out[key] = value.strip()
    return out if FIELDS & set(out) else {}


def _seconds(value: str) -> int | None:
    value = value.strip()
    m = re.fullmatch(r"(\d{1,3}):(\d{2})", value)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"\d{1,5}(?:\.\d+)?", value)
    return int(float(value)) if m else None


def mmss(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _int(value: str | None) -> int | None:
    return int(value) if value and re.fullmatch(r"\d{1,9}", value.strip()) else None


def parse_note(text: str) -> dict | None:
    """A note string → {encounterName, encounterId, difficulty, timed, notes};
    None when no line reads as a WoWUtils entry (so a password on the
    clipboard never shows as a note)."""
    if not isinstance(text, str):
        return None
    head: dict[str, Any] = {"encounterName": None, "encounterId": None, "difficulty": None}
    timed: list[dict] = []
    notes: list[dict] = []
    structured = False
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        f = _fields(line)
        if "encounterid" in f or ("name" in f and "time" not in f and "text" not in f):
            structured = True
            head["encounterId"] = _int(f.get("encounterid")) or head["encounterId"]
            head["encounterName"] = _text(f.get("name"), 64) or head["encounterName"]
            head["difficulty"] = _text(f.get("difficulty"), 24) or head["difficulty"]
        t = _seconds(f["time"]) if "time" in f else None
        if t is not None:
            structured = True
            tags = [x for x in _TAG_SPLIT.split(f.get("tag", "")) if x][:16]
            timed.append({"t": t, "time": mmss(t), "phase": _int(f.get("ph") or f.get("phase")),
                          "spell": _int(f.get("spellid")), "tags": [chat_text(x, 32) for x in tags],
                          "text": _text(f.get("text"), TEXT_MAX)})
        elif "encounterid" not in f and not ("name" in f and "text" not in f):
            body = f["text"] if "text" in f else (line if not f else "")
            body = _text(body, TEXT_MAX)
            if body:
                notes.append({"text": body})
    if not structured:
        return None
    timed.sort(key=lambda e: (e["phase"] or 0, e["t"]))
    return {**head, "timed": timed[:MAX_ENTRIES * 2], "notes": notes[:MAX_ENTRIES]}


def classify(parsed: dict, character: str, role: str) -> dict:
    """Timed entries tagged for the character or role are personal, the other
    timed ones shared; untimed lines are notes."""
    mine = {x.lower() for x in (character, role) if x}
    personal, shared = [], []
    for e in parsed["timed"]:
        (personal if mine & {t.lower() for t in e["tags"]} else shared).append(e)
    return {"personal": personal[:MAX_ENTRIES], "shared": shared[:MAX_ENTRIES], "notes": parsed["notes"]}


def _note_id(key: str | None, index: int) -> str:
    return "n" + hashlib.sha1(f"{key}\0{'' if key else index}".encode()).hexdigest()[:10]


def load_saved_variables(text: str) -> dict:
    """Every note-like string in the file, in file order, and the id of the one
    a key naming the active note points at (None if there is none)."""
    notes: list[dict] = []
    keyed: list[tuple[str, str]] = []
    for i, m in enumerate(_ENTRY.finditer(text)):
        key = m.group("qkey") or m.group("key")
        key = lua_unescape(key) if key else None
        value = lua_unescape(m.group("val"))
        if key:
            keyed.append((key, value))
        parsed = parse_note(value) if ":" in value else None
        if parsed:
            name = parsed["encounterName"] or chat_text(key or "", 48) or f"Note {len(notes) + 1}"
            notes.append({"id": _note_id(key, i), "key": key, "name": name, "parsed": parsed})
    active = None
    by_key = {n["key"]: n["id"] for n in notes if n["key"]}
    for key, value in keyed:
        if _ACTIVE_KEY.search(key):
            active = by_key.get(value) or next((n["id"] for n in notes if n["key"] == key), None)
            if active:
                break
    if active is None and notes:
        active = notes[-1]["id"]
    return {"notes": notes, "active": active}


def _choices(ctx: Context) -> tuple[list[dict], str | None]:
    """[(id, name, parsed, source)] in the order offered, and the file's active id."""
    out = []
    pasted = ctx.state.get("pasted")
    if pasted:
        out.append({"id": PASTED_ID, "name": pasted["name"], "parsed": pasted["parsed"], "source": "paste"})
    data = ctx.data or {"notes": [], "active": None}
    for n in data["notes"]:
        out.append({"id": n["id"], "name": n["name"], "parsed": n["parsed"], "source": "file"})
    return out, data["active"]


def view(ctx: Context) -> dict:
    choices, file_active = _choices(ctx)
    pick = ctx.state.get("pick")
    shown = (next((c for c in choices if c["id"] == pick), None)
             or next((c for c in choices if c["id"] == file_active), None)
             or (choices[0] if choices else None))
    general = ctx.general
    out: dict[str, Any] = {
        "encounterName": None, "encounterId": None, "difficulty": None,
        "personal": [], "shared": [], "notes": [],
        "choices": [{"id": c["id"], "name": c["name"], "source": c["source"]} for c in choices],
        "active": shown["id"] if shown else None, "source": shown["source"] if shown else None,
        "file": ctx.data is not None, "filter": general,
    }
    if shown:
        p = shown["parsed"]
        lists = classify(p, general["character"], general["role"])
        for cat in ("personal", "shared"):
            lists[cat] = [_named(ctx, e) for e in lists[cat]]
        out.update(encounterName=p["encounterName"] or shown["name"], encounterId=p["encounterId"],
                   difficulty=p["difficulty"], **lists)
    return out


def _named(ctx: Context, entry: dict) -> dict:
    """The entry with its spell's name and icon once Battle.net has them (an API
    key in Settings → Battle.net); the parsed note itself stays as it was."""
    info = ctx.spell(entry["spell"]) if entry["spell"] else None
    return {**entry, "spellName": info["name"], "icon": info["icon"]} if info else entry


def _paste(ctx: Context, args: dict) -> None:
    text = args.get("text") or ctx.clipboard_text()
    parsed = parse_note(text or "")
    if not parsed:
        raise CommandError("empty")   # nothing on the clipboard reads as a note
    ctx.update(pasted={"name": parsed["encounterName"] or "Pasted note", "parsed": parsed}, pick=PASTED_ID)


def _pick(ctx: Context, args: dict) -> None:
    ids = {c["id"] for c in _choices(ctx)[0]}
    if args["id"] not in ids:
        raise CommandError("invalid")
    ctx.update(pick=args["id"])


MODULE = AddonModule(
    id="nsrt",
    name="Northern Sky Raid Tools",
    description="Live reminders from your NSRT note, and NSRT's tools",
    order=10,
    commands=(
        Command("open_nsrt", "NSRT hub", ("/ns",)),
        Command("nkm_refresh", "Sync kick macro", ("/nkm refresh",)),
        Command("nsrt_test", "Test alerts", ("/ns test",), verified=False),
        Command("nsrt_arrange", "Group split", ("/ns arrange",), verified=False),
        Command("cz_check", "Captain Zeynith check", ("/cz check",)),
        Command("cz_announce", "Captain Zeynith announce", ("/cz announce",)),
        Command("nsrt_paste", "Paste note", args={"text": Arg("note", optional=True)}, run=_paste),
        Command("nsrt_pick", "Show note", args={"id": Arg("id")}, run=_pick),
    ),
    state={"pasted": None, "pick": None},
    # The page gets the view, not the pasted note itself.
    public=lambda s, now: {"pick": s.get("pick"), "pasted": bool(s.get("pasted"))},
    watch=Watch(NOTE_FILE, load_saved_variables),
    view=view,
)
