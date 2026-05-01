import asyncio
import os
import json
import time
import logging
import threading
import subprocess
import requests
import psutil
import speedtest
import spotipy
import re
import socket
import struct
import uuid
import base64
import urllib.parse
from spotipy.oauth2 import SpotifyOAuth
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, HTMLResponse
from contextlib import asynccontextmanager
import uvicorn
from fastapi.staticfiles import StaticFiles 
import pynvml
import hashlib

# --- CONFIGURATION MANAGER ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
WEATHER_CACHE_FILE = os.path.join(BASE_DIR, "weather_cache.json")
SPOTIFY_CACHE_FILE = os.path.join(BASE_DIR, ".cache")
SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")

# Global Audio Process Tracker for Kill-and-Replace
current_audio_process = None

logging.basicConfig(
    filename=os.path.join(BASE_DIR, 'server.log'),
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

# Global Spotify OAuth Instance Tracker
global_sp_oauth = None

# Global NVML (GPU) Hardware Handle
has_nvml = False
nvml_handle = None
try:
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
    with open(CONFIG_FILE, "w") as f: json.dump(config, f, indent=4)

config = load_config()

# --- OS DETECTION ---
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

# --- MACRO SYSTEM (evdev fallback) ---
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
            return
        try:
            import evdev
            MacroSystem._init()
            if MacroSystem._ui:
                for k in keys:
                    MacroSystem._ui.write(evdev.ecodes.EV_KEY, k, 1)
                MacroSystem._ui.syn()
                for k in reversed(keys):
                    MacroSystem._ui.write(evdev.ecodes.EV_KEY, k, 0)
                MacroSystem._ui.syn()
        except Exception as e:
            logger.error(f"MacroSystem failed: {e}")

# --- AUDIO SYSTEM (PipeWire/WirePlumber Wrapper) ---
class AudioSystem:
    @staticmethod
    def run(cmd):
        try: return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        except Exception as e:
            logger.debug(f"AudioSystem.run failed for cmd {cmd}: {e}")
            return ""

    @staticmethod
    def get_state(target):
        out = AudioSystem.run(['wpctl', 'get-volume', target])
        if not out: return {"vol": 0, "muted": True}
        try:
            parts = out.split()
            vol = int(float(parts[1]) * 100) if len(parts) > 1 else 0
        except Exception: vol = 0
        return {"vol": vol, "muted": '[MUTED]' in out}

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
                    clean = line.replace('│', '').replace('├─', '').replace('└─', '').strip()
                    if not clean: continue
                    match = re.search(r'^(\*)?\s*(\d+)\.\s+([^\[]+)', clean)
                    if match:
                        is_active, dev_id, raw_name = bool(match.group(1)), match.group(2), match.group(3).strip()
                        
                        # --- Hide the Virtual Soundboard from the UI ---
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
    def set_vol(target, val): AudioSystem.run(['wpctl', 'set-volume', target, f"{val}%"])
    @staticmethod
    def toggle_mute(target): AudioSystem.run(['wpctl', 'set-mute', target, 'toggle'])
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

class DiscordIPC:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.sock = None
        self.access_token = config.get("disc_token", "")
        self.connected = False
        self.running = False
        self.voice_state = {"mute": False, "deaf": False}
        self.voice_supported = False # Flag if IPC supports voice settings
        self.auth_pending = False # Flag for FastAPI to check
        self.pre_deafen_mute = False # Tracks mute state before a deafen action

    def get_pipe_paths(self):
        paths_found = []
        if os.name == 'nt': 
            if os.path.exists(r'\\.\pipe\discord-ipc-0'):
                paths_found.append(r'\\.\pipe\discord-ipc-0')
            return paths_found
        
        # Linux paths
        env_vars = ['XDG_RUNTIME_DIR', 'TMPDIR', 'TMP', 'TEMP']
        paths = [os.environ.get(v) for v in env_vars if os.environ.get(v)]
        paths.extend([f"/run/user/{os.getuid()}", "/tmp"])
        
        for base_path in paths:
            for i in range(10):
                path = os.path.join(base_path, f"discord-ipc-{i}")
                # Flatpak/Snap sandboxed paths
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
                
                # Handshake
                self.send(0, {"v": 1, "client_id": self.client_id})
                res = self.recv()
                if not res:
                    self.close()
                    continue
                    
                # Skip arRPC because it intercepts IPC but doesn't support Voice Commands
                if res.get("data", {}).get("user", {}).get("username") == "arrpc":
                    logger.info(f"Skipping {pipe_path} because it is arRPC (unsupported voice IPC).")
                    self.close()
                    continue
                
                if not self.access_token:
                    self.auth_pending = True # Signal frontend to show auth button
                    self.connected = True # Must stay connected to receive IPC AUTHORIZE
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
                # Connection closed or incomplete header
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
                config["disc_token"] = self.access_token
                save_config()
                self.auth_pending = False
                return True
            logger.error(f"Discord Token Exchange Failed: {r.text}")
        except Exception as e:
             logger.error(f"Discord Token Exception: {e}")
        return False

    def authenticate(self):
        self.send(1, {"cmd": "AUTHENTICATE", "args": {"access_token": self.access_token}, "nonce": str(uuid.uuid4())})
        
        # Wait for authentication to complete before sending dependent commands
        auth_res = self.recv()
        if auth_res and auth_res.get("evt") == "ERROR":
            logger.error(f"Discord IPC Auth Error: {auth_res}")
            self.access_token = "" # Invalidate token
            self.auth_pending = True
            return

        self.send(1, {"cmd": "SUBSCRIBE", "evt": "VOICE_SETTINGS_UPDATE", "args": {}, "nonce": str(uuid.uuid4())})
        self.send(1, {"cmd": "GET_VOICE_SETTINGS", "args": {}, "nonce": "GET_VOICE"})
        
        # Consume the two responses to update state immediately
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
            if not self.connected and not self.auth_pending:
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
                 time.sleep(2) # Sleep if auth is pending

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
    if config.get("disc_id") and config.get("disc_secret"):
        disc_ipc_instance = DiscordIPC(config["disc_id"], config["disc_secret"])
        threading.Thread(target=disc_ipc_instance.loop, daemon=True).start()

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

# --- MEDIA ENGINES ---
def get_spotify_api_meta(): # Removed host_url argument
    sp_oauth = get_sp_oauth()
    if not sp_oauth: return None
    try:
        token_info = sp_oauth.get_cached_token() # Uses the cached global object!
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

        # 1. Detect a song change and trigger Burst Mode
        if current_song != last_mpris_title:
            last_mpris_title = current_song
            mpris_burst_active = True
            mpris_burst_start = time.time()
            last_art_url = "WAITING"

        # 2. Handle Burst Mode Polling
        if mpris_burst_active:
            if raw_art_url.startswith('file://'):
                path = urllib.parse.unquote(raw_art_url.replace('file://', ''))
                # Only accept the file if it actually has data
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    last_mpris_path = path
                    mod_time = str(os.path.getmtime(path))
                    f_size = str(os.path.getsize(path))
                    song_hash = hashlib.md5((current_song + mod_time + f_size).encode()).hexdigest()
                    last_art_url = f"/api/local_art?h={song_hash}"
                    mpris_burst_active = False # Got it! Stop bursting.
                elif time.time() - mpris_burst_start > 6.0:
                    last_art_url = ""
                    mpris_burst_active = False # 6-second timeout. Give up.
            else:
                last_art_url = raw_art_url
                mpris_burst_active = False
        else:
            # 3. Failsafe: if the file updates peacefully while out of burst mode
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

# --- STATE MANAGERS & FASTAPI WEBSOCKET MANAGER ---
force_media_update, current_media_source = False, "local"
spotify_cache, last_spotify_check, last_audio_devs = None, 0, []
last_weather_data = {"temp": "--", "desc": "--", "timestamp": 0}
weather_update_event = asyncio.Event()
last_host_url = "127.0.0.1:5000"

class ConnectionManager:
    def __init__(self): self.active_connections: list[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections: self.active_connections.remove(websocket)
    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try: await connection.send_json(message)
            except: pass

ws_manager = ConnectionManager()

# --- BACKGROUND ASYNC TASKS ---
async def hardware_loop():
    global last_spotify_check, spotify_cache, last_audio_devs, force_media_update, current_media_source
    global mpris_burst_active
    last_gpu_check, gpu_cache = 0, ""
    
    last_audio_check = 0
    audio_cache = {"spk": {"vol": 0, "muted": False}, "mic": {"vol": 0, "muted": False}, "active_dev": "NONE"}
    batt_cache = "--"

    # Helper to safely read the battery file off the main thread
    def read_batt():
        try:
            with open("/tmp/g502_battery.txt", "r") as f: return f.read().strip()
        except: return "--"
        
    while True:
        curr_time = time.time()
        
        # HARDWARE POLLING (Strictly locked to 1.0s to prevent CPU spikes during media bursts!)
        if curr_time - last_audio_check >= 1.0:
            audio_data = await asyncio.to_thread(AudioSystem.poll_all)
            curr_sinks = audio_data['sinks']
            if [s['raw_name'] for s in curr_sinks] != [s['raw_name'] for s in last_audio_devs]:
                last_audio_devs = curr_sinks
                await ws_manager.broadcast({"type": "hw_scan_results", "data": curr_sinks})
            
            # Update our caches
            audio_cache = {"spk": audio_data['spk'], "mic": audio_data['mic'], "active_dev": audio_data['active_sink_name']}
            batt_cache = await asyncio.to_thread(read_batt)
            last_audio_check = curr_time

        # Spotify Polling (Now using the blazing fast cached object)
        if curr_time - last_spotify_check > 3.0 or force_media_update:
            if force_media_update: await asyncio.sleep(0.4)
            spotify_cache = await asyncio.to_thread(get_spotify_api_meta) 
            last_spotify_check = time.time()
            force_media_update = False

        # Context Routing
        media = spotify_cache
        if not media or media['status'] != 'Playing':
            local_media = await asyncio.to_thread(get_local_mpris_meta)
            if local_media['status'] == 'Playing' or not media: 
                media, current_media_source = local_media, "local"
            else: current_media_source = "spotify"
        else: current_media_source = "spotify"

        # GPU Polling (Direct RAM Read via NVML)
        if curr_time - last_gpu_check > 2.0:
            if has_nvml:
                try:
                    util = await asyncio.to_thread(pynvml.nvmlDeviceGetUtilizationRates, nvml_handle)
                    gpu_cache = str(util.gpu)
                except: gpu_cache = ""
            last_gpu_check = curr_time

        # Determine Discord state
        disc_has_token = bool(config.get("disc_token", ""))
        disc_has_creds = bool(config.get("disc_id")) and bool(config.get("disc_secret"))
        
        # Build the auth URL completely independently of the socket
        fallback_auth_url = ""
        if disc_has_creds and not disc_has_token:
            # Manually generate it so it works even if Discord is offline
            scopes = "rpc rpc.voice.read rpc.voice.write"
            redirect_uri = urllib.parse.quote("http://127.0.0.1:5000/disc_callback")
            fallback_auth_url = f"https://discord.com/api/oauth2/authorize?client_id={config['disc_id']}&redirect_uri={redirect_uri}&response_type=code&scope={scopes}"

        # Baseline state, fully decoupled from the active socket
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
            
            if disc_ipc_instance.connected:
                disc_state["mute"] = disc_ipc_instance.voice_state.get("mute", False)
                disc_state["deaf"] = disc_ipc_instance.voice_state.get("deaf", False)
            
            # Only send auth URL if we don't have a token AND the socket is waiting for one
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
        
        # --- DYNAMIC BURST SLEEP ---
        sleep_duration = 0.2 if mpris_burst_active else 1.0
        await asyncio.sleep(sleep_duration)
async def fetch_weather():
    global last_weather_data, weather_force_update
    
    async def do_fetch():
        global last_weather_data
        if config.get("weather_api") and config.get("weather_city"):
            try:
                # Push only the blocking HTTP request to the background thread
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
                    
                    # We can now safely broadcast natively in the async loop
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

async def pipewire_auto_router():
    """Injects Soundboard audio directly into applications using the microphone."""
    while True:
        try:
            # 1. Get Default Microphone name
            def_src = (await asyncio.to_thread(subprocess.check_output, ['pactl', 'get-default-source'])).decode().strip()

            # 2. Get Soundboard Monitor Ports
            pw_out = (await asyncio.to_thread(subprocess.check_output, ['pw-link', '-o'])).decode()
            sb_monitors = [p.strip() for p in pw_out.splitlines() if 'Dashboard-Soundboard' in p and 'monitor' in p]
            
            if not sb_monitors:
                await asyncio.sleep(2)
                continue
            
            sb_FL = sb_monitors[0]
            sb_FR = sb_monitors[1] if len(sb_monitors) > 1 else sb_FL

            # 3. Find Apps Capturing the Mic
            pw_links = (await asyncio.to_thread(subprocess.check_output, ['pw-link', '-l'])).decode()
            target_app_ports = []
            is_mic_capture = False
            
            for line in pw_links.splitlines():
                if not line.startswith((' ', '\t')):
                    # Did we find the default microphone?
                    is_mic_capture = (def_src in line and 'capture' in line)
                elif is_mic_capture and '|->' in line:
                    # Grab the app port connected to it
                    app_port = line.split('|->')[1].strip()
                    # Exclude the soundboard itself and the native headphone loopback
                    if 'Dashboard-Soundboard' not in app_port and 'loopback' not in app_port.lower():
                        target_app_ports.append(app_port)

            # 4. Inject Audio into Apps!
            for i, app_port in enumerate(target_app_ports):
                src = sb_FL if i % 2 == 0 else sb_FR
                # Run the link (fails silently if already linked, which is what we want)
                await asyncio.to_thread(subprocess.run, ['pw-link', src, app_port], stderr=subprocess.DEVNULL)

        except Exception as e:
            logger.debug(f"PipeWire auto-router error: {e}")
        
        await asyncio.sleep(5) 


# --- FASTAPI APP & ROUTES ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure local sounds directory exists
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    
    # Automated PipeWire Virtual Sink Setup
    if get_os_target() == "linux":
        try:
            # 1. SAVE the real physical defaults before Linux can hijack them!
            try:
                real_sink = subprocess.check_output(['pactl', 'get-default-sink']).decode().strip()
                real_src = subprocess.check_output(['pactl', 'get-default-source']).decode().strip()
            except:
                real_sink, real_src = "", ""

            # 2. Create the Virtual Sink
            sinks_output = subprocess.check_output(['pactl', 'list', 'short', 'sinks']).decode()
            if 'Dashboard-Soundboard' not in sinks_output:
                logger.info("Virtual Sink 'Dashboard-Soundboard' not found. Creating it now...")
                subprocess.run([
                    'pactl', 'load-module', 'module-null-sink', 
                    'sink_name=Dashboard-Soundboard', 
                    'sink_properties=device.description="Dashboard-Soundboard"'
                ], check=True)
            
            # 3. Force Volume to 100%
            subprocess.run(['pactl', 'set-sink-volume', 'Dashboard-Soundboard', '100%'], stderr=subprocess.DEVNULL)
            
            # 4. Native Loopback to Headphones (Let PipeWire handle it!)
            modules_output = subprocess.check_output(['pactl', 'list', 'short', 'modules']).decode()
            if 'source=Dashboard-Soundboard.monitor' not in modules_output:
                subprocess.run(['pactl', 'load-module', 'module-loopback', 'source=Dashboard-Soundboard.monitor'], check=True)
                logger.info("Native Audio Loopback established.")

            # 5. AGGRESSIVELY RESTORE the physical defaults so Discord's mic doesn't drop
            if real_sink and 'Dashboard' not in real_sink:
                subprocess.run(['pactl', 'set-default-sink', real_sink], stderr=subprocess.DEVNULL)
            if real_src and 'Dashboard' not in real_src:
                subprocess.run(['pactl', 'set-default-source', real_src], stderr=subprocess.DEVNULL)
                
        except Exception as e:
            logger.error(f"Failed to setup PipeWire virtual sink: {e}")

    restart_discord_ipc()
    asyncio.create_task(hardware_loop())
    asyncio.create_task(fetch_weather())
    
    if get_os_target() == "linux":
        asyncio.create_task(pipewire_auto_router())
        
    yield
    if disc_ipc_instance: disc_ipc_instance.close()

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

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
    
    with open(os.path.join(BASE_DIR, "templates", "index.html"), "r") as f:
        html = f.read()

    # Wait up to 3.0 seconds for the background Discord thread to establish the initial connection
    # This prevents the UI from loading without the Discord panel if the connection is just a split-second away.
    if disc_ipc_instance and not disc_ipc_instance.connected and not disc_ipc_instance.auth_pending:
        for _ in range(30):
            if disc_ipc_instance.connected or disc_ipc_instance.auth_pending:
                break
            await asyncio.sleep(0.1)

    # Pre-render Discord state to prevent layout shift / flashing
    if disc_ipc_instance and disc_ipc_instance.connected:
        html = html.replace('id="panel-discord" class="glass panel" style="display: none;', 'id="panel-discord" class="glass panel" style="display: flex;')
        if disc_ipc_instance.voice_state.get("mute", False):
            html = html.replace('class="btn" id="btn-disc-mute"', 'class="btn muted" id="btn-disc-mute"')
        if disc_ipc_instance.voice_state.get("deaf", False):
            html = html.replace('class="btn" id="btn-disc-deaf"', 'class="btn muted" id="btn-disc-deaf"')

    response = HTMLResponse(content=html)
    # FORCES WEBVIEW TO CHECK SERVER EVERY TIME
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
    
    # Process the code in a background thread to avoid blocking FastAPI
    success = await asyncio.to_thread(disc_ipc_instance.exchange_code, code)
    if success:
        logger.info("Discord authorization successful!")
    else:
        logger.error("Discord authorization failed during callback.")
        
    return RedirectResponse('/')

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global config, weather_force_update, force_media_update, current_media_source, current_audio_process, global_sp_oauth
    
    await ws_manager.connect(websocket)
            
    await websocket.send_json({"type": "config_sync", "data": {"cfg": config, "hw": AudioSystem.get_hardware_sinks(), "os_target": get_os_target()}})
    
    if last_weather_data.get("temp") != "--":
        await websocket.send_json({"type": "weather_data", "data": last_weather_data})
    
    try:
        while True:
            text = await websocket.receive_text()
            msg = json.loads(text)
            msg_type, data = msg.get("type"), msg.get("data")
            
            if msg_type == 'req_local_sounds':
                sounds = await asyncio.to_thread(get_local_sounds)
                await websocket.send_json({"type": "local_sounds_list", "data": sounds})
            
            elif msg_type == 'save_config':
                old_id, old_secret = config.get("disc_id"), config.get("disc_secret")
                old_weather_api, old_weather_city = config.get("weather_api"), config.get("weather_city")
                old_spot_id = config.get("spot_id")

                config.update(data)
                await asyncio.to_thread(save_config)

                # If Spotify credentials change, force a rebuild of the object
                if old_spot_id != config.get("spot_id"):
                    global_sp_oauth = None
                
                audio_data = await asyncio.to_thread(AudioSystem.poll_all)
                await ws_manager.broadcast({"type": "config_sync", "data": {"cfg": config, "hw": audio_data['sinks'], "os_target": get_os_target()}})
                
                if old_id != config.get("disc_id") or old_secret != config.get("disc_secret"): restart_discord_ipc()
                if old_weather_api != config.get("weather_api") or old_weather_city != config.get("weather_city"): 
                    weather_update_event.set()

            elif msg_type == 'action':
                action = data
                
                # --- NATIVE KILL-AND-REPLACE AUDIO LOGIC ---
                if action.startswith('local_play_'):
                    filename = action.split('local_play_')[1]
                    sounds_dir = config.get("sounds_path", "")
                    
                    if sounds_dir:
                        filepath = os.path.join(sounds_dir, filename)
                        
                        # Kill currently playing process if active
                        if current_audio_process is not None and current_audio_process.poll() is None:
                            current_audio_process.terminate()
                            current_audio_process.wait()
                        
                        if os.path.exists(filepath):
                            current_audio_process = subprocess.Popen(
                                ['pw-play', '--volume=1.0', '--target', 'Dashboard-Soundboard', filepath], 
                                stdout=subprocess.DEVNULL, 
                                stderr=subprocess.DEVNULL
                            )
                
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
                    config["spot_token"] = ""
                    if os.path.exists(SPOTIFY_CACHE_FILE):
                        try: os.remove(SPOTIFY_CACHE_FILE)
                        except: pass
                    save_config()

                elif action == 'disc_clear_auth':
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
                            # If deafened, clicking Mute toggles deafen OFF, but leaves mute ON
                            disc_ipc_instance.set_voice(deaf=False, mute=True)
                            disc_ipc_instance.pre_deafen_mute = True
                        else:
                            # Normal mute toggle
                            disc_ipc_instance.set_voice(mute=not is_mute)
                            disc_ipc_instance.pre_deafen_mute = not is_mute
                
                elif action == 'disc_deaf':
                    if disc_ipc_instance and disc_ipc_instance.connected and getattr(disc_ipc_instance, "voice_supported", False):
                        is_deaf = disc_ipc_instance.voice_state.get("deaf", False)
                        is_mute = disc_ipc_instance.voice_state.get("mute", False)
                        
                        if not is_deaf:
                            # Turning deafen ON: remember current mute state, set both to True
                            disc_ipc_instance.pre_deafen_mute = is_mute
                            disc_ipc_instance.set_voice(deaf=True, mute=True)
                        else:
                            # Turning deafen OFF: set deaf to False, restore mute to pre-deafen state
                            restore_mute = getattr(disc_ipc_instance, "pre_deafen_mute", False)
                            disc_ipc_instance.set_voice(deaf=False, mute=restore_mute)
                
                elif action == 'disc_cam':
                    pass # IPC does not support camera
                
                elif action == 'disc_screen':
                    pass # IPC does not support screen share
                elif action == 'app_term': subprocess.Popen(['alacritty'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=(os.name != 'nt')) 
                elif action == 'app_web': subprocess.Popen(['brave'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=(os.name != 'nt'))
                elif action == 'app_task': subprocess.Popen(['gnome-system-monitor'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=(os.name != 'nt')) 
                elif action == 'app_clip': await asyncio.to_thread(MacroSystem.send_keys, 119)
                elif action == 'app_soundpad': 
                    # Keep Soundpad launch for Windows only; Linux no longer needs a GUI launch.
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
                    try:
                        st = await asyncio.to_thread(speedtest.Speedtest)
                        await asyncio.to_thread(st.get_best_server)
                        down, up = await asyncio.to_thread(st.download), await asyncio.to_thread(st.upload)
                        await ws_manager.broadcast({"type": "speedtest_result", "data": {'down': round(down / 1_000_000, 1), 'up': round(up / 1_000_000, 1)}})
                    except: await ws_manager.broadcast({"type": "speedtest_result", "data": {'down': 'ERR', 'up': 'ERR'}})
                asyncio.create_task(run_st())

    except WebSocketDisconnect: ws_manager.disconnect(websocket)

if __name__ == '__main__':
    uvicorn.run(
        "server:app", 
        host='0.0.0.0', 
        port=5000, 
        reload=True, 
        reload_dirs=[BASE_DIR, os.path.join(BASE_DIR, "templates")]
    )