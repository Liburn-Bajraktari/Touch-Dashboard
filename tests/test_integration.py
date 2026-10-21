import contextlib
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
from server import _app

class _StubMonitor:
    """Stands in for the D-Bus lock and UPower monitors.

    Their GLib threads fail to stop when the venv's PyGObject and the system
    dbus-python load two different gi modules, which errored every teardown.
    """
    on_lock_change = None
    on_battery_change = None

    def start(self):
        pass

    def stop(self):
        pass

    def get_state(self):
        return {"pct": None, "state": "unknown", "model": "", "vendor": ""}


class _StubLock(_StubMonitor):
    def get_state(self):
        return False


@pytest.fixture
def mock_hardware():
    with contextlib.ExitStack() as stack:
        mock_audio = stack.enter_context(patch("server.AudioSystem.poll_all"))
        mock_audio.return_value = {
            "sinks": [],
            "spk": {"vol": 50, "muted": False},
            "mic": {"vol": 50, "muted": False},
            "active_sink_name": "NONE",
        }
        mock_media = stack.enter_context(
            patch("server.media_module.get_windows_media_meta_async", new_callable=AsyncMock))
        mock_media.return_value = None
        stack.enter_context(patch("server._lock_monitor_mod.create", return_value=_StubLock()))
        if sys.platform.startswith("linux"):
            stack.enter_context(patch("server._mouse_battery_mod.UPowerMouseMonitor", _StubMonitor))
        yield

# The Host check only answers to this PC's own names, and the admin role is
# loopback + the dashboard's origin: test as the desktop window does.
BASE_URL = "http://127.0.0.1:8888"
WS_URL = "ws://127.0.0.1:8888/ws"   # websocket_connect does not use base_url
ADMIN_ORIGIN = {"origin": BASE_URL}


@pytest.fixture
def client(mock_hardware):
    with TestClient(_app, base_url=BASE_URL, client=("127.0.0.1", 50000)) as c:
        yield c


@contextlib.contextmanager
def _admin_ws(client):
    """A WebSocket past the auth handshake; config_sync is the next message."""
    with client.websocket_connect(WS_URL, headers=ADMIN_ORIGIN) as websocket:
        websocket.send_json({"type": "auth"})
        assert websocket.receive_json() == {"type": "auth_ok", "data": {"role": "admin", "device_id": "", "name": ""}}
        yield websocket

def test_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<!DOCTYPE html>" in response.text

def test_manifest(client):
    response = client.get("/manifest.json")
    assert response.status_code == 200
    assert "Touch Dashboard" in response.text

@pytest.mark.asyncio
async def test_websocket_flow(client):
    with _admin_ws(client) as websocket:
        # First message is config_sync
        config_sync = websocket.receive_json()
        assert config_sync["type"] == "config_sync"
        assert "cfg" in config_sync["data"]
        
        # Then sys_data from hardware_loop (an admin also gets the paired device list)
        sys_data = _receive_until(websocket, "sys_data")
        payload = sys_data["data"]
        assert "cpu" in payload
        assert "ram" in payload
        assert "audio" in payload


def _receive_until(websocket, msg_type, limit=20):
    for _ in range(limit):
        msg = websocket.receive_json()
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"no {msg_type} message within {limit} messages")


def test_set_theme_broadcasts_and_persists(client):
    import server
    original = dict(server.config)
    with patch("server.save_config") as save:
        with _admin_ws(client) as websocket:
            assert websocket.receive_json()["type"] == "config_sync"
            websocket.send_json({"type": "set_theme", "data": {"id": "daylight", "ground": "#e8eaed"}})
            assert _receive_until(websocket, "theme_state")["data"] == "daylight"
        assert server.config["theme"] == "daylight"
        assert server.config["theme_ground"] == "#e8eaed"
        assert save.called
    server.config.clear()
    server.config.update(original)


def test_set_theme_rejects_bad_values(client):
    import server
    original = dict(server.config)
    with patch("server.save_config"):
        with _admin_ws(client) as websocket:
            assert websocket.receive_json()["type"] == "config_sync"
            websocket.send_json({"type": "set_theme", "data": "not-an-object"})
            websocket.send_json({"type": "set_theme", "data": {"id": "../../etc", "ground": "red"}})
            websocket.send_json({"type": "set_theme", "data": {"id": "ember", "ground": "url(x)"}})
            assert _receive_until(websocket, "theme_state")["data"] == "ember"
        assert server.config["theme"] == "ember"
        assert server.config["theme_ground"] == original["theme_ground"]
    server.config.clear()
    server.config.update(original)


def test_index_etag_and_gzip(client):
    first = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert first.status_code == 200
    assert first.headers["content-encoding"] == "gzip"
    etag = first.headers["etag"]
    again = client.get("/", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["etag"] == etag


def test_security_and_static_cache_headers(client):
    page = client.get("/")
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "default-src" in page.headers["content-security-policy"]
    asset = client.get("/static/themes.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "no-cache"


def test_voice_channel_snapshot_is_detached():
    import server
    live = {"name": "Squad", "users": {"1": {"name": "a", "speaking": False}}}
    snap = server._snapshot_voice_channel(live)
    live["users"]["1"]["speaking"] = True
    live["users"]["2"] = {"name": "b", "speaking": False}
    assert snap["users"] == {"1": {"name": "a", "speaking": False}}
    assert server._snapshot_voice_channel(None) is None


def _restore_config(original):
    import server
    server.config.clear()
    server.config.update(original)


def test_config_sync_advertises_features(client):
    with _admin_ws(client) as websocket:
        sync = websocket.receive_json()
        assert "disc_voice" in sync["data"]["features"]


def test_sys_data_carries_load_sample_seq(client):
    with _admin_ws(client) as websocket:
        payload = _receive_until(websocket, "sys_data")["data"]
        assert isinstance(payload["load_seq"], int) and payload["load_seq"] >= 1


def test_set_volume_is_clamped(client):
    import server
    from unittest.mock import call
    original = dict(server.config)
    with patch("server.AudioSystem.set_vol") as set_vol, patch("server.save_config"):
        with _admin_ws(client) as websocket:
            assert websocket.receive_json()["type"] == "config_sync"
            websocket.send_json({"type": "set_volume", "data": {"type": "speaker", "val": 150}})
            websocket.send_json({"type": "set_volume", "data": {"type": "mic", "val": -5}})
            # Messages are handled in order: once this answers, both levels went through.
            websocket.send_json({"type": "set_theme", "data": {"id": "ember", "ground": "#0e0d0c"}})
            _receive_until(websocket, "theme_state")
    assert set_vol.call_args_list == [call("@DEFAULT_AUDIO_SINK@", 100), call("@DEFAULT_AUDIO_SOURCE@", 0)]
    _restore_config(original)


def test_disc_voice_sets_the_requested_state(client):
    import server
    original = dict(server.config)
    # A real client (it only connects when its loop runs), so config_sync and
    # sys_data still serialise; only the command is mocked.
    ipc = server.DiscordIPC("1", "secret")
    ipc.connected = True
    ipc.set_voice = MagicMock()
    with patch("server.disc_ipc_instance", ipc), patch("server.save_config"):
        with _admin_ws(client) as websocket:
            assert websocket.receive_json()["type"] == "config_sync"
            websocket.send_json({"type": "disc_voice", "data": {"mute": 1, "deaf": 0}})
            websocket.send_json({"type": "set_theme", "data": {"id": "ember", "ground": "#0e0d0c"}})
            _receive_until(websocket, "theme_state")
    ipc.set_voice.assert_called_once_with(mute=True, deaf=False)
    _restore_config(original)


def test_save_config_cleans_hidden_audio_devices(client):
    import server
    original = dict(server.config)
    with patch("server.save_config"):
        with _admin_ws(client) as websocket:
            assert websocket.receive_json()["type"] == "config_sync"
            websocket.send_json({"type": "save_config",
                                 "data": {"hidden_audio_devices": ["HDMI", "HDMI", 5, "", "x" * 300, "Headset"]}})
            sync = _receive_until(websocket, "config_sync")
    assert sync["data"]["cfg"]["hidden_audio_devices"] == ["HDMI", "Headset"]
    assert server._clean_hidden_devices("HDMI") == []
    _restore_config(original)


def test_update_check_skips_drafts_and_prereleases():
    """Tablets install the APK found here, so a pre-release must never be it."""
    import asyncio
    import io
    import json
    import server

    def release(tag, prerelease=False, draft=False):
        return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "body": "",
                "assets": [{"name": f"TouchDashboard-{tag}.apk",
                            "browser_download_url": f"https://example.invalid/{tag}/TouchDashboard-{tag}.apk"}]}

    listing = [release("v9.9.0", prerelease=True), release("v9.8.0", draft=True), release("v9.7.0")]
    saved = (server.update_available, server.latest_version, server.update_apk_url, server.update_notes)
    with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(listing).encode())):
        asyncio.run(server.check_for_updates())
    try:
        assert server.update_apk_url.endswith("/v9.7.0/TouchDashboard-v9.7.0.apk")
        assert server.latest_version == "v9.7.0"
    finally:
        server.update_available, server.latest_version, server.update_apk_url, server.update_notes = saved
