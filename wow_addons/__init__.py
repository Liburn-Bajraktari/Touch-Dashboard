"""
wow_addons — World of Warcraft addon integrations (the WoW plugin page).

Each addon is a module in wow_addons/modules/: a file defining MODULE, an
AddonModule with its commands, its own settings and shared state, and
optionally a SavedVariables file to watch and a view of it for the page. The
core never names an addon: a new one is a file there plus its page side,
static/wow/modules/<id>.js. A module that fails to import is skipped and the
others keep working.

How a command reaches the game: WoW lets no addon talk to anything outside it,
so a slash command is typed as a player would type it. The text goes on the
clipboard, then Enter (open chat), Ctrl+V, Enter; the clipboard's text is put
back afterwards. A command given one of the Ctrl+Alt+Shift+F1-F12 slots presses
that key instead, which runs the macro the player bound to it in game. That
macro's text is fixed, so only commands without arguments can have a slot.
Either way WoW must be the window in front.

Nothing here writes to SavedVariables: WoW reads them only at login and
overwrites them on logout, so a write would be lost or clobber the player's.
"""
from __future__ import annotations

import copy
import glob
import importlib
import logging
import math
import os
import pkgutil
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

from . import clipboard as _clipboard
from .armory import REGIONS

logger = logging.getLogger(__name__)

MODULE_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
COMMAND_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
ITEM_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
ACCOUNT_RE = re.compile(r"[A-Za-z0-9#_. -]{0,64}")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

SLOTS = tuple(f"F{n}" for n in range(1, 13))
SLOT_MODIFIERS = ("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTSHIFT")
STEP_S = 0.08                    # between keys: WoW reads input once a frame
SLASH_MAX = 255                  # WoW's chat box takes 255 characters
NAME_MAX = 48
PATH_MAX = 1024
NOTE_MAX = 64 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024


class CommandError(Exception):
    """A command that did not run. `reason` goes back to the screen in wow_cmd_ack:
    unknown, disabled, invalid, no_keyboard, no_clipboard, empty, failed."""

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

    @property
    def bindable(self) -> bool:
        return bool(self.slash) and not self.args and len(self.slash) == 1


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


class Context:
    """What a command's `run` and a module's `view` may use."""

    def __init__(self, core: "WowAddons", module: AddonModule, command: Command | None = None,
                 args: dict | None = None):
        self._core = core
        self.module = module
        self.command = command
        self.args = args or {}

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

    def update(self, **changes) -> None:
        self._core.update_state(self.module.id, changes)

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
                "keybinds", "slash")
ADMIN_ONLY_KEYS = frozenset({"wow_path", "account", "slash"})


class WowAddons:
    """The plugin: modules, their settings and state, commands and watched files.

    Thread-safe: the server calls run() and poll() from worker threads and the
    message builders from the event loop. Commands run one at a time, since
    they share the clipboard and the keyboard."""

    def __init__(self, modules: list[AddonModule], *, send_keys: Callable[..., None],
                 keys_ready: Callable[[], bool] = lambda: True, clipboard: Any = _clipboard,
                 open_url: Callable[[str], Any] | None = None, launch: Callable[[str], Any] | None = None,
                 armory: Any = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.modules: dict[str, AddonModule] = {}
        self._commands: dict[str, tuple[AddonModule, Command]] = {}
        for m in sorted(modules, key=lambda m: (m.order, m.id)):
            if m.id in self.modules or m.id in GENERAL_KEYS:
                # Its settings live under wow_addons[<id>], beside the general ones.
                logger.warning("WoW addon module id %r is taken; the module is skipped", m.id)
                continue
            self.modules[m.id] = m
            for c in m.commands:
                if not COMMAND_ID_RE.fullmatch(c.id) or c.id in self._commands:
                    logger.warning("WoW command %r of %s skipped (bad or duplicate id)", c.id, m.id)
                    continue
                self._commands[c.id] = (m, c)
        self.send_keys = send_keys
        self.keys_ready = keys_ready
        self.clipboard = clipboard
        self.open_url = open_url
        self.launch = launch
        self.armory = armory
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._settings = self.clean_settings({})
        self._state = {mid: copy.deepcopy(m.state) for mid, m in self.modules.items()}
        self._data: dict[str, Any] = {}
        self._stamps: dict[str, tuple | None] = {}
        self._files: dict[str, str | None] = {}
        self._dirty = False

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

        keybinds: dict[str, str] = {}
        raw_keys = raw.get("keybinds") if isinstance(raw.get("keybinds"), dict) else {}
        for slot in SLOTS:
            cmd_id = raw_keys.get(slot)
            entry = self._commands.get(cmd_id) if isinstance(cmd_id, str) else None
            if entry and entry[1].bindable and cmd_id not in keybinds.values():
                keybinds[slot] = cmd_id
        out["keybinds"] = keybinds

        slash: dict[str, str] = {}
        raw_slash = raw.get("slash") if isinstance(raw.get("slash"), dict) else {}
        for cmd_id, text in raw_slash.items():
            entry = self._commands.get(cmd_id) if isinstance(cmd_id, str) else None
            template = self._clean_template(entry[1], text) if entry else None
            if template:
                slash[cmd_id] = template
        out["slash"] = slash

        for mid, m in self.modules.items():
            sub = raw.get(mid) if isinstance(raw.get(mid), dict) else {}
            clean = m.clean or _default_clean(m.defaults)
            out[mid] = {**copy.deepcopy(m.defaults), **clean(sub)}
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

    def configure(self, raw: Any) -> dict:
        settings = self.clean_settings(raw)
        with self._lock:
            path_changed = (settings["wow_path"], settings["account"]) != (
                self._settings["wow_path"], self._settings["account"])
            self._settings = settings
            if path_changed:
                self._stamps.clear()   # read again from the new place
        return settings

    def settings(self) -> dict:
        with self._lock:
            return self._settings

    def module_settings(self, module_id: str) -> dict:
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

    def who(self) -> tuple[str, str, str] | None:
        """(region, realm, character) of the player, when all three are set."""
        s = self.settings()
        return (s["region"], s["realm"], s["character"]) if s["realm"] and s["character"] else None

    def enabled(self, module_id: str) -> bool:
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

    # ── commands ──────────────────────────────────────────────────────────

    def run(self, command_id: Any, args: Any = None) -> str:
        """Run a command for a screen: the id of its module, or CommandError."""
        entry = self._commands.get(command_id) if isinstance(command_id, str) else None
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
        return module.id

    def slot_of(self, command_id: str) -> str | None:
        with self._lock:
            return next((s for s, c in self._settings["keybinds"].items() if c == command_id), None)

    def template(self, cmd: Command) -> tuple[str, ...]:
        with self._lock:
            own = self._settings["slash"].get(cmd.id)
        return (own,) if own else cmd.slash

    def lines(self, cmd: Command, args: dict) -> list[str]:
        out = []
        for line in self.template(cmd):
            text = PLACEHOLDER_RE.sub(lambda m: str(args.get(m.group(1), "")), line).strip()
            if text:
                out.append(text[:SLASH_MAX])
        return out

    def send(self, cmd: Command, args: dict) -> None:
        if not cmd.slash:
            return
        slot = self.slot_of(cmd.id) if cmd.bindable else None
        if not self.keys_ready():
            raise CommandError("no_keyboard")
        if slot:
            self.send_keys(*SLOT_MODIFIERS, f"KEY_{slot}")
            return
        self._chat(self.lines(cmd, args))

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

    # ── watched files ─────────────────────────────────────────────────────

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
            return self._poll()

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

    def state_msg(self, admin: bool) -> dict:
        """wow_state: the modules, their commands, settings and shared state.
        Paired devices get no paths, private settings or the player's own texts."""
        now = self.clock()
        with self._lock:
            settings = self._settings
            modules = []
            for mid, m in self.modules.items():
                commands = []
                for c in m.commands:
                    commands.append({
                        "id": c.id, "label": c.label, "args": {k: a.describe() for k, a in c.args.items()},
                        "template": list(self.template(c)), "default": list(c.slash),
                        "verified": c.verified, "bindable": c.bindable, "slot": self.slot_of(c.id),
                    })
                modules.append({"id": mid, "name": m.name, "description": m.description,
                                "enabled": settings["modules"][mid], "commands": commands,
                                "watch": m.watch.filename if m.watch else None})
            public = {k: v for k, v in settings.items() if k not in self.modules
                      and (admin or k not in ADMIN_ONLY_KEYS)}
            for mid, m in self.modules.items():
                public[mid] = {k: v for k, v in settings[mid].items() if admin or k not in m.private}
            state = {}
            for mid, m in self.modules.items():
                s = dict(self._state[mid])
                state[mid] = m.public(s, now) if m.public else s
            data: dict[str, Any] = {"modules": modules, "settings": public, "state": state, "slots": list(SLOTS)}
            if admin:
                data["files"] = {mid: self._files.get(mid) for mid, m in self.modules.items() if m.watch}
        return {"type": "wow_state", "data": data}

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
