"""
server.py — Touch Dashboard backend.

Architecture:
  FastAPI + uvicorn handle HTTP and WebSocket transport.
  desktop.py (pywebview + pystray) provides the desktop window and tray icon.
  server-only mode skips the desktop entirely (headless).

Platform logic lives in the companion modules:
  audio_system.py  — cross-platform audio
  desktop.py       — pywebview window + pystray tray
  discord_ipc.py   — Discord IPC client
  macro_system.py  — keyboard macros + app enumeration
  media.py         — Spotify / MPRIS / Windows Media Transport
"""
from __future__ import annotations

import sys
import os



# ─── Windows COM Apartment Mode Initialization ────────────────────────────────
# Python threads default to no COM apartment. If a GC cycle runs on an uninitialized
# thread while collecting a COM object (like an audio endpoint), Windows throws an 
# Access Violation (0xC0000005) and violently crashes the app.
if sys.platform.startswith("win"):
    sys.coinit_flags = 0
    import comtypes  # MUST import here to lock MTA before PyQt locks STA!

# Pythonw.exe sets sys.stdout and sys.stderr to None.
# Some third-party libraries like speedtest-cli expect them to have a 'fileno' attribute.
# We patch them to os.devnull to prevent fatal crashes on startup.
if sys.stdout is None: sys.stdout = open(os.devnull, 'w')
if sys.stderr is None: sys.stderr = open(os.devnull, 'w')
if sys.stdin is None:  sys.stdin = open(os.devnull, 'r')
import asyncio
import contextlib
import urllib.request
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import argparse
import importlib.util
import site
import subprocess
import tempfile
import webbrowser
from dataclasses import dataclass
try:
    from pydub import AudioSegment as _AudioSegment
    _PYDUB_AVAILABLE = True
except ImportError:
    _AudioSegment = None  # type: ignore
    _PYDUB_AVAILABLE = False

# ─── pywebview storage path (before any webview import) ──────────────────────
# Disable pywebview's private mode so the WebSocket auth token persists
# across page loads. Actual storage is placed in DATA_DIR (set after imports).

# ─── Fast JSON (orjson → stdlib fallback) ─────────────────────────────────────
try:
    import orjson as _json_lib  

    def _dumps(obj) -> str:
        return _json_lib.dumps(obj).decode()

    def _loads(s: str | bytes):
        return _json_lib.loads(s)

except ImportError:
    _dumps = json.dumps   
    _loads = json.loads   

# ─── Runtime dependency check ─────────────────────────────────────────────────

RUNTIME_DEPENDENCIES = [
    ("fastapi",   "fastapi"),
    ("uvicorn",   "uvicorn[standard]"),
    ("requests",  "requests"),
    ("psutil",    "psutil"),
    ("speedtest", "speedtest-cli"),
    ("spotipy",   "spotipy"),
    ("PIL",       "Pillow"),
    ("pynvml",    "nvidia-ml-py"),
    # pywebview and pystray are Arch system packages (python-pywebview, python-pystray);
    # pip-install fallback works on other distros/Windows.
    ("webview",   "pywebview"),
    ("pystray",   "pystray"),
]
if sys.platform.startswith("linux"):
    RUNTIME_DEPENDENCIES += [("evdev", "evdev"), ("gi", "PyGObject")]
elif sys.platform.startswith("win"):
    RUNTIME_DEPENDENCIES += [("pycaw", "pycaw"), ("comtypes", "comtypes")]


def _is_linux_cli():
    if not sys.platform.startswith("linux"):
        return False
    if "--server-only" in sys.argv or "--reload" in sys.argv:
        return True
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))



def _show_popup(title, msg):
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
        messagebox.showinfo(title, msg, parent=r); r.destroy()
        return True
    except Exception:
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x40)
            return True
        except Exception:
            pass
    if sys.platform.startswith("linux") and not _is_linux_cli():
        for tool in (["zenity", "--info", "--title", title, "--text", msg],
                     ["kdialog", "--title", title, "--msgbox", msg]):
            if shutil.which(tool[0]):
                try: subprocess.run(tool, check=False); return True
                except Exception: pass
    return False


def ensure_runtime_dependencies():
    missing = [pkg for mod, pkg in RUNTIME_DEPENDENCIES
               if importlib.util.find_spec(mod) is None]
    if not missing:
        return

    msg = (f"Touch Dashboard is missing required dependencies:\n\n"
           f"{chr(10).join(f'  - {p}' for p in missing)}\n\n"
           f"Please install them using your system package manager or `pip install -r requirements.txt`.")
    print(msg, flush=True)
    _show_popup("Touch Dashboard - Missing Dependencies", msg)
    raise SystemExit(f"Missing deps: {missing}")


ensure_runtime_dependencies()

# ─── Third-party imports (after dep check) ────────────────────────────────────

import requests
import psutil
import speedtest as speedtest_lib

# Monkey-patch speedtest-cli to prefer c.speedtest.net for server lists.
# This fixes the issue where the PyPI version picks far away servers.
_orig_build_request = speedtest_lib.build_request
def _patched_build_request(*args, **kwargs):
    args = list(args)
    if args and isinstance(args[0], str) and "www.speedtest.net/speedtest-servers" in args[0]:
        args[0] = args[0].replace("www.speedtest.net", "c.speedtest.net")
    elif "url" in kwargs and isinstance(kwargs["url"], str) and "www.speedtest.net/speedtest-servers" in kwargs["url"]:
        kwargs["url"] = kwargs["url"].replace("www.speedtest.net", "c.speedtest.net")
    return _orig_build_request(*args, **kwargs)
speedtest_lib.build_request = _patched_build_request
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.websockets import WebSocketState
from starlette.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager
import uvicorn

logger = logging.getLogger(__name__)

# Version Management
VERSION = "v0.4.0" # Fallbacks
REQUIRED_APK_VERSION = "v0.3.0"

version_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.json")
try:
    if os.path.exists(version_file):
        with open(version_file, "r") as f:
            v_data = json.load(f)
            VERSION = v_data.get("desktop_version", VERSION)
            REQUIRED_APK_VERSION = v_data.get("required_apk_version", REQUIRED_APK_VERSION)
except Exception as e:
    logger.error(f"Failed to load version.json: {e}")

update_available = False
latest_version = ""
update_apk_url = ""
update_notes = ""



try:
    from PIL import Image, ImageDraw    
except ImportError:
    Image = ImageDraw = None

# Desktop window / tray (imported lazily in launch_desktop to avoid
# initialising the GTK/WebKit2 subsystem in server-only mode).
import desktop as _desktop_module

import warnings
try:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning, message=".*pynvml.*")
        import pynvml  
except ImportError:
    pynvml = None

# ─── Local modules ─────────────────────────────────────────────────────────────

from audio_system import AudioSystem
from discord_ipc import DiscordIPC
from macro_system import MacroSystem, AppEnumerator
import media as media_module
import security

import lock_monitor as _lock_monitor_mod

if sys.platform.startswith("linux"):
    import mouse_battery as _mouse_battery_mod
    import mpris_monitor as _mpris_monitor_mod

# ─── Paths & config ────────────────────────────────────────────────────────────

BASE_DIR     = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                               else os.path.abspath(__file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
APP_ICON_PATH = os.path.join(RESOURCE_DIR, "static", "favicon.ico")
WINDOWS_APP_USER_MODEL_ID = "TouchDashboard.TouchDashboard"

if sys.platform.startswith("win"):
    DATA_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "touch-dashboard")
else:
    DATA_DIR = os.path.join(os.path.expanduser("~"), ".config", "touch-dashboard")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE         = os.path.join(DATA_DIR, "config.json")
WEATHER_CACHE_FILE  = os.path.join(DATA_DIR, "weather_cache.json")
SPOTIFY_CACHE_FILE  = os.path.join(DATA_DIR, "spotify_token.json")
SOUNDS_DIR          = os.path.join(DATA_DIR, "sounds")
os.makedirs(SOUNDS_DIR, exist_ok=True)

# Until v0.11.11 the Spotify token was DATA_DIR/.cache: the very path the
# soundboard's cache folder below lives in. Once that folder existed (v0.8.1),
# no Spotify sign-in could be saved; a token file there would instead have
# stopped the folder from being made. Move such a file to the token's own name.
_OLD_SPOTIFY_CACHE = os.path.join(DATA_DIR, ".cache")
if os.path.isfile(_OLD_SPOTIFY_CACHE):
    try:
        if os.path.exists(SPOTIFY_CACHE_FILE):
            os.remove(_OLD_SPOTIFY_CACHE)
        else:
            os.replace(_OLD_SPOTIFY_CACHE, SPOTIFY_CACHE_FILE)
            os.chmod(SPOTIFY_CACHE_FILE, 0o600)
    except OSError:
        pass

NORMALIZED_SOUNDS_CACHE_DIR = os.path.join(DATA_DIR, ".cache", "normalized_sounds")
os.makedirs(NORMALIZED_SOUNDS_CACHE_DIR, exist_ok=True)

CONFIG_LOCK = threading.RLock()

DEFAULT_CONFIG: dict = {
    "weather_api": "", "weather_city": "",
    "spot_id": "", "spot_secret": "",
    "disc_id": "", "disc_secret": "",
    "disc_enabled": True,
    "disc_vc_enabled": True,
    "disc_sort_speakers": True,
    "show_mouse_batt": True,
    "show_hw_stats": True,
    "spot_enabled": True,
    "weather_enabled": True,
    "audio_enabled": True,
    "sb_enabled": True,
    "sb_normalization_enabled": False,
    "sb_normalization_db": 89.0,
    "soundboard_volume": 100,
    "audio_names": {},
    "hidden_audio_devices": [],   # raw sink names the output switch skips
    "audio_order": [],            # raw sink names, first to last; others follow at the bottom
    "soundpad_buttons": [],
    "local_buttons": [],
    "sounds_path": "",
    "macros": [],
    "theme": "graphite",      # preset id from static/themes.js
    "theme_ground": "#000000",  # that preset's ground, painted by the desktop window before load
    "play_key_cover": True,   # the play key takes the cover's colour; off, the theme's accent
    "idle_dim_min": 5,        # burn-in care on tablets: dim the home screen after this many idle minutes (0: never)
    "idle_clock_min": 15,     # ... and show the full-screen clock after this many (0: never)
    "device_av_controls": True,  # paired tablets may toggle Discord camera / screen share
}

THEME_ID_RE    = re.compile(r"[a-z0-9-]{1,32}")
SERVER_FEATURES = ["disc_voice", "pairing", "media_seek", "media_source", "media_like",
                   "media_result"]   # see _make_config_sync
HEX_COLOUR_RE  = re.compile(r"#[0-9a-fA-F]{6}")
ITEM_ID_RE     = re.compile(r"[A-Za-z0-9_-]{1,64}")   # macro "id" / soundboard pad "uid"
APP_VERSION_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,4}")   # the Android app's version, from auth


def _with_ids(items, key: str) -> list:
    """Give every dict item a unique, well-formed `key`; everything else is kept.

    Devices run macros and pads by this id, never by their command or path.
    """
    if not isinstance(items, list):
        return []
    seen: set[str] = set()
    out = []
    for item in items:
        if isinstance(item, dict):
            value = item.get(key)
            if not (isinstance(value, str) and ITEM_ID_RE.fullmatch(value)) or value in seen:
                item = {**item, key: secrets.token_hex(8)}
            seen.add(item[key])
        out.append(item)
    return out


# Modules that no longer exist (clip recording); macros naming them are dropped
# when the config loads or is saved.
RETIRED_MODULES = frozenset({"app_clip"})


def _live_macros(items):
    """Drop macros that name a retired module: they would be keys that do nothing."""
    if not isinstance(items, list):
        return items
    return [m for m in items if not (isinstance(m, dict) and m.get("type") in ("premade", "plugin")
                                     and m.get("action_data") in RETIRED_MODULES)]


def _with_item_ids(cfg: dict) -> dict:
    return {**cfg, "macros": _with_ids(_live_macros(cfg.get("macros")), "id"),
            "local_buttons": _with_ids(cfg.get("local_buttons"), "uid"),
            "soundpad_buttons": _with_ids(cfg.get("soundpad_buttons"), "uid")}


def _migrate_config():
    for old, new in (
        (os.path.join(BASE_DIR, "config.json"),       CONFIG_FILE),
        (os.path.join(BASE_DIR, ".cache"),             SPOTIFY_CACHE_FILE),   # a file there, from very old installs
        (os.path.join(BASE_DIR, "weather_cache.json"), WEATHER_CACHE_FILE),
    ):
        if os.path.isfile(old) and not os.path.exists(new):
            try: shutil.copy2(old, new)
            except Exception: pass
        elif os.path.exists(old) and old == os.path.join(BASE_DIR, "config.json"):
            try:
                with open(old) as f: old_data = json.load(f)
                with open(new)  as f: new_data = json.load(f)
                for k, v in old_data.items():
                    if k not in new_data or not new_data[k]:
                        new_data[k] = v
                with open(new, "w") as f: json.dump(new_data, f, indent=2)
            except Exception: pass


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config():
    with CONFIG_LOCK:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp, CONFIG_FILE)


_migrate_config()
config = load_config()
if (_cfg_with_ids := _with_item_ids(config)) != config:
    config = _cfg_with_ids
    save_config()

# ─── Logging ───────────────────────────────────────────────────────────────────

_log_file = os.path.join(DATA_DIR, "server.log")
logging.basicConfig(
    filename=_log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

# Enable the fault handler so native crashes (SIGSEGV / access violations)
# write a Python stack dump to server.log instead of silently killing the process.
# This catches crashes in _ctypes.pyd, COM vtable calls, Qt/Chromium etc.
import faulthandler as _faulthandler
try:
    _fh_file = open(_log_file, "a", buffering=1)  # line-buffered append
    _faulthandler.enable(file=_fh_file, all_threads=True)
    logger.info("faulthandler enabled → %s", _log_file)
except Exception as _fh_err:
    logger.warning("faulthandler could not be enabled: %s", _fh_err)


# ─── Access control (see security.py) ──────────────────────────────────────────

AUTH_TIMEOUT_S          = 5.0    # the first message must be auth or pair_request
WS_CLOSE_POLICY         = 1008
WS_CLOSE_UNAUTHORIZED   = 4401   # token missing, invalid or revoked: the client pairs again
WS_CLOSE_TRY_LATER      = 1013   # too many unauthenticated sockets open
# Largest WebSocket message uvicorn accepts (its default is 16 MiB). The biggest
# real one, a save_config with every macro and pad, is a few kilobytes.
WS_MAX_MESSAGE_BYTES    = 1 << 20
# --reload re-imports this module in a child process; the port reaches it through the environment.
SERVER_PORT = int(os.environ.get("TD_PORT", "8888"))

paired_devices = security.PairedDevices(os.path.join(DATA_DIR, security.PAIRED_DEVICES_FILE))
pairing        = security.PairingRequests()
pending_sockets = security.PendingSockets()
local_token    = security.LocalControlToken(os.path.join(DATA_DIR, security.LOCAL_TOKEN_FILE))
# The LAN addresses are filled in by lifespan(); get_lan_ips is defined below.
origin_policy  = security.OriginPolicy(SERVER_PORT, lambda: get_lan_ips())


# ─── Shared state ──────────────────────────────────────────────────────────────

REQ_SESSION = requests.Session()

global_sp_oauth: SpotifyOAuth | None = None
disc_ipc_instance: DiscordIPC | None = None
uvicorn_server: uvicorn.Server | None = None

# NVML
has_nvml = False
nvml_handle = None
if pynvml:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        try:
            pynvml.nvmlInit()
            nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            has_nvml = True
        except Exception as e:
            logger.debug(f"NVML init: {e}")

# Misc media state
force_media_update   = False
spotify_cache: dict | None = None
last_spotify_check   = 0.0
# The source the card shows; every media key goes to it (media.pick_source).
media_shown: dict | None   = None
media_pin: dict | None     = None    # {"id", "at"}: a source picked on the chip
media_sources: list[dict]  = []      # every playing or paused source, for the chip
spotify_like = {"item": "", "liked": None}   # the shown Spotify item, saved in the library?
spotify_recheck_at = 0.0             # read the Web API again then: its first reply after a command is often stale
spotify_reader: asyncio.Task | None = None   # the Web API read in flight (_read_spotify)
mpris_mon = None                     # MprisMonitor (Linux); None → playerctl is polled
last_audio_devs: list = []
last_weather_data    = {"temp": "--", "desc": "--", "timestamp": 0}
upower_monitor       = None   # UPowerMouseMonitor instance (Linux only)
lock_monitor_instance = None  # LockMonitor instance (all platforms)
# NOTE: asyncio primitives MUST be created inside a running event loop.
# We declare them as None here and initialise them inside lifespan().
weather_update_event: asyncio.Event | None = None
last_host_url        = "127.0.0.1:8888"
speedtest_lock: asyncio.Lock | None = None
# Signalled by a local play_clip to make pipewire_auto_router run immediately.
sb_route_event: asyncio.Event | None = None

# Template cache (mtime-based)
_tmpl_cache = {"mtime": 0.0, "html": "", "etag": ""}

def _load_template() -> tuple[str, str]:
    path  = os.path.join(RESOURCE_DIR, "templates", "index.html")
    mtime = os.path.getmtime(path)
    if _tmpl_cache["mtime"] != mtime:
        with open(path, encoding="utf-8") as f:
            _tmpl_cache["html"]  = f.read()
        _tmpl_cache["mtime"] = mtime
        _tmpl_cache["etag"]  = f'"{int(mtime * 1000):x}-{len(_tmpl_cache["html"]):x}"'
    return _tmpl_cache["html"], _tmpl_cache["etag"]

# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_os_target() -> str:
    return "windows" if sys.platform.startswith("win") else "linux"


def _is_lan_ipv4(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
        return (ip.version == 4 and not ip.is_loopback
                and not ip.is_unspecified and not ip.is_link_local
                and not ip.is_multicast)
    except ValueError:
        return False


def get_lan_ip() -> str:
    return (get_lan_ips() or ["LAN unavailable"])[0]


def get_lan_ips() -> list[str]:
    """This PC's LAN IPv4 addresses, private ones first (the first is the tray's)."""
    candidates: list[str] = []
    for target in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 80))
                candidates.append(s.getsockname()[0])
        except Exception:
            pass
    try:
        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    candidates.append(addr.address)
    except Exception:
        pass
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    valid = []
    seen: set = set()
    for ip in candidates:
        if ip not in seen and _is_lan_ipv4(ip):
            seen.add(ip)
            valid.append(ip)
    private = [ip for ip in valid if ipaddress.ip_address(ip).is_private]
    return private + [ip for ip in valid if ip not in private]


# get_lan_ip() opens sockets and may block on a hostname lookup, and config_sync
# needs it on every save; the address rarely changes, so cache it briefly.
LAN_IP_TTL = 30.0
_lan_ip_cache: dict = {"ip": "", "ts": 0.0}


def get_lan_ip_cached() -> str:
    now = time.monotonic()
    if not _lan_ip_cache["ip"] or now - _lan_ip_cache["ts"] > LAN_IP_TTL:
        _lan_ip_cache["ip"] = get_lan_ip()
        _lan_ip_cache["ts"] = now
    return _lan_ip_cache["ip"]


class _SpotifyTokenFile(CacheFileHandler):
    """The token file, written whole (tmp, then os.replace) and 0600 from the
    start: it holds the access and refresh tokens."""

    def save_token_to_cache(self, token_info):
        tmp = self.cache_path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(token_info, f)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            logger.warning(f"Couldn't save the Spotify sign-in to {self.cache_path}: {e}")


def get_sp_oauth() -> SpotifyOAuth | None:
    global global_sp_oauth
    if not config.get("spot_id") or not config.get("spot_secret"):
        return None
    if global_sp_oauth is None:
        global_sp_oauth = SpotifyOAuth(
            client_id=config["spot_id"],
            client_secret=config["spot_secret"],
            redirect_uri="http://127.0.0.1:8888/callback",
            # The library scopes are for the like key; a token from before
            # them keeps working for playback (media.spotify_token).
            scope="user-read-playback-state user-modify-playback-state "
                  "user-library-read user-library-modify",
            open_browser=False,
            cache_handler=_SpotifyTokenFile(cache_path=SPOTIFY_CACHE_FILE),
        )
    return global_sp_oauth


def resolve_sound_path(filename: str) -> str | None:
    sounds_dir = config.get("sounds_path", "")
    if not sounds_dir:
        return None
    base = os.path.realpath(sounds_dir)
    candidate = os.path.realpath(os.path.join(base, filename))
    if (candidate != base and candidate.startswith(base + os.sep)
            and os.path.isfile(candidate)):
        return candidate
    return None

def get_normalized_sound_path(original_filepath: str) -> str:
    """Returns the path to the cached normalized sound file, generating it if necessary."""
    if not config.get("sb_normalization_enabled", False):
        return original_filepath

    if not _PYDUB_AVAILABLE:
        logger.warning("pydub is not installed — sound normalization disabled. Run: pip install 'pydub>=0.25.1' audioop-lts")
        return original_filepath
    
    target_db = float(config.get("sb_normalization_db", 89.0))
    # Soundpad uses 89 dB SPL as default, which maps roughly to -14 dBFS digitally.
    target_dbfs = target_db - 103.0 
    
    filename = os.path.basename(original_filepath)
    # create a hash of the filepath and the target db to avoid collisions and allow changing target db
    import hashlib
    hash_str = hashlib.md5(f"{original_filepath}_{target_dbfs}".encode()).hexdigest()[:8]
    cache_filename = f"{hash_str}_{filename}"
    cache_filepath = os.path.join(NORMALIZED_SOUNDS_CACHE_DIR, cache_filename)
    
    try:
        # Check if cache exists and is newer than original
        if os.path.exists(cache_filepath) and os.path.getmtime(cache_filepath) >= os.path.getmtime(original_filepath):
            return cache_filepath
            
        logger.info(f"Normalizing '{filename}' to {target_dbfs} dBFS...")
        audio: _AudioSegment = _AudioSegment.from_file(original_filepath) # type: ignore
        change_in_dbfs = target_dbfs - audio.dBFS # type: ignore
        normalized_audio = audio.apply_gain(change_in_dbfs) # type: ignore
        
        # Determine format for export
        ext = os.path.splitext(filename)[1].lower().replace('.', '')
        if ext == 'm4a': ext = 'mp4'
        if ext not in ['mp3', 'wav', 'ogg', 'flac', 'mp4']:
            ext = 'wav'
            cache_filepath = cache_filepath + '.wav'
            
        normalized_audio.export(cache_filepath, format=ext)
        return cache_filepath
    except Exception as e:
        logger.error(f"Failed to normalize audio '{filename}': {e}")
        return original_filepath


def get_local_sounds() -> list[dict]:
    sounds_dir = config.get("sounds_path", "")
    if not sounds_dir or not os.path.isdir(sounds_dir):
        return []
    allowed = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}
    try:
        items = []
        for fname in os.listdir(sounds_dir):
            if os.path.splitext(fname)[1].lower() in allowed:
                items.append({"id": fname, "name": os.path.splitext(fname)[0]})
        return sorted(items, key=lambda x: x["name"].lower())
    except Exception as e:
        logger.error(f"Scan sounds dir: {e}")
        return []


os_disc_mute = False
os_disc_deaf = False

def restart_discord_ipc():
    global disc_ipc_instance
    if disc_ipc_instance:
        disc_ipc_instance.running = False
        disc_ipc_instance.close()
        disc_ipc_instance = None
    if (config.get("disc_enabled", True)
            and config.get("disc_id") and config.get("disc_secret")):

        def _save_token(token: str):
            with CONFIG_LOCK:
                config["disc_token"] = token
                save_config()

        disc_ipc_instance = DiscordIPC(
            client_id=config["disc_id"],
            client_secret=config["disc_secret"],
            access_token=config.get("disc_token", ""),
            config_save_fn=_save_token,
        )
        # Wire up state-change trigger
        def _on_disc_change():
            if main_event_loop and sys_data_trigger:
                main_event_loop.call_soon_threadsafe(sys_data_trigger.set)

        def _on_auth_error(_unsigned_url: str):
            # The IPC client's URL carries no OAuth state; open ours instead.
            auth_url = _discord_auth_url()
            if main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    asyncio.to_thread(webbrowser.open, auth_url),
                    main_event_loop
                )
            else:
                threading.Thread(target=webbrowser.open, args=(auth_url,), daemon=True).start()

        disc_ipc_instance.on_state_change = _on_disc_change
        disc_ipc_instance.on_auth_error = _on_auth_error
        threading.Thread(target=disc_ipc_instance.loop, daemon=True,
                         name="discord-ipc").start()


# ─── WebSocket manager ─────────────────────────────────────────────────────────

@dataclass(eq=False)
class Client:
    """An authenticated socket. Only these ever receive state."""
    ws: WebSocket
    role: str              # "admin" (loopback + own origin) or "device" (paired token)
    ip: str
    device_id: str = ""
    name: str = ""
    app: str | None = None   # the Android app's version (sent since v0.11.12)
    in_app: bool = False     # an Android WebView: the app, even one too old to say its version

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class ConnectionManager:
    def __init__(self):
        self._clients: list[Client] = []

    def has_clients(self) -> bool:
        return bool(self._clients)

    def add(self, client: Client):
        self._clients.append(client)

    def remove(self, client: Client):
        if client in self._clients:
            self._clients.remove(client)

    def online_device_apps(self) -> dict[str, tuple[bool, str | None]]:
        """device_id → (in the Android app, its version or None when it is too old to say)."""
        return {c.device_id: (c.in_app, c.app) for c in self._clients if c.device_id}

    def device_clients(self, device_id: str) -> list[Client]:
        return [c for c in self._clients if c.device_id == device_id]

    async def broadcast(self, msg: dict, admin_only: bool = False):
        await self.broadcast_by_role({"admin": msg} if admin_only else {"admin": msg, "device": msg})

    async def broadcast_by_role(self, msgs: dict[str, dict]):
        """Send msgs[client.role] to each client; roles without an entry get nothing."""
        targets = [c for c in self._clients if c.role in msgs]
        if not targets:
            return
        payloads = {role: _dumps(m) for role, m in msgs.items()}

        async def _send(c: Client):
            try:
                await c.ws.send_text(payloads[c.role])
                return None
            except Exception:
                return c

        for stale in await asyncio.gather(*[_send(c) for c in targets]):
            if stale is not None:
                self.remove(stale)


ws_manager       = ConnectionManager()
main_event_loop: asyncio.AbstractEventLoop | None = None
sys_data_trigger: asyncio.Event | None            = None

# ─── Background tasks ──────────────────────────────────────────────────────────

AUDIO_POLL_INTERVAL   = 1.0   # wpctl status + get-volume (3 processes on Linux)
SPOTIFY_POLL_INTERVAL = 3.0   # Spotify Web API round trip
LOCAL_MEDIA_INTERVAL  = 1.0   # playerctl; Discord speaking events must not each spawn one
BURST_INTERVAL        = 0.2   # while a track change is settling (MPRIS art)
GPU_POLL_INTERVAL     = 2.0
LOAD_SAMPLE_INTERVAL  = 1.0   # CPU/RAM sample; one sparkline point on the client
TRIGGER_COALESCE_S    = 0.025
FORCED_MEDIA_DELAY    = 0.4   # let Spotify / the player apply a just-sent command
SPOTIFY_RECHECK_S     = 1.5   # ...and read Spotify once more after this


def _read_batt_windows() -> dict:
    """Windows fallback: read the legacy g502_battery.txt file."""
    path = os.path.join(os.environ.get("TEMP", ""), "g502_battery.txt")
    try:
        with open(path) as f:
            # File may contain just a number like "73" or "73%"
            pct_str = f.read().strip().replace("%", "").strip()
        return {"pct": float(pct_str), "state": "unknown", "model": "", "vendor": ""}
    except (OSError, ValueError):
        return {"pct": None, "state": "unknown", "model": "", "vendor": ""}


def _snapshot_voice_channel(vc: dict | None) -> dict | None:
    """Copy the IPC thread's live dict so payload comparison sees its changes.

    The Discord thread mutates users[uid]["speaking"] in place; holding the live
    object in last_sys_data_payload made every speaking/join/leave event compare
    equal and wait for an unrelated field (CPU %) to change before broadcasting.
    list() and dict() are single C calls, so this is safe against that thread.
    """
    if not vc:
        return None
    snap = dict(vc)
    snap["users"] = {uid: dict(u) for uid, u in list(vc.get("users", {}).items())}
    return snap


# An <img> cannot send an Authorization header, so the cover URL is its own
# credential: sys_data (authenticated clients only) carries a URL signed for
# the cover playing now, and /api/local_art serves nothing else.
_ART_KEY = secrets.token_bytes(32)
LOCAL_ART_PREFIX = "/api/local_art?h="
_current_art_h = ""


def _sign_art(h: str) -> str:
    return hmac.new(_ART_KEY, h.encode(), "sha256").hexdigest()[:32]


def _signed_media(media: dict | None) -> dict | None:
    global _current_art_h
    url = media.get("art_url") if media else None
    if not (isinstance(url, str) and url.startswith(LOCAL_ART_PREFIX)):
        _current_art_h = ""
        return media
    _current_art_h = url.removeprefix(LOCAL_ART_PREFIX)
    return {**media, "art_url": f"{url}&k={_sign_art(_current_art_h)}"}


async def _fetch_spotify(forced: bool) -> dict | None:
    if forced:
        await asyncio.sleep(FORCED_MEDIA_DELAY)
    return await asyncio.to_thread(media_module.get_spotify_api_meta, get_sp_oauth(), REQ_SESSION)


async def _read_spotify(forced: bool) -> None:
    """One Web API read, off hardware_loop's tick, pushed when it lands. In the
    tick, every key on a Spotify source held the local app's new state (the
    MPRIS monitor has it at once) for FORCED_MEDIA_DELAY plus Spotify's round
    trip, and every 3 s poll held Discord, volume and media events as long."""
    global spotify_cache, last_spotify_check
    try:
        spotify_cache = await _fetch_spotify(forced)
    except Exception as e:   # network, Spotify; the next poll tries again
        logger.debug(f"hardware_loop spotify poll failed: {e}")
    last_spotify_check = time.monotonic()
    if sys_data_trigger:
        sys_data_trigger.set()


async def _fetch_local_media(forced: bool) -> list[dict]:
    if forced:
        await asyncio.sleep(FORCED_MEDIA_DELAY)
    if sys.platform.startswith("win"):
        return await media_module.get_windows_players_async()
    return await asyncio.to_thread(media_module.poll_mpris_players)


async def _refresh_like(item_type: str, item_id: str) -> None:
    liked = await asyncio.to_thread(media_module.spotify_liked, get_sp_oauth(), item_type, item_id, REQ_SESSION)
    if spotify_like["item"] == item_id:
        spotify_like["liked"] = liked
        if sys_data_trigger:
            sys_data_trigger.set()


def _now_playing(art_url: str) -> dict | None:
    """sys_data's `spotify`: the shown source, its keys and the chip's list."""
    t = media_shown
    if t is None:
        # Older pages read Settings' Spotify sign-in state from this.
        return spotify_cache if spotify_cache and spotify_cache.get("status") == "Auth_Required" else None
    liked = None
    if t.get("can_like") and t.get("item_id"):
        if spotify_like["item"] != t["item_id"]:
            spotify_like.update(item=t["item_id"], liked=None)
            asyncio.create_task(_refresh_like(t.get("item_type", "track"), t["item_id"]))
        liked = bool(spotify_like["liked"])   # unknown yet: shown unlit
    elif t.get("can_like") and t.get("api_behind"):
        liked = False                         # the Web API is still on the last track
    # Spotify plays but its Web API cannot like (not signed in, or signed in
    # before the library scopes): the key is still there, and signs in.
    auth = media_module.spotify_auth
    like_signin = (liked is None and t["app"] == "spotify"
                   and (auth["state"] == "required" or (auth["state"] == "ok" and not auth["likes"])))
    return {
        **media_module.public_media(t, art_url),
        "liked": liked,
        "like_signin": like_signin,
        "sources": [{"id": s["id"], "name": s["name"], "status": s["status"]} for s in media_sources],
        "source_id": t["id"],
        "pinned": media_pin is not None,
    }


# OAuth "state": a sign-in callback is accepted only with the value this server
# put in the authorize URL, so a web page cannot make the PC's browser finish
# a sign-in into someone else's Spotify or Discord account (login CSRF).
OAUTH_STATE_TTL_S = 600.0
DISCORD_SCOPES = ("rpc rpc.notifications.read rpc.voice.read rpc.video.read rpc.screenshare.read "
                  "rpc.activities.write rpc.screenshare.write rpc.video.write rpc.voice.write")
_oauth_states: dict[str, tuple[str, float]] = {}   # provider -> (state, expires)
_oauth_lock = threading.Lock()                     # the Discord IPC thread builds URLs too


def _oauth_state(provider: str) -> str:
    """The pending state for this provider, kept until it is used or expires."""
    now = time.monotonic()
    with _oauth_lock:
        state, expires = _oauth_states.get(provider, ("", 0.0))
        if not state or now >= expires:
            state = secrets.token_urlsafe(24)
            _oauth_states[provider] = (state, now + OAUTH_STATE_TTL_S)
        return state


def _spend_oauth_state(provider: str, presented) -> bool:
    """True once for the pending state; a wrong value does not cancel the real one."""
    with _oauth_lock:
        state, expires = _oauth_states.get(provider, ("", 0.0))
        if not (state and time.monotonic() < expires and isinstance(presented, str)
                and hmac.compare_digest(presented.encode(), state.encode())):
            return False
        del _oauth_states[provider]
        return True


def _discord_auth_url() -> str:
    params = urllib.parse.urlencode({
        "client_id": config.get("disc_id", ""),
        "response_type": "code",
        "redirect_uri": "http://127.0.0.1:8888/disc_callback",
        "scope": DISCORD_SCOPES,
        "state": _oauth_state("discord"),
    })
    return f"https://discord.com/oauth2/authorize?{params}"


def _device_sys_data(msg: dict) -> dict:
    """sys_data for paired devices: without the Discord sign-in URL and its OAuth state."""
    discord = msg["data"].get("discord")
    if not discord or "auth_url" not in discord:
        return msg
    return {**msg, "data": {**msg["data"], "discord": {k: v for k, v in discord.items() if k != "auth_url"}}}


def _discord_state() -> dict:
    disc_has_token = bool(config.get("disc_token", ""))
    disc_has_creds = bool(config.get("disc_id")) and bool(config.get("disc_secret"))
    state: dict = {
        "mute": False, "deaf": False,
        "auth_required": disc_has_creds and not disc_has_token,
        "auth_url": "",
        "connected": False,
        "voice_supported": False,
        "authorized": disc_has_token,
    }
    if disc_has_creds and not disc_has_token:
        state["auth_url"] = _discord_auth_url()
    ipc = disc_ipc_instance
    if ipc:
        state.update({
            "connected":       ipc.connected,
            "voice_supported": ipc.voice_supported,
            "auth_pending":    ipc.auth_pending,
            "vesktop_ipc_warning": ipc.vesktop_ipc_warning,
        })
        if ipc.auth_pending:
            state["authorized"] = False
            state["auth_url"] = _discord_auth_url()
        if ipc.connected:
            voice = getattr(ipc, 'voice_state', {})
            state["mute"] = voice.get('mute', False)
            state["deaf"] = voice.get('deaf', False)
            state["video"] = voice.get('video', False)
            state["screenshare"] = voice.get('screenshare', False)
            state["voice_channel"] = _snapshot_voice_channel(ipc.voice_channel)
    return state


async def hardware_loop():
    global last_audio_devs, spotify_reader
    global force_media_update, main_event_loop, sys_data_trigger
    global media_shown, media_pin, media_sources, spotify_recheck_at

    main_event_loop  = asyncio.get_running_loop()
    sys_data_trigger = asyncio.Event()
    loop, trigger = main_event_loop, sys_data_trigger
    # The volume worker read back a level it applied: push it now, not on the next poll.
    AudioSystem.on_volume_applied = lambda: loop.call_soon_threadsafe(trigger.set)

    last_gpu_check   = 0.0
    last_load_check  = 0.0
    load_cache       = {"cpu": 0.0, "ram": 0.0}
    load_seq         = 0
    gpu_cache        = ""
    last_audio_check = 0.0
    last_local_check = 0.0
    local_players: list[dict] = []
    audio_cache      = {
        "spk": {"vol": 0, "muted": False},
        "mic": {"vol": 0, "muted": False},
        "active_dev": "NONE",
    }
    # mouse_batt is a structured dict; None pct means no device found.
    batt_cache: dict = {"pct": None, "state": "unknown", "model": "", "vendor": ""}
    last_sys_data_payload = {}
    is_linux = sys.platform.startswith("linux")
    spotify_reread = False   # a key was pressed while a Web API read was in flight

    while True:
        if not ws_manager.has_clients():
            await asyncio.sleep(2.0)
            continue

        curr   = time.monotonic()
        # Taken now: a key pressed while this tick awaits forces the next one.
        forced, force_media_update = force_media_update, False
        burst  = is_linux and media_module.is_mpris_burst_active()
        local_interval  = BURST_INTERVAL if burst else LOCAL_MEDIA_INTERVAL
        monitor = mpris_mon if mpris_mon is not None and mpris_mon.running else None

        # Everything due this tick runs concurrently; each is a subprocess,
        # an HTTP call or a COM query, so the tick costs the slowest, not the sum.
        jobs: dict = {}
        if curr - last_audio_check >= AUDIO_POLL_INTERVAL:
            jobs["audio"] = asyncio.to_thread(AudioSystem.poll_all, config.get("audio_names", {}))
            if sys.platform.startswith("win"):
                jobs["batt"] = asyncio.to_thread(_read_batt_windows)
        recheck = bool(spotify_recheck_at) and curr >= spotify_recheck_at
        if forced or recheck or spotify_reread or curr - last_spotify_check > SPOTIFY_POLL_INTERVAL:
            if spotify_reader is None or spotify_reader.done():
                spotify_reader = asyncio.create_task(_read_spotify(forced or spotify_reread))
                spotify_reread = False
                if recheck:
                    spotify_recheck_at = 0.0
            elif forced:
                spotify_reread = True
        # The MPRIS monitor is read in memory below; polled sources are fetched.
        if monitor is None and (forced or curr - last_local_check >= local_interval):
            jobs["local"] = _fetch_local_media(forced)
        if has_nvml and nvml_handle and curr - last_gpu_check > GPU_POLL_INTERVAL:
            assert pynvml is not None
            jobs["gpu"] = asyncio.to_thread(pynvml.nvmlDeviceGetUtilizationRates, nvml_handle)
        if is_linux and upower_monitor is not None:
            # Event-driven: read every tick so a battery signal shows at once.
            # Off the loop: with no UPower mouse it falls back to scanning sysfs.
            jobs["batt"] = asyncio.to_thread(upower_monitor.get_state)

        results = dict(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)))
        for name, value in results.items():
            if isinstance(value, Exception):
                logger.debug(f"hardware_loop {name} poll failed: {value}")

        audio_data = results.get("audio")
        if "audio" in jobs:
            last_audio_check = curr
        if isinstance(audio_data, dict):
            curr_sinks = AudioSystem.ordered(audio_data["sinks"], config.get("audio_order"))
            # A device added or removed, or the order changed in Audio routing.
            if [s["raw_name"] for s in curr_sinks] != [s["raw_name"] for s in last_audio_devs]:
                last_audio_devs = curr_sinks
                await ws_manager.broadcast({"type": "hw_scan_results", "data": curr_sinks})
            audio_cache = {
                "spk": audio_data["spk"],
                "mic": audio_data["mic"],
                "active_dev": audio_data["active_sink_name"],
            }
            # Windows polls levels without get_state(); ordered by when this poll began.
            AudioSystem.remember("@DEFAULT_AUDIO_SINK@", audio_data["spk"], curr)
            AudioSystem.remember("@DEFAULT_AUDIO_SOURCE@", audio_data["mic"], curr)
        if isinstance(results.get("batt"), dict):
            batt_cache = results["batt"]
        if "local" in jobs:
            local_players = [] if isinstance(results["local"], Exception) else results["local"]
            last_local_check = curr
        if monitor is not None:
            local_players = media_module.from_monitor(monitor.players())
        if "gpu" in jobs:
            gpu = results["gpu"]
            gpu_cache = "" if isinstance(gpu, Exception) else str(gpu.gpu)
            last_gpu_check = curr

        # The source to show and control: a pinned one, else the latest to start.
        spot = spotify_cache if spotify_cache and spotify_cache.get("status") in media_module.LIVE else None
        media_sources = media_module.merge_spotify(media_module.dedupe(local_players), spot)
        media_pin     = media_module.resolve_pin(media_sources, media_pin)
        media_shown   = media_module.pick_source(media_sources, media_pin)
        # Off the loop: a file cover is stat()ed, a data: cover decoded once.
        art_url = await asyncio.to_thread(media_module.resolve_art, media_shown) if media_shown else ""
        media = _now_playing(art_url)

        # Load is a once-a-second sample: ticks woken by Discord events or volume
        # read-backs reuse it, so they broadcast only what actually changed and
        # the client's minute of load history stays a minute (load_seq).
        if curr - last_load_check >= LOAD_SAMPLE_INTERVAL:
            load_cache = {"cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent}
            load_seq += 1
            last_load_check = curr

        new_payload = {
            "cpu":        load_cache["cpu"],
            "ram":        load_cache["ram"],
            "load_seq":   load_seq,
            "gpu":        gpu_cache or None,
            # mouse_batt is a dict: {pct: float|null, state: str, model: str, vendor: str}
            "mouse_batt": batt_cache,
            # pc_locked: True when the host session's lock screen is active.
            "pc_locked":  lock_monitor_instance.get_state() if lock_monitor_instance else False,
            "spotify":    _signed_media(media),
            "spotify_auth": dict(media_module.spotify_auth),
            "discord":    _discord_state(),
            "audio": {
                # The latest level: the 1 s poll, or a fader's read-back since.
                "spk": AudioSystem.latest("@DEFAULT_AUDIO_SINK@", audio_cache["spk"]),
                "mic": AudioSystem.latest("@DEFAULT_AUDIO_SOURCE@", audio_cache["mic"]),
                "active_dev": audio_cache["active_dev"],
            },
        }

        if new_payload != last_sys_data_payload:
            last_sys_data_payload = new_payload
            msg = {"type": "sys_data", "data": new_payload}
            await ws_manager.broadcast_by_role({"admin": msg, "device": _device_sys_data(msg)})

        burst = is_linux and media_module.is_mpris_burst_active()
        sleep_time = BURST_INTERVAL if burst else 1.0
        try:
            await asyncio.wait_for(sys_data_trigger.wait(), timeout=sleep_time)
            # One Discord change arrives as several IPC frames (the reply,
            # VOICE_SETTINGS_UPDATE, the user's VOICE_STATE_UPDATE): let the
            # rest land so they go out as one sys_data, not three.
            await asyncio.sleep(TRIGGER_COALESCE_S)
            sys_data_trigger.clear()
        except asyncio.TimeoutError:
            pass


def _weather_from_owm(data: dict) -> dict:
    """OpenWeatherMap's current weather (metric) as the page gets it. temp and
    desc are on every screen; the rest feeds the full-screen clock and is None
    where OpenWeatherMap leaves it out. temp_min / temp_max are not passed on:
    they are the spread across the city right now, not a day's high and low."""
    main, wind, sun = data.get("main") or {}, data.get("wind") or {}, data.get("sys") or {}

    def whole(value, scale=1.0):
        return round(value * scale) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    city = data.get("name")
    return {
        "temp":       round(main["temp"]),
        "desc":       data["weather"][0]["description"].title(),
        "feels_like": whole(main.get("feels_like")),
        "humidity":   whole(main.get("humidity")),
        "wind_kmh":   whole(wind.get("speed"), 3.6),        # m/s in metric units
        "sunrise":    whole(sun.get("sunrise")),             # UTC epoch seconds
        "sunset":     whole(sun.get("sunset")),
        "tz":         whole(data.get("timezone")),           # the city's offset from UTC, seconds
        "city":       city if isinstance(city, str) and city else None,
        "timestamp":  int(time.time()),
    }


async def fetch_weather():
    global last_weather_data

    async def _do_fetch():
        global last_weather_data
        if not (config.get("weather_api") and config.get("weather_city")):
            last_weather_data["timestamp"] = int(time.time())
            return
        try:
            res = await asyncio.to_thread(
                REQ_SESSION.get,
                "http://api.openweathermap.org/data/2.5/weather",
                params={"q": config["weather_city"], "appid": config["weather_api"],
                        "units": "metric"},
                timeout=5,
            )
            data = res.json()
            if "main" in data:
                last_weather_data = _weather_from_owm(data)
                def _write_weather(data):
                    with open(WEATHER_CACHE_FILE, "w") as f:
                        json.dump(data, f)
                await asyncio.to_thread(_write_weather, last_weather_data)
                await ws_manager.broadcast({"type": "weather_data", "data": last_weather_data})
        except Exception as e:
            logger.debug(f"Weather fetch: {e}")
        last_weather_data["timestamp"] = int(time.time())

    if os.path.exists(WEATHER_CACHE_FILE):
        try:
            def _read_weather():
                with open(WEATHER_CACHE_FILE) as f:
                    return json.load(f)
            # This runs once on startup, but we still make it async-friendly
            # (or we could leave it sync since it's before the loop really starts, 
            # but for consistency we use to_thread). Wait, fetch_weather is an async task.
            last_weather_data = await asyncio.to_thread(_read_weather)
        except Exception:
            pass

    if time.time() - int(last_weather_data.get("timestamp", 0)) > 1800:
        await _do_fetch()

    assert weather_update_event is not None  # set in lifespan()
    while True:
        try:
            await asyncio.wait_for(weather_update_event.wait(), timeout=1800)
            weather_update_event.clear()
        except asyncio.TimeoutError:
            pass
        await _do_fetch()


async def _do_pipewire_route() -> None:
    """One-shot: link Dashboard-Soundboard monitor into every active capture consumer.

    Uses a two-pass approach:
      Pass 1 — pw-link -o: find all nodes that have capture output ports.
               These are mic sources + intermediaries (NoiseTorch, EasyEffects).
      Pass 2 — pw-link -l: walk forward |-> links from those capture sources;
               collect destinations whose node does NOT appear in Pass 1.
               Those are the true leaf consumers (Discord/WEBRTC, OBS, etc.).

    Correctly handles chains: Mic → NoiseTorch → EasyEffects → Discord
    by injecting directly into Discord, skipping noise gates.
    Does NOT require knowledge of the default source name.
    """
    def _run(cmd):
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2)

    # Collect Dashboard-Soundboard monitor output port names.
    pw_out   = (await asyncio.to_thread(_run, ["pw-link", "-o"])).decode()
    monitors = [p.strip() for p in pw_out.splitlines()
                if "Dashboard-Soundboard" in p and "monitor" in p]
    if not monitors:
        return

    sb_FL = monitors[0]
    sb_FR = monitors[1] if len(monitors) > 1 else sb_FL

    # Pass 1: use pw-link -o (output ports only, no |<- noise) to build the set
    # of node names that have capture-type output ports (mic sources + processors).
    capture_source_nodes: set[str] = set()
    for line in pw_out.splitlines():
        line = line.strip()
        if ":" in line and "capture" in line and "Dashboard-Soundboard" not in line:
            node_name = line.split(":")[0]
            capture_source_nodes.add(node_name)

    # Use pw-dump to collect valid application nodes (those that have a real application.name)
    # This filters out internal processing nodes of EasyEffects, NoiseTorch, and loopbacks
    valid_apps: set[str] = set()
    try:
        dump_out = (await asyncio.to_thread(_run, ["pw-dump"])).decode()
        import json
        dump_data = json.loads(dump_out)
        for obj in dump_data:
            if obj.get("type") == "PipeWire:Interface:Node":
                props = obj.get("info", {}).get("props", {})
                if props.get("application.name"):
                    valid_apps.add(props.get("node.name", ""))
    except Exception as e:
        logger.error(f"Failed to parse pw-dump: {e}")

    # Pass 2: walk pw-link -l forward links (|-> only, ignore |<- lines).
    # Collect destinations of capture-source links whose dest node is NOT
    # itself a capture source — those are the final endpoint apps.
    pw_links = (await asyncio.to_thread(_run, ["pw-link", "-l"])).decode()
    app_ports: list[str] = []
    is_capture_out = False
    for line in pw_links.splitlines():
        if not line.startswith((" ", "\t")):
            stripped = line.strip()
            # Only mark as a capture source header if it's in our Pass-1 set.
            node_name = stripped.split(":")[0] if ":" in stripped else ""
            is_capture_out = node_name in capture_source_nodes
        elif is_capture_out and "|->" in line:
            dest = line.split("|->")[1].strip()
            dest_node = dest.split(":")[0]
            if ("Dashboard-Soundboard" not in dest
                    and dest_node not in capture_source_nodes):
                
                # Exclude intermediary nodes (EasyEffects, loopbacks)
                # by requiring them to be in valid_apps
                if valid_apps and dest_node not in valid_apps:
                    continue
                    
                app_ports.append(dest)

    # Disconnect any rogue links from the soundboard to invalid apps (like EasyEffects internals)
    # This cleans up old links created before the router was fixed.
    current_src_port = ""
    for line in pw_links.splitlines():
        if not line.startswith((" ", "\t")):
            current_src_port = line.strip()
        elif current_src_port.startswith("Dashboard-Soundboard") and "monitor" in current_src_port and "|->" in line:
            dest_port = line.split("|->")[1].strip()
            # Preserve loopbacks so local audio feedback works (via module-loopback)
            if dest_port not in app_ports and "loopback" not in dest_port:
                await asyncio.to_thread(
                    subprocess.run, ["pw-link", "-d", current_src_port, dest_port],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                )

    for i, dest_port in enumerate(app_ports):
        src = sb_FL if i % 2 == 0 else sb_FR
        await asyncio.to_thread(
            subprocess.run, ["pw-link", src, dest_port],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )


async def pipewire_auto_router():
    """Route Dashboard-Soundboard monitor into microphone capture targets (Linux only).

    Wakes immediately when sb_route_event is set (triggered by a local play_clip)
    and also re-runs every 30 s to repair links torn down by PipeWire.
    """
    while True:
        try:
            # Block until triggered by a playback event OR 30-second keepalive.
            if sb_route_event is not None:
                try:
                    await asyncio.wait_for(sb_route_event.wait(), timeout=30.0)
                    sb_route_event.clear()
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(30)

            await _do_pipewire_route()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"PipeWire router: {e}")



async def _current_sinks() -> list[dict]:
    """The last sink list hardware_loop saw; poll only if it has none yet.

    config_sync's "hw" only feeds the Audio routing tab (raw names), so a save
    does not need three fresh wpctl processes to rebuild it.
    """
    order = config.get("audio_order")
    if last_audio_devs:
        return AudioSystem.ordered(last_audio_devs, order)   # the order may have just changed
    sinks = await asyncio.to_thread(AudioSystem.get_hardware_sinks, config.get("audio_names", {}))
    return AudioSystem.ordered(sinks, order)


async def _make_config_sync(sinks: list[dict], initial_disc: dict | None = None) -> dict:
    data = {
        "cfg":             config,
        "hw":              sinks,
        "os_target":       get_os_target(),
        "host_ip":         await asyncio.to_thread(get_lan_ip_cached),
        "server_version":  VERSION,
        "req_apk_version": REQUIRED_APK_VERSION,
        "update_available": update_available,
        "latest_version":  latest_version,
        "update_apk_url":  update_apk_url,
        "update_notes":    update_notes,
        # Messages this server understands beyond the original set; clients
        # fall back to the older behaviour when one is missing.
        "features":        SERVER_FEATURES,
        "clip_source":     _clip_source(config),
    }
    if initial_disc is not None:
        data["disc_init"] = initial_disc
    return {
        "type": "config_sync",
        "data": data,
    }


# What a paired device may read of the config: display and live-control state.
# An allowlist, so a new secret added to the config later is never sent by default.
DEVICE_CFG_KEYS = (
    "theme", "play_key_cover", "idle_dim_min", "idle_clock_min", "audio_names", "hidden_audio_devices", "disc_vc_enabled", "disc_sort_speakers",
    "show_mouse_batt", "show_hw_stats", "spot_enabled", "weather_enabled", "disc_enabled",
    "audio_enabled", "sb_enabled", "soundboard_volume", "soundboard_feedback", "device_av_controls",
)
DEVICE_SYNC_KEYS = (
    "hw", "os_target", "host_ip", "server_version", "req_apk_version", "update_apk_url",
    "features", "clip_source", "disc_init",
)
# Built-in modules a premade / plugin macro may name.
MACRO_MODULES = frozenset({"app_term", "app_web", "app_files", "app_task", "app_soundpad"})


def _clip_source(cfg: dict) -> str:
    """Which pad list the soundboard plays: local files or Soundpad indices."""
    return "local" if get_os_target() == "linux" or cfg.get("sounds_path") else "soundpad"


def _device_macro(m: dict) -> dict:
    out = {k: m.get(k, "") for k in ("id", "type", "display_name", "icon_url")}
    # A device runs macros by id; it only needs to know which key opens the soundboard.
    if m.get("type") == "plugin" and m.get("action_data") in MACRO_MODULES:
        out["action_data"] = m["action_data"]
    return out


def _device_config_sync(msg: dict) -> dict:
    data = {k: msg["data"][k] for k in DEVICE_SYNC_KEYS if k in msg["data"]}
    cfg = msg["data"]["cfg"]
    device_cfg = {k: cfg[k] for k in DEVICE_CFG_KEYS if k in cfg}
    device_cfg["macros"] = [_device_macro(m) for m in cfg.get("macros", []) if isinstance(m, dict)]
    pads_key = "local_buttons" if data.get("clip_source") == "local" else "soundpad_buttons"
    device_cfg[pads_key] = [{"uid": p.get("uid", ""), "name": p.get("name", "")}
                            for p in cfg.get(pads_key, []) if isinstance(p, dict)]
    data["cfg"] = device_cfg
    return {"type": "config_sync", "data": data}


async def _config_sync_by_role(initial_disc: dict | None = None, sinks: list | None = None) -> dict[str, dict]:
    msg = await _make_config_sync(sinks if sinks is not None else await _current_sinks(), initial_disc)
    return {"admin": msg, "device": _device_config_sync(msg)}


async def broadcast_config_sync():
    await ws_manager.broadcast_by_role(await _config_sync_by_role())


# Check for updates on GitHub. Releases are still mirrored to Codeberg by
# release.sh, but only for installs that predate the move and poll it.
RELEASES_API_URL = "https://api.github.com/repos/Liburn-Bajraktari/Touch-Dashboard/releases"


async def check_for_updates() -> dict:
    """Ask GitHub for the newest release. "checked" is False when the
    release list could not be fetched, so the page can say the check failed
    instead of reporting the last known state as up to date."""
    global update_available, latest_version, update_apk_url, update_notes
    checked = False
    try:
        def fetch_releases():
            try:
                req = urllib.request.Request(
                    RELEASES_API_URL,
                    headers={"User-Agent": f"TouchDashboard/{VERSION}", "Accept": "application/vnd.github+json"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = _loads(resp.read())
                    if isinstance(data, list) and len(data) > 0:
                        return data
            except Exception as err:
                logger.debug(f"Failed to fetch releases from {RELEASES_API_URL}: {err}")
            return []
        
        releases = await asyncio.to_thread(fetch_releases)
        if releases and isinstance(releases, list):
            checked = True
            desktop_checked = False
            apk_url_found = False
            prev_available = update_available
            
            for release in releases:
                tag = release.get("tag_name", "")
                # Drafts and pre-releases are never offered: not to the desktop,
                # and not to tablets, which install the APK found here.
                if not tag or tag == "pre-release" or release.get("draft") or release.get("prerelease"):
                    continue
                    
                # Find the newest APK asset across recent releases
                if not apk_url_found:
                    for asset in release.get("assets", []):
                        if asset.get("name", "").endswith(".apk"):
                            update_apk_url = asset.get("browser_download_url", "")
                            apk_url_found = True
                            break
                            
                # Check for desktop update (ignore tags starting with apk-)
                if not desktop_checked and not tag.lower().startswith("apk-"):
                    if tag != f"v{VERSION}" and tag != VERSION:
                        update_available = True
                        latest_version = tag
                        update_notes = release.get("body", "")
                    desktop_checked = True
                    
                if desktop_checked and apk_url_found:
                    break

            if update_available and (not prev_available or latest_version):
                if ws_manager.has_clients():
                    logger.info(f"Update available ({latest_version}) — broadcasting to connected clients.")
                    try:
                        await broadcast_config_sync()
                    except Exception as b_err:
                        logger.error(f"Failed to broadcast update notification: {b_err}")
    except Exception as e:
        logger.error(f"Update check failed: {e}")

    return {
        "checked":          checked,
        "update_available": update_available,
        "latest_version":  latest_version,
        "update_notes":    update_notes,
    }


# ─── FastAPI app ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    await origin_policy.refresh()
    await asyncio.to_thread(local_token.rotate)

    async def update_monitor_loop():
        # Allow initial clients to connect before first check
        await asyncio.sleep(2)
        while True:
            await check_for_updates()
            await asyncio.sleep(1800)  # Recheck every 30 minutes

    asyncio.create_task(update_monitor_loop())

    # Linux: create Dashboard-Soundboard virtual sink
    if get_os_target() == "linux":
        async def _run(cmd: list[str], timeout: int = 3) -> subprocess.CompletedProcess:
            return await asyncio.to_thread(subprocess.run, cmd, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, timeout=timeout)
        async def _out(cmd: list[str], timeout: int = 3) -> bytes:
            return await asyncio.to_thread(subprocess.check_output, cmd, stderr=subprocess.DEVNULL, timeout=timeout)

        try:
            try:
                real_sink = (await _out(["pactl", "get-default-sink"])).decode().strip()  
                real_src  = (await _out(["pactl", "get-default-source"])).decode().strip()  
            except Exception:
                real_sink = real_src = ""

            sinks_out = (await _out(["pactl", "list", "short", "sinks"])).decode()
            if "Dashboard-Soundboard" not in sinks_out:
                await _run(["pactl", "load-module", "module-null-sink",
                      "sink_name=Dashboard-Soundboard",
                      'sink_properties=device.description="Dashboard-Soundboard"'])

            # Apply stored volume (0-200%) so restarts preserve the user setting.
            with CONFIG_LOCK:
                sb_vol = int(config.get("soundboard_volume", 100))
            sb_vol = max(0, min(200, sb_vol))
            await _run(["pactl", "set-sink-volume", "Dashboard-Soundboard", f"{sb_vol}%"])
            await _run(["pactl", "set-sink-mute",   "Dashboard-Soundboard", "0"])

            # Self-listen: loopback from Dashboard-Soundboard monitor → real speakers.
            # We must specify sink= explicitly; without it PipeWire picks the default
            # sink at load-time which may be a virtual sink (EasyEffects, etc.).
            mods_out = (await _out(["pactl", "list", "short", "modules"])).decode()
            if config.get("soundboard_feedback", True) and "sink=" + real_sink not in mods_out and real_sink:
                await _run(["pactl", "load-module", "module-loopback",
                            "source=Dashboard-Soundboard.monitor",
                            f"sink={real_sink}",
                            "sink_dont_move=1",
                            "source_dont_move=1"])

            # Mic routing (→ Discord etc.) is done via pw-link in
            # pipewire_auto_router, triggered immediately on each playback.

            if real_sink and "Dashboard" not in real_sink:
                await _run(["pactl", "set-default-sink",   real_sink])
            if real_src  and "Dashboard" not in real_src:
                await _run(["pactl", "set-default-source", real_src])
        except Exception as e:
            logger.error(f"PipeWire setup: {e}")

    # Initialise asyncio primitives here — the event loop is guaranteed to
    # exist at this point. Creating them at module scope binds them to a
    # different (or non-existent) loop and causes:
    #   RuntimeError: Task got Future <Event> attached to a different loop
    global weather_update_event, speedtest_lock, upower_monitor, lock_monitor_instance, sb_route_event
    weather_update_event = asyncio.Event()
    speedtest_lock       = asyncio.Lock()
    sb_route_event       = asyncio.Event()

    # The lock, UPower and MPRIS monitors each import GLib on their own thread.
    # Imported there at the same moment, PyGObject can build two GLib wrappers
    # and every loop fails ("Expected GLib.MainContext, but got
    # gi.repository.GLib.MainContext"): import it once here first.
    if get_os_target() == "linux":
        try:
            import gi  # type: ignore[import-untyped]
            gi.require_version("Gio", "2.0")
            from gi.repository import GLib, Gio  # type: ignore[import-untyped]  # noqa: F401
            import dbus.mainloop.glib  # type: ignore[import-untyped]  # noqa: F401
        except (ImportError, ValueError) as e:
            logger.warning(f"GLib / dbus-python unavailable: {e}")

    # All platforms: start the lock-state monitor.
    # The callback fires call_soon_threadsafe(sys_data_trigger.set) so that
    # hardware_loop wakes immediately when the PC is locked or unlocked.
    _loop_ref_lock = asyncio.get_running_loop()
    lock_monitor_instance = _lock_monitor_mod.create()

    def _on_lock_change(is_locked: bool):
        # Runs on the lock-monitor thread — wake the asyncio loop.
        if _loop_ref_lock and sys_data_trigger:
            _loop_ref_lock.call_soon_threadsafe(sys_data_trigger.set)

    lock_monitor_instance.on_lock_change = _on_lock_change
    lock_monitor_instance.start()

    # Linux: start the event-driven UPower mouse battery monitor.
    # The callback fires call_soon_threadsafe(sys_data_trigger.set) so that
    # hardware_loop wakes immediately on a battery property change.
    if get_os_target() == "linux":
        _loop_ref = asyncio.get_running_loop()
        upower_monitor = _mouse_battery_mod.UPowerMouseMonitor()

        def _on_batt_change(pct, state_int):
            # Runs on the upower-monitor GLib thread — wake the asyncio loop.
            if _loop_ref and sys_data_trigger:
                _loop_ref.call_soon_threadsafe(sys_data_trigger.set)

        upower_monitor.on_battery_change = _on_batt_change
        upower_monitor.start()

        # Media players over D-Bus; if it cannot start, playerctl is polled.
        global mpris_mon
        monitor = _mpris_monitor_mod.MprisMonitor()

        def _on_media_change():
            if _loop_ref and sys_data_trigger:
                _loop_ref.call_soon_threadsafe(sys_data_trigger.set)

        monitor.on_change = _on_media_change
        if await asyncio.to_thread(monitor.start):
            mpris_mon = monitor

    restart_discord_ipc()

    tasks = [
        asyncio.create_task(hardware_loop(),  name="hardware_loop"),
        asyncio.create_task(fetch_weather(),  name="fetch_weather"),
    ]
    if get_os_target() == "linux":
        tasks.append(asyncio.create_task(pipewire_auto_router(), name="pw_router"))

    app.state.bg_tasks = tasks
    yield

    if spotify_reader is not None and not spotify_reader.done():
        tasks.append(spotify_reader)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if disc_ipc_instance:
        disc_ipc_instance.close()
    if lock_monitor_instance is not None:
        lock_monitor_instance.stop()
    if upower_monitor is not None:
        upower_monitor.stop()
    if mpris_mon is not None:
        mpris_mon.stop()


_app = FastAPI(lifespan=lifespan)


def _require_loopback(request: Request) -> None:
    """OAuth sign-in happens on the PC: its redirect URIs are 127.0.0.1 only."""
    if not security.is_loopback(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="Only available on the PC")


def _require_local_control(request: Request) -> None:
    """Routes for local processes: loopback, a JSON POST (a cross-site page
    cannot send one without a preflight, which this server never answers)
    and the control token from the 0600 file in the data folder."""
    _require_loopback(request)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not local_token.check(token.strip()):
        raise HTTPException(status_code=401, detail="Bearer token required",
                            headers={"WWW-Authenticate": "Bearer"})


@_app.post("/api/wakeup", dependencies=[Depends(_require_local_control)])
def wakeup_endpoint():
    # Bring an existing desktop window to front (second-instance signal).
    _desktop_module.wakeup()
    return {"status": "waking up"}

@_app.post("/api/exit", dependencies=[Depends(_require_local_control)])
def exit_endpoint():
    import threading
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"status": "shutting down"}

def _csp(host: str, loopback: bool) -> str:
    """The page's Content Security Policy for one response.

    The socket may only go back to the host this page came from. 'unsafe-inline'
    stays (inline handlers throughout the template). 'unsafe-eval' only on
    loopback: pywebview builds window.pywebview.api.* with new Function, and
    the desktop window is always 127.0.0.1; pages served to the LAN never need it.
    """
    script = "'self' 'unsafe-inline'" + (" 'unsafe-eval'" if loopback else "")
    sockets = f" ws://{host}" if origin_policy.host_allowed(host) else ""
    return (f"default-src 'self'; script-src {script}; style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data: https:; connect-src 'self'{sockets}; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class _SecurityHeaders:
    """Pure ASGI, so responses stream straight through (BaseHTTPMiddleware
    wraps every request in an extra task and buffers the body).

    Static assets get "no-cache": the browser revalidates with its ETag and
    gets a 304, so a server update can never leave a stale themes.js paired
    with a new index.html.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_static = scope.get("path", "").startswith("/static/")
        host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), "").lower()
        client = scope.get("client")
        csp = _csp(host, security.is_loopback(client[0] if client else None))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Frame-Options"]         = "DENY"
                headers["X-Content-Type-Options"]  = "nosniff"
                headers["Content-Security-Policy"] = csp
                if is_static:
                    headers["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _HostGuard:
    """403 for any HTTP request or WebSocket whose Host is not ours (DNS rebinding).

    A rebinding page reaches 127.0.0.1 under the attacker's own host name, so
    its Host header never matches 127.0.0.1, localhost or a LAN address of this PC.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), "")
        if not origin_policy.host_allowed(host) and origin_policy.wants_refresh(host):
            await origin_policy.refresh()
        if origin_policy.host_allowed(host):
            await self.app(scope, receive, send)
            return
        logger.warning("Refused %s for Host %r", scope["type"], host[:100])
        if scope["type"] == "websocket":
            await receive()   # websocket.connect; closing before accept answers the handshake with 403
            await send({"type": "websocket.close", "code": WS_CLOSE_POLICY})
            return
        await JSONResponse({"error": "Forbidden host"}, status_code=403)(scope, receive, send)


# Added last = outermost: headers are set on the already-compressed response,
# and on the Host guard's 403s.
_app.add_middleware(_HostGuard)
_app.add_middleware(GZipMiddleware, minimum_size=1024)
_app.add_middleware(_SecurityHeaders)


_app.mount("/static", StaticFiles(directory=os.path.join(RESOURCE_DIR, "static")), name="static")

# ─── HTTP routes ───────────────────────────────────────────────────────────────

@_app.get("/api/local_art")
async def serve_local_art(request: Request, h: str = "", k: str = ""):
    # Signed by _signed_media() for the cover playing now: a revoked device's old URLs stop working.
    if not (h and h == _current_art_h and hmac.compare_digest(k.encode(), _sign_art(h).encode())):
        return JSONResponse({"error": "No art"}, status_code=404)
    # CORS lets the APK (a file:// page) read the pixels to tint the UI from
    # the cover; only our own origins are echoed back, never "*".
    headers = {"Vary": "Origin"}
    origin = request.headers.get("origin")
    if origin and origin_policy.origin_allowed(
            origin, request.client.host if request.client else None, request.headers.get("user-agent", "")):
        headers["Access-Control-Allow-Origin"] = origin
    cover = media_module.get_cover(h)    # a player's data: cover, decoded once
    if cover:
        return Response(content=cover[0], media_type=cover[1], headers=headers)
    path = media_module.get_last_mpris_art_path()
    if path and await asyncio.to_thread(os.path.exists, path):
        return FileResponse(path, headers=headers)
    return JSONResponse({"error": "No art"}, status_code=404)


@_app.get("/")
async def index(request: Request):
    global last_host_url
    last_host_url = request.url.netloc
    html, etag = _load_template()
    # NOTE: Discord initial state is now delivered via WS config_sync,
    # so we no longer mutate the HTML here.
    # "no-cache" + ETag: always revalidated (an update is seen at once), but a
    # reload of an unchanged page is a 304 instead of the whole template.
    headers = {"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache",
               "Expires": "0", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return HTMLResponse(content=html, headers=headers)


@_app.get("/favicon.ico")
async def _favicon():
    return JSONResponse({})


@_app.get("/manifest.json")
async def _manifest():
    return JSONResponse({
        "name": "Touch Dashboard", "short_name": "Dashboard",
        "start_url": "/?v=2.0", "display": "standalone",
        "orientation": "landscape",
        "background_color": "#000000", "theme_color": "#000000",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any maskable"},
        ],
    }, headers={"Cache-Control": "no-cache"})


@_app.get("/spotify_login", dependencies=[Depends(_require_loopback)])
async def spotify_login():
    sp = get_sp_oauth()
    if not sp:
        return JSONResponse({"error": "No Spotify credentials configured"})
    return RedirectResponse(sp.get_authorize_url(state=_oauth_state("spotify")))


def _oauth_state_mismatch() -> HTMLResponse:
    return HTMLResponse("<p>This sign-in was not started from Touch Dashboard, or it expired. "
                        "Start it again from Settings on the PC.</p>", status_code=400)


@_app.get("/callback", dependencies=[Depends(_require_loopback)])
async def spotify_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    global force_media_update
    sp = get_sp_oauth()
    if not sp:
        return RedirectResponse("/")
    if not _spend_oauth_state("spotify", state):
        logger.warning("Spotify sign-in callback refused: OAuth state missing or wrong")
        return _oauth_state_mismatch()
    if error or not code:
        # Spotify sends ?error=access_denied when the consent is cancelled.
        logger.warning(f"Spotify sign-in not completed: {error or 'no code'}")
        return _spotify_sign_in_failed(error or "Spotify sent no sign-in code")
    try:
        # A fresh sign-in replaces whatever is cached (an older one without the like key's scopes).
        await asyncio.to_thread(sp.get_access_token, code, check_cache=False)
    except Exception as e:   # spotipy.SpotifyOauthError: invalid_client, invalid_grant, a redirect URI mismatch
        logger.warning(f"Spotify sign-in failed: {e}")
        return _spotify_sign_in_failed(str(e))
    logger.info("Spotify signed in")
    force_media_update = True        # read playback (and the like state) now, not in 3 s
    if sys_data_trigger:
        sys_data_trigger.set()
    return RedirectResponse("/")


# What Spotify's ?error= on the redirect means, and what to do. Errors from the
# token exchange (invalid_client, invalid_grant, a redirect URI mismatch) get the default.
SPOTIFY_SIGN_IN_HELP = {
    "access_denied": "The sign-in was cancelled on Spotify's page.",
    "server_error": (
        "Spotify refused the sign-in on its side, after you logged in. Your app's Client ID and "
        "Redirect URI were accepted: Spotify sent you back here. Since February 2026 an app in "
        "Spotify's Development Mode only works while its owner has Spotify Premium, and only for "
        "accounts added under User Management in the app's dashboard (up to five). Check both "
        "on developer.spotify.com/dashboard."),
    "temporarily_unavailable": "Spotify's sign-in is busy or down. Try again in a few minutes.",
}
SPOTIFY_SIGN_IN_DEFAULT_HELP = (
    "Check the Client ID, Client secret and the Redirect URI (http://127.0.0.1:8888/callback) "
    "of your app in Spotify's developer dashboard.")


def _spotify_sign_in_failed(reason: str) -> HTMLResponse:
    help_text = SPOTIFY_SIGN_IN_HELP.get(reason, SPOTIFY_SIGN_IN_DEFAULT_HELP)
    return HTMLResponse(
        "<p>Spotify sign-in did not complete: " + html.escape(reason[:300]) + "</p>"
        "<p>" + html.escape(help_text) + "</p>"
        "<p>Then try again from Settings → Spotify on the PC. Spotify's own keys "
        "(play, next, seek) keep working without this sign-in; it is needed for the like key "
        "and for Spotify playing on another device.</p>", status_code=400)


@_app.get("/disc_callback", dependencies=[Depends(_require_loopback)])
async def discord_callback(code: str | None = None, state: str | None = None):
    if not code or not disc_ipc_instance:
        return RedirectResponse("/")
    if not _spend_oauth_state("discord", state):
        logger.warning("Discord sign-in callback refused: OAuth state missing or wrong")
        return _oauth_state_mismatch()
    ok = await asyncio.to_thread(disc_ipc_instance.exchange_code, code)
    if ok:
        disc_ipc_instance.needs_reauth = True
    return RedirectResponse("/")


_update_running = False
RELEASE_TAG_RE       = re.compile(r"v\d+\.\d+\.\d+")
RELEASE_DOWNLOAD_URL = "https://github.com/Liburn-Bajraktari/Touch-Dashboard/releases/download"
SUMS_MAX_BYTES       = 64 * 1024
INSTALLER_MAX_BYTES  = 1 << 20


def _download(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"TouchDashboard/{VERSION}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url} is larger than {limit} bytes")
    return data


def _sha256sums(text: str) -> dict[str, str]:
    """{file name: hex digest} from a sha256sum listing."""
    sums = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def _fetch_verified_installer(tag: str) -> str:
    """The installer published with release `tag`, checked against that
    release's SHA256SUMS, saved to a temp file; returns its path.

    An update runs exactly what was released, never the main branch.
    """
    if not RELEASE_TAG_RE.fullmatch(tag):
        raise ValueError(f"not a release tag: {tag!r}")
    name = "setup.sh" if get_os_target() == "linux" else "install.ps1"
    sums = _sha256sums(_download(f"{RELEASE_DOWNLOAD_URL}/{tag}/SHA256SUMS", SUMS_MAX_BYTES)
                       .decode("utf-8", errors="replace"))
    body = _download(f"{RELEASE_DOWNLOAD_URL}/{tag}/{name}", INSTALLER_MAX_BYTES)
    expected = sums.get(name, "")
    if not expected or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected):
        raise ValueError(f"{name} of {tag} does not match its SHA256SUMS")
    fd, path = tempfile.mkstemp(prefix="touch-dashboard-update-", suffix=os.path.splitext(name)[1])
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    return path


def _installer_argv(path: str) -> list[str]:
    if get_os_target() == "linux":
        return ["bash", path]
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]


def _start_update(tag: str) -> bool:
    """Install release `tag` in the background and stream the output to admins.

    Started over the admin WebSocket (run_update), never over plain HTTP. The
    installer gets the tag too (TD_RELEASE_TAG) and checks the app archive it
    downloads against the same SHA256SUMS.
    """
    global _update_running
    if _update_running:
        return False
    _update_running = True
    current_dir = os.path.dirname(os.path.abspath(__file__))
    env = {**os.environ, "AUTO_UPDATE": "1", "BACKGROUND_UPDATE": "1",
           "TARGET_DIR": current_dir, "TD_RELEASE_TAG": tag}

    # Run in background and stream output to WS
    async def run_update():
        global _update_running
        installer = ""
        try:
            installer = await asyncio.to_thread(_fetch_verified_installer, tag)
            logger.info("Updating to %s with the verified installer %s", tag, installer)
            # An argument list, no shell: nothing in a path or tag is ever parsed as a command.
            process = await asyncio.create_subprocess_exec(
                *_installer_argv(installer),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            
            buf = b""
            progress_pct = 0.0
            
            while True:
                if process.stdout is None: break
                char = await process.stdout.read(1)
                if not char:
                    break
                buf += char
                if char in (b'\r', b'\n'):
                    line = buf.decode('utf-8', errors='replace').strip()
                    buf = b""
                    if not line:
                        continue
                        
                    import re
                    match = re.search(r'(\d+\.\d+)%', line)
                    if match:
                        progress_pct = float(match.group(1))
                        
                    await ws_manager.broadcast(admin_only=True, msg={
                        "type": "update_progress",
                        "data": {
                            "text": line,
                            "progress": progress_pct,
                            "error": False
                        }
                    })
                    
            await process.wait()
            os.unlink(installer)
            installer = ""
            if process.returncode != 0:
                _update_running = False
                await ws_manager.broadcast(admin_only=True, msg={
                    "type": "update_progress",
                    "data": {
                        "text": f"\n[!] Update failed with exit code {process.returncode}",
                        "progress": progress_pct,
                        "error": True
                    }
                })
            else:
                await ws_manager.broadcast(admin_only=True, msg={
                    "type": "update_progress",
                    "data": {
                        "text": f"\n[*] Update successful! Restarting server...",
                        "progress": 100,
                        "error": False
                    }
                })
                await asyncio.sleep(1) # wait for ws broadcast
                
                # Cleanly close Discord IPC to allow immediate reconnection on restart
                restart_discord_ipc()

                # Restart the server
                if get_os_target() == "linux":
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    # On Windows, os.execv is unreliable, better to spawn and exit
                    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512) | 0x00000008
                    subprocess.Popen([sys.executable] + sys.argv, creationflags=flags)
                    os._exit(0)
        except Exception as e:
            _update_running = False
            logger.error(f"Update to {tag} failed: {e}")
            await ws_manager.broadcast(admin_only=True, msg={
                "type": "update_progress",
                "data": {
                    "text": f"\n[!] Update failed: {e}",
                    "progress": 0,
                    "error": True
                }
            })
        finally:
            if installer:
                with contextlib.suppress(OSError):
                    os.unlink(installer)

    asyncio.create_task(run_update())
    return True

# ─── WebSocket ─────────────────────────────────────────────────────────────────
#
# Handshake: the Origin is checked before accept(). The first message, within
# AUTH_TIMEOUT_S, must be {"type": "auth", "token": ...} or
# {"type": "pair_request", "device_name": ...}; nothing about the PC is sent
# before one succeeds. Every later message goes through _dispatch(), the one
# place roles are enforced (ADMIN_ACTIONS).

async def _send(ws: WebSocket, msg: dict) -> bool:
    try:
        await ws.send_text(_dumps(msg))
        return True
    except Exception:   # the peer is gone; the caller decides what that means
        return False


async def _close(ws: WebSocket, code: int, reason: str = "") -> None:
    try:
        await ws.close(code=code, reason=reason)
    except (RuntimeError, OSError, WebSocketDisconnect):
        pass   # already closed by the peer


def _parse_message(raw) -> dict | None:
    try:
        msg = _loads(raw)
    except (ValueError, TypeError):
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get("type"), str) else None


def _refused(key: str, reason: str) -> dict:
    return {"type": "refused", "data": {"action": key[:64], "reason": reason}}


def _initial_disc() -> dict:
    """Discord state for the first config_sync (replaces DOM surgery)."""
    return {
        "connected": bool(disc_ipc_instance and disc_ipc_instance.connected),
        "authorized": bool(config.get("disc_token", "")),
        "mute": (disc_ipc_instance.voice_state.get("mute", False)
                 if disc_ipc_instance else False),
        "deaf": (disc_ipc_instance.voice_state.get("deaf", False)
                 if disc_ipc_instance else False),
    }


def _legacy_update_notice() -> dict:
    """What a tablet app from before pairing gets instead of state.

    Those apps never send auth, so they cannot connect any more; a config_sync
    is the one message they act on, and this one only carries the required
    app version and its public download link, so they ask the user to update.
    """
    return {"type": "config_sync", "data": {
        "auth_required": True, "cfg": {}, "hw": [], "features": [],
        "req_apk_version": REQUIRED_APK_VERSION, "update_apk_url": update_apk_url,
    }}


def _devices_msg() -> dict:
    apps = ws_manager.online_device_apps()
    rows = []
    for d in paired_devices.public_list():
        in_app, app = apps.get(d["id"], (False, None))
        # client / app only while connected: "app" with its version (None: older
        # than v0.11.12, which did not send it) or "browser".
        rows.append({**d, "online": d["id"] in apps,
                     "client": ("app" if in_app or app else "browser") if d["id"] in apps else None,
                     "app": app})
    return {"type": "paired_devices", "data": rows}


def _in_app(ws: WebSocket) -> bool:
    return bool(security.ANDROID_WEBVIEW_UA.search(ws.headers.get("user-agent", "")))


def _app_version(msg: dict) -> str | None:
    """The app version a page reports in auth / pair_request ("0.11.12"), if it is one."""
    app = msg.get("app")
    return app if isinstance(app, str) and APP_VERSION_RE.fullmatch(app) else None


async def _broadcast_devices() -> None:
    await ws_manager.broadcast(_devices_msg(), admin_only=True)


def _pair_pending_msg(req: security.PendingPair) -> dict:
    return {"type": "pair_pending", "data": {
        "id": req.id, "name": req.name, "ip": req.ip, "code": req.code,
        "expires_in": int(req.remaining(pairing.clock())),
    }}


@_app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    ip = ws.client.host if ws.client else ""
    origin = ws.headers.get("origin")
    user_agent = ws.headers.get("user-agent", "")
    # No Origin: not a browser (it still needs a token). A foreign one: a web page.
    if origin is not None and not origin_policy.origin_allowed(origin, ip, user_agent):
        logger.warning("WebSocket from %s refused: origin %r, user agent %r", ip, origin[:100], user_agent[:200])
        await ws.close(code=WS_CLOSE_POLICY)
        return
    if not pending_sockets.acquire(ip):
        logger.warning("WebSocket from %s refused: too many unauthenticated connections", ip)
        await ws.close(code=WS_CLOSE_TRY_LATER)
        return
    try:
        await ws.accept()
        client = await _authenticate(ws, ip, origin)
    finally:
        pending_sockets.release(ip)
    if client is None:
        return
    try:
        # auth_ok goes out before the client joins the broadcasts, so it is always first.
        await ws.send_text(_dumps({"type": "auth_ok", "data": {
            "role": client.role, "device_id": client.device_id, "name": client.name}}))
        ws_manager.add(client)
        await _send_initial_state(client)
        # Until the client hangs up, or a handler closed it (unpair_self).
        while ws.application_state == WebSocketState.CONNECTED:
            msg = _parse_message(await ws.receive_text())
            if msg is None:
                await _send(ws, _refused("", "malformed"))
                continue
            await _dispatch(client, msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        ws_manager.remove(client)
        if client.device_id:
            await _device_seen(client.device_id)


async def _authenticate(ws: WebSocket, ip: str, origin: str | None) -> Client | None:
    """The first message decides the role; anything else closes the socket."""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.info("WebSocket from %s sent no auth within %.0f s", ip, AUTH_TIMEOUT_S)
        await _send(ws, _legacy_update_notice())
        await _close(ws, WS_CLOSE_POLICY, "auth required")
        return None
    except WebSocketDisconnect:
        return None
    except KeyError:   # a binary frame
        await _close(ws, WS_CLOSE_POLICY, "auth required")
        return None

    msg = _parse_message(raw) or {}
    if msg.get("type") == "auth":
        if security.is_loopback(ip) and origin_policy.is_admin_origin(origin):
            return Client(ws, "admin", ip)
        device = paired_devices.verify(msg.get("token"))
        if device is None:
            logger.warning("WebSocket from %s refused: no valid pairing token", ip)
            await _close(ws, WS_CLOSE_UNAUTHORIZED, "pairing required")
            return None
        return Client(ws, "device", ip, device["id"], device["name"], _app_version(msg), _in_app(ws))
    if msg.get("type") == "pair_request":
        return await _pair(ws, ip, msg.get("device_name"), _app_version(msg), _in_app(ws))
    await _close(ws, WS_CLOSE_POLICY, "auth required")
    return None


async def _pair(ws: WebSocket, ip: str, device_name, app: str | None = None, in_app: bool = False) -> Client | None:
    """Hold the socket until the PC user allows or denies it, or it expires."""
    try:
        req = pairing.open(ip, device_name)
    except security.PairingRefused as refused:
        logger.warning("Pairing request from %s refused: %s", ip, refused.reason)
        await _send(ws, {"type": "pair_result", "data": {"status": refused.reason}})
        await _close(ws, WS_CLOSE_POLICY, refused.reason)
        return None

    logger.info("Pairing request %s from %s (%r)", req.id, ip, req.name)
    status = "expired"
    try:
        await _send(ws, {"type": "pair_code", "data": {
            "code": req.code, "expires_in": int(req.remaining(pairing.clock()))}})
        await ws_manager.broadcast(_pair_pending_msg(req), admin_only=True)
        # A window hidden in the tray must come forward, or nobody sees the prompt.
        await asyncio.to_thread(_desktop_module.wakeup)
        status = await _await_pair_decision(ws, req)
    finally:
        pairing.close(req)
        await ws_manager.broadcast({"type": "pair_resolved", "data": {"id": req.id, "status": status}},
                                   admin_only=True)
    logger.info("Pairing request %s: %s", req.id, status)

    if status == "approved":
        try:
            device, token = await asyncio.to_thread(paired_devices.add, req.name)
        except OSError as e:
            logger.error("Could not save the paired device: %s", e)
            status = "error"
        else:
            if await _send(ws, {"type": "pair_result", "data": {
                    "status": "approved", "token": token, "device_id": device["id"], "name": device["name"]}}):
                await _broadcast_devices()
                return Client(ws, "device", ip, device["id"], device["name"], app, in_app)
            # It hung up before receiving its token: nothing can ever use this entry.
            await asyncio.to_thread(paired_devices.revoke, device["id"])
            return None
    if status != "cancelled":
        await _send(ws, {"type": "pair_result", "data": {"status": status}})
    await _close(ws, WS_CLOSE_POLICY, status)
    return None


async def _await_pair_decision(ws: WebSocket, req: security.PendingPair) -> str:
    """'approved' or 'denied' from the PC, 'expired', or 'cancelled' when the
    client hangs up or sends anything (pair_cancel) while it waits."""
    listener = asyncio.ensure_future(ws.receive())
    try:
        done, _ = await asyncio.wait({req.decision, listener}, timeout=req.remaining(pairing.clock()),
                                     return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not listener.done():
            listener.cancel()
            await asyncio.wait({listener})
    if listener in done:
        listener.exception()   # retrieved: a closed socket here just means "cancelled"
        return "cancelled"
    if req.decision in done:
        return "approved" if req.decision.result() else "denied"
    return "expired"


async def _send_initial_state(client: Client) -> None:
    hw = await asyncio.to_thread(AudioSystem.get_hardware_sinks, config.get("audio_names", {}))
    hw = AudioSystem.ordered(hw, config.get("audio_order"))
    syncs = await _config_sync_by_role(_initial_disc(), sinks=hw)
    await client.ws.send_text(_dumps(syncs[client.role]))

    if last_weather_data.get("temp") != "--":
        await client.ws.send_text(_dumps({"type": "weather_data", "data": last_weather_data}))

    if client.is_admin:
        await client.ws.send_text(_dumps(_devices_msg()))
        for req in pairing.pending():
            await client.ws.send_text(_dumps(_pair_pending_msg(req)))
    else:
        # Which app a tablet runs: the bundled page decides how the keys behave,
        # and an update postponed with "Later" keeps the old one.
        logger.info("Device %r connected from %s (%s)", client.name, client.ip,
                    f"app {client.app}" if client.app
                    else "an app older than v0.11.12" if client.in_app else "a browser")
        await _device_seen(client.device_id)


async def _device_seen(device_id: str) -> None:
    """Record last_seen and tell admins the device went on- or offline."""
    try:
        await asyncio.to_thread(paired_devices.touch, device_id)
    except OSError as e:
        logger.warning("Could not update last_seen for device %s: %s", device_id, e)
    await _broadcast_devices()


# ── dispatch ──────────────────────────────────────────────────────────────────

def _action_key(msg: dict) -> str:
    """The message type, or 'action:<name>' for an action message."""
    if msg["type"] == "action":
        name = msg.get("data")
        return f"action:{name}" if isinstance(name, str) else "action:"
    return msg["type"]


def _refusal(client: Client, key: str) -> str | None:
    if key not in KNOWN_ACTIONS:
        return "unknown"
    if key in ADMIN_ACTIONS and not client.is_admin:
        return "admin_only"
    if key in DEVICE_AV_ACTIONS and not client.is_admin and not config.get("device_av_controls", True):
        return "disabled"
    return None


async def _dispatch(client: Client, msg: dict) -> None:
    key = _action_key(msg)
    reason = _refusal(client, key)
    if reason:
        logger.warning("Refused %r from %s (%s): %s", key[:64], client.ip, client.role, reason)
        await _send(client.ws, _refused(key, reason))
        return
    try:
        await WS_HANDLERS[msg["type"]](client, msg)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        logger.warning("Rejected %r from %s: %r", key, client.ip, e)
        await _send(client.ws, _refused(key, "invalid"))


# ── handlers: (client, msg); msg["data"] unless the protocol puts fields at the top ──

async def _ws_req_local_sounds(client: Client, msg: dict) -> None:
    sounds = await asyncio.to_thread(get_local_sounds)
    await client.ws.send_text(_dumps({"type": "local_sounds_list", "data": sounds}))


async def _ws_req_apps_list(client: Client, msg: dict) -> None:
    apps = await asyncio.to_thread(AppEnumerator.get_apps)
    await client.ws.send_text(_dumps({"type": "apps_list", "data": apps}))


async def _ws_save_macros(client: Client, msg: dict) -> None:
    if not isinstance(msg.get("data"), list):
        raise TypeError("macros must be a list")
    with CONFIG_LOCK:
        config["macros"] = _with_ids(_live_macros(msg["data"]), "id")
    await asyncio.to_thread(save_config)
    await broadcast_config_sync()


def _lookup_id(msg: dict) -> str:
    """The top-level "id" of run_macro / play_clip; a command or path never matches one."""
    value = msg.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not (isinstance(value, str) and ITEM_ID_RE.fullmatch(value)):
        raise ValueError(f"not an id: {str(value)[:64]!r}")
    return value


async def _ws_run_macro(client: Client, msg: dict) -> None:
    macro_id = _lookup_id(msg)
    macro = next((m for m in config.get("macros", [])
                  if isinstance(m, dict) and m.get("id") == macro_id), None)
    if macro is None:
        raise ValueError(f"no macro {macro_id!r}")
    await _run_macro(client, macro)


async def _run_macro(client: Client, macro: dict) -> None:
    """Run a configured macro: what it does comes from the config, never the client."""
    m_type, action_data = macro.get("type"), macro.get("action_data")
    if m_type == "app" and isinstance(action_data, str) and action_data.strip():
        await asyncio.to_thread(AppEnumerator.launch_app, action_data)
    elif m_type == "macro":
        keys = action_data if isinstance(action_data, list) else [action_data]
        keys = [k for k in keys if isinstance(k, (str, int)) and not isinstance(k, bool)]
        await asyncio.to_thread(MacroSystem.send_keys, *keys)
    elif m_type in ("premade", "plugin") and action_data in MACRO_MODULES:
        await _handle_action(client.ws, action_data)
    else:
        raise ValueError(f"macro {macro.get('id')!r} has nothing to run")


async def _ws_save_config(client: Client, msg: dict) -> None:
    global global_sp_oauth
    data = msg.get("data")
    if not isinstance(data, dict):
        raise TypeError("config must be an object")
    old_disc  = config.get("disc_enabled", True)
    old_disc_id, old_disc_sec = config.get("disc_id"), config.get("disc_secret")
    old_wx_api, old_wx_city   = config.get("weather_api"), config.get("weather_city")
    old_spot_id               = config.get("spot_id")

    for key in ("hidden_audio_devices", "audio_order"):
        if key in data:
            data[key] = _clean_sink_names(data[key])
    if "macros" in data:
        data["macros"] = _live_macros(data["macros"])
    for key, id_key in (("macros", "id"), ("local_buttons", "uid"), ("soundpad_buttons", "uid")):
        if key in data:
            data[key] = _with_ids(data[key], id_key)
    if "device_av_controls" in data:
        data["device_av_controls"] = data["device_av_controls"] is True
    for key, choices in IDLE_CHOICES.items():
        if key in data:
            data[key] = _idle_minutes(key, data[key], choices)
    with CONFIG_LOCK:
        config.update(data)
    await asyncio.to_thread(save_config)

    if old_spot_id != config.get("spot_id"):
        global_sp_oauth = None

    await broadcast_config_sync()

    if (old_disc != config.get("disc_enabled", True)
            or old_disc_id  != config.get("disc_id")
            or old_disc_sec != config.get("disc_secret")):
        restart_discord_ipc()

    if (old_wx_api  != config.get("weather_api")
            or old_wx_city != config.get("weather_city")):
        if weather_update_event is not None:
            weather_update_event.set()


async def _ws_action(client: Client, msg: dict) -> None:
    await _handle_action(client.ws, msg["data"])


async def _ws_set_volume(client: Client, msg: dict) -> None:
    data = msg["data"]
    target = ("@DEFAULT_AUDIO_SINK@" if data["type"] == "speaker"
              else "@DEFAULT_AUDIO_SOURCE@")
    # Non-blocking (a dict write and an Event): no thread hop per drag step.
    AudioSystem.set_vol(target, max(0, min(100, int(data["val"]))))


async def _ws_disc_voice(client: Client, msg: dict) -> None:
    # The state the user asked for, not a toggle.
    data = msg.get("data")
    if disc_ipc_instance and disc_ipc_instance.connected and isinstance(data, dict):
        disc_ipc_instance.set_voice(
            mute=bool(data["mute"]) if "mute" in data else None,
            deaf=bool(data["deaf"]) if "deaf" in data else None,
        )


async def _ws_set_sb_volume(client: Client, msg: dict) -> None:
    vol = max(0, min(200, int(msg["data"].get("val", 100))))
    with CONFIG_LOCK:
        config["soundboard_volume"] = vol
    await asyncio.to_thread(save_config)
    if get_os_target() == "linux":
        await asyncio.to_thread(
            subprocess.run,
            ["pactl", "set-sink-volume", "Dashboard-Soundboard", f"{vol}%"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def _toggle_feedback(enable: bool):
    try:
        mods_out = subprocess.check_output(["pactl", "list", "short", "modules"]).decode()
        if enable:
            if "source=Dashboard-Soundboard.monitor" not in mods_out:
                real_sink = subprocess.check_output(["pactl", "get-default-sink"]).decode().strip()
                subprocess.run(["pactl", "load-module", "module-loopback",
                                "source=Dashboard-Soundboard.monitor",
                                f"sink={real_sink}",
                                "sink_dont_move=1",
                                "source_dont_move=1"], check=True)
        else:
            for line in mods_out.splitlines():
                if "source=Dashboard-Soundboard.monitor" in line and "module-loopback" in line:
                    mod_id = line.split()[0]
                    subprocess.run(["pactl", "unload-module", mod_id], check=True)
    except Exception as e:
        logger.error(f"Failed to toggle soundboard feedback: {e}")


async def _ws_toggle_sb_feedback(client: Client, msg: dict) -> None:
    if get_os_target() != "linux":
        return
    with CONFIG_LOCK:
        new_val = not config.get("soundboard_feedback", True)
        config["soundboard_feedback"] = new_val
    await asyncio.to_thread(save_config)
    await asyncio.to_thread(_toggle_feedback, new_val)
    # Send a targeted update instead of a full config_sync to avoid UI resets
    await ws_manager.broadcast({"type": "sb_feedback_state", "data": new_val})


async def _ws_set_theme(client: Client, msg: dict) -> None:
    # Targeted message rather than config_sync: a full sync would reset
    # fields another client is still editing in its settings modal.
    payload  = msg.get("data") if isinstance(msg.get("data"), dict) else {}
    theme_id = str(payload.get("id", ""))
    ground   = str(payload.get("ground", ""))
    if THEME_ID_RE.fullmatch(theme_id):
        with CONFIG_LOCK:
            config["theme"] = theme_id
            if HEX_COLOUR_RE.fullmatch(ground):
                config["theme_ground"] = ground
        await asyncio.to_thread(save_config)
        await ws_manager.broadcast({"type": "theme_state", "data": theme_id})


async def _ws_set_play_key(client: Client, msg: dict) -> None:
    """{cover: bool}: the play key in the cover's colour or the theme's. Targeted,
    like set_theme, so nobody's half-edited settings are reset."""
    cover = msg["data"]["cover"]
    if not isinstance(cover, bool):
        raise TypeError("cover must be a boolean")
    with CONFIG_LOCK:
        config["play_key_cover"] = cover
    await asyncio.to_thread(save_config)
    await ws_manager.broadcast({"type": "play_key_state", "data": cover})


SEEK_MAX_S = 24 * 3600


def _seconds_arg(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError("seconds must be a finite number")
    return max(-SEEK_MAX_S, min(SEEK_MAX_S, float(value)))


async def _ws_media_seek(client: Client, msg: dict) -> None:
    """{to: s} (absolute, the page's own clock) or {by: s} (relative, when the
    page does not know the position) on the shown source."""
    data = msg["data"]
    if ("to" in data) == ("by" in data):
        raise ValueError("send one of to / by")
    if "to" in data:
        await _media_command("seek_to", max(0.0, _seconds_arg(data["to"])), client.ws)
    else:
        await _media_command("seek_by", _seconds_arg(data["by"]), client.ws)


async def _ws_media_source(client: Client, msg: dict) -> None:
    """{id}: show and control that source until it closes or another starts
    playing; "" goes back to following whatever plays."""
    global media_pin
    sid = msg["data"]["id"]
    if not isinstance(sid, str):
        raise TypeError("id must be a string")
    if sid and not any(s["id"] == sid for s in media_sources):
        raise ValueError("no such source")
    media_pin = {"id": sid, "at": time.monotonic()} if sid else None
    if sys_data_trigger:
        sys_data_trigger.set()


async def _ws_media_like(client: Client, msg: dict) -> None:
    """{liked: bool}: the state asked for (not a toggle), for the shown Spotify item."""
    liked = msg["data"]["liked"]
    if not isinstance(liked, bool):
        raise TypeError("liked must be a boolean")
    t = media_shown
    if t and t.get("can_like") and t.get("api_behind"):
        await _media_result(client.ws, "like", False)   # the key shows unlit: take the tap back
        return
    if not (t and t.get("can_like") and t.get("item_id")):
        raise ValueError("nothing to like")
    ok = await asyncio.to_thread(media_module.spotify_set_liked, get_sp_oauth(),
                                 t.get("item_type", "track"), t["item_id"], liked, REQ_SESSION)
    if ok:
        spotify_like.update(item=t["item_id"], liked=liked)
    if sys_data_trigger:
        sys_data_trigger.set()
    await _media_result(client.ws, "like", ok)


async def _ws_run_speedtest(client: Client, msg: dict) -> None:
    asyncio.create_task(_run_speedtest())


async def _ws_play_clip(client: Client, msg: dict) -> None:
    clip_id = _lookup_id(msg)
    source = _clip_source(config)
    pads = config.get("local_buttons" if source == "local" else "soundpad_buttons", [])
    pad = next((p for p in pads if isinstance(p, dict) and p.get("uid") == clip_id), None)
    if pad is None:
        raise ValueError(f"no clip {clip_id!r}")
    await _play_clip(pad, source)


async def _ws_check_update(client: Client, msg: dict) -> None:
    await client.ws.send_text(_dumps({"type": "update_check", "data": await check_for_updates()}))


async def _ws_run_update(client: Client, msg: dict) -> None:
    if not (update_available and RELEASE_TAG_RE.fullmatch(latest_version)):
        await _send(client.ws, _refused("run_update", "no_update"))
    elif not _start_update(latest_version):
        await _send(client.ws, _refused("run_update", "busy"))


async def _ws_pair_decision(client: Client, msg: dict) -> None:
    data = msg["data"]
    if not pairing.decide(data["id"], data.get("allow") is True):
        await _send(client.ws, _refused("pair_decision", "expired"))


async def _ws_rename_device(client: Client, msg: dict) -> None:
    device_id, name = msg["data"]["id"], msg["data"]["name"]
    if not (isinstance(device_id, str) and isinstance(name, str)):
        raise TypeError("id and name must be strings")
    if not await asyncio.to_thread(paired_devices.rename, device_id, name):
        raise ValueError("unknown device")
    for c in ws_manager.device_clients(device_id):
        c.name = security.clean_device_name(name)
    await _broadcast_devices()


async def _ws_revoke_device(client: Client, msg: dict) -> None:
    device_id = msg["data"]["id"]
    if not isinstance(device_id, str):
        raise TypeError("id must be a string")
    await _revoke_device(device_id, WS_CLOSE_UNAUTHORIZED, "revoked")


async def _ws_unpair_self(client: Client, msg: dict) -> None:
    """"Forget this PC" on the tablet: its token stops working here too."""
    if not client.device_id:
        raise ValueError("not a paired device")
    await _revoke_device(client.device_id, 1000, "forgotten")


async def _revoke_device(device_id: str, code: int, reason: str) -> None:
    """Delete the device's entry, then close every socket it has open."""
    if not await asyncio.to_thread(paired_devices.revoke, device_id):
        raise ValueError("unknown device")
    logger.info("Paired device %s removed (%s)", device_id, reason)
    for c in ws_manager.device_clients(device_id):
        ws_manager.remove(c)
        await _close(c.ws, code, reason)
    await _broadcast_devices()


WS_HANDLERS = {
    "req_local_sounds":   _ws_req_local_sounds,
    "req_apps_list":      _ws_req_apps_list,
    "save_macros":        _ws_save_macros,
    "run_macro":          _ws_run_macro,
    "save_config":        _ws_save_config,
    "action":             _ws_action,
    "set_volume":         _ws_set_volume,
    "disc_voice":         _ws_disc_voice,
    "set_sb_volume":      _ws_set_sb_volume,
    "toggle_sb_feedback": _ws_toggle_sb_feedback,
    "set_theme":          _ws_set_theme,
    "set_play_key":       _ws_set_play_key,
    "media_seek":         _ws_media_seek,
    "media_source":       _ws_media_source,
    "media_like":         _ws_media_like,
    "run_speedtest":      _ws_run_speedtest,
    "play_clip":          _ws_play_clip,
    "check_update":       _ws_check_update,
    "run_update":         _ws_run_update,
    "pair_decision":      _ws_pair_decision,
    "rename_device":      _ws_rename_device,
    "revoke_device":      _ws_revoke_device,
    "unpair_self":        _ws_unpair_self,
}

# Names a client may send as {"type": "action", "data": <name>}. Launchers
# (app_term, app_soundpad, ...) are not here: they run only as a configured macro.
CLIENT_ACTIONS = (
    "sys_power_off", "stop_local_audio",
    "spot_play", "spot_next", "spot_prev", "spot_clear_auth",
    "disc_auth", "disc_clear_auth", "disc_mute", "disc_deaf", "disc_disconnect", "disc_cam", "disc_screen",
    "audio_cycle", "audio_mute_spk", "audio_mute_mic",
)
KNOWN_ACTIONS = frozenset(WS_HANDLERS.keys() - {"action"}) | {f"action:{a}" for a in CLIENT_ACTIONS}

# Settings, macros and pads, pairing, updates: the PC only.
ADMIN_ACTIONS = frozenset({
    "save_config", "save_macros", "set_theme", "set_play_key", "req_local_sounds", "req_apps_list",
    "check_update", "run_update", "pair_decision", "rename_device", "revoke_device",
    "action:spot_clear_auth", "action:disc_auth", "action:disc_clear_auth",
})
# Paired devices may use these only while device_av_controls is on.
DEVICE_AV_ACTIONS = frozenset({"action:disc_cam", "action:disc_screen"})





SPEEDTEST_LIVE_INTERVAL = 0.25   # s between live throughput readings
SPEEDTEST_LIVE_SMOOTHING = 0.5   # weight of the newest reading (exponential average)


async def _speedtest_live(step: str) -> None:
    """Broadcast the live throughput of a speed test phase until cancelled.

    Read from the network interface counters: the test saturates the link, so
    they track it closely, and speedtest-cli itself reports nothing until a
    phase ends. The figures shown at the end are speedtest-cli's own.
    """
    key = "bytes_recv" if step == "down" else "bytes_sent"

    def read() -> tuple[float, int]:
        return time.monotonic(), getattr(psutil.net_io_counters(), key)

    last_t, last_b = await asyncio.to_thread(read)
    smoothed = None
    while True:
        await asyncio.sleep(SPEEDTEST_LIVE_INTERVAL)
        now_t, now_b = await asyncio.to_thread(read)
        if now_t <= last_t:
            continue
        mbps = max(0, now_b - last_b) * 8 / (now_t - last_t) / 1e6
        last_t, last_b = now_t, now_b
        smoothed = mbps if smoothed is None else smoothed + SPEEDTEST_LIVE_SMOOTHING * (mbps - smoothed)
        await ws_manager.broadcast({"type": "speedtest_live", "data": {"step": step, "mbps": round(smoothed, 1)}})


async def _run_speedtest():
    """Ping, download, upload, each reported as it happens.

    speedtest_progress {step: connect|ping|down|up, progress, ping?, server?},
    speedtest_live {step, mbps} about four times a second while a phase runs,
    speedtest_intermediate {down} after the download, then speedtest_result
    {down, up, ping, server}, or {down: "ERR"|"BUSY", up: ...}.
    """
    if speedtest_lock is None:
        return
    if speedtest_lock.locked():
        await ws_manager.broadcast({"type": "speedtest_result",
                                     "data": {"down": "BUSY", "up": "BUSY"}})
        return
    async with speedtest_lock:
        live = None
        try:
            loop = asyncio.get_running_loop()

            def progress_cb(step):
                def cb(i, total, start=False, **kwargs):
                    if total > 0:
                        asyncio.run_coroutine_threadsafe(ws_manager.broadcast(
                            {"type": "speedtest_progress", "data": {"step": step, "progress": i / total}}), loop)
                return cb

            await ws_manager.broadcast({"type": "speedtest_progress", "data": {"step": "connect", "progress": 0}})
            st   = await asyncio.to_thread(speedtest_lib.Speedtest)
            best = await asyncio.to_thread(st.get_best_server)
            ping = round(float(st.results.ping or best.get("latency") or 0), 1)
            server = ", ".join(x for x in (best.get("sponsor"), best.get("name")) if x)
            await ws_manager.broadcast({"type": "speedtest_progress", "data": {
                "step": "ping", "progress": 1.0, "ping": ping, "server": server}})

            live = asyncio.create_task(_speedtest_live("down"))
            down = await asyncio.to_thread(st.download, callback=progress_cb("down"))
            live.cancel()
            await ws_manager.broadcast({"type": "speedtest_intermediate",
                                         "data": {"down": round(down / 1e6, 1)}})

            live = asyncio.create_task(_speedtest_live("up"))
            up = await asyncio.to_thread(st.upload, callback=progress_cb("up"))
            live.cancel()

            await ws_manager.broadcast({"type": "speedtest_result", "data": {
                "down": round(down / 1e6, 1), "up": round(up / 1e6, 1), "ping": ping, "server": server}})
        except Exception:
            logger.warning("Speed test failed", exc_info=True)
            await ws_manager.broadcast({"type": "speedtest_result",
                                         "data": {"down": "ERR", "up": "ERR"}})
        finally:
            if live is not None:
                live.cancel()


SOUNDPAD_INDEX_RE = re.compile(r"[0-9]{1,6}")


async def _play_clip(pad: dict, source: str) -> None:
    """Play a configured soundboard pad, resolved here from the config.

    A local file must resolve inside the sounds folder; a Soundpad index must
    be a number, because Soundpad parses its -rc argument as a command.
    """
    if source == "local":
        filepath = await asyncio.to_thread(resolve_sound_path, str(pad.get("id", "")))
        if not filepath:
            raise ValueError("clip is missing or outside the sounds folder")
        play_path = await asyncio.to_thread(get_normalized_sound_path, filepath)
        await asyncio.to_thread(AudioSystem.play_local_sound, play_path)
        # Immediately wire monitor ports into capture consumers.
        # pw-play needs ~0.3 s to register its ports in the PipeWire graph
        # before pw-link can see them, so we delay the trigger slightly.
        if get_os_target() == "linux" and sb_route_event is not None:
            async def _delayed_route():
                await asyncio.sleep(0.3)
                sb_route_event.set()
            asyncio.create_task(_delayed_route(), name="sb_route_trigger")
        return

    index = str(pad.get("id", ""))
    if not SOUNDPAD_INDEX_RE.fullmatch(index):
        raise ValueError("Soundpad index must be a number")
    if sys.platform.startswith("win"):
        for sp_path in (
            r"C:\Program Files\Soundpad\Soundpad.exe",
            r"C:\Program Files (x86)\Steam\steamapps\common\Soundpad\Soundpad.exe",
        ):
            if os.path.exists(sp_path):
                await asyncio.to_thread(
                    subprocess.run, [sp_path, "-rc", f"DoPlaySound({int(index)})"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                break


# ── Media keys ───────────────────────────────────────────────────────────────
# Every key goes to the source the card shows (media_shown), through that
# source's own channel: MPRIS, SMTC, or Spotify's Web API for a remote device.

async def _media_position(t: dict) -> float | None:
    """The shown source's position now, for Previous and relative seeks."""
    via = t["via"]
    if via == "mpris" and mpris_mon is not None:
        return await asyncio.to_thread(mpris_mon.position, t["bus"])
    if via == "playerctl":
        return await asyncio.to_thread(media_module.playerctl_position, t["instance"])
    if via == "smtc":
        return await media_module.smtc_position(t["aumid"])
    pos = t["position"]
    if pos is not None and t["status"] == "Playing" and t.get("fetched_at"):
        pos += time.monotonic() - t["fetched_at"]
    return pos


def _spotify_api_do(t: dict, op: str, arg: float | None) -> bool:
    info = media_module.spotify_token(get_sp_oauth())
    if not info:
        return False
    sp = media_module.spotify_client(info, REQ_SESSION)
    try:
        if op == "play":
            # The cached state: one round trip, not a GET first.
            sp.pause_playback() if t["status"] == "Playing" else sp.start_playback()
        elif op == "next":
            sp.next_track()
        elif op == "prev":
            sp.previous_track()
        elif op in ("seek", "seek_rel") and arg is not None:
            if op == "seek_rel":
                pos = t["position"] or 0.0
                if t["status"] == "Playing" and t.get("fetched_at"):
                    pos += time.monotonic() - t["fetched_at"]
                arg += pos
            sp.seek_track(int(max(0.0, arg) * 1000))
        else:
            return False
        return True
    except Exception as e:   # 403 without Premium, a device that went away
        logger.info(f"Spotify Web API {op} failed: {e}")
        return False


async def _media_do(t: dict, op: str, arg: float | None = None) -> bool:
    """play | next | prev | seek (absolute s) | seek_rel (s) on one source."""
    via = t["via"]
    if via == "mpris" and mpris_mon is not None:
        if op == "seek":
            return await asyncio.to_thread(mpris_mon.set_position, t["bus"], arg, t["trackid"], t["position"])
        if op == "seek_rel":
            return await asyncio.to_thread(mpris_mon.call, t["bus"], "Seek", "x", int(arg * 1e6))
        method = {"play": "PlayPause", "next": "Next", "prev": "Previous"}[op]
        return await asyncio.to_thread(mpris_mon.call, t["bus"], method)
    if via == "playerctl":
        if op == "seek":
            args = ["position", f"{max(0.0, arg):.2f}"]
        elif op == "seek_rel":
            args = ["position", f"{abs(arg):.2f}{'+' if arg >= 0 else '-'}"]
        else:
            args = [{"play": "play-pause", "next": "next", "prev": "previous"}[op]]
        return await asyncio.to_thread(media_module.playerctl_command, t["instance"], *args)
    if via == "smtc":
        if op == "seek_rel":
            pos = await media_module.smtc_position(t["aumid"])
            if pos is None:
                return False
            op, arg = "seek", pos + arg
        return await media_module.smtc_command(t["aumid"], op, arg)
    if via == "spotify_api":
        return await asyncio.to_thread(_spotify_api_do, t, op, arg)
    return False


async def _media_keys(cmd: str) -> None:
    """Nothing to target (nothing shown, or Windows refused): what the keys
    did before, playerctl's first player on Linux, else media keys."""
    if cmd not in ("play", "next", "prev"):
        return
    if sys.platform.startswith("linux") and await asyncio.to_thread(
            media_module.playerctl_command, "", {"play": "play-pause", "next": "next", "prev": "previous"}[cmd]):
        return
    key = {"play": "KEY_PLAYPAUSE", "next": "KEY_NEXTSONG", "prev": "KEY_PREVIOUSSONG"}[cmd]
    await asyncio.to_thread(MacroSystem.send_keys, key)


async def _media_result(ws: WebSocket | None, cmd: str, ok: bool) -> None:
    """Tell the screen that sent a media key whether it went through: the page
    shows the result at once and takes it back only on ok: false."""
    if ws is not None:
        await _send(ws, {"type": "media_result", "data": {"cmd": cmd, "ok": ok}})


async def _media_command(cmd: str, value: float | None = None, ws: WebSocket | None = None) -> None:
    """play | next | prev | seek_to | seek_by on the shown source."""
    global force_media_update, spotify_recheck_at
    t = media_shown
    if t is None:
        await _media_keys(cmd)
        force_media_update = True
        await _media_result(ws, cmd, True)    # sent blind: nothing to check
        return
    if cmd == "prev":
        plan = media_module.prev_plan(await _media_position(t), t["caps"])
        ok = await _media_do(t, "seek", 0.0) if plan == "restart" else await _media_do(t, "prev")
    elif cmd == "seek_to":
        if t["length"]:
            value = min(value, max(0.0, t["length"] - 1.0))   # past the end MPRIS skips a track
        ok = await _media_do(t, "seek", max(0.0, value))
    elif cmd == "seek_by":
        ok = await _media_do(t, "seek_rel", value)
    else:
        ok = await _media_do(t, cmd)
    if not ok:
        logger.info(f"media {cmd} on {t['name'] or t['id']} ({t['via']}) did not go through")
        if t["via"] == "smtc":
            await _media_keys(cmd)
    # The MPRIS monitor reports the result by itself; a forced read waits
    # FORCED_MEDIA_DELAY plus a round trip, so only polled sources (and
    # Spotify, whose cover and like state come from the Web API) get one.
    # Spotify's runs off the tick: the monitor's state goes out first.
    force_media_update = t["via"] != "mpris" or t["app"] == "spotify"
    if ok and t["app"] == "spotify":
        spotify_recheck_at = time.monotonic() + SPOTIFY_RECHECK_S
    if sys_data_trigger:
        sys_data_trigger.set()
    await _media_result(ws, "seek" if cmd.startswith("seek") else cmd, ok)


async def _handle_action(ws: WebSocket, action: str):
    """Dispatch an 'action' message from the WebSocket client."""
    global force_media_update

    # ── system actions ────────────────────────────────────────────────────────
    if action == "sys_power_off":
        if sys.platform.startswith("win"):
            try:
                import comtypes.client
                shell = comtypes.client.CreateObject("Shell.Application")
                shell.ShutdownWindows()
            except Exception as e:
                logger.error(f"Failed to show Windows shutdown prompt: {e}")
        elif sys.platform.startswith("linux"):
            desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            try:
                if "kde" in desktop:
                    subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.kde.LogoutPrompt", "/LogoutPrompt", "org.kde.LogoutPrompt.promptShutDown"], check=False)
                elif "gnome" in desktop:
                    subprocess.run(["gnome-session-quit", "--power-off"], check=False)
                elif "xfce" in desktop:
                    subprocess.run(["xfce4-session-logout"], check=False)
                elif "cinnamon" in desktop:
                    subprocess.run(["cinnamon-session-quit", "--power-off"], check=False)
                elif "mate" in desktop:
                    subprocess.run(["mate-session-save", "--shutdown-dialog"], check=False)
                else:
                    logger.warning(f"Unsupported DE for shutdown prompt: {desktop}")
            except Exception as e:
                logger.error(f"Failed to show Linux shutdown prompt: {e}")
        return

    if action == "stop_local_audio":
        await asyncio.to_thread(AudioSystem.stop_local_sound)
        if sys.platform.startswith("win"):
            for sp_path in (
                r"C:\Program Files\Soundpad\Soundpad.exe",
                r"C:\Program Files (x86)\Steam\steamapps\common\Soundpad\Soundpad.exe",
            ):
                if os.path.exists(sp_path):
                    await asyncio.to_thread(
                        subprocess.run, [sp_path, "-rc", "DoStopSound()"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    break
        return

    # ── Spotify / media ───────────────────────────────────────────────────────
    if action.startswith("spot_"):
        if action == "spot_clear_auth":
            global global_sp_oauth
            global_sp_oauth = None
            with CONFIG_LOCK:
                config["spot_token"] = ""
            await asyncio.to_thread(save_config)
            def _rm_spot_cache():
                if os.path.exists(SPOTIFY_CACHE_FILE):
                    try: os.remove(SPOTIFY_CACHE_FILE)
                    except Exception: pass
            await asyncio.to_thread(_rm_spot_cache)
            return

        cmd = {"spot_play": "play", "spot_next": "next", "spot_prev": "prev"}.get(action)
        if cmd:
            await _media_command(cmd, ws=ws)
        return

    # ── Discord ────────────────────────────────────────────────────────────────
    if action == "disc_clear_auth":
        with CONFIG_LOCK:
            config["disc_token"] = ""
        await asyncio.to_thread(save_config)
        restart_discord_ipc()
        return

    if action == "disc_auth":
        auth_url = _discord_auth_url() if disc_ipc_instance else ""
        if disc_ipc_instance and disc_ipc_instance.connected:
            # Standard Discord (and Vesktop): trigger native in-app consent popup via IPC.
            disc_ipc_instance.send(1, {
                "cmd": "AUTHORIZE",
                "args": {"client_id": disc_ipc_instance.client_id,
                          "scopes": ["rpc", "rpc.notifications.read", "rpc.voice.read", "rpc.video.read", "rpc.screenshare.read", "rpc.activities.write", "rpc.screenshare.write", "rpc.video.write", "rpc.voice.write"]},
                "nonce": "AUTHORIZE_REQ",
            })
            
            # Standard Discord silently drops the AUTHORIZE frame if scopes are not
            # whitelisted (like rpc.voice.read). Start a timer to open the browser if so.
            if auth_url:
                async def _fallback_timer():
                    await asyncio.sleep(1.5)
                    if disc_ipc_instance and getattr(disc_ipc_instance, 'auth_pending', False):
                        logger.warning("Discord IPC AUTHORIZE timed out (silently dropped?). Opening browser.")
                        await asyncio.to_thread(webbrowser.open, auth_url)
                asyncio.create_task(_fallback_timer())
        elif auth_url:
            # Not yet connected — open the auth URL directly so the user can
            # grant permission; the server-side callback will handle the code.
            await asyncio.to_thread(webbrowser.open, auth_url)
        return

    if action == "disc_mute":
        if disc_ipc_instance and disc_ipc_instance.connected:
            # Flip the local copy too: a second tap before Discord answers must
            # toggle back, not resend the same state (older clients only toggle).
            vs = disc_ipc_instance.voice_state
            vs["mute"] = not vs.get("mute", False)
            disc_ipc_instance.send(1, {"cmd": "SET_VOICE_SETTINGS", "args": {"mute": vs["mute"]}, "nonce": "MUTE"})
        return

    if action == "disc_deaf":
        if disc_ipc_instance and disc_ipc_instance.connected:
            vs = disc_ipc_instance.voice_state
            vs["deaf"] = not vs.get("deaf", False)
            disc_ipc_instance.send(1, {"cmd": "SET_VOICE_SETTINGS", "args": {"deaf": vs["deaf"]}, "nonce": "DEAF"})
        return

    if action == "disc_disconnect":
        if disc_ipc_instance and disc_ipc_instance.connected:
            disc_ipc_instance.send(1, {"cmd": "SELECT_VOICE_CHANNEL",
                                        "args": {"channel_id": None},
                                        "nonce": str(uuid.uuid4())})
        return

    if action == "disc_cam":
        if disc_ipc_instance and disc_ipc_instance.connected:
            disc_ipc_instance.toggle_video()
        return

    if action == "disc_screen":
        if disc_ipc_instance and disc_ipc_instance.connected:
            # Discord's TOGGLE_SCREENSHARE stops a share, streams a game it has
            # detected, or else opens its own "share your screen" picker inside
            # its window (choosing an app by pid is Windows-only, and on Wayland
            # the system asks what to share anyway). In a minimized or tray window
            # that picker is invisible, so starting a share brings Discord forward
            # first. Stopping one needs nothing on screen.
            starting = not disc_ipc_instance.voice_state.get("screenshare")
            if starting:
                await asyncio.to_thread(_show_discord)
                await asyncio.sleep(DISCORD_FRONT_DELAY_S)
            disc_ipc_instance.toggle_screenshare()
            logger.info("Discord: screen share %s", "toggled on: Discord brought forward for its picker"
                        if starting else "toggled off")
        return

    # ── system apps ───────────────────────────────────────────────────────────
    if action == "app_term":
        await asyncio.to_thread(_launch_terminal)
        return

    if action == "app_web":
        await asyncio.to_thread(_launch_browser)
        return

    if action == "app_files":
        await asyncio.to_thread(_launch_file_manager)
        return

    if action == "app_task":
        await asyncio.to_thread(_launch_task_manager)
        return

    if action == "app_soundpad":
        if sys.platform.startswith("win"):
            for sp_path in (
                r"C:\Program Files\Soundpad\Soundpad.exe",
                r"C:\Program Files (x86)\Steam\steamapps\common\Soundpad\Soundpad.exe",
            ):
                if os.path.exists(sp_path):
                    subprocess.Popen([sp_path])
                    break
        return

    # ── audio ─────────────────────────────────────────────────────────────────
    if action == "audio_cycle":
        await asyncio.to_thread(AudioSystem.cycle_device, config.get("audio_names", {}),
                                config.get("hidden_audio_devices", []), config.get("audio_order", []))
        return

    if action == "audio_mute_spk":
        await asyncio.to_thread(
            AudioSystem.toggle_mute, "@DEFAULT_AUDIO_SINK@", MacroSystem.send_keys
        )
        return

    if action == "audio_mute_mic":
        await asyncio.to_thread(
            AudioSystem.toggle_mute, "@DEFAULT_AUDIO_SOURCE@", MacroSystem.send_keys
        )
        return


# ─── App launch helpers ────────────────────────────────────────────────────────

# Time for Discord to come forward (its launcher hands over to the running
# window) before the share picker opens in it.
DISCORD_FRONT_DELAY_S = 0.6


def _show_discord() -> None:
    """Bring the running Discord app to the front without changing what it shows.

    A discord:// link navigates: a bare one opens Discord's home route, the
    Friends page. Started again with no link, Discord only shows and focuses its
    window, so this starts the app registered for discord:// links with nothing
    to open."""
    try:
        if sys.platform.startswith("win"):
            import winreg
            # '"C:\...\app-1.0.x\Discord.exe" --url -- "%1"', written by Discord itself
            command = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, r"discord\shell\open\command")
            exe = re.match(r'\s*(?:"([^"]+)"|(\S+))', command)
            if exe:
                _popen([exe.group(1) or exe.group(2)])
        elif shutil.which("xdg-mime") and shutil.which("gtk-launch"):
            entry = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/discord"],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            if entry.endswith(".desktop"):
                # Exec=/usr/bin/discord --url -- %u: with no link, %u is dropped
                _popen(["gtk-launch", entry])
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"Could not bring Discord forward: {e}")


def _popen(cmd: list[str], **kwargs):
    import typing
    kwargs.setdefault("stdout", subprocess.DEVNULL)
    kwargs.setdefault("stderr", subprocess.DEVNULL)
    if sys.platform != "win32":
        kwargs.setdefault("start_new_session", True)
    
    popen_cls: typing.Any = subprocess.Popen
    popen_cls(cmd, **kwargs)


def _launch_terminal():
    if sys.platform.startswith("win"):
        if shutil.which("wt.exe"):
            return _popen(["wt.exe"])
        return _popen(["cmd.exe", "/c", "start", "cmd.exe"])
    de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if os.environ.get("TERMINAL") and shutil.which(os.environ["TERMINAL"]):
        return _popen([os.environ["TERMINAL"]])
    if "kde" in de:
        for kread in ("kreadconfig6", "kreadconfig5"):
            if shutil.which(kread):
                try:
                    t = subprocess.check_output(
                        [kread, "--file", "kdeglobals", "--group", "General",
                         "--key", "TerminalApplication"],
                        stderr=subprocess.DEVNULL,
                    ).decode().strip()
                    if t and shutil.which(t):
                        return _popen([t])
                except Exception:
                    pass
    if "gnome" in de or "cinnamon" in de or "mate" in de:
        if shutil.which("gsettings"):
            schemas = {
                "gnome":    "org.gnome.desktop.default-applications.terminal",
                "cinnamon": "org.cinnamon.desktop.default-applications.terminal",
                "mate":     "org.mate.applications-terminal",
            }
            for key, schema in schemas.items():
                if key in de:
                    try:
                        t = subprocess.check_output(
                            ["gsettings", "get", schema, "exec"],
                            stderr=subprocess.DEVNULL,
                        ).decode().strip().strip("'\"")
                        if t and shutil.which(t):
                            return _popen([t])
                    except Exception:
                        pass
    if "xfce" in de and shutil.which("exo-open"):
        return _popen(["exo-open", "--launch", "TerminalEmulator"])
    for w in ("xdg-terminal-exec", "xdg-terminal", "i3-sensible-terminal"):
        if shutil.which(w):
            return _popen([w])
    for t in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
              "mate-terminal", "lxterminal", "alacritty", "kitty", "wezterm", "xterm"):
        if shutil.which(t):
            return _popen([t])


def _launch_browser():
    if sys.platform.startswith("win"):
        return _popen(["cmd.exe", "/c", "start", "http://"])
    de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if os.environ.get("BROWSER") and shutil.which(os.environ["BROWSER"]):
        return _popen([os.environ["BROWSER"]])
    if shutil.which("xdg-open"):
        return _popen(["xdg-open", "http://"])
    for b in ("firefox", "brave", "google-chrome", "chromium", "vivaldi"):
        if shutil.which(b):
            return _popen([b])


def _launch_file_manager():
    if sys.platform.startswith("win"):
        return _popen(["explorer.exe"])
    if os.environ.get("FILEMANAGER") and shutil.which(os.environ["FILEMANAGER"]):
        return _popen([os.environ["FILEMANAGER"]])
    home = os.path.expanduser("~")
    if shutil.which("xdg-open"):
        return _popen(["xdg-open", home])
    if shutil.which("gio"):
        return _popen(["gio", "open", home])
    de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "xfce" in de and shutil.which("exo-open"):
        return _popen(["exo-open", "--launch", "FileManager"])
    if shutil.which("xdg-mime") and shutil.which("gtk-launch"):
        try:
            entry = subprocess.check_output(
                ["xdg-mime", "query", "default", "inode/directory"],
                stderr=subprocess.DEVNULL, timeout=2,
            ).decode().strip()
            if entry.endswith(".desktop"):
                return _popen(["gtk-launch", entry])
        except Exception:
            pass
    candidates: list[str] = []
    if "kde" in de:        candidates = ["dolphin"]
    elif "gnome" in de:    candidates = ["nautilus"]
    elif "cinnamon" in de: candidates = ["nemo"]
    elif "mate" in de:     candidates = ["caja"]
    elif "xfce" in de:     candidates = ["thunar"]
    elif "lxqt" in de:     candidates = ["pcmanfm-qt", "pcmanfm"]
    for fm in candidates:
        if shutil.which(fm):
            return _popen([fm])
    for fm in ("dolphin", "nautilus", "nemo", "thunar", "caja", "pcmanfm-qt", "pcmanfm"):
        if shutil.which(fm):
            return _popen([fm])


def _launch_task_manager():
    if sys.platform.startswith("win"):
        return _popen(["taskmgr.exe"])
    de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    candidates: list[str] = []
    if "kde"      in de: candidates = ["plasma-systemmonitor", "ksysguard"]
    elif "gnome"  in de: candidates = ["gnome-system-monitor"]
    elif "xfce"   in de: candidates = ["xfce4-taskmanager"]
    elif "mate"   in de: candidates = ["mate-system-monitor"]
    else: candidates = ["plasma-systemmonitor", "gnome-system-monitor",
                         "xfce4-taskmanager", "ksysguard"]
    for t in candidates:
        if shutil.which(t):
            return _popen([t])
    # CLI fallback
    for cli in ("htop", "top"):
        if shutil.which(cli):
            term = shutil.which("x-terminal-emulator") or shutil.which("xterm")
            if term:
                return _popen([term, "-e", cli])


# (Windows desktop helpers — configure_windows_app_identity, set_windows_window_icon,
#  _int_handle, _get_window_hwnd — are now in desktop.py)



# (DesktopTrayApp removed — window and tray are now in desktop.py via pywebview + pystray)


# ─── Server lifecycle ──────────────────────────────────────────────────────────

def _wait_for_server(port: int, timeout: float = 10.0) -> bool:
    """Poll until the uvicorn server accepts TCP connections or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    logger.warning(f"Server did not become ready on port {port} within {timeout}s")
    return False


def stop_fastapi_server():
    if uvicorn_server: uvicorn_server.should_exit = True


def run_fastapi_server(host="0.0.0.0", port=8888, reload=False):
    global uvicorn_server
    # The Host / Origin allowlists name this port; --reload's child reads it from the environment.
    os.environ["TD_PORT"] = str(port)
    origin_policy.port = port
    if reload:
        uvicorn.run("server:_app", host=host, port=port, reload=True, ws_max_size=WS_MAX_MESSAGE_BYTES,
                    reload_dirs=[BASE_DIR, os.path.join(RESOURCE_DIR, "templates")])
    else:
        cfg = uvicorn.Config(_app, host=host, port=port, log_level="warning",
                             ws_max_size=WS_MAX_MESSAGE_BYTES)
        uvicorn_server = uvicorn.Server(cfg)
        uvicorn_server.run()


def launch_desktop(host: str = "0.0.0.0", port: int = 8888) -> None:
    """Start the pywebview desktop window with pystray system tray."""
    _desktop_module.launch_desktop(
        host=host,
        port=port,
        icon_path=APP_ICON_PATH,
        data_dir=DATA_DIR,
        run_server_fn=run_fastapi_server,
        stop_server_fn=stop_fastapi_server,
        wait_server_fn=_wait_for_server,
        get_ip_fn=get_lan_ip,
        app_user_model_id=WINDOWS_APP_USER_MODEL_ID,
        background_color=_theme_ground(),
    )


# Burn-in care on tablets (Settings → General → Screen care): minutes, 0 = never.
IDLE_CHOICES = {"idle_dim_min": (0, 2, 5, 10), "idle_clock_min": (0, 10, 15, 30)}


def _idle_minutes(key: str, value, choices: tuple[int, ...]) -> int:
    """An idle delay from a client: one of the offered choices, else the default."""
    if isinstance(value, int) and not isinstance(value, bool) and value in choices:
        return value
    return DEFAULT_CONFIG[key]


def _clean_sink_names(value) -> list[str]:
    """hidden_audio_devices / audio_order from a client: distinct raw sink names, bounded."""
    if not isinstance(value, list):
        return []
    names = [v for v in value if isinstance(v, str) and 0 < len(v) <= 256]
    return list(dict.fromkeys(names))[:64]


def _theme_ground() -> str:
    ground = str(config.get("theme_ground", ""))
    return ground if HEX_COLOUR_RE.fullmatch(ground) else DEFAULT_CONFIG["theme_ground"]


# ─── Entry point ───────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Touch Dashboard")
    p.add_argument("--server-only", action="store_true")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8888)
    p.add_argument("--reload", action="store_true")
    return p.parse_args()


def _write_crash_report(exc_type, exc_value, exc_tb, thread_name: str = "main") -> tuple[str, str]:
    """Write a timestamped crash report to DATA_DIR; return (file_path, error_code)."""
    import traceback
    import hashlib
    raw        = f"{getattr(exc_type, '__name__', str(exc_type))}:{exc_value}"
    error_code = "TD-" + hashlib.md5(raw.encode(errors="replace")).hexdigest()[:8].upper()
    ts         = time.strftime("%Y%m%d_%H%M%S")
    crash_path = os.path.join(DATA_DIR, f"crash_{ts}.txt")
    try:
        with open(crash_path, "w", encoding="utf-8") as f:
            f.write("Touch Dashboard — Crash Report\n")
            f.write(f"Error Code : {error_code}\n")
            f.write(f"Timestamp  : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Thread     : {thread_name}\n")
            f.write(f"Platform   : {sys.platform} | Python {sys.version}\n")
            f.write("-" * 60 + "\n\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except Exception:
        pass
    return crash_path, error_code


def _show_crash_dialog(error_code: str, crash_path: str):
    """Show a user-visible crash popup with an actionable error code."""
    msg = (
        f"Touch Dashboard encountered a fatal error and must close.\n\n"
        f"Error Code:  {error_code}\n\n"
        f"Please forward this code to the developer for debugging.\n"
        f"Full report saved to:\n{crash_path}"
    )
    try:
        _show_popup("Touch Dashboard \u2014 Fatal Error", msg)
    except Exception:
        pass


def _global_exception_handler(exc_type, exc_value, exc_tb):
    """Handle uncaught exceptions on the main thread."""
    if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    crash_path, error_code = _write_crash_report(exc_type, exc_value, exc_tb)
    try:
        logger.critical(
            f"Unhandled main-thread exception [{error_code}]: {exc_value}",
            exc_info=(exc_type, exc_value, exc_tb),
        )
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)
    _show_crash_dialog(error_code, crash_path)


def _thread_exception_handler(args):
    """Handle uncaught exceptions on background threads."""
    exc_type  = args.exc_type
    exc_value = args.exc_value
    exc_tb    = args.exc_traceback
    thread    = getattr(args, "thread", None)
    if exc_type is None or issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
        return
    thread_name = getattr(thread, "name", "background")
    crash_path, error_code = _write_crash_report(exc_type, exc_value, exc_tb, thread_name)
    try:
        logger.critical(
            f"Unhandled exception in thread '{thread_name}' [{error_code}]: {exc_value}",
            exc_info=(exc_type, exc_value, exc_tb),
        )
    except Exception:
        pass
    # Show popup only for non-daemon threads — daemon crashes are usually transient.
    if not getattr(thread, "daemon", True):
        _show_crash_dialog(error_code, crash_path)


sys.excepthook = _global_exception_handler
if hasattr(threading, "excepthook"):
    threading.excepthook = _thread_exception_handler

if __name__ == "__main__":
    args = _parse_args()
    if args.server_only:
        run_fastapi_server(host=args.host, port=args.port, reload=args.reload)
    else:
        launch_desktop(host=args.host, port=args.port)
