"""
server.py — Touch Dashboard backend.

Architecture:
  FastAPI + uvicorn handle HTTP and WebSocket transport.
  pywebview (Linux/Windows) or server-only mode provides the desktop window.
  pystray provides the system-tray icon on all platforms.

Platform logic lives in the companion modules:
  audio_system.py  — cross-platform audio
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

# ─── ChromiumFlags (before any Qt/CE import) ──────────────────────────────────
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    (
        "--disable-site-isolation-trials "
        "--disable-features=RendererCodeIntegrity "
        "--js-flags=--max-old-space-size=128 "
        "--disable-gpu-memory-buffer-video-frames "
        "--disable-reading-from-canvas "
        "--disable-dev-shm-usage "
        "--disable-logging"
    ),
)

# Prevent Windows WebView2 from suspending/crashing when hidden in the tray
os.environ.setdefault(
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "--disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding"
)

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
    ("PyQt6",     "PyQt6"),
    ("PIL",       "Pillow"),
    ("pynvml",    "pynvml"),
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


def _format_dep_msg(pkgs, lead):
    return lead + "\n\nMissing dependencies:\n\n" + "\n".join(f"  - {p}" for p in pkgs)


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


def _ask_popup(title, msg) -> bool | None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
        ans = messagebox.askyesno(title, msg, parent=r); r.destroy()
        return ans
    except Exception:
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes
            res = ctypes.windll.user32.MessageBoxW(None, msg, title, 0x24)
            return res == 6
        except Exception:
            pass
    if sys.platform.startswith("linux") and not _is_linux_cli():
        for tool in (["zenity", "--question", "--title", title, "--text", msg],
                     ["kdialog", "--title", title, "--yesno", msg]):
            if shutil.which(tool[0]):
                try:
                    r = subprocess.run(tool, check=False)
                    return r.returncode == 0
                except Exception:
                    pass
    return None


def ensure_runtime_dependencies():
    missing = [pkg for mod, pkg in RUNTIME_DEPENDENCIES
               if importlib.util.find_spec(mod) is None]
    if not missing:
        return

    if getattr(sys, "frozen", False):
        _show_popup("Touch Dashboard", _format_dep_msg(
            missing, "Packaged build is missing dependencies. Please reinstall."))
        raise SystemExit(f"Missing deps: {missing}")

    if os.environ.get("TOUCH_DASHBOARD_SKIP_AUTO_INSTALL") == "1":
        _show_popup("Touch Dashboard", _format_dep_msg(
            missing, "Auto-install disabled. Install deps manually."))
        raise SystemExit(f"Missing deps: {missing}")

    msg = _format_dep_msg(
        missing, "Touch Dashboard needs to install missing Python dependencies."
    ) + "\n\nInstall them now?"
    print(msg, flush=True)

    approved = _ask_popup("Touch Dashboard Dependencies", msg)
    if approved is None:
        try:
            approved = input("Install? [y/N]: ").strip().lower() in ("y", "yes")
        except EOFError:
            approved = False
    if not approved:
        raise SystemExit("Dependency install declined.")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    cmd = [sys.executable, "-m", "pip", "install"]
    if not in_venv:
        cmd.append("--user")
        try:
            import sysconfig
            stdlib = sysconfig.get_path("stdlib", sysconfig.get_default_scheme())
            if stdlib and os.path.exists(os.path.join(stdlib, "EXTERNALLY-MANAGED")):
                cmd.append("--break-system-packages")
        except Exception:
            pass
    cmd.extend(missing)
    if subprocess.run(cmd).returncode != 0:
        raise RuntimeError("Failed to install deps. Run `pip install -r requirements.txt` manually.")
    try: site.main()
    except Exception: pass
    importlib.invalidate_caches()


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

try:
    from PIL import Image, ImageDraw  # type: ignore[import]
    from PyQt6.QtCore import QObject, pyqtSlot as Slot, QUrl, Qt, QTimer, QThread, QFile, QIODevice, QTextStream, pyqtSignal
    from PyQt6.QtGui import QIcon, QAction
    from PyQt6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon, QMenu
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineScript
    from PyQt6.QtWebChannel import QWebChannel
except ImportError:
    Image = ImageDraw = QApplication = pyqtSignal = QObject = None

if QObject is not None:
    class SingleInstanceSignals(QObject):
        wakeup = pyqtSignal()
    si_signals = SingleInstanceSignals()
else:
    si_signals = None

try:
    import pynvml  # type: ignore[import]
except ImportError:
    pynvml = None

# ─── Local modules ─────────────────────────────────────────────────────────────

from audio_system import AudioSystem
from discord_ipc import DiscordIPC
from macro_system import MacroSystem, AppEnumerator
import media as media_module

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
uvicorn_server = None

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

        disc_ipc_instance.on_state_change = _on_disc_change
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
    batt_cache = "--"
    last_sys_data_payload = {}

    def _read_batt() -> str:
        # Mouse battery — reads a file written by an external script.
        # Path is /tmp/g502_battery.txt on Linux; on Windows looks in %TEMP%.
        candidates = [
            "/tmp/g502_battery.txt",
            os.path.join(os.environ.get("TEMP", ""), "g502_battery.txt"),
        ]
        for p in candidates:
            try:
                with open(p) as f:
                    return f.read().strip()
            except Exception:
                pass
        return "--"

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
            batt_cache      = await asyncio.to_thread(_read_batt)
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
                        pynvml.nvmlDeviceGetUtilizationRates, nvml_handle
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
            "is_vesktop": False,
        }
        if disc_has_creds and not disc_has_token:
            scopes = "rpc rpc.voice.read rpc.voice.write rpc.guilds.read"
            redir  = urllib.parse.quote("http://127.0.0.1:8888/disc_callback")
            disc_state["auth_url"] = (
                f"https://discord.com/api/oauth2/authorize"
                f"?client_id={config['disc_id']}&redirect_uri={redir}"
                f"&response_type=code&scope={scopes}"
            )
        if disc_ipc_instance:
            disc_state.update({
                "connected":       disc_ipc_instance.connected,
                "voice_supported": disc_ipc_instance.voice_supported,
                "auth_pending":    disc_ipc_instance.auth_pending,
                "is_vesktop":      disc_ipc_instance.is_vesktop,
            })
            # Vesktop (arRPC) is always "authorized" — no OAuth token needed.
            if disc_ipc_instance.is_vesktop and disc_ipc_instance.connected:
                disc_state["authorized"] = True
                disc_state["auth_required"] = False
                disc_state["auth_url"] = ""
            if disc_ipc_instance.connected:
                disc_state["mute"] = disc_ipc_instance.voice_state.get("mute", False)
                disc_state["deaf"] = disc_ipc_instance.voice_state.get("deaf", False)
                disc_state["voice_channel"] = disc_ipc_instance.voice_channel
            if not disc_has_token and disc_ipc_instance.auth_pending:
                disc_state["auth_url"] = disc_ipc_instance.get_auth_url()

        new_payload = {
            "cpu":        psutil.cpu_percent(interval=None),
            "ram":        psutil.virtual_memory().percent,
            "gpu":        gpu_cache or None,
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
            last_weather_data["timestamp"] = time.time()
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
                    "timestamp": time.time(),
                }
                with open(WEATHER_CACHE_FILE, "w") as f:
                    json.dump(last_weather_data, f)
                await ws_manager.broadcast({"type": "weather_data", "data": last_weather_data})
        except Exception as e:
            logger.debug(f"Weather fetch: {e}")
        last_weather_data["timestamp"] = time.time()

    if os.path.exists(WEATHER_CACHE_FILE):
        try:
            with open(WEATHER_CACHE_FILE) as f:
                last_weather_data = json.load(f)
        except Exception:
            pass

    if time.time() - last_weather_data.get("timestamp", 0) > 1800:
        await _do_fetch()

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

    # Linux: create Dashboard-Soundboard virtual sink
    if get_os_target() == "linux":
        def _run(cmd, timeout=3):
            return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=timeout)
        def _out(cmd, timeout=3):
            return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout)

        try:
            try:
                real_sink = _out(["pactl", "get-default-sink"]).decode().strip()
                real_src  = _out(["pactl", "get-default-source"]).decode().strip()
            except Exception:
                real_sink = real_src = ""

            sinks_out = _out(["pactl", "list", "short", "sinks"]).decode()
            if "Dashboard-Soundboard" not in sinks_out:
                _run(["pactl", "load-module", "module-null-sink",
                      "sink_name=Dashboard-Soundboard",
                      'sink_properties=device.description="Dashboard-Soundboard"'])
            _run(["pactl", "set-sink-volume", "Dashboard-Soundboard", "100%"])
            _run(["pactl", "set-sink-mute",   "Dashboard-Soundboard", "0"])

            mods_out = _out(["pactl", "list", "short", "modules"]).decode()
            if "source=Dashboard-Soundboard.monitor" not in mods_out:
                _run(["pactl", "load-module", "module-loopback",
                      "source=Dashboard-Soundboard.monitor"])

            if real_sink and "Dashboard" not in real_sink:
                _run(["pactl", "set-default-sink",   real_sink])
            if real_src  and "Dashboard" not in real_src:
                _run(["pactl", "set-default-source", real_src])
        except Exception as e:
            logger.error(f"PipeWire setup: {e}")

    # Initialise asyncio primitives here — the event loop is guaranteed to
    # exist at this point. Creating them at module scope binds them to a
    # different (or non-existent) loop and causes:
    #   RuntimeError: Task got Future <Event> attached to a different loop
    global weather_update_event, speedtest_lock
    weather_update_event = asyncio.Event()
    speedtest_lock       = asyncio.Lock()

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


_app = FastAPI(lifespan=lifespan)

@_app.get("/api/wakeup")
def wakeup_endpoint():
    if si_signals is not None:
        si_signals.wakeup.emit()
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
async def spotify_callback(code: str = None):
    sp = get_sp_oauth()
    if not sp or not code:
        return RedirectResponse("/")
    try:
        await asyncio.to_thread(sp.get_access_token, code)
    except Exception:
        pass
    return RedirectResponse("/")


@_app.get("/disc_callback")
async def discord_callback(code: str = None):
    if not code or not disc_ipc_instance:
        return RedirectResponse("/")
    ok = await asyncio.to_thread(disc_ipc_instance.exchange_code, code)
    if ok:
        disc_ipc_instance.needs_reauth = True
    return RedirectResponse("/")


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
            if os.path.exists(SPOTIFY_CACHE_FILE):
                try: os.remove(SPOTIFY_CACHE_FILE)
                except Exception: pass
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
            if disc_ipc_instance.is_vesktop:
                # arRPC (Vesktop) does not implement the IPC AUTHORIZE command.
                # Always use the web OAuth flow via the system browser.
                if auth_url:
                    webbrowser.open(auth_url)
            else:
                # Standard Discord: trigger native in-app consent popup via IPC.
                disc_ipc_instance.send(1, {
                    "cmd": "AUTHORIZE",
                    "args": {"client_id": disc_ipc_instance.client_id,
                              "scopes": ["rpc", "rpc.guilds.read"]},
                    "nonce": str(uuid.uuid4()),
                })
        elif auth_url:
            # Not yet connected — open the auth URL directly so the user can
            # grant permission; the server-side callback will handle the code.
            webbrowser.open(auth_url)
        return

    if action == "disc_mute":
        if disc_ipc_instance and disc_ipc_instance.connected and disc_ipc_instance.voice_supported:
            is_deaf = disc_ipc_instance.voice_state.get("deaf", False)
            is_mute = disc_ipc_instance.voice_state.get("mute", False)
            if is_deaf:
                disc_ipc_instance.set_voice(deaf=False, mute=True)
                disc_ipc_instance.pre_deafen_mute = True
            else:
                disc_ipc_instance.set_voice(mute=not is_mute)
                disc_ipc_instance.pre_deafen_mute = not is_mute
        else:
            await asyncio.to_thread(MacroSystem.send_keys, "KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_M")
        return

    if action == "disc_deaf":
        if disc_ipc_instance and disc_ipc_instance.connected and disc_ipc_instance.voice_supported:
            is_deaf = disc_ipc_instance.voice_state.get("deaf", False)
            is_mute = disc_ipc_instance.voice_state.get("mute", False)
            if not is_deaf:
                disc_ipc_instance.pre_deafen_mute = is_mute
                disc_ipc_instance.set_voice(deaf=True, mute=True)
            else:
                restore = getattr(disc_ipc_instance, "pre_deafen_mute", False)
                disc_ipc_instance.set_voice(deaf=False, mute=restore)
        else:
            await asyncio.to_thread(MacroSystem.send_keys, "KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_D")
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
    subprocess.Popen(cmd, **{**defaults, **kwargs})


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


# ─── Windows desktop helpers ───────────────────────────────────────────────────

_WINDOW_ICON_HANDLES: list = []


def configure_windows_app_identity():
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_USER_MODEL_ID)
    except Exception as e:
        logger.debug(f"AppUserModelID: {e}")


def _int_handle(v):
    if v is None: return None
    for attr in ("ToInt64", "ToInt32"):
        if hasattr(v, attr): v = getattr(v, attr)()
    if hasattr(v, "value"): v = v.value
    try: return int(v) or None
    except Exception: return None


def set_windows_window_icon(window, title="Touch Dashboard"):
    if not sys.platform.startswith("win") or not os.path.exists(APP_ICON_PATH):
        return
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32   = ctypes.windll.user32
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG, ICON_SMALL2 = 0, 1, 2

        user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                       ctypes.c_int, ctypes.c_int, wt.UINT]
        user32.LoadImageW.restype  = wt.HANDLE

        large = _int_handle(user32.LoadImageW(None, APP_ICON_PATH, IMAGE_ICON,
                                               user32.GetSystemMetrics(11),
                                               user32.GetSystemMetrics(12), LR_LOADFROMFILE))
        small = _int_handle(user32.LoadImageW(None, APP_ICON_PATH, IMAGE_ICON,
                                               user32.GetSystemMetrics(49),
                                               user32.GetSystemMetrics(50), LR_LOADFROMFILE))
        if not large and not small:
            return

        hwnd = _get_window_hwnd(window, title)
        if not hwnd:
            return

        user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.SendMessageW.restype  = wt.LPARAM
        if large: user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,    large)
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL,  small)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL2, small)

        _WINDOW_ICON_HANDLES.extend(h for h in (large, small) if h)
    except Exception as e:
        logger.debug(f"Window icon: {e}")


def _get_window_hwnd(window, title: str) -> int | None:
    # Try pywebview attributes first
    for obj in (window, getattr(window, "native", None), getattr(window, "gui", None)):
        if obj is None:
            continue
        h = _int_handle(obj)
        if h: return h
        for attr in ("hwnd", "handle", "Handle"):
            h = _int_handle(getattr(obj, attr, None))
            if h: return h

    # Fallback: enumerate process windows
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        pid    = os.getpid()
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def _proc(hwnd, _):
            p = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value != pid:
                return True
            if title:
                n   = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value != title:
                    return True
            h = _int_handle(hwnd)
            if h: found.append(h)
            return True

        user32.EnumWindows(_proc, 0)
        return found[0] if found else None
    except Exception:
        return None


# ─── Tray API (exposed to pywebview JS context) ────────────────────────────────

class _Api(QObject):
    def __init__(self, app_window):
        super().__init__()
        self._app = app_window

    @Slot()
    def minimize(self):
        self._app.showMinimized()

    @Slot()
    def maximize(self):
        if self._app.isFullScreen():
            self._app.showNormal()
        else:
            self._app.showFullScreen()

    @Slot()
    def close(self):
        if self._app.minimize_to_tray:
            self._app.hide()
            self._app.window_visible = False
            self._app.toggle_action.setText("Show Dashboard")
        else:
            self._app.quit_app()

    @Slot(bool)
    def set_minimize_to_tray(self, val: bool):
        self._app.minimize_to_tray = val

    @Slot(result=str)
    def get_local_ip(self):
        return self._app.local_ip

    @Slot(str)
    def open_url(self, url: str):
        """Open a URL in the system's default external browser."""
        try:
            webbrowser.open(url)
        except Exception as e:
            logger.warning(f"open_url failed for '{url}': {e}")

    @Slot()
    def start_drag(self):
        if hasattr(self._app.windowHandle(), "startSystemMove"):
            self._app.windowHandle().startSystemMove()


class DesktopTrayApp(QMainWindow):
    def __init__(self, port: int = 8888):
        super().__init__()
        self.port            = port
        self.local_ip        = get_lan_ip()
        self.window_title    = f"Touch Dashboard — {self.local_ip}"
        self.window_visible  = True
        self.shutting_down   = False
        self.minimize_to_tray = True
        
        self.setWindowTitle(self.window_title)
        self.resize(1280, 800)
        
        # Frameless and translucent
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        self.view = QWebEngineView(self)
        self.view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        self.setCentralWidget(self.view)
        
        # WebChannel setup
        self.channel = QWebChannel()
        self.api = _Api(self)
        self.channel.registerObject("api", self.api)
        self.view.page().setWebChannel(self.channel)
        
        # Load URL
        self.view.setUrl(QUrl(f"http://127.0.0.1:{self.port}"))
        
        self.setWindowIcon(self._make_icon())
        self._setup_tray()
        set_windows_window_icon(self, self.window_title)
        
    def _make_icon(self):
        if os.path.exists(APP_ICON_PATH):
            return QIcon(APP_ICON_PATH)
        return QIcon()

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self._make_icon())
        self.tray.setToolTip("Touch Dashboard")
        
        self.menu = QMenu()
        
        self.toggle_action = QAction("Hide Dashboard", self)
        self.toggle_action.triggered.connect(self.toggle_window)
        self.menu.addAction(self.toggle_action)
        
        ip_action = QAction(f"IP: {self.local_ip}", self)
        ip_action.setEnabled(False)
        self.menu.addAction(ip_action)
        
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_app)
        self.menu.addAction(quit_action)
        
        self.tray.setContextMenu(self.menu)
        self.tray.show()

    def toggle_window(self):
        if self.window_visible:
            self.hide()
            self.window_visible = False
            self.toggle_action.setText("Show Dashboard")
        else:
            self.showNormal()
            self.activateWindow()
            self.window_visible = True
            self.toggle_action.setText("Hide Dashboard")

    def closeEvent(self, event):
        if self.shutting_down:
            event.accept()
        elif self.minimize_to_tray:
            self.hide()
            self.window_visible = False
            self.toggle_action.setText("Show Dashboard")
            event.ignore()
        else:
            self.quit_app()
            event.accept()

    def quit_app(self):
        self.shutting_down = True
        stop_fastapi_server()
        QApplication.quit()


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


_lock_file = None

def launch_desktop(host="0.0.0.0", port=8888):
    global _win_mutex
    import urllib.request, time

    if sys.platform.startswith("win"):
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        ERROR_ACCESS_DENIED = 5
        mutex_name = f"Global\\TouchDashboard_Mutex_{port}"
        
        try:
            kernel32 = ctypes.windll.kernel32
            _win_mutex = kernel32.CreateMutexW(None, False, mutex_name)
            last_error = kernel32.GetLastError()
            
            if last_error in (ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED) or not _win_mutex:
                # Mutex exists -> Another instance is running!
                for _ in range(10):
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/wakeup", timeout=0.5)
                        break
                    except Exception:
                        time.sleep(0.5)
                os._exit(0)
        except Exception as e:
            # Completely swallow any bizarre OS errors to prevent alarming the user
            logger.warning(f"Mutex creation failed: {e}. Assuming duplicate instance and exiting.")
            os._exit(0)

    if QApplication is None:
        logger.warning("PyQt6 missing — server-only mode.")
        run_fastapi_server(host=host, port=port)
        return

    configure_windows_app_identity()
    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)

    srv_thread = threading.Thread(
        target=run_fastapi_server,
        kwargs={"host": host, "port": port},
        daemon=True, name="fastapi",
    )
    srv_thread.start()
    # Poll instead of sleeping blindly — ready when the port accepts connections.
    if not _wait_for_server(port):
        logger.error(f"FastAPI server did not start on port {port}; Qt window may show a blank page.")

    desktop = DesktopTrayApp(port=port)

    if si_signals is not None:
        def _handle_wakeup():
            desktop.showNormal()
            desktop.activateWindow()
            desktop.window_visible = True
            desktop.toggle_action.setText("Hide Dashboard")
        si_signals.wakeup.connect(_handle_wakeup)

    desktop.show()
    
    sys.exit(qt_app.exec())


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
