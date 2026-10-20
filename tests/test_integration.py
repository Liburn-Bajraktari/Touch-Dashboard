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

@pytest.fixture
def client(mock_hardware):
    with TestClient(_app) as c:
        yield c

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
    with client.websocket_connect("/ws") as websocket:
        # First message is config_sync
        config_sync = websocket.receive_json()
        assert config_sync["type"] == "config_sync"
        assert "cfg" in config_sync["data"]
        
        # Subsequent messages are sys_data (from hardware_loop)
        sys_data = websocket.receive_json()
        assert sys_data["type"] == "sys_data"
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
        with client.websocket_connect("/ws") as websocket:
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
        with client.websocket_connect("/ws") as websocket:
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
