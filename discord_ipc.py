"""
discord_ipc.py — Discord Rich-Presence IPC client for Touch Dashboard.

Connects to the locally-running Discord (or arRPC/Vesktop) process via
a named socket / Windows named pipe, subscribes to voice state events,
and exposes mute/deafen controls.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import time
import uuid
import urllib.parse

logger = logging.getLogger(__name__)


class DiscordIPC:
    """
    Manages a persistent connection to the Discord IPC socket.
    Thread-safe; meant to run in its own daemon thread via `loop()`.
    """

    def __init__(self, client_id: str, client_secret: str,
                 access_token: str = "", config_save_fn=None):
        self.client_id     = client_id
        self.client_secret = client_secret
        self.access_token  = access_token
        self._config_save  = config_save_fn  # callable(token: str) → None

        self._sock: socket.socket | None = None
        self._win_pipe = None              # open() file handle on Windows

        self.connected      = False
        self.running        = False
        self.auth_pending   = False
        self.needs_reauth   = False
        self.is_vesktop     = False
        self.voice_supported = False
        self.voice_state    = {"mute": False, "deaf": False}
        self.voice_channel: dict | None = None
        self.pre_deafen_mute = False

        # Hooks set by the server to trigger a data push after state changes
        self.on_state_change = None   # asyncio-safe callable

    # ── pipe discovery ─────────────────────────────────────────────────────────

    def _get_pipe_paths(self) -> list[str]:
        if sys.platform.startswith("win"):
            paths = []
            for i in range(10):
                p = f"\\\\.\\pipe\\discord-ipc-{i}"
                if os.path.exists(p):
                    paths.append(p)
            return paths

        # Linux / macOS: search runtime dirs
        env_vars = ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP")
        bases = [os.environ[v] for v in env_vars if os.environ.get(v)]
        bases += [f"/run/user/{os.getuid()}", "/tmp"]

        found: list[str] = []
        for base in bases:
            for i in range(10):
                for candidate in (
                    os.path.join(base, f"discord-ipc-{i}"),
                    os.path.join(base, "app/com.discordapp.Discord", f"discord-ipc-{i}"),
                ):
                    if os.path.exists(candidate) and candidate not in found:
                        found.append(candidate)
        return found

    # ── low-level I/O ──────────────────────────────────────────────────────────

    def _sock_send(self, data: bytes) -> None:
        if sys.platform.startswith("win") and self._win_pipe:
            self._win_pipe.write(data)
            self._win_pipe.flush()
        elif self._sock:
            self._sock.sendall(data)

    def _sock_recv(self, length: int) -> bytes:
        buf = b""
        while len(buf) < length:
            if sys.platform.startswith("win") and self._win_pipe:
                chunk = self._win_pipe.read(length - len(buf))
            elif self._sock:
                chunk = self._sock.recv(length - len(buf))
            else:
                return b""
            if not chunk:
                return b""
            buf += chunk
        return buf

    def send(self, opcode: int, payload: dict) -> None:
        logger.debug(f"DISCORD SEND [{opcode}]: {payload}")
        data = json.dumps(payload).encode("utf-8")
        try:
            self._sock_send(struct.pack("<II", opcode, len(data)) + data)
        except Exception:
            self.connected = False

    def recv(self) -> dict | None:
        try:
            header = self._sock_recv(8)
            if not header or len(header) < 8:
                return None
            _opcode, length = struct.unpack("<II", header)
            payload = self._sock_recv(length)
            if not payload:
                return None
            res = json.loads(payload.decode("utf-8"))
            logger.debug(f"DISCORD RECV: {res}")
            return res
        except socket.timeout:
            return {}
        except Exception as e:
            logger.debug(f"DISCORD RECV error: {e}")
            return None

    # ── connection ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        for path in self._get_pipe_paths():
            try:
                if sys.platform.startswith("win"):
                    # Use binary mode with no buffering for reliable pipe I/O
                    self._win_pipe = open(path, "r+b", buffering=0)
                    self._sock = None
                else:
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self._sock.connect(path)
                    self._sock.settimeout(2.0)
                    self._win_pipe = None

                self.send(0, {"v": 1, "client_id": self.client_id})
                res = self.recv()
                if not res:
                    self._close_transport(); continue

                self.is_vesktop = (
                    res.get("data", {}).get("user", {}).get("username") == "arrpc"
                )
                if self.is_vesktop:
                    self.auth_pending = False
                    self.connected = True
                    return True

                if not self.access_token:
                    self.auth_pending = True
                    self.connected = True
                    return True

                self._authenticate()
                self.connected = True
                return True

            except Exception as e:
                logger.debug(f"Discord connect error on '{path}': {e}")
                self._close_transport()

        return False

    def _close_transport(self) -> None:
        for attr in ("_sock", "_win_pipe"):
            obj = getattr(self, attr, None)
            if obj:
                try: obj.close()
                except Exception: pass
            setattr(self, attr, None)

    def close(self) -> None:
        self.connected = False
        self._close_transport()

    # ── authentication ─────────────────────────────────────────────────────────

    def get_auth_url(self) -> str:
        scopes = "rpc rpc.voice.read rpc.voice.write rpc.guilds.read"
        redirect = urllib.parse.quote("http://127.0.0.1:5000/disc_callback")
        return (
            f"https://discord.com/api/oauth2/authorize"
            f"?client_id={self.client_id}"
            f"&redirect_uri={redirect}"
            f"&response_type=code"
            f"&scope={scopes}"
        )

    def exchange_code(self, code: str) -> bool:
        """Exchange an OAuth2 code for an access token (HTTP, blocking)."""
        try:
            import requests
            data = {
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  "http://127.0.0.1:5000/disc_callback",
            }
            r = requests.post(
                "https://discord.com/api/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=5,
            )
            if r.status_code == 200:
                self.access_token = r.json().get("access_token", "")
                if self._config_save:
                    self._config_save(self.access_token)
                return True
            logger.error(f"Discord token exchange: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"Discord token exchange exception: {e}")
        return False

    def _authenticate(self) -> None:
        self.send(1, {
            "cmd": "AUTHENTICATE",
            "args": {"access_token": self.access_token},
            "nonce": str(uuid.uuid4()),
        })
        res = self.recv()
        if res and res.get("evt") == "ERROR":
            logger.error(f"Discord auth error: {res}")
            self.access_token = ""
            if self._config_save:
                self._config_save("")
            self.auth_pending = True
            return

        for evt in ("VOICE_SETTINGS_UPDATE", "VOICE_CHANNEL_SELECT"):
            self.send(1, {"cmd": "SUBSCRIBE", "evt": evt, "args": {}, "nonce": str(uuid.uuid4())})
        self.send(1, {"cmd": "GET_VOICE_SETTINGS",        "args": {}, "nonce": "GET_VOICE"})
        self.send(1, {"cmd": "GET_SELECTED_VOICE_CHANNEL", "args": {}, "nonce": "GET_VC"})
        self.auth_pending = False

    # ── IPC event loop ─────────────────────────────────────────────────────────

    def loop(self) -> None:
        self.running = True
        while self.running:
            if self.needs_reauth and self.connected:
                self.needs_reauth = False
                self._authenticate()

            if not self.connected:
                if not self.connect():
                    time.sleep(5)
                    continue

            try:
                if self._sock:
                    self._sock.settimeout(5.0)
                res = self.recv()
                if res is None:
                    self.close(); continue
                if res:
                    self._handle(res)
            except Exception as e:
                logger.error(f"Discord loop error: {e}")
                self.close()
                time.sleep(2)

    def _trigger(self) -> None:
        """Notify server that state changed (calls the registered hook if any)."""
        if self.on_state_change:
            try:
                self.on_state_change()
            except Exception:
                pass

    def _handle(self, res: dict) -> None:
        evt = res.get("evt")
        cmd = res.get("cmd")
        nonce = res.get("nonce")
        data  = res.get("data") or {}

        if evt == "ERROR":
            if nonce == "GET_GUILD_VC":
                # Missing rpc.guilds.read scope — force re-auth
                self.access_token = ""
                if self._config_save:
                    self._config_save("")
                self.auth_pending = True
                self._trigger()
            else:
                logger.error(f"Discord IPC error: {res}")
            return

        # Authorization via local IPC (in-app OAuth)
        if cmd == "AUTHORIZE" and "code" in data:
            if self.exchange_code(data["code"]):
                self._authenticate()
            return

        # Voice settings
        if evt in ("VOICE_SETTINGS_UPDATE",) or cmd in ("GET_VOICE_SETTINGS", "SET_VOICE_SETTINGS") or nonce == "GET_VOICE":
            self.voice_supported = True
            if "mute" in data: self.voice_state["mute"] = data["mute"]
            if "deaf" in data: self.voice_state["deaf"] = data["deaf"]
            self._trigger()
            return

        # Voice channel
        if cmd == "GET_SELECTED_VOICE_CHANNEL" or evt == "VOICE_CHANNEL_SELECT":
            cid = data.get("id") or data.get("channel_id")
            if not cid:
                self.voice_channel = None
                self._trigger()
                return
            if evt == "VOICE_CHANNEL_SELECT":
                self.send(1, {"cmd": "GET_SELECTED_VOICE_CHANNEL", "args": {}, "nonce": "GET_VC"})
                return
            # Build voice_channel from the full data returned by GET_SELECTED_VOICE_CHANNEL
            self.voice_channel = {
                "id": data.get("id"), "name": data.get("name"),
                "guild_id": data.get("guild_id"), "guild_name": None, "users": {},
            }
            cid_str = str(data.get("id"))
            for sub_evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE",
                            "SPEAKING_START", "SPEAKING_STOP"):
                self.send(1, {"cmd": "SUBSCRIBE", "evt": sub_evt,
                               "args": {"channel_id": cid_str}, "nonce": str(uuid.uuid4())})
            if data.get("guild_id"):
                self.send(1, {"cmd": "GET_GUILD",
                               "args": {"guild_id": str(data["guild_id"]), "timeout": 3},
                               "nonce": "GET_GUILD_VC"})
            for vs in data.get("voice_states", []):
                user = vs.get("user", {})
                uid = str(user.get("id") or "")
                if uid:
                    vstate = vs.get("voice_state", {})
                    self.voice_channel["users"][uid] = {
                        "id": uid,
                        "name": vs.get("nick") or user.get("global_name") or user.get("username"),
                        "avatar": user.get("avatar"),
                        "mute": vstate.get("mute") or vstate.get("self_mute"),
                        "deaf": vstate.get("deaf") or vstate.get("self_deaf"),
                        "speaking": False,
                    }
            self._trigger()
            return

        # Voice state changes
        if evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE") and self.voice_channel:
            user = data.get("user", {})
            uid  = str(user.get("id") or "")
            if uid:
                u = self.voice_channel["users"].setdefault(uid, {"id": uid, "speaking": False})
                u["name"]   = data.get("nick") or user.get("global_name") or user.get("username")
                u["avatar"] = user.get("avatar")
                vs = data.get("voice_state", {})
                u["mute"] = vs.get("mute") or vs.get("self_mute")
                u["deaf"] = vs.get("deaf") or vs.get("self_deaf")
            self._trigger()
            return

        if evt == "VOICE_STATE_DELETE" and self.voice_channel:
            uid = str((data.get("user") or {}).get("id") or "")
            if uid in self.voice_channel["users"]:
                del self.voice_channel["users"][uid]
            self._trigger()
            return

        if evt in ("SPEAKING_START", "SPEAKING_STOP") and self.voice_channel:
            uid = str(data.get("user_id") or "")
            if uid in self.voice_channel["users"]:
                self.voice_channel["users"][uid]["speaking"] = (evt == "SPEAKING_START")
            self._trigger()
            return

        if nonce == "GET_GUILD_VC" and self.voice_channel:
            if data and evt != "ERROR":
                g_id = str(data.get("id") or "")
                if str(self.voice_channel.get("guild_id")) == g_id:
                    self.voice_channel["guild_name"] = data.get("name")
                    self._trigger()
            return

    # ── voice controls ─────────────────────────────────────────────────────────

    def set_voice(self, mute: bool | None = None, deaf: bool | None = None) -> None:
        if not self.connected:
            return
        args: dict = {}
        if mute is not None: args["mute"] = mute
        if deaf is not None: args["deaf"] = deaf
        self.send(1, {"cmd": "SET_VOICE_SETTINGS", "args": args, "nonce": str(uuid.uuid4())})
