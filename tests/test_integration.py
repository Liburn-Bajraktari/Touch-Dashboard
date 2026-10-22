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


def test_set_play_key_broadcasts_and_persists(client):
    """Settings → Themes: the play key in the cover's colour or the theme's, for every screen."""
    import server
    original = dict(server.config)
    with patch("server.save_config") as save:
        with _admin_ws(client) as websocket:
            sync = websocket.receive_json()
            assert sync["data"]["cfg"]["play_key_cover"] is True
            websocket.send_json({"type": "set_play_key", "data": {"cover": "no"}})
            assert _receive_until(websocket, "refused")["data"]["reason"] == "invalid"
            websocket.send_json({"type": "set_play_key", "data": {"cover": False}})
            assert _receive_until(websocket, "play_key_state")["data"] is False
        assert server.config["play_key_cover"] is False
        assert save.called
    assert "play_key_cover" in server.DEVICE_CFG_KEYS   # tablets follow it
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
    assert server._clean_sink_names("HDMI") == []
    _restore_config(original)


def test_saved_output_order_reaches_every_screen(client):
    """Audio routing's order: config_sync's hw comes sorted; devices not in it go last."""
    import server
    original = dict(server.config)
    sinks = [{"id": str(i), "raw_name": n, "name": n, "custom_name": "", "is_active": n == "HDMI"}
             for i, n in enumerate(["Speakers", "HDMI", "Headset", "USB DAC"], start=40)]
    with patch("server.save_config"), patch("server.last_audio_devs", sinks):
        with _admin_ws(client) as websocket:
            assert websocket.receive_json()["type"] == "config_sync"
            websocket.send_json({"type": "save_config",
                                 "data": {"audio_order": ["Headset", "Headset", 7, "HDMI", "Speakers"]}})
            sync = _receive_until(websocket, "config_sync")
    assert sync["data"]["cfg"]["audio_order"] == ["Headset", "HDMI", "Speakers"]
    assert [s["raw_name"] for s in sync["data"]["hw"]] == ["Headset", "HDMI", "Speakers", "USB DAC"]
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


def test_update_check_says_when_it_could_not_check():
    """Offline, the answer must not read as "up to date": the page says the check failed."""
    import asyncio
    import io
    import json
    import server

    saved = (server.update_available, server.latest_version, server.update_apk_url, server.update_notes)
    try:
        with patch("server.urllib.request.urlopen", side_effect=OSError("offline")):
            result = asyncio.run(server.check_for_updates())
        assert result["checked"] is False
        listing = [{"tag_name": server.VERSION, "prerelease": False, "draft": False, "body": "", "assets": []}]
        with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(listing).encode())):
            result = asyncio.run(server.check_for_updates())
        assert result["checked"] is True
    finally:
        server.update_available, server.latest_version, server.update_apk_url, server.update_notes = saved


def test_idle_screen_care_settings_are_checked_and_reach_tablets(client):
    import server
    original = dict(server.config)
    with patch("server.save_config"):
        with _admin_ws(client) as websocket:
            sync = websocket.receive_json()["data"]["cfg"]
            assert (sync["idle_dim_min"], sync["idle_clock_min"]) == (5, 15)
            websocket.send_json({"type": "save_config", "data": {"idle_dim_min": 10, "idle_clock_min": 0}})
            sync = _receive_until(websocket, "config_sync")["data"]["cfg"]
            assert (sync["idle_dim_min"], sync["idle_clock_min"]) == (10, 0)
            websocket.send_json({"type": "save_config", "data": {"idle_dim_min": 3, "idle_clock_min": "15"}})
            sync = _receive_until(websocket, "config_sync")["data"]["cfg"]
            assert (sync["idle_dim_min"], sync["idle_clock_min"]) == (5, 15)   # not offered: defaults
    assert {"idle_dim_min", "idle_clock_min"} <= set(server.DEVICE_CFG_KEYS)
    server.config.clear()
    server.config.update(original)


def test_weather_carries_the_full_screen_clock_details():
    import server
    owm = {"name": "Pristina", "timezone": 7200,
           "weather": [{"description": "few clouds"}],
           "main": {"temp": 17.6, "feels_like": 16.4, "humidity": 64, "temp_min": 12.0, "temp_max": 21.0},
           "wind": {"speed": 3.4}, "sys": {"sunrise": 1761109080, "sunset": 1761147720}}
    w = server._weather_from_owm(owm)
    assert (w["temp"], w["desc"], w["feels_like"], w["humidity"]) == (18, "Few Clouds", 16, 64)
    assert w["wind_kmh"] == 12 and w["tz"] == 7200 and w["city"] == "Pristina"
    assert (w["sunrise"], w["sunset"]) == (1761109080, 1761147720)
    assert "temp_min" not in w and "temp_max" not in w     # not a day's high and low
    sparse = server._weather_from_owm({"weather": [{"description": "mist"}], "main": {"temp": 3}})
    assert sparse["feels_like"] is None and sparse["wind_kmh"] is None and sparse["city"] is None


def test_retired_clip_macros_are_dropped():
    """The clip recording plugin is gone: a saved key for it would do nothing."""
    import server
    macros = [{"type": "plugin", "action_data": "app_clip", "display_name": "CLIP"},
              {"type": "plugin", "action_data": "app_soundpad", "display_name": "Soundboard"},
              {"type": "macro", "action_data": ["KEY_LEFTALT", "KEY_F10"], "display_name": "CLIP"}]
    kept = server._with_item_ids({"macros": macros})["macros"]
    assert [m["action_data"] for m in kept] == ["app_soundpad", ["KEY_LEFTALT", "KEY_F10"]]
    assert all(m.get("id") for m in kept)
    assert "app_clip" not in server.MACRO_MODULES


def test_speedtest_reports_ping_live_speed_and_result():
    """connect → ping (with server) → live readings → intermediate → result."""
    import asyncio
    import itertools
    import types
    import server

    class FakeSpeedtest:
        results = types.SimpleNamespace(ping=12.44)

        def get_best_server(self):
            return {"sponsor": "Telekom", "name": "Pristina", "latency": 13.0}

        def download(self, callback=None):
            import time
            for i in range(1, 4):
                callback(i, 3)
                time.sleep(0.3)   # long enough for live readings
            return 418.3e6

        def upload(self, callback=None):
            import time
            callback(1, 1)
            time.sleep(0.3)
            return 38.2e6

    counter = itertools.count(0, 12_500_000)   # +12.5 MB per read
    counters = lambda: types.SimpleNamespace(bytes_recv=next(counter), bytes_sent=next(counter))
    sent = []

    async def run():
        server.speedtest_lock = asyncio.Lock()
        with patch.object(server.speedtest_lib, "Speedtest", FakeSpeedtest), \
             patch.object(server.psutil, "net_io_counters", counters), \
             patch.object(server, "SPEEDTEST_LIVE_INTERVAL", 0.05), \
             patch.object(server.ws_manager, "broadcast", new=AsyncMock(side_effect=lambda m, *a, **k: sent.append(m))):
            await server._run_speedtest()
            await asyncio.sleep(0.05)   # progress callbacks land via run_coroutine_threadsafe

    saved_lock = server.speedtest_lock
    try:
        asyncio.run(run())
    finally:
        server.speedtest_lock = saved_lock
    kinds = [m["type"] for m in sent]
    progress = [m["data"] for m in sent if m["type"] == "speedtest_progress"]
    assert progress[0]["step"] == "connect"
    ping = next(p for p in progress if p["step"] == "ping")
    assert ping["ping"] == 12.4 and ping["server"] == "Telekom, Pristina"
    live = [m["data"] for m in sent if m["type"] == "speedtest_live"]
    assert {d["step"] for d in live} == {"down", "up"} and all(d["mbps"] > 0 for d in live)
    assert kinds.index("speedtest_intermediate") < kinds.index("speedtest_result")
    assert sent[-1] == {"type": "speedtest_result",
                        "data": {"down": 418.3, "up": 38.2, "ping": 12.4, "server": "Telekom, Pristina"}}
