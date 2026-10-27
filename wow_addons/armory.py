"""
armory.py — what Raider.IO and Battle.net say about the player's character.

Raider.IO (no key): the character's spec, item level, Mythic+ score and raid
progress, this week's affixes, and the raids' names and dates. Battle.net
(an API client's ID and secret, client credentials): the character's profile
and avatar, and spell names and icons for NSRT's reminders. Nobody signs in:
Battle.net only returns to HTTPS addresses after a sign-in, and everything
used here is public game data.

This is data, not live state: Blizzard updates a character when it logs out,
Raider.IO when it next crawls it. The boss of an NSRT note cannot be named
from here: notes carry the in-game encounter id, which neither API indexes
(Raider.IO numbers encounters itself, Blizzard's journal by journal id).

Every method that touches the network blocks (the server calls them from
worker threads) and raises ArmoryError with a short reason.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

REGIONS = ("eu", "us", "kr", "tw")
TOKEN_URL = "https://oauth.battle.net/token"       # every region but China
API_URL = "https://{region}.api.blizzard.com"
LOCALES = {"us": "en_US", "eu": "en_GB", "kr": "en_US", "tw": "en_US"}
RIO_URL = "https://raider.io/api/v1"
RIO_FIELDS = "gear,mythic_plus_scores_by_season:current,raid_progression"
TIMEOUT_S = 8
REFRESH_S = 30 * 60          # the character, while the WoW page's Character module is on
RETRY_S = 5 * 60             # after a failed refresh
REQUEST_GAP_S = 60           # a refresh asked for from a screen, at most this often
AFFIXES_S = 3 * 3600
RAIDS_S = 24 * 3600
SPELL_TTL_S = 30 * 24 * 3600
SPELL_RETRY_S = 3600         # a spell Battle.net does not know
SPELLS_PER_ROUND = 40
# Images the page may show: Blizzard's and Raider.IO's own image hosts.
IMAGE_HOSTS = frozenset({"render.worldofwarcraft.com", "cdn.raiderio.net", "cdn.raider.io"})

# Role tags for NSRT's filter, from the active spec. Ranged or melee needs the
# class too: a Frost mage is ranged, a Frost death knight melee.
RANGED = {
    ("druid", "balance"), ("hunter", "beast mastery"), ("hunter", "marksmanship"),
    ("mage", "arcane"), ("mage", "fire"), ("mage", "frost"), ("priest", "shadow"),
    ("shaman", "elemental"), ("warlock", "affliction"), ("warlock", "demonology"),
    ("warlock", "destruction"), ("evoker", "devastation"), ("evoker", "augmentation"),
}
HEALERS = {
    ("druid", "restoration"), ("shaman", "restoration"), ("priest", "holy"),
    ("priest", "discipline"), ("paladin", "holy"), ("monk", "mistweaver"), ("evoker", "preservation"),
}
TANKS = {
    ("death knight", "blood"), ("demon hunter", "vengeance"), ("druid", "guardian"),
    ("monk", "brewmaster"), ("paladin", "protection"), ("warrior", "protection"),
}


class ArmoryError(Exception):
    """not_found, no_key, refused (the API key), busy (rate limited), offline, failed."""

    def __init__(self, reason: str, sources: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.sources = sources or {}


def role_tag(class_name: str | None, spec: str | None, rio_role: str | None = None) -> str:
    """tank, healer, ranged or melee; "" when the spec is unknown."""
    key = ((class_name or "").lower(), (spec or "").lower())
    if key in TANKS or rio_role == "TANK":
        return "tank"
    if key in HEALERS or rio_role == "HEALING":
        return "healer"
    if key in RANGED:
        return "ranged"
    return "melee" if key[1] else ""


def realm_slug(realm: str) -> str:
    """Blizzard's realm slug, near enough: "Twisting Nether" → twisting-nether."""
    return re.sub(r"\s+", "-", realm.strip().lower().replace("'", ""))


def image(url) -> str | None:
    """An https image on Blizzard's or Raider.IO's own hosts; anything else is dropped."""
    if not isinstance(url, str) or len(url) > 512:
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    return url if p.scheme == "https" and p.hostname in IMAGE_HOSTS else None


def _text(value) -> str | None:
    """A Blizzard name: a string, an object with one, or one per locale."""
    if isinstance(value, dict):
        if "name" in value:
            return _text(value["name"])
        for loc in ("en_US", "en_GB"):
            if isinstance(value.get(loc), str):
                return value[loc]
        return next((v for v in value.values() if isinstance(v, str)), None)
    return value if isinstance(value, str) else None


def _time(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None
    except (ValueError, AttributeError):
        return None


class Armory:
    def __init__(self, cache_file: str | None = None, session=None, clock=time.time,
                 user_agent: str = "TouchDashboard"):
        self._session = session or requests.Session()
        self._ua = {"User-Agent": user_agent}
        self._clock = clock
        self._lock = threading.RLock()
        self._key = ("", "")
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._cache_file = cache_file
        self._spells: dict[str, dict] = {}       # "id" → {name, icon, at}; name None: unknown to Battle.net
        self._wanted: set[int] = set()
        self._affixes: dict[str, tuple[float, dict]] = {}
        self._raids: dict[int, tuple[float, list]] = {}
        self.character: dict | None = None       # the last refresh, merged
        self.error: str | None = None            # why the last refresh failed
        self._refreshed: tuple | None = None     # (region, realm, name) of self.character
        self._refreshed_at = 0.0
        self._failed_at = 0.0
        self._asked_at = 0.0
        self._asked = False
        self._load()

    # ── key ───────────────────────────────────────────────────────────────

    def configure(self, client_id: str, secret: str) -> bool:
        """Set the API key; whether it changed (the character is then read again,
        now with or without Battle.net)."""
        key = (client_id.strip() if isinstance(client_id, str) else "",
               secret.strip() if isinstance(secret, str) else "")
        with self._lock:
            changed, self._key = key != self._key, key
            if changed and self._refreshed:
                self._asked = True
            return changed

    def has_key(self) -> bool:
        with self._lock:
            return all(self._key)

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None, rio: bool = False) -> dict:
        try:
            r = self._session.get(url, params=params, headers={**self._ua, **(headers or {})}, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            logger.info("Armory: %s unreachable (%s)", urlparse(url).hostname, type(e).__name__)
            raise ArmoryError("offline") from None
        if r.status_code == 404 or (rio and r.status_code == 400):
            raise ArmoryError("not_found")     # Raider.IO answers 400 for an unknown character
        if r.status_code in (401, 403):
            raise ArmoryError("refused")
        if r.status_code == 429:
            raise ArmoryError("busy")
        if r.status_code >= 400:
            raise ArmoryError("failed")
        try:
            data = r.json()
        except ValueError:
            raise ArmoryError("failed") from None
        if not isinstance(data, dict):
            raise ArmoryError("failed")
        return data

    def _token(self, key: tuple[str, str]) -> str:
        if not all(key):
            raise ArmoryError("no_key")
        with self._lock:
            hit = self._tokens.get(key)
            if hit and self._clock() < hit[1]:
                return hit[0]
        try:
            r = self._session.post(TOKEN_URL, data={"grant_type": "client_credentials"}, auth=key,
                                   headers=self._ua, timeout=TIMEOUT_S)
        except requests.RequestException:
            raise ArmoryError("offline") from None
        if r.status_code in (400, 401, 403):
            raise ArmoryError("refused")
        if r.status_code >= 400:
            raise ArmoryError("failed")
        try:
            data = r.json()
            token, ttl = data["access_token"], float(data.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise ArmoryError("failed") from None
        with self._lock:
            self._tokens[key] = (token, self._clock() + max(60.0, ttl - 300))
        return token

    def _bnet(self, region: str, path: str, namespace: str, key: tuple[str, str] | None = None) -> dict:
        key = key or self._key
        params = {"namespace": f"{namespace}-{region}", "locale": LOCALES.get(region, "en_US")}
        for attempt in (1, 2):
            token = self._token(key)
            try:
                return self._get(API_URL.format(region=region) + path, params,
                                 {"Authorization": f"Bearer {token}"})
            except ArmoryError as e:
                if e.reason != "refused" or attempt == 2:
                    raise
                with self._lock:   # a token Blizzard revoked early: fetch a new one once
                    self._tokens.pop(key, None)
        raise ArmoryError("failed")

    def _rio(self, path: str, **params) -> dict:
        return self._get(RIO_URL + path, params, rio=True)

    # ── the character ─────────────────────────────────────────────────────

    def _rio_character(self, region: str, realm: str, name: str) -> dict:
        d = self._rio("/characters/profile", region=region, realm=realm, name=name, fields=RIO_FIELDS)
        seasons = d.get("mythic_plus_scores_by_season") or []
        score = None
        if seasons and isinstance(seasons[0], dict):
            score = (seasons[0].get("scores") or {}).get("all")
        url = d.get("profile_url") or ""
        slug = re.match(r"https://raider\.io/characters/[a-z]+/([^/]+)/", url)
        gear = d.get("gear") or {}
        raids = {}
        for raid_slug, p in (d.get("raid_progression") or {}).items():
            if isinstance(p, dict):
                raids[raid_slug] = {"summary": p.get("summary"), "expansion": p.get("expansion_id"),
                                    "total": p.get("total_bosses"), "normal": p.get("normal_bosses_killed"),
                                    "heroic": p.get("heroic_bosses_killed"), "mythic": p.get("mythic_bosses_killed")}
        return {
            "name": d.get("name"), "realm": d.get("realm"), "realm_slug": slug.group(1) if slug else None,
            "class": d.get("class"), "spec": d.get("active_spec_name"), "rio_role": d.get("active_spec_role"),
            "ilvl": gear.get("item_level_equipped"), "score": round(score) if isinstance(score, (int, float)) else None,
            "raids": raids, "avatar": image(d.get("thumbnail_url")), "profile_url": url if url.startswith("https://raider.io/") else None,
        }

    def _bnet_character(self, region: str, realm: str, name: str, key: tuple[str, str] | None) -> dict:
        path = f"/profile/wow/character/{quote(realm)}/{quote(name.lower())}"
        p = self._bnet(region, path, "profile", key)
        out = {
            "name": p.get("name"), "realm": _text(p.get("realm")), "realm_slug": (p.get("realm") or {}).get("slug"),
            "class": _text(p.get("character_class")), "spec": _text(p.get("active_spec")),
            "level": p.get("level"), "ilvl": p.get("equipped_item_level"), "guild": _text(p.get("guild")),
            "faction": _text(p.get("faction")),
        }
        try:
            media = self._bnet(region, path + "/character-media", "profile", key)
            assets = {a.get("key"): a.get("value") for a in media.get("assets") or [] if isinstance(a, dict)}
            out["avatar"] = image(assets.get("avatar") or assets.get("inset"))
        except ArmoryError:
            pass   # the profile without its picture
        return out

    def lookup(self, region: str, realm: str, name: str, key: tuple[str, str] | None = None) -> dict:
        """The character from both, merged: Battle.net's profile first (fresh at
        logout), Raider.IO's score and raid progress. `sources` says what each
        answered ("ok" or a reason)."""
        if region not in REGIONS or not realm.strip() or not name.strip():
            raise ArmoryError("not_found")
        sources: dict[str, str] = {}
        rio = bnet = None
        try:
            rio = self._rio_character(region, realm.strip(), name.strip())
            sources["raiderio"] = "ok"
        except ArmoryError as e:
            sources["raiderio"] = e.reason
        slug = (rio or {}).get("realm_slug") or realm_slug(realm)
        try:
            bnet = self._bnet_character(region, slug, name.strip(), key or self._key)
            sources["bnet"] = "ok"
        except ArmoryError as e:
            sources["bnet"] = e.reason
        if not rio and not bnet:
            reasons = set(sources.values())
            reason = next((r for r in ("not_found", "refused", "busy", "offline") if r in reasons), "failed")
            raise ArmoryError(reason, sources)
        merged = {**(rio or {}), **{k: v for k, v in (bnet or {}).items() if v is not None}}
        merged.setdefault("raids", {})
        if rio and rio.get("avatar") and not merged.get("avatar"):
            merged["avatar"] = rio["avatar"]
        merged["region"] = region
        merged["role_tag"] = role_tag(merged.get("class"), merged.get("spec"), (rio or {}).get("rio_role"))
        merged["sources"] = sources
        return merged

    def refresh_due(self, who: tuple | None) -> bool:
        """Whether to read the character again: another one, asked for, or old."""
        if not who or not all(who):
            return False
        with self._lock:
            now = self._clock()
            if self._asked or who != self._refreshed:
                return True
            if self.error:
                return now - self._failed_at >= RETRY_S
            return now - self._refreshed_at >= REFRESH_S

    def ask_refresh(self) -> None:
        """A screen's Refresh key; at most once a minute."""
        with self._lock:
            if self._clock() - self._asked_at >= REQUEST_GAP_S:
                self._asked_at = self._clock()
                self._asked = True

    def wants_refresh(self) -> bool:
        with self._lock:
            return self._asked

    def refresh(self, who: tuple) -> None:
        """Read the character, its raids' names and this week's affixes."""
        region, realm, name = who
        with self._lock:
            self._asked = False
        try:
            char = self.lookup(region, realm, name)
        except ArmoryError as e:
            with self._lock:
                self.error, self._failed_at, self._refreshed = e.reason, self._clock(), who
                if self.character and self.character.get("_who") != who:
                    self.character = None
            return
        char["raid_list"] = self._current_raids(region, char.get("raids") or {})
        char["_who"] = who
        char["updated"] = self._clock()
        with self._lock:
            self.character, self.error = char, None
            self._refreshed, self._refreshed_at = who, self._clock()
        try:
            self.affixes(region)
        except ArmoryError:
            pass

    # ── raids and affixes ─────────────────────────────────────────────────

    def _static_raids(self, expansion: int) -> list:
        with self._lock:
            hit = self._raids.get(expansion)
            if hit and self._clock() - hit[0] < RAIDS_S:
                return hit[1]
        data = self._rio("/raiding/static-data", expansion_id=expansion)
        raids = [r for r in data.get("raids") or [] if isinstance(r, dict)]
        with self._lock:
            self._raids[expansion] = (self._clock(), raids)
        return raids

    def _current_raids(self, region: str, progress: dict) -> list:
        """The character's raids that are open now in its region, newest first
        (at most three), with their names. A tier's combined entry ("tier-…")
        only when no single raid is open."""
        if not progress:
            return []
        newest = max((p.get("expansion") or 0 for p in progress.values()), default=0)
        now = self._clock()
        try:
            static = {r.get("slug"): r for r in self._static_raids(newest)} if newest else {}
        except ArmoryError:
            static = {}
        rows = []
        for slug, p in progress.items():
            if (p.get("expansion") or 0) != newest:
                continue
            raid = static.get(slug) or {}
            starts = _time((raid.get("starts") or {}).get(region))
            ends = _time((raid.get("ends") or {}).get(region))
            if static and (starts is None or starts > now or (ends is not None and ends <= now)):
                continue
            rows.append({"slug": slug, "name": raid.get("name") or slug.replace("-", " ").title(),
                         "summary": p.get("summary"), "starts": starts or 0})
        single = [r for r in rows if not r["slug"].startswith("tier-")]
        rows = single or rows
        rows.sort(key=lambda r: r["starts"], reverse=True)
        return [{k: r[k] for k in ("slug", "name", "summary")} for r in rows[:3]]

    def affixes(self, region: str) -> dict | None:
        """This week's affixes for the region (kept three hours)."""
        with self._lock:
            hit = self._affixes.get(region)
            if hit and self._clock() - hit[0] < AFFIXES_S:
                return hit[1]
        d = self._rio("/mythic-plus/affixes", region=region, locale="en")
        out = {"title": d.get("title"), "list": [
            {"id": a.get("id"), "name": a.get("name"), "icon": image(a.get("icon_url"))}
            for a in d.get("affix_details") or [] if isinstance(a, dict) and a.get("name")][:6]}
        with self._lock:
            self._affixes[region] = (self._clock(), out)
        return out

    def cached_affixes(self, region: str) -> dict | None:
        with self._lock:
            hit = self._affixes.get(region)
            return hit[1] if hit else None

    # ── spells ────────────────────────────────────────────────────────────

    def spell(self, spell_id) -> dict | None:
        """{name, icon} of a spell already fetched; otherwise None, and it is
        fetched by the next fetch_spells() (with an API key)."""
        if not isinstance(spell_id, int) or spell_id <= 0:
            return None
        with self._lock:
            hit = self._spells.get(str(spell_id))
            now = self._clock()
            fresh = hit and now - hit["at"] < (SPELL_TTL_S if hit.get("name") else SPELL_RETRY_S)
            if not fresh and all(self._key):
                self._wanted.add(spell_id)
            if hit and hit.get("name"):
                return {"name": hit["name"], "icon": hit.get("icon")}
        return None

    def wanted(self) -> bool:
        with self._lock:
            return bool(self._wanted) and all(self._key)

    def fetch_spells(self, region: str) -> bool:
        """Fetch up to SPELLS_PER_ROUND wanted spells; whether any name arrived."""
        with self._lock:
            batch = sorted(self._wanted)[:SPELLS_PER_ROUND]
            self._wanted.difference_update(batch)
        region = region if region in REGIONS else "us"
        got = False
        for i, sid in enumerate(batch):
            try:
                d = self._bnet(region, f"/data/wow/spell/{sid}", "static")
                entry = {"name": _text(d.get("name")), "icon": None, "at": self._clock()}
                try:
                    media = self._bnet(region, f"/data/wow/media/spell/{sid}", "static")
                    entry["icon"] = next((image(a.get("value")) for a in media.get("assets") or []
                                          if isinstance(a, dict) and a.get("key") == "icon"), None)
                except ArmoryError:
                    pass
            except ArmoryError as e:
                if e.reason != "not_found":   # the key, the network: try the rest later
                    with self._lock:
                        self._wanted.update(batch[i:])
                    break
                entry = {"name": None, "icon": None, "at": self._clock()}
            with self._lock:
                self._spells[str(sid)] = entry
            got = got or bool(entry["name"])
        if batch:
            self._save()
        return got

    def _load(self) -> None:
        if not self._cache_file or not os.path.isfile(self._cache_file):
            return
        try:
            with open(self._cache_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for sid, e in (data.get("spells") or {}).items():
            if (isinstance(e, dict) and str(sid).isdigit() and isinstance(e.get("at"), (int, float))
                    and (e.get("name") is None or isinstance(e.get("name"), str))):
                self._spells[str(sid)] = {"name": e.get("name"), "icon": image(e.get("icon")), "at": e["at"]}

    def _save(self) -> None:
        if not self._cache_file:
            return
        with self._lock:
            data = {"spells": dict(self._spells)}
        tmp = self._cache_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._cache_file)
        except OSError as e:
            logger.warning("Could not save the spell cache: %s", e)
