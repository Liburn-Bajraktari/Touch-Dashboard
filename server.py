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
import urllib.request
import ipaddress
import json
import logging
import os
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
import webbrowser

# ─── pywebview storage path (before any webview import) ──────────────────────
# Disable pywebview's private mode so the WebSocket auth token persists
# across page loads. Actual storage is placed in DATA_DIR (set after imports).

# ─── Fast JSON (orjson → stdlib fallback) ─────────────────────────────────────
try:
    import orjson as _json_lib  # type: ignore[import]

    def _dumps(obj) -> str:
        return _json_lib.dumps(obj).decode()

    def _loads(s: str | bytes):
        return _json_lib.loads(s)

except ImportError:
    _dumps = json.dumps   # type: ignore[assignment]
    _loads = json.loads   # type: ignore[assignment]

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
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import uvicorn

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



try:
    from PIL import Image, ImageDraw  # type: ignore[import]
except ImportError:
    Image = ImageDraw = None

# Desktop window / tray (imported lazily in launch_desktop to avoid
# initialising the GTK/WebKit2 subsystem in server-only mode).
import desktop as _desktop_module

import warnings
try:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning, message=".*pynvml.*")
        import pynvml  # type: ignore[import]
except ImportError:
    pynvml = None

# ─── Local modules ─────────────────────────────────────────────────────────────

from audio_system import AudioSystem
from discord_ipc import DiscordIPC
from macro_system import MacroSystem, AppEnumerator
import media as media_module

if sys.platform.startswith("linux"):
    import mouse_battery as _mouse_battery_mod

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
SPOTIFY_CACHE_FILE  = os.path.join(DATA_DIR, ".cache")
SOUNDS_DIR          = os.path.join(DATA_DIR, "sounds")
os.makedirs(SOUNDS_DIR, exist_ok=True)

CONFIG_LOCK = threading.RLock()

DEFAULT_CONFIG: dict = {
    "weather_api": "", "weather_city": "Pristina",
    "spot_id": "", "spot_secret": "",
    "disc_id": "", "disc_secret": "",
    "disc_enabled": True,
    "disc_vc_enabled": True,
    "show_mouse_batt": True,
    "show_hw_stats": True,
    "spot_enabled": True,
    "weather_enabled": True,
    "audio_enabled": True,
    "sb_enabled": True,
    "audio_names": {},
    "soundpad_buttons": [],
    "local_buttons": [],
    "sounds_path": "",
    "macros": [],
}


def _migrate_config():
    for old, new in (
        (os.path.join(BASE_DIR, "config.json"),       CONFIG_FILE),
        (os.path.join(BASE_DIR, ".cache"),             SPOTIFY_CACHE_FILE),
        (os.path.join(BASE_DIR, "weather_cache.json"), WEATHER_CACHE_FILE),
    ):
        if os.path.exists(old) and not os.path.exists(new):
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

# ─── Logging ───────────────────────────────────────────────────────────────────

_log_file = os.path.join(DATA_DIR, "server.log")
logging.basicConfig(
    filename=_log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

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
current_media_source = "local"
spotify_cache: dict | None = None
last_spotify_check   = 0.0
last_audio_devs: list = []
last_weather_data    = {"temp": "--", "desc": "--", "timestamp": 0}
upower_monitor       = None   # UPowerMouseMonitor instance (Linux only)
# NOTE: asyncio primitives MUST be created inside a running event loop.
# We declare them as None here and initialise them inside lifespan().
weather_update_event: asyncio.Event | None = None
last_host_url        = "127.0.0.1:8888"
speedtest_lock: asyncio.Lock | None = None

# Template cache (mtime-based)
_tmpl_cache = {"mtime": 0.0, "html": ""}

def _load_template() -> str:
    path  = os.path.join(RESOURCE_DIR, "templates", "index.html")
    mtime = os.path.getmtime(path)
    if _tmpl_cache["mtime"] != mtime:
        with open(path, encoding="utf-8") as f:
            _tmpl_cache["html"]  = f.read()
        _tmpl_cache["mtime"] = mtime
    return _tmpl_cache["html"]

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
    return (private or valid or ["LAN unavailable"])[0]


def get_sp_oauth() -> SpotifyOAuth | None:
    global global_sp_oauth
    if not config.get("spot_id") or not config.get("spot_secret"):
        return None
    if global_sp_oauth is None:
        global_sp_oauth = SpotifyOAuth(
            client_id=config["spot_id"],
            client_secret=config["spot_secret"],
            redirect_uri="http://127.0.0.1:8888/callback",
            scope="user-read-playback-state user-modify-playback-state",
            open_browser=False,
            cache_path=SPOTIFY_CACHE_FILE,
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

        def _on_auth_error(auth_url: str):
            if main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    asyncio.to_thread(webbrowser.open, auth_url),
                    main_event_loop
                )
            else:
                webbrowser.open(auth_url)

        disc_ipc_instance.on_state_change = _on_disc_change
        disc_ipc_instance.on_auth_error = _on_auth_error
        threading.Thread(target=disc_ipc_instance.loop, daemon=True,
                         name="discord-ipc").start()


# ─── WebSocket manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._conns: list[WebSocket] = []

    def has_clients(self) -> bool:
        return bool(self._conns)

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._conns.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._conns:
            self._conns.remove(ws)

    async def broadcast(self, msg: dict):
        if not self._conns:
            return
        payload = _dumps(msg)
        
        async def _send(ws: WebSocket):
            try:
                await ws.send_text(payload)
                return None
            except Exception:
                return ws
                
        results = await asyncio.gather(*[_send(ws) for ws in self._conns])
        
        stale = [ws for ws in results if ws is not None]
        for ws in stale:
            self.disconnect(ws)


ws_manager       = ConnectionManager()
main_event_loop: asyncio.AbstractEventLoop | None = None
sys_data_trigger: asyncio.Event | None            = None

# ─── Background tasks ──────────────────────────────────────────────────────────

async def hardware_loop():
    global last_spotify_check, spotify_cache, last_audio_devs
    global force_media_update, current_media_source, main_event_loop, sys_data_trigger

    main_event_loop  = asyncio.get_running_loop()
    sys_data_trigger = asyncio.Event()

    last_gpu_check  = 0.0
    gpu_cache       = ""
    last_audio_check = 0.0
    audio_cache     = {
        "spk": {"vol": 0, "muted": False},
        "mic": {"vol": 0, "muted": False},
        "active_dev": "NONE",
    }
    # mouse_batt is now a structured dict; None pct means no device found.
    batt_cache: dict = {"pct": None, "state": "unknown", "model": "", "vendor": ""}
    last_sys_data_payload = {}

    def _read_batt_windows() -> dict:
        """Windows fallback: read the legacy g502_battery.txt file."""
        candidates = [
            os.path.join(os.environ.get("TEMP", ""), "g502_battery.txt"),
        ]
        for p in candidates:
            try:
                with open(p) as f:
                    raw = f.read().strip()
                # File may contain just a number like "73" or "73%"
                pct_str = raw.replace("%", "").strip()
                return {"pct": float(pct_str), "state": "unknown",
                        "model": "", "vendor": ""}
            except Exception:
                pass
        return {"pct": None, "state": "unknown", "model": "", "vendor": ""}

    while True:
        if not ws_manager.has_clients():
            await asyncio.sleep(2.0)
            continue

        curr = time.monotonic()

        # Audio (1 Hz)
        if curr - last_audio_check >= 1.0:
            audio_names  = config.get("audio_names", {})
            audio_data   = await asyncio.to_thread(AudioSystem.poll_all, audio_names)
            curr_sinks   = audio_data["sinks"]
            if [s["raw_name"] for s in curr_sinks] != [s["raw_name"] for s in last_audio_devs]:
                last_audio_devs = curr_sinks
                await ws_manager.broadcast({"type": "hw_scan_results", "data": curr_sinks})
            audio_cache = {
                "spk": audio_data["spk"],
                "mic": audio_data["mic"],
                "active_dev": audio_data["active_sink_name"],
            }
            # Battery: on Linux, read from UPower monitor (event-driven, no file I/O).
            # On Windows, fall back to the legacy g502_battery.txt file.
            if sys.platform.startswith("linux") and upower_monitor is not None:
                batt_cache = upower_monitor.get_state()
            elif sys.platform.startswith("win"):
                batt_cache = await asyncio.to_thread(_read_batt_windows)
            last_audio_check = curr

        # Spotify (3 Hz max, or on demand)
        if time.monotonic() - last_spotify_check > 3.0 or force_media_update:
            if force_media_update:
                await asyncio.sleep(0.4)
            sp_oauth        = get_sp_oauth()
            spotify_cache   = await asyncio.to_thread(
                media_module.get_spotify_api_meta, sp_oauth, REQ_SESSION
            )
            last_spotify_check = time.monotonic()
            force_media_update = False

        # Determine active media source
        media = spotify_cache
        if not media or media["status"] != "Playing":
            if sys.platform.startswith("win"):
                local_media = await media_module.get_windows_media_meta_async()
            else:
                local_media = await asyncio.to_thread(media_module.get_local_mpris_meta)
            if local_media and local_media.get("status") in ("Playing", "Paused"):
                if not media or local_media["status"] == "Playing":
                    media, current_media_source = local_media, "local"
                else:
                    current_media_source = "spotify"
            elif media:
                current_media_source = "spotify"
        else:
            current_media_source = "spotify"

        # GPU (0.5 Hz)
        if curr - last_gpu_check > 2.0:
            if has_nvml and nvml_handle:
                try:
                    util      = await asyncio.to_thread(
                        pynvml.nvmlDeviceGetUtilizationRates, nvml_handle  # type: ignore[attr-defined]
                    )
                    gpu_cache = str(util.gpu)
                except Exception:
                    gpu_cache = ""
            last_gpu_check = curr

        # Discord state snapshot
        disc_has_token = bool(config.get("disc_token", ""))
        disc_has_creds = bool(config.get("disc_id")) and bool(config.get("disc_secret"))
        disc_state: dict = {
            "mute": False, "deaf": False,
            "auth_required": disc_has_creds and not disc_has_token,
            "auth_url": "",
            "connected": False,
            "voice_supported": False,
            "authorized": disc_has_token,
        }
        if disc_has_creds and not disc_has_token:
            scopes = "rpc rpc.notifications.read rpc.voice.read rpc.video.read rpc.screenshare.read rpc.activities.write rpc.screenshare.write rpc.video.write rpc.voice.write"
            params = urllib.parse.urlencode({
                "client_id": config['disc_id'],
                "response_type": "code",
                "redirect_uri": "http://127.0.0.1:8888/disc_callback",
                "scope": scopes
            })
            disc_state["auth_url"] = f"https://discord.com/oauth2/authorize?{params}"
        if disc_ipc_instance:
            disc_state.update({
                "connected":       disc_ipc_instance.connected,
                "voice_supported": disc_ipc_instance.voice_supported,
                "auth_pending":    disc_ipc_instance.auth_pending,
                "vesktop_ipc_warning": disc_ipc_instance.vesktop_ipc_warning,
            })
            if disc_ipc_instance.connected:
                disc_state["mute"] = getattr(disc_ipc_instance, 'voice_state', {}).get('mute', False)
                disc_state["deaf"] = getattr(disc_ipc_instance, 'voice_state', {}).get('deaf', False)
                disc_state["voice_channel"] = disc_ipc_instance.voice_channel
            if not disc_has_token and disc_ipc_instance.auth_pending:
                disc_state["auth_url"] = disc_ipc_instance.get_auth_url()

        new_payload = {
            "cpu":        psutil.cpu_percent(interval=None),
            "ram":        psutil.virtual_memory().percent,
            "gpu":        gpu_cache or None,
            # mouse_batt is a dict: {pct: float|null, state: str, model: str, vendor: str}
            "mouse_batt": batt_cache,
            "spotify":    media,
            "discord":    disc_state,
            "audio": {
                "spk": audio_cache["spk"],
                "mic": audio_cache["mic"],
                "active_dev": audio_cache["active_dev"],
            },
        }

        if new_payload != last_sys_data_payload:
            last_sys_data_payload = new_payload
            await ws_manager.broadcast({
                "type": "sys_data",
                "data": new_payload,
            })

        burst = (sys.platform.startswith("linux")
                 and media_module.is_mpris_burst_active())
        sleep_time = 0.2 if burst else 1.0
        try:
            await asyncio.wait_for(sys_data_trigger.wait(), timeout=sleep_time)
            sys_data_trigger.clear()
        except asyncio.TimeoutError:
            pass


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
                last_weather_data = {
                    "temp":      round(data["main"]["temp"]),
                    "desc":      data["weather"][0]["description"].title(),
                    "timestamp": int(time.time()),
                }
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


async def pipewire_auto_router():
    """Route Dashboard-Soundboard monitor into microphone capture targets (Linux only)."""
    while True:
        try:
            def _run(cmd):
                return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2)

            def_src = (await asyncio.to_thread(_run, ["pactl", "get-default-source"])).decode().strip()
            pw_out  = (await asyncio.to_thread(_run, ["pw-link", "-o"])).decode()
            monitors = [p.strip() for p in pw_out.splitlines()
                        if "Dashboard-Soundboard" in p and "monitor" in p]
            if not monitors:
                await asyncio.sleep(2)
                continue

            sb_FL = monitors[0]
            sb_FR = monitors[1] if len(monitors) > 1 else sb_FL

            pw_links  = (await asyncio.to_thread(_run, ["pw-link", "-l"])).decode()
            app_ports: list[str] = []
            is_cap    = False
            for line in pw_links.splitlines():
                if not line.startswith((" ", "\t")):
                    is_cap = def_src in line and "capture" in line
                elif is_cap and "|->" in line:
                    port = line.split("|->")[1].strip()
                    if "Dashboard-Soundboard" not in port and "loopback" not in port.lower():
                        app_ports.append(port)

            for i, port in enumerate(app_ports):
                src = sb_FL if i % 2 == 0 else sb_FR
                await asyncio.to_thread(
                    subprocess.run, ["pw-link", src, port],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                )
        except Exception as e:
            logger.debug(f"PipeWire router: {e}")
        await asyncio.sleep(5)


# ─── FastAPI app ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(SOUNDS_DIR, exist_ok=True)

    # Check for updates on Codeberg
    async def check_for_updates():
        global update_available, latest_version, update_apk_url
        try:
            def fetch_releases():
                url = "https://codeberg.org/api/v1/repos/liburnb/Touch-Dashboard/releases"
                req = urllib.request.Request(url, headers={"User-Agent": f"TouchDashboard/{VERSION}"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return _loads(resp.read())
            
            releases = await asyncio.to_thread(fetch_releases)
            if releases and isinstance(releases, list):
                desktop_checked = False
                apk_url_found = False
                
                for release in releases:
                    tag = release.get("tag_name", "")
                    if not tag or tag == "pre-release":
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
                        desktop_checked = True
                        
                    if desktop_checked and apk_url_found:
                        break
        except Exception as e:
            logger.error(f"Update check failed: {e}")

    asyncio.create_task(check_for_updates())

    # Linux: create Dashboard-Soundboard virtual sink
    if get_os_target() == "linux":
        async def _run(cmd, timeout=3):
            return await asyncio.to_thread(subprocess.run, cmd, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, timeout=timeout)
        async def _out(cmd, timeout=3):
            return await asyncio.to_thread(subprocess.check_output, cmd, stderr=subprocess.DEVNULL, timeout=timeout)

        try:
            try:
                real_sink = (await _out(["pactl", "get-default-sink"])).decode().strip()  # type: ignore[union-attr]
                real_src  = (await _out(["pactl", "get-default-source"])).decode().strip()  # type: ignore[union-attr]
            except Exception:
                real_sink = real_src = ""

            sinks_out = (await _out(["pactl", "list", "short", "sinks"])).decode()  # type: ignore[union-attr]
            if "Dashboard-Soundboard" not in sinks_out:
                await _run(["pactl", "load-module", "module-null-sink",
                      "sink_name=Dashboard-Soundboard",
                      'sink_properties=device.description="Dashboard-Soundboard"'])
            await _run(["pactl", "set-sink-volume", "Dashboard-Soundboard", "100%"])
            await _run(["pactl", "set-sink-mute",   "Dashboard-Soundboard", "0"])

            mods_out = (await _out(["pactl", "list", "short", "modules"])).decode()  # type: ignore[union-attr]
            if "source=Dashboard-Soundboard.monitor" not in mods_out:
                await _run(["pactl", "load-module", "module-loopback",
                      "source=Dashboard-Soundboard.monitor"])

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
    global weather_update_event, speedtest_lock, upower_monitor
    weather_update_event = asyncio.Event()
    speedtest_lock       = asyncio.Lock()

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

    restart_discord_ipc()

    tasks = [
        asyncio.create_task(hardware_loop(),  name="hardware_loop"),
        asyncio.create_task(fetch_weather(),  name="fetch_weather"),
    ]
    if get_os_target() == "linux":
        tasks.append(asyncio.create_task(pipewire_auto_router(), name="pw_router"))

    app.state.bg_tasks = tasks
    yield

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if disc_ipc_instance:
        disc_ipc_instance.close()
    if upower_monitor is not None:
        upower_monitor.stop()


_app = FastAPI(lifespan=lifespan)

@_app.get("/api/wakeup")
def wakeup_endpoint():
    # Bring an existing desktop window to front (second-instance signal).
    _desktop_module.wakeup()
    return {"status": "waking up"}

@_app.post("/api/exit")
def exit_endpoint():
    import threading
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"status": "shutting down"}

@_app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"]        = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval' ws: wss:; "
        "img-src 'self' data: https:;"
    )
    return resp


_app.mount("/static", StaticFiles(directory=os.path.join(RESOURCE_DIR, "static")), name="static")

# ─── HTTP routes ───────────────────────────────────────────────────────────────

@_app.get("/api/local_art")
async def serve_local_art():
    path = media_module.get_last_mpris_art_path()
    if path and os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"error": "No art"}, status_code=404)


@_app.get("/")
async def index(request: Request):
    global last_host_url
    last_host_url = request.url.netloc
    html = _load_template()
    # NOTE: Discord initial state is now delivered via WS config_sync,
    # so we no longer mutate the HTML here.
    resp = HTMLResponse(content=html)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


@_app.get("/favicon.ico")
async def _favicon():
    return JSONResponse({})


@_app.get("/manifest.json")
async def _manifest():
    return JSONResponse({
        "name": "Touch Dashboard", "short_name": "Dashboard",
        "start_url": "/?v=2.0", "display": "standalone",
        "orientation": "landscape",
        "background_color": "#090e17", "theme_color": "#0ea5e9",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any maskable"},
        ],
    }, headers={"Cache-Control": "no-cache"})


@_app.get("/spotify_login")
async def spotify_login():
    sp = get_sp_oauth()
    if not sp:
        return JSONResponse({"error": "No Spotify credentials configured"})
    return RedirectResponse(sp.get_authorize_url())


@_app.get("/callback")
async def spotify_callback(code: str | None = None):
    sp = get_sp_oauth()
    if not sp or not code:
        return RedirectResponse("/")
    try:
        await asyncio.to_thread(sp.get_access_token, code)
    except Exception:
        pass
    return RedirectResponse("/")


@_app.get("/disc_callback")
async def discord_callback(code: str | None = None):
    if not code or not disc_ipc_instance:
        return RedirectResponse("/")
    ok = await asyncio.to_thread(disc_ipc_instance.exchange_code, code)
    if ok:
        disc_ipc_instance.needs_reauth = True
    return RedirectResponse("/")

@_app.post("/api/update")
async def api_update():
    """Trigger the auto-update process."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    if get_os_target() == "linux":
        cmd = f"export AUTO_UPDATE=1 BACKGROUND_UPDATE=1 TARGET_DIR='{current_dir}'; bash -c \"$(curl -fsSL https://codeberg.org/liburnb/Touch-Dashboard/raw/branch/main/scripts/setup.sh)\""
    else:
        cmd = f"powershell -ExecutionPolicy Bypass -Command \"$env:AUTO_UPDATE=1; $env:BACKGROUND_UPDATE=1; $env:TARGET_DIR='{current_dir}'; irm https://codeberg.org/liburnb/Touch-Dashboard/raw/branch/main/scripts/install.ps1 | iex\""
    
    # Run in background and stream output to WS
    async def run_update():
        logger.info(f"Triggering background update: {cmd}")
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            
            buf = b""
            progress_pct = 0.0
            
            while True:
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
                        
                    await ws_manager.broadcast({
                        "type": "update_progress",
                        "data": {
                            "text": line,
                            "progress": progress_pct,
                            "error": False
                        }
                    })
                    
            await process.wait()
            if process.returncode != 0:
                await ws_manager.broadcast({
                    "type": "update_progress",
                    "data": {
                        "text": f"\n[!] Update failed with exit code {process.returncode}",
                        "progress": progress_pct,
                        "error": True
                    }
                })
            else:
                await ws_manager.broadcast({
                    "type": "update_progress",
                    "data": {
                        "text": f"\n[*] Update successful! Restarting server...",
                        "progress": 100,
                        "error": False
                    }
                })
                await asyncio.sleep(1) # wait for ws broadcast
                
                # Restart the server
                if get_os_target() == "linux":
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    # On Windows, os.execv is unreliable, better to spawn and exit
                    subprocess.Popen([sys.executable] + sys.argv, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008)
                    os._exit(0)
        except Exception as e:
            logger.error(f"Error reading update stream: {e}")
            await ws_manager.broadcast({
                "type": "update_progress",
                "data": {
                    "text": f"\n[!] Internal error during update: {e}",
                    "progress": 0,
                    "error": True
                }
            })
            
    asyncio.create_task(run_update())
    return {"status": "updating"}

# ─── WebSocket ─────────────────────────────────────────────────────────────────

@_app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global config, force_media_update, current_media_source, global_sp_oauth

    await ws_manager.connect(ws)
    try:
        audio_names = config.get("audio_names", {})
        hw = await asyncio.to_thread(AudioSystem.get_hardware_sinks, audio_names)

        # Initial state push — Discord state included here (replaces DOM surgery)
        disc_has_token = bool(config.get("disc_token", ""))
        initial_disc = {
            "connected": bool(disc_ipc_instance and disc_ipc_instance.connected),
            "authorized": disc_has_token,
            "mute": (disc_ipc_instance.voice_state.get("mute", False)
                     if disc_ipc_instance else False),
            "deaf": (disc_ipc_instance.voice_state.get("deaf", False)
                     if disc_ipc_instance else False),
        }
        await ws.send_text(_dumps({
            "type": "config_sync",
            "data": {
                "cfg":       config,
                "hw":        hw,
                "os_target": get_os_target(),
                "host_ip":   get_lan_ip(),
                "disc_init": initial_disc,
                "update_available": update_available,
                "latest_version": latest_version,
                "update_apk_url": update_apk_url,
                "server_version": VERSION,
                "req_apk_version": REQUIRED_APK_VERSION,
            },
        }))

        if last_weather_data.get("temp") != "--":
            await ws.send_text(_dumps({"type": "weather_data", "data": last_weather_data}))

        # ── message loop ───────────────────────────────────────────────────────
        while True:
            raw = await ws.receive_text()
            msg  = _loads(raw)
            mtype = msg.get("type")
            data  = msg.get("data")

            # ── queries ────────────────────────────────────────────────────────
            if mtype == "req_local_sounds":
                sounds = await asyncio.to_thread(get_local_sounds)
                await ws.send_text(_dumps({"type": "local_sounds_list", "data": sounds}))

            elif mtype == "req_apps_list":
                apps = await asyncio.to_thread(AppEnumerator.get_apps)
                await ws.send_text(_dumps({"type": "apps_list", "data": apps}))

            # ── save macros ────────────────────────────────────────────────────
            elif mtype == "save_macros":
                with CONFIG_LOCK:
                    config["macros"] = data
                await asyncio.to_thread(save_config)
                audio_data = await asyncio.to_thread(AudioSystem.poll_all,
                                                     config.get("audio_names", {}))
                await ws_manager.broadcast(_make_config_sync(audio_data))

            # ── macro exec ─────────────────────────────────────────────────────
            elif mtype == "macro_exec":
                m_type      = data.get("type")
                action_data = data.get("action_data")
                if m_type == "app":
                    await asyncio.to_thread(AppEnumerator.launch_app, action_data)
                elif m_type == "macro":
                    keys = action_data if isinstance(action_data, list) else [action_data]
                    await asyncio.to_thread(MacroSystem.send_keys, *keys)
                elif m_type in ("premade", "plugin"):
                    mtype = "action"
                    data  = action_data

            # ── save config ────────────────────────────────────────────────────
            if mtype == "save_config":
                old_disc  = config.get("disc_enabled", True)
                old_disc_id, old_disc_sec = config.get("disc_id"), config.get("disc_secret")
                old_wx_api, old_wx_city   = config.get("weather_api"), config.get("weather_city")
                old_spot_id               = config.get("spot_id")

                with CONFIG_LOCK:
                    config.update(data)
                await asyncio.to_thread(save_config)

                if old_spot_id != config.get("spot_id"):
                    global_sp_oauth = None

                audio_data = await asyncio.to_thread(AudioSystem.poll_all,
                                                     config.get("audio_names", {}))
                await ws_manager.broadcast(_make_config_sync(audio_data))

                if (old_disc != config.get("disc_enabled", True)
                        or old_disc_id  != config.get("disc_id")
                        or old_disc_sec != config.get("disc_secret")):
                    restart_discord_ipc()

                if (old_wx_api  != config.get("weather_api")
                        or old_wx_city != config.get("weather_city")):
                    if weather_update_event is not None:
                        weather_update_event.set()

            # ── actions ────────────────────────────────────────────────────────
            elif mtype == "action":
                await _handle_action(ws, data)

            # ── volume ─────────────────────────────────────────────────────────
            elif mtype == "set_volume":
                target = ("@DEFAULT_AUDIO_SINK@" if data["type"] == "speaker"
                          else "@DEFAULT_AUDIO_SOURCE@")
                await asyncio.to_thread(AudioSystem.set_vol, target, data["val"])

            # ── speedtest ──────────────────────────────────────────────────────
            elif mtype == "run_speedtest":
                asyncio.create_task(_run_speedtest())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        ws_manager.disconnect(ws)


def _make_config_sync(audio_data: dict) -> dict:
    return {
        "type": "config_sync",
        "data": {
            "cfg":       config,
            "hw":        audio_data.get("sinks", []),
            "os_target": get_os_target(),
            "host_ip":   get_lan_ip(),
            "server_version": VERSION,
            "update_available": update_available,
            "latest_version": latest_version,
            "update_apk_url": update_apk_url,
        },
    }


async def _run_speedtest():
    if speedtest_lock is None:
        return
    if speedtest_lock.locked():
        await ws_manager.broadcast({"type": "speedtest_result",
                                     "data": {"down": "BUSY", "up": "BUSY"}})
        return
    async with speedtest_lock:
        try:
            st   = await asyncio.to_thread(speedtest_lib.Speedtest)
            await asyncio.to_thread(st.get_best_server)
            down = await asyncio.to_thread(st.download)
            up   = await asyncio.to_thread(st.upload)
            await ws_manager.broadcast({"type": "speedtest_result",
                                         "data": {"down": round(down / 1e6, 1),
                                                  "up":   round(up   / 1e6, 1)}})
        except Exception:
            await ws_manager.broadcast({"type": "speedtest_result",
                                         "data": {"down": "ERR", "up": "ERR"}})


async def _handle_action(ws: WebSocket, action: str):
    """Dispatch an 'action' message from the WebSocket client."""
    global force_media_update, current_media_source

    # ── local soundboard ──────────────────────────────────────────────────────
    if action.startswith("local_play_"):
        filename = action.removeprefix("local_play_")
        filepath = resolve_sound_path(filename)
        if filepath:
            await asyncio.to_thread(AudioSystem.play_local_sound, filepath)
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
                save_config()
            def _rm_spot_cache():
                if os.path.exists(SPOTIFY_CACHE_FILE):
                    try: os.remove(SPOTIFY_CACHE_FILE)
                    except Exception: pass
            await asyncio.to_thread(_rm_spot_cache)
            return

        sp_oauth = get_sp_oauth()
        routed   = False
        if current_media_source == "spotify" and sp_oauth:
            try:
                token = await asyncio.to_thread(sp_oauth.get_cached_token)
                if token:
                    sp = spotipy.Spotify(auth=token["access_token"], requests_timeout=3)
                    if action == "spot_play":
                        c = await asyncio.to_thread(sp.current_playback)
                        if c and c.get("is_playing"):
                            await asyncio.to_thread(sp.pause_playback)
                        else:
                            await asyncio.to_thread(sp.start_playback)
                    elif action == "spot_next":
                        await asyncio.to_thread(sp.next_track)
                    elif action == "spot_prev":
                        await asyncio.to_thread(sp.previous_track)
                    routed = True
                    force_media_update = True
            except Exception:
                pass

        if not routed:
            if sys.platform.startswith("linux") and shutil.which("playerctl"):
                cmd_map = {
                    "spot_play": ["playerctl", "play-pause"],
                    "spot_next": ["playerctl", "next"],
                    "spot_prev": ["playerctl", "previous"],
                }
                if action in cmd_map:
                    await asyncio.to_thread(
                        subprocess.run, cmd_map[action],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
            else:
                # Windows: use media keys
                key_map = {
                    "spot_play": "KEY_PLAYPAUSE",
                    "spot_next": "KEY_NEXTSONG",
                    "spot_prev": "KEY_PREVIOUSSONG",
                }
                if action in key_map:
                    await asyncio.to_thread(MacroSystem.send_keys, key_map[action])
            force_media_update = True
        return

    # ── Soundpad (Windows) ────────────────────────────────────────────────────
    if action.startswith("sp_play_"):
        sp_id = action.split("sp_play_")[1]
        if sys.platform.startswith("win"):
            for sp_path in (
                r"C:\Program Files\Soundpad\Soundpad.exe",
                r"C:\Program Files (x86)\Steam\steamapps\common\Soundpad\Soundpad.exe",
            ):
                if os.path.exists(sp_path):
                    await asyncio.to_thread(
                        subprocess.run, [sp_path, "-rc", f"DoPlaySound({sp_id})"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    break
        return

    # ── Discord ────────────────────────────────────────────────────────────────
    if action == "disc_clear_auth":
        with CONFIG_LOCK:
            config["disc_token"] = ""
            save_config()
        restart_discord_ipc()
        return

    if action == "disc_auth":
        auth_url = disc_ipc_instance.get_auth_url() if disc_ipc_instance else ""
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
            current_mute = getattr(disc_ipc_instance, 'voice_state', {}).get('mute', False)
            disc_ipc_instance.send(1, {"cmd": "SET_VOICE_SETTINGS", "args": {"mute": not current_mute}, "nonce": "MUTE"})
        return

    if action == "disc_deaf":
        if disc_ipc_instance and disc_ipc_instance.connected:
            current_deaf = getattr(disc_ipc_instance, 'voice_state', {}).get('deaf', False)
            disc_ipc_instance.send(1, {"cmd": "SET_VOICE_SETTINGS", "args": {"deaf": not current_deaf}, "nonce": "DEAF"})
        return

    if action == "disc_disconnect":
        if disc_ipc_instance and disc_ipc_instance.connected:
            disc_ipc_instance.send(1, {"cmd": "SELECT_VOICE_CHANNEL",
                                        "args": {"channel_id": None},
                                        "nonce": str(uuid.uuid4())})
        return

    # disc_cam / disc_screen — placeholders
    if action in ("disc_cam", "disc_screen"):
        return

    # ── system apps ───────────────────────────────────────────────────────────
    if action == "app_term":
        await asyncio.to_thread(_launch_terminal)
        return

    if action == "app_web":
        await asyncio.to_thread(_launch_browser)
        return

    if action == "app_task":
        await asyncio.to_thread(_launch_task_manager)
        return

    if action == "app_clip":
        await asyncio.to_thread(MacroSystem.send_keys, 119)  # KEY_SYSRQ / Print Screen
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
        await asyncio.to_thread(AudioSystem.cycle_device, config.get("audio_names", {}))
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

def _popen(cmd, **kwargs):
    defaults = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform != "win32":
        defaults["start_new_session"] = True
    subprocess.Popen(cmd, **{**defaults, **kwargs})  # type: ignore[call-overload]


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
    if reload:
        uvicorn.run("server:_app", host=host, port=port, reload=True,
                    reload_dirs=[BASE_DIR, os.path.join(RESOURCE_DIR, "templates")])
    else:
        cfg = uvicorn.Config(_app, host=host, port=port, log_level="warning")
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
    )


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
