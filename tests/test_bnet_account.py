"""Battle.net sign-in and the saved characters: the code traded for the
account's characters (the token dropped), the 0600 list, characters added by
name, and switching between them from any screen."""
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wow_addons.armory import Armory, character_id

from test_armory import NOW, FakeSession, Resp

ACCOUNT = {"wow_accounts": [{"characters": [
    {"name": "Magicbane", "realm": {"name": "Twisting Nether", "slug": "twisting-nether"},
     "playable_class": {"name": "Mage"}, "level": 80, "faction": {"name": "Horde"}},
    {"name": "Steelhide", "realm": {"name": "Draenor", "slug": "draenor"},
     "playable_class": {"name": "Warrior"}, "level": 72},
]}]}


class AccountSession(FakeSession):
    """The account's characters in the EU only; the other regions know none."""

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data))
        return Resp(200, {"access_token": "user-token" if data.get("grant_type") == "authorization_code" else "app",
                          "expires_in": 86399})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        u = urlparse(url)
        if u.path == "/userinfo":
            return Resp(200, {"battletag": "Magic#1234"})
        if u.path == "/profile/user/wow":
            return Resp(200, ACCOUNT) if u.hostname.startswith("eu.") else Resp(404, {})
        return Resp(404, {})


def make(tmp_path, session=None):
    a = Armory(session=session or AccountSession(), clock=lambda: NOW, account_file=str(tmp_path / "chars.json"))
    a.configure("id", "secret")
    return a


def test_the_sign_in_url_asks_for_the_profile():
    a = Armory(session=AccountSession(), clock=lambda: NOW)
    a.configure("client", "secret")
    q = parse_qs(urlparse(a.authorize_url("https://relay.example/cb", "8888.xyz")).query)
    assert q["client_id"] == ["client"] and q["scope"] == ["wow.profile openid"]
    assert q["redirect_uri"] == ["https://relay.example/cb"] and q["state"] == ["8888.xyz"]


def test_signing_in_lists_the_accounts_characters_and_keeps_no_token(tmp_path):
    session = AccountSession()
    a = make(tmp_path, session)
    acct = a.sign_in("the-code", "http://127.0.0.1:8888/bnet_callback")
    assert acct["battletag"] == "Magic#1234"
    assert [(c["name"], c["region"], c["source"]) for c in acct["characters"]] == [
        ("Magicbane", "eu", "account"), ("Steelhide", "eu", "account")]
    trade = next(c for c in session.calls if c[0] == "POST")
    assert trade[2]["grant_type"] == "authorization_code" and trade[2]["code"] == "the-code"
    saved = (tmp_path / "chars.json").read_text(encoding="utf-8")
    assert "user-token" not in saved and "Magicbane" in saved
    if sys.platform != "win32":       # Windows keeps only a read-only bit; %APPDATA%'s ACL guards it
        assert stat.S_IMODE(os.stat(tmp_path / "chars.json").st_mode) == 0o600


def test_added_characters_outlive_a_sign_out_and_a_new_sign_in(tmp_path):
    a = make(tmp_path)
    a.sign_in("code", "http://127.0.0.1:8888/bnet_callback")
    added = a.add_character({"region": "us", "name": "Leaf", "realm": "Area 52", "class": "Druid", "level": 70})
    assert added["source"] == "added" and a.find_character(added["id"])["name"] == "Leaf"
    a.mark_used(added["id"])
    assert a.account()["characters"][0]["id"] == added["id"]          # the ones played lately first
    a.sign_out()
    assert [c["name"] for c in a.account()["characters"]] == ["Leaf"]
    a.sign_in("code", "http://127.0.0.1:8888/bnet_callback")
    assert {c["name"] for c in a.account()["characters"]} == {"Magicbane", "Steelhide", "Leaf"}
    assert a.remove_character(added["id"]) and not a.remove_character(added["id"])
    again = make(tmp_path)                                               # read back from the file
    assert {c["name"] for c in again.account()["characters"]} == {"Magicbane", "Steelhide"}


# ── over HTTP and the WebSocket ───────────────────────────────────────────────

from test_integration import mock_hardware  # noqa: E402,F401
from test_security import BASE_URL, _device, _paired_token, app  # noqa: E402,F401
from test_integration import _receive_until  # noqa: E402
from test_wow import wow  # noqa: E402,F401

import server  # noqa: E402


def test_the_callback_needs_its_state_and_this_pc(app, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "armory", make(tmp_path))
    r = app.get("/bnet_login", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert state.startswith(f"{server.origin_policy.port}.")
    assert app.get("/bnet_callback?code=x&state=8888.wrong").status_code in (400, 403)
    with patch.object(server.armory, "sign_in", wraps=server.armory.sign_in) as sign_in:
        ok = app.get(f"/bnet_callback?code=the-code&state={state}")
    assert ok.status_code == 200 and "Signed in as Magic#1234" in ok.text and sign_in.called
    assert app.get(f"/bnet_callback?code=again&state={state}").status_code in (400, 403)   # once only


def test_the_redirect_is_https_or_this_server():
    clean = server._clean_bnet_redirect
    assert clean("https://me.github.io/Touch-Dashboard/bnet-callback.html").startswith("https://")
    assert clean("http://127.0.0.1:8888/bnet_callback") == "http://127.0.0.1:8888/bnet_callback"
    for bad in ("http://evil.example/bnet_callback", "http://127.0.0.1:8888/other", "https://u:p@x.example/",
                "javascript:alert(1)", 5, "x" * 600):
        assert clean(bad) == ""


def test_a_tablet_switches_character_from_the_plate(app, wow, tmp_path, monkeypatch):
    arm = make(tmp_path)
    arm.sign_in("code", "http://127.0.0.1:8888/bnet_callback")
    monkeypatch.setattr(server, "armory", arm)
    monkeypatch.setattr(server.wow, "armory", arm)
    target = character_id("eu", "draenor", "Steelhide")
    with patch("server.save_config"), _device(app, _paired_token()) as ws:
        _receive_until(ws, "wow_state")
        ws.send_json({"type": "wow_addon_action", "command": "char_switch", "args": {"id": target}})
        assert _receive_until(ws, "wow_cmd_ack")["data"]["status"] == "sent"
        ws.send_json({"type": "wow_addon_action", "command": "char_switch", "args": {"id": "cnotacharacter"}})
        assert _receive_until(ws, "wow_cmd_ack")["data"]["status"] == "error"
    s = server.wow.settings()
    assert (s["character"], s["realm"]) == ("Steelhide", "Draenor")
