"""
wow_addons — World of Warcraft addon integrations (the WoW plugin page).

Each addon is a module in wow_addons/modules/: a file defining MODULE, an
AddonModule with its commands, widgets (the plates the page can show), default
keys, its own settings and shared state, and optionally a SavedVariables file
to watch and a view of it for the page. The core never names an addon: a new
one is a file there plus its page side, static/wow/modules/<id>.js. A module
that fails to import is skipped and the others keep working.

How a command reaches the game. WoW lets no addon talk to anything outside it,
so the PC presses keys, as a player would; WoW must be the window in front.
Three ways, best first:

1. A key slot. Ctrl+Alt+Shift+F1-F12 run a macro bound to them in game, so a
   key with a slot is one key press: instant, and it works in combat and in
   chat lockdown. The macro is the player's own, or with the companion addon
   (addon/TouchDashboard) one of its secure buttons holding the slot's text,
   which the PC loads into it (out of combat: WoW refuses in combat).
2. The companion addon's input box. Ctrl+Alt+Shift+Home focuses a hidden box;
   the PC reads the game's state from it (Ctrl+C), pastes the command and
   presses Enter; the addon runs the addon's slash command itself. Nothing
   pops up in chat. Blizzard's own (/cast, /wm, chat, /readycheck...) an addon
   may not run: out of combat the box loads them into its run button instead,
   and the PC presses Ctrl+Alt+Shift+End, a real key press that clicks it.
3. Chat: the text on the clipboard, then Enter, Ctrl+V, Enter. Always works
   out of chat lockdown; the chat box opens for a moment. Blizzard's commands
   come here only in combat (the run button cannot change then), without the
   companion, or when WoW's window cannot be seen.

The clipboard's own text is put back afterwards, every time.

What comes back: only what the companion puts in its box when asked (gold,
bags, durability, buffs, keystone, boss mod bars... nothing that is secret in
combat, see Midnight's addon rules) and what WoW writes to SavedVariables at
/reload, logout and exit. Nothing here writes SavedVariables: WoW reads them
only at login and overwrites them on logout.
"""
from __future__ import annotations

import copy
import glob
import importlib
import json
import logging
import math
import os
import pkgutil
import re
import secrets
import shutil
import threading
import time
import unicodedata
import urllib.parse
import zlib
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable

from . import clipboard as _clipboard
from . import link as _link
from .screen import NoScreen
from .armory import REGIONS

logger = logging.getLogger(__name__)

MODULE_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
COMMAND_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
CUSTOM_ID_RE = re.compile(r"c_[a-z0-9_]{1,40}")
ITEM_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
ACCOUNT_RE = re.compile(r"[A-Za-z0-9#_. -]{0,64}")
GLYPH_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
VERSION_RE = re.compile(r"^##\s*Version:\s*([0-9][0-9A-Za-z.\-]{0,15})\s*$", re.M)

SLOTS = tuple(f"F{n}" for n in range(1, 13))
SLOT_MODIFIERS = ("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTSHIFT")
# The companion's input box. Not Del/PgUp/PgDn: with Ctrl+Alt+Shift, KDE logs
# out, reboots or halts without asking.
FOCUS_KEYS = ("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTSHIFT", "KEY_HOME")
RUN_KEYS = ("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTSHIFT", "KEY_END")   # the companion's run button
STEP_S = 0.08                    # between keys: WoW reads input once a frame
COPY_WAIT_S = 0.35               # WoW's clipboard reaches the desktop's (through Wine) within this
BEACON_WAIT_S = 0.5              # the companion's box lights its beacon within a few frames
LINK_FRESH_S = 2.0               # the live link is down when no frame of it was read for this long
LINK_FAST_S = 0.1                # between reads of the live link while it is up
LINK_SLOW_S = 1.0                # ... while it is not (no WoW, the link off, the addon not running)
LINK_PAGE_S = 0.02               # ... while a payload's pages are still coming: the next frame (one page a frame)
LINK_REFRESH_S = 1.0             # times left recounted this often while the link's payload stays
QUIET_S = 300                    # a companion that never answered is not tried again for this long
SLASH_MAX = 255                  # WoW's chat box takes 255 characters
MACRO_MAX = 255                  # so does a macro
NAME_MAX = 48
LABEL_MAX = 24
PATH_MAX = 1024
NOTE_MAX = 64 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
SNAPSHOT_MAX = 256 * 1024
CUSTOM_MAX = 200
CUSTOM_LINES = 8
CELLS_MAX = 16                   # widgets in a column
KEYS_MAX = 48                    # keys in a keys widget
SIZES = ("half", "full")
COLUMNS = 3
KEYS_WIDGET = "keys"             # the core's own widget: a grid of keys the player fills
# Widgets that moved to another module: a saved layout keeps its plate.
WIDGET_ALIASES = {"bigwigs.timers": "raid.timers"}

COMPANION = "TouchDashboard"     # its folder in Interface/AddOns
COMPANION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon", COMPANION)
SNAPSHOT_TAG = "TDS1"
ENVELOPE_TAG = "TD1"

# First words of Blizzard's own slash commands: protected (secure) ones, chat,
# emotes and other built-ins. They are typed into chat or pressed as a slot;
# the companion runs only addons' commands. A word missing here is tried as an
# addon command once: the companion says it does not know it, and from then on
# it is typed into chat.
GAME_WORDS = frozenset("""
cast spell use castrandom userandom castsequence stopcasting stopspelltarget cancelaura cancelform
cancelqueuedspell equip equipslot equipset changeactionbar swapactionbar target tar targetexact
targetenemy targetenemyplayer targetfriend targetfriendplayer targetparty targetraid cleartarget
targetlasttarget targetlastenemy targetlastfriend assist focus clearfocus maintank mt maintankoff
mtoff mainassist ma mainassistoff maoff duel forfeit yield petattack petfollow petmoveto petstay
petpassive petdefensive petaggressive petassist petautocaston petautocastoff petautocasttoggle
startattack stopattack stopmacro click worldmarker wm clearworldmarker cwm targetmarker tm
leavevehicle dismount ping follow f randompet randomfavoritepet summonpet dismisspet
s say y yell sh shout p party raid ra rsay rw raidwarning i instance g guild o officer e em me emote
w whisper t tell send r reply bn
reload reloadui console readycheck rolecheck countdown roll random rnd combatlog chatlog afk dnd
away busy logout camp quit exit invite inv uninvite kick u un votekick promote pr leave leaveparty
who whois friends ignore unignore join channel chatlist stopwatch timetest framestack fstack
eventtrace etrace dump run script macro help played calendar pvp lfg lfd lfr raidinfo
resetinstances inspect ins trade t trade timeplayed guildroster groster gquit ginvite guildinvite
dance wave cheer bow thank thanks hello hi bye goodbye lol laugh cry roar flex kneel sit stand sleep
applaud clap salute point kiss hug no yes nod charge oom train violin threaten taunt rude
""".split())


class CommandError(Exception):
    """A command that did not run. `reason` goes back to the screen in wow_cmd_ack:
    unknown, disabled, invalid, no_keyboard, no_clipboard, empty, failed,
    no_answer (the companion did not answer), in_combat (slots wait for it)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def chat_text(value: Any, max_len: int = NAME_MAX) -> str:
    """One line a player could type: no control characters, quotes, backslashes
    or WoW's | escapes (colours, links), whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value)
    text = "".join(" " if c.isspace() else c for c in text if c.isspace() or unicodedata.category(c)[0] != "C")
    text = text.replace('"', "").replace("\\", "").replace("|", "")
    return " ".join(text.split())[:max_len]


def clean_int(value: Any, lo: int, hi: int, default: int) -> int:
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,9}\s*", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return max(lo, min(hi, int(round(value))))


def _pct(text: str) -> str:
    """Percent-encode what would break the companion's line: %, |, ;, =, line breaks, controls."""
    return urllib.parse.quote(text, safe=" /!#$&'()*+,-.:<>?@[]^_`{}~\"")


def adler32(text: str) -> int:
    """The checksum the companion computes over the slot text it holds."""
    return zlib.adler32(text.encode("utf-8")) & 0xFFFFFFFF


@dataclass(frozen=True)
class Arg:
    """One argument of a command, checked on the server whatever the page sent.

    kind: int (clamped to lo..hi), text (one chat-safe line, max_len), bool,
    choice (one of `choices`, or of the list in the module setting `setting`),
    id (an item id) or note (multi-line text, never typed into chat).
    A missing value takes the module setting `setting` (int, text), then
    `default`; with neither, the command is invalid unless `optional`.
    """
    kind: str
    lo: int = 0
    hi: int = 0
    max_len: int = 40
    setting: str = ""
    default: Any = None
    choices: tuple[str, ...] = ()
    optional: bool = False
    label: str = ""                # how the key editor names it

    def clean(self, value: Any, settings: dict) -> Any:
        if value is None or value == "":
            if self.setting and self.kind in ("int", "text"):
                value = settings.get(self.setting)
            if value is None or value == "":
                value = self.default
            if value is None:
                if self.optional:
                    return None
                raise CommandError("invalid")
        if self.kind == "int":
            if isinstance(value, bool):
                raise CommandError("invalid")
            n = clean_int(value, self.lo, self.hi, -1)
            if n < 0:
                raise CommandError("invalid")
            return n
        if self.kind == "text":
            text = chat_text(value, self.max_len)
            if not text:
                if self.default is None:
                    raise CommandError("invalid")
                return self.default
            return text
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise CommandError("invalid")
            return value
        if self.kind == "choice":
            allowed = self.choices or tuple(settings.get(self.setting) or ())
            if not isinstance(value, str) or value not in allowed:
                raise CommandError("invalid")
            return value
        if self.kind == "id":
            if not isinstance(value, str) or not ITEM_ID_RE.fullmatch(value):
                raise CommandError("invalid")
            return value
        if self.kind == "note":
            if not isinstance(value, str) or len(value) > NOTE_MAX:
                raise CommandError("invalid")
            return value.replace("\r\n", "\n").replace("\r", "\n")
        raise CommandError("invalid")

    def describe(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind}
        if self.kind == "int":
            out.update(lo=self.lo, hi=self.hi)
        if self.kind == "text":
            out["max_len"] = self.max_len
        if self.setting:
            out["setting"] = self.setting
        if self.choices:
            out["choices"] = list(self.choices)
        if self.label:
            out["label"] = self.label
        if self.default is not None and not isinstance(self.default, bool):
            out["default"] = self.default
        return out


@dataclass(frozen=True)
class Command:
    """Something a screen can ask for. Without `run`, it types its chat lines
    (or presses its slot); with `run`, that does its work and may call
    ctx.send() to type them. Commands without chat lines change state only."""
    id: str
    label: str
    slash: tuple[str, ...] = ()
    args: dict[str, Arg] = field(default_factory=dict)
    verified: bool = True          # False: our guess at the addon's command; the player can change it
    run: Callable[["Context", dict], None] | None = None
    glyph: str = ""                # its key's lucide glyph
    category: str = ""             # where the key editor lists it ("": its module's name)
    via: str = ""                  # "game" (Blizzard's own), "addon", or "": from its first word
    description: str = ""
    key: str = ""                  # the KeyGroup it has a key in on the first layout
    addon: str = ""                # the addon it needs, its Interface/AddOns folder ("": its module's)

    @property
    def bindable(self) -> bool:
        """Can have a key slot: it types text (a slot's macro holds it fixed)."""
        return bool(self.slash)


@dataclass(frozen=True)
class Widget:
    """A plate the page can place: `half` widgets share a row two by two,
    `full` ones take the column's width; `grow` ones split its spare height."""
    id: str
    name: str
    description: str = ""
    sizes: tuple[str, ...] = ("full",)
    grow: bool = False
    column: int = -1               # on the first layout: 0, 1 or 2 (-1: only from the picker)
    order: int = 100


@dataclass(frozen=True)
class KeyGroup:
    """A keys widget on the first layout, filled with the commands naming it."""
    id: str
    title: str
    column: int = 1
    order: int = 100
    size: str = "full"


@dataclass(frozen=True)
class KeyTemplate:
    """A keys plate to start from: the page offers it when one is added, and
    the player edits it from there. Keys as a keys plate holds them: {cmd,
    args?, label?, glyph?}; those whose command is not known are left out."""
    id: str
    title: str
    description: str = ""
    glyph: str = "layout-grid"
    keys: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Watch:
    """A SavedVariables file, read again whenever it changes (WoW writes it on
    /reload, logout and exit)."""
    filename: str
    load: Callable[[str], Any]


@dataclass(frozen=True)
class AddonModule:
    id: str
    name: str
    description: str = ""
    commands: tuple[Command, ...] = ()
    defaults: dict = field(default_factory=dict)            # its settings, wow_addons[<id>]
    clean: Callable[[dict], dict] | None = None             # raw → valid settings, defaults filled in
    private: frozenset[str] = frozenset()                   # settings paired devices never get
    state: dict = field(default_factory=dict)               # shared by every screen (a timer, a toggle)
    public: Callable[[dict, float], dict] | None = None     # state → what screens get (now: monotonic)
    watch: Watch | None = None
    view: Callable[["Context"], dict | None] | None = None  # sent as wow_<id>_update
    order: int = 100
    widgets: tuple[Widget, ...] = ()
    key_groups: tuple[KeyGroup, ...] = ()
    templates: tuple[KeyTemplate, ...] = ()                 # keys plates to start from
    addon: str = ""                                         # its folder in Interface/AddOns ("": none)
    snapshot: bool = False                                  # its view reads the companion's snapshot


CUSTOM = AddonModule(id="custom", name="Custom commands",
                     description="Your own slash commands, for addons this page does not know")


def discover() -> list[AddonModule]:
    """Every module in wow_addons/modules/ that defines MODULE."""
    from . import modules as package
    found: list[AddonModule] = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{package.__name__}.{info.name}")
        except Exception:   # one broken addon file must not take the others down
            logger.exception("WoW addon module %s failed to load", info.name)
            continue
        module = getattr(mod, "MODULE", None)
        if isinstance(module, AddonModule) and MODULE_ID_RE.fullmatch(module.id):
            found.append(module)
        else:
            logger.warning("WoW addon module %s defines no valid MODULE", info.name)
    return found


def _default_clean(defaults: dict) -> Callable[[dict], dict]:
    """Keep a value only where it has its default's type."""
    def clean(raw: dict) -> dict:
        out = {}
        for key, default in defaults.items():
            value = raw.get(key, default)
            same = isinstance(value, type(default)) and isinstance(value, bool) == isinstance(default, bool)
            out[key] = value if same else default
        return out
    return clean


def _version(v: Any) -> tuple[int, ...]:
    """A companion version for comparing: "2.10.0" after "2.9.1"; anything else is (0,)."""
    parts = re.findall(r"\d+", v) if isinstance(v, str) else []
    return tuple(int(p) for p in parts[:4]) or (0,)


def bundled_version() -> str | None:
    """The companion addon's version in this install (addon/TouchDashboard)."""
    try:
        with open(os.path.join(COMPANION_DIR, f"{COMPANION}.toc"), encoding="utf-8") as f:
            m = VERSION_RE.search(f.read(4096))
        return m.group(1) if m else None
    except OSError:
        return None


class Context:
    """What a command's `run` and a module's `view` may use."""

    def __init__(self, core: "WowAddons", module: AddonModule, command: Command | None = None,
                 args: dict | None = None):
        self._core = core
        self.module = module
        self.command = command
        self.args = args or {}
        self.reply_data: Any = None

    @property
    def settings(self) -> dict:
        return self._core.module_settings(self.module.id)

    @property
    def general(self) -> dict:
        """The player: character name and role tag."""
        s = self._core.settings()
        return {"character": s["character"], "role": s["role"]}

    @property
    def armory(self):
        """Raider.IO and Battle.net (wow_addons.armory.Armory), or None."""
        return self._core.armory

    def who(self) -> tuple[str, str, str] | None:
        """(region, realm, character) when all three are set."""
        return self._core.who()

    def region(self) -> str:
        return self._core.settings()["region"]

    def spell(self, spell_id) -> dict | None:
        """{name, icon} of a spell once Battle.net has named it (needs an API key)."""
        return self._core.armory.spell(spell_id) if self._core.armory else None

    def set_player(self, character: str, realm: str, region: str) -> None:
        """Switch the player's character; the server saves it (take_dirty)."""
        self._core.set_player(character, realm, region)

    @property
    def state(self) -> dict:
        return self._core.module_state(self.module.id)

    @property
    def data(self) -> Any:
        """What the module's watch loaded from its file (None: no file)."""
        return self._core.watch_data(self.module.id)

    def game(self) -> dict | None:
        """The companion's last snapshot of the game: {data, age (s)}, or None."""
        return self._core.snapshot()

    def companion(self) -> dict:
        return self._core.companion_status()

    def sync(self, full: bool = False) -> None:
        """Read the game's state through the companion now (a Sync key)."""
        self._core.sync(full)

    def update(self, **changes) -> None:
        self._core.update_state(self.module.id, changes)

    def reply(self, data: Any) -> None:
        """Data for the screen that sent this command, in its wow_cmd_ack."""
        self.reply_data = data

    def now(self) -> float:
        return self._core.clock()

    def send(self, command_id: str | None = None, args: dict | None = None) -> None:
        """Type this command's chat lines (or press its slot); another command of
        the same module with `command_id`."""
        cmd = self.command if command_id is None else next(
            (c for c in self.module.commands if c.id == command_id), None)
        if cmd is None:
            raise CommandError("unknown")
        self._core.send(cmd, self.args if command_id is None else (args or {}))

    def send_as(self, slash: tuple[str, ...], via: str) -> None:
        """Type this command with other chat lines: the same command for
        another addon (BigWigs' /break or DBM's), its key slot and the
        player's own text for it still first."""
        self._core.send(dataclasses.replace(self.command, slash=slash, via=via), self.args)

    def has_addon(self, folder: str) -> bool | None:
        """Is this addon in the game's Interface/AddOns (None: no game folder to look in)."""
        return self._core.has_addon(folder)

    def clipboard_text(self) -> str | None:
        return self._core.clipboard.get_text()

    def open_url(self, url: str) -> None:
        if self._core.open_url is None:
            raise CommandError("failed")
        self._core.open_url(url)

    def launch(self, exec_cmd: str) -> None:
        if self._core.launch is None:
            raise CommandError("failed")
        self._core.launch(exec_cmd)


GENERAL_KEYS = ("modules", "character", "role", "role_auto", "region", "realm", "wow_path", "account",
                "keybinds", "slash", "companion", "custom", "layout")
ADMIN_ONLY_KEYS = frozenset({"wow_path", "account", "slash"})


class WowAddons:
    """The plugin: modules, their settings and state, commands and watched files.

    Thread-safe: the server calls run() and poll() from worker threads and the
    message builders from the event loop. Commands run one at a time, since
    they share the clipboard and the keyboard."""

    def __init__(self, modules: list[AddonModule], *, send_keys: Callable[..., None],
                 keys_ready: Callable[[], bool] = lambda: True, clipboard: Any = _clipboard,
                 open_url: Callable[[str], Any] | None = None, launch: Callable[[str], Any] | None = None,
                 armory: Any = None, companion_dir: str = COMPANION_DIR, screen: Any = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.modules: dict[str, AddonModule] = {}
        self._commands: dict[str, tuple[AddonModule, Command]] = {}
        self._widgets: dict[str, Widget] = {}
        for m in sorted(modules, key=lambda m: (m.order, m.id)):
            if m.id in self.modules or m.id in GENERAL_KEYS or m.id == CUSTOM.id:
                # Its settings live under wow_addons[<id>], beside the general ones.
                logger.warning("WoW addon module id %r is taken; the module is skipped", m.id)
                continue
            self.modules[m.id] = m
            for c in m.commands:
                if not COMMAND_ID_RE.fullmatch(c.id) or c.id in self._commands or CUSTOM_ID_RE.fullmatch(c.id):
                    logger.warning("WoW command %r of %s skipped (bad or duplicate id)", c.id, m.id)
                    continue
                self._commands[c.id] = (m, c)
            for w in m.widgets:
                if MODULE_ID_RE.fullmatch(w.id) and set(w.sizes) <= set(SIZES) and w.sizes:
                    self._widgets[f"{m.id}.{w.id}"] = w
        self.send_keys = send_keys
        self.keys_ready = keys_ready
        self.clipboard = clipboard
        # WoW's window: whether it is in front and whether the companion's box
        # is open (wow_addons/screen.py). NoScreen: neither can be told.
        self.screen = screen if screen is not None else NoScreen()
        self.open_url = open_url
        self.launch = launch
        self.armory = armory
        self.companion_dir = companion_dir
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._custom: dict[str, Command] = {}
        self._settings = self.clean_settings({})
        self._state = {mid: copy.deepcopy(m.state) for mid, m in self.modules.items()}
        self._data: dict[str, Any] = {}
        self._stamps: dict[str, tuple | None] = {}
        self._files: dict[str, str | None] = {}
        self._dirty = False
        self._touched: set[str] = set()
        self._learned: dict[str, str] = {}     # command id → "game": the companion said it is no addon's
        self._snapshot: dict | None = None
        self._snapshot_at: float | None = None
        self._slots_hash: int | None = None    # of the slot text the companion holds
        self._answered = False                 # the companion answered since the server started
        self._quiet_until = 0.0
        self._why = "no_box"                   # why the last exchange gave nothing back
        self._link = _link.Link()
        self._link_at: float | None = None     # the last frame of the live link read
        self._link_x: int | None = None        # where the strip was found along the top edge
        self._link_parsed = 0.0
        self._link_told = False                # the link's state the screens were last given
        self._addons_dir: str | None = None
        self._installed: frozenset[str] | None = None
        self._update_failed: str | None = None   # a bundled version that could not be copied in
        self._companion_version: str | None = None

    # ── settings ──────────────────────────────────────────────────────────

    def clean_settings(self, raw: Any) -> dict:
        """wow_addons as saved: every key present and valid, defaults filled in."""
        raw = raw if isinstance(raw, dict) else {}
        out: dict[str, Any] = {}
        toggles = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
        out["modules"] = {mid: toggles.get(mid) is not False for mid in self.modules}
        out["character"] = chat_text(raw.get("character"), NAME_MAX)
        out["role"] = chat_text(raw.get("role"), NAME_MAX)
        out["role_auto"] = raw.get("role_auto") is not False   # the role tag follows the character's spec
        out["region"] = raw.get("region") if raw.get("region") in REGIONS else REGIONS[0]
        out["realm"] = chat_text(raw.get("realm"), 64)
        path = raw.get("wow_path")
        out["wow_path"] = path.strip()[:PATH_MAX] if isinstance(path, str) and "\0" not in path else ""
        account = raw.get("account")
        out["account"] = (account.strip() if isinstance(account, str) and ACCOUNT_RE.fullmatch(account.strip())
                          and ".." not in account else "")
        out["companion"] = raw.get("companion") is not False   # used once it is installed

        for mid, m in self.modules.items():
            sub = raw.get(mid) if isinstance(raw.get(mid), dict) else {}
            clean = m.clean or _default_clean(m.defaults)
            out[mid] = {**copy.deepcopy(m.defaults), **clean(sub)}

        out["custom"] = self._clean_custom(raw.get("custom"))
        custom = self._custom_commands(out["custom"])
        lookup = lambda cid: self._lookup(cid, custom)  # noqa: E731

        slash: dict[str, str] = {}
        raw_slash = raw.get("slash") if isinstance(raw.get("slash"), dict) else {}
        for cmd_id, text in raw_slash.items():
            entry = self._commands.get(cmd_id) if isinstance(cmd_id, str) else None
            template = self._clean_template(entry[1], text) if entry else None
            if template:
                slash[cmd_id] = template
        out["slash"] = slash

        keybinds: dict[str, dict] = {}
        seen: set[str] = set()
        raw_keys = raw.get("keybinds") if isinstance(raw.get("keybinds"), dict) else {}
        for slot in SLOTS:
            value = raw_keys.get(slot)
            if isinstance(value, str):          # before keys had arguments: a command id
                value = {"cmd": value}
            if not isinstance(value, dict):
                continue
            entry = lookup(value.get("cmd"))
            if not entry or not entry[1].bindable:
                continue
            module, cmd = entry
            args = self._clean_args(cmd, value.get("args"), out[module.id] if module.id in self.modules else {})
            if args is None:
                continue
            text = self._macro_text(cmd, args, out[module.id] if module.id in self.modules else {}, slash)
            if not text or len(text) > MACRO_MAX or text in seen:
                continue
            seen.add(text)
            keybinds[slot] = {"cmd": cmd.id, "args": args}
        out["keybinds"] = keybinds

        out["layout"] = self._clean_layout(raw.get("layout"), lookup, out)
        return out

    @staticmethod
    def _clean_template(cmd: Command, text: Any) -> str | None:
        """A player's own text for a one-line command: a slash command naming only
        its arguments. Anything else keeps the default."""
        if not cmd.slash or len(cmd.slash) != 1 or not isinstance(text, str):
            return None
        text = text.strip()
        if not text.startswith("/") or len(text) > SLASH_MAX:
            return None
        if any(unicodedata.category(c)[0] == "C" for c in text):
            return None
        rest = PLACEHOLDER_RE.sub("", text)
        if set(PLACEHOLDER_RE.findall(text)) - set(cmd.args) or "{" in rest or "}" in rest:
            return None
        return None if text == cmd.slash[0] else text

    @staticmethod
    def _clean_custom(raw: Any) -> list[dict]:
        """The player's own commands: [{id, label, glyph, lines, via}]. Lines are
        slash commands as typed (no placeholders), at most CUSTOM_LINES."""
        out: list[dict] = []
        seen: set[str] = set()
        for c in raw[:CUSTOM_MAX] if isinstance(raw, list) else ():
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if not isinstance(cid, str) or not CUSTOM_ID_RE.fullmatch(cid) or cid in seen:
                continue
            lines = []
            for line in c.get("lines") if isinstance(c.get("lines"), list) else ():
                if not isinstance(line, str):
                    continue
                line = line.strip()
                if line.startswith("/") and len(line) <= SLASH_MAX \
                        and not any(unicodedata.category(ch)[0] == "C" for ch in line):
                    lines.append(line)
            label = chat_text(c.get("label"), LABEL_MAX) or (chat_text(lines[0], LABEL_MAX) if lines else "")
            if not lines or not label:
                continue
            seen.add(cid)
            glyph = c.get("glyph") if isinstance(c.get("glyph"), str) and GLYPH_RE.fullmatch(c["glyph"]) else ""
            via = c.get("via") if c.get("via") in ("game", "addon") else "auto"
            out.append({"id": cid, "label": label, "glyph": glyph, "lines": lines[:CUSTOM_LINES], "via": via})
        return out

    @staticmethod
    def _custom_commands(custom: list[dict]) -> dict[str, Command]:
        return {c["id"]: Command(c["id"], c["label"], tuple(c["lines"]), glyph=c["glyph"] or "terminal",
                                 category=CUSTOM.name, via="" if c["via"] == "auto" else c["via"])
                for c in custom}

    def _lookup(self, cmd_id: Any, custom: dict[str, Command] | None = None) -> tuple[AddonModule, Command] | None:
        if not isinstance(cmd_id, str):
            return None
        entry = self._commands.get(cmd_id)
        if entry:
            return entry
        cmd = (self._custom if custom is None else custom).get(cmd_id)
        return (CUSTOM, cmd) if cmd else None

    @staticmethod
    def _clean_args(cmd: Command, raw: Any, settings: dict) -> dict | None:
        """A key's arguments: the ones given, each valid for its command (None: one is not)."""
        if raw is None:
            return {}
        if not isinstance(raw, dict) or set(raw) - set(cmd.args):
            return None
        out = {}
        try:
            for name, value in raw.items():
                if value is None or value == "":
                    continue
                v = cmd.args[name].clean(value, settings)
                if v is not None:
                    out[name] = v
        except CommandError:
            return None
        return out

    def _macro_text(self, cmd: Command, args: dict, settings: dict, slash: dict | None = None) -> str:
        """What a slot's macro holds for this command: its lines, arguments filled in."""
        try:
            full = {n: a.clean(args.get(n), settings) for n, a in cmd.args.items()}
        except CommandError:
            return ""
        own = (slash if slash is not None else self.settings()["slash"]).get(cmd.id)
        return "\n".join(self._fill((own,) if own else cmd.slash, cmd, full))

    @staticmethod
    def _fill(template: tuple[str, ...], cmd: Command, args: dict) -> list[str]:
        out = []
        for line in template:
            text = PLACEHOLDER_RE.sub(lambda m: str(args.get(m.group(1), "")) if m.group(1) in cmd.args
                                      else m.group(0), line).strip()
            if text:
                out.append(text[:SLASH_MAX])
        return out

    # ── layout ────────────────────────────────────────────────────────────

    def _clean_key(self, raw: Any, lookup, settings: dict) -> dict | None:
        if not isinstance(raw, dict):
            return None
        entry = lookup(raw.get("cmd"))
        if not entry:
            return None
        module, cmd = entry
        args = self._clean_args(cmd, raw.get("args"), settings[module.id] if module.id in self.modules else {})
        key: dict[str, Any] = {"cmd": cmd.id}
        if args:
            key["args"] = args
        label = chat_text(raw.get("label"), LABEL_MAX)
        if label:
            key["label"] = label
        glyph = raw.get("glyph")
        if isinstance(glyph, str) and GLYPH_RE.fullmatch(glyph):
            key["glyph"] = glyph
        return key

    def _clean_layout(self, raw: Any, lookup, settings: dict) -> dict | None:
        """{"columns": [[cell, …] × 3]}. A cell: {"w": widget, "size"} — "w" is
        "<module>.<widget>", "keys" (with "id", "title", "keys": [key or None])
        or None (a blank the page offers to fill). Each module widget once.
        None: never saved (the first layout is built from the modules)."""
        if not isinstance(raw, dict) or not isinstance(raw.get("columns"), list):
            return None
        used: set[str] = set()
        key_ids: set[str] = set()
        columns = []
        for col in (raw["columns"] + [[]] * COLUMNS)[:COLUMNS]:
            cells = []
            for cell in col[:CELLS_MAX] if isinstance(col, list) else ():
                if not isinstance(cell, dict):
                    continue
                w = cell.get("w")
                size = cell.get("size") if cell.get("size") in SIZES else "full"
                if w is None:
                    cells.append({"w": None, "size": size})
                elif w == KEYS_WIDGET:
                    kid = cell.get("id")
                    if not isinstance(kid, str) or not ITEM_ID_RE.fullmatch(kid) or kid in key_ids:
                        kid = f"k{len(key_ids) + 1}"
                        while kid in key_ids:
                            kid += "x"
                    key_ids.add(kid)
                    keys = [self._clean_key(k, lookup, settings) for k in
                            (cell.get("keys")[:KEYS_MAX] if isinstance(cell.get("keys"), list) else [])]
                    cells.append({"w": KEYS_WIDGET, "size": size, "id": kid,
                                  "title": chat_text(cell.get("title"), LABEL_MAX), "keys": keys})
                elif isinstance(w, str) and WIDGET_ALIASES.get(w, w) in self._widgets and WIDGET_ALIASES.get(w, w) not in used:
                    w = WIDGET_ALIASES.get(w, w)
                    used.add(w)
                    sizes = self._widgets[w].sizes
                    cells.append({"w": w, "size": size if size in sizes else sizes[0]})
            columns.append(cells)
        return {"columns": columns}

    def default_layout(self) -> dict:
        """The first layout: each module's placed widgets and its default keys."""
        placed: list[list[tuple[int, int, dict]]] = [[] for _ in range(COLUMNS)]
        groups: dict[str, KeyGroup] = {}
        members: dict[str, list[dict]] = {}
        for n, (mid, m) in enumerate(self.modules.items()):
            for w in m.widgets:
                if 0 <= w.column < COLUMNS and f"{mid}.{w.id}" in self._widgets:
                    placed[w.column].append((w.order, n, {"w": f"{mid}.{w.id}", "size": w.sizes[0]}))
            for g in m.key_groups:
                groups.setdefault(g.id, g)
            for c in m.commands:
                if c.key and c.id in self._commands and self._commands[c.id][1] is c:
                    members.setdefault(c.key, []).append({"cmd": c.id})
        for gid, g in groups.items():
            if members.get(gid) and 0 <= g.column < COLUMNS:
                placed[g.column].append((g.order, 0, {"w": KEYS_WIDGET, "size": g.size if g.size in SIZES else "full",
                                                      "id": gid, "title": g.title, "keys": members[gid]}))
        return {"columns": [[cell for _, _, cell in sorted(col, key=lambda t: (t[0], t[1]))] for col in placed]}

    def layout(self) -> dict:
        return self.settings()["layout"] or self.default_layout()

    def configure(self, raw: Any) -> dict:
        settings = self.clean_settings(raw)
        with self._lock:
            path_changed = (settings["wow_path"], settings["account"]) != (
                self._settings["wow_path"], self._settings["account"])
            self._settings = settings
            self._custom = self._custom_commands(settings["custom"])
            if path_changed:
                self._stamps.clear()   # read again from the new place
                self._installed = None
        return settings

    def settings(self) -> dict:
        with self._lock:
            return self._settings

    def module_settings(self, module_id: str) -> dict:
        if module_id not in self.modules:
            return {}
        with self._lock:
            return self._settings.get(module_id, {})

    def set_player(self, character: str, realm: str, region: str) -> dict:
        """Switch to another character (a command, the settings' list). Marks the
        settings for the server to save: see take_dirty()."""
        settings = self.configure({**self.settings(), "character": character, "realm": realm, "region": region})
        with self._lock:
            self._dirty = True
        return settings

    def take_dirty(self) -> bool:
        """Whether a command changed the settings since the last call."""
        with self._lock:
            dirty, self._dirty = self._dirty, False
            return dirty

    def take_touched(self) -> set[str]:
        """Modules whose views a command changed besides its own (a snapshot came back)."""
        with self._lock:
            touched, self._touched = self._touched, set()
            return touched

    def who(self) -> tuple[str, str, str] | None:
        """(region, realm, character) of the player, when all three are set."""
        s = self.settings()
        return (s["region"], s["realm"], s["character"]) if s["realm"] and s["character"] else None

    def enabled(self, module_id: str) -> bool:
        if module_id == CUSTOM.id:
            return True
        with self._lock:
            return bool(self._settings["modules"].get(module_id))

    # ── state ─────────────────────────────────────────────────────────────

    def module_state(self, module_id: str) -> dict:
        with self._lock:
            return dict(self._state.get(module_id, {}))

    def update_state(self, module_id: str, changes: dict) -> None:
        # Values are replaced, never changed in place, so a shallow copy is a snapshot.
        with self._lock:
            self._state[module_id] = {**self._state.get(module_id, {}), **changes}

    def watch_data(self, module_id: str) -> Any:
        with self._lock:
            return self._data.get(module_id)

    def snapshot(self) -> dict | None:
        with self._lock:
            if self._snapshot is None:
                return None
            return {"data": self._snapshot, "age": max(0.0, self.clock() - self._snapshot_at),
                    "link": self._link_up()}

    # ── the live link ─────────────────────────────────────────────────────

    def _link_up(self, now: float | None = None) -> bool:
        return self._link_at is not None and (now if now is not None else self.clock()) - self._link_at < LINK_FRESH_S

    def link_interval(self) -> float:
        with self._lock:
            if not self._link_up():
                return LINK_SLOW_S
            # Read at a steady 10 Hz, a game drawing pages at 30 fps could show
            # the same page at every read: the rest is read frame by frame.
            return LINK_PAGE_S if self._link.pending else LINK_FAST_S

    def read_link(self) -> set[str]:
        """Read the companion's live link from WoW's window (link.py): the
        modules whose views changed (its state, or the link going up or down)."""
        if not self._companion_ready(ignore_quiet=True):     # switched off, or not in this game folder
            return set()
        page = None
        size = self.screen.size()
        rgb = self.screen.grab(0, 0, size[0], _link.ROWS) if size else None
        if rgb:
            # Where it was last time, else wherever its calibration cells are
            hint = [self._link_x] if self._link_x is not None else []
            for x in hint + [x for x in _link.locate(rgb, size[0]) if x not in hint]:
                page = _link.read_row(rgb, size[0], x)
                if page is not None:
                    self._link_x = x
                    break
        now = self.clock()
        snap_mods = {mid for mid, m in self.modules.items() if m.snapshot}
        with self._lock:
            if page is not None:
                self._link_at = now
            flipped = self._link_up(now) != self._link_told
            self._link_told = self._link_up(now)
            told = snap_mods if flipped else set()
            if flipped:
                logger.info("WoW live link %s", f"up at x={self._link_x}" if self._link_told else "down")
            if page is None:
                return told
            payload = self._link.feed(page, now)
            if payload is None and (self._link.payload is None or now - self._link_parsed < LINK_REFRESH_S):
                return told
            try:
                snap = _link.parse(payload or self._link.payload, self._link.wow_time(now))
            except ValueError:
                return told
            self._link_parsed = now
            old = self._snapshot or {}
            if old.get("char") == snap["char"]:
                # What only the box's full read carries (/tdc full) stays until another character
                snap.update({k: old[k] for k in ("items", "vault", "result") if k in old})
            if payload is None:                    # the same state, its times left counted again
                self._snapshot, self._snapshot_at = snap, now
                return told
        self._took(snap)
        return snap_mods

    # ── commands ──────────────────────────────────────────────────────────

    def run(self, command_id: Any, args: Any = None) -> str:
        """Run a command for a screen: the id of its module, or CommandError."""
        return self.run_ex(command_id, args)[0]

    def run_ex(self, command_id: Any, args: Any = None) -> tuple[str, Any]:
        """Run a command: (its module's id, data for the screen that asked)."""
        entry = self._lookup(command_id)
        if entry is None:
            raise CommandError("unknown")
        module, cmd = entry
        if not self.enabled(module.id):
            raise CommandError("disabled")
        args = {} if args is None else args
        if not isinstance(args, dict) or set(args) - set(cmd.args):
            raise CommandError("invalid")
        settings = self.module_settings(module.id)
        clean = {name: arg.clean(args.get(name), settings) for name, arg in cmd.args.items()}
        ctx = Context(self, module, cmd, clean)
        with self._run_lock:
            try:
                if cmd.run:
                    cmd.run(ctx, clean)
                else:
                    ctx.send()
            except CommandError:
                raise
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
                logger.warning("WoW command %s failed: %r", cmd.id, e)
                raise CommandError("failed") from e
        return module.id, ctx.reply_data

    def slot_of(self, command_id: str, args: dict | None = None) -> str | None:
        """The slot holding this command with these arguments (missing ones at their defaults)."""
        entry = self._lookup(command_id)
        if not entry:
            return None
        module, cmd = entry
        settings = self.module_settings(module.id)
        text = self._macro_text(cmd, args or {}, settings)
        with self._lock:
            binds = dict(self._settings["keybinds"])
        for slot, kb in binds.items():
            if kb["cmd"] == command_id and self._macro_text(cmd, kb["args"], settings) == text:
                return slot
        return None

    def template(self, cmd: Command) -> tuple[str, ...]:
        with self._lock:
            own = self._settings["slash"].get(cmd.id)
        return (own,) if own else cmd.slash

    def lines(self, cmd: Command, args: dict) -> list[str]:
        return self._fill(self.template(cmd), cmd, args)

    def route(self, cmd: Command) -> str:
        """"game" (Blizzard's own: slot or chat) or "addon" (the companion can run it)."""
        learned = self._learned.get(cmd.id)
        if learned:
            return learned
        if cmd.via in ("game", "addon"):
            return cmd.via
        first = (self.template(cmd) or ("",))[0].lstrip("/").split(" ", 1)[0].lower()
        return "game" if first in GAME_WORDS or first.isdigit() else "addon"

    def send(self, cmd: Command, args: dict) -> None:
        if not cmd.slash:
            return
        if not self.keys_ready():
            raise CommandError("no_keyboard")
        if self.screen.front() is False:
            raise CommandError("not_front")
        lines = self.lines(cmd, args)
        if not lines:
            raise CommandError("empty")
        slot = self.slot_of(cmd.id, args)
        if slot:
            if self._use_companion():
                self._load_slots()
            self.send_keys(*SLOT_MODIFIERS, f"KEY_{slot}")
            return
        route = self.route(cmd)
        if route == "game" and not self._in_combat() and self._use_companion():
            result = self._companion(lines, want_result=True, load=True)
            if result == "loaded":
                if not self._box_gone():                 # the chord would go to the box: nothing to press
                    raise CommandError("no_answer")
                self.send_keys(*RUN_KEYS)
                return
            if result is None and self._answered:
                raise CommandError(self._why)
            if result == "":                             # loaded or not: not known, so not run twice
                raise CommandError("no_answer")
            self._chat(lines)                            # in combat, too long, or a companion before 2.5.0
            return
        if route == "addon" and self._use_companion():
            result = self._companion(lines, want_result=True)
            if result == "unknown":            # not an addon's command: Blizzard's, typed from now on
                with self._lock:
                    self._learned[cmd.id] = "game"
                self._chat(lines)
            elif result is None:               # no answer: nothing was pasted or entered
                if self._answered:             # it did before: something holds the keyboard in game
                    raise CommandError(self._why)
                self._chat(lines)              # not loaded in game: chat works
            return
        self._chat(lines)

    def sync(self, full: bool = False) -> None:
        """Read the game's state through the companion (no command)."""
        if not self._companion_ready(ignore_quiet=True):
            raise CommandError("no_companion")
        if self.screen.beacon() is None:
            raise CommandError("no_check")
        if not self.keys_ready():
            raise CommandError("no_keyboard")
        if self.screen.front() is False:
            raise CommandError("not_front")
        full = full or self._link_up()          # the live link has the rest: only bags and vault are left to read
        if self._companion(["/tdc full"] if full else [], want_result=full) is None:
            raise CommandError(self._why)

    def _chat(self, lines: list[str]) -> None:
        """Type each line into WoW's chat through the clipboard, then put the
        clipboard's text back."""
        if not lines:
            raise CommandError("empty")
        saved = self.clipboard.get_text()
        try:
            for line in lines:
                if not self.clipboard.set_text(line):
                    raise CommandError("no_clipboard")
                self.sleep(STEP_S)
                self.send_keys("KEY_ENTER")
                self.sleep(STEP_S)
                self.send_keys("KEY_LEFTCTRL", "KEY_V")
                self.sleep(STEP_S)
                self.send_keys("KEY_ENTER")
                self.sleep(STEP_S)
        finally:
            if saved is not None:
                self.clipboard.set_text(saved)

    # ── the companion addon ───────────────────────────────────────────────

    def _companion_ready(self, ignore_quiet: bool = False) -> bool:
        if not self.settings()["companion"] or not self._companion_version:
            return False
        return ignore_quiet or self.clock() >= self._quiet_until

    def _use_companion(self, ignore_quiet: bool = False) -> bool:
        """Installed, switched on, and its box can be seen: never pressed blind."""
        return self._companion_ready(ignore_quiet) and self.screen.beacon() is not None

    def _box_open(self, wait: float = BEACON_WAIT_S) -> bool:
        """The companion's beacon shows in WoW's window, waited for up to `wait`."""
        for _ in range(max(1, round(wait / (STEP_S / 4)))):
            if self.screen.beacon():
                return True
            self.sleep(STEP_S / 4)
        return False

    def _in_combat(self) -> bool:
        """In combat, as the live link or the box last said (the run button cannot change then)."""
        with self._lock:
            snap = self._snapshot or {}
        return snap.get("combat") is True and self._link_up()

    def _box_gone(self, wait: float = BEACON_WAIT_S) -> bool:
        """The companion's box has given the keyboard back (its beacon is out), waited for up to `wait`."""
        for _ in range(max(1, round(wait / (STEP_S / 4)))):
            if self.screen.beacon() is False:
                return True
            self.sleep(STEP_S / 4)
        return False

    def _companion(self, lines: list[str], *, want_result: bool, load: bool = False) -> str | None:
        """One exchange with the companion's input box: read its state, paste
        `lines`, Enter (`load`: into its run button, not run: "loaded", "combat", "long").
        Its answer for the lines ("ok", "unknown", "queued", …),
        "" when not asked or not given, or None when it did not answer, in which
        case nothing was pasted or entered (`_why` says which: `no_box`, its box
        never opened; `no_answer`, it opened but gave nothing back).

        Ctrl+C, Ctrl+V and Enter are pressed only while the box's beacon shows:
        without the box they would reach the game's own bindings."""
        saved = self.clipboard.get_text()
        nonce = secrets.token_hex(4)
        self._why = "no_box"
        try:
            if not self.clipboard.set_text(f"TDX {nonce}"):
                raise CommandError("no_clipboard")
            self.sleep(STEP_S)
            self.send_keys(*FOCUS_KEYS)
            if not self._box_open():
                with self._lock:
                    if not self._answered:
                        self._quiet_until = self.clock() + QUIET_S
                return None
            if not self._link_up():                 # the live link brings the state without it
                self.send_keys("KEY_LEFTCTRL", "KEY_C")
                before = self._read_snapshot(None)
                if before is None:
                    self._why = "no_answer"
                    return None
                self._took(before)
            newer = self._newer_installed()      # the companion says so in game, once a session
            flags = ("r" if want_result else "") + ("b" if load else "") + (f"n{newer}" if newer else "") or "-"
            envelope = f"{ENVELOPE_TAG} {nonce} {flags} {_pct(chr(10).join(lines))}"
            if not self.clipboard.set_text(envelope):
                raise CommandError("no_clipboard")
            self.sleep(STEP_S)
            if not self._box_open(0):
                return None
            self.send_keys("KEY_LEFTCTRL", "KEY_V")
            self.sleep(STEP_S)
            if not self._box_open(0):
                return None
            self.send_keys("KEY_ENTER")
            if not want_result:
                return ""
            self.sleep(STEP_S)
            self.clipboard.set_text(f"TDX {nonce}")
            if not self._box_open():
                return ""
            self.send_keys("KEY_LEFTCTRL", "KEY_C")
            after = self._read_snapshot(nonce)
            if after is None:
                return ""
            self._took(after)
            result = after.get("result")
            return result if isinstance(result, str) else ""
        finally:
            if saved is not None:
                self.clipboard.set_text(saved)

    def _read_snapshot(self, nonce: str | None) -> dict | None:
        """The companion's "TDS1 <nonce> <json>" on the clipboard, waited for a moment."""
        for _ in range(max(1, round(COPY_WAIT_S / (STEP_S / 2)))):
            self.sleep(STEP_S / 2)
            snap = self._parse_snapshot(self.clipboard.get_text(), nonce)
            if snap is not None:
                return snap
        return None

    @staticmethod
    def _parse_snapshot(text: Any, nonce: str | None) -> dict | None:
        if not isinstance(text, str) or not text.startswith(SNAPSHOT_TAG + " ") or len(text) > SNAPSHOT_MAX:
            return None
        parts = text.split(" ", 2)
        if len(parts) != 3 or (nonce is not None and parts[1] != nonce):
            return None
        try:
            data = json.loads(parts[2])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _took(self, snap: dict) -> None:
        with self._lock:
            self._snapshot = snap
            self._snapshot_at = self.clock()
            sh = snap.get("sh")
            self._slots_hash = sh if isinstance(sh, int) and not isinstance(sh, bool) else None
            self._answered = True
            self._quiet_until = 0.0
            self._touched |= {mid for mid, m in self.modules.items() if m.snapshot}

    def slot_payload(self) -> str:
        """Every slot's macro text for the companion: "1=<text>;3=<text>"."""
        s = self.settings()
        parts = []
        for slot, kb in sorted(s["keybinds"].items(), key=lambda kv: int(kv[0][1:])):
            entry = self._lookup(kb["cmd"])
            if entry:
                text = self._macro_text(entry[1], kb["args"], s.get(entry[0].id, {}))
                if text:
                    parts.append(f"{slot[1:]}={_pct(text)}")
        return ";".join(parts)

    def _load_slots(self) -> None:
        """Give the companion's secure buttons the slots' text when it holds other text."""
        payload = self.slot_payload()
        if self._slots_hash == adler32(payload):
            return
        result = self._companion([f"/tdc slots {payload}".rstrip()], want_result=True)
        if result == "queued":   # in combat: loaded once it ends; pressing now would run the old text
            raise CommandError("in_combat")

    def companion_status(self, admin: bool = False) -> dict:
        with self._lock:
            running = (self._snapshot or {}).get("ver")
            out = {"installed": self._companion_version, "bundled": bundled_version(),
                   "running": running if isinstance(running, str) else None,
                   "enabled": self._settings["companion"], "link": self._link_up(),
                   "answered": None if self._snapshot_at is None else round(self.clock() - self._snapshot_at)}
            if admin:
                out["dir"] = self._addons_dir
        return out

    def _update_companion(self) -> None:
        """The companion in the game folder, older than this install's: replaced
        with it (WoW loads it at the next /reload or login, and the companion
        then says so in game: `_newer_installed`). Only where it is already
        installed and switched on; a failed copy is not tried again for that
        version."""
        with self._lock:
            have, on = self._companion_version, self._settings["companion"]
        new = bundled_version()
        if not (have and new and on) or _version(have) >= _version(new) or self._update_failed == new:
            return
        try:
            got = self.install_companion()
        except CommandError:
            self._update_failed = new
            return
        logger.info("Updated the WoW companion addon in the game folder from %s to %s", have, got)

    def _newer_installed(self) -> str | None:
        """The version in the game folder when WoW still runs an older one (the
        snapshot's): it takes a /reload."""
        with self._lock:
            running = (self._snapshot or {}).get("ver")
            installed = self._companion_version
        if isinstance(running, str) and installed and _version(running) < _version(installed):
            return installed
        return None

    def install_companion(self) -> str:
        """Copy addon/TouchDashboard into the game's Interface/AddOns. Its version,
        or CommandError("no_folder") without an AddOns folder."""
        self._scan_addons()
        target_root = self._addons_dir
        if not target_root or not os.path.isdir(target_root):
            raise CommandError("no_folder")
        target = os.path.join(target_root, COMPANION)
        try:
            os.makedirs(target, exist_ok=True)
            for name in sorted(os.listdir(self.companion_dir)):
                src = os.path.join(self.companion_dir, name)
                if os.path.isfile(src) and name.endswith((".toc", ".lua", ".xml")):
                    shutil.copyfile(src, os.path.join(target, name))
        except OSError as e:
            logger.warning("Could not install the WoW companion addon in %s: %s", target, e)
            raise CommandError("failed") from e
        with self._lock:
            self._installed = None
        self._scan_addons()
        return self._companion_version or ""

    def _scan_addons(self) -> None:
        """Which addons are in the game's Interface/AddOns, and the companion's version."""
        base = self.settings()["wow_path"]
        addons_dir = None
        if base:
            # The game folder, its _retail_, or a SavedVariables folder 4 levels under _retail_.
            up = os.path.expanduser(base).rstrip("/\\")
            roots = [up, os.path.join(up, "_retail_")]
            for _ in range(4):
                up = os.path.dirname(up)
                roots.append(up)
            addons_dir = next((os.path.join(r, "Interface", "AddOns") for r in roots
                               if os.path.isdir(os.path.join(r, "Interface", "AddOns"))), None)
        names: frozenset[str] = frozenset()
        version = None
        if addons_dir:
            try:
                names = frozenset(n for n in os.listdir(addons_dir) if os.path.isdir(os.path.join(addons_dir, n)))
            except OSError:
                names = frozenset()
            if COMPANION in names:
                try:
                    with open(os.path.join(addons_dir, COMPANION, f"{COMPANION}.toc"), encoding="utf-8",
                              errors="replace") as f:
                        m = VERSION_RE.search(f.read(4096))
                    version = m.group(1) if m else "0"
                except OSError:
                    version = None
        with self._lock:
            self._addons_dir = addons_dir
            self._installed = names if addons_dir else None
            self._companion_version = version

    # ── watched files ─────────────────────────────────────────────────────

    def has_addon(self, folder: str) -> bool | None:
        with self._lock:
            return None if self._installed is None else folder in self._installed

    def find_file(self, filename: str) -> str | None:
        """The newest SavedVariables/<filename> under the configured folder: the
        game folder, its _retail_, or a SavedVariables folder itself."""
        settings = self.settings()
        base = settings["wow_path"]
        if not base:
            return None
        base = os.path.expanduser(base)
        if os.path.isfile(os.path.join(base, filename)):
            return os.path.join(base, filename)
        account = glob.escape(settings["account"]) if settings["account"] else "*"
        found: list[str] = []
        for root in (base, os.path.join(base, "_retail_")):
            found += glob.glob(os.path.join(glob.escape(root), "WTF", "Account", account,
                                            "SavedVariables", glob.escape(filename)))
        best, best_mtime = None, -1.0
        for path in found:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        return best

    def poll(self) -> set[str]:
        """Read every watched file that changed; the ids of modules whose data did."""
        with self._poll_lock:
            before = (self._installed, self._companion_version)
            self._scan_addons()
            self._update_companion()
            changed = self._poll()
            if (self._installed, self._companion_version) != before:
                changed.add("")   # the modules' installed flags: wow_state
            return changed

    def _poll(self) -> set[str]:
        changed: set[str] = set()
        for mid, m in self.modules.items():
            if m.watch is None:
                continue
            if not self.enabled(mid):
                with self._lock:
                    if self._stamps.pop(mid, None) is not None or self._data.pop(mid, None) is not None:
                        changed.add(mid)
                continue
            path = self.find_file(m.watch.filename)
            stamp = None
            if path:
                try:
                    st = os.stat(path)
                    stamp = (path, st.st_mtime_ns, st.st_size)
                except OSError:
                    path = None
            with self._lock:
                if mid in self._stamps and self._stamps[mid] == stamp:
                    continue
                if mid not in self._stamps and stamp is None:   # not there: a change only if it was read before
                    self._stamps[mid] = None
                    self._files[mid] = None
                    if self._data.pop(mid, None) is not None:
                        changed.add(mid)
                    continue
            data = None
            if stamp and stamp[2] <= MAX_FILE_BYTES:
                try:
                    with open(path, "rb") as f:
                        data = m.watch.load(f.read().decode("latin-1"))
                except OSError as e:
                    logger.warning("Could not read %s: %s", path, e)
                except Exception:   # a module's parser must not stop the others' watches
                    logger.exception("WoW addon %s could not read %s", mid, path)
            elif stamp:
                logger.warning("%s is larger than %d bytes; not read", path, MAX_FILE_BYTES)
            with self._lock:
                self._stamps[mid] = stamp
                self._files[mid] = path
                self._data[mid] = data
            changed.add(mid)
        return changed

    # ── messages ──────────────────────────────────────────────────────────

    def _describe(self, m: AddonModule, c: Command) -> dict:
        return {
            "id": c.id, "label": c.label, "args": {k: a.describe() for k, a in c.args.items()},
            "template": list(self.template(c)), "default": list(c.slash),
            "verified": c.verified, "bindable": c.bindable, "slot": self.slot_of(c.id) if c.bindable else None,
            "glyph": c.glyph, "category": c.category or m.name, "via": self.route(c) if c.slash else "",
            "description": c.description, "addon": c.addon or m.addon or None,
        }

    def state_msg(self, admin: bool) -> dict:
        """wow_state: the modules, their commands and widgets, settings, the
        layout and shared state. Paired devices get no paths, private settings
        or the player's own texts."""
        now = self.clock()
        with self._lock:
            settings = self._settings
            installed = self._installed
            modules = []
            for mid, m in [*self.modules.items(), (CUSTOM.id, CUSTOM)]:
                cmds = list(self._custom.values()) if m is CUSTOM else m.commands
                if m is CUSTOM and not cmds:
                    continue
                modules.append({
                    "id": mid, "name": m.name, "description": m.description,
                    "enabled": self.enabled(mid), "commands": [self._describe(m, c) for c in cmds],
                    "watch": m.watch.filename if m.watch else None,
                    "widgets": [{"id": w.id, "name": w.name, "description": w.description,
                                 "sizes": list(w.sizes), "grow": w.grow} for w in m.widgets
                                if f"{mid}.{w.id}" in self._widgets],
                    "addon": m.addon or None,
                    "installed": None if not m.addon or installed is None else m.addon in installed,
                })
            public = {k: v for k, v in settings.items() if k not in self.modules
                      and (admin or k not in ADMIN_ONLY_KEYS)}
            public["layout"] = settings["layout"] or self.default_layout()
            public["layout_default"] = settings["layout"] is None
            for mid, m in self.modules.items():
                public[mid] = {k: v for k, v in settings[mid].items() if admin or k not in m.private}
            state = {}
            for mid, m in self.modules.items():
                s = dict(self._state[mid])
                state[mid] = m.public(s, now) if m.public else s
            data: dict[str, Any] = {"modules": modules, "settings": public, "state": state, "slots": list(SLOTS),
                                    "companion": self.companion_status(admin), "templates": self._templates(settings)}
            if admin:
                data["files"] = {mid: self._files.get(mid) for mid, m in self.modules.items() if m.watch}
                data["slot_text"] = {}
                for slot, kb in settings["keybinds"].items():
                    entry = self._lookup(kb["cmd"])
                    if entry:
                        data["slot_text"][slot] = self._macro_text(entry[1], kb["args"],
                                                                   self.module_settings(entry[0].id))
        return {"type": "wow_state", "data": data}

    def _templates(self, settings: dict) -> list[dict]:
        """The keys plates the page offers to start from: enabled modules',
        each key checked as a saved one would be."""
        lookup = lambda cid: self._lookup(cid, self._custom)  # noqa: E731
        installed = self._installed

        def have(key: dict) -> bool:          # an addon's key only when that addon is in the game folder
            entry = lookup(key.get("cmd"))
            folder = entry and (entry[1].addon or entry[0].addon)
            return not folder or installed is None or folder in installed

        out = []
        for mid, m in self.modules.items():
            if not settings["modules"].get(mid):
                continue
            for t in m.templates:
                keys = [k for k in (self._clean_key(dict(k), lookup, settings) for k in t.keys[:KEYS_MAX])
                        if k and have(k)]
                if keys:
                    out.append({"id": f"{mid}.{t.id}", "title": t.title, "description": t.description,
                                "glyph": t.glyph, "module": mid, "keys": keys})
        return out

    def live_msg(self) -> dict:
        """wow_live: what a command changes (shared state, the companion's status),
        without wow_state's catalog. The same for every screen."""
        now = self.clock()
        with self._lock:
            state = {mid: (m.public(dict(self._state[mid]), now) if m.public else dict(self._state[mid]))
                     for mid, m in self.modules.items()}
        return {"type": "wow_live", "data": {"state": state, "companion": self.companion_status(False)}}

    def update_msgs(self, module_ids: set[str] | None = None) -> list[dict]:
        """wow_<id>_update for each enabled module with a view."""
        out = []
        for mid, m in self.modules.items():
            if m.view is None or (module_ids is not None and mid not in module_ids) or not self.enabled(mid):
                continue
            try:
                view = m.view(Context(self, m))
            except Exception:   # a broken view must not stop the others
                logger.exception("WoW addon %s view failed", mid)
                continue
            out.append({"type": f"wow_{mid}_update", "data": view})
        return out
