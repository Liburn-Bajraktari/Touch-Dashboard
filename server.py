import os
import time
import threading
import subprocess
import requests
import psutil
import speedtest
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

app = Flask(__name__)
# Using gevent to avoid Eventlet deprecation warnings
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

config = {
    "weather_api": "",
    "weather_city": "Pristina",
    "mouse_batt_file": "/tmp/g502_battery.txt",
    "spot_id": "",
    "spot_secret": ""
}

spotify_cache = None
last_spotify_check = 0

def run_cmd(cmd_list):
    try: subprocess.Popen(cmd_list, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass

def get_spotify_api_meta():
    if not config.get("spot_id") or not config.get("spot_secret"): return None
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri="http://127.0.0.1:5000/callback", scope="user-read-playback-state user-modify-playback-state", open_browser=False))
        current = sp.current_playback()
        if current and current.get('is_playing'):
            return {"status": "Playing", "artist": current['item']['artists'][0]['name'], "title": current['item']['name'], "source": "spotify"}
        elif current:
            return {"status": "Paused", "artist": current['item']['artists'][0]['name'], "title": current['item']['name'], "source": "spotify"}
    except Exception: pass
    return None

def get_local_mpris_meta():
    try:
        status = subprocess.check_output(['playerctl', 'status'], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
        artist = subprocess.check_output(['playerctl', 'metadata', 'artist'], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
        title = subprocess.check_output(['playerctl', 'metadata', 'title'], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
        return {"status": status, "artist": artist or "Unknown", "title": title, "source": "local"}
    except: return {"status": "Stopped", "artist": "", "title": "Nothing Playing", "source": "none"}

def hardware_loop():
    global last_spotify_check, spotify_cache
    while True:
        current_time = time.time()
        if current_time - last_spotify_check > 3.0:
            spotify_cache = get_spotify_api_meta()
            last_spotify_check = current_time

        media_data = spotify_cache
        if not media_data or media_data['status'] != 'Playing':
            local_media = get_local_mpris_meta()
            if local_media['status'] == 'Playing' or not media_data:
                media_data = local_media

        data = {
            "cpu": psutil.cpu_percent(interval=None),
            "ram": psutil.virtual_memory().percent,
            "spotify": media_data
        }

        try:
            with open(config["mouse_batt_file"], "r") as f: data["mouse_batt"] = f.read().strip()
        except: data["mouse_batt"] = "--"

        socketio.emit('sys_data', data)
        time.sleep(0.5)

def fetch_weather():
    while True:
        if config.get("weather_api") and config.get("weather_city"):
            url = f"http://api.openweathermap.org/data/2.5/weather?q={config['weather_city']}&appid={config['weather_api']}&units=metric"
            try:
                res = requests.get(url, timeout=5).json()
                if "main" in res: socketio.emit('weather_data', {"temp": round(res["main"]["temp"]), "desc": res["weather"][0]["description"].title()})
            except: pass
        time.sleep(600)

@app.route('/')
def index():
    return render_template('index.html')

# --- THIS FIXES THE BROWSER INSTALL/FULLSCREEN ISSUE ---
@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Command Center",
        "short_name": "Dash",
        "display": "fullscreen",
        "orientation": "landscape",
        "background_color": "#090e17",
        "theme_color": "#090e17",
        "icons": [{"src": "https://upload.wikimedia.org/wikipedia/commons/4/49/A_black_image.jpg", "sizes": "192x192", "type": "image/jpeg"}]
    })

@socketio.on('save_config')
def handle_config(data):
    global config
    config.update(data)

@socketio.on('action')
def handle_action(action):
    if action.startswith('spot_'):
        sp_success = False
        if config.get("spot_id") and config.get("spot_secret"):
            try:
                sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri="http://127.0.0.1:5000/callback", scope="user-read-playback-state user-modify-playback-state", open_browser=False))
                if action == 'spot_play':
                    playback = sp.current_playback()
                    if playback and playback['is_playing']: sp.pause_playback()
                    else: sp.start_playback()
                elif action == 'spot_next': sp.next_track()
                elif action == 'spot_prev': sp.previous_track()
                sp_success = True
            except: pass
        if not sp_success:
            if action == 'spot_play': run_cmd(['playerctl', 'play-pause'])
            elif action == 'spot_next': run_cmd(['playerctl', 'next'])
            elif action == 'spot_prev': run_cmd(['playerctl', 'previous'])
    elif action == 'disc_mute': run_cmd(['ydotool', 'key', '29:1', '42:1', '50:1', '50:0', '42:0', '29:0'])
    elif action == 'disc_deaf': run_cmd(['ydotool', 'key', '29:1', '42:1', '32:1', '32:0', '42:0', '29:0'])
    elif action == 'app_term': run_cmd(['alacritty'])
    elif action == 'app_web': run_cmd(['brave'])
    elif action == 'app_task': run_cmd(['gnome-system-monitor'])
    elif action == 'app_clip': run_cmd(['ydotool', 'key', '119:1', '119:0'])
    elif action == 'app_soundpad': run_cmd(['soundux'])
    elif action == 'audio_xonar': run_cmd(['wpctl', 'set-default', '50'])
    elif action == 'audio_mobius': run_cmd(['wpctl', 'set-default', '51'])
    elif action == 'audio_mute_spk': run_cmd(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', 'toggle'])
    elif action == 'audio_mute_mic': run_cmd(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SOURCE@', 'toggle'])

@socketio.on('set_volume')
def handle_volume(data):
    target = '@DEFAULT_AUDIO_SINK@' if data['type'] == 'speaker' else '@DEFAULT_AUDIO_SOURCE@'
    run_cmd(['wpctl', 'set-volume', target, f"{data['val']}%"])

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
