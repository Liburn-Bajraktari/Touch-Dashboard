import asyncio
import os
import json
import time
import logging
import threading
import subprocess
import re
import socket
import struct
import uuid
import base64
import urllib.parse
import argparse
import sys
import importlib.util
import site
import shutil
import ipaddress

RUNTIME_DEPENDENCIES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("requests", "requests"),
    ("psutil", "psutil"),
    ("speedtest", "speedtest-cli"),
    ("spotipy", "spotipy"),
    ("webview", "pywebview"),
    ("pystray", "pystray"),
    ("PIL", "Pillow"),
    ("pynvml", "pynvml"),
]

if sys.platform.startswith("linux"):
    RUNTIME_DEPENDENCIES.append(("evdev", "evdev"))
    RUNTIME_DEPENDENCIES.append(("gi", "PyGObject"))


def ensure_runtime_dependencies():
    missing_packages = [
        package_name
        for module_name, package_name in RUNTIME_DEPENDENCIES
        if importlib.util.find_spec(module_name) is None
    ]
    if not missing_packages:
        return

    if getattr(sys, "frozen", False):
        show_dependency_notice(
            missing_packages,
            "Touch Dashboard is missing bundled dependencies and cannot repair a packaged build automatically.\n"
            "Please reinstall the app or rebuild it with the listed dependencies included.",
        )
        raise SystemExit("Touch Dashboard packaged build is missing dependencies: " + ", ".join(missing_packages))

    if os.environ.get("TOUCH_DASHBOARD_SKIP_AUTO_INSTALL") == "1":
        show_dependency_notice(
            missing_packages,
            "Automatic dependency installation is disabled by TOUCH_DASHBOARD_SKIP_AUTO_INSTALL=1.\n"
            "Install the listed dependencies manually.",
        )
        raise SystemExit("Touch Dashboard dependencies are missing and auto-install is disabled: " + ", ".join(missing_packages))

    if not request_dependency_install_permission(missing_packages):
        raise SystemExit(
            "Touch Dashboard cannot start because required Python dependencies are missing: "
            + ", ".join(missing_packages)
        )

    print("Touch Dashboard: installing missing Python dependencies: " + ", ".join(missing_packages), flush=True)
    in_virtualenv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    cmd = [sys.executable, "-m", "pip", "install"]
    if not in_virtualenv:
        cmd.append("--user")
        import sysconfig
        stdlib_path = sysconfig.get_path("stdlib", sysconfig.get_default_scheme())
        if stdlib_path and os.path.exists(os.path.join(stdlib_path, "EXTERNALLY-MANAGED")):
            cmd.append("--break-system-packages")
    cmd.extend(missing_packages)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to install missing Python dependencies. "
            "Run `pip install -r requirements.txt` manually, or set TOUCH_DASHBOARD_SKIP_AUTO_INSTALL=1 to disable auto-install."
        )

    try:
        site.main()
    except Exception:
        pass
    importlib.invalidate_caches()


def format_dependency_message(missing_packages, lead):
    return (
        lead
        + "\n\nMissing dependencies:\n\n"
        + "\n".join(f"- {pkg}" for pkg in missing_packages)
    )


def show_dependency_popup(title, message):
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(title, message, parent=root)
        root.destroy()
        return True
    except Exception as e:
        print(f"Touch Dashboard: dependency popup failed: {e}", flush=True)

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
            return True
        except Exception as e:
            print(f"Touch Dashboard: native dependency popup failed: {e}", flush=True)

    if sys.platform.startswith("linux") and not is_linux_cli_launch():
        if shutil.which("zenity"):
            try:
                subprocess.run(["zenity", "--info", "--title", title, "--text", message], check=False)
                return True
            except Exception as e:
                print(f"Touch Dashboard: zenity dependency popup failed: {e}", flush=True)
        if shutil.which("kdialog"):
            try:
                subprocess.run(["kdialog", "--title", title, "--msgbox", message], check=False)
                return True
            except Exception as e:
                print(f"Touch Dashboard: kdialog dependency popup failed: {e}", flush=True)

    return False


def show_dependency_notice(missing_packages, lead):
    message = format_dependency_message(missing_packages, lead)
    shown = False
    if not is_linux_cli_launch():
        shown = show_dependency_popup("Touch Dashboard Dependencies", message)
    print(message, flush=True)
    return shown


def is_linux_cli_launch():
    if not sys.platform.startswith("linux"):
        return False
    if "--server-only" in sys.argv or "--reload" in sys.argv:
        return True
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def request_dependency_install_permission(missing_packages):
    message = format_dependency_message(
        missing_packages,
        "Touch Dashboard needs to install these missing Python dependencies.",
    ) + "\n\nDo you agree to install them now?"

    print(message, flush=True)

    if not is_linux_cli_launch():
        if ask_dependency_popup("Touch Dashboard Dependencies", message) is True:
            return True
        if ask_dependency_popup.last_result is False:
            return False

    try:
        answer = input("Install missing dependencies? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def ask_dependency_popup(title, message):
    ask_dependency_popup.last_result = None
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        approved = messagebox.askyesno(title, message, parent=root)
        root.destroy()
        ask_dependency_popup.last_result = approved
        return approved
    except Exception as e:
        print(f"Touch Dashboard: dependency permission popup failed: {e}", flush=True)

    if sys.platform.startswith("win"):
        try:
            import ctypes
            result = ctypes.windll.user32.MessageBoxW(None, message, title, 0x24)
            approved = result == 6
            ask_dependency_popup.last_result = approved
            return approved
        except Exception as e:
            print(f"Touch Dashboard: native dependency permission popup failed: {e}", flush=True)

    if sys.platform.startswith("linux") and not is_linux_cli_launch():
        if shutil.which("zenity"):
            try:
                result = subprocess.run(["zenity", "--question", "--title", title, "--text", message], check=False)
                approved = result.returncode == 0
                ask_dependency_popup.last_result = approved
                return approved
            except Exception as e:
                print(f"Touch Dashboard: zenity dependency permission popup failed: {e}", flush=True)
        if shutil.which("kdialog"):
            try:
                result = subprocess.run(["kdialog", "--title", title, "--yesno", message], check=False)
                approved = result.returncode == 0
                ask_dependency_popup.last_result = approved
                return approved
            except Exception as e:
                print(f"Touch Dashboard: kdialog dependency permission popup failed: {e}", flush=True)

    return None


ask_dependency_popup.last_result = None


ensure_runtime_dependencies()

import requests
import psutil
import speedtest
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, HTMLResponse
from contextlib import asynccontextmanager
import uvicorn
from fastapi.staticfiles import StaticFiles 
import hashlib

try:
    import webview
except ImportError:
    webview = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None

try:
    import pynvml
except ImportError:
    pynvml = None

# --- Configuration ---
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
APP_ICON_PATH = os.path.join(RESOURCE_DIR, "static", "favicon.ico")
WINDOWS_APP_USER_MODEL_ID = "TouchDashboard.TouchDashboard"

DATA_DIR = os.path.join(os.path.expanduser("~"), ".config", "touch-dashboard")
if sys.platform.startswith("win"):
    DATA_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "touch-dashboard")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
WEATHER_CACHE_FILE = os.path.join(DATA_DIR, "weather_cache.json")
SPOTIFY_CACHE_FILE = os.path.join(DATA_DIR, ".cache")
SOUNDS_DIR = os.path.join(DATA_DIR, "sounds")
os.makedirs(SOUNDS_DIR, exist_ok=True)

CONFIG_LOCK = threading.RLock()

def migrate_config():
    old_config = os.path.join(BASE_DIR, "config.json")
    old_spot = os.path.join(BASE_DIR, ".cache")
    old_weather = os.path.join(BASE_DIR, "weather_cache.json")
    
    if os.path.exists(old_config):
        try:
            with open(old_config, "r") as f:
                old_data = json.load(f)
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r") as f:
                    new_data = json.load(f)
                # Merge old into new (new takes precedence for keys it has, but old fills in missing/empty ones)
                for k, v in old_data.items():
                    if k not in new_data or not new_data[k]:
                        new_data[k] = v
                with open(CONFIG_FILE, "w") as f:
                    json.dump(new_data, f, indent=4)
            else:
                shutil.copy2(old_config, CONFIG_FILE)
        except: pass

    if os.path.exists(old_spot) and not os.path.exists(SPOTIFY_CACHE_FILE):
        try: shutil.copy2(old_spot, SPOTIFY_CACHE_FILE)
        except: pass
    if os.path.exists(old_weather) and not os.path.exists(WEATHER_CACHE_FILE):
        try: shutil.copy2(old_weather, WEATHER_CACHE_FILE)
        except: pass

migrate_config()

current_audio_process = None
uvicorn_server = None

logging.basicConfig(
    filename=os.path.join(DATA_DIR, 'server.log'),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "weather_api": "", "weather_city": "Pristina",
    "spot_id": "", "spot_secret": "",
    "disc_id": "", "disc_secret": "",
    "audio_names": {},
    "soundpad_buttons": [],
    "local_buttons": [],
    "sounds_path": ""
}

global_sp_oauth = None

has_nvml = False
nvml_handle = None
try:
    if pynvml is not None:
        pynvml.nvmlInit()
        nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        has_nvml = True
except Exception as e:
    logger.error(f"NVML Init failed: {e}")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f: return {**DEFAULT_CONFIG, **json.load(f)}
        except: return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config():
    with CONFIG_LOCK:
        tmp_file = f"{CONFIG_FILE}.tmp"
        with open(tmp_file, "w") as f:
            json.dump(config, f, indent=4)
        os.replace(tmp_file, CONFIG_FILE)

config = load_config()

# --- Platform ---
def get_os_target():
    return "windows" if os.name == 'nt' else "linux"

def get_local_sounds():
    """Scans the dynamically configured sounds directory for audio files."""
    sounds_dir = config.get("sounds_path", "")
    
    if not sounds_dir or not os.path.exists(sounds_dir):
        return []
        
    allowed_exts = {'.mp3', '.wav', '.ogg'}
    sounds = []
    
    try:
        for f in os.listdir(sounds_dir):
            ext = os.path.splitext(f)[1].lower()
            if ext in allowed_exts:
                sounds.append({
                    "id": f, 
                    "name": os.path.splitext(f)[0]
                })
        return sorted(sounds, key=lambda x: x['name'].lower())
    except Exception as e:
        logger.error(f"Failed to scan sounds directory: {e}")
        return []

# --- Local Soundboard ---
def resolve_sound_path(filename):
    sounds_dir = config.get("sounds_path", "")
    if not sounds_dir:
        return None

    base_path = os.path.realpath(sounds_dir)
    candidate = os.path.realpath(os.path.join(base_path, filename))
    if candidate != base_path and candidate.startswith(base_path + os.sep) and os.path.isfile(candidate):
        return candidate
    return None

def play_local_sound(filename):
    global current_audio_process
    filepath = resolve_sound_path(filename)
    if not filepath:
        logger.warning(f"Rejected local sound path outside configured directory: {filename}")
        return

    if current_audio_process is not None and current_audio_process.poll() is None:
        current_audio_process.terminate()
        try:
            current_audio_process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            current_audio_process.kill()
            current_audio_process.wait(timeout=1)

    current_audio_process = subprocess.Popen(
        ['pw-play', '--volume=1.0', '--target', 'Dashboard-Soundboard', filepath],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

# --- Macros ---
class MacroSystem:
    _ui = None
    
    @staticmethod
    def _init():
        if MacroSystem._ui is None and os.name != 'nt':
            try:
                import evdev
                MacroSystem._ui = evdev.UInput()
            except Exception as e:
                logger.error(f"Failed to init UInput for macros: {e}")

    @staticmethod
    def send_keys(*keys):
        if os.name == 'nt':
            try:
                import ctypes
                PUL = ctypes.POINTER(ctypes.c_ulong)
                class KeyBdInput(ctypes.Structure):
                    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]
                class HardwareInput(ctypes.Structure):
                    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short), ("wParamH", ctypes.c_ushort)]
                class MouseInput(ctypes.Structure):
                    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]
                class Input_I(ctypes.Union):
                    _fields_ = [("ki", KeyBdInput), ("mi", MouseInput), ("hi", HardwareInput)]
                class Input(ctypes.Structure):
                    _fields_ = [("type", ctypes.c_ulong), ("ii", Input_I)]

                vk_map = {
                    "KEY_A": 0x41, "KEY_B": 0x42, "KEY_C": 0x43, "KEY_D": 0x44, "KEY_E": 0x45, "KEY_F": 0x46, "KEY_G": 0x47, "KEY_H": 0x48,
                    "KEY_I": 0x49, "KEY_J": 0x4A, "KEY_K": 0x4B, "KEY_L": 0x4C, "KEY_M": 0x4D, "KEY_N": 0x4E, "KEY_O": 0x4F, "KEY_P": 0x50,
                    "KEY_Q": 0x51, "KEY_R": 0x52, "KEY_S": 0x53, "KEY_T": 0x54, "KEY_U": 0x55, "KEY_V": 0x56, "KEY_W": 0x57, "KEY_X": 0x58,
                    "KEY_Y": 0x59, "KEY_Z": 0x5A,
                    "KEY_0": 0x30, "KEY_1": 0x31, "KEY_2": 0x32, "KEY_3": 0x33, "KEY_4": 0x34, "KEY_5": 0x35, "KEY_6": 0x36, "KEY_7": 0x37,
                    "KEY_8": 0x38, "KEY_9": 0x39,
                    "KEY_LEFTCTRL": 0xA2, "KEY_RIGHTCTRL": 0xA3, "KEY_LEFTSHIFT": 0xA0, "KEY_RIGHTSHIFT": 0xA1,
                    "KEY_LEFTALT": 0xA4, "KEY_RIGHTALT": 0xA5, "KEY_ENTER": 0x0D, "KEY_ESC": 0x1B, "KEY_BACKSPACE": 0x08,
                    "KEY_TAB": 0x09, "KEY_SPACE": 0x20, "KEY_PAUSE": 0x13,
                    "KEY_F1": 0x70, "KEY_F2": 0x71, "KEY_F3": 0x72, "KEY_F4": 0x73, "KEY_F5": 0x74, "KEY_F6": 0x75, "KEY_F7": 0x76, "KEY_F8": 0x77,
                    "KEY_F9": 0x78, "KEY_F10": 0x79, "KEY_F11": 0x7A, "KEY_F12": 0x7B,
                    "KEY_MUTE": 0xAD, "KEY_VOLUMEDOWN": 0xAE, "KEY_VOLUMEUP": 0xAF,
                    "KEY_NEXTSONG": 0xB0, "KEY_PREVIOUSSONG": 0xB1, "KEY_STOPCD": 0xB2, "KEY_PLAYPAUSE": 0xB3
                }

                inputs_down = []
                inputs_up = []
                for k in keys:
                    vk = k if isinstance(k, int) else vk_map.get(str(k).upper(), 0)
                    if not vk: continue
                    ii_down = Input_I()
                    ii_down.ki = KeyBdInput(vk, 0, 0, 0, None)
                    inputs_down.append(Input(1, ii_down))
                    
                    ii_up = Input_I()
                    ii_up.ki = KeyBdInput(vk, 0, 0x0002, 0, None)
                    inputs_up.insert(0, Input(1, ii_up))
                
                if inputs_down:
                    ctypes.windll.user32.SendInput(len(inputs_down), (Input * len(inputs_down))(*inputs_down), ctypes.sizeof(Input))
                    time.sleep(0.05)
                    ctypes.windll.user32.SendInput(len(inputs_up), (Input * len(inputs_up))(*inputs_up), ctypes.sizeof(Input))
            except Exception as e:
                logger.error(f"Windows MacroSystem failed: {e}")
            return

        try:
            import evdev
            MacroSystem._init()
            if MacroSystem._ui:
                for k in keys:
                    ecode = k if isinstance(k, int) else getattr(evdev.ecodes, str(k).upper(), None)
                    if ecode is not None:
                        MacroSystem._ui.write(evdev.ecodes.EV_KEY, ecode, 1)
                MacroSystem._ui.syn()
                for k in reversed(keys):
                    ecode = k if isinstance(k, int) else getattr(evdev.ecodes, str(k).upper(), None)
                    if ecode is not None:
                        MacroSystem._ui.write(evdev.ecodes.EV_KEY, ecode, 0)
                MacroSystem._ui.syn()
        except Exception as e:
            logger.error(f"MacroSystem failed: {e}")

# --- App Enumeration ---
class AppEnumerator:
    _cached_apps = None
    _lock = threading.Lock()

    @staticmethod
    def get_apps():
        with AppEnumerator._lock:
            if AppEnumerator._cached_apps is not None:
                return AppEnumerator._cached_apps

            apps = []
            if os.name == 'nt':
                try:
                    ps_script = """
                    $apps = Get-StartApps | Select-Object Name, AppID
                    $result = @()
                    foreach ($app in $apps) {
                        $result += [PSCustomObject]@{
                            name = $app.Name
                            exec = $app.AppID
                        }
                    }
                    $result | ConvertTo-Json
                    """
                    out = subprocess.check_output(['powershell', '-NoProfile', '-Command', ps_script], stderr=subprocess.DEVNULL, timeout=10).decode().strip()
                    if out:
                        data = json.loads(out)
                        for d in data:
                            apps.append({"name": d.get("name"), "exec": d.get("exec"), "icon": ""})
                except Exception as e:
                    logger.error(f"Failed to enumerate Windows apps: {e}")
            else:
                try:
                    paths = [
                        os.path.expanduser('~/.local/share/applications'),
                        '/usr/share/applications'
                    ]
                    for path in paths:
                        if not os.path.exists(path): continue
                        for root_dir, dirs, files in os.walk(path):
                            for file in files:
                                if file.endswith('.desktop'):
                                    filepath = os.path.join(root_dir, file)
                                    try:
                                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                                            content = f.read()
                                            
                                        name = ""
                                        exec_cmd = ""
                                        icon = ""
                                        in_desktop_entry = False
                                        for line in content.splitlines():
                                            line = line.strip()
                                            if line == '[Desktop Entry]':
                                                in_desktop_entry = True
                                            elif line.startswith('[') and line != '[Desktop Entry]':
                                                in_desktop_entry = False
                                            
                                            if in_desktop_entry:
                                                if line.startswith('Name=') and not name:
                                                    name = line.split('=', 1)[1]
                                                elif line.startswith('Exec=') and not exec_cmd:
                                                    exec_cmd = line.split('=', 1)[1]
                                                elif line.startswith('Icon=') and not icon:
                                                    icon = line.split('=', 1)[1]
                                                    
                                        if name and exec_cmd and not 'NoDisplay=true' in content:
                                            exec_cmd = exec_cmd.replace('%f', '').replace('%F', '').replace('%u', '').replace('%U', '').replace('%c', '').replace('%k', '').strip()
                                            apps.append({"name": name, "exec": exec_cmd, "icon": icon})
                                    except: pass
                except Exception as e:
                    logger.error(f"Failed to enumerate Linux apps: {e}")

            unique_apps = {}
            for a in apps:
                if a['name'] not in unique_apps:
                    unique_apps[a['name']] = a
            
            AppEnumerator._cached_apps = sorted(list(unique_apps.values()), key=lambda x: x['name'].lower() if x['name'] else "")
            return AppEnumerator._cached_apps

    @staticmethod
    def launch_app(exec_cmd):
        if os.name == 'nt':
            try:
                subprocess.Popen(['explorer.exe', f'shell:AppsFolder\\{exec_cmd}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                logger.error(f"Failed to launch Windows app {exec_cmd}: {e}")
        else:
            try:
                import shlex
                args = shlex.split(exec_cmd)
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception as e:
                logger.error(f"Failed to launch Linux app {exec_cmd}: {e}")

# --- Audio ---
class AudioSystem:
    _last_state = {}
    _vol_targets = {}
    _vol_lock = threading.Lock()
    _vol_thread = None

    @staticmethod
    def _vol_worker():
        while True:
            tasks = []
            with AudioSystem._vol_lock:
                for target, val in list(AudioSystem._vol_targets.items()):
                    tasks.append((target, val))
                AudioSystem._vol_targets.clear()
            
            if not tasks:
                time.sleep(0.05)
                continue
                
            for target, val in tasks:
                AudioSystem.run(['wpctl', 'set-volume', target, f"{val}%"])
                AudioSystem.run(['wpctl', 'set-mute', target, '0'])

    @staticmethod
    def start_worker():
        if AudioSystem._vol_thread is None:
            AudioSystem._vol_thread = threading.Thread(target=AudioSystem._vol_worker, daemon=True)
            AudioSystem._vol_thread.start()

    @staticmethod
    def run(cmd):
        try: return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        except Exception as e:
            logger.debug(f"AudioSystem.run failed for cmd {cmd}: {e}")
            return ""

    @staticmethod
    def get_state(target):
        out = AudioSystem.run(['wpctl', 'get-volume', target])
        if not out: 
            return AudioSystem._last_state.get(target, {"vol": 0, "muted": False})
        try:
            parts = out.split()
            vol = int(float(parts[1]) * 100) if len(parts) > 1 else 0
        except Exception: vol = 0
        
        state = {"vol": vol, "muted": '[MUTED]' in out}
        AudioSystem._last_state[target] = state
        return state

    @staticmethod
    def poll_all():
        sinks = []
        active_sink_name = "NONE"
        try:
            out = AudioSystem.run(['wpctl', 'status'])
            section = None
            for line in out.splitlines():
                if 'Sinks:' in line: section = 'sinks'; continue
                elif any(x in line for x in ['Sources:', 'Filters:', 'Streams:', 'Video:', 'Devices:']): 
                    if section == 'sinks': section = None
                    continue
                
                if section == 'sinks':
                    clean = line.translate(str.maketrans('', '', '\u2502\u251c\u2514\u2500')).strip()
                    if not clean: continue
                    match = re.search(r'^(\*)?\s*(\d+)\.\s+([^\[]+)', clean)
                    if match:
                        is_active, dev_id, raw_name = bool(match.group(1)), match.group(2), match.group(3).strip()
                        
                        if "Dashboard-Soundboard" in raw_name:
                            continue
                            
                        custom_name = config.get("audio_names", {}).get(raw_name, "")
                        display_name = custom_name[:10] if custom_name else raw_name[:5].upper()
                        sinks.append({"id": dev_id, "name": display_name, "raw_name": raw_name, "custom_name": custom_name, "is_active": is_active})
                        if is_active: active_sink_name = display_name
        except Exception as e: logger.error(f"Audio parse error: {e}")
        
        return {
            "sinks": sinks, "active_sink_name": active_sink_name if sinks else "NONE",
            "spk": AudioSystem.get_state('@DEFAULT_AUDIO_SINK@'), "mic": AudioSystem.get_state('@DEFAULT_AUDIO_SOURCE@')
        }

    @staticmethod
    def get_hardware_sinks(): return AudioSystem.poll_all()['sinks']
    @staticmethod
    def get_active_sink_id(sinks=None):
        if sinks is None: sinks = AudioSystem.poll_all()['sinks']
        for s in sinks:
            if s.get('is_active'): return s['id']
        return None
    @staticmethod
    def set_vol(target, val): 
        AudioSystem.start_worker()
        with AudioSystem._vol_lock:
            AudioSystem._vol_targets[target] = val

    @staticmethod
    def toggle_mute(target):
        if get_os_target() == 'linux':
            AudioSystem.run(['wpctl', 'set-mute', target, 'toggle'])
        elif os.name == 'nt' and target == '@DEFAULT_AUDIO_SINK@':
            MacroSystem.send_keys('KEY_MUTE')
    @staticmethod
    def cycle_device():
        sinks = AudioSystem.poll_all()['sinks']
        if not sinks: return
        active_id = AudioSystem.get_active_sink_id(sinks)
        next_sink = sinks[0]
        if active_id:
            for i, s in enumerate(sinks):
                if s['id'] == active_id:
                    next_sink = sinks[(i + 1) % len(sinks)]
                    break
        AudioSystem.run(['wpctl', 'set-default', next_sink['id']])

# --- Discord IPC ---
class DiscordIPC:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.sock = None
        self.access_token = config.get("disc_token", "")
        self.connected = False
        self.running = False
        self.voice_state = {"mute": False, "deaf": False}
        self.voice_supported = False
        self.auth_pending = False
        self.pre_deafen_mute = False
        self.is_vesktop = False

    def get_pipe_paths(self):
        paths_found = []
        if os.name == 'nt': 
            if os.path.exists(r'\\.\pipe\discord-ipc-0'):
                paths_found.append(r'\\.\pipe\discord-ipc-0')
            return paths_found
        
        env_vars = ['XDG_RUNTIME_DIR', 'TMPDIR', 'TMP', 'TEMP']
        paths = [os.environ.get(v) for v in env_vars if os.environ.get(v)]
        paths.extend([f"/run/user/{os.getuid()}", "/tmp"])
        
        for base_path in paths:
            for i in range(10):
                path = os.path.join(base_path, f"discord-ipc-{i}")
                flatpak_path = os.path.join(base_path, "app/com.discordapp.Discord", f"discord-ipc-{i}")
                
                if os.path.exists(path) and path not in paths_found: paths_found.append(path)
                if os.path.exists(flatpak_path) and flatpak_path not in paths_found: paths_found.append(flatpak_path)
        return paths_found

    def connect(self):
        pipe_paths = self.get_pipe_paths()
        if not pipe_paths: return False
        
        for pipe_path in pipe_paths:
            try:
                if os.name == 'nt': 
                    self.sock = open(pipe_path, 'w+b')
                else:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(pipe_path)
                    self.sock.settimeout(2.0)
                
                self.send(0, {"v": 1, "client_id": self.client_id})
                res = self.recv()
                if not res:
                    self.close()
                    continue
                    
                self.is_vesktop = (res.get("data", {}).get("user", {}).get("username") == "arrpc")
                
                if self.is_vesktop:
                    self.auth_pending = False
                    self.connected = True
                    return True
                
                if not self.access_token:
                    self.auth_pending = True
                    self.connected = True
                    return True
                
                self.authenticate()
                self.connected = True
                return True
            except Exception as e:
                logger.error(f"Discord connect error on {pipe_path}: {e}")
                self.close()
                
        return False

    def close(self):
        self.connected = False
        if self.sock:
            try: self.sock.close()
            except: pass
        self.sock = None

    def sock_send(self, data):
        if os.name == 'nt':
            self.sock.write(data)
            self.sock.flush()
        else: 
            self.sock.sendall(data)

    def sock_recv(self, length):
        data = b""
        while len(data) < length:
            chunk = self.sock.read(length - len(data)) if os.name == 'nt' else self.sock.recv(length - len(data))
            if not chunk: return b""
            data += chunk
        return data

    def send(self, opcode, payload):
        logger.info(f"DISCORD SEND [{opcode}]: {payload}")
        data = json.dumps(payload).encode('utf-8')
        try: self.sock_send(struct.pack("<II", opcode, len(data)) + data)
        except Exception: self.connected = False

    def recv(self):
        try:
            header = self.sock_recv(8)
            if not header or len(header) < 8: 
                return None
            opcode, length = struct.unpack("<II", header)
            payload = self.sock_recv(length)
            if not payload:
                return None
            res = json.loads(payload.decode('utf-8'))
            logger.info(f"DISCORD RECV [{opcode}]: {res}")
            return res
        except socket.timeout:
            return {}
        except Exception as e:
            logger.error(f"DISCORD RECV ERROR: {e}")
            return None

    def get_auth_url(self):
        """Generates the URL the user must visit to authorize the app."""
        scopes = "rpc rpc.voice.read rpc.voice.write"
        redirect_uri = urllib.parse.quote("http://127.0.0.1:5000/disc_callback")
        return f"https://discord.com/api/oauth2/authorize?client_id={self.client_id}&redirect_uri={redirect_uri}&response_type=code&scope={scopes}"

    def exchange_code(self, code):
        """Exchanges the callback code for an access token."""
        try:
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1:5000/disc_callback"
            }
            r = requests.post("https://discord.com/api/oauth2/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=5)
            if r.status_code == 200:
                self.access_token = r.json().get("access_token")
                with CONFIG_LOCK:
                    config["disc_token"] = self.access_token
                    save_config()
                return True
            logger.error(f"Discord Token Exchange Failed: {r.text}")
        except Exception as e:
             logger.error(f"Discord Token Exception: {e}")
        return False

    def authenticate(self):
        self.send(1, {"cmd": "AUTHENTICATE", "args": {"access_token": self.access_token}, "nonce": str(uuid.uuid4())})
        
        auth_res = self.recv()
        if auth_res and auth_res.get("evt") == "ERROR":
            logger.error(f"Discord IPC Auth Error: {auth_res}")
            self.access_token = ""
            with CONFIG_LOCK:
                config["disc_token"] = ""
                save_config()
            self.auth_pending = True
            return

        self.send(1, {"cmd": "SUBSCRIBE", "evt": "VOICE_SETTINGS_UPDATE", "args": {}, "nonce": str(uuid.uuid4())})
        self.send(1, {"cmd": "GET_VOICE_SETTINGS", "args": {}, "nonce": "GET_VOICE"})
        self.auth_pending = False
        
        for _ in range(2):
            res = self.recv()
            if res and res.get("cmd") == "GET_VOICE_SETTINGS" and res.get("evt") != "ERROR":
                self.voice_supported = True
                data = res.get("data", {})
                if "mute" in data: self.voice_state["mute"] = data["mute"]
                if "deaf" in data: self.voice_state["deaf"] = data["deaf"]

    def loop(self):
        self.running = True
        last_ping = time.time()
        while self.running:
            if getattr(self, "needs_reauth", False) and self.connected:
                self.needs_reauth = False
                self.authenticate()
                
            if not self.connected:
                if not self.connect():
                    time.sleep(5)
                    continue
            if self.connected:
                try:
                    if os.name != 'nt': self.sock.settimeout(5.0)
                    res = self.recv()
                    if res is None: 
                        self.close()
                        continue
                        
                    if res:
                        if res.get("evt") == "ERROR":
                            logger.error(f"DISCORD IPC ERROR: {res}")
                            
                        if res.get("cmd") == "AUTHORIZE" and "data" in res and "code" in res["data"]:
                            code = res["data"]["code"]
                            logger.info(f"Got IPC AUTH code: {code}")
                            success = self.exchange_code(code)
                            if success:
                                self.authenticate()

                        if (
                            res.get("evt") == "VOICE_SETTINGS_UPDATE" or 
                            res.get("cmd") == "GET_VOICE_SETTINGS" or 
                            res.get("cmd") == "SET_VOICE_SETTINGS" or
                            res.get("nonce") == "GET_VOICE"
                        ):
                            self.voice_supported = True
                            data = res.get("data", {})
                            if "mute" in data: self.voice_state["mute"] = data["mute"]
                            if "deaf" in data: self.voice_state["deaf"] = data["deaf"]
                except Exception as e:
                    logger.error(f"Discord loop error: {e}")
                    self.close()
                    time.sleep(2)
            else:
                 time.sleep(2)

    def set_voice(self, mute=None, deaf=None):
        if not self.connected: return
        args = {}
        if mute is not None: args["mute"] = mute
        if deaf is not None: args["deaf"] = deaf
        self.send(1, {"cmd": "SET_VOICE_SETTINGS", "args": args, "nonce": str(uuid.uuid4())})
disc_ipc_instance = None
def restart_discord_ipc():
    global disc_ipc_instance
    if disc_ipc_instance:
        disc_ipc_instance.running = False
        disc_ipc_instance.close()
        disc_ipc_instance = None
    if config.get("disc_id") and config.get("disc_secret"):
        disc_ipc_instance = DiscordIPC(config["disc_id"], config["disc_secret"])
        threading.Thread(target=disc_ipc_instance.loop, daemon=True).start()

# --- Spotify ---
def get_sp_oauth():
    global global_sp_oauth
    if not config.get("spot_id") or not config.get("spot_secret"): return None
    
    if global_sp_oauth is None:
        global_sp_oauth = SpotifyOAuth(
            client_id=config["spot_id"], 
            client_secret=config["spot_secret"], 
            redirect_uri="http://127.0.0.1:5000/callback", 
            scope="user-read-playback-state user-modify-playback-state", 
            open_browser=False, 
            cache_path=SPOTIFY_CACHE_FILE
        )
    return global_sp_oauth

# --- Media Metadata ---
def get_spotify_api_meta():
    sp_oauth = get_sp_oauth()
    if not sp_oauth: return None
    try:
        token_info = sp_oauth.get_cached_token()
        if not token_info: return {"status": "Auth_Required", "artist": "", "title": "Spotify Not Authorized", "art_url": ""}
        
        sp = spotipy.Spotify(auth=token_info['access_token'], requests_timeout=3)
        curr = sp.current_playback()
        if curr and curr.get('item'):
            art_url = curr['item']['album']['images'][0]['url'] if curr['item'].get('album') and curr['item']['album'].get('images') else ""
            return {
                "status": "Playing" if curr.get('is_playing') else "Paused",
                "artist": curr['item']['artists'][0]['name'] if curr.get('item', {}).get('artists') else "Unknown",
                "title": curr['item'].get('name', 'Unknown'),
                "art_url": art_url
            }
    except Exception as e: logger.debug(f"Spotify API error: {e}")
    return None

last_mpris_title = ""
last_mpris_path = ""
mpris_burst_active = False
mpris_burst_start = 0
last_art_url = ""

def get_local_mpris_meta():
    global last_mpris_title, last_mpris_path, mpris_burst_active, mpris_burst_start, last_art_url
    try:
        meta = AudioSystem.run(['playerctl', 'metadata', '--format', '{{status}}|||{{artist}}|||{{title}}|||{{mpris:artUrl}}'])
        if not meta: return {"status": "Stopped", "artist": "", "title": "Nothing Playing", "art_url": ""}
        parts = meta.split('|||')
        status = parts[0].strip() if len(parts) > 0 else "Stopped"
        if status not in ['Playing', 'Paused']: return {"status": "Stopped", "artist": "", "title": "Nothing Playing", "art_url": ""}
        
        artist = parts[1].strip() if len(parts) > 1 else "Unknown"
        title = parts[2].strip() if len(parts) > 2 else "Unknown"
        raw_art_url = parts[3].strip() if len(parts) > 3 else ""

        current_song = f"{artist}-{title}"

        # Some players expose cover art before the file is fully written.
        if current_song != last_mpris_title:
            last_mpris_title = current_song
            mpris_burst_active = True
            mpris_burst_start = time.time()
            last_art_url = "WAITING"

        if mpris_burst_active:
            if raw_art_url.startswith('file://'):
                path = urllib.parse.unquote(raw_art_url.replace('file://', ''))
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    last_mpris_path = path
                    mod_time = str(os.path.getmtime(path))
                    f_size = str(os.path.getsize(path))
                    song_hash = hashlib.md5((current_song + mod_time + f_size).encode()).hexdigest()
                    last_art_url = f"/api/local_art?h={song_hash}"
                    mpris_burst_active = False
                elif time.time() - mpris_burst_start > 6.0:
                    last_art_url = ""
                    mpris_burst_active = False
            else:
                last_art_url = raw_art_url
                mpris_burst_active = False
        else:
            if raw_art_url.startswith('file://'):
                path = urllib.parse.unquote(raw_art_url.replace('file://', ''))
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    last_mpris_path = path
                    mod_time = str(os.path.getmtime(path))
                    f_size = str(os.path.getsize(path))
                    song_hash = hashlib.md5((current_song + mod_time + f_size).encode()).hexdigest()
                    last_art_url = f"/api/local_art?h={song_hash}"

        return {"status": status, "artist": artist, "title": title, "art_url": last_art_url}
    except: return {"status": "Stopped", "artist": "", "title": "Nothing Playing", "art_url": ""}

# --- Shared State ---
force_media_update, current_media_source = False, "local"
spotify_cache, last_spotify_check, last_audio_devs = None, 0, []
last_weather_data = {"temp": "--", "desc": "--", "timestamp": 0}
weather_update_event = asyncio.Event()
last_host_url = "127.0.0.1:5000"
index_template_cache = {"mtime": 0.0, "html": ""}
speedtest_lock = asyncio.Lock()

def load_index_template():
    path = os.path.join(RESOURCE_DIR, "templates", "index.html")
    mtime = os.path.getmtime(path)
    if index_template_cache["mtime"] != mtime:
        with open(path, "r", encoding="utf-8") as f:
            index_template_cache["html"] = f.read()
        index_template_cache["mtime"] = mtime
    return index_template_cache["html"]

class ConnectionManager:
    def __init__(self): self.active_connections: list[WebSocket] = []
    def has_clients(self):
        return bool(self.active_connections)
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections: self.active_connections.remove(websocket)
    async def broadcast(self, message: dict):
        stale_connections = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.debug(f"WebSocket broadcast failed; dropping stale connection: {e}")
                stale_connections.append(connection)
        for connection in stale_connections:
            self.disconnect(connection)

ws_manager = ConnectionManager()

# --- Background Tasks ---
async def hardware_loop():
    global last_spotify_check, spotify_cache, last_audio_devs, force_media_update, current_media_source
    global mpris_burst_active
    last_gpu_check, gpu_cache = 0, ""
    
    last_audio_check = 0
    audio_cache = {"spk": {"vol": 0, "muted": False}, "mic": {"vol": 0, "muted": False}, "active_dev": "NONE"}
    batt_cache = "--"

    def read_batt():
        try:
            with open("/tmp/g502_battery.txt", "r") as f: return f.read().strip()
        except: return "--"
        
    while True:
        curr_time = time.time()

        if not ws_manager.has_clients():
            await asyncio.sleep(2.0)
            continue
        
        # Keep hardware polling slower during short media-art bursts.
        if curr_time - last_audio_check >= 1.0:
            audio_data = await asyncio.to_thread(AudioSystem.poll_all)
            curr_sinks = audio_data['sinks']
            if [s['raw_name'] for s in curr_sinks] != [s['raw_name'] for s in last_audio_devs]:
                last_audio_devs = curr_sinks
                await ws_manager.broadcast({"type": "hw_scan_results", "data": curr_sinks})
            
            audio_cache = {"spk": audio_data['spk'], "mic": audio_data['mic'], "active_dev": audio_data['active_sink_name']}
            batt_cache = await asyncio.to_thread(read_batt)
            last_audio_check = curr_time

        if curr_time - last_spotify_check > 3.0 or force_media_update:
            if force_media_update: await asyncio.sleep(0.4)
            spotify_cache = await asyncio.to_thread(get_spotify_api_meta) 
            last_spotify_check = time.time()
            force_media_update = False

        media = spotify_cache
        if not media or media['status'] != 'Playing':
            local_media = await asyncio.to_thread(get_local_mpris_meta)
            if local_media['status'] == 'Playing' or not media: 
                media, current_media_source = local_media, "local"
            else: current_media_source = "spotify"
        else: current_media_source = "spotify"

        if curr_time - last_gpu_check > 2.0:
            if has_nvml:
                try:
                    util = await asyncio.to_thread(pynvml.nvmlDeviceGetUtilizationRates, nvml_handle)
                    gpu_cache = str(util.gpu)
                except: gpu_cache = ""
            last_gpu_check = curr_time

        disc_has_token = bool(config.get("disc_token", ""))
        disc_has_creds = bool(config.get("disc_id")) and bool(config.get("disc_secret"))
        
        fallback_auth_url = ""
        if disc_has_creds and not disc_has_token:
            scopes = "rpc rpc.voice.read rpc.voice.write"
            redirect_uri = urllib.parse.quote("http://127.0.0.1:5000/disc_callback")
            fallback_auth_url = f"https://discord.com/api/oauth2/authorize?client_id={config['disc_id']}&redirect_uri={redirect_uri}&response_type=code&scope={scopes}"

        disc_state = {
            "mute": False, 
            "deaf": False, 
            "auth_required": disc_has_creds and not disc_has_token, 
            "auth_url": fallback_auth_url, 
            "connected": False, 
            "voice_supported": False,
            "authorized": disc_has_token
        }
        
        if disc_ipc_instance:
            disc_state["connected"] = disc_ipc_instance.connected
            disc_state["voice_supported"] = getattr(disc_ipc_instance, "voice_supported", False)
            disc_state["auth_pending"] = getattr(disc_ipc_instance, "auth_pending", False)
            
            if disc_ipc_instance.connected:
                disc_state["mute"] = disc_ipc_instance.voice_state.get("mute", False)
                disc_state["deaf"] = disc_ipc_instance.voice_state.get("deaf", False)
            
            if not disc_has_token and disc_ipc_instance.auth_pending:
                disc_state["auth_url"] = disc_ipc_instance.get_auth_url()

        await ws_manager.broadcast({
            "type": "sys_data",
            "data": {
                "cpu": psutil.cpu_percent(interval=None), 
                "ram": psutil.virtual_memory().percent,
                "gpu": gpu_cache if gpu_cache else None, 
                "mouse_batt": batt_cache, 
                "spotify": media,
                "discord": disc_state,
                "audio": {"spk": audio_cache['spk'], "mic": audio_cache['mic'], "active_dev": audio_cache['active_dev']}
            }
        })
        
        sleep_duration = 0.2 if mpris_burst_active else 1.0
        await asyncio.sleep(sleep_duration)
async def fetch_weather():
    global last_weather_data, weather_force_update
    
    async def do_fetch():
        global last_weather_data
        if config.get("weather_api") and config.get("weather_city"):
            try:
                res = await asyncio.to_thread(
                    requests.get, 
                    "http://api.openweathermap.org/data/2.5/weather", 
                    params={"q": config['weather_city'], "appid": config['weather_api'], "units": "metric"}, 
                    timeout=5
                )
                res_data = res.json()
                if "main" in res_data:
                    last_weather_data = {"temp": round(res_data["main"]["temp"]), "desc": res_data["weather"][0]["description"].title(), "timestamp": time.time()}
                    with open(WEATHER_CACHE_FILE, "w") as f: json.dump(last_weather_data, f)
                    
                    await ws_manager.broadcast({"type": "weather_data", "data": last_weather_data})
            except Exception as e:
                logger.debug(f"Weather fetch error: {e}")
                
        last_weather_data["timestamp"] = time.time()

    if os.path.exists(WEATHER_CACHE_FILE):
        try:
            with open(WEATHER_CACHE_FILE, "r") as f: last_weather_data = json.load(f)
        except: pass

    if time.time() - last_weather_data.get("timestamp", 0) > 1800: 
        await do_fetch()

    while True:
        try:
            await asyncio.wait_for(weather_update_event.wait(), timeout=1800)
            weather_update_event.clear() 
        except asyncio.TimeoutError:
            pass 
        await do_fetch()

# --- PipeWire Routing ---
async def pipewire_auto_router():
    """Injects Soundboard audio directly into applications using the microphone."""
    async def check_output_limited(cmd, timeout=2):
        return await asyncio.to_thread(
            subprocess.check_output,
            cmd,
            stderr=subprocess.DEVNULL,
            timeout=timeout
        )

    async def run_limited(cmd, timeout=2):
        return await asyncio.to_thread(
            subprocess.run,
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout
        )

    while True:
        try:
            def_src = (await check_output_limited(['pactl', 'get-default-source'])).decode().strip()

            pw_out = (await check_output_limited(['pw-link', '-o'])).decode()
            sb_monitors = [p.strip() for p in pw_out.splitlines() if 'Dashboard-Soundboard' in p and 'monitor' in p]
            
            if not sb_monitors:
                await asyncio.sleep(2)
                continue
            
            sb_FL = sb_monitors[0]
            sb_FR = sb_monitors[1] if len(sb_monitors) > 1 else sb_FL

            pw_links = (await check_output_limited(['pw-link', '-l'])).decode()
            target_app_ports = []
            is_mic_capture = False
            
            for line in pw_links.splitlines():
                if not line.startswith((' ', '\t')):
                    is_mic_capture = (def_src in line and 'capture' in line)
                elif is_mic_capture and '|->' in line:
                    app_port = line.split('|->')[1].strip()
                    if 'Dashboard-Soundboard' not in app_port and 'loopback' not in app_port.lower():
                        target_app_ports.append(app_port)

            for i, app_port in enumerate(target_app_ports):
                src = sb_FL if i % 2 == 0 else sb_FR
                await run_limited(['pw-link', src, app_port])

        except Exception as e:
            logger.debug(f"PipeWire auto-router error: {e}")
        
        await asyncio.sleep(5) 


# --- FastAPI App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(SOUNDS_DIR, exist_ok=True)

    def check_output_limited(cmd, timeout=3):
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout)

    def run_limited(cmd, timeout=3, check=False):
        return subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=check
        )
    
    if get_os_target() == "linux":
        try:
            # Preserve the real defaults before creating the virtual sink.
            try:
                real_sink = check_output_limited(['pactl', 'get-default-sink']).decode().strip()
                real_src = check_output_limited(['pactl', 'get-default-source']).decode().strip()
            except:
                real_sink, real_src = "", ""

            sinks_output = check_output_limited(['pactl', 'list', 'short', 'sinks']).decode()
            if 'Dashboard-Soundboard' not in sinks_output:
                logger.info("Virtual Sink 'Dashboard-Soundboard' not found. Creating it now...")
                run_limited([
                    'pactl', 'load-module', 'module-null-sink', 
                    'sink_name=Dashboard-Soundboard', 
                    'sink_properties=device.description="Dashboard-Soundboard"'
                ], check=True)
            
            run_limited(['pactl', 'set-sink-volume', 'Dashboard-Soundboard', '100%'])
            
            modules_output = check_output_limited(['pactl', 'list', 'short', 'modules']).decode()
            if 'source=Dashboard-Soundboard.monitor' not in modules_output:
                run_limited(['pactl', 'load-module', 'module-loopback', 'source=Dashboard-Soundboard.monitor'], check=True)
                logger.info("Native Audio Loopback established.")

            if real_sink and 'Dashboard' not in real_sink:
                run_limited(['pactl', 'set-default-sink', real_sink])
            if real_src and 'Dashboard' not in real_src:
                run_limited(['pactl', 'set-default-source', real_src])
                
        except Exception as e:
            logger.error(f"Failed to setup PipeWire virtual sink: {e}")

    restart_discord_ipc()
    app.state.background_tasks = [
        asyncio.create_task(hardware_loop(), name="hardware_loop"),
        asyncio.create_task(fetch_weather(), name="fetch_weather"),
    ]
    
    if get_os_target() == "linux":
        app.state.background_tasks.append(asyncio.create_task(pipewire_auto_router(), name="pipewire_auto_router"))
        
    yield
    for task in getattr(app.state, "background_tasks", []):
        task.cancel()
    if getattr(app.state, "background_tasks", []):
        await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
    if disc_ipc_instance: disc_ipc_instance.close()

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self' 'unsafe-inline' 'unsafe-eval' ws: wss: https://unpkg.com https://cdnjs.cloudflare.com; img-src 'self' data: https:;"
    return response

app.mount("/static", StaticFiles(directory=os.path.join(RESOURCE_DIR, "static")), name="static")

# --- Routes ---
@app.get('/api/local_art')
async def serve_local_art():
    global last_mpris_path
    if last_mpris_path and os.path.exists(last_mpris_path):
        return FileResponse(last_mpris_path)
    return JSONResponse({"error": "No art found"}, status_code=404)

@app.get('/')
async def index(request: Request):
    global last_host_url
    last_host_url = request.url.netloc
    
    html = load_index_template()

    if disc_ipc_instance and disc_ipc_instance.connected and not disc_ipc_instance.auth_pending:
        html = html.replace('id="panel-discord" class="glass panel" style="display: none;', 'id="panel-discord" class="glass panel" style="display: flex;')
        if disc_ipc_instance.voice_state.get("mute", False):
            html = html.replace('class="btn" id="btn-disc-mute"', 'class="btn muted" id="btn-disc-mute"')
        if disc_ipc_instance.voice_state.get("deaf", False):
            html = html.replace('class="btn" id="btn-disc-deaf"', 'class="btn muted" id="btn-disc-deaf"')

    response = HTMLResponse(content=html)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get('/favicon.ico')
async def favicon():
    return JSONResponse({})

@app.get('/manifest.json')
async def manifest():
    return JSONResponse(content={
        "name": "Command Center Dashboard",
        "short_name": "CmdCenter",
        "start_url": "/?v=1.6",
        "display": "standalone",
        "orientation": "landscape",
        "background_color": "#090e17",
        "theme_color": "#0ea5e9",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    }, headers={"Cache-Control": "no-cache"})

@app.get('/spotify_login')
async def spotify_login(request: Request):
    sp_oauth = get_sp_oauth()
    if not sp_oauth: return JSONResponse({"error": "No credentials"})
    return RedirectResponse(sp_oauth.get_authorize_url())

@app.get('/callback')
async def callback(request: Request, code: str = None):
    sp_oauth = get_sp_oauth()
    if not sp_oauth or not code: return RedirectResponse('/')
    try: await asyncio.to_thread(sp_oauth.get_access_token, code)
    except: pass
    return RedirectResponse('/')

@app.get('/disc_callback')
async def discord_callback(request: Request, code: str = None):
    """Catches the OAuth2 redirect from Discord."""
    if not code or not disc_ipc_instance:
        return RedirectResponse('/')
    
    success = await asyncio.to_thread(disc_ipc_instance.exchange_code, code)
    if success:
        logger.info("Discord authorization successful!")
        # Safely flag for re-auth on the next loop tick instead of closing the socket
        disc_ipc_instance.needs_reauth = True
    else:
        logger.error("Discord authorization failed during callback.")
        
    return RedirectResponse('/')

# --- WebSocket ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global config, weather_force_update, force_media_update, current_media_source, current_audio_process, global_sp_oauth
    
    await ws_manager.connect(websocket)
    
    try:
        initial_hardware = await asyncio.to_thread(AudioSystem.get_hardware_sinks)
        await websocket.send_json({
            "type": "config_sync",
            "data": {
                "cfg": config,
                "hw": initial_hardware,
                "os_target": get_os_target(),
                "host_ip": get_lan_ip(),
            },
        })

        if last_weather_data.get("temp") != "--":
            await websocket.send_json({"type": "weather_data", "data": last_weather_data})

        while True:
            text = await websocket.receive_text()
            msg = json.loads(text)
            msg_type, data = msg.get("type"), msg.get("data")
            
            if msg_type == 'req_local_sounds':
                sounds = await asyncio.to_thread(get_local_sounds)
                await websocket.send_json({"type": "local_sounds_list", "data": sounds})
                
            elif msg_type == 'req_apps_list':
                apps = await asyncio.to_thread(AppEnumerator.get_apps)
                await websocket.send_json({"type": "apps_list", "data": apps})
                
            elif msg_type == 'save_macros':
                with CONFIG_LOCK:
                    config["macros"] = data
                await asyncio.to_thread(save_config)
                audio_data = await asyncio.to_thread(AudioSystem.poll_all)
                await ws_manager.broadcast({
                    "type": "config_sync",
                    "data": {
                        "cfg": config,
                        "hw": audio_data['sinks'],
                        "os_target": get_os_target(),
                        "host_ip": get_lan_ip(),
                    },
                })
                
            elif msg_type == 'macro_exec':
                m_type = data.get('type')
                action_data = data.get('action_data')
                
                if m_type == 'app':
                    await asyncio.to_thread(AppEnumerator.launch_app, action_data)
                elif m_type == 'macro':
                    keys = action_data if isinstance(action_data, list) else [action_data]
                    await asyncio.to_thread(MacroSystem.send_keys, *keys)
                elif m_type in ('premade', 'plugin'):
                    # Fallback into the existing action system
                    msg_type = 'action'
                    data = action_data
                    
            if msg_type == 'save_config':
                old_id, old_secret = config.get("disc_id"), config.get("disc_secret")
                old_weather_api, old_weather_city = config.get("weather_api"), config.get("weather_city")
                old_spot_id = config.get("spot_id")

                with CONFIG_LOCK:
                    config.update(data)
                await asyncio.to_thread(save_config)

                if old_spot_id != config.get("spot_id"):
                    global_sp_oauth = None
                
                audio_data = await asyncio.to_thread(AudioSystem.poll_all)
                await ws_manager.broadcast({
                    "type": "config_sync",
                    "data": {
                        "cfg": config,
                        "hw": audio_data['sinks'],
                        "os_target": get_os_target(),
                        "host_ip": get_lan_ip(),
                    },
                })
                
                if old_id != config.get("disc_id") or old_secret != config.get("disc_secret"): restart_discord_ipc()
                if old_weather_api != config.get("weather_api") or old_weather_city != config.get("weather_city"): 
                    weather_update_event.set()

            elif msg_type == 'action':
                action = data
                
                if action.startswith('local_play_'):
                    filename = action.removeprefix('local_play_')
                    await asyncio.to_thread(play_local_sound, filename)
                
                elif action.startswith('spot_'):
                    routed_to_spot = False
                    sp_oauth = get_sp_oauth()
                    if current_media_source == "spotify" and sp_oauth:
                        try:
                            token_info = await asyncio.to_thread(sp_oauth.get_cached_token)
                            if token_info:
                                sp = spotipy.Spotify(auth=token_info['access_token'], requests_timeout=3)
                                if action == 'spot_play':
                                    c = await asyncio.to_thread(sp.current_playback)
                                    if c and c.get('is_playing'): await asyncio.to_thread(sp.pause_playback)
                                    else: await asyncio.to_thread(sp.start_playback)
                                elif action == 'spot_next': await asyncio.to_thread(sp.next_track)
                                elif action == 'spot_prev': await asyncio.to_thread(sp.previous_track)
                                routed_to_spot, force_media_update = True, True
                        except: pass
                    if not routed_to_spot:
                        if action == 'spot_play': await asyncio.to_thread(AudioSystem.run, ['playerctl', 'play-pause'])
                        elif action == 'spot_next': await asyncio.to_thread(AudioSystem.run, ['playerctl', 'next'])
                        elif action == 'spot_prev': await asyncio.to_thread(AudioSystem.run, ['playerctl', 'previous'])
                        force_media_update = True
                
                elif action.startswith('sp_play_'): 
                    sp_id = action.split('sp_play_')[1]
                    if os.name == 'nt':
                        sp_path = r"C:\Program Files\Soundpad\Soundpad.exe"
                        if not os.path.exists(sp_path):
                            sp_path = r"C:\Program Files (x86)\Steam\steamapps\common\Soundpad\Soundpad.exe"
                        if os.path.exists(sp_path):
                            await asyncio.to_thread(AudioSystem.run, [sp_path, '-rc', f'DoPlaySound({sp_id})'])

                elif action == 'spot_clear_auth':
                    global_sp_oauth = None 
                    with CONFIG_LOCK:
                        config["spot_token"] = ""
                        save_config()
                    if os.path.exists(SPOTIFY_CACHE_FILE):
                        try: os.remove(SPOTIFY_CACHE_FILE)
                        except: pass

                elif action == 'disc_clear_auth':
                    with CONFIG_LOCK:
                        config["disc_token"] = ""
                        save_config()
                    restart_discord_ipc()

                elif action == 'disc_auth':
                    if disc_ipc_instance and disc_ipc_instance.connected:
                        disc_ipc_instance.send(1, {
                            "cmd": "AUTHORIZE", 
                            "args": {
                                "client_id": disc_ipc_instance.client_id, 
                                "scopes": ["rpc", "rpc.voice.read", "rpc.voice.write"]
                            }, 
                            "nonce": str(uuid.uuid4())
                        })

                elif action == 'disc_mute':
                    if disc_ipc_instance and disc_ipc_instance.connected and getattr(disc_ipc_instance, "voice_supported", False):
                        is_deaf = disc_ipc_instance.voice_state.get("deaf", False)
                        is_mute = disc_ipc_instance.voice_state.get("mute", False)
                        
                        if is_deaf:
                            disc_ipc_instance.set_voice(deaf=False, mute=True)
                            disc_ipc_instance.pre_deafen_mute = True
                        else:
                            disc_ipc_instance.set_voice(mute=not is_mute)
                            disc_ipc_instance.pre_deafen_mute = not is_mute
                    else:
                        try:
                            import evdev
                            await asyncio.to_thread(MacroSystem.send_keys, evdev.ecodes.KEY_LEFTCTRL, evdev.ecodes.KEY_LEFTSHIFT, evdev.ecodes.KEY_M)
                        except: pass
                
                elif action == 'disc_deaf':
                    if disc_ipc_instance and disc_ipc_instance.connected and getattr(disc_ipc_instance, "voice_supported", False):
                        is_deaf = disc_ipc_instance.voice_state.get("deaf", False)
                        is_mute = disc_ipc_instance.voice_state.get("mute", False)
                        
                        if not is_deaf:
                            disc_ipc_instance.pre_deafen_mute = is_mute
                            disc_ipc_instance.set_voice(deaf=True, mute=True)
                        else:
                            restore_mute = getattr(disc_ipc_instance, "pre_deafen_mute", False)
                            disc_ipc_instance.set_voice(deaf=False, mute=restore_mute)
                    else:
                        try:
                            import evdev
                            await asyncio.to_thread(MacroSystem.send_keys, evdev.ecodes.KEY_LEFTCTRL, evdev.ecodes.KEY_LEFTSHIFT, evdev.ecodes.KEY_D)
                        except: pass
                
                elif action == 'disc_cam':
                    pass
                
                elif action == 'disc_screen':
                    pass
                elif action == 'app_term':
                    cmd = None
                    if os.name == 'nt':
                        if shutil.which('wt.exe'):
                            cmd = ['wt.exe']
                        else:
                            cmd = ['cmd.exe', '/c', 'start', 'cmd.exe']
                    elif sys.platform == 'darwin':
                        cmd = ['open', '-a', 'Terminal']
                    else:
                        if 'TERMINAL' in os.environ and shutil.which(os.environ['TERMINAL']):
                            cmd = [os.environ['TERMINAL']]
                        else:
                            de = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
                            
                            if 'kde' in de:
                                for kread in ['kreadconfig6', 'kreadconfig5']:
                                    if shutil.which(kread):
                                        try:
                                            kde_term = subprocess.check_output([kread, '--file', 'kdeglobals', '--group', 'General', '--key', 'TerminalApplication']).decode().strip()
                                            if kde_term and shutil.which(kde_term):
                                                cmd = [kde_term]
                                                break
                                        except: pass
                                        
                            elif 'gnome' in de or 'cinnamon' in de or 'mate' in de:
                                if shutil.which('gsettings'):
                                    schema = 'org.gnome.desktop.default-applications.terminal'
                                    if 'cinnamon' in de: schema = 'org.cinnamon.desktop.default-applications.terminal'
                                    elif 'mate' in de: schema = 'org.mate.applications-terminal'
                                    try:
                                        term_exec = subprocess.check_output(['gsettings', 'get', schema, 'exec']).decode().strip().strip("'\"")
                                        if term_exec and shutil.which(term_exec):
                                            cmd = [term_exec]
                                    except: pass
                                    
                            elif 'xfce' in de:
                                if shutil.which('exo-open'):
                                    cmd = ['exo-open', '--launch', 'TerminalEmulator']
                                
                            if not cmd:
                                for wrapper in ['xdg-terminal-exec', 'xdg-terminal', 'i3-sensible-terminal']:
                                    if shutil.which(wrapper):
                                        cmd = [wrapper]
                                        break
                                
                            if not cmd:
                                term_list = ['x-terminal-emulator', 'gnome-terminal', 'konsole', 'xfce4-terminal', 'mate-terminal', 'lxterminal', 'alacritty', 'kitty', 'wezterm', 'terminator', 'urxvt', 'xterm']
                                for t in term_list:
                                    if shutil.which(t):
                                        cmd = [t]
                                        break
                    if cmd:
                        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=(os.name != 'nt'))
                elif action == 'app_web':
                    cmd = None
                    if os.name == 'nt':
                        cmd = ['cmd.exe', '/c', 'start', 'http://']
                    elif sys.platform == 'darwin':
                        cmd = ['open', 'http://']
                    else:
                        de = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
                        if 'BROWSER' in os.environ and shutil.which(os.environ['BROWSER']):
                            cmd = [os.environ['BROWSER']]
                        else:
                            if 'kde' in de:
                                for kread in ['kreadconfig6', 'kreadconfig5']:
                                    if shutil.which(kread):
                                        try:
                                            kde_browser = subprocess.check_output([kread, '--file', 'kdeglobals', '--group', 'General', '--key', 'BrowserApplication']).decode().strip()
                                            if kde_browser:
                                                bin_name = kde_browser.replace('.desktop', '')
                                                if bin_name.startswith('!'): bin_name = bin_name[1:]
                                                if bin_name == 'brave-browser': bin_name = 'brave'
                                                if shutil.which(bin_name):
                                                    cmd = [bin_name]
                                                    break
                                        except: pass
                            
                            if not cmd and shutil.which('xdg-settings'):
                                try:
                                    browser_desktop = subprocess.check_output(['xdg-settings', 'get', 'default-web-browser']).decode().strip()
                                    if browser_desktop:
                                        bin_name = browser_desktop.replace('.desktop', '')
                                        if bin_name == 'brave-browser': bin_name = 'brave'
                                        if shutil.which(bin_name):
                                            cmd = [bin_name]
                                except: pass
                                
                            if not cmd and shutil.which('xdg-open'):
                                cmd = ['xdg-open', 'http://']
                                
                            if not cmd:
                                browser_list = ['x-www-browser', 'firefox', 'brave', 'google-chrome', 'chromium', 'vivaldi', 'opera', 'epiphany', 'midori']
                                for b in browser_list:
                                    if shutil.which(b):
                                        cmd = [b]
                                        break
                    if cmd:
                        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=(os.name != 'nt'))
                elif action == 'app_task':
                    cmd = None
                    if os.name == 'nt':
                        cmd = ['taskmgr.exe']
                    elif sys.platform == 'darwin':
                        cmd = ['open', '-a', 'Activity Monitor']
                    else:
                        de = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
                        if 'kde' in de:
                            task_list = ['plasma-systemmonitor', 'ksysguard']
                        elif 'gnome' in de or 'cinnamon' in de or 'mate' in de:
                            task_list = ['gnome-system-monitor', 'mate-system-monitor']
                        elif 'xfce' in de:
                            task_list = ['xfce4-taskmanager']
                        else:
                            task_list = ['plasma-systemmonitor', 'gnome-system-monitor', 'xfce4-taskmanager', 'ksysguard', 'htop', 'top']
                            
                        for t in task_list:
                            if shutil.which(t):
                                if t in ['htop', 'top']:
                                    # Need a terminal to run CLI task managers
                                    if shutil.which('x-terminal-emulator'):
                                        cmd = ['x-terminal-emulator', '-e', t]
                                    else:
                                        cmd = ['xterm', '-e', t]
                                else:
                                    cmd = [t]
                                break
                    if cmd:
                        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=(os.name != 'nt'))
                elif action == 'app_clip': await asyncio.to_thread(MacroSystem.send_keys, 119)
                elif action == 'app_soundpad': 
                    if os.name == 'nt':
                        sp_path = r"C:\Program Files\Soundpad\Soundpad.exe"
                        if not os.path.exists(sp_path):
                            sp_path = r"C:\Program Files (x86)\Steam\steamapps\common\Soundpad\Soundpad.exe"
                        if os.path.exists(sp_path):
                            subprocess.Popen([sp_path])
                            
                elif action == 'audio_cycle': await asyncio.to_thread(AudioSystem.cycle_device)
                elif action == 'audio_mute_spk': await asyncio.to_thread(AudioSystem.toggle_mute, '@DEFAULT_AUDIO_SINK@')
                elif action == 'audio_mute_mic': await asyncio.to_thread(AudioSystem.toggle_mute, '@DEFAULT_AUDIO_SOURCE@')

            elif msg_type == 'set_volume':
                target = '@DEFAULT_AUDIO_SINK@' if data['type'] == 'speaker' else '@DEFAULT_AUDIO_SOURCE@'
                await asyncio.to_thread(AudioSystem.set_vol, target, data['val'])

            elif msg_type == 'run_speedtest':
                async def run_st():
                    if speedtest_lock.locked():
                        await ws_manager.broadcast({"type": "speedtest_result", "data": {'down': 'BUSY', 'up': 'BUSY'}})
                        return
                    async with speedtest_lock:
                        try:
                            st = await asyncio.to_thread(speedtest.Speedtest)
                            await asyncio.to_thread(st.get_best_server)
                            down, up = await asyncio.to_thread(st.download), await asyncio.to_thread(st.upload)
                            await ws_manager.broadcast({"type": "speedtest_result", "data": {'down': round(down / 1_000_000, 1), 'up': round(up / 1_000_000, 1)}})
                        except: await ws_manager.broadcast({"type": "speedtest_result", "data": {'down': 'ERR', 'up': 'ERR'}})
                asyncio.create_task(run_st())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket handler error: {e}")
    finally:
        ws_manager.disconnect(websocket)

def _is_lan_ipv4(ip_addr):
    try:
        ip = ipaddress.ip_address(ip_addr)
    except ValueError:
        return False
    return (
        ip.version == 4
        and not ip.is_loopback
        and not ip.is_unspecified
        and not ip.is_link_local
        and not ip.is_multicast
    )


def _pick_lan_ipv4(candidates):
    seen = set()
    valid = []
    for ip_addr in candidates:
        if not ip_addr or ip_addr in seen or not _is_lan_ipv4(ip_addr):
            continue
        seen.add(ip_addr)
        valid.append(ip_addr)

    private_ips = [ip_addr for ip_addr in valid if ipaddress.ip_address(ip_addr).is_private]
    if private_ips:
        return private_ips[0]
    return valid[0] if valid else None


def get_lan_ip():
    candidates = []

    for target in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 80))
                candidates.append(sock.getsockname()[0])
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
        hostname = socket.gethostname()
        candidates.extend(socket.gethostbyname_ex(hostname)[2])
    except Exception:
        pass

    return _pick_lan_ipv4(candidates) or "LAN unavailable"


def stop_fastapi_server():
    if uvicorn_server is not None:
        uvicorn_server.should_exit = True


def run_fastapi_server(host='0.0.0.0', port=5000, reload=False):
    global uvicorn_server
    if reload:
        uvicorn.run(
            "server:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=[BASE_DIR, os.path.join(RESOURCE_DIR, "templates")],
        )
    else:
        config_obj = uvicorn.Config(app, host=host, port=port, reload=False, log_level="info")
        uvicorn_server = uvicorn.Server(config_obj)
        uvicorn_server.run()


_WINDOW_ICON_HANDLES = []


def configure_windows_app_identity():
    if not sys.platform.startswith("win"):
        return

    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_USER_MODEL_ID)
    except Exception as e:
        logger.debug(f"Could not set Windows app identity: {e}")


def _int_handle(value):
    if value is None:
        return None
    if hasattr(value, "ToInt64"):
        value = value.ToInt64()
    elif hasattr(value, "ToInt32"):
        value = value.ToInt32()
    if hasattr(value, "value"):
        value = value.value
    try:
        handle = int(value)
    except (TypeError, ValueError):
        return None
    return handle or None


def _safe_getattr(obj, attr):
    try:
        return getattr(obj, attr, None)
    except Exception:
        return None


def _window_handle_candidates(window):
    handles = []

    def add_handle(value):
        handle = _int_handle(value)
        if handle and handle not in handles:
            handles.append(handle)

    for obj in (window, _safe_getattr(window, "native"), _safe_getattr(window, "gui")):
        if obj is None:
            continue
        add_handle(obj)
        for attr in ("hwnd", "handle", "Handle"):
            add_handle(_safe_getattr(obj, attr))

    return handles


def _find_windows_process_handles(title):
    if not sys.platform.startswith("win"):
        return []

    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    user32 = ctypes.windll.user32
    current_pid = os.getpid()
    handles = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != current_pid:
            return True

        if title:
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value != title:
                return True

        handle = _int_handle(hwnd)
        if handle and handle not in handles:
            handles.append(handle)
        return True

    try:
        user32.EnumWindows(enum_proc, 0)
    except Exception:
        return []
    return handles


def set_windows_window_icon(window, title="Touch Dashboard"):
    if not sys.platform.startswith("win") or not os.path.exists(APP_ICON_PATH):
        return

    try:
        import ctypes
        from ctypes import wintypes
    except Exception as e:
        logger.debug(f"Could not load Windows icon APIs: {e}")
        return

    user32 = ctypes.windll.user32
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1
    ICON_SMALL2 = 2
    SM_CXICON = 11
    SM_CYICON = 12
    SM_CXSMICON = 49
    SM_CYSMICON = 50

    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = wintypes.LPARAM

    large_icon = _int_handle(
        user32.LoadImageW(
            None,
            APP_ICON_PATH,
            IMAGE_ICON,
            user32.GetSystemMetrics(SM_CXICON),
            user32.GetSystemMetrics(SM_CYICON),
            LR_LOADFROMFILE,
        )
    )
    small_icon = _int_handle(
        user32.LoadImageW(
            None,
            APP_ICON_PATH,
            IMAGE_ICON,
            user32.GetSystemMetrics(SM_CXSMICON),
            user32.GetSystemMetrics(SM_CYSMICON),
            LR_LOADFROMFILE,
        )
    )

    if not large_icon and not small_icon:
        logger.debug(f"Could not load Windows app icon from {APP_ICON_PATH}")
        return

    handles = _window_handle_candidates(window)
    if not handles:
        handles = _find_windows_process_handles(title)

    if not handles:
        logger.debug("Could not find a native Windows handle for the dashboard window")
        return

    applied = False
    for hwnd in handles:
        if large_icon:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, large_icon)
            applied = True
        if small_icon:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small_icon)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL2, small_icon)
            applied = True

    if applied:
        _WINDOW_ICON_HANDLES.extend(handle for handle in (large_icon, small_icon) if handle)


class DesktopTrayApp:
    def __init__(self, port=5000):
        self.port = port
        self.local_ip = get_lan_ip()
        self.window_title = f"Touch Dashboard - {self.local_ip}"
        self.window = None
        self.icon = None
        self.window_visible = False
        self.shutting_down = False
        self.tray_available = False
        self.lock = threading.RLock()

    def make_icon_image(self):
        try:
            if os.path.exists(APP_ICON_PATH):
                with Image.open(APP_ICON_PATH) as icon:
                    icon.load()
                    icon = icon.convert("RGBA")
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
                icon.thumbnail((64, 64), resample)
                image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                image.alpha_composite(icon, ((64 - icon.width) // 2, (64 - icon.height) // 2))
                return image
        except Exception as e:
            logger.debug(f"Could not load tray icon from favicon: {e}")

        image = Image.new("RGBA", (64, 64), (9, 14, 23, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((10, 10, 54, 54), radius=12, fill=(14, 165, 233, 255))
        draw.rounded_rectangle((18, 18, 46, 46), radius=7, fill=(15, 23, 42, 255))
        draw.rectangle((24, 24, 40, 30), fill=(248, 250, 252, 255))
        draw.rectangle((24, 34, 40, 40), fill=(248, 250, 252, 255))
        return image

    def build_menu(self):
        return pystray.Menu(
            pystray.MenuItem(self._toggle_label, self.toggle_window, default=True),
            pystray.MenuItem(f"Local IP: {self.local_ip}", lambda icon, item: None, enabled=False),
            pystray.MenuItem("Quit", self.quit),
        )

    def _toggle_label(self, item):
        return "Hide Dashboard" if self.window_visible else "Show Dashboard"

    def start_tray(self):
        self.icon = pystray.Icon("Touch Dashboard", self.make_icon_image(), "Touch Dashboard", self.build_menu())
        try:
            self.icon.run_detached()
            self.tray_available = True
        except Exception as e:
            self.tray_available = False
            logger.error(f"Tray startup failed; showing dashboard window directly after GUI starts: {e}")

    def on_webview_ready(self):
        set_windows_window_icon(self.window, self.window_title)
        if self.tray_available:
            self.hide_window()
        else:
            self.show_window()

    def attach_window(self, window):
        self.window = window
        try:
            self.window.events.closing += self.on_window_closing
        except Exception as e:
            logger.debug(f"Could not attach window close handler: {e}")

    def on_window_closing(self):
        if self.shutting_down:
            return True
        self.hide_window()
        return False

    def toggle_window(self, icon=None, item=None):
        with self.lock:
            if self.window_visible:
                self.hide_window()
            else:
                self.show_window()
            if self.icon:
                self.icon.update_menu()

    def show_window(self):
        if not self.window:
            return
        try:
            self.window.show()
            self.window.restore()
        except Exception:
            try:
                self.window.show()
            except Exception as e:
                logger.error(f"Failed to show dashboard window: {e}")
                return
        self.window_visible = True

    def hide_window(self):
        if not self.window:
            return
        try:
            self.window.hide()
            self.window_visible = False
        except Exception as e:
            logger.error(f"Failed to hide dashboard window: {e}")

    def quit(self, icon=None, item=None):
        with self.lock:
            self.shutting_down = True
            stop_fastapi_server()
            if self.icon:
                self.icon.stop()
            if self.window:
                try:
                    self.window.destroy()
                except Exception as e:
                    logger.debug(f"Window destroy failed during quit: {e}")


def launch_desktop(host='0.0.0.0', port=5000):
    if webview is None or pystray is None or Image is None:
        logger.warning("pywebview, pystray, or Pillow is not installed; starting FastAPI without a tray window.")
        run_fastapi_server(host=host, port=port, reload=False)
        return

    configure_windows_app_identity()

    server_thread = threading.Thread(
        target=run_fastapi_server,
        kwargs={"host": host, "port": port, "reload": False},
        daemon=True,
        name="touch-dashboard-fastapi",
    )
    server_thread.start()
    time.sleep(1.0)

    desktop_app = DesktopTrayApp(port=port)
    window_kwargs = {
        "title": desktop_app.window_title,
        "url": f"http://127.0.0.1:{port}",
        "frameless": True,
        "width": 1280,
        "height": 800,
        "hidden": True,
    }
    try:
        window = webview.create_window(**window_kwargs)
    except TypeError:
        window_kwargs.pop("hidden", None)
        window = webview.create_window(**window_kwargs)
    desktop_app.attach_window(window)
    desktop_app.start_tray()
    if sys.platform.startswith("linux"):
        webview.start(desktop_app.on_webview_ready, gui="gtk")
    else:
        webview.start(desktop_app.on_webview_ready)
    stop_fastapi_server()


def parse_args():
    parser = argparse.ArgumentParser(description="Touch Dashboard desktop/server launcher")
    parser.add_argument("--server-only", action="store_true", help="Run FastAPI without opening a PyWebView window")
    parser.add_argument("--host", default="0.0.0.0", help="FastAPI bind host")
    parser.add_argument("--port", type=int, default=5000, help="FastAPI bind port")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload for development server-only runs")
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    if args.server_only:
        run_fastapi_server(host=args.host, port=args.port, reload=args.reload)
    else:
        launch_desktop(host=args.host, port=args.port)
