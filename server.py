import os
import time
import threading
import subprocess
import requests
import psutil
import speedtest
from flask import Flask, render_template
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# In-memory config (populated by the tablet's local storage)
config = {
    "weather_api": "",
    "weather_city": "Pristina",
    "mouse_batt_file": "/tmp/g502_battery.txt" # Ensure your script outputs here
}

def run_cmd(cmd_list):
    """Executes a shell command silently."""
    try:
        subprocess.Popen(cmd_list, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Command failed: {e}")

def get_spotify_meta():
    """Fetches media data without blocking if Spotify is closed."""
    try:
        status = subprocess.check_output(['playerctl', 'status'], timeout=1).decode().strip()
        artist = subprocess.check_output(['playerctl', 'metadata', 'artist'], timeout=1).decode().strip()
        title = subprocess.check_output(['playerctl', 'metadata', 'title'], timeout=1).decode().strip()
        return {"status": status, "artist": artist, "title": title}
    except subprocess.TimeoutExpired:
        return {"status": "Timeout", "artist": "", "title": "Service Unresponsive"}
    except:
        return {"status": "Stopped", "artist": "", "title": "Nothing is Currently Playing"}

def hardware_loop():
    """Core 500ms polling loop."""
    while True:
        data = {
            "cpu": psutil.cpu_percent(interval=None),
            "ram": psutil.virtual_memory().percent,
            "spotify": get_spotify_meta()
        }

        try:
            with open(config["mouse_batt_file"], "r") as f:
                data["mouse_batt"] = f.read().strip()
        except FileNotFoundError:
            data["mouse_batt"] = "--"

        socketio.emit('sys_data', data)
        time.sleep(0.5)

def fetch_weather():
    """Polls OpenWeather API every 10 minutes."""
    while True:
        if config.get("weather_api") and config.get("weather_city"):
            url = f"http://api.openweathermap.org/data/2.5/weather?q={config['weather_city']}&appid={config['weather_api']}&units=metric"
            try:
                res = requests.get(url, timeout=5).json()
                if "main" in res:
                    weather_data = {
                        "temp": round(res["main"]["temp"]),
                        "desc": res["weather"][0]["description"].title()
                    }
                    socketio.emit('weather_data', weather_data)
            except Exception as e:
                print(f"Weather error: {e}")
        time.sleep(600)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('save_config')
def handle_config(data):
    global config
    config.update(data)
    print("Dashboard config updated.")

@socketio.on('action')
def handle_action(action):
    # Media
    if action == 'spot_play': run_cmd(['playerctl', 'play-pause'])
    elif action == 'spot_next': run_cmd(['playerctl', 'next'])
    elif action == 'spot_prev': run_cmd(['playerctl', 'previous'])

    # Discord Macros (Adjust keycodes if your ydotool maps differently)
    # 29=Ctrl, 42=Shift, 50=M, 32=D
    elif action == 'disc_mute': run_cmd(['ydotool', 'key', '29:1', '42:1', '50:1', '50:0', '42:0', '29:0'])
    elif action == 'disc_deaf': run_cmd(['ydotool', 'key', '29:1', '42:1', '32:1', '32:0', '42:0', '29:0'])

    # Apps
    elif action == 'app_term': run_cmd(['alacritty'])
    elif action == 'app_web': run_cmd(['brave'])
    elif action == 'app_task': run_cmd(['gnome-system-monitor'])
    elif action == 'app_clip': run_cmd(['ydotool', 'key', '119:1', '119:0']) # Pause/Break for Medal
    elif action == 'app_soundpad': run_cmd(['soundux'])

    # Audio Switching (Replace node IDs via `wpctl status`)
    elif action == 'audio_xonar': run_cmd(['wpctl', 'set-default', '50']) # Example ID
    elif action == 'audio_mobius': run_cmd(['wpctl', 'set-default', '51']) # Example ID

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
            down = round(st.download() / 1_000_000, 1)
            up = round(st.upload() / 1_000_000, 1)
            socketio.emit('speedtest_result', {'down': down, 'up': up})
        except Exception as e:
            socketio.emit('speedtest_result', {'down': 'ERR', 'up': 'ERR'})
            print(f"Speedtest failed: {e}")
    # Run in thread so it doesn't freeze the 500ms dashboard updates
    threading.Thread(target=test, daemon=True).start()

if __name__ == '__main__':
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=fetch_weather, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000)
