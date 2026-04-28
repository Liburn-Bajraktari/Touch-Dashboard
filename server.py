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
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from contextlib import asynccontextmanager
import uvicorn

# --- CONFIGURATION MANAGER ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
WEATHER_CACHE_FILE = os.path.join(BASE_DIR, "weather_cache.json")
SPOTIFY_CACHE_FILE = os.path.join(BASE_DIR, ".cache")

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
        self.client_id, self.client_secret = client_id, client_secret
        self.sock, self.access_token = None, config.get("disc_token", "")
        self.connected, self.running, self.warned_local = False, False, False
        self.voice_state = {"mute": False, "deaf": False}

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
            if os.name == 'nt': self.sock = open(pipe_path, 'w+b')
            else:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(pipe_path)
                self.sock.settimeout(2.0)
            self.send(0, {"v": 1, "client_id": self.client_id})
            self.recv()
            if not self.access_token: self.authorize()
            else: self.authenticate()
            self.connected = True
            return True
        except Exception:
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
        data = b""
        while len(data) < length:
            chunk = self.sock.read(length - len(data)) if os.name == 'nt' else self.sock.recv(length - len(data))
            if not chunk: return b""
            data += chunk
        return data

    def send(self, opcode, payload):
        data = json.dumps(payload).encode('utf-8')
        try: self.sock_send(struct.pack("<II", opcode, len(data)) + data)
        except Exception: self.connected = False

    def recv(self):
        try:
            header = self.sock_recv(8)
            if len(header) < 8: return {}
            opcode, length = struct.unpack("<II", header)
            return json.loads(self.sock_recv(length).decode('utf-8'))
        except: return {}

    def authorize(self):
        nonce = str(uuid.uuid4())
        self.send(1, {"cmd": "AUTHORIZE", "args": {"client_id": self.client_id, "scopes": ["rpc", "rpc.voice.read", "rpc.voice.write"]}, "nonce": nonce})
        while True:
            res = self.recv()
            if not res: break
            if res.get("cmd") == "AUTHORIZE" and res.get("nonce") == nonce:
                if code := res.get("data", {}).get("code"): self.exchange_code(code)
                break

    def exchange_code(self, code):
        try:
            r = requests.post("https://discord.com/api/oauth2/token", data={"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "authorization_code", "code": code, "redirect_uri": "http://127.0.0.1"}, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=5)
            if r.status_code == 200:
                self.access_token = r.json().get("access_token")
                config["disc_token"] = self.access_token
                save_config()
                self.authenticate()
        except: pass

    def authenticate(self):
        self.send(1, {"cmd": "AUTHENTICATE", "args": {"access_token": self.access_token}, "nonce": str(uuid.uuid4())})
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
                if not res: 
                    self.close()
                    continue
                if res.get("evt") == "VOICE_SETTINGS_UPDATE" or res.get("nonce") == "GET_VOICE":
                    data = res.get("data", {})
                    self.voice_state["mute"] = data.get("mute", False)
                    self.voice_state["deaf"] = data.get("deaf", False)
            except:
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
    if config.get("disc_id") and config.get("disc_secret"):
        disc_ipc_instance = DiscordIPC(config["disc_id"], config["disc_secret"])
        threading.Thread(target=disc_ipc_instance.loop, daemon=True).start()

# --- MEDIA ENGINES ---
def get_spotify_api_meta(host_url):
    if not config.get("spot_id") or not config.get("spot_secret"): return None
    try:
        sp_oauth = SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri=f"http://{host_url}/callback", open_browser=False, cache_path=SPOTIFY_CACHE_FILE)
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

def get_local_mpris_meta():
    try:
        meta = AudioSystem.run(['playerctl', 'metadata', '--format', '{{status}}|||{{artist}}|||{{title}}|||{{mpris:artUrl}}'])
        if not meta: return {"status": "Stopped", "artist": "", "title": "Nothing Playing", "art_url": ""}
        parts = meta.split('|||')
        status = parts[0].strip() if len(parts) > 0 else "Stopped"
        if status not in ['Playing', 'Paused']: return {"status": "Stopped", "artist": "", "title": "Nothing Playing", "art_url": ""}
        
        artist, title, art_url = (parts[1].strip() if len(parts) > 1 else "Unknown"), (parts[2].strip() if len(parts) > 2 else "Unknown"), (parts[3].strip() if len(parts) > 3 else "")

        if art_url.startswith('file://'):
            path = urllib.parse.unquote(art_url.replace('file://', ''))
            try:
                with open(path, 'rb') as img_f:
                    b64 = base64.b64encode(img_f.read()).decode('utf-8')
                    ext = path.split('.')[-1].lower()
                    mime = f"image/{ext}" if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else "image/jpeg"
                    art_url = f"data:{mime};base64,{b64}"
            except: art_url = ""

        return {"status": status, "artist": artist, "title": title, "art_url": art_url}
    except: return {"status": "Stopped", "artist": "", "title": "Nothing Playing", "art_url": ""}

# --- STATE MANAGERS & FASTAPI WEBSOCKET MANAGER ---
force_media_update, current_media_source = False, "local"
spotify_cache, last_spotify_check, last_audio_devs = None, 0, []
last_weather_data = {"temp": "--", "desc": "--", "timestamp": 0}
weather_force_update = False
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
    global last_spotify_check, spotify_cache, last_audio_devs, force_media_update, current_media_source, last_host_url
    last_gpu_check, gpu_cache = 0, ""
    
    while True:
        curr_time = time.time()
        
        # Audio Polling (Run blocking subprocess in thread)
        audio_data = await asyncio.to_thread(AudioSystem.poll_all)
        curr_sinks = audio_data['sinks']
        if [s['raw_name'] for s in curr_sinks] != [s['raw_name'] for s in last_audio_devs]:
            last_audio_devs = curr_sinks
            await ws_manager.broadcast({"type": "hw_scan_results", "data": curr_sinks})

        # Spotify Polling
        if curr_time - last_spotify_check > 3.0 or force_media_update:
            if force_media_update: await asyncio.sleep(0.4)
            spotify_cache = await asyncio.to_thread(get_spotify_api_meta, last_host_url)
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

        # GPU Polling
        if curr_time - last_gpu_check > 2.0:
            try: gpu_cache = await asyncio.to_thread(AudioSystem.run, ['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'])
            except: gpu_cache = ""
            last_gpu_check = curr_time

        try:
            with open("/tmp/g502_battery.txt", "r") as f: mouse_batt = f.read().strip()
        except: mouse_batt = "--"

        await ws_manager.broadcast({
            "type": "sys_data",
            "data": {
                "cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent,
                "gpu": gpu_cache if gpu_cache else None, "mouse_batt": mouse_batt, "spotify": media,
                "discord": disc_ipc_instance.voice_state if disc_ipc_instance and disc_ipc_instance.connected else {"mute": False, "deaf": False},
                "audio": {"spk": audio_data['spk'], "mic": audio_data['mic'], "active_dev": audio_data['active_sink_name']}
            }
        })
        await asyncio.sleep(0.5)

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
        for _ in range(1800):
            if weather_force_update:
                weather_force_update = False
                break
            await asyncio.sleep(1)
        await do_fetch()

# --- FASTAPI APP & ROUTES ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    restart_discord_ipc()
    asyncio.create_task(hardware_loop())
    asyncio.create_task(fetch_weather())
    yield
    if disc_ipc_instance: disc_ipc_instance.close()

app = FastAPI(lifespan=lifespan)

@app.get('/')
async def index(request: Request):
    global last_host_url
    last_host_url = request.url.netloc
    # FIX: Explicitly point to the 'templates' folder as shown in your screenshot
    return FileResponse(os.path.join(BASE_DIR, "templates", "index.html"))

@app.get('/favicon.ico')
async def favicon():
    # Silences the 404 error in your terminal
    return JSONResponse({})

@app.get('/manifest.json')
async def manifest():
    return JSONResponse({
        "name": "Command Center", "short_name": "Dash", "display": "fullscreen", "orientation": "landscape",
        "background_color": "#090e17", "theme_color": "#090e17",
        "icons": [{"src": "https://upload.wikimedia.org/wikipedia/commons/4/49/A_black_image.jpg", "sizes": "192x192", "type": "image/jpeg"}]
    })

@app.get('/spotify_login')
async def spotify_login(request: Request):
    if not config.get("spot_id") or not config.get("spot_secret"): return JSONResponse({"error": "No credentials"})
    sp_oauth = SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri=f"http://{request.url.netloc}/callback", scope="user-read-playback-state user-modify-playback-state", open_browser=False, cache_path=SPOTIFY_CACHE_FILE)
    return RedirectResponse(sp_oauth.get_authorize_url())

@app.get('/callback')
async def callback(request: Request, code: str = None):
    if not config.get("spot_id") or not config.get("spot_secret") or not code: return RedirectResponse('/')
    sp_oauth = SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri=f"http://{request.url.netloc}/callback", scope="user-read-playback-state user-modify-playback-state", open_browser=False, cache_path=SPOTIFY_CACHE_FILE)
    try: await asyncio.to_thread(sp_oauth.get_access_token, code)
    except: pass
    return RedirectResponse('/')

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # SENIOR DEV FIX: All globals must be at the absolute top.
    # We only need 'global' for variables we REBIND (using =). 
    # config.update() is a mutation, but we'll keep it here for clarity.
    global config, weather_force_update, force_media_update, current_media_source
    
    await ws_manager.connect(websocket)
    await websocket.send_json({"type": "config_sync", "data": {"cfg": config, "hw": AudioSystem.get_hardware_sinks()}})
    
    if last_weather_data.get("temp") != "--":
        await websocket.send_json({"type": "weather_data", "data": last_weather_data})
    
    try:
        while True:
            text = await websocket.receive_text()
            msg = json.loads(text)
            msg_type, data = msg.get("type"), msg.get("data")
            
            if msg_type == 'save_config':
                # REMOVED: Redundant 'global' declaration that caused the SyntaxError
                old_id, old_secret = config.get("disc_id"), config.get("disc_secret")
                old_weather_api, old_weather_city = config.get("weather_api"), config.get("weather_city")
                
                config.update(data)
                await asyncio.to_thread(save_config)
                
                # Immediate sync for custom names
                audio_data = await asyncio.to_thread(AudioSystem.poll_all)
                await ws_manager.broadcast({"type": "config_sync", "data": {"cfg": config, "hw": audio_data['sinks']}})
                
                if old_id != config.get("disc_id") or old_secret != config.get("disc_secret"): restart_discord_ipc()
                if old_weather_api != config.get("weather_api") or old_weather_city != config.get("weather_city"): weather_force_update = True

            elif msg_type == 'action':
                action = data
                if action.startswith('spot_'):
                    routed_to_spot = False
                    if current_media_source == "spotify" and config.get("spot_id"):
                        try:
                            sp_oauth = SpotifyOAuth(client_id=config["spot_id"], client_secret=config["spot_secret"], redirect_uri=f"http://{last_host_url}/callback", open_browser=False, cache_path=SPOTIFY_CACHE_FILE)
                            token_info = await asyncio.to_thread(sp_oauth.get_cached_token)
                            if token_info:
                                sp = spotipy.Spotify(auth=token_info['access_token'], requests_timeout=3)
                                if action == 'spot_play':
                                    c = await asyncio.to_thread(sp.current_playback)
                                    if c and c.get('is_playing'): await asyncio.to_thread(sp.pause_playback)
                                    else: await asyncio.to_thread(sp.start_playback)
                                elif action == 'spot_next': await asyncio.to_track(sp.next_track)
                                elif action == 'spot_prev': await asyncio.to_thread(sp.previous_track)
                                routed_to_spot, force_media_update = True, True
                        except: pass
                    if not routed_to_spot:
                        if action == 'spot_play': await asyncio.to_thread(AudioSystem.run, ['playerctl', 'play-pause'])
                        elif action == 'spot_next': await asyncio.to_thread(AudioSystem.run, ['playerctl', 'next'])
                        elif action == 'spot_prev': await asyncio.to_thread(AudioSystem.run, ['playerctl', 'previous'])
                        force_media_update = True
                
                elif action.startswith('sp_play_'): await asyncio.to_thread(AudioSystem.run, ['soundux', '--play', action.split('sp_play_')[1]])
                elif action == 'disc_mute':
                    if disc_ipc_instance and disc_ipc_instance.connected: disc_ipc_instance.set_voice(mute=not disc_ipc_instance.voice_state["mute"])
                    else: await asyncio.to_thread(AudioSystem.run, ['ydotool', 'key', '29:1', '42:1', '50:1', '50:0', '42:0', '29:0']) 
                elif action == 'disc_deaf':
                    if disc_ipc_instance and disc_ipc_instance.connected: disc_ipc_instance.set_voice(deaf=not disc_ipc_instance.voice_state["deaf"])
                    else: await asyncio.to_thread(AudioSystem.run, ['ydotool', 'key', '29:1', '42:1', '32:1', '32:0', '42:0', '29:0'])
                elif action == 'app_term': await asyncio.to_thread(AudioSystem.run, ['alacritty']) 
                elif action == 'app_web': await asyncio.to_thread(AudioSystem.run, ['brave'])
                elif action == 'app_task': await asyncio.to_thread(AudioSystem.run, ['gnome-system-monitor']) 
                elif action == 'app_clip': await asyncio.to_thread(AudioSystem.run, ['ydotool', 'key', '119:1', '119:0'])
                elif action == 'app_soundpad': await asyncio.to_thread(AudioSystem.run, ['soundux']) 
                elif action == 'audio_cycle': await asyncio.to_thread(AudioSystem.cycle_device)
                elif action == 'audio_mute_spk': await asyncio.to_thread(AudioSystem.toggle_mute, '@DEFAULT_AUDIO_SINK@')
                elif action == 'audio_mute_mic': await asyncio.to_thread(AudioSystem.toggle_mute, '@DEFAULT_AUDIO_SOURCE@')

            elif msg_type == 'set_volume':
                target = '@DEFAULT_AUDIO_SINK@' if data['type'] == 'speaker' else '@DEFAULT_AUDIO_SOURCE@'
                await asyncio.to_thread(AudioSystem.set_vol, target, data['val'])

            elif msg_type == 'run_speedtest':
                async def run_st():
                    try:
                        st = speedtest.Speedtest()
                        await asyncio.to_thread(st.get_best_server)
                        down, up = await asyncio.to_thread(st.download), await asyncio.to_thread(st.upload)
                        await ws_manager.broadcast({"type": "speedtest_result", "data": {'down': round(down / 1_000_000, 1), 'up': round(up / 1_000_000, 1)}})
                    except: await ws_manager.broadcast({"type": "speedtest_result", "data": {'down': 'ERR', 'up': 'ERR'}})
                asyncio.create_task(run_st())

    except WebSocketDisconnect: ws_manager.disconnect(websocket)

if __name__ == '__main__':
    # We explicitly tell uvicorn to watch both the root and the templates subfolder
    uvicorn.run(
        "server:app", 
        host='0.0.0.0', 
        port=5000, 
        reload=True, 
        reload_dirs=[BASE_DIR, os.path.join(BASE_DIR, "templates")]
    )