import os
import json
import time
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
CONFIG_FILE = "config.json"
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
        except: return ""

    @staticmethod
    def get_hardware_sinks():
        """Parses wpctl to auto-discover all connected audio outputs."""
        devices = []
        try:
            out = AudioSystem.run(['wpctl', 'status'])
            capture = False
            for line in out.splitlines():
                if 'Sinks:' in line: capture = True; continue
                if capture and any(x in line for x in ['Sources:', 'Filters:', 'Streams:', 'Video:']): break
                if capture:
                    clean = line.replace('│', '').replace('├─', '').replace('└─', '').strip()
                    if not clean: continue
                    match = re.search(r'^(\*)?\s*(\d+)\.\s+([^\[]+)', clean)
                    if match:
                        is_active = bool(match.group(1))
                        dev_id = match.group(2)
                        dev_name = match.group(3).strip()
                        custom_names = config.get("audio_names", {})
                        custom_name = custom_names.get(dev_name, "")
                        display_name = custom_name[:10] if custom_name else dev_name[:5].upper()
                        devices.append({"id": dev_id, "name": display_name, "raw_name": dev_name, "custom_name": custom_name, "is_active": is_active})
        except Exception as e: print(f"Audio parse error: {e}")
        return devices

    @staticmethod
    def get_active_sink_id():
        for s in AudioSystem.get_hardware_sinks():
            if s.get('is_active'): return s['id']
        return None

    @staticmethod
    def get_state(target):
        out = AudioSystem.run(['wpctl', 'get-volume', target])
        if not out: return {"vol": 0, "muted": True}
        try: vol = int(float(out.split()[1]) * 100)
        except: vol = 0
        return {"vol": vol, "muted": '[MUTED]' in out}

    @staticmethod
    def set_vol(target, val): AudioSystem.run(['wpctl', 'set-volume', target, f"{val}%"])
    
    @staticmethod
    def toggle_mute(target): AudioSystem.run(['wpctl', 'set-mute', target, 'toggle'])

    @staticmethod
    def cycle_device():
        sinks = AudioSystem.get_hardware_sinks()
        if not sinks: return
        active_id = AudioSystem.get_active_sink_id()
        next_sink = sinks[0]
        if active_id:
            for i, s in enumerate(sinks):
                if s['id'] == active_id:
                    next_sink = sinks[(i + 1) % len(sinks)]
                    break
        AudioSystem.run(['wpctl', 'set-default', next_sink['id']])

    @staticmethod
    def get_active_name():
        sinks = AudioSystem.get_hardware_sinks()
        active_id = AudioSystem.get_active_sink_id()
        for s in sinks:
            if s['id'] == active_id: return s['name']
        return sinks[0]['name'] if sinks else "NONE"

class DiscordIPC:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.sock = None
        self.access_token = config.get("disc_token", "")
        self.connected = False
        self.voice_state = {"mute": False, "deaf": False}
        self.running = False

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
        if not pipe_path: return False
        try:
            if os.name == 'nt':
                self.sock = open(pipe_path, 'w+b')
            else:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(pipe_path)
                self.sock.settimeout(2.0)
            
            self.send(0, {"v": 1, "client_id": self.client_id})
            self.recv() # Handshake response
            
            if not self.access_token: self.authorize()
            else: self.authenticate()
            
            self.connected = True
            return True
        except Exception as e:
            print(f"Discord IPC Error: {e}")
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
        except: self.connected = False

    def recv(self):
        try:
            header = self.sock_recv(8)
            if len(header) < 8: return {}
            opcode, length = struct.unpack("<II", header)
            data = self.sock_recv(length)
            return json.loads(data.decode('utf-8'))
        except socket.timeout: return None
        except: return {}

    def authorize(self):
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
                self.authenticate()
        except Exception as e: print(f"Discord Token Error: {e}")

    def authenticate(self):
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
                    time.sleep(5)
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
                self.close()
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

# --- APP INITIALIZATION ---
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')
spotify_cache = None
last_spotify_check = 0
last_audio_devs = []

# --- MEDIA ENGINES ---
def get_spotify_api_meta():
    if not config.get("spot_id") or not config.get("spot_secret"): return None
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri="http://127.0.0.1:5000/callback", open_browser=False))
        curr = sp.current_playback()
        if curr and curr.get('is_playing'): return {"status": "Playing", "artist": curr['item']['artists'][0]['name'], "title": curr['item']['name']}
        elif curr: return {"status": "Paused", "artist": curr['item']['artists'][0]['name'], "title": curr['item']['name']}
    except: pass 
    return None

def get_local_mpris_meta():
    try:
        status = AudioSystem.run(['playerctl', 'status'])
        artist = AudioSystem.run(['playerctl', 'metadata', 'artist'])
        title = AudioSystem.run(['playerctl', 'metadata', 'title'])
        return {"status": status, "artist": artist or "Unknown", "title": title}
    except: return {"status": "Stopped", "artist": "", "title": "Nothing Playing"}

# --- CORE SYSTEM LOOP ---
def hardware_loop():
    global last_spotify_check, spotify_cache, last_audio_devs
    while True:
        curr_time = time.time()
        
        # Audio Device Auto-Scan
        curr_sinks = AudioSystem.get_hardware_sinks()
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

        data = {
            "cpu": psutil.cpu_percent(interval=None),
            "ram": psutil.virtual_memory().percent,
            "spotify": media,
            "discord": disc_ipc_instance.voice_state if disc_ipc_instance and disc_ipc_instance.connected else {"mute": False, "deaf": False},
            "audio": {
                "spk": AudioSystem.get_state('@DEFAULT_AUDIO_SINK@'),
                "mic": AudioSystem.get_state('@DEFAULT_AUDIO_SOURCE@'),
                "active_dev": AudioSystem.get_active_name()
            }
        }
        
        try:
            with open("/tmp/g502_battery.txt", "r") as f: data["mouse_batt"] = f.read().strip()
        except: data["mouse_batt"] = "--"

        socketio.emit('sys_data', data)
        time.sleep(0.5)

def fetch_weather():
    while True:
        if config.get("weather_api") and config.get("weather_city"):
            try:
                url = f"http://api.openweathermap.org/data/2.5/weather?q={config['weather_city']}&appid={config['weather_api']}&units=metric"
                res = requests.get(url, timeout=5).json()
                if "main" in res: socketio.emit('weather_data', {"temp": round(res["main"]["temp"]), "desc": res["weather"][0]["description"].title()})
            except: pass
        time.sleep(600)

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

@socketio.on('save_config')
def handle_config_save(data):
    global config
    old_id = config.get("disc_id")
    old_secret = config.get("disc_secret")
    config.update(data)
    save_config()
    if old_id != config.get("disc_id") or old_secret != config.get("disc_secret"):
        restart_discord_ipc()

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
            except: pass
        if not sp_success:
            if action == 'spot_play': AudioSystem.run(['playerctl', 'play-pause'])
            elif action == 'spot_next': AudioSystem.run(['playerctl', 'next'])
            elif action == 'spot_prev': AudioSystem.run(['playerctl', 'previous'])

    elif action.startswith('sp_play_'):
        sp_id = action.split('sp_play_')[1]
        AudioSystem.run(['soundux', '--play', sp_id]) # Adjust as needed for specific linux soundpad alternative

    elif action == 'disc_mute':
        if disc_ipc_instance and disc_ipc_instance.connected: disc_ipc_instance.set_voice(mute=not disc_ipc_instance.voice_state["mute"])
        else: AudioSystem.run(['ydotool', 'key', '29:1', '42:1', '50:1', '50:0', '42:0', '29:0']) 
    elif action == 'disc_deaf':
        if disc_ipc_instance and disc_ipc_instance.connected: disc_ipc_instance.set_voice(deaf=not disc_ipc_instance.voice_state["deaf"])
        else: AudioSystem.run(['ydotool', 'key', '29:1', '42:1', '32:1', '32:0', '42:0', '29:0'])
    elif action == 'disc_cam': pass # Implement specific ydotool sequence if desired
    elif action == 'disc_screen': pass # Implement specific ydotool sequence if desired
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
            st = speedtest.Speedtest()
            st.get_best_server()
            socketio.emit('speedtest_result', {'down': round(st.download() / 1_000_000, 1), 'up': round(st.upload() / 1_000_000, 1)})
        except: socketio.emit('speedtest_result', {'down': 'ERR', 'up': 'ERR'})
    threading.Thread(target=test, daemon=True).start()

if __name__ == '__main__':
    restart_discord_ipc()
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=fetch_weather, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000)
