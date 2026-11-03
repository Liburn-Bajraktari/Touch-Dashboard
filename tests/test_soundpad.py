"""Soundpad, the Windows soundboard (soundpad.py), through a stand-in for its
remote control pipe, and the server's pads played through it."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundpad
from soundpad import Soundpad, SoundpadError, parse_soundlist

LIST = """<?xml version="1.0" encoding="UTF-8"?>
<Soundlist>
<Sound index="1" url="C:\\Sounds\\airhorn.mp3" artist="" title="Air horn" duration="0:02"/>
<Sound index="2" url="C:\\Sounds\\bruh.wav" artist="" title="" duration="0:01"/>
<Sound index="x" url="C:\\Sounds\\broken.wav"/>
<Sound index="3" url="C:\\Sounds\\Ünïcode – sound.ogg" title="Ünïcode"/>
</Soundlist>"""


class App:
    """Soundpad at the other end of the pipe: answers as its remote control does."""

    def __init__(self, sounds=LIST):
        self.sounds = sounds
        self.commands = []
        self.volume = 70
        self.silent = False
        self.chunk = None            # reply bytes made available this many at a time

    def answer(self, command: str) -> str:
        self.commands.append(command)
        if command == "GetSoundFileCount()":
            return str(self.sounds.count('<Sound '))
        if command == "GetSoundlist()":
            return self.sounds
        if command == "GetVolume()":
            return str(self.volume)
        if command.startswith(("DoPlaySound(", "DoStopSound(", "IsAlive(", "SetVolume(", "DoAddSound(")):
            return "R-200"
        return "R-404: Command not found."


class Pipe:
    def __init__(self, app):
        self.app, self.out, self.closed = app, b"", False

    def write(self, data):
        if not self.app.silent:
            self.out += self.app.answer(data.decode()).encode()

    def flush(self):
        pass

    def read(self, n):
        data, self.out = self.out[:n], self.out[n:]
        return data

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(app=None):
    app = app or App()
    pipes = []
    clock = Clock()

    def opener():
        p = Pipe(app)
        pipes.append(p)
        return p

    def waiting(pipe):
        if pipe.closed:
            return None
        n = len(pipe.out)
        return min(n, app.chunk) if app.chunk else n

    return Soundpad(opener=opener, waiting=waiting, clock=clock, sleep=clock.sleep), app, pipes


def test_a_pad_plays_to_the_speakers_and_the_mic_as_asked():
    sp, app, _ = make()
    sp.play(12, speakers=True, mic=False)
    assert app.commands == ["DoPlaySound(12, true, false)"]
    sp.stop()
    sp.set_volume(140)                                  # Soundpad's volume ends at 100
    assert app.commands[1:] == ["DoStopSound()", "SetVolume(100)"]
    assert sp.volume() == 70


def test_anything_but_r200_is_an_error_in_soundpads_words():
    sp, app, _ = make()
    app.answer = lambda c: "R-404: Command not found."
    with pytest.raises(SoundpadError, match="R-404"):
        sp.play(1)


def test_the_list_is_read_with_its_titles_and_files_as_it_is_now():
    sp, app, _ = make()
    assert sp.sounds() == [
        {"index": 1, "title": "Air horn", "path": "C:\\Sounds\\airhorn.mp3"},
        {"index": 2, "title": "bruh", "path": "C:\\Sounds\\bruh.wav"},          # no title: the file's name
        {"index": 3, "title": "Ünïcode", "path": "C:\\Sounds\\Ünïcode – sound.ogg"},
    ]
    app.sounds = LIST.replace('<Sound index="x" url="C:\\Sounds\\broken.wav"/>',
                              '<Sound index="4" url="C:\\Sounds\\new.wav" title="New"/>')
    assert [s["title"] for s in sp.sounds()] == ["Air horn", "bruh", "New", "Ünïcode"]   # Soundpad's order


def test_a_pad_follows_its_file_when_the_list_is_reordered():
    sp, app, _ = make()
    pad = {"id": "1", "path": "c:\\sounds\\AIRHORN.mp3"}            # saved as number 1
    assert sp.index_of(pad) == 1
    app.sounds = LIST.replace('index="1"', 'index="9"')
    app.sounds = app.sounds.replace('<Sound index="x" url="C:\\Sounds\\broken.wav"/>', '<Sound index="5" url="C:\\x.wav"/>')
    assert sp.index_of(pad) == 9                       # moved in Soundpad (the count the same): still the air horn
    assert sp.index_of({"id": "2", "path": "C:\\gone.wav"}) == 2   # its file is gone: the number
    with pytest.raises(ValueError):
        sp.index_of({"id": "1); DoExit("})


def test_a_long_answer_is_read_whole_however_it_arrives():
    sp, app, _ = make(App(sounds="<Soundlist>" + "".join(
        f'<Sound index="{i}" url="C:\\s\\{i}.wav" title="Sound {i}"/>' for i in range(1, 400)) + "</Soundlist>"))
    app.chunk = 4096
    assert len(sp.sounds()) == 399


def test_soundpad_not_answering_times_out_and_a_closed_pipe_is_opened_again():
    sp, app, pipes = make()
    app.silent = True
    with pytest.raises(TimeoutError):
        sp.request("IsAlive()")
    app.silent = False
    assert sp.alive() and len(pipes) == 2              # opened again after the failed one
    pipes[-1].closed = True                            # Soundpad restarted
    assert sp.alive() and len(pipes) == 3


def test_a_press_is_one_command_and_r200_ends_it_at_once():
    sp, app, _ = make()
    soundpad.play_pad(sp, {"id": "2"}, True, True)
    assert app.commands == ["DoPlaySound(2, true, true)"]          # no IsAlive() first
    assert sp._clock.t == 0                                         # no wait for more after R-200
    soundpad.play_pad(sp, {"id": "1", "path": "C:\\Sounds\\bruh.wav"}, True, True)
    assert app.commands[1:] == ["GetSoundlist()", "DoPlaySound(2, true, true)"]
    assert sp._clock.t == 0                                         # the list ends at </Soundlist>


def test_r200_in_pieces_and_an_error_are_read_whole():
    sp, app, _ = make()
    app.chunk = 2                                                   # "R-", "20", "0"
    sp.play(1)
    app.answer = lambda c: "R-404: Command not found."
    with pytest.raises(SoundpadError, match="Command not found"):
        sp.play(1)


def test_what_is_left_of_an_earlier_reply_is_not_read_as_the_next():
    sp, app, pipes = make()
    sp.play(1)
    pipes[0].out += b" (late)"                                      # written after R-200 was read
    assert sp.request("GetVolume()") == "70"


def test_soundpad_is_started_only_when_its_pipe_is_not_there(monkeypatch):
    sp, app, pipes = make()
    real, started = sp._opener, []

    def opener():
        if not started:
            raise FileNotFoundError("no pipe")
        return real()
    sp._opener = opener
    monkeypatch.setattr(soundpad, "start", lambda s: started.append(s) or True)
    soundpad.play_pad(sp, {"id": "3"}, True, False)
    assert started == [sp] and app.commands == ["DoPlaySound(3, true, false)"]

    started.clear()
    sp._close()
    monkeypatch.setattr(soundpad, "start", lambda s: False)        # not installed
    with pytest.raises(FileNotFoundError, match="not running"):
        soundpad.play_pad(sp, {"id": "3"})


def test_soundpad_hanging_is_not_started_again(monkeypatch):
    sp, app, _ = make()
    app.silent = True
    monkeypatch.setattr(soundpad, "start", lambda s: pytest.fail("Soundpad started again"))
    with pytest.raises(TimeoutError):
        soundpad.play_pad(sp, {"id": "1"})


def test_without_soundpad_nothing_is_alive():
    def gone():
        raise FileNotFoundError("no pipe")
    assert Soundpad(opener=gone).alive() is False


def test_text_arguments_take_no_quotes():
    sp, app, _ = make()
    sp.add("C:\\Sounds\\new.wav")
    assert app.commands == ['DoAddSound("C:\\Sounds\\new.wav")']
    with pytest.raises(ValueError):
        sp.add('C:\\x" ) DoExit("')


def test_a_list_without_xml_is_empty():
    assert parse_soundlist("R-404: nope") == []


# ── the server's pads ────────────────────────────────────────────────────────

def test_a_pad_plays_through_soundpad_with_the_route_keys(monkeypatch):
    import server
    sp, app, _ = make()
    monkeypatch.setattr(server, "soundpad_rc", sp)
    monkeypatch.setattr(server._soundpad_mod, "start", lambda s: True)
    monkeypatch.setitem(server.config, "soundboard_feedback", False)       # deafened here
    monkeypatch.setitem(server.config, "soundboard_to_mic", True)
    asyncio.run(server._play_clip({"uid": "p1", "id": "1", "path": "C:\\Sounds\\bruh.wav", "name": "Bruh"},
                                  "soundpad"))
    assert app.commands[-1] == "DoPlaySound(2, false, true)"               # found by its file


def test_a_pad_soundpad_cannot_play_is_refused(monkeypatch):
    import server
    def gone():
        raise FileNotFoundError("no pipe")
    monkeypatch.setattr(server, "soundpad_rc", Soundpad(opener=gone))       # not running
    monkeypatch.setattr(server._soundpad_mod, "start", lambda s: False)    # and not installed
    with pytest.raises(ValueError, match="not running"):
        asyncio.run(server._play_clip({"uid": "p1", "id": "1"}, "soundpad"))
