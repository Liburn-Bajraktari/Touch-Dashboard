import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
from server import _app

@pytest.fixture
def mock_hardware():
    with patch("server.AudioSystem.poll_all") as mock_audio:
        mock_audio.return_value = {
            "sinks": [],
            "spk": {"vol": 50, "muted": False},
            "mic": {"vol": 50, "muted": False},
            "active_sink_name": "NONE",
        }
        with patch("server.media_module.get_windows_media_meta_async", new_callable=AsyncMock) as mock_media:
            mock_media.return_value = None
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
