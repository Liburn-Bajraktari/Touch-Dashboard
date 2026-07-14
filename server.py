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

# PyInstaller windowless mode sets sys.stdout and sys.stderr to None.
# Some third-party libraries like speedtest-cli expect them to have a 'fileno' attribute.
# We patch them to os.devnull to prevent fatal crashes on startup.
if sys.stdout is None: sys.stdout = open(os.devnull, 'w')
if sys.stderr is None: sys.stderr = open(os.devnull, 'w')
if sys.stdin is None:  sys.stdin = open(os.devnull, 'r')
import asyncio
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
    ("webview",   "pywebview"),
    ("pystray",   "pystray"),
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
    import webview  # type: ignore[import]
except ImportError:
    webview = None

try:
    import pystray  # type: ignore[import]
    from PIL import Image, ImageDraw  # type: ignore[import]
except ImportError:
    pystray = Image = ImageDraw = None

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

logging.basicConfig(
    filename=os.path.join(DATA_DIR, "server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

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
weather_update_event = asyncio.Event()
last_host_url        = "127.0.0.1:5000"
speedtest_lock       = asyncio.Lock()

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
            redirect_uri="http://127.0.0.1:5000/callback",
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
        }
        if disc_has_creds and not disc_has_token:
            scopes = "rpc rpc.voice.read rpc.voice.write rpc.guilds.read"
            redir  = urllib.parse.quote("http://127.0.0.1:5000/disc_callback")
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
            })
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
        if disc_ipc_instance and disc_ipc_instance.connected:
            disc_ipc_instance.send(1, {
                "cmd": "AUTHORIZE",
                "args": {"client_id": disc_ipc_instance.client_id,
                          "scopes": ["rpc", "rpc.voice.read", "rpc.voice.write", "rpc.guilds.read"]},
                "nonce": str(uuid.uuid4()),
            })
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

class _Api:
    def __init__(self, app):
        self._app = app
    def minimize(self):
        if self._app.window: self._app.window.minimize()
    def maximize(self):
        if self._app.window: self._app.window.toggle_fullscreen()
    def close(self):
        if self._app.window:
            if self._app.minimize_to_tray: self._app.hide_window()
            else: self._app.quit()
    def set_minimize_to_tray(self, val): self._app.minimize_to_tray = bool(val)
    def get_local_ip(self): return self._app.local_ip


class DesktopTrayApp:
    def __init__(self, port: int = 5000):
        self.port            = port
        self.local_ip        = get_lan_ip()
        self.window_title    = f"Touch Dashboard — {self.local_ip}"
        self.window          = None
        self.icon            = None
        self.window_visible  = False
        self.shutting_down   = False
        self.tray_available  = False
        self.minimize_to_tray = True
        self._lock           = threading.RLock()

    def _make_icon(self):
        if os.path.exists(APP_ICON_PATH):
            try:
                img = Image.open(APP_ICON_PATH).convert("RGBA")
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
                img.thumbnail((64, 64), resample)
                canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                canvas.alpha_composite(img, ((64 - img.width) // 2, (64 - img.height) // 2))
                return canvas
            except Exception:
                pass
        # Fallback: draw a simple geometric icon
        img  = Image.new("RGBA", (64, 64), (9, 14, 23, 255))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((10, 10, 54, 54), radius=12, fill=(14, 165, 233, 255))
        draw.rounded_rectangle((18, 18, 46, 46), radius=7,  fill=(15, 23, 42, 255))
        draw.rectangle((24, 24, 40, 30), fill=(248, 250, 252, 255))
        draw.rectangle((24, 34, 40, 40), fill=(248, 250, 252, 255))
        return img

    def _toggle_label(self, _item):
        return "Hide Dashboard" if self.window_visible else "Show Dashboard"

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem(self._toggle_label, self.toggle_window, default=True),
            pystray.MenuItem(f"IP: {self.local_ip}", lambda *_: None, enabled=False),
            pystray.MenuItem("Quit", self.quit),
        )

    def start_tray(self):
        self.icon = pystray.Icon("Touch Dashboard", self._make_icon(),
                                  "Touch Dashboard", self._build_menu())
        try:
            self.icon.run_detached()
            self.tray_available = True
        except Exception as e:
            self.tray_available = False
            logger.error(f"Tray start failed: {e}")

    def attach_window(self, window):
        self.window = window
        try:
            self.window.events.closing += self._on_close
        except Exception:
            pass

    def on_webview_ready(self):
        set_windows_window_icon(self.window, self.window_title)
        self.window_visible = True

    def _on_close(self):
        if self.shutting_down:
            return True
        if self.minimize_to_tray:
            self.hide_window()
            return False
        return True

    def toggle_window(self, *_):
        with self._lock:
            if self.window_visible: self.hide_window()
            else: self.show_window()
            if self.icon: self.icon.update_menu()

    def show_window(self):
        if not self.window: return
        try: self.window.show(); self.window.restore()
        except Exception:
            try: self.window.show()
            except Exception as e: logger.error(f"show_window: {e}"); return
        self.window_visible = True

    def hide_window(self):
        if not self.window: return
        try: self.window.hide(); self.window_visible = False
        except Exception as e: logger.error(f"hide_window: {e}")

    def quit(self, *_):
        with self._lock:
            self.shutting_down = True
            stop_fastapi_server()
            if self.icon:
                try: self.icon.stop()
                except Exception: pass
            if self.window:
                try: self.window.destroy()
                except Exception: pass


# ─── Server lifecycle ──────────────────────────────────────────────────────────

def stop_fastapi_server():
    if uvicorn_server: uvicorn_server.should_exit = True


def run_fastapi_server(host="0.0.0.0", port=5000, reload=False):
    global uvicorn_server
    if reload:
        uvicorn.run("server:_app", host=host, port=port, reload=True,
                    reload_dirs=[BASE_DIR, os.path.join(RESOURCE_DIR, "templates")])
    else:
        cfg = uvicorn.Config(_app, host=host, port=port, log_level="warning")
        uvicorn_server = uvicorn.Server(cfg)
        uvicorn_server.run()


def launch_desktop(host="0.0.0.0", port=5000):
    if webview is None or pystray is None or Image is None:
        logger.warning("pywebview/pystray/Pillow missing — server-only mode.")
        run_fastapi_server(host=host, port=port)
        return

    configure_windows_app_identity()

    srv_thread = threading.Thread(
        target=run_fastapi_server,
        kwargs={"host": host, "port": port},
        daemon=True, name="fastapi",
    )
    srv_thread.start()
    time.sleep(1.0)   # Give the server a moment to bind

    desktop = DesktopTrayApp(port=port)
    api     = _Api(desktop)

    win_kwargs = {
        "title":       desktop.window_title,
        "url":         f"http://127.0.0.1:{port}",
        "frameless":   True,
        "width":       1280,
        "height":      800,
        "hidden":      False,
        "easy_drag":   False,
        "transparent": True,
        "js_api":      api,
    }
    try:
        win = webview.create_window(**win_kwargs)
    except TypeError:
        win_kwargs.pop("hidden", None)
        win = webview.create_window(**win_kwargs)

    desktop.attach_window(win)
    desktop.start_tray()

    if sys.platform.startswith("linux"):
        webview.start(desktop.on_webview_ready, gui="qt")
    else:
        webview.start(desktop.on_webview_ready)

    stop_fastapi_server()


# ─── Entry point ───────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Touch Dashboard")
    p.add_argument("--server-only", action="store_true")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--reload", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.server_only:
        run_fastapi_server(host=args.host, port=args.port, reload=args.reload)
    else:
        launch_desktop(host=args.host, port=args.port)
