"""
media.py — Media sources and controls for Touch Dashboard.

Sources (each becomes a "player record", see _record()):
  • Spotify  — Spotipy + SpotifyOAuth (all platforms, requires credentials)
  • MPRIS    — mpris_monitor.py (D-Bus events), or playerctl polled each second
               when the monitor cannot run (Linux)
  • Windows Media Transport — winrt Windows.Media.Control (Windows 10+)

The server shows one record (pick_source) and sends every key to that same
source. Decisions that do not need a player live here so they can be tested:
the order players are picked in, the key layout (media_kind), what Previous
does (prev_plan) and covers sent as data: URIs (resolve_art).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)

# ── Player names ─────────────────────────────────────────────────────────────
# MPRIS bus names and Windows AppUserModelIDs → the name people know.
_PLAYER_NAMES = {
    "spotify": "Spotify", "firefox": "Firefox", "chromium": "Chromium",
    "chrome": "Chrome", "brave": "Brave", "msedge": "Edge", "vivaldi": "Vivaldi",
    "plasma-browser-integration": "Browser", "vlc": "VLC", "mpv": "mpv",
    "elisa": "Elisa", "strawberry": "Strawberry", "rhythmbox": "Rhythmbox",
    "microsoft.zunemusic": "Media Player", "tidal": "TIDAL", "deezer": "Deezer",
    "kdeconnect": "KDE Connect",
}
PLAYER_LABEL_MAX = 20


def app_of(raw: str) -> str:
    """'firefox.instance_1_23' / 'Spotify.exe' / 'Microsoft.ZuneMusic_8wek…!App' → 'firefox' …"""
    base = (raw or "").split("!")[0].split("_")[0].lower().removesuffix(".exe")
    return base if base in _PLAYER_NAMES else base.split(".")[0]


def _player_label(raw: str) -> str:
    """'firefox.instance_1_23' / 'Spotify.exe' / 'Microsoft.ZuneMusic_8wek…!App' → 'Firefox' …"""
    if not raw:
        return ""
    base = app_of(raw)
    if base in _PLAYER_NAMES:
        return _PLAYER_NAMES[base]
    # Opaque ids (Firefox's Windows AUMID is a hex hash) are not names.
    if base.isalpha() and len(base) <= PLAYER_LABEL_MAX:
        return base.capitalize()
    return ""


def player_name(raw: str, identity: str = "") -> str:
    """Our short name for a known app; otherwise the player's own (MPRIS
    Identity, e.g. 'Jellium Desktop'). The browser bridge's Identity names
    the browser, which beats 'Browser'."""
    app = app_of(raw)
    if app in _PLAYER_NAMES and app != "plasma-browser-integration":
        return _PLAYER_NAMES[app]
    identity = (identity or "").strip()
    if identity and len(identity) <= PLAYER_LABEL_MAX:
        return identity
    return _player_label(raw)


def _seconds(value, scale: float = 1.0) -> float | None:
    try:
        secs = float(value) / scale
    except (TypeError, ValueError):
        return None
    return round(secs, 1) if secs >= 0 else None


ALL_CAPS = {"prev": True, "next": True, "seek": True}


def _record(*, id: str, via: str, raw: str, name: str, status: str, title: str = "",
            artist: str = "", album: str = "", url: str = "", art: str = "",
            trackid: str = "", position: float | None = None, length: float | None = None,
            caps: dict | None = None, started: float = 0.0, **extra) -> dict:
    """One playing (or paused) source, the same shape whatever it came from."""
    return {
        "id": id, "via": via, "app": app_of(raw), "key": (raw or "").lower(), "name": name,
        "status": status, "title": title, "artist": artist, "album": album, "url": url,
        "art": art, "trackid": trackid, "position": position, "length": length or None,
        "caps": dict(caps or ALL_CAPS), "started": started, **extra,
    }


class _Starts:
    """When each source last started playing (monotonic), for pick_source().
    Event sources know it; polled ones get it from consecutive readings."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[str, float]] = {}

    def note(self, sid: str, status: str) -> float:
        prev_status, started = self._last.get(sid, ("", 0.0))
        if status == "Playing" and prev_status != "Playing":
            started = time.monotonic()
        self._last[sid] = (status, started)
        return started

    def forget(self, prefix: str, keep: set[str]) -> None:
        """Drop sources of one kind that are gone, so a return counts as a start."""
        for sid in [s for s in self._last if s.startswith(prefix) and s not in keep]:
            del self._last[sid]


_starts = _Starts()


# ── Picking a source ─────────────────────────────────────────────────────────

LIVE = ("Playing", "Paused")
BROWSERS = frozenset({
    "firefox", "chromium", "chrome", "google-chrome", "brave", "msedge", "vivaldi", "opera",
    "librewolf", "zen", "floorp", "waterfox", "epiphany", "falkon", "thorium",
    "plasma-browser-integration",
})
BROWSER_BRIDGE = "plasma-browser-integration"


# What a tab's title adds to the media's own: an unread count YouTube, Gmail
# or Discord put first ("(3) ", "(99+) ") or a playing mark, and the site's
# name last (" - YouTube", " | Twitch").
_TAB_COUNT = re.compile(r"^\s*(?:[(\[]\d+\+?[)\]]|[▶►])\s*")
_TAB_SITE = re.compile(r"\s+[-–—|·]\s+([^-–—|·]{1,40})$")
SAME_LENGTH_S = 2.0


def _bare_title(title: str) -> str:
    return _TAB_COUNT.sub("", title).strip().casefold()


def _without_site(bare: str, url: str) -> str | None:
    """A bare tab title without the site's name at its end, when the page's
    host carries that name ("YouTube" on www.youtube.com); else None."""
    site = _TAB_SITE.search(bare)
    host = (urllib.parse.urlsplit(url).hostname or "") if url else ""
    words = re.findall(r"\w+", site.group(1)) if site else []
    return bare[:site.start()] if host and words and all(w in host for w in words) else None


def _title_forms(title: str, url: str) -> set[str]:
    """The ways a tab's title can read: as it is, without the count, and
    without the site's name, but only a name the page's host carries
    ("YouTube" on www.youtube.com), so "Song - Remix" stays itself."""
    bare = _bare_title(title)
    forms = {title.strip().casefold(), bare, _without_site(bare, url) or ""}
    return forms - {""}


def _same_title(a: dict, b: dict, url: str) -> bool:
    """One tab's title, perhaps dressed as the tab's: either copy may fall
    back to the page's title when it cannot read the page's media metadata
    (the bridge does on YouTube, giving "(3) Title - YouTube")."""
    if not a["title"] or not b["title"]:
        return False
    return (_bare_title(b["title"]) in _title_forms(a["title"], url)
            or _bare_title(a["title"]) in _title_forms(b["title"], url))


def _copy_of(copy: dict, own: dict) -> bool:
    """copy republishes own's media. KDE's browser bridge names the browser
    process whose tab it shows (kde:pid), the process behind the browser's own
    player: that settles it whatever the titles say (an ad, a track change).
    Without processes (the playerctl poll, a sandboxed browser) the bridge and
    a browser must show one title and length."""
    if copy["id"] == own["id"]:
        return False
    if copy.get("media_pid") and copy["media_pid"] == own.get("pid"):
        return True
    if copy["app"] != BROWSER_BRIDGE or own["app"] not in BROWSERS or own["app"] == BROWSER_BRIDGE:
        return False
    a, b = copy["length"], own["length"]
    return _same_title(copy, own, copy["url"]) and not (a and b and abs(a - b) > SAME_LENGTH_S)


def _is_page_title(p: dict, other: dict, url: str) -> bool:
    """p's title is other's with a tab's dressing around it."""
    return (p["title"].casefold() != other["title"].casefold()
            and _bare_title(other["title"]) in _title_forms(p["title"], url))


def _lend(own: dict, copy: dict) -> dict:
    """own, with what the copy knows about the same item that own does not."""
    merged = dict(own)
    merged["started"] = max(own["started"], copy["started"])
    # The bridge's name is the browser's (Chrome, Brave), where own's bus name says Chromium.
    if copy["app"] == BROWSER_BRIDGE and copy["name"] != _PLAYER_NAMES[BROWSER_BRIDGE]:
        merged["name"] = copy["name"]
    url = copy["url"] or own["url"]
    # One page, and own has only the tab's title: Firefox shows Twitch as
    # "Emiru - Twitch" while the bridge reads the stream's title and streamer.
    page_title = (bool(own["url"]) and own["url"] == copy["url"] and bool(copy["title"])
                  and _without_site(_bare_title(own["title"]), url) is not None
                  and _without_site(_bare_title(copy["title"]), url) is None)
    if not (page_title or _same_title(own, copy, url)):
        return merged          # the copy is on another track for now: nothing of it fits
    if page_title or _is_page_title(own, copy, url):
        # own has only the tab's title; the copy read the page's metadata.
        for field in ("title", "artist", "album", "art"):
            merged[field] = copy[field] or own[field]
    # Not the track id: MPRIS SetPosition needs own's.
    for field in ("url", "art", "album"):
        if not merged[field] and copy[field]:
            merged[field] = copy[field]
    for field in ("length", "position"):
        if merged[field] is None and copy[field] is not None:
            merged[field] = copy[field]
    return merged


def dedupe(players: list[dict]) -> list[dict]:
    """A player that republishes another's media is dropped: KDE's browser
    bridge copies the tab a browser already publishes itself. The browser's
    own player stays (its keys, its track id) and takes what the copy knows
    that it does not: the page url, a cover, the album, a length, and the
    page's metadata when it only has the tab's title. A bridge with no
    browser player behind it (one that publishes none) stays a source."""
    copies = [p for p in players if p["app"] == BROWSER_BRIDGE or p.get("media_pid")]
    if not copies:
        return players
    replace: dict[str, dict] = {}
    drop: set[str] = set()
    for c in copies:
        # A browser player that stopped does not stand in for a copy still playing or paused.
        own = next((p for p in players if p["id"] not in drop and _copy_of(c, p)
                    and (p["status"] in LIVE or c["status"] not in LIVE)), None)
        if own is None:
            continue
        replace[own["id"]] = _lend(replace.get(own["id"], own), c)
        drop.add(c["id"])
    return [replace.get(p["id"], p) for p in players if p["id"] not in drop]


# KDE's bridge can miss a tab closing (closing its window): its player then
# stays Paused on that tab's item for good. The browser's own player does not:
# Firefox drops its player when the tab closes or the media ends (it keeps a
# paused tab for 24 h), Chrome's goes Stopped. Seen with a YouTube video that
# played to its end in a window then closed: Firefox's player left at the end,
# the bridge's stayed Paused at 11:01 of 11:01, and the card kept it.
LET_GO_S = 3.0          # a browser player may leave and come back between two items
END_SLACK_S = 1.0


def _ended(p: dict) -> bool:
    return (p["status"] != "Playing" and bool(p["length"]) and p["position"] is not None
            and p["position"] >= p["length"] - END_SLACK_S)


class _Copies:
    """Copies standing alone with an item their browser let go (local_sources)."""

    def __init__(self) -> None:
        self._carried: dict[str, tuple] = {}   # copy id → its item when the browser's own player last had it
        self._alone: dict[str, tuple] = {}     # copy id → ((item, status), since): let go, not dropped yet

    def settle(self, players: list[dict], kept: list[dict], now: float) -> list[dict]:
        kept_ids = {p["id"] for p in kept}
        present, stale = set(), set()
        for c in players:
            if not (c["app"] == BROWSER_BRIDGE or c.get("media_pid")):
                continue
            present.add(c["id"])
            item = (c["title"], c["url"])
            if c["id"] not in kept_ids:          # dedupe() merged it: the browser carries it
                self._carried[c["id"]] = item
                self._alone.pop(c["id"], None)
                continue
            if self._carried.get(c["id"]) != item:
                self._carried.pop(c["id"], None)  # another item since (or never carried)
                if not _ended(c):
                    self._alone.pop(c["id"], None)
                    continue
            # A change (resumed in the tab) waits for the browser's player again.
            state = (item, c["status"])
            held = self._alone.get(c["id"])
            if held is None or held[0] != state:
                held = self._alone[c["id"]] = (state, now)
            if now - held[1] >= LET_GO_S:
                stale.add(c["id"])
        for known in (self._carried, self._alone):
            for sid in [s for s in known if s not in present]:
                del known[sid]
        return [p for p in kept if p["id"] not in stale]


_copies = _Copies()


def local_sources(players: list[dict], now: float | None = None) -> list[dict]:
    """dedupe(), less the copies whose browser let their item go: the
    browser's own player carried it and no longer does, or it sits at its
    end. A copy no browser player ever carried stays, as in dedupe().
    Live streams come out with live=True and no length (is_live)."""
    kept = _copies.settle(players, dedupe(players), time.monotonic() if now is None else now)
    return [{**p, "live": True, "length": None} if is_live(p) else p for p in kept]


def _paused_rank(p: dict) -> tuple:
    # Never played since we started watching: Spotify, then the browser bridge.
    return (p["started"], p["app"] == "spotify", p["app"] == BROWSER_BRIDGE)


def pick_source(players: list[dict], pin: dict | None = None) -> dict | None:
    """The source the card shows and the keys control: a pinned one while it
    exists, else whatever started playing most recently, else the paused
    one that played last."""
    live = [p for p in players if p["status"] in LIVE]
    if pin:
        for p in live:
            if p["id"] == pin["id"]:
                return p
    playing = [p for p in live if p["status"] == "Playing"]
    if playing:
        return max(playing, key=lambda p: p["started"])
    return max(live, key=_paused_rank) if live else None


def resolve_pin(players: list[dict], pin: dict | None) -> dict | None:
    """A pick holds until that player closes or another one starts playing
    after it was made (the user started something else)."""
    if not pin:
        return None
    ids = {p["id"] for p in players if p["status"] in LIVE}
    if pin["id"] not in ids:
        return None
    if any(p["id"] != pin["id"] and p["status"] == "Playing" and p["started"] > pin["at"]
           for p in players):
        return None
    return pin


def merge_spotify(players: list[dict], spot: dict | None) -> list[dict]:
    """Spotify's Web API and the local Spotify app describe the same playback.
    When the app runs here, keep its record (instant, controllable without
    Premium) with the API's cover, episode flag and track id; otherwise the
    API record is a source of its own (a phone, a speaker).

    After a track change the API reports the old track for up to a few
    seconds. While the app is playing or paused here, a title that differs is
    that lag, not another device: the app's record stands alone (api_behind,
    keeping only whether the API can like) instead of the old track joining
    as a second source, which, newer to start, took the card and the keys."""
    if not spot or spot.get("status") not in LIVE:
        return players
    title = spot["title"].casefold()
    for i, p in enumerate(players):
        if p["app"] == "spotify" and p["title"].casefold() == title:
            merged = {**p, "episode": spot.get("episode", False),
                      "item_id": spot.get("item_id"), "item_type": spot.get("item_type"),
                      "can_like": spot.get("can_like", False)}
            if spot.get("art"):
                merged["art"] = spot["art"]
            return players[:i] + [merged] + players[i + 1:]
    for i, p in enumerate(players):
        if p["app"] == "spotify" and p["status"] in LIVE:
            behind = {**p, "can_like": spot.get("can_like", False), "api_behind": True}
            return players[:i] + [behind] + players[i + 1:]
    return players + [spot]


# ── Services and live streams ────────────────────────────────────────────────
# A browser plays a service: the chip names the service by the page's host
# (first match wins, so YouTube Music comes before YouTube), and the browser
# only when the host is none of these.
SERVICES = (
    ("music.youtube.com", "YouTube Music"), ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("twitch.tv", "Twitch"), ("kick.com", "Kick"), ("netflix.com", "Netflix"),
    ("primevideo.com", "Prime Video"), ("disneyplus.com", "Disney+"), ("max.com", "Max"),
    ("hbomax.com", "Max"), ("hulu.com", "Hulu"), ("crunchyroll.com", "Crunchyroll"),
    ("tv.apple.com", "Apple TV"), ("music.apple.com", "Apple Music"), ("open.spotify.com", "Spotify"),
    ("soundcloud.com", "SoundCloud"), ("bandcamp.com", "Bandcamp"), ("deezer.com", "Deezer"),
    ("tidal.com", "TIDAL"), ("music.amazon.com", "Amazon Music"), ("vimeo.com", "Vimeo"),
    ("dailymotion.com", "Dailymotion"), ("mixcloud.com", "Mixcloud"),
    ("paramountplus.com", "Paramount+"), ("peacocktv.com", "Peacock"), ("rumble.com", "Rumble"),
    ("app.plex.tv", "Plex"), ("pandora.com", "Pandora"),
)


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "") if url else ""
    except ValueError:
        return ""


def service_of(url: str) -> str:
    """'https://www.twitch.tv/emiru' → 'Twitch'; '' for a site that is none of SERVICES."""
    host = _host(url)
    return next((name for domain, name in SERVICES if host == domain or host.endswith("." + domain)), "")


def source_name(p: dict) -> str:
    """The name the chip shows: the service a browser plays, else the player's own."""
    return (service_of(p["url"]) or p["name"]) if p["app"] in BROWSERS else p["name"]


# A browser reports a live stream's length as Infinity (Firefox and Chrome: the
# largest int64 of µs, ~2.6 billion hours; the page showed "-2562047…"), or
# none at all (KDE's bridge). A channel's own page on Twitch or Kick is live;
# its videos have a length.
LIVE_LENGTH_S = 10 * 365 * 86400.0          # longer than any recording
_LIVE_PAGES = {"twitch.tv": re.compile(r"/([A-Za-z0-9_]{2,25})/?"),
               "kick.com": re.compile(r"/([A-Za-z0-9_-]{2,40})/?")}
_NOT_CHANNELS = frozenset({"directory", "videos", "video", "settings", "search", "downloads", "jobs",
                           "p", "turbo", "subscriptions", "inventory", "wallet", "drops", "friends",
                           "messages", "following", "categories", "browse", "clips"})


def _channel_page(url: str) -> tuple[str, str] | None:
    """(site, channel) for a channel's own page on Twitch or Kick."""
    host = _host(url).removeprefix("www.").removeprefix("m.")
    pattern = _LIVE_PAGES.get(host)
    m = pattern.fullmatch(urllib.parse.urlsplit(url).path) if pattern else None
    if not m or m.group(1).lower() in _NOT_CHANNELS:
        return None
    return host, m.group(1)


def is_live(p: dict) -> bool:
    if p["length"] is not None:
        return p["length"] >= LIVE_LENGTH_S
    return p["app"] in BROWSERS and _channel_page(p["url"]) is not None


class _LiveEdge:
    """Where the live edge is in the shown stream's own timeline. It moves
    with the clock, playing or not; the position falls behind it when the
    stream stalls, is paused or scrubbed back. A position past it (the player
    caught up, or went back to live) moves it there."""

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._edge = self._from = self._at = 0.0

    def note(self, p: dict, now: float) -> dict | None:
        pos = p["position"]
        if pos is None:
            self._key = None
            return None
        key = (p["id"], p["url"] or p["title"])
        if key != self._key:
            self._key, self._edge, self._from = key, pos, pos
        else:
            self._edge = max(self._edge + (now - self._at), pos)
            self._from = min(self._from, pos)
        self._at = now
        return {"edge": round(self._edge, 1), "from": round(self._from, 1)}


_live_edge = _LiveEdge()


def live_edge(p: dict, now: float | None = None) -> dict:
    """sys_data's `live` for a live source: {edge, from} in its position's
    timeline (from: the earliest position seen), or {} without a position."""
    return _live_edge.note(p, time.monotonic() if now is None else now) or {}


# ── Key layout ───────────────────────────────────────────────────────────────

LONG_FORM_S = 600       # 10 min: episodes, films, long videos, mixes
MUSIC_SITES = ("music.youtube.com", "open.spotify.com", "soundcloud.com", "bandcamp.com",
               "music.apple.com", "tidal.com", "deezer.com", "music.amazon.com")
# Players that only play video. Matched within the bus name or AUMID, since
# some are reverse-DNS (io.github.celluloid_player.Celluloid).
VIDEO_APPS = ("stremio", "freetube", "celluloid", "haruna", "totem", "showtime",
              "smplayer", "plexhtpc")


def _is_music_site(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "") if url else ""
    return any(host == s or host.endswith("." + s) for s in MUSIC_SITES)


def media_kind(p: dict) -> str:
    """'long' (5 keys: ⏮ ↺10 ⏯ ↻30 ⏭) or 'music' (3 keys). Long needs a
    seekable source and something that says long-form or video: no per-app
    code, so Jellyfin or Plex music keeps 3 keys and their episodes get 5."""
    if not p["caps"].get("seek"):
        return "music"
    if p.get("episode") or p.get("video"):
        return "long"
    if any(v in p["key"] for v in VIDEO_APPS):
        return "long"
    if p["app"] in BROWSERS and not _is_music_site(p["url"]) and not p["album"]:
        return "long"    # a page with no album: YouTube, Twitch, a video site
    if p["length"] and p["length"] >= LONG_FORM_S:
        return "long"
    return "music"


# ── Previous ─────────────────────────────────────────────────────────────────

RESTART_AFTER_S = 3.0


def prev_plan(position: float | None, caps: dict, live: bool = False) -> str:
    """'restart' or 'prev'. Like Spotify's own key: past 3 s Previous goes
    back to the start, a second press goes to the previous item. A player
    that cannot go back (a single video, Jellyfin's first episode) restarts.
    A live stream has no start to go back to."""
    if live:
        return "prev"
    if caps.get("seek") and position is not None and position > RESTART_AFTER_S:
        return "restart"
    if not caps.get("prev") and caps.get("seek"):
        return "restart"
    return "prev"


def public_media(p: dict, art_url: str) -> dict:
    """The record as sys_data's `spotify` field (older pages read the first
    seven keys only)."""
    return {
        "status": p["status"], "artist": p["artist"], "title": p["title"], "art_url": art_url,
        "player": source_name(p), "position": p["position"], "length": p["length"],
        "caps": p["caps"], "kind": media_kind(p),
    }


# ── Covers ───────────────────────────────────────────────────────────────────
# A file:// cover is served from disk by /api/local_art; a data: URI (Jellium
# sends a 384 KB one) is decoded once per track and served from memory, so it
# no longer rides in every sys_data. http(s) covers go to the page as they are.

COVER_MAX_BYTES = 4 * 1024 * 1024
COVER_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")   # no SVG: it can carry script
ART_WAIT_S = 6.0          # a player may write the cover file, or name it, after the title

_last_mpris_path = ""
_cover: tuple = ("", "", b"", "")       # (key, h, data, mime); swapped whole, read by the art route
_art_wait = {"key": "", "since": 0.0, "active": False}
_full_art = {"key": "", "raw": ""}      # a poll's cut-short cover URL, fetched whole once per track


def _art_key(p: dict) -> str:
    return f"{p['id']}|{p['trackid']}|{p['artist']}-{p['title']}"


def _data_cover(key: str, uri: str) -> str:
    global _cover
    head, _, b64 = uri.partition(",")
    mime = head[5:].split(";")[0].lower()
    if mime not in COVER_TYPES or ";base64" not in head or len(b64) > COVER_MAX_BYTES * 4 // 3 + 4:
        return ""
    try:
        data = base64.b64decode(b64)
    except (binascii.Error, ValueError):
        return ""
    h = hashlib.md5(data).hexdigest()
    _cover = (key, h, data, mime)
    return f"/api/local_art?h={h}"


def get_cover(h: str) -> tuple[bytes, str] | None:
    """The in-memory cover with this hash, as (bytes, mime)."""
    key, ch, data, mime = _cover
    return (data, mime) if h and h == ch else None


# Twitch gives a stream no cover: neither the browser's player nor KDE's
# bridge has one. Its channel page's og:image is the streamer's profile
# picture. Fetched once per channel in the background (the tick never waits)
# from a URL built here, not the page's own, and served from memory like a
# data: cover. Only images on Twitch's CDN, checked by their first bytes.
PAGE_COVER_HOSTS = {"twitch.tv": ".jtvnw.net"}
PAGE_HEAD_MAX = 512 * 1024      # og:image is in the page's <head>
PAGE_COVER_TIMEOUT_S = 5
PAGE_COVER_RETRY_S = 300.0
PAGE_COVER_KEEP = 32
_OG_IMAGE = re.compile(rb"""<meta\s[^>]*?(?:property|name)=["']og:image["'][^>]*>""", re.I)
_CONTENT = re.compile(rb"""content=["']([^"']+)["']""", re.I)
_page_covers: dict[str, tuple] = {}     # page → ("busy",) | ("failed", at) | (h, data, mime)
_page_lock = threading.Lock()


def cover_page(p: dict) -> str:
    """The channel page whose picture stands in for a missing cover, or ''."""
    page = _channel_page(p["url"]) if p["app"] in BROWSERS else None
    if not page or page[0] not in PAGE_COVER_HOSTS:
        return ""
    return f"https://www.{page[0]}/{page[1].lower()}"


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _get_capped(session, url: str, cap: int) -> bytes:
    with session.get(url, timeout=PAGE_COVER_TIMEOUT_S, stream=True,
                     headers={"User-Agent": "Touch-Dashboard"}) as r:
        r.raise_for_status()
        data = b""
        for chunk in r.iter_content(64 * 1024):
            data += chunk
            if len(data) > cap:
                break
        return data


def _load_page_cover(page: str) -> None:
    import requests  # type: ignore[import]
    got: tuple = ("failed", time.monotonic())
    try:
        with requests.Session() as session:
            m = _OG_IMAGE.search(_get_capped(session, page, PAGE_HEAD_MAX))
            content = _CONTENT.search(m.group(0)) if m else None
            img = content.group(1).decode("ascii", "replace").replace("&amp;", "&") if content else ""
            cdn = PAGE_COVER_HOSTS[_host(page).removeprefix("www.")]
            if urllib.parse.urlsplit(img).scheme != "https" or not _host(img).endswith(cdn):
                raise ValueError(f"no picture on {cdn} in the page ({img[:80]!r})")
            data = _get_capped(session, img, COVER_MAX_BYTES)
            mime = _image_type(data)
            if len(data) > COVER_MAX_BYTES or not mime:
                raise ValueError("not a picture we serve")
            got = (hashlib.md5(data).hexdigest(), data, mime)
    except Exception as e:   # network, Twitch's page changed: no cover, try again later
        logger.info(f"Cover from {page}: {e}")
    with _page_lock:
        _page_covers[page] = got


def _page_cover(page: str) -> tuple | None:
    """(h, data, mime) once fetched; None while it loads, or after it failed."""
    with _page_lock:
        got = _page_covers.get(page)
        if got and len(got) == 3:
            return got
        if got and (got[0] == "busy" or time.monotonic() - got[1] < PAGE_COVER_RETRY_S):
            return None
        while len(_page_covers) >= PAGE_COVER_KEEP:
            del _page_covers[next(iter(_page_covers))]
        _page_covers[page] = ("busy",)
    threading.Thread(target=_load_page_cover, args=(page,), name="page-cover", daemon=True).start()
    return None


def resolve_art(p: dict) -> str:
    """The cover URL for the page: '' for none, 'WAITING' while a new track's
    cover may still appear (the page keeps the previous one meanwhile)."""
    global _last_mpris_path, _cover
    raw, key = p.get("art") or "", _art_key(p)
    if raw.startswith("data:") and _cover[0] == key:
        _art_wait["active"] = False
        return f"/api/local_art?h={_cover[1]}"
    # Cut short by the playerctl poll (trunc() adds no ellipsis at this length).
    if p.get("via") == "playerctl" and (len(raw) >= POLL_CUT or raw.endswith("…")):
        if _full_art["key"] != key:
            _full_art.update(key=key, raw=_playerctl_art(p.get("instance", "")))
        raw = _full_art["raw"]
    page = "" if raw else cover_page(p)
    got = _page_cover(page) if page else None
    if got:
        _cover = (key, *got)
        _art_wait["active"] = False
        return f"/api/local_art?h={got[0]}"
    if raw.startswith("data:"):
        _art_wait["active"] = False
        return _data_cover(key, raw)
    if raw.startswith("file://"):
        path = urllib.parse.unquote(urllib.parse.urlsplit(raw).path)
        try:
            st = os.stat(path)
        except OSError:
            st = None
        if st and st.st_size > 0:
            _art_wait["active"] = False
            _last_mpris_path = path
            song = f"{p['artist']}-{p['title']}"
            return "/api/local_art?h=" + hashlib.md5(
                (song + str(st.st_mtime) + str(st.st_size)).encode()).hexdigest()
    elif raw.startswith(("http://", "https://")):
        _art_wait["active"] = False
        return raw
    # No cover yet, or its file is not written yet.
    now = time.monotonic()
    if _art_wait["key"] != key:
        _art_wait.update(key=key, since=now)
    _art_wait["active"] = now - _art_wait["since"] < ART_WAIT_S
    return "WAITING" if _art_wait["active"] else ""


def get_last_mpris_art_path() -> str:
    """Return the filesystem path of the last-seen MPRIS album art."""
    return _last_mpris_path


def is_mpris_burst_active() -> bool:
    """A new track's cover is still expected: poll fast until it shows."""
    return _art_wait["active"]


# ── MPRIS from mpris_monitor (events) ────────────────────────────────────────

MPRIS_PREFIX = "org.mpris.MediaPlayer2."


def from_monitor(players: list[dict]) -> list[dict]:
    """mpris_monitor.MprisMonitor.players() → records."""
    out = []
    for m in players:
        raw = m["bus"].removeprefix(MPRIS_PREFIX)
        out.append(_record(
            id="mpris:" + raw, via="mpris", raw=raw, name=player_name(raw, m["identity"]),
            status=m["status"], title=m["title"], artist=m["artist"], album=m["album"],
            url=m["url"], art=m["art"], trackid=m["trackid"], position=m["position"],
            length=m["length"], caps=m["caps"], started=m["started"], bus=m["bus"],
            pid=m.get("pid"), media_pid=m.get("media_pid"),
            episode="/episode/" in m["trackid"] or "/episode/" in m["url"]))
    return out


# ── MPRIS from playerctl (fallback poll, Linux) ──────────────────────────────

# shutil.which() walks $PATH; this runs every second, so resolve it once and
# only look again (once a minute) while playerctl is missing.
PLAYERCTL_RECHECK = 60.0
_playerctl: dict = {"path": None, "checked": float("-inf")}

# Every player in one process. Covers and urls are cut short: a data: cover is
# fetched whole once per track (resolve_art). trunc() needs playerctl 2.4.
POLL_CUT = 512
_POLL_FIELDS = ["{{playerInstance}}", "{{status}}", "{{artist}}", "{{title}}", "{{xesam:album}}",
                f"{{{{trunc(xesam:url, {POLL_CUT})}}}}", f"{{{{trunc(mpris:artUrl, {POLL_CUT})}}}}",
                "{{position}}", "{{mpris:length}}", "{{mpris:trackid}}"]
_POLL_FORMATS = ["|||".join(_POLL_FIELDS),
                 "|||".join(re.sub(r"trunc\((.*), \d+\)", r"\1", f) for f in _POLL_FIELDS)]
_poll = {"format": 0}
CAPS_REFRESH_S = 15.0
_caps_cache: dict[str, tuple[str, float, dict]] = {}   # instance → (track, read at, caps)
_identity_cache: dict[str, str] = {}


def _playerctl_path() -> str | None:
    now = time.monotonic()
    if _playerctl["path"] is None and now - _playerctl["checked"] >= PLAYERCTL_RECHECK:
        _playerctl["path"] = shutil.which("playerctl")
        _playerctl["checked"] = now
    return _playerctl["path"]


def _bus_props(instance: str, iface: str, props: list[str]) -> list[str] | None:
    """Raw property values through busctl (one process), else gdbus."""
    bus = MPRIS_PREFIX + instance
    if shutil.which("busctl"):
        try:
            out = subprocess.check_output(
                ["busctl", "--user", "get-property", bus, "/org/mpris/MediaPlayer2", iface, *props],
                stderr=subprocess.DEVNULL, timeout=1).decode().splitlines()
            return out if len(out) == len(props) else None
        except (subprocess.SubprocessError, OSError):
            return None
    if shutil.which("gdbus"):
        try:
            return [subprocess.check_output(
                ["gdbus", "call", "--session", "--dest", bus, "--object-path", "/org/mpris/MediaPlayer2",
                 "--method", "org.freedesktop.DBus.Properties.Get", iface, prop],
                stderr=subprocess.DEVNULL, timeout=1).decode().strip() for prop in props]
        except (subprocess.SubprocessError, OSError):
            return None
    return None


def _parse_bool(raw: str) -> bool:
    # busctl: 'b true'; gdbus: '(<true>,)'
    return "true" in raw


def _poll_caps(instance: str, track: str) -> dict:
    cached = _caps_cache.get(instance)
    now = time.monotonic()
    if cached and cached[0] == track and now - cached[1] < CAPS_REFRESH_S:
        return cached[2]
    raw = _bus_props(instance, "org.mpris.MediaPlayer2.Player", ["CanGoPrevious", "CanGoNext", "CanSeek"])
    caps = dict(zip(("prev", "next", "seek"), map(_parse_bool, raw))) if raw else dict(ALL_CAPS)
    _caps_cache[instance] = (track, now, caps)
    return caps


def _poll_identity(instance: str) -> str:
    if instance not in _identity_cache:
        raw = _bus_props(instance, "org.mpris.MediaPlayer2", ["Identity"])
        # busctl: 's "Jellium Desktop"'; gdbus: "(<'Jellium Desktop'>,)"
        text = raw[0] if raw else ""
        for lead, trail in (('s "', '"'), ("(<'", "'>,)")):
            if text.startswith(lead) and text.endswith(trail):
                text = text[len(lead):-len(trail)]
                break
        else:
            text = ""
        _identity_cache[instance] = text
    return _identity_cache[instance]


def _playerctl_art(instance: str) -> str:
    playerctl = _playerctl_path()
    if not (playerctl and instance):
        return ""
    try:
        return subprocess.check_output([playerctl, "-p", instance, "metadata", "mpris:artUrl"],
                                       stderr=subprocess.DEVNULL, timeout=2).decode().strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def parse_poll_line(line: str) -> dict | None:
    parts = line.split("|||")
    if len(parts) != len(_POLL_FIELDS):
        return None
    instance, status, artist, title, album, url, art, pos, length, trackid = (s.strip() for s in parts)
    trackid = trackid.strip("'")     # playerctl quotes object paths
    if not instance:
        return None
    sid = "playerctl:" + instance
    return _record(
        id=sid, via="playerctl", raw=instance, name="", status=status, title=title,
        artist=artist, album=album, url=url.removesuffix("…"), art=art, trackid=trackid,
        position=_seconds(pos, 1e6), length=_seconds(length, 1e6), instance=instance,
        episode="/episode/" in trackid or "/episode/" in url)


def poll_mpris_players() -> list[dict]:
    """Every MPRIS player, read by one playerctl process (Linux fallback)."""
    if not sys.platform.startswith("linux"):
        return []
    playerctl = _playerctl_path()
    if not playerctl:
        return []
    while True:
        try:
            r = subprocess.run(
                [playerctl, "-a", "metadata", "--format", _POLL_FORMATS[_poll["format"]]],
                capture_output=True, timeout=2)
        except (subprocess.SubprocessError, OSError) as e:
            logger.debug(f"playerctl poll: {e}")
            return []
        if b"unknown template function" in r.stderr and _poll["format"] + 1 < len(_POLL_FORMATS):
            _poll["format"] += 1     # playerctl before 2.4: no trunc()
            continue
        break
    out = r.stdout.decode(errors="replace")   # empty (exit 1) when no player runs
    players = []
    for line in out.splitlines():
        p = parse_poll_line(line)
        if p is None or p["status"] not in LIVE:
            continue
        p["name"] = player_name(p["instance"], _poll_identity(p["instance"]))
        p["caps"] = _poll_caps(p["instance"], p["trackid"] or p["title"])
        p["started"] = _starts.note(p["id"], p["status"])
        players.append(p)
    _starts.forget("playerctl:", {p["id"] for p in players})
    return players


def playerctl_command(instance: str, *args: str) -> bool:
    playerctl = _playerctl_path()
    if not playerctl:
        return False
    cmd = [playerctl, *(["-p", instance] if instance else []), *args]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=2).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def playerctl_position(instance: str) -> float | None:
    playerctl = _playerctl_path()
    if not (playerctl and instance):
        return None
    try:
        out = subprocess.check_output([playerctl, "-p", instance, "position"],
                                      stderr=subprocess.DEVNULL, timeout=2).decode()
    except (subprocess.SubprocessError, OSError):
        return None
    return _seconds(out.strip())


# ── Spotify ────────────────────────────────────────────────────────────────────

SPOTIFY_ID = "spotify:api"
LIBRARY_SCOPE = "user-library-modify"
# For Settings → Spotify: state off | required | ok, and whether the sign-in
# includes the library scopes (the like key). Set by each Web API poll.
spotify_auth = {"state": "off", "likes": False}


def spotify_token(sp_oauth) -> dict | None:
    """The cached token, refreshed when expired, whatever scopes it carries.

    spotipy's get_cached_token() refuses a token that lacks any scope the app
    now asks for, which would sign everyone out of playback because the like
    key added two. Its refresh also stamps the requested scopes on the token,
    so the granted ones are put back (they do not change on a refresh)."""
    if sp_oauth is None:
        return None
    try:
        info = sp_oauth.cache_handler.get_cached_token()
        if not info:
            return None
        if sp_oauth.is_token_expired(info):
            granted = info.get("scope", "")
            info = sp_oauth.refresh_access_token(info["refresh_token"])
            info["scope"] = granted
            sp_oauth.cache_handler.save_token_to_cache(info)
        return info
    except Exception as e:   # network, a revoked refresh token
        logger.debug(f"Spotify token: {e}")
        return None


def spotify_can_like(info: dict | None) -> bool:
    return bool(info) and LIBRARY_SCOPE in str(info.get("scope", "")).split()


def spotify_client(info: dict, req_session=None):
    import spotipy  # type: ignore[import]
    kwargs: dict = {"auth": info["access_token"], "requests_timeout": 3}
    if req_session is not None:
        kwargs["requests_session"] = req_session
    return spotipy.Spotify(**kwargs)


def spotify_caps(curr: dict) -> dict:
    """Spotify's `actions` lists what is disallowed, either inside a
    `disallows` object (older responses) or as flags of its own."""
    actions = curr.get("actions") or {}
    dis = actions.get("disallows", actions)
    return {"prev": not dis.get("skipping_prev"), "next": not dis.get("skipping_next"),
            "seek": not dis.get("seeking")}


def spotify_record(curr: dict | None, can_like: bool = False) -> dict | None:
    """A current_playback() reply (tracks and episodes) → a record."""
    if not curr or not curr.get("item"):
        return None
    item = curr["item"]
    episode = (curr.get("currently_playing_type") or item.get("type")) == "episode"
    if episode:
        show = item.get("show") or {}
        artist, album = show.get("name", ""), ""
        images = item.get("images") or show.get("images") or []
    else:
        album_obj = item.get("album") or {}
        artist = (item.get("artists") or [{}])[0].get("name", "Unknown")
        album, images = album_obj.get("name", ""), album_obj.get("images") or []
    status = "Playing" if curr.get("is_playing") else "Paused"
    return _record(
        id=SPOTIFY_ID, via="spotify_api", raw="spotify", name="Spotify", status=status,
        title=item.get("name", "Unknown"), artist=artist, album=album,
        art=images[0]["url"] if images else "", trackid=item.get("uri", ""),
        position=_seconds(curr.get("progress_ms"), 1000),
        length=_seconds(item.get("duration_ms"), 1000), caps=spotify_caps(curr),
        started=_starts.note(SPOTIFY_ID, status), episode=episode, item_id=item.get("id"),
        item_type="episode" if episode else "track", can_like=can_like,
        fetched_at=time.monotonic())


def get_spotify_api_meta(sp_oauth, req_session=None) -> dict | None:
    """
    Return current Spotify playback as a record.
    Returns None if Spotify is not configured or nothing plays.
    Returns a dict with status="Auth_Required" if authentication is needed.
    """
    if sp_oauth is None:
        spotify_auth.update(state="off", likes=False)
        return None
    info = spotify_token(sp_oauth)
    spotify_auth.update(state="ok" if info else "required", likes=spotify_can_like(info))
    if not info:
        return {"status": "Auth_Required", "artist": "", "title": "Spotify Not Authorized", "art_url": ""}
    try:
        curr = spotify_client(info, req_session).current_playback(additional_types="track,episode")
        record = spotify_record(curr, spotify_can_like(info))
        if record is None:
            _starts.note(SPOTIFY_ID, "Stopped")
        return record
    except Exception as e:
        logger.debug(f"Spotify API error: {e}")
    return None


def spotify_liked(sp_oauth, item_type: str, item_id: str, req_session=None) -> bool | None:
    """Whether the track or episode is in the user's library; None when the
    token cannot tell (signed in before likes, or the API refused)."""
    info = spotify_token(sp_oauth)
    if not (spotify_can_like(info) and item_id):
        return None
    try:
        sp = spotify_client(info, req_session)
        contains = (sp.current_user_saved_episodes_contains if item_type == "episode"
                    else sp.current_user_saved_tracks_contains)
        return bool(contains([item_id])[0])
    except Exception as e:
        logger.info(f"Spotify library check failed: {e}")
        return None


def spotify_set_liked(sp_oauth, item_type: str, item_id: str, liked: bool, req_session=None) -> bool:
    info = spotify_token(sp_oauth)
    if not (spotify_can_like(info) and item_id):
        return False
    try:
        sp = spotify_client(info, req_session)
        if item_type == "episode":
            fn = sp.current_user_saved_episodes_add if liked else sp.current_user_saved_episodes_delete
        else:
            fn = sp.current_user_saved_tracks_add if liked else sp.current_user_saved_tracks_delete
        fn([item_id])
        return True
    except Exception as e:
        logger.warning(f"Spotify like failed: {e}")
        return False


# ── Windows Media Transport ────────────────────────────────────────────────────

_win_media_cache: list = []
_win_media_ts: float   = float("-inf")
_win_media_ttl: float  = 2.0   # seconds between Windows SMTCS queries
WIN_MAX_SESSIONS = 6
WIN_VIDEO = 2                  # Windows.Media.MediaPlaybackType.Video

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


def _winrt_timeline(session, playing: bool) -> tuple[float | None, float | None]:
    """Position and length from SMTC; position is as of last_updated_time."""
    try:
        from datetime import datetime, timezone
        tl = session.get_timeline_properties()
        length = tl.end_time.total_seconds()
        if length <= 0:
            return None, None
        position = tl.position.total_seconds()
        if playing and tl.last_updated_time:
            position += max(0.0, (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds())
        return round(min(position, length), 1), round(length, 1)
    except Exception as e:  # winrt projections differ between versions
        logger.debug(f"Windows SMTC timeline: {e}")
        return None, None


def _winrt_int(value) -> int | None:
    """An enum or an IReference<enum> (projections differ) as an int."""
    for v in (value, getattr(value, "value", None)):
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


async def _smtc_manager():
    from winrt.windows.media.control import (   # type: ignore[import]
        GlobalSystemMediaTransportControlsSessionManager as SMTCSM,
    )
    return await asyncio.wait_for(SMTCSM.request_async(), timeout=2.0)


def _smtc_session(manager, aumid: str):
    for s in manager.get_sessions():
        if (getattr(s, "source_app_user_model_id", "") or "") == aumid:
            return s
    return None


async def _query_winrt_sessions() -> list[dict]:
    """Every playing or paused SMTC session as a record (winrt 3.x coroutines
    are natively awaitable — no run_in_executor needed)."""
    if not _WINRT_AVAILABLE:
        return []
    try:
        from winrt.windows.media.control import (   # type: ignore[import]
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlayStatus,
        )
        # PlayStatus values: 0=Closed, 1=Opened, 2=Changing, 3=Stopped, 4=Playing, 5=Paused
        PLAYING = getattr(PlayStatus, "PLAYING", 4)
        PAUSED  = getattr(PlayStatus, "PAUSED",  5)
        manager = await _smtc_manager()
        current = manager.get_current_session()
        current_id = (getattr(current, "source_app_user_model_id", "") or "") if current else ""
        out = []
        for session in list(manager.get_sessions())[:WIN_MAX_SESSIONS]:
            pb = session.get_playback_info()
            status_val = pb.playback_status if pb else None
            if status_val not in (PLAYING, PAUSED):
                continue
            props = await asyncio.wait_for(session.try_get_media_properties_async(), timeout=2.0)
            aumid = getattr(session, "source_app_user_model_id", "") or ""
            status = "Playing" if status_val == PLAYING else "Paused"
            position, length = _winrt_timeline(session, status == "Playing")
            controls = getattr(pb, "controls", None)
            caps = {
                "prev": bool(getattr(controls, "is_previous_enabled", True)),
                "next": bool(getattr(controls, "is_next_enabled", True)),
                "seek": bool(getattr(controls, "is_playback_position_enabled", True)),
            }
            ptype = _winrt_int(getattr(pb, "playback_type", None))
            if ptype is None and props is not None:
                ptype = _winrt_int(getattr(props, "playback_type", None))
            sid = "smtc:" + aumid
            started = _starts.note(sid, status)
            out.append(_record(
                id=sid, via="smtc", raw=aumid, name=_player_label(aumid) or "Media", status=status,
                title=(props.title or "Unknown") if props else "Unknown",
                artist=(props.artist or "Unknown") if props else "Unknown",
                album=(getattr(props, "album_title", "") or "") if props else "",
                position=position, length=length, caps=caps,
                # Windows' own current session wins a tie.
                started=started + (0.001 if aumid == current_id else 0.0),
                aumid=aumid, video=ptype == WIN_VIDEO))
        _starts.forget("smtc:", {p["id"] for p in out})
        return out
    except asyncio.TimeoutError:
        logger.debug("Windows SMTCS query timed out")
        return []
    except Exception as e:
        logger.debug(f"Windows SMTCS error: {e}")
        return []


async def get_windows_players_async() -> list[dict]:
    """
    Every Windows media session as a record.
    Caches results for _win_media_ttl seconds to avoid hammering the OS.
    """
    global _win_media_cache, _win_media_ts
    if not _WINRT_AVAILABLE:
        return []
    now = time.monotonic()
    if now - _win_media_ts < _win_media_ttl:
        return _win_media_cache
    try:
        result = await _query_winrt_sessions()
    except Exception as e:
        logger.debug(f"Windows media async wrapper: {e}")
        result = []
    _win_media_cache, _win_media_ts = result, now
    return result


async def smtc_position(aumid: str) -> float | None:
    if not _WINRT_AVAILABLE:
        return None
    try:
        session = _smtc_session(await _smtc_manager(), aumid)
        return _winrt_timeline(session, True)[0] if session else None
    except Exception as e:
        logger.debug(f"Windows SMTC position: {e}")
        return None


async def smtc_command(aumid: str, cmd: str, value: float | None = None) -> bool:
    """play / next / prev / seek (absolute seconds) on that app's session.
    False when Windows or the app refused: the caller falls back to media keys."""
    global _win_media_ts
    if not _WINRT_AVAILABLE:
        return False
    try:
        session = _smtc_session(await _smtc_manager(), aumid)
        if session is None:
            return False
        if cmd == "play":
            op = session.try_toggle_play_pause_async()
        elif cmd == "next":
            op = session.try_skip_next_async()
        elif cmd == "prev":
            op = session.try_skip_previous_async()
        elif cmd == "seek" and value is not None:
            op = session.try_change_playback_position_async(int(max(0.0, value) * 10_000_000))
        else:
            return False
        ok = bool(await asyncio.wait_for(op, timeout=2.0))
        _win_media_ts = float("-inf")    # read the result on the next tick
        return ok
    except Exception as e:
        logger.debug(f"Windows SMTC {cmd}: {e}")
        return False
