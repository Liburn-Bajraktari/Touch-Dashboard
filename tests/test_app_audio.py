"""app_audio: per-app streams for the full-screen mixer.

pactl's JSON is given as data and every process call is patched, so nothing
touches the PipeWire session.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app_audio
from app_audio import IN, OUT

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def stream(index, app="Firefox", binary="firefox", vol=100, mute=False, corked=False, owner=None,
           source=None, **props):
    p = {"application.name": app, "application.process.binary": binary, **props}
    entry = {"index": index, "owner_module": owner, "corked": corked, "mute": mute,
             "volume": {"front-left": {"value": round(vol * 65536 / 100)},
                        "front-right": {"value": round(vol * 65536 / 100)}},
             "properties": {k: v for k, v in p.items() if v is not None}}
    if source is not None:
        entry["source"] = source
    return entry


@pytest.fixture(autouse=True)
def fresh_state():
    app_audio._last = {OUT: {}, IN: {}}
    app_audio._icons.clear()
    app_audio.stale = False
    yield
    app_audio._last = {OUT: {}, IN: {}}
    app_audio._icons.clear()


@pytest.mark.parametrize("props, expected", [
    ({"application.process.binary": "firefox", "application.name": "Firefox"}, "firefox"),
    ({"application.process.binary": "Discord", "application.name": "WEBRTC VoiceEngine"}, "discord"),
    ({"application.process.binary": "C:\\Games\\Hollow Knight.exe"}, "hollow-knight"),
    ({"application.id": "mpv", "application.name": "mpv"}, "mpv"),            # no process (Flatpak, ALSA)
    ({"application.name": "Google Chrome"}, "google-chrome"),
    ({"node.name": "Firefox"}, "firefox"),
    ({"application.process.binary": "../../"}, None),
    ({}, None),
])
def test_app_id(props, expected):
    assert app_audio.app_id(props) == expected


@pytest.mark.parametrize("props, expected", [
    ({"application.name": "Firefox", "application.process.binary": "firefox"}, "Firefox"),
    ({"application.name": "WEBRTC VoiceEngine", "application.process.binary": "Discord"}, "Discord"),
    ({"application.name": "ALSA plug-in [mpv]", "application.process.binary": "mpv"}, "mpv"),
    ({"application.name": "", "application.process.binary": "spotify"}, "Spotify"),
    ({"application.name": "gsr-default_input", "application.process.binary": "gpu-screen-recorder"},
     "Gpu screen recorder"),
    ({"application.name": "hollow_knight", "application.process.binary": "hollow_knight"}, "hollow_knight"),
    ({"application.name": "obs", "application.process.binary": "obs"}, "obs"),
    ({"application.name": "A" * 80}, "A" * app_audio.NAME_MAX),
])
def test_display_name(props, expected):
    assert app_audio.display_name(props, app_audio.app_id(props) or "x") == expected


def test_one_strip_per_app_with_the_loudest_level():
    groups = app_audio.groups_from_pactl([
        stream(12, vol=40, corked=True),
        stream(10, vol=72, mute=True),          # two tabs: one strip
        stream(11, app="Spotify", binary="spotify", vol=85),
    ], OUT)
    assert list(groups) == ["firefox", "spotify"]   # in the order each app's oldest stream appeared
    ff = groups["firefox"]
    assert (ff["vol"], ff["streams"]) == (72, [10, 12])
    assert ff["muted"] is False                     # muted only when every stream is
    assert ff["active"] is True                     # one plays: the app is live
    assert groups["spotify"]["name"] == "Spotify"


def test_streams_that_are_not_an_app_are_left_out():
    groups = app_audio.groups_from_pactl([
        stream(1, app=None, binary=None, owner="536870917", **{"media.name": "loopback-1208-13 output"}),
        stream(2, app="pavucontrol", binary="pavucontrol", **{"media.name": "Peak detect"}),
        stream(3, app="plasmashell", binary="plasmashell", **{"stream.monitor": "true"}),
        stream(4, app="plasmashell", binary="plasmashell", **{"media.role": "event"}),
        stream(5, app="pw-play", binary="pw-play", **{"target.object": app_audio.SOUNDBOARD_SINK}),
        stream(6, app=None, binary=None),         # nothing names it
        stream(7),
    ], OUT)
    assert list(groups) == ["firefox"]


def test_recording_from_a_monitor_is_not_the_mic():
    groups = app_audio.groups_from_pactl([
        stream(20, app="WEBRTC VoiceEngine", binary="Discord", source=59),
        stream(21, app="gsr-default_output", binary="gpu-screen-recorder", source=65),
        stream(22, app=None, binary=None, owner="536870918", source=59),    # NoiseTorch's filter chain
    ], IN, monitors=frozenset({65}))
    assert list(groups) == ["discord"]
    assert groups["discord"]["name"] == "Discord"


def test_monitor_sources_are_read_once_until_a_new_source_appears():
    app_audio._sources_seen.clear()
    listing = "59\talsa_input.usb-Mic\tPipeWire\n65\talsa_output.usb-Speakers.monitor\tPipeWire\n"
    with patch("app_audio._run", return_value=listing) as run:
        assert app_audio._monitors({59, 65}) == frozenset({65})
        assert app_audio._monitors({59}) == frozenset({65})
        assert run.call_count == 1
        app_audio._monitors({59, 70})
        assert run.call_count == 2


def test_level_is_the_loudest_channel():
    assert app_audio._level({"front-left": {"value": 65536}, "front-right": {"value": 32768}}) == 100
    assert app_audio._level({"mono": {"value": 37356}}) == 57
    assert app_audio._level({"mono": {"value": True}}) == 0
    assert app_audio._level(None) == 0


def _reading():
    app_audio._last = {OUT: app_audio.groups_from_pactl([stream(10, mute=True), stream(12)], OUT),
                       IN: app_audio.groups_from_pactl([stream(20, source=59)], IN)}


def test_a_level_goes_to_every_stream_of_the_app_and_unmutes():
    _reading()
    app_audio._last[OUT]["firefox"]["muted"] = True
    with patch("app_audio._run", return_value="") as run, patch.object(sys, "platform", "linux"):
        app_audio.set_volume(OUT, "firefox", 140)
    cmds = [c.args[0] for c in run.call_args_list]
    assert ["pactl", "set-sink-input-volume", "10", "100%"] in cmds   # clamped
    assert ["pactl", "set-sink-input-volume", "12", "100%"] in cmds
    assert ["pactl", "set-sink-input-mute", "10", "0"] in cmds
    assert app_audio.latest()[OUT]["firefox"]["vol"] == 100
    assert app_audio.latest()[OUT]["firefox"]["muted"] is False
    assert app_audio.stale is True


def test_mute_reaches_recording_streams():
    _reading()
    with patch("app_audio._run", return_value="") as run, patch.object(sys, "platform", "linux"):
        app_audio.set_mute(IN, "firefox", True)
        app_audio.set_mute(IN, "nobody", True)    # gone since the reading: nothing to do
    assert [c.args[0] for c in run.call_args_list] == [["pactl", "set-source-output-mute", "20", "1"]]
    assert app_audio.latest()[IN]["firefox"]["muted"] is True


def test_known_only_takes_apps_the_reading_listed():
    _reading()
    assert app_audio.known(OUT, "firefox")
    assert not app_audio.known(OUT, "spotify")
    assert not app_audio.known("sideways", "firefox")
    assert not app_audio.known(OUT, ["firefox"])


def test_public_rows_carry_no_stream_details():
    _reading()
    rows = app_audio.public(app_audio.latest()[OUT])
    assert rows == [{"id": "firefox", "name": "Firefox", "vol": 100, "muted": False,
                     "active": True, "icon": False}]


def test_read_parses_pactl(tmp_path):
    import json
    outputs = {
        ("pactl", "-f", "json", "list", "sink-inputs"): json.dumps([stream(10)]),
        ("pactl", "-f", "json", "list", "source-outputs"): json.dumps([stream(20, source=59)]),
        ("pactl", "list", "short", "sources"): "59\talsa_input.usb-Mic\tPipeWire\n",
    }
    with patch("app_audio._run", side_effect=lambda cmd: outputs.get(tuple(cmd), "")), \
            patch.object(sys, "platform", "linux"), patch("app_audio.find_icon", return_value=None):
        reading = app_audio.read()
    assert list(reading[OUT]) == ["firefox"] and list(reading[IN]) == ["firefox"]
    assert app_audio.latest() is reading


def test_read_survives_pactl_failing():
    with patch("app_audio._run", return_value=""), patch.object(sys, "platform", "linux"):
        assert app_audio.read() == {OUT: {}, IN: {}}


# ── icons ─────────────────────────────────────────────────────────────────────

def _icon(root, size, name, data=PNG):
    path = root / "hicolor" / size / "apps" / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def test_find_icon_prefers_the_largest_png(tmp_path):
    _icon(tmp_path, "48x48", "firefox")
    big = _icon(tmp_path, "256x256", "firefox")
    assert app_audio.find_icon(["firefox"], roots=[str(tmp_path)], pixmaps=[]) == big


def test_find_icon_tries_each_name_then_pixmaps(tmp_path):
    pixmaps = tmp_path / "pixmaps"
    pixmaps.mkdir()
    (pixmaps / "discord.png").write_bytes(PNG)
    found = app_audio.find_icon([None, "WEBRTC VoiceEngine", "Discord"], roots=[str(tmp_path)],
                                pixmaps=[str(pixmaps)])
    assert found == str(pixmaps / "discord.png")   # "Discord" also looked up in lower case


def test_find_icon_takes_names_not_paths_and_only_pngs(tmp_path):
    _icon(tmp_path, "128x128", "evil", data=b"<svg onload=alert(1)>")
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    roots, none = [str(tmp_path)], []
    assert app_audio.find_icon(["evil"], roots=roots, pixmaps=none) is None
    assert app_audio.find_icon([str(secret), "../secret", "hicolor/../../secret"], roots=roots,
                               pixmaps=[str(tmp_path)]) is None
    _icon(tmp_path, "64x64", "huge", data=PNG + b"\0" * app_audio.ICON_MAX_BYTES)
    assert app_audio.find_icon(["huge"], roots=roots, pixmaps=none) is None


def test_icon_path_is_looked_up_once_and_only_for_read_apps(tmp_path):
    with patch("app_audio.find_icon", return_value=str(tmp_path / "x.png")) as find, \
            patch.object(sys, "platform", "linux"):
        assert app_audio.icon_path("nobody") is None      # the route: never a lookup of its own
        assert app_audio.icon_path("firefox", ["firefox"]) == str(tmp_path / "x.png")
        assert app_audio.icon_path("firefox", ["firefox"]) == str(tmp_path / "x.png")
        assert app_audio.icon_path("firefox") == str(tmp_path / "x.png")
    assert find.call_count == 1


# ── Windows sessions (pycaw stand-ins) ────────────────────────────────────────

class _Proc:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Volume:
    def __init__(self, vol, muted):
        self.vol, self.muted = vol, muted

    def GetMasterVolume(self):
        return self.vol

    def GetMute(self):
        return int(self.muted)

    def SetMasterVolume(self, v, ctx):
        self.vol = v

    def SetMute(self, m, ctx):
        self.muted = bool(m)


class _Session:
    def __init__(self, exe, vol=1.0, muted=False, state=1, pid=1234, label=""):
        self.Process = _Proc(exe) if exe else None
        self.ProcessId = pid
        self.DisplayName = label
        self.State = state
        self.SimpleAudioVolume = _Volume(vol, muted)


def test_windows_sessions_group_by_process():
    sessions = [_Session("firefox.exe", 0.4, state=0), _Session("firefox.exe", 0.72),
                _Session(None, 0.5, pid=0), _Session("Spotify.exe", 0.85, True),
                _Session("svchost.exe", label="@%SystemRoot%\\System32\\AudioSrv.Dll,-202")]
    with patch("app_audio._win_sessions", return_value=sessions):
        groups = app_audio._win_read_kind(OUT)
    assert list(groups) == ["firefox", "system", "spotify", "svchost"]
    assert (groups["firefox"]["vol"], groups["firefox"]["active"]) == (72, True)
    assert groups["system"]["name"] == "System sounds"
    assert groups["spotify"]["muted"] is True
    assert groups["svchost"]["name"] == "Svchost"   # an indirect resource string is no name


def test_windows_level_and_mute_reach_every_session_of_the_app():
    sessions = [_Session("firefox.exe", 0.4, muted=True), _Session("firefox.exe", 0.7), _Session("obs64.exe")]
    with patch("app_audio._win_sessions", return_value=sessions):
        app_audio._win_apply(OUT, "firefox", vol=55)
        assert [round(s.SimpleAudioVolume.vol, 2) for s in sessions] == [0.55, 0.55, 1.0]
        assert sessions[0].SimpleAudioVolume.muted is False   # a level unmutes
        app_audio._win_apply(OUT, "firefox", muted=True)
        assert [s.SimpleAudioVolume.muted for s in sessions] == [True, True, False]
