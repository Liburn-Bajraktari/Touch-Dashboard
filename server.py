import gevent.monkey
gevent.monkey.patch_all()

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
from spotipy.oauth2 import SpotifyOAuth
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

# --- CONFIGURATION MANAGER ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
WEATHER_CACHE_FILE = os.path.join(BASE_DIR, "weather_cache.json")
SPOTIFY_CACHE_FILE = os.path.join(BASE_DIR, ".cache")

# Setup Logging
logging.basicConfig(
    filename=os.path.join(BASE_DIR, 'server.log'),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.info("Starting Touch Dashboard Server...")

DEFAULT_CONFIG = {
    "weather_api": "", "weather_city": "Pristina",
    "spot_id": "", "spot_secret": "",
    "disc_id": "", "disc_secret": "",
    "audio_names": {},
    "soundpad_buttons": []
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f: return {**DEFAULT_CONFIG, **json.load(f)}
        except: return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config():
    with open(CONFIG_FILE, "w") as f: json.dump(config, f, indent=4)

config = load_config()

# --- AUDIO SYSTEM (PipeWire/WirePlumber Wrapper) ---
class AudioSystem:
    @staticmethod
    def run(cmd):
        try: return subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        except Exception as e:
            logger.debug(f"AudioSystem.run failed for cmd {cmd}: {e}")
            return ""

    @staticmethod
    def get_state(target):
        out = AudioSystem.run(['wpctl', 'get-volume', target])
        if not out:
            logger.warning(f"wpctl get-volume returned empty for {target}. Defaulting to 0% [MUTED]")
            return {"vol": 0, "muted": True}
        try:
            vol_str = out.split()[1]
            vol = int(float(vol_str) * 100)
        except Exception as e:
            logger.error(f"Failed to parse volume output '{out}' for {target}: {e}")
            vol = 0
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
                        is_active = bool(match.group(1))
                        dev_id = match.group(2)
                        raw_name = match.group(3).strip()
                        
                        custom_names = config.get("audio_names", {})
                        custom_name = custom_names.get(raw_name, "")
                        display_name = custom_name[:10] if custom_name else raw_name[:5].upper()
                        sinks.append({"id": dev_id, "name": display_name, "raw_name": raw_name, "custom_name": custom_name, "is_active": is_active})
                        if is_active:
                            active_sink_name = display_name
        except Exception as e:
            logger.error(f"Audio parse error: {e}")
        
        return {
            "sinks": sinks,
            "active_sink_name": active_sink_name if sinks else "NONE",
            "spk": AudioSystem.get_state('@DEFAULT_AUDIO_SINK@'),
            "mic": AudioSystem.get_state('@DEFAULT_AUDIO_SOURCE@')
        }

    @staticmethod
    def get_hardware_sinks():
        return AudioSystem.poll_all()['sinks']

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
        self.voice_state = {"mute": False, "deaf": False}
        self.running = False
        self.warned_local = False

    def get_pipe_path(self):
        if os.name == 'nt': return r'\\.\pipe\discord-ipc-0'
        uid = os.getuid() if hasattr(os, 'getuid') else 1000
        for i in range(10):
            path = f"/run/user/{uid}/discord-ipc-{i}"
            if os.path.exists(path): return path
            path = f"/tmp/discord-ipc-{i}"
            if os.path.exists(path): return path
        return None

    def connect(self):
        pipe_path = self.get_pipe_path()
        if not pipe_path:
            if not self.warned_local:
                logger.warning("Discord IPC pipe not found. Ensure Discord is running locally on the same machine/OS as this Python script!")
                self.warned_local = True
            return False
        try:
            if os.name == 'nt':
                self.sock = open(pipe_path, 'w+b')
            else:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(pipe_path)
                self.sock.settimeout(2.0)
            
            logger.info("Connected to local Discord IPC socket.")
            self.send(0, {"v": 1, "client_id": self.client_id})
            self.recv() # Handshake response
            
            if not self.access_token: self.authorize()
            else: self.authenticate()
            
            self.connected = True
            return True
        except Exception as e:
            logger.error(f"Discord IPC Connection Error: {e}")
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
        else: self.sock.sendall(data)

    def sock_recv(self, length):
        if os.name == 'nt':
            data = b""
            while len(data) < length:
                chunk = self.sock.read(length - len(data))
                if not chunk: return b""
                data += chunk
            return data
        else:
            data = b""
            while len(data) < length:
                chunk = self.sock.recv(length - len(data))
                if not chunk: return b""
                data += chunk
            return data

    def send(self, opcode, payload):
        data = json.dumps(payload).encode('utf-8')
        header = struct.pack("<II", opcode, len(data))
        try: self.sock_send(header + data)
        except Exception as e:
            logger.error(f"Discord IPC send failed: {e}")
            self.connected = False

    def recv(self):
        try:
            header = self.sock_recv(8)
            if len(header) < 8: return {}
            opcode, length = struct.unpack("<II", header)
            data = self.sock_recv(length)
            return json.loads(data.decode('utf-8'))
        except socket.timeout: return None
        except Exception as e:
            logger.debug(f"Discord IPC recv empty/error: {e}")
            return {}

    def authorize(self):
        logger.info("Requesting Discord Authorization...")
        nonce = str(uuid.uuid4())
        self.send(1, {"cmd": "AUTHORIZE", "args": {"client_id": self.client_id, "scopes": ["rpc", "rpc.voice.read", "rpc.voice.write"]}, "nonce": nonce})
        while True:
            res = self.recv()
            if res is None: continue
            if not res: break
            if res.get("cmd") == "AUTHORIZE" and res.get("nonce") == nonce:
                code = res.get("data", {}).get("code")
                if code: self.exchange_code(code)
                break

    def exchange_code(self, code):
        data = {"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "authorization_code", "code": code, "redirect_uri": "http://127.0.0.1"}
        try:
            r = requests.post("https://discord.com/api/oauth2/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=5)
            if r.status_code == 200:
                self.access_token = r.json().get("access_token")
                config["disc_token"] = self.access_token
                save_config()
                logger.info("Successfully fetched and saved Discord access token.")
                self.authenticate()
            else:
                logger.error(f"Failed to fetch Discord token: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"Discord Token HTTP Error: {e}")

    def authenticate(self):
        logger.info("Authenticating with Discord IPC...")
        nonce = str(uuid.uuid4())
        self.send(1, {"cmd": "AUTHENTICATE", "args": {"access_token": self.access_token}, "nonce": nonce})
        self.recv()
        self.send(1, {"cmd": "SUBSCRIBE", "evt": "VOICE_SETTINGS_UPDATE", "nonce": str(uuid.uuid4())})
        self.send(1, {"cmd": "GET_VOICE_SETTINGS", "nonce": "GET_VOICE"})

    def loop(self):
        self.running = True
        while self.running:
            if not self.connected:
                if not self.connect():
                    socketio.sleep(5)
                    continue
            try:
                if os.name != 'nt': self.sock.settimeout(5.0)
                res = self.recv()
                if res is None: continue 
                if not res: 
                    self.close()
                    continue
                evt = res.get("evt")
                if evt == "VOICE_SETTINGS_UPDATE" or res.get("nonce") == "GET_VOICE":
                    data = res.get("data", {})
                    self.voice_state["mute"] = data.get("mute", False)
                    self.voice_state["deaf"] = data.get("deaf", False)
            except Exception as e:
                logger.debug(f"Discord IPC Loop exception: {e}")
                self.close()
                socketio.sleep(2)

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
        socketio.start_background_task(disc_ipc_instance.loop)

# --- APP INITIALIZATION ---
app = Flask(__name__)
app.logger.disabled = True
logging.getLogger('werkzeug').disabled = True
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')
spotify_cache = None
last_spotify_check = 0
last_audio_devs = []

# --- MEDIA ENGINES ---
def get_spotify_api_meta():
    if not config.get("spot_id") or not config.get("spot_secret"): return None
    try:
        sp_oauth = SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri="http://127.0.0.1:5000/callback", open_browser=False, cache_path=SPOTIFY_CACHE_FILE)
        token_info = sp_oauth.get_cached_token()
        if not token_info: return None # Fail silently to prevent terminal input blocking
        
        sp = spotipy.Spotify(auth=token_info['access_token'])
        curr = sp.current_playback()
        if curr and curr.get('is_playing'): return {"status": "Playing", "artist": curr['item']['artists'][0]['name'], "title": curr['item']['name']}
        elif curr: return {"status": "Paused", "artist": curr['item']['artists'][0]['name'], "title": curr['item']['name']}
    except Exception as e:
        logger.debug(f"Spotify API error: {e}")
    return None

def get_local_mpris_meta():
    try:
        meta = AudioSystem.run(['playerctl', 'metadata', '--format', '{{status}}|||{{artist}}|||{{title}}'])
        if not meta: return {"status": "Stopped", "artist": "", "title": "Nothing Playing"}
        parts = meta.split('|||')
        status = parts[0].strip() if len(parts) > 0 else "Stopped"
        if status not in ['Playing', 'Paused']: return {"status": "Stopped", "artist": "", "title": "Nothing Playing"}
        
        artist = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Unknown"
        title = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "Unknown"
        return {"status": status, "artist": artist, "title": title}
    except: return {"status": "Stopped", "artist": "", "title": "Nothing Playing"}

# --- CORE SYSTEM LOOP ---
def hardware_loop():
    global last_spotify_check, spotify_cache, last_audio_devs
    last_gpu_check = 0
    gpu_cache = ""
    logger.info("Hardware monitoring loop started.")
    while True:
        curr_time = time.time()
        
        # Audio Device Auto-Scan & State Polling
        audio_data = AudioSystem.poll_all()
        curr_sinks = audio_data['sinks']
        curr_names = [s['raw_name'] for s in curr_sinks]
        last_names = [s['raw_name'] for s in last_audio_devs]
        
        if curr_names != last_names:
            last_audio_devs = curr_sinks
            socketio.emit('hw_scan_results', curr_sinks)

        # Spotify Polling (3s limit)
        if curr_time - last_spotify_check > 3.0:
            spotify_cache = get_spotify_api_meta()
            last_spotify_check = curr_time

        media = spotify_cache
        if not media or media['status'] != 'Playing':
            local_media = get_local_mpris_meta()
            if local_media['status'] == 'Playing' or not media: media = local_media

        # GPU Polling (2s limit, heavy process)
        if curr_time - last_gpu_check > 2.0:
            try: gpu_cache = AudioSystem.run(['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'])
            except: gpu_cache = ""
            last_gpu_check = curr_time

        data = {
            "cpu": psutil.cpu_percent(interval=None),
            "ram": psutil.virtual_memory().percent,
            "gpu": gpu_cache if gpu_cache else None,
            "spotify": media,
            "discord": disc_ipc_instance.voice_state if disc_ipc_instance and disc_ipc_instance.connected else {"mute": False, "deaf": False},
            "audio": {
                "spk": audio_data['spk'],
                "mic": audio_data['mic'],
                "active_dev": audio_data['active_sink_name']
            }
        }
        
        try:
            with open("/tmp/g502_battery.txt", "r") as f: data["mouse_batt"] = f.read().strip()
        except: data["mouse_batt"] = "--"

        socketio.emit('sys_data', data)
        socketio.sleep(0.5)

WEATHER_CACHE_FILE = os.path.join(BASE_DIR, "weather_cache.json")

def load_weather_cache():
    if os.path.exists(WEATHER_CACHE_FILE):
        try:
            with open(WEATHER_CACHE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"temp": "--", "desc": "--", "timestamp": 0}

def save_weather_cache(data):
    try:
        with open(WEATHER_CACHE_FILE, "w") as f: json.dump(data, f)
    except: pass

last_weather_data = load_weather_cache()
weather_force_update = False

def do_fetch_weather():
    global last_weather_data
    if config.get("weather_api") and config.get("weather_city"):
        try:
            logger.info(f"Fetching weather for {config['weather_city']}...")
            url = "http://api.openweathermap.org/data/2.5/weather"
            params = {
                "q": config['weather_city'],
                "appid": config['weather_api'],
                "units": "metric"
            }
            res = requests.get(url, params=params, timeout=5).json()
            if "main" in res: 
                last_weather_data = {
                    "temp": round(res["main"]["temp"]), 
                    "desc": res["weather"][0]["description"].title(),
                    "timestamp": time.time()
                }
                save_weather_cache(last_weather_data)
                socketio.emit('weather_data', last_weather_data)
                logger.info("Weather updated successfully.")
            else:
                logger.warning(f"Weather API returned unexpected data: {res}")
                last_weather_data["timestamp"] = time.time() # Prevent spamming on bad API key
        except Exception as e:
            logger.error(f"Weather fetch error: {e}")
            last_weather_data["timestamp"] = time.time() # Prevent spamming on network error
    else:
        last_weather_data["timestamp"] = time.time() # Prevent spamming when not configured

def fetch_weather():
    global last_weather_data, weather_force_update
    if time.time() - last_weather_data.get("timestamp", 0) > 1800:
        do_fetch_weather()

    while True:
        # Sleep cooperatively for up to 30 mins, but check flag every 1s
        for _ in range(1800):
            if weather_force_update:
                weather_force_update = False
                break
            socketio.sleep(1)
        do_fetch_weather()

# --- ROUTES & SOCKETS ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Command Center", "short_name": "Dash", "display": "fullscreen", "orientation": "landscape",
        "background_color": "#090e17", "theme_color": "#090e17",
        "icons": [{"src": "https://upload.wikimedia.org/wikipedia/commons/4/49/A_black_image.jpg", "sizes": "192x192", "type": "image/jpeg"}]
    })

@socketio.on('connect')
def handle_connect():
    socketio.emit('config_sync', {"cfg": config, "hw": AudioSystem.get_hardware_sinks()})
    if last_weather_data.get("temp") != "--":
        socketio.emit('weather_data', last_weather_data)

@socketio.on('save_config')
def handle_config_save(data):
    global config, weather_force_update
    old_id = config.get("disc_id")
    old_secret = config.get("disc_secret")
    old_weather_api = config.get("weather_api")
    old_weather_city = config.get("weather_city")
    config.update(data)
    save_config()
    logger.info("Configuration updated via frontend settings.")
    
    if old_id != config.get("disc_id") or old_secret != config.get("disc_secret"):
        restart_discord_ipc()
    if old_weather_api != config.get("weather_api") or old_weather_city != config.get("weather_city"):
        weather_force_update = True

@socketio.on('action')
def handle_action(action):
    if action.startswith('spot_'):
        sp_success = False
        if config.get("spot_id"):
            try:
                sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri="http://127.0.0.1:5000/callback", open_browser=False))
                if action == 'spot_play':
                    c = sp.current_playback()
                    if c and c['is_playing']: sp.pause_playback()
                    else: sp.start_playback()
                elif action == 'spot_next': sp.next_track()
                elif action == 'spot_prev': sp.previous_track()
                sp_success = True
            except Exception as e:
                logger.error(f"Spotify action error: {e}")
        if not sp_success:
            if action == 'spot_play': AudioSystem.run(['playerctl', 'play-pause'])
            elif action == 'spot_next': AudioSystem.run(['playerctl', 'next'])
            elif action == 'spot_prev': AudioSystem.run(['playerctl', 'previous'])

    elif action.startswith('sp_play_'):
        sp_id = action.split('sp_play_')[1]
        AudioSystem.run(['soundux', '--play', sp_id])

    elif action == 'disc_mute':
        if disc_ipc_instance and disc_ipc_instance.connected: disc_ipc_instance.set_voice(mute=not disc_ipc_instance.voice_state["mute"])
        else: AudioSystem.run(['ydotool', 'key', '29:1', '42:1', '50:1', '50:0', '42:0', '29:0']) 
    elif action == 'disc_deaf':
        if disc_ipc_instance and disc_ipc_instance.connected: disc_ipc_instance.set_voice(deaf=not disc_ipc_instance.voice_state["deaf"])
        else: AudioSystem.run(['ydotool', 'key', '29:1', '42:1', '32:1', '32:0', '42:0', '29:0'])
    elif action == 'disc_cam': pass
    elif action == 'disc_screen': pass
    elif action == 'app_term': AudioSystem.run(['alacritty']) 
    elif action == 'app_web': AudioSystem.run(['brave'])
    elif action == 'app_task': AudioSystem.run(['gnome-system-monitor']) 
    elif action == 'app_clip': AudioSystem.run(['ydotool', 'key', '119:1', '119:0'])
    elif action == 'app_soundpad': AudioSystem.run(['soundux']) 
    elif action == 'audio_cycle': AudioSystem.cycle_device()
    elif action == 'audio_mute_spk': AudioSystem.toggle_mute('@DEFAULT_AUDIO_SINK@')
    elif action == 'audio_mute_mic': AudioSystem.toggle_mute('@DEFAULT_AUDIO_SOURCE@')

@socketio.on('set_volume')
def handle_volume(data):
    target = '@DEFAULT_AUDIO_SINK@' if data['type'] == 'speaker' else '@DEFAULT_AUDIO_SOURCE@'
    AudioSystem.set_vol(target, data['val'])

@socketio.on('run_speedtest')
def handle_speedtest():
    def test():
        try:
            logger.info("Running speedtest...")
            st = speedtest.Speedtest()
            st.get_best_server()
            res = {'down': round(st.download() / 1_000_000, 1), 'up': round(st.upload() / 1_000_000, 1)}
            socketio.emit('speedtest_result', res)
            logger.info(f"Speedtest complete: {res}")
        except Exception as e:
            logger.error(f"Speedtest failed: {e}")
            socketio.emit('speedtest_result', {'down': 'ERR', 'up': 'ERR'})
    socketio.start_background_task(test)

if __name__ == '__main__':
    logger.info("Server starting on 0.0.0.0:5000")
    restart_discord_ipc()
    socketio.start_background_task(hardware_loop)
    socketio.start_background_task(fetch_weather)
    socketio.run(app, host='0.0.0.0', port=5000)
