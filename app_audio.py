"""
app_audio.py — Per-application audio for the full-screen mixer.

Linux:   PipeWire through its PulseAudio interface (`pactl -f json`): playback
         streams (sink-inputs) and recording streams (source-outputs).
Windows: WASAPI audio sessions through pycaw: render sessions on the default
         output, capture sessions on the default input. Untested on real Windows.

Streams are grouped per application, one strip per app however many streams
it opens (a browser opens one per tab); a level or mute set on a strip goes
to every stream of that app. Read only while a screen shows the mixer
(server.py polls `read()` then), so it costs nothing otherwise.

The pure parts (`app_id`, `display_name`, `groups_from_pactl`, `find_icon`)
are tested in tests/test_app_audio.py. Nothing here imports server.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

OUT, IN = "out", "in"            # playback (left of the master), recording (right)
DIRS = (OUT, IN)
APP_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SOUNDBOARD_SINK = "Dashboard-Soundboard"   # our own pw-play target and its loopbacks
NAME_MAX = 40

# Names a stream carries that say nothing about the app: the process name reads better.
GENERIC_NAMES = {"webrtc voiceengine", "chromium", "chromium input", "audio stream",
                 "playback", "record", "recordstream", "playstream"}
_ALSA_PLUGIN_RE = re.compile(r"ALSA plug-in \[(.+?)\]", re.I)

# Icon files: the app's own (hicolor, pixmaps, Flatpak exports), never a
# theme's restyled copy. PNG only: an SVG served from this origin could carry script.
ICON_ROOTS = (
    os.path.expanduser("~/.local/share/icons"),
    os.path.expanduser("~/.local/share/flatpak/exports/share/icons"),
    "/var/lib/flatpak/exports/share/icons",
    "/usr/local/share/icons",
    "/usr/share/icons",
)
PIXMAP_DIRS = ("/usr/share/pixmaps",)
ICON_SIZES = ("256x256", "192x192", "128x128", "96x96", "64x64", "48x48")
ICON_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")   # no slashes: names, not paths
ICON_MAX_BYTES = 1 << 20
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ICON_CACHE_MAX = 256

_icons: dict[str, str | None] = {}   # app id → icon file (None: looked, none found)
_icons_lock = threading.Lock()

# The newest reading per direction: {app id: group}. Replaced whole by read()
# and patched by set_volume()/set_mute(), so readers never see half a reading.
_last: dict[str, dict[str, dict]] = {OUT: {}, IN: {}}
stale = False   # a change was applied: read again on the next tick


# ── naming ────────────────────────────────────────────────────────────────────

def _slug(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    s = re.split(r"[\\/]", value.strip())[-1].lower()   # a path's last part, Windows' too
    if s.endswith(".exe"):
        s = s[:-4]
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-._")[:64]
    return s if APP_ID_RE.fullmatch(s) else None


def app_id(props: dict) -> str | None:
    """The app a stream belongs to: its process, else the name it gives."""
    for key in ("application.process.binary", "application.id", "application.name", "node.name"):
        s = _slug(props.get(key))
        if s:
            return s
    return None


def _pretty(slug: str) -> str:
    """'gpu-screen-recorder' → 'Gpu screen recorder'."""
    words = re.sub(r"[-_]+", " ", slug).strip() or slug
    return words[:1].upper() + words[1:]


# A stream name that is an identifier ("gsr-default_input"), not something to read.
_MACHINE_NAME_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)+")


def display_name(props: dict, aid: str) -> str:
    name = (props.get("application.name") or "").strip()
    m = _ALSA_PLUGIN_RE.fullmatch(name)
    if m:
        name = m.group(1).strip()
    binary = _slug(props.get("application.process.binary"))
    if binary and name.lower() != binary and (name.lower() in GENERIC_NAMES or _MACHINE_NAME_RE.fullmatch(name)):
        name = ""
    return (name or _pretty(binary or aid))[:NAME_MAX]


# ── Linux: pactl ──────────────────────────────────────────────────────────────

def _level(volume) -> int:
    """The loudest channel, in percent (65536 = 100 %)."""
    if not isinstance(volume, dict):
        return 0
    values = [ch.get("value") for ch in volume.values() if isinstance(ch, dict)]
    values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(max(values) * 100 / 65536) if values else 0


def _skip(entry: dict, kind: str, monitors: frozenset[int]) -> bool:
    props = entry.get("properties") or {}
    owner = entry.get("owner_module")
    if owner not in (None, "", "4294967295"):
        return True   # a module's own stream: loopbacks, filter chains (NoiseTorch), our routing
    if props.get("stream.monitor") == "true" or "peak detect" in (props.get("media.name") or "").lower():
        return True   # a volume meter (pavucontrol, the desktop's applet)
    if props.get("media.role") == "event":
        return True   # a notification sound: gone before it could be touched
    if props.get("target.object") == SOUNDBOARD_SINK:
        return True   # the soundboard's own pw-play
    if kind == IN and entry.get("source") in monitors:
        return True   # recording what plays (a screen recorder's desktop audio), not the mic
    return False


def groups_from_pactl(entries: list, kind: str, monitors: frozenset[int] = frozenset()) -> dict[str, dict]:
    """pactl's sink-inputs (OUT) or source-outputs (IN), one group per app, in
    the order each app's oldest stream appeared."""
    groups: dict[str, dict] = {}
    for entry in sorted((e for e in entries if isinstance(e, dict)), key=lambda e: e.get("index", 0)):
        props = entry.get("properties") or {}
        if _skip(entry, kind, monitors):
            continue
        aid = app_id(props)
        if aid is None or not isinstance(entry.get("index"), int):
            continue
        vol, muted, live = _level(entry.get("volume")), bool(entry.get("mute")), not entry.get("corked")
        g = groups.get(aid)
        if g is None:
            groups[aid] = {
                "id": aid, "name": display_name(props, aid), "vol": vol, "muted": muted,
                "active": live, "streams": [entry["index"]],
                "icons": [props.get(k) for k in ("application.icon_name", "application.id",
                                                  "application.process.binary", "application.name")],
            }
            continue
        if g["name"] == _pretty(aid) and display_name(props, aid) != g["name"]:
            g["name"] = display_name(props, aid)
        g["vol"] = max(g["vol"], vol)
        g["muted"] = g["muted"] and muted   # muted only when every stream is
        g["active"] = g["active"] or live
        g["streams"].append(entry["index"])
    return groups


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=2, check=False).stdout.decode()
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("app_audio %s: %s", cmd[:4], e)
        return ""


def _pactl_list(what: str) -> list:
    out = _run(["pactl", "-f", "json", "list", what])
    try:
        data = json.loads(out) if out else []
    except ValueError:
        return []
    return data if isinstance(data, list) else []


_sources_seen: set[int] = set()
_monitor_sources: frozenset[int] = frozenset()


def _monitors(source_ids: set[int]) -> frozenset[int]:
    """Indexes of the monitor sources, re-read only when a stream records from one not seen yet."""
    global _monitor_sources
    if not source_ids <= _sources_seen:
        mons: set[int] = set()
        for line in _run(["pactl", "list", "short", "sources"]).splitlines():
            parts = line.split("\t")
            if len(parts) > 1 and parts[0].isdigit():
                _sources_seen.add(int(parts[0]))
                if parts[1].endswith(".monitor"):
                    mons.add(int(parts[0]))
        _monitor_sources = frozenset(mons)
    return _monitor_sources


def _linux_read() -> dict[str, dict[str, dict]]:
    playing = _pactl_list("sink-inputs")
    recording = _pactl_list("source-outputs")
    sources = {e.get("source") for e in recording if isinstance(e, dict) and isinstance(e.get("source"), int)}
    return {OUT: groups_from_pactl(playing, OUT),
            IN: groups_from_pactl(recording, IN, _monitors(sources))}


def _pactl_kind(kind: str) -> str:
    return "sink-input" if kind == OUT else "source-output"


# ── Windows: pycaw sessions ───────────────────────────────────────────────────

def _win_sessions(kind: str) -> list:
    """pycaw AudioSession objects on the default output (OUT) or input (IN)."""
    from pycaw.pycaw import AudioUtilities  # type: ignore[import]
    if kind == OUT:
        return list(AudioUtilities.GetAllSessions())
    # pycaw only lists render sessions; capture ones are the same walk on the mic.
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL  # type: ignore[import]
    from pycaw.pycaw import AudioSession, IAudioSessionControl2, IAudioSessionManager2  # type: ignore[import]
    mic = AudioUtilities.GetMicrophone()
    dev = getattr(mic, "_dev", mic)
    if dev is None:
        return []
    mgr = cast(dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None), POINTER(IAudioSessionManager2))
    sessions = []
    enum = mgr.GetSessionEnumerator()
    for i in range(enum.GetCount()):
        ctl = enum.GetSession(i)
        if ctl is None:
            continue
        ctl2 = ctl.QueryInterface(IAudioSessionControl2)
        if ctl2 is not None:
            sessions.append(AudioSession(ctl2))
    return sessions


def _win_session_app(session) -> tuple[str, str] | None:
    """(app id, name) for a session; the system sounds session is 'system'."""
    proc = getattr(session, "Process", None)
    if proc is None:
        return ("system", "System sounds") if getattr(session, "ProcessId", 0) == 0 else None
    try:
        exe = proc.name()
    except Exception:   # psutil: the process has exited or is not ours to read
        return None
    aid = _slug(exe)
    if aid is None:
        return None
    label = (getattr(session, "DisplayName", "") or "").strip()
    if label.startswith("@"):   # an indirect resource string ("@%SystemRoot%\...")
        label = ""
    return aid, (label or _pretty(aid))[:NAME_MAX]


def _win_read_kind(kind: str) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for session in _win_sessions(kind):
        app = _win_session_app(session)
        if app is None:
            continue
        aid, name = app
        try:
            sav = session.SimpleAudioVolume
            vol, muted = round(sav.GetMasterVolume() * 100), bool(sav.GetMute())
            live = getattr(session, "State", 0) == 1   # AudioSessionStateActive
        except Exception as e:   # comtypes COMError: the session just closed
            logger.debug("app_audio session %s: %s", aid, e)
            continue
        g = groups.setdefault(aid, {"id": aid, "name": name, "vol": vol, "muted": muted,
                                    "active": live, "streams": [], "icons": []})
        g["vol"], g["muted"], g["active"] = max(g["vol"], vol), g["muted"] and muted, g["active"] or live
    return groups


def _win_apply(kind: str, aid: str, vol: int | None = None, muted: bool | None = None) -> None:
    for session in _win_sessions(kind):
        app = _win_session_app(session)
        if app is None or app[0] != aid:
            continue
        sav = session.SimpleAudioVolume
        if vol is not None:
            sav.SetMasterVolume(max(0.0, min(1.0, vol / 100)), None)
            sav.SetMute(0, None)
        if muted is not None:
            sav.SetMute(1 if muted else 0, None)


def _windows(fn, *args):
    """Run a pycaw call with COM initialised on this thread; never raises."""
    from audio_system import _ensure_com   # COM set-up lives with the rest of the WASAPI code
    import gc
    try:
        _ensure_com()
        return fn(*args)
    except Exception as e:   # comtypes/pycaw raise COMError, OSError and AttributeError across versions
        logger.debug("app_audio %s: %s", getattr(fn, "__name__", fn), e)
        return None
    finally:
        gc.collect(0)


# ── public API ────────────────────────────────────────────────────────────────

def read() -> dict[str, dict[str, dict]]:
    """Read every app's level now: {OUT: {id: group}, IN: {id: group}}."""
    global stale, _last
    stale = False
    if sys.platform.startswith("win"):
        result = {kind: _windows(_win_read_kind, kind) or {} for kind in DIRS}
    else:
        result = _linux_read()
    for groups in result.values():
        for g in groups.values():
            g["has_icon"] = icon_path(g["id"], g.get("icons")) is not None
    _last = result
    return result


def latest() -> dict[str, dict[str, dict]]:
    return _last


def known(kind: str, aid) -> bool:
    return kind in DIRS and isinstance(aid, str) and aid in _last.get(kind, {})


def _patch(kind: str, aid: str, **fields) -> None:
    group = _last.get(kind, {}).get(aid)
    if group is not None:
        _last[kind] = {**_last[kind], aid: {**group, **fields}}


def set_volume(kind: str, aid: str, val: int) -> None:
    """Set every stream of an app to val % (0–100); moving a fader unmutes, as on the masters."""
    global stale
    group = _last.get(kind, {}).get(aid)
    if group is None:
        return
    val = max(0, min(100, int(val)))
    if sys.platform.startswith("win"):
        _windows(_win_apply, kind, aid, val, None)
    else:
        what = _pactl_kind(kind)
        for idx in group["streams"]:
            _run(["pactl", f"set-{what}-volume", str(idx), f"{val}%"])
            if group["muted"]:
                _run(["pactl", f"set-{what}-mute", str(idx), "0"])
    _patch(kind, aid, vol=val, muted=False)
    stale = True


def set_mute(kind: str, aid: str, muted: bool) -> None:
    global stale
    group = _last.get(kind, {}).get(aid)
    if group is None:
        return
    if sys.platform.startswith("win"):
        _windows(_win_apply, kind, aid, None, muted)
    else:
        what = _pactl_kind(kind)
        for idx in group["streams"]:
            _run(["pactl", f"set-{what}-mute", str(idx), "1" if muted else "0"])
    _patch(kind, aid, muted=muted)
    stale = True


def public(groups: dict[str, dict]) -> list[dict]:
    """What a screen gets for one direction (the server adds each icon's signed URL)."""
    return [{"id": g["id"], "name": g["name"], "vol": g["vol"], "muted": g["muted"],
             "active": g["active"], "icon": bool(g.get("has_icon"))} for g in groups.values()]


# ── icons ─────────────────────────────────────────────────────────────────────

def _is_png(path: str) -> bool:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > ICON_MAX_BYTES:
            return False
        with open(path, "rb") as f:
            return f.read(8) == PNG_MAGIC
    except OSError:
        return False


def find_icon(names, roots=ICON_ROOTS, pixmaps=PIXMAP_DIRS) -> str | None:
    """The largest PNG of the first name that has one, by icon-theme name only."""
    seen: list[str] = []
    for name in names or ():
        if not isinstance(name, str):
            continue
        for variant in (name.strip(), name.strip().lower()):
            if ICON_NAME_RE.fullmatch(variant) and variant not in seen:
                seen.append(variant)
    for name in seen:
        for size in ICON_SIZES:
            for root in roots:
                path = os.path.join(root, "hicolor", size, "apps", name + ".png")
                if _is_png(path):
                    return path
        for d in pixmaps:
            path = os.path.join(d, name + ".png")
            if _is_png(path):
                return path
    return None


def icon_path(aid: str, names=None) -> str | None:
    """The icon file for an app id, looked up once (names: what its streams call their icon)."""
    if sys.platform.startswith("win"):
        return None
    with _icons_lock:
        if aid in _icons:
            return _icons[aid]
    if names is None:
        return None   # the route serves only icons a reading has already found
    path = find_icon([*names, aid])
    with _icons_lock:
        if len(_icons) >= _ICON_CACHE_MAX:
            _icons.pop(next(iter(_icons)))
        _icons[aid] = path
    return path
