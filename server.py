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
                if re.search(r'^\s*Sinks:', line): capture = True; continue
                if capture and re.search(r'^\s*(Sources|Filters|Streams|Video):', line): break
                if capture:
                    match = re.search(r'(?:\*|\s)\s+(\d+)\.\s+([^\[]+)', line)
                    if match: 
                        dev_id = match.group(1)
                        dev_name = match.group(2).strip()
                        custom_names = config.get("audio_names", {})
                        custom_name = custom_names.get(dev_name, "")
                        display_name = custom_name[:10] if custom_name else dev_name[:5].upper()
                        devices.append({"id": dev_id, "name": display_name, "raw_name": dev_name, "custom_name": custom_name})
        except Exception as e: print(f"Audio parse error: {e}")
        return devices

    @staticmethod
    def get_active_sink_id():
        try:
            out = AudioSystem.run(['wpctl', 'status'])
            capture = False
            for line in out.splitlines():
                if re.search(r'^\s*Sinks:', line): capture = True; continue
                if capture and re.search(r'^\s*(Sources|Filters|Streams|Video):', line): break
                if capture:
                    match = re.search(r'(\*?)\s+(\d+)\.\s+([^\[]+)', line)
                    if match and match.group(1) == '*':
                        return match.group(2)
        except: pass
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
    config.update(data)
    save_config()

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

    elif action == 'disc_mute': AudioSystem.run(['ydotool', 'key', '29:1', '42:1', '50:1', '50:0', '42:0', '29:0']) 
    elif action == 'disc_deaf': AudioSystem.run(['ydotool', 'key', '29:1', '42:1', '32:1', '32:0', '42:0', '29:0'])
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
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=fetch_weather, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000)
