"""Media metadata: player names, durations and the MPRIS parser."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import media  # noqa: E402


@pytest.mark.parametrize("raw, label", [
    ("spotify", "Spotify"),
    ("firefox.instance_1_23", "Firefox"),
    ("chromium.instance1234", "Chromium"),
    ("plasma-browser-integration", "Browser"),
    ("Spotify.exe", "Spotify"),
    ("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic", "Media Player"),
    ("308046B0AF4A39CB", ""),          # Firefox's Windows AUMID is a hash, not a name
    ("strawberry", "Strawberry"),
    ("someplayer", "Someplayer"),
    ("", ""),
])
def test_player_label(raw, label):
    assert media._player_label(raw) == label


def test_seconds():
    assert media._seconds("183000000", 1e6) == 183.0
    assert media._seconds(61500, 1000) == 61.5
    assert media._seconds("", 1e6) is None
    assert media._seconds(None) is None
    assert media._seconds(-1) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="MPRIS is Linux only")
def test_mpris_meta_reads_player_position_and_length():
    line = b"Playing|||Artist|||Title|||https://example.test/a.jpg|||firefox.instance_9|||61000000|||245000000\n"
    with patch.object(media, "_playerctl_path", return_value="/usr/bin/playerctl"), \
         patch.object(media.subprocess, "check_output", return_value=line):
        meta = media.get_local_mpris_meta()
    assert meta["status"] == "Playing"
    assert meta["player"] == "Firefox"
    assert meta["position"] == 61.0
    assert meta["length"] == 245.0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="MPRIS is Linux only")
def test_mpris_meta_without_length_reports_none():
    # Browsers often publish no mpris:length; the UI then hides the progress bar.
    line = b"Paused|||Artist|||Title||||||plasma-browser-integration|||12000000|||\n"
    with patch.object(media, "_playerctl_path", return_value="/usr/bin/playerctl"), \
         patch.object(media.subprocess, "check_output", return_value=line):
        meta = media.get_local_mpris_meta()
    assert meta["status"] == "Paused"
    assert meta["player"] == "Browser"
    assert meta["length"] is None
