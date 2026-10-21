"""
security.py — Who may talk to the dashboard server.

The server listens on the whole LAN, so every WebSocket client proves itself:

  admin   loopback address + the dashboard's own origin (the desktop window,
          a browser on the PC).
  device  a token issued when the PC user approved a pairing request.

This module holds the parts with no FastAPI dependency: the paired-device
store, pending pairing requests and their limits, the Host / Origin
allowlists, and the local control token that installers and a second
instance use for /api/exit and /api/wakeup. desktop.py imports it too, so it
must never import server.py.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

TOKEN_BYTES          = 32      # secrets.token_urlsafe(32): 256 bits
MAX_TOKEN_LEN        = 256     # anything longer is not one of ours; skip hashing it
DEVICE_NAME_MAX      = 40
DEFAULT_DEVICE_NAME  = "Unnamed device"
PAIR_TTL_S           = 60.0
PAIR_RATE_WINDOW_S   = 60.0
PAIR_RATE_MAX        = 5
LAN_REFRESH_S        = 30.0    # at most this often, re-read interface addresses for an unknown IP host
MAX_PENDING_SOCKETS  = 32      # unauthenticated sockets open at once, from everyone
MAX_PENDING_PER_IP   = 4       # ... and from one address

PAIRED_DEVICES_FILE  = "paired_devices.json"
LOCAL_TOKEN_FILE     = "local_control.token"

# The Android app loads its bundled page from file:///android_asset with file
# access enabled, so Chromium sends "Origin: file://" (measured: without that
# setting it would be "null"). WebViewAssetLoader pages send the appassets
# origin. "null" is never accepted: sandboxed iframes on any site send it.
APK_ORIGINS = frozenset({"file://", "https://appassets.androidplatform.net"})
LOOPBACK_NAMES = ("127.0.0.1", "localhost")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def clean_device_name(value) -> str:
    """A display name from a client: printable, single-spaced, bounded."""
    if not isinstance(value, str):
        return DEFAULT_DEVICE_NAME
    name = " ".join(_CONTROL_CHARS.sub(" ", value).split())[:DEVICE_NAME_MAX].strip()
    return name or DEFAULT_DEVICE_NAME


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_private(path: str, text: str) -> None:
    """Atomically write a file only this user can read (0600 on POSIX)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ─── Paired devices ────────────────────────────────────────────────────────────

class PairedDevices:
    """paired_devices.json: [{id, name, token_hash, created, last_seen}].

    Only SHA-256 hashes of tokens are stored. Every mutation writes the file
    first and swaps the in-memory list after, so the two never disagree when
    the disk write fails. Mutators do file I/O: call them via asyncio.to_thread.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._devices: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            logger.error("Could not read %s: %s — no devices are paired", self.path, e)
            return []
        keys = ("id", "name", "token_hash", "created", "last_seen")
        return [
            {k: str(d[k]) for k in keys}
            for d in (raw if isinstance(raw, list) else [])
            if isinstance(d, dict) and all(isinstance(d.get(k), str) and d.get(k) for k in keys[:3])
            and all(k in d for k in keys)
        ]

    def _commit(self, devices: list[dict]) -> None:
        write_private(self.path, json.dumps(devices, indent=2))
        self._devices = devices

    def verify(self, token) -> dict | None:
        """The device this token belongs to (without its hash), or None."""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN:
            return None
        digest = hash_token(token)
        found = None
        with self._lock:
            # Compare against every entry, no early exit, so timing says nothing about position.
            for d in self._devices:
                if hmac.compare_digest(d["token_hash"], digest):
                    found = d
        return _public(found) if found else None

    def add(self, name: str) -> tuple[dict, str]:
        """Pair a new device; returns (its public entry, the plaintext token)."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = _utc_now()
        entry = {"id": secrets.token_hex(6), "name": clean_device_name(name),
                 "token_hash": hash_token(token), "created": now, "last_seen": now}
        with self._lock:
            self._commit(self._devices + [entry])
        return _public(entry), token

    def touch(self, device_id: str) -> None:
        with self._lock:
            if any(d["id"] == device_id for d in self._devices):
                self._commit([{**d, "last_seen": _utc_now()} if d["id"] == device_id else d
                              for d in self._devices])

    def rename(self, device_id: str, name: str) -> bool:
        with self._lock:
            if not any(d["id"] == device_id for d in self._devices):
                return False
            name = clean_device_name(name)
            self._commit([{**d, "name": name} if d["id"] == device_id else d for d in self._devices])
            return True

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            kept = [d for d in self._devices if d["id"] != device_id]
            if len(kept) == len(self._devices):
                return False
            self._commit(kept)
            return True

    def get(self, device_id: str) -> dict | None:
        with self._lock:
            return next((_public(d) for d in self._devices if d["id"] == device_id), None)

    def public_list(self) -> list[dict]:
        with self._lock:
            return [_public(d) for d in self._devices]


def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "token_hash"}


# ─── Pending pairing requests ──────────────────────────────────────────────────

class PairingRefused(Exception):
    """A pairing request that was not opened; .reason goes back to the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(eq=False)
class PendingPair:
    id: str
    ip: str
    name: str
    code: str
    deadline: float
    decision: asyncio.Future = field(repr=False)

    def remaining(self, now: float) -> float:
        return max(0.0, self.deadline - now)


class PairingRequests:
    """Open requests, waiting for Allow / Deny on the PC.

    One pending request per IP, at most PAIR_RATE_MAX opened per rolling
    minute (across all IPs, so nobody can flood the PC with prompts), and each
    expires after PAIR_TTL_S. Event-loop only: no locking.
    """

    def __init__(self, ttl: float = PAIR_TTL_S, rate_max: int = PAIR_RATE_MAX,
                 window: float = PAIR_RATE_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl, self.rate_max, self.window, self.clock = ttl, rate_max, window, clock
        self._pending: dict[str, PendingPair] = {}
        self._opened: collections.deque[float] = collections.deque()

    def open(self, ip: str, name: str) -> PendingPair:
        now = self.clock()
        while self._opened and now - self._opened[0] >= self.window:
            self._opened.popleft()
        if any(r.ip == ip for r in self._pending.values()):
            raise PairingRefused("busy")
        if len(self._opened) >= self.rate_max:
            raise PairingRefused("rate_limited")
        self._opened.append(now)
        req = PendingPair(id=secrets.token_hex(8), ip=ip, name=clean_device_name(name),
                          code=f"{secrets.randbelow(1_000_000):06d}", deadline=now + self.ttl,
                          decision=asyncio.get_running_loop().create_future())
        self._pending[req.id] = req
        return req

    def decide(self, req_id, allow: bool) -> bool:
        req = self._pending.get(req_id) if isinstance(req_id, str) else None
        if req is None or req.decision.done() or req.remaining(self.clock()) <= 0:
            return False
        req.decision.set_result(bool(allow))
        return True

    def close(self, req: PendingPair) -> None:
        self._pending.pop(req.id, None)

    def pending(self) -> list[PendingPair]:
        return list(self._pending.values())


# ─── Sockets that have not authenticated yet ──────────────────────────────────

class PendingSockets:
    """Slots for sockets between accept() and auth / pairing.

    Such a socket costs a task and memory for up to AUTH_TIMEOUT_S, or the
    pairing TTL, before it has proved anything; this caps how many anyone can
    hold open. Event-loop only: no locking.
    """

    def __init__(self, total: int = MAX_PENDING_SOCKETS, per_ip: int = MAX_PENDING_PER_IP) -> None:
        self.total, self.per_ip = total, per_ip
        self._by_ip: collections.Counter[str] = collections.Counter()

    def acquire(self, ip: str) -> bool:
        if sum(self._by_ip.values()) >= self.total or self._by_ip[ip] >= self.per_ip:
            return False
        self._by_ip[ip] += 1
        return True

    def release(self, ip: str) -> None:
        self._by_ip[ip] -= 1
        if self._by_ip[ip] <= 0:
            del self._by_ip[ip]


# ─── Host and Origin allowlists ────────────────────────────────────────────────

class OriginPolicy:
    """The Host headers and WebSocket origins this server answers to.

    Hosts: 127.0.0.1, localhost and this PC's LAN IPv4 addresses, on our port.
    A rebinding attack arrives with the attacker's host name, never with one
    of these. The LAN addresses are read at startup; an unknown IP literal
    triggers a re-read (rate-limited), so a DHCP change does not lock the
    tablet out until a restart.
    """

    def __init__(self, port: int, lan_ips_fn: Callable[[], Iterable[str]],
                 refresh_s: float = LAN_REFRESH_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.port = port
        self._lan_ips_fn = lan_ips_fn
        self._refresh_s = refresh_s
        self._clock = clock
        self._last_refresh = float("-inf")
        self._lan_ips: frozenset[str] = frozenset()

    def set_lan_ips(self, ips: Iterable[str]) -> None:
        self._lan_ips = frozenset(ips)
        self._last_refresh = self._clock()

    async def refresh(self) -> None:
        self.set_lan_ips(await asyncio.to_thread(lambda: list(self._lan_ips_fn())))

    def hosts(self) -> frozenset[str]:
        return frozenset(f"{h}:{self.port}" for h in (*LOOPBACK_NAMES, *self._lan_ips))

    def host_allowed(self, host: str | None) -> bool:
        return bool(host) and host.lower() in self.hosts()

    def wants_refresh(self, host: str | None) -> bool:
        """An IP literal we do not know yet, and the last re-read is old enough."""
        name, _, port = (host or "").rpartition(":")
        try:
            ipaddress.IPv4Address(name)
        except ValueError:
            return False
        return port == str(self.port) and self._clock() - self._last_refresh >= self._refresh_s

    def origin_allowed(self, origin: str | None) -> bool:
        if not origin:
            return False
        origin = origin.lower()
        return origin in APK_ORIGINS or origin in {f"http://{h}" for h in self.hosts()}

    def is_admin_origin(self, origin: str | None) -> bool:
        return bool(origin) and origin.lower() in {f"http://{n}:{self.port}" for n in LOOPBACK_NAMES}


# ─── Local control token ───────────────────────────────────────────────────────

class LocalControlToken:
    """A per-run secret in a 0600 file, for local processes only.

    /api/exit (installers) and /api/wakeup (a second instance) are reachable
    from loopback, where other OS users and every browser tab also live; the
    file is readable only by the user running the app.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._token = ""

    def rotate(self) -> None:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        try:
            write_private(self.path, token)
        except OSError as e:
            logger.error("Could not write %s: %s — /api/exit and /api/wakeup are disabled", self.path, e)
            return
        self._token = token

    def check(self, presented: str) -> bool:
        return bool(self._token) and hmac.compare_digest(presented.encode(), self._token.encode())


def read_local_token(data_dir: str) -> str:
    """The running instance's control token, or "" if it has not written one."""
    try:
        with open(os.path.join(data_dir, LOCAL_TOKEN_FILE), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""
