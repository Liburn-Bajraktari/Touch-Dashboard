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


# ── Voice channel tracking ────────────────────────────────────────────────────
# Discord's VOICE_STATE_* events carry no channel id, so the client must follow
# exactly one channel: these replay what Discord sends when you switch channels,
# are moved by someone else, or hang up.

ME, FRIEND, OLD_FRIEND = "100", "200", "300"


def _vs(uid, name, mute=False):
    return {"user": {"id": uid, "username": name, "avatar": None}, "nick": None,
            "voice_state": {"self_mute": mute, "self_deaf": False}}


def _channel(cid, name, guild, *states):
    return {"cmd": "GET_SELECTED_VOICE_CHANNEL", "evt": None, "nonce": "GET_VC",
            "data": {"id": cid, "name": name, "guild_id": guild, "voice_states": list(states)}}


@pytest.fixture
def tracked():
    """An IPC client in channel A (guild G) with a friend, its sent commands recorded."""
    client = DiscordIPC("1", "secret")
    client.on_state_change = lambda: None
    client.user_id = ME
    sent = []
    client.send = lambda op, payload: sent.append(payload)
    client._request_channel()                                    # after AUTHENTICATE
    client._handle(_channel("A", "Lobby", "G", _vs(ME, "me"), _vs(OLD_FRIEND, "old")))
    client._handle(_channel("A", "Lobby", "G", _vs(ME, "me"), _vs(OLD_FRIEND, "old")))
    client._handle({"cmd": "GET_GUILD", "nonce": "GET_GUILD_VC", "data": {"id": "G", "name": "Squad"}})
    sent.clear()
    return client, sent


def _subs(sent, cmd):
    return sorted({p["args"]["channel_id"] for p in sent if p.get("cmd") == cmd})


def test_joining_subscribes_before_the_snapshot_is_used(tracked):
    client, _ = tracked
    assert client._vc_sub == "A" and not client._vc_pending
    assert set(client.voice_channel["users"]) == {ME, OLD_FRIEND}
    assert client.voice_channel["guild_name"] == "Squad"


def test_switching_channels_follows_only_the_new_one(tracked):
    client, sent = tracked
    client._handle({"evt": "VOICE_CHANNEL_SELECT", "data": {"channel_id": "B", "guild_id": "G"}})
    assert _subs(sent, "UNSUBSCRIBE") == ["A"] and _subs(sent, "SUBSCRIBE") == ["B"]
    assert sent[-1]["cmd"] == "GET_SELECTED_VOICE_CHANNEL"
    # Still on its way from channel A: must not turn up in B's list.
    client._handle({"evt": "VOICE_STATE_UPDATE", "data": _vs(OLD_FRIEND, "old", mute=True)})
    client._handle(_channel("B", "Gaming", "G", _vs(ME, "me"), _vs(FRIEND, "friend")))
    vc = client.voice_channel
    assert (vc["id"], vc["name"]) == ("B", "Gaming")
    assert set(vc["users"]) == {ME, FRIEND}
    assert vc["guild_name"] == "Squad"                           # same server: no flicker
    assert not any(p.get("cmd") == "GET_GUILD" for p in sent)
    # Events after the snapshot apply again.
    client._handle({"evt": "SPEAKING_START", "data": {"user_id": FRIEND}})
    assert vc["users"][FRIEND]["speaking"] is True


def test_being_moved_without_a_select_event(tracked):
    """Moved by someone else: our own VOICE_STATE_DELETE in the old channel is the only sign."""
    client, sent = tracked
    client._handle({"evt": "VOICE_STATE_DELETE", "data": {"user": {"id": ME}}})
    assert client._vc_pending and sent[-1]["cmd"] == "GET_SELECTED_VOICE_CHANNEL"
    assert set(client.voice_channel["users"]) == {ME, OLD_FRIEND}   # unchanged until we know
    client._handle(_channel("B", "AFK", "H", _vs(ME, "me")))         # where we are now
    assert _subs(sent, "UNSUBSCRIBE") == ["A"] and _subs(sent, "SUBSCRIBE") == ["B"]
    assert client.voice_channel["id"] == "A"                      # snapshot taken after subscribing
    client._handle(_channel("B", "AFK", "H", _vs(ME, "me")))
    assert client.voice_channel["id"] == "B" and set(client.voice_channel["users"]) == {ME}
    assert client.voice_channel["guild_name"] is None             # another server: asked for
    assert any(p.get("cmd") == "GET_GUILD" and p["args"]["guild_id"] == "H" for p in sent)


def test_refreshing_the_same_channel_keeps_speaking_and_subscriptions(tracked):
    client, sent = tracked
    client._handle({"evt": "SPEAKING_START", "data": {"user_id": OLD_FRIEND}})
    client._handle({"evt": "VOICE_CHANNEL_SELECT", "data": {"channel_id": "A", "guild_id": "G"}})
    client._handle(_channel("A", "Lobby", "G", _vs(ME, "me"), _vs(OLD_FRIEND, "old")))
    assert _subs(sent, "SUBSCRIBE") == [] and _subs(sent, "UNSUBSCRIBE") == []
    assert client.voice_channel["users"][OLD_FRIEND]["speaking"] is True
    assert client.voice_channel["guild_name"] == "Squad"


def test_hanging_up_stops_listening(tracked):
    client, sent = tracked
    client._handle({"evt": "VOICE_CHANNEL_SELECT", "data": {"channel_id": None}})
    assert client.voice_channel is None and client._vc_sub is None
    assert _subs(sent, "UNSUBSCRIBE") == ["A"]
    client._handle({"evt": "VOICE_STATE_DELETE", "data": {"user": {"id": ME}}})   # arrives late
    assert client.voice_channel is None


def test_events_naming_another_channel_are_ignored(tracked):
    client, _ = tracked
    client._handle({"evt": "SPEAKING_START", "data": {"user_id": OLD_FRIEND, "channel_id": "Z"}})
    assert client.voice_channel["users"][OLD_FRIEND]["speaking"] is False


def test_a_failed_channel_request_does_not_freeze_the_list(tracked):
    client, _ = tracked
    client._request_channel()
    client._handle({"cmd": "GET_SELECTED_VOICE_CHANNEL", "evt": "ERROR", "data": {"code": 1000}})
    client._handle({"evt": "VOICE_STATE_CREATE", "data": _vs(FRIEND, "friend")})
    assert FRIEND in client.voice_channel["users"]


def test_reconnecting_subscribes_again(tracked):
    client, sent = tracked
    client._close_transport = lambda: None
    client.close()
    assert client._vc_sub is None and not client._vc_pending
    client._request_channel()
    client._handle(_channel("A", "Lobby", "G", _vs(ME, "me")))
    assert _subs(sent, "SUBSCRIBE") == ["A"]


def test_authenticate_remembers_who_we_are():
    client = DiscordIPC("1", "secret", access_token="t")
    client.send = lambda op, payload: None
    client.recv = lambda: {"cmd": "AUTHENTICATE", "evt": None,
                           "data": {"user": {"id": 4242, "username": "me"}, "scopes": ["rpc"]}}
    client._authenticate()
    assert client.user_id == "4242" and client._vc_pending
