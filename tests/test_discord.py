"""Discord IPC: camera / screen share events, leaving a call, and log redaction."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import discord_ipc
from discord_ipc import DiscordIPC


@pytest.fixture
def ipc():
    client = DiscordIPC("1", "secret")
    client.on_state_change = lambda: None
    return client


@pytest.mark.parametrize("evt, key", [("VIDEO_STATE_UPDATE", "video"), ("SCREENSHARE_STATE_UPDATE", "screenshare")])
def test_camera_and_screen_share_follow_discord(ipc, evt, key):
    # Discord's payload: {"active": bool, ...} (screen share adds pid / application).
    ipc._handle({"evt": evt, "data": {"active": True, "pid": 4242}})
    assert ipc.voice_state[key] is True
    ipc._handle({"evt": evt, "data": {"active": False}})
    assert ipc.voice_state[key] is False


def test_older_field_names_still_read():
    assert discord_ipc._event_active({"screenshare_enabled": True}, "screenshare_enabled") is True
    assert discord_ipc._event_active({"enabled": True}, "video_enabled") is True
    assert discord_ipc._event_active({}, "video_enabled") is False


def test_leaving_the_call_ends_camera_and_screen_share(ipc):
    ipc.voice_state.update(video=True, screenshare=True)
    ipc.voice_channel = {"id": "1", "users": {}}
    ipc._handle({"evt": "VOICE_CHANNEL_SELECT", "data": {"channel_id": None}})
    assert ipc.voice_channel is None
    assert ipc.voice_state["video"] is False and ipc.voice_state["screenshare"] is False


def test_errors_are_logged_without_the_access_token():
    reply = {"cmd": "AUTHENTICATE", "evt": "ERROR",
             "data": {"code": 4009, "message": "Invalid access token: MTQ1OTI0MjAzMzg3MjEwOTcxMA.JTy9qqki3W3QzHvUGHSV"}}
    logged = discord_ipc._redact(reply)
    assert "MTQ1OTI0" not in logged and "<redacted>" in logged and "4009" in logged
