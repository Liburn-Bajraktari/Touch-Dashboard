"""
media.py — Media metadata for Touch Dashboard.

Sources:
  • Spotify  — Spotipy + SpotifyOAuth (all platforms, requires credentials)
  • MPRIS    — playerctl CLI (Linux only)
  • Windows Media Transport — winrt Windows.Media.Control (Windows 10+)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import sys
import time
import urllib.parse

logger = logging.getLogger(__name__)

# ── Spotify ────────────────────────────────────────────────────────────────────

def get_spotify_api_meta(sp_oauth, req_session=None) -> dict | None:
    """
    Return current Spotify playback as a media dict.
    Returns None if Spotify is not configured.
    Returns a dict with status="Auth_Required" if authentication is needed.
    """
    if sp_oauth is None:
        return None
    try:
        import spotipy  # type: ignore[import]
        token_info = sp_oauth.get_cached_token()
        if not token_info:
            return {
                "status": "Auth_Required",
                "artist": "", "title": "Spotify Not Authorized", "art_url": "",
            }
        kwargs: dict = {"auth": token_info["access_token"], "requests_timeout": 3}
        if req_session is not None:
            kwargs["requests_session"] = req_session
        sp = spotipy.Spotify(**kwargs)
        curr = sp.current_playback()
        if curr and curr.get("item"):
            item    = curr["item"]
            art_url = ""
            if item.get("album") and item["album"].get("images"):
                art_url = item["album"]["images"][0]["url"]
            return {
                "status": "Playing" if curr.get("is_playing") else "Paused",
                "artist": (item.get("artists") or [{}])[0].get("name", "Unknown"),
                "title":  item.get("name", "Unknown"),
                "art_url": art_url,
            }
    except Exception as e:
        logger.debug(f"Spotify API error: {e}")
    return None


# ── MPRIS (Linux) ─────────────────────────────────────────────────────────────

_last_mpris_title = ""
_last_mpris_path  = ""
_mpris_burst_active = False
_mpris_burst_start  = 0.0
_last_art_url = ""


def get_local_mpris_meta() -> dict:
    """Query playerctl for the currently playing track (Linux only)."""
    global _last_mpris_title, _last_mpris_path, _mpris_burst_active
    global _mpris_burst_start, _last_art_url

    _STOPPED = {"status": "Stopped", "artist": "", "title": "Nothing Playing", "art_url": ""}

    if not sys.platform.startswith("linux"):
        return _STOPPED

    try:
        import shutil
        if not shutil.which("playerctl"):
            return _STOPPED

        out = subprocess.check_output(
            ["playerctl", "--player=spotify,plasma-browser-integration,%any", "metadata", "--format",
             "{{status}}|||{{artist}}|||{{title}}|||{{mpris:artUrl}}"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode().strip()

        if not out:
            return _STOPPED

        parts = out.split("|||")
        status = parts[0].strip() if parts else "Stopped"
        if status not in ("Playing", "Paused"):
            return _STOPPED

        artist      = parts[1].strip() if len(parts) > 1 else "Unknown"
        title       = parts[2].strip() if len(parts) > 2 else "Unknown"
        raw_art_url = parts[3].strip() if len(parts) > 3 else ""
        current_song = f"{artist}-{title}"

        if current_song != _last_mpris_title:
            _last_mpris_title   = current_song
            _mpris_burst_active = True
            _mpris_burst_start  = time.time()
            _last_art_url       = "WAITING"

        if _mpris_burst_active:
            if raw_art_url.startswith("file://"):
                path = urllib.parse.unquote(raw_art_url.replace("file://", ""))
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    _last_mpris_path    = path
                    mod_time            = str(os.path.getmtime(path))
                    f_size              = str(os.path.getsize(path))
                    song_hash           = hashlib.md5(
                        (current_song + mod_time + f_size).encode()
                    ).hexdigest()
                    _last_art_url       = f"/api/local_art?h={song_hash}"
                    _mpris_burst_active = False
                elif time.time() - _mpris_burst_start > 6.0:
                    _last_art_url       = ""
                    _mpris_burst_active = False
            elif raw_art_url:
                _last_art_url       = raw_art_url
                _mpris_burst_active = False
            else:
                if time.time() - _mpris_burst_start > 6.0:
                    _last_art_url       = ""
                    _mpris_burst_active = False
        else:
            if raw_art_url.startswith("file://"):
                path = urllib.parse.unquote(raw_art_url.replace("file://", ""))
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    _last_mpris_path = path
                    mod_time         = str(os.path.getmtime(path))
                    f_size           = str(os.path.getsize(path))
                    song_hash        = hashlib.md5(
                        (current_song + mod_time + f_size).encode()
                    ).hexdigest()
                    _last_art_url = f"/api/local_art?h={song_hash}"
            elif raw_art_url and raw_art_url != _last_art_url:
                _last_art_url = raw_art_url

        return {"status": status, "artist": artist, "title": title, "art_url": _last_art_url}
    except Exception as e:
        logger.debug(f"MPRIS error: {e}")
        return _STOPPED


def get_last_mpris_art_path() -> str:
    """Return the filesystem path of the last-seen MPRIS album art."""
    return _last_mpris_path


def is_mpris_burst_active() -> bool:
    return _mpris_burst_active


# ── Windows Media Transport ────────────────────────────────────────────────────

_win_media_cache: dict = {}
_win_media_ts: float   = 0.0
_win_media_ttl: float  = 2.0   # seconds between Windows SMTCS queries

# Probe the actual winrt import once at module load so _WINRT_AVAILABLE is
# only True when the packages are genuinely installed and importable.
_WINRT_AVAILABLE = False
if sys.platform.startswith("win"):
    try:
        import winrt.windows.media.control as _winrt_probe  # type: ignore[import]
        del _winrt_probe
        _WINRT_AVAILABLE = True
    except Exception:
        _WINRT_AVAILABLE = False


async def _query_winrt_session() -> dict | None:
    """
    Async query of Windows.Media.Control SMTCS.
    winrt 3.x coroutines are natively awaitable — no run_in_executor needed.
    Returns a media dict or None if nothing is playing / winrt unavailable.
    """
    if not _WINRT_AVAILABLE:
        return None
    try:
        from winrt.windows.media.control import (   # type: ignore[import]
            GlobalSystemMediaTransportControlsSessionManager as SMTCSM,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlayStatus,
        )
        manager = await asyncio.wait_for(SMTCSM.request_async(), timeout=2.0)
        session = manager.get_current_session()
        if session is None:
            return None

        props = await asyncio.wait_for(
            session.try_get_media_properties_async(), timeout=2.0
        )
        pb_info = session.get_playback_info()
        status_val = pb_info.playback_status if pb_info else None

        # PlayStatus values: 0=Closed, 1=Opened, 2=Changing, 3=Stopped, 4=Playing, 5=Paused
        PLAYING = getattr(PlayStatus, "PLAYING", 4)
        PAUSED  = getattr(PlayStatus, "PAUSED",  5)

        if status_val not in (PLAYING, PAUSED):
            return None

        return {
            "status": "Playing" if status_val == PLAYING else "Paused",
            "artist": (props.artist or "Unknown") if props else "Unknown",
            "title":  (props.title  or "Unknown") if props else "Unknown",
            "art_url": "",
        }
    except asyncio.TimeoutError:
        logger.debug("Windows SMTCS query timed out")
        return None
    except Exception as e:
        logger.debug(f"Windows SMTCS error: {e}")
        return None


async def get_windows_media_meta_async() -> dict | None:
    """
    Async wrapper for Windows media queries.
    Caches results for _win_media_ttl seconds to avoid hammering the OS.
    """
    global _win_media_cache, _win_media_ts
    if not _WINRT_AVAILABLE:
        return None
    now = time.monotonic()
    if now - _win_media_ts < _win_media_ttl and _win_media_cache is not None:
        return _win_media_cache or None

    try:
        result = await _query_winrt_session()
    except Exception as e:
        logger.debug(f"Windows media async wrapper: {e}")
        result = None

    _win_media_cache = result or {}
    _win_media_ts    = now
    return result
