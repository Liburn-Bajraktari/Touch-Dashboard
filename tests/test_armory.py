"""Raider.IO and Battle.net for the WoW page (wow_addons.armory), the Character
module and spell names on NSRT cards. HTTP goes to a fake session: nothing here
touches the network."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import requests

import wow_addons
from wow_addons import WowAddons
from wow_addons.armory import Armory, ArmoryError, REFRESH_S, RETRY_S, role_tag

RIO_PROFILE = {
    "name": "Magicbane", "realm": "Twisting Nether", "class": "Mage", "active_spec_name": "Frost",
    "active_spec_role": "DPS", "thumbnail_url": "https://render.worldofwarcraft.com/eu/character/x-avatar.jpg",
    "profile_url": "https://raider.io/characters/eu/twisting-nether/Magicbane",
    "gear": {"item_level_equipped": 671.4},
    "mythic_plus_scores_by_season": [{"season": "season-mn-2", "scores": {"all": 2860.3}}],
    "raid_progression": {
        "the-venomous-abyss": {"summary": "6/8 M", "expansion_id": 11, "total_bosses": 8,
                               "normal_bosses_killed": 8, "heroic_bosses_killed": 8, "mythic_bosses_killed": 6},
        "the-unbinding-of-kithix": {"summary": "1/1 H", "expansion_id": 11, "total_bosses": 1},
        "tier-mn-1": {"summary": "9/9 M", "expansion_id": 11},
        "old-raid": {"summary": "8/8 M", "expansion_id": 10},
    },
}
BNET_PROFILE = {
    "name": "Magicbane", "realm": {"name": "Twisting Nether", "slug": "twisting-nether"},
    "character_class": {"name": "Mage"}, "active_spec": {"name": "Frost"}, "level": 90,
    "equipped_item_level": 672, "guild": {"name": "Northern Sky"}, "faction": {"name": "Horde"},
}
STATIC = {"raids": [
    {"slug": "the-venomous-abyss", "name": "The Venomous Abyss",
     "starts": {"eu": "2026-08-19T04:00:00Z"}, "ends": {"eu": "2030-01-01T00:00:00Z"}},
    {"slug": "the-unbinding-of-kithix", "name": "The Unbinding of Kith'ix",
     "starts": {"eu": "2026-10-14T04:00:00Z"}, "ends": {"eu": "2030-01-01T00:00:00Z"}},
    {"slug": "tier-mn-1", "name": "MN Tier 1", "starts": {"eu": "2026-03-18T04:00:00Z"},
     "ends": {"eu": "2026-08-19T04:00:00Z"}},
]}
AFFIXES = {"title": "Tyrannical, Fortified", "affix_details": [
    {"id": 9, "name": "Tyrannical", "icon_url": "https://cdn.raiderio.net/images/wow/icons/large/a.jpg"},
    {"id": 10, "name": "Fortified", "icon_url": "https://evil.example/track.png"},
]}
NOW = 1_793_000_000.0   # 2026-10-26: Kith'ix and the Abyss open, tier 1 over


class Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    """Answers by path; records every request."""

    def __init__(self, routes=None, token_status=200, offline=False):
        self.routes = routes if routes is not None else {}
        self.token_status = token_status
        self.offline = offline
        self.calls = []

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        self.calls.append(("POST", url, auth))
        if self.offline:
            raise requests.ConnectionError("down")
        return Resp(self.token_status, {"access_token": "tok", "expires_in": 86399})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        if self.offline:
            raise requests.ConnectionError("down")
        path = urlparse(url).path
        for prefix, answer in self.routes.items():
            if path == prefix or path.startswith(prefix + "?"):
                return Resp(*answer)
        return Resp(404, {})


def routes(**extra):
    r = {
        "/api/v1/characters/profile": (200, RIO_PROFILE),
        "/api/v1/raiding/static-data": (200, STATIC),
        "/api/v1/mythic-plus/affixes": (200, AFFIXES),
        "/profile/wow/character/twisting-nether/magicbane": (200, BNET_PROFILE),
        "/profile/wow/character/twisting-nether/magicbane/character-media": (
            200, {"assets": [{"key": "avatar", "value": "https://render.worldofwarcraft.com/eu/c/avatar.jpg"}]}),
        "/data/wow/spell/45438": (200, {"name": "Ice Block"}),
        "/data/wow/media/spell/45438": (
            200, {"assets": [{"key": "icon", "value": "https://render.worldofwarcraft.com/eu/icons/56/ice.jpg"}]}),
    }
    r.update(extra)
    return r


def armory(session, key=True, tmp_path=None, now=None):
    clock = now or [NOW]
    a = Armory(cache_file=str(tmp_path / "spells.json") if tmp_path else None, session=session,
               clock=lambda: clock[0])
    if key:
        a.configure("id", "secret")
    return a, clock


# ── lookup ────────────────────────────────────────────────────────────────────

def test_lookup_merges_battle_net_over_raider_io():
    s = FakeSession(routes())
    c = armory(s)[0].lookup("eu", "Twisting Nether", "Magicbane")
    assert (c["name"], c["realm"], c["class"], c["spec"], c["level"], c["guild"]) == (
        "Magicbane", "Twisting Nether", "Mage", "Frost", 90, "Northern Sky")
    assert c["ilvl"] == 672 and c["score"] == 2860 and c["role_tag"] == "ranged"
    assert c["avatar"] == "https://render.worldofwarcraft.com/eu/c/avatar.jpg"
    assert c["sources"] == {"raiderio": "ok", "bnet": "ok"}
    # Blizzard's path takes Raider.IO's realm slug and a lower-case name, with a bearer token.
    bnet = [x for x in s.calls if x[0] == "GET" and "blizzard" in x[1]][0]
    assert bnet[1] == "https://eu.api.blizzard.com/profile/wow/character/twisting-nether/magicbane"
    assert bnet[2]["namespace"] == "profile-eu" and bnet[3]["Authorization"] == "Bearer tok"


def test_without_a_key_raider_io_is_enough():
    s = FakeSession(routes())
    c = armory(s, key=False)[0].lookup("eu", "Twisting Nether", "Magicbane")
    assert c["sources"] == {"raiderio": "ok", "bnet": "no_key"}
    assert c["ilvl"] == 671.4 and c["avatar"] == RIO_PROFILE["thumbnail_url"]
    assert not [x for x in s.calls if x[0] == "POST"]


def test_unknown_characters_refused_keys_and_no_network():
    s = FakeSession(routes(**{"/api/v1/characters/profile": (400, {"message": "Could not find"})}))
    with pytest.raises(ArmoryError) as err:
        armory(s)[0].lookup("eu", "Twisting Nether", "Nobody")
    assert err.value.reason == "not_found" and err.value.sources == {"raiderio": "not_found", "bnet": "not_found"}

    c = armory(FakeSession(routes(), token_status=401))[0].lookup("eu", "Twisting Nether", "Magicbane")
    assert c["sources"] == {"raiderio": "ok", "bnet": "refused"}

    with pytest.raises(ArmoryError, match="offline"):
        armory(FakeSession(offline=True))[0].lookup("eu", "Twisting Nether", "Magicbane")
    with pytest.raises(ArmoryError, match="not_found"):
        armory(FakeSession(routes()))[0].lookup("xx", "Twisting Nether", "Magicbane")


def test_role_tags_follow_class_and_spec():
    assert role_tag("Death Knight", "Frost") == "melee" and role_tag("Mage", "Frost") == "ranged"
    assert role_tag("Paladin", "Holy") == "healer" and role_tag("Monk", "Brewmaster") == "tank"
    assert role_tag("Druid", "Feral") == "melee" and role_tag(None, None) == ""


# ── refresh: raids, affixes, timing ───────────────────────────────────────────

def test_refresh_reads_open_raids_and_affixes():
    a, _ = armory(FakeSession(routes()))
    a.refresh(("eu", "Twisting Nether", "Magicbane"))
    # Open now, newest first; the tier's combined entry and last expansion's raid left out.
    assert a.character["raid_list"] == [
        {"slug": "the-unbinding-of-kithix", "name": "The Unbinding of Kith'ix", "summary": "1/1 H"},
        {"slug": "the-venomous-abyss", "name": "The Venomous Abyss", "summary": "6/8 M"}]
    # Only Raider.IO's and Blizzard's own image hosts reach the page.
    assert a.cached_affixes("eu")["list"] == [
        {"id": 9, "name": "Tyrannical", "icon": AFFIXES["affix_details"][0]["icon_url"]},
        {"id": 10, "name": "Fortified", "icon": None}]


def test_the_character_is_read_again_when_due():
    a, clock = armory(FakeSession(routes()))
    who = ("eu", "Twisting Nether", "Magicbane")
    assert not a.refresh_due(None) and a.refresh_due(who)
    a.refresh(who)
    assert not a.refresh_due(who) and a.refresh_due(("eu", "Draenor", "Other"))
    clock[0] += REFRESH_S
    assert a.refresh_due(who)
    a.refresh(who)
    a.ask_refresh()
    assert a.refresh_due(who)
    a.refresh(who)
    a.ask_refresh()                      # a second Refresh within the minute is ignored
    assert not a.refresh_due(who)

    down, clock = armory(FakeSession(offline=True))
    down.refresh(who)
    assert down.error == "offline" and not down.refresh_due(who)
    clock[0] += RETRY_S
    assert down.refresh_due(who)


# ── spells ────────────────────────────────────────────────────────────────────

def test_spells_are_fetched_once_and_kept(tmp_path):
    s = FakeSession(routes())
    a, _ = armory(s, tmp_path=tmp_path)
    assert a.spell(45438) is None and a.spell(999) is None and a.wanted()
    assert a.fetch_spells("eu") is True
    assert a.spell(45438) == {"name": "Ice Block", "icon": "https://render.worldofwarcraft.com/eu/icons/56/ice.jpg"}
    assert a.spell(999) is None and not a.wanted()      # unknown to Battle.net: not asked again soon
    saved = json.loads((tmp_path / "spells.json").read_text())
    assert saved["spells"]["45438"]["name"] == "Ice Block"
    again = Armory(cache_file=str(tmp_path / "spells.json"), session=FakeSession(), clock=lambda: NOW)
    assert again.spell(45438)["name"] == "Ice Block"


def test_spells_need_a_key_and_wait_out_a_refused_one():
    a, _ = armory(FakeSession(routes()), key=False)
    assert a.spell(45438) is None and not a.wanted()
    a, _ = armory(FakeSession(routes(), token_status=401))
    a.spell(45438)
    assert a.fetch_spells("eu") is False and a.wanted()  # kept for later


# ── the WoW page ──────────────────────────────────────────────────────────────

def make(arm, settings):
    core = WowAddons(wow_addons.discover(), send_keys=lambda *k: None, armory=arm, sleep=lambda s: None)
    core.configure(settings)
    return core


def view(core, module):
    return next(m["data"] for m in core.update_msgs() if m["type"] == f"wow_{module}_update")


def test_character_plate_states():
    a, _ = armory(FakeSession(routes()))
    core = make(a, {})
    assert view(core, "char")["state"] == "unset"
    core.configure({"character": "Magicbane", "realm": "Twisting Nether", "region": "eu"})
    assert view(core, "char")["state"] == "loading"
    a.refresh(core.who())
    v = view(core, "char")
    assert v["state"] == "ok" and v["character"]["score"] == 2860 and len(v["affixes"]) == 2
    assert v["character"]["raids"][0]["name"] == "The Unbinding of Kith'ix"
    a._session.offline = True
    a.ask_refresh()
    a.refresh(core.who())
    v = view(core, "char")
    assert v["state"] == "ok" and v["stale"] and v["reason"] == "offline"   # the last reading stays up
    with pytest.raises(wow_addons.CommandError, match="empty"):
        make(a, {}).run("char_refresh")


def test_nsrt_cards_name_their_spells():
    a, _ = armory(FakeSession(routes()))
    core = make(a, {"character": "Magicbane"})
    core._state["nsrt"]["pasted"] = None
    core.clipboard = type("C", (), {"get_text": lambda self: "time:10;tag:Magicbane;spellid:45438;text:Block",
                                    "set_text": lambda self, t: True})()
    core.run("nsrt_paste")
    assert "spellName" not in view(core, "nsrt")["personal"][0]       # not fetched yet
    a.fetch_spells("eu")
    entry = view(core, "nsrt")["personal"][0]
    assert entry["spellName"] == "Ice Block" and entry["icon"].endswith("ice.jpg")


def test_region_and_realm_are_cleaned():
    core = make(None, {"region": "moon", "realm": ' Twisting "Nether"\n'})
    assert core.settings()["region"] == "eu" and core.settings()["realm"] == "Twisting Nether"
    assert core.who() is None
    core.configure({"region": "us", "realm": "Illidan", "character": "Someone"})
    assert core.who() == ("us", "Illidan", "Someone")


# ── over the WebSocket ────────────────────────────────────────────────────────

from test_integration import _receive_until, mock_hardware  # noqa: E402,F401
from test_security import _admin, app  # noqa: E402,F401

import server  # noqa: E402


def test_settings_lookup_answers_with_the_character(app, monkeypatch):
    seen = {}

    def fake_lookup(region, realm, name, key):
        seen["args"] = (region, realm, name, key)
        return {**RIO_PROFILE, "region": region, "role_tag": "ranged", "score": 2860, "ilvl": 672,
                "sources": {"raiderio": "ok", "bnet": "ok"}, "raids": {"secret": "internal"}}
    monkeypatch.setattr(server.armory, "lookup", fake_lookup)
    with _admin(app) as ws:
        ws.send_json({"type": "bnet_lookup", "data": {"region": "eu", "realm": "Twisting Nether", "name": "Magicbane",
                                                      "client_id": " typed-id ", "client_secret": "typed-secret"}})
        result = _receive_until(ws, "bnet_lookup_result")["data"]
    assert result["ok"] and result["character"]["role_tag"] == "ranged"
    assert set(result["character"]) == set(server.CHARACTER_KEYS)
    assert seen["args"] == ("eu", "Twisting Nether", "Magicbane", ("typed-id", "typed-secret"))


def test_a_new_key_reads_the_character_again():
    a, _ = armory(FakeSession(routes()), key=False)
    who = ("eu", "Twisting Nether", "Magicbane")
    a.refresh(who)
    assert a.character["sources"]["bnet"] == "no_key" and not a.refresh_due(who)
    assert a.configure("id", "secret") is True and a.refresh_due(who)
    a.refresh(who)
    assert a.character["sources"]["bnet"] == "ok" and a.configure("id", "secret") is False
