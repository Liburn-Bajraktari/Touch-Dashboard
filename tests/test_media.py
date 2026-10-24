"""Media sources: player names, the playerctl poll, which source is shown,
the key layout, Previous, covers and Spotify records."""
import base64
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _line(instance="firefox.instance_9", status="Playing", artist="Artist", title="Title", album="",
          url="", art="", pos="61000000", length="245000000", trackid="'/org/mpris/track/1'"):
    return "|||".join([instance, status, artist, title, album, url, art, pos, length, trackid])


def test_poll_line_reads_player_position_and_length():
    p = media.parse_poll_line(_line())
    assert (p["status"], p["app"], p["position"], p["length"]) == ("Playing", "firefox", 61.0, 245.0)
    assert p["trackid"] == "/org/mpris/track/1"          # playerctl quotes object paths
    assert p["id"] == "playerctl:firefox.instance_9"


def test_poll_line_without_length_reports_none():
    # Browsers often publish no mpris:length; the UI then hides the progress bar.
    p = media.parse_poll_line(_line(instance="plasma-browser-integration", status="Paused", length=""))
    assert p["length"] is None
    assert media.parse_poll_line("a|||b") is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="MPRIS is Linux only")
def test_poll_reads_every_player_and_falls_back_without_trunc():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[-1])
        if "trunc(" in cmd[-1]:   # playerctl before 2.4
            return MagicMock(stdout=b"", stderr=b"Could not execute command: unknown template function: trunc")
        out = _line() + "\n" + _line(instance="spotify", status="Paused", title="Song") + "\n" \
            + _line(instance="vlc", status="Stopped") + "\n"
        return MagicMock(stdout=out.encode(), stderr=b"")

    with patch.object(media, "_playerctl_path", return_value="/usr/bin/playerctl"), \
         patch.object(media.subprocess, "run", side_effect=run), \
         patch.object(media, "_bus_props", return_value=None), \
         patch.dict(media._poll, {"format": 0}):
        players = media.poll_mpris_players()
        assert [p["name"] for p in players] == ["Firefox", "Spotify"]     # Stopped is left out
        assert media._poll["format"] == 1 and len(calls) == 2
        media.poll_mpris_players()
        assert len(calls) == 3                                             # remembered


def _p(id="a", app="vlc", status="Playing", started=0.0, title="T", album="", url="", length=None,
       caps=None, **extra):
    return media._record(id=id, via="mpris", raw=app, name=app, status=status, title=title,
                         album=album, url=url, length=length, caps=caps, started=started, **extra)


def test_playing_beats_a_paused_spotify():
    # The old priority list showed a paused Spotify over a playing browser.
    spotify = _p("s", "spotify", "Paused", started=0.0)
    browser = _p("b", "firefox", "Playing", started=5.0)
    assert media.pick_source([spotify, browser])["id"] == "b"


def test_latest_to_start_wins_then_the_paused_one_played_last():
    a, b = _p("a", started=10.0), _p("b", started=20.0)
    assert media.pick_source([a, b])["id"] == "b"
    a2, b2 = _p("a", status="Paused", started=30.0), _p("b", status="Paused", started=20.0)
    assert media.pick_source([a2, b2])["id"] == "a"
    # Never played since we started watching: Spotify first.
    assert media.pick_source([_p("v", "vlc", "Paused"), _p("s", "spotify", "Paused")])["id"] == "s"
    assert media.pick_source([_p("x", status="Stopped")]) is None


def test_pin_holds_until_the_player_closes_or_another_starts():
    paused, playing = _p("yt", "firefox", "Paused", started=5.0), _p("sp", "spotify", started=8.0)
    pin = {"id": "yt", "at": 10.0}
    assert media.resolve_pin([paused, playing], pin) == pin
    assert media.pick_source([paused, playing], pin)["id"] == "yt"
    restarted = _p("sp", "spotify", started=12.0)                 # started after the pick
    assert media.resolve_pin([paused, restarted], pin) is None
    assert media.resolve_pin([playing], pin) is None              # the pinned one closed


def test_browser_bridge_copy_is_dropped():
    own = _p("f", "firefox", title="Video")
    bridge = _p("p", "plasma-browser-integration", title="Video")
    other = _p("c", "chromium", title="Other tab")
    assert [p["id"] for p in media.dedupe([own, bridge, other])] == ["p", "c"]


YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_bridge_copy_with_the_tab_title_gives_way_to_the_browsers_own():
    # Seen on YouTube in Chrome: the bridge could not read the page's media
    # metadata and published the tab's title, unread count first, and no cover.
    own = _p("mpris:chromium.instance7", "chromium.instance7", title="Never Gonna Give You Up",
             art="file:///tmp/.org.chromium.Chromium.abc", length=213.0, started=4.0)
    own["artist"] = "Rick Astley"
    bridge = _p("mpris:plasma-browser-integration", "plasma-browser-integration",
                title="(3) Never Gonna Give You Up - YouTube", url=YT, length=212.6, started=5.0)
    [shown] = media.dedupe([bridge, own])
    assert shown["id"] == own["id"] and shown["title"] == "Never Gonna Give You Up"
    assert shown["artist"] == "Rick Astley" and shown["art"] == own["art"]
    assert shown["url"] == YT and shown["started"] == 5.0          # the page url joins it
    assert media.media_kind(shown) == "long"                         # a video page, not music


@pytest.mark.parametrize("tab_title", [
    "(3) Video", "(99+) Video", "[2] Video", "▶ Video", "Video - YouTube", "(1) Video – YouTube",
])
def test_tab_title_forms_match(tab_title):
    own = _p("own", "chromium", title="Video")
    bridge = _p("bridge", "plasma-browser-integration", title=tab_title, url=YT)
    assert [p["id"] for p in media.dedupe([own, bridge])] == ["own"]


def test_bridge_metadata_beats_the_browsers_tab_title():
    # The other way round: the bridge read the metadata, the browser has the
    # tab's title. The bridge's copy stays and takes the cover it lacks.
    own = _p("own", "firefox", title="(2) Video - YouTube", art="file:///tmp/cover.png")
    bridge = _p("bridge", "plasma-browser-integration", title="Video", url=YT)
    [shown] = media.dedupe([own, bridge])
    assert shown["id"] == "bridge" and shown["title"] == "Video"
    assert shown["art"] == "file:///tmp/cover.png"


def test_different_tabs_stay_apart():
    bridge = _p("bridge", "plasma-browser-integration", title="Song - Remix", url="https://example.com/x")
    own = _p("own", "chromium", title="Song")
    assert len(media.dedupe([bridge, own])) == 2                     # "Remix" is not the site
    no_url = _p("bridge", "plasma-browser-integration", title="Song - YouTube")
    assert len(media.dedupe([no_url, own])) == 2                     # no page to name
    long_one = _p("own", "chromium", title="Video", length=600.0)
    short_one = _p("bridge", "plasma-browser-integration", title="(1) Video", url=YT, length=212.0)
    assert len(media.dedupe([short_one, long_one])) == 2             # two videos, one title
    other_app = _p("own", "vlc", title="Video")                      # not a browser
    assert len(media.dedupe([short_one, other_app])) == 2


def test_local_spotify_app_takes_the_web_api_details():
    local = _p("mpris:spotify", "spotify", title="Song")
    api = _p("spotify:api", "spotify", title="song", art="https://i.scdn.co/x", episode=True,
             item_id="42", item_type="episode", can_like=True)
    merged = media.merge_spotify([local], api)
    assert len(merged) == 1 and merged[0]["id"] == "mpris:spotify"     # controlled locally
    assert (merged[0]["art"], merged[0]["item_id"], merged[0]["episode"]) == ("https://i.scdn.co/x", "42", True)
    remote = media.merge_spotify([_p("v", "vlc")], api)               # a phone or a speaker
    assert [p["id"] for p in remote] == ["v", "spotify:api"]


def test_web_api_a_track_behind_does_not_take_the_card():
    # After ⏭ the Web API still reports the old track for a moment. It used to
    # join as a second source and, newer to start (signed in after Spotify
    # started), take the card and the keys: the old title stayed up and the
    # next tap went through the Web API instead of the app.
    local = _p("mpris:spotify", "spotify", title="New song", started=100.0)
    api = _p("spotify:api", "spotify", title="Old song", started=200.0, art="https://i.scdn.co/old",
             item_id="old", item_type="track", can_like=True)
    merged = media.merge_spotify([local], api)
    assert [p["id"] for p in merged] == ["mpris:spotify"]
    assert media.pick_source(merged)["title"] == "New song"
    # Nothing of the old track carries over; only that the API can like.
    assert merged[0]["api_behind"] and merged[0]["can_like"]
    assert "item_id" not in merged[0] and merged[0]["art"] == ""
    # The app open but stopped: the API's playback is elsewhere (a phone).
    stopped = media.merge_spotify([_p("mpris:spotify", "spotify", "Stopped", title="")], api)
    assert [p["id"] for p in stopped] == ["mpris:spotify", "spotify:api"]


@pytest.mark.parametrize("record, kind", [
    (_p(app="jelliumdesktop", album="Season 4", length=1501.7), "long"),    # an episode
    (_p(app="jelliumdesktop", album="Album", length=240), "music"),          # Jellyfin music
    (_p(app="firefox", length=300), "long"),                                 # YouTube: no album
    (_p(app="plasma-browser-integration", url="https://music.youtube.com/watch?v=x", length=200), "music"),
    (_p(app="chromium", album="Album", length=200), "music"),                # a music site with an album
    (_p(app="vlc", length=5400), "long"),                                    # a film
    (_p(app="vlc", album="Album", length=240), "music"),
    (_p(app="chrome", caps={"prev": False, "next": False, "seek": False}), "music"),   # a live stream
    (_p(app="spotify", length=200, episode=True), "long"),                   # a podcast
    (_p(app="io.github.celluloid_player.celluloid", length=120), "long"),    # a video-only app
    (_p(app="spotify", length=200), "music"),
])
def test_media_kind(record, kind):
    assert media.media_kind(record) == kind


@pytest.mark.parametrize("position, caps, plan", [
    (42.0, {"prev": True, "seek": True}, "restart"),
    (2.0, {"prev": True, "seek": True}, "prev"),
    (2.0, {"prev": False, "seek": True}, "restart"),     # Jellium's first episode, a single video
    (42.0, {"prev": True, "seek": False}, "prev"),       # cannot seek: let the player decide
    (None, {"prev": True, "seek": True}, "prev"),
])
def test_prev_plan(position, caps, plan):
    assert media.prev_plan(position, caps) == plan


def test_data_cover_is_decoded_once_and_served_from_memory():
    jpeg = b"\xff\xd8\xff\xe0" + bytes(5000) + b"\xff\xd9"
    p = _p(art="data:image/jpeg;base64," + base64.b64encode(jpeg).decode())
    url = media.resolve_art(p)
    assert url.startswith("/api/local_art?h=") and len(url) < 64
    assert media.get_cover(url.split("h=")[1]) == (jpeg, "image/jpeg")
    with patch.object(media, "_data_cover") as decode:
        assert media.resolve_art(p) == url          # same track: no second decode
        decode.assert_not_called()
    svg = _p(id="s", art="data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode())
    assert media.resolve_art(svg) in ("", "WAITING")    # never served: an SVG can carry script


def test_cover_waits_for_a_new_track_then_gives_up():
    p = _p(id="w", title="New", art="")
    assert media.resolve_art(p) == "WAITING" and media.is_mpris_burst_active()
    with patch.object(media.time, "monotonic", return_value=time.monotonic() + media.ART_WAIT_S + 1):
        assert media.resolve_art(p) == "" and not media.is_mpris_burst_active()
    assert media.resolve_art(_p(id="w", art="https://example.test/c.jpg")) == "https://example.test/c.jpg"


@pytest.mark.parametrize("actions, caps", [
    ({"disallows": {"skipping_prev": True}}, {"prev": False, "next": True, "seek": True}),
    ({"seeking": True, "skipping_next": True}, {"prev": True, "next": False, "seek": False}),
    ({}, {"prev": True, "next": True, "seek": True}),
])
def test_spotify_caps_read_both_shapes(actions, caps):
    assert media.spotify_caps({"actions": actions}) == caps


def test_spotify_episode_record():
    curr = {"is_playing": True, "progress_ms": 61000, "currently_playing_type": "episode",
            "item": {"id": "ep1", "name": "Episode", "uri": "spotify:episode:ep1", "duration_ms": 3600000,
                     "type": "episode", "images": [{"url": "https://i.scdn.co/ep"}],
                     "show": {"name": "The Show"}}}
    r = media.spotify_record(curr, can_like=True)
    assert (r["artist"], r["art"], r["episode"], r["item_type"]) == ("The Show", "https://i.scdn.co/ep", True, "episode")
    assert media.media_kind(r) == "long"
    assert media.public_media(r, "x")["player"] == "Spotify"


def test_spotify_token_keeps_an_older_sign_in():
    # Signed in before the like key's scopes: still plays, likes stay off.
    oauth = MagicMock()
    oauth.cache_handler.get_cached_token.return_value = {
        "access_token": "a", "refresh_token": "r", "scope": "user-read-playback-state user-modify-playback-state"}
    oauth.is_token_expired.return_value = True
    oauth.refresh_access_token.return_value = {"access_token": "b", "refresh_token": "r",
                                               "scope": "user-library-modify (spotipy writes the requested ones)"}
    info = media.spotify_token(oauth)
    assert info["access_token"] == "b"
    assert not media.spotify_can_like(info)
    assert media.spotify_can_like({"scope": "user-read-playback-state user-library-modify"})


def test_monitor_metadata():
    import mpris_monitor
    md = {"xesam:title": "T", "xesam:artist": ["A", "B"], "mpris:length": 1501927000,
          "mpris:trackid": "/x/track/1", "mpris:artUrl": "file:///tmp/a.png"}
    assert mpris_monitor.parse_metadata(md) == {
        "title": "T", "artist": "A, B", "album": "", "url": "", "art": "file:///tmp/a.png",
        "trackid": "/x/track/1", "length": 1501.9}
    rec = media.from_monitor([{**mpris_monitor.parse_metadata(md), "bus": "org.mpris.MediaPlayer2.JelliumDesktop.instance_1",
                               "identity": "Jellium Desktop", "status": "Playing", "position": 3.0,
                               "caps": {"prev": False, "next": True, "seek": True}, "started": 1.0}])[0]
    assert (rec["name"], rec["app"], rec["id"]) == ("Jellium Desktop", "jelliumdesktop", "mpris:JelliumDesktop.instance_1")
