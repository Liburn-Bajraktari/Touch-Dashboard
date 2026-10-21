"""AudioSystem: level readings, the volume worker, and output cycling.

Linux paths only (wpctl); every process call is patched, so nothing touches
the PipeWire session.
"""
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from audio_system import AudioSystem

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="wpctl paths")

SINK = "@DEFAULT_AUDIO_SINK@"


@pytest.fixture(autouse=True)
def fresh_state():
    AudioSystem._last_state.clear()
    AudioSystem._state_at.clear()
    yield
    AudioSystem.on_volume_applied = None
    AudioSystem._last_state.clear()
    AudioSystem._state_at.clear()


@pytest.mark.parametrize("out, expected", [
    ("Volume: 0.57", {"vol": 57, "muted": False}),        # int() made this 56
    ("Volume: 0.29 [MUTED]", {"vol": 29, "muted": True}),
    ("Volume: 1.00", {"vol": 100, "muted": False}),
])
def test_get_state_rounds_the_level(out, expected):
    with patch.object(AudioSystem, "run", return_value=out):
        assert AudioSystem.get_state(SINK) == expected


def test_remember_keeps_the_newest_reading():
    AudioSystem.remember(SINK, {"vol": 40, "muted": False}, at=5.0)
    AudioSystem.remember(SINK, {"vol": 10, "muted": False}, at=3.0)   # a poll that began earlier
    assert AudioSystem.latest(SINK, {}) == {"vol": 40, "muted": False}
    AudioSystem.remember(SINK, {"vol": 55, "muted": False}, at=6.0)
    assert AudioSystem.latest(SINK, {}) == {"vol": 55, "muted": False}


def test_volume_worker_applies_reads_back_and_reports():
    calls = []

    def run(cmd):
        calls.append(cmd)
        return "Volume: 0.42" if cmd[1] == "get-volume" else ""

    applied = threading.Event()
    AudioSystem.remember(SINK, {"vol": 30, "muted": False})
    AudioSystem.on_volume_applied = applied.set
    with patch.object(AudioSystem, "run", side_effect=run):
        AudioSystem.set_vol(SINK, 42)
        assert applied.wait(2), "worker never reported the applied level"
    assert ["wpctl", "set-volume", SINK, "42%"] in calls
    # Not muted: no second process to unmute.
    assert not any(c[1] == "set-mute" for c in calls)
    assert ["wpctl", "get-volume", SINK] in calls
    assert AudioSystem.latest(SINK, {}) == {"vol": 42, "muted": False}


def test_volume_worker_unmutes_a_muted_channel():
    calls = []
    applied = threading.Event()
    AudioSystem.remember(SINK, {"vol": 30, "muted": True})
    AudioSystem.on_volume_applied = applied.set
    with patch.object(AudioSystem, "run", side_effect=lambda cmd: calls.append(cmd) or "Volume: 0.50"):
        AudioSystem.set_vol(SINK, 50)
        assert applied.wait(2)
    assert ["wpctl", "set-mute", SINK, "0"] in calls


def _sinks(active):
    return [{"id": str(i), "raw_name": name, "name": name, "is_active": name == active}
            for i, name in enumerate(["Speakers", "HDMI", "Headset"], start=40)]


@pytest.mark.parametrize("active, hidden, expected", [
    ("Speakers", [], "41"),                  # plain next
    ("Speakers", ["HDMI"], "42"),            # skips the hidden one
    ("Headset", ["Speakers"], "41"),         # wraps, then skips
    ("HDMI", ["HDMI"], "42"),                # active itself hidden: on to the next visible
])
def test_cycle_device_skips_hidden_outputs(active, hidden, expected):
    with patch.object(AudioSystem, "poll_all", return_value={"sinks": _sinks(active)}), \
         patch.object(AudioSystem, "run") as run:
        AudioSystem.cycle_device({}, hidden)
    run.assert_called_once_with(["wpctl", "set-default", expected])


def test_cycle_device_with_every_output_hidden_does_nothing():
    with patch.object(AudioSystem, "poll_all", return_value={"sinks": _sinks("HDMI")}), \
         patch.object(AudioSystem, "run") as run:
        AudioSystem.cycle_device({}, ["Speakers", "HDMI", "Headset"])
    run.assert_not_called()
