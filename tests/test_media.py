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
    bridge = _p("p", "plasma-browser-integration", title="Video", url=YT, art="file:///tmp/b.jpg")
    other = _p("c", "chromium", title="Other tab")
    kept = media.dedupe([own, bridge, other])
    assert [p["id"] for p in kept] == ["f", "c"]                     # the browser's own stays
    assert (kept[0]["url"], kept[0]["art"]) == (YT, "file:///tmp/b.jpg")


def test_bridge_copy_of_a_browser_process_is_dropped_whatever_its_title():
    # Seen with Firefox: the bridge names the browser's process (kde:pid), the
    # one behind Firefox's own player. Titles and lengths may disagree for a
    # while (an ad, the next video), and both showed as sources.
    own = _p("mpris:firefox.instance_1_87", "firefox.instance_1_87", title="Ad", length=15.0,
             url=YT, pid=5191)
    bridge = _p("mpris:plasma-browser-integration", "plasma-browser-integration", title="Video",
                length=661.3, art="file:///tmp/b.jpg", pid=5792, media_pid=5191, started=3.0)
    [shown] = media.dedupe([own, bridge])
    assert shown["id"] == own["id"] and shown["title"] == "Ad"
    assert (shown["art"], shown["length"]) == ("", 15.0)             # another item: nothing lent
    assert shown["started"] == 3.0
    # Not "Chromium" from Chrome's bus name: the bridge names the browser.
    chrome = _p("mpris:chromium.instance7", "chromium.instance7", title="Video", pid=7)
    named = dict(bridge, name="Google Chrome", media_pid=7)
    assert media.dedupe([chrome, named])[0]["name"] == "Google Chrome"
    # Any player naming another's process is a copy, not only KDE's.
    relay = _p("r", "relay", title="Video", media_pid=5191)
    assert [p["id"] for p in media.dedupe([own, relay])] == [own["id"]]


def test_bridge_of_another_process_or_a_stopped_browser_stays():
    own = _p("own", "firefox", title="Song", pid=5191)
    elsewhere = _p("bridge", "plasma-browser-integration", title="Video", pid=5792, media_pid=4000)
    assert len(media.dedupe([own, elsewhere])) == 2                 # another browser's tab
    stopped = _p("own", "firefox", status="Stopped", title="", pid=5191)
    paused = _p("bridge", "plasma-browser-integration", status="Paused", title="Video", media_pid=5191)
    assert [p["id"] for p in media.dedupe([stopped, paused])] == ["own", "bridge"]


YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _firefox(status="Paused", position=55.0):
    return _p("mpris:firefox.instance_1_9", "firefox.instance_1_9", status, title="Video",
              length=661.3, position=position, pid=900810)


def _bridge(status="Paused", title="Video", position=55.0):
    return _p("mpris:plasma-browser-integration", "plasma-browser-integration", status,
              title=title, url=YT, length=661.3, position=position, media_pid=900810)


def _ids(players):
    return [p["id"] for p in players]


@pytest.fixture
def copies(monkeypatch):
    monkeypatch.setattr(media, "_copies", media._Copies())


def test_a_copy_its_browser_let_go_leaves_the_card(copies):
    # Seen with Firefox: the video's window closed, Firefox's player left, and
    # the bridge's stayed Paused on the video for good.
    bridge = _bridge()
    assert _ids(media.local_sources([_firefox(), bridge], now=0.0)) == ["mpris:firefox.instance_1_9"]
    assert _ids(media.local_sources([bridge], now=1.0)) == [bridge["id"]]     # it may come back
    assert _ids(media.local_sources([bridge], now=3.9)) == [bridge["id"]]
    assert media.local_sources([bridge], now=4.0) == []


def test_a_copy_its_browser_stopped_leaves_the_card(copies):
    # Chrome's player stays on the bus and goes Stopped instead of leaving.
    chrome = _p("mpris:chromium.instance7", "chromium.instance7", "Paused", title="Video", pid=900810)
    bridge = _bridge()
    assert _ids(media.local_sources([chrome, bridge], now=0.0)) == [chrome["id"]]
    stopped = dict(chrome, status="Stopped", title="")
    assert _ids(media.local_sources([stopped, bridge], now=1.0)) == [stopped["id"], bridge["id"]]
    assert _ids(media.local_sources([stopped, bridge], now=4.0)) == [stopped["id"]]


def test_a_copy_resumed_in_the_tab_waits_for_the_browser_again(copies):
    media.local_sources([_firefox(), _bridge()], now=0.0)
    media.local_sources([_bridge()], now=1.0)
    playing = _bridge("Playing")
    assert _ids(media.local_sources([playing], now=3.5)) == [playing["id"]]   # its status changed
    assert _ids(media.local_sources([_firefox("Playing"), playing], now=4.0)) == ["mpris:firefox.instance_1_9"]
    assert _ids(media.local_sources([playing], now=5.0)) == [playing["id"]]
    assert media.local_sources([playing], now=8.0) == []


def test_a_copy_on_another_item_or_never_carried_stays(copies):
    media.local_sources([_firefox(), _bridge()], now=0.0)
    other = _bridge(title="Next video", position=2.0)        # the tab went on; its browser player follows
    assert _ids(media.local_sources([other], now=10.0)) == [other["id"]]
    # A bridge with no browser player behind it (one that publishes none).
    alone = _p("bridge", "plasma-browser-integration", "Paused", title="Song", length=200.0, position=80.0)
    assert _ids(media.local_sources([alone], now=0.0)) == ["bridge"]
    assert _ids(media.local_sources([alone], now=100.0)) == ["bridge"]
    assert _ids(media.local_sources([_p("v", "vlc", "Paused", length=60.0, position=60.0)], now=0.0)) == ["v"]


def test_a_copy_paused_at_its_end_leaves_the_card(copies):
    # Firefox stops controlling media that ended. Found that way (the server
    # started later), nothing says a browser carried it.
    ended = _bridge(position=661.3)
    assert _ids(media.local_sources([ended], now=0.0)) == [ended["id"]]
    assert media.local_sources([ended], now=3.0) == []
    near = _bridge("Playing", position=661.3)                # still playing: not ended
    assert _ids(media.local_sources([near], now=10.0)) == [near["id"]]


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


def test_bridge_metadata_replaces_the_browsers_tab_title():
    # The other way round: the bridge read the metadata, the browser has the
    # tab's title. The browser's own player stays and shows the bridge's.
    own = _p("own", "firefox", title="(2) Video - YouTube", art="file:///tmp/cover.png")
    bridge = _p("bridge", "plasma-browser-integration", title="Video", url=YT)
    bridge["artist"] = "Channel"
    [shown] = media.dedupe([own, bridge])
    assert shown["id"] == "own" and (shown["title"], shown["artist"]) == ("Video", "Channel")
    assert shown["art"] == "file:///tmp/cover.png" and shown["url"] == YT


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
        "trackid": "/x/track/1", "length": 1501.9, "media_pid": None}
    assert mpris_monitor.parse_metadata({"kde:pid": 5191})["media_pid"] == 5191
    assert mpris_monitor.parse_metadata({"kde:pid": "x"})["media_pid"] is None
    rec = media.from_monitor([{**mpris_monitor.parse_metadata(md), "bus": "org.mpris.MediaPlayer2.JelliumDesktop.instance_1",
                               "identity": "Jellium Desktop", "status": "Playing", "position": 3.0,
                               "caps": {"prev": False, "next": True, "seek": True}, "started": 1.0,
                               "pid": 42}])[0]
    assert (rec["name"], rec["app"], rec["id"]) == ("Jellium Desktop", "jelliumdesktop", "mpris:JelliumDesktop.instance_1")
    assert (rec["pid"], rec["media_pid"]) == (42, None)


# ── MPRIS monitor: brief pauses ──────────────────────────────────────────────

FF_BUS = "org.mpris.MediaPlayer2.firefox.instance_1_87"


class _Params:
    def __init__(self, *values):
        self._values = values

    def unpack(self):
        return self._values


def _monitor(status="Playing"):
    """A monitor with one player and no D-Bus: timers are collected, not run."""
    import mpris_monitor
    m = mpris_monitor.MprisMonitor()
    m._owners[":1.5"] = FF_BUS
    m._players[FF_BUS] = {
        "identity": "Firefox", "status": status, "meta": mpris_monitor.parse_metadata({}),
        "position": 10.0, "position_at": time.monotonic(), "rate": 1.0,
        "caps": {"prev": True, "next": True, "seek": True}, "started": 1.0, "pid": None,
        "held": None, "held_at": 0.0,
    }
    timers, notified = [], []
    m._later = lambda seconds, fn, *args: timers.append((seconds, fn, args))
    m.position = lambda bus: None
    m.on_change = lambda: notified.append(1)
    return m, timers, notified


def _status(m, status):
    m._on_properties_changed(None, ":1.5", None, None, None,
                             _Params("org.mpris.MediaPlayer2.Player", {"PlaybackStatus": status}, []))


def _run(timers):
    for _, fn, args in timers:
        fn(*args)
    timers.clear()


def test_a_blip_of_pause_never_shows():
    # Firefox reports ~100 ms pauses every few seconds while a video buffers.
    import mpris_monitor
    m, timers, _ = _monitor()
    _status(m, "Paused")
    assert m.players()[0]["status"] == "Playing"          # held
    assert timers[0][0] == mpris_monitor.PAUSE_SETTLE_S
    _status(m, "Playing")
    _run(timers)
    p = m.players()[0]
    assert p["status"] == "Playing"
    assert p["started"] == 1.0                            # not "started again": the card stays put


def test_a_pause_that_lasts_shows_where_it_stopped():
    m, timers, notified = _monitor()
    _status(m, "Paused")
    held_at = m._players[FF_BUS]["held_at"]
    before = len(notified)
    _run(timers)
    p = m._players[FF_BUS]
    assert p["status"] == "Paused" and p["held"] is None
    assert p["position_at"] == held_at                    # frozen at the pause, not at the timer
    assert len(notified) == before + 1


def test_a_pause_after_a_blip_waits_its_own_time():
    m, timers, _ = _monitor()
    _status(m, "Paused")
    _status(m, "Playing")                                 # blip
    _status(m, "Paused")                                  # a real pause, a moment later
    first, second = timers
    first[1](*first[2])                                   # the blip's timer: not this pause's
    assert m.players()[0]["status"] == "Playing"
    second[1](*second[2])
    assert m.players()[0]["status"] == "Paused"


def test_stopping_while_held_and_resuming_after():
    m, timers, _ = _monitor()
    _status(m, "Paused")
    _status(m, "Stopped")                                 # still one hold, now for Stopped
    assert len(timers) == 1
    _run(timers)
    assert m.players()[0]["status"] == "Stopped"
    _status(m, "Playing")                                 # a real resume shows at once
    p = m.players()[0]
    assert p["status"] == "Playing" and p["started"] > 1.0
