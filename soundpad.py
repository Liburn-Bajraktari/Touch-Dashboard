"""
soundpad.py — Soundpad (leppsoft.com), the soundboard on Windows.

Windows has no way of its own to play a sound into the microphone, and
Soundpad does: it plays a sound to the speakers, the mic, or both. It is
driven through its remote control pipe, \\\\.\\pipe\\sp_remote_control, with
the protocol of Leppsoft's own client (SoundpadRemoteControl.java): a command
as text, written whole; the reply is what Soundpad then writes back, "R-200"
when a command worked, "R-<code>: …" when not, the value itself for a Get.
One request at a time on one connection (overlapping requests break the pipe).

Up to v0.16.4 every pad press started Soundpad.exe again with `-rc
DoPlaySound(n)`, from one of two fixed folders, and a pad was the sound's
number typed in by hand. Now pads pick a sound from Soundpad's own list and
keep its file, so reordering the list in Soundpad does not change what a pad
plays.

The pipe is read only as far as Soundpad has written (PeekNamedPipe): a
read waiting on a synchronous pipe would hold everything else.
"""
from __future__ import annotations

import logging
import ntpath
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

PIPE = r"\\.\pipe\sp_remote_control"
EXE = "soundpad.exe"
TIMEOUT_S = 2.0       # a reply must start within this
QUIET_S = 0.05        # and has ended once nothing more comes for this long
START_WAIT_S = 8.0    # Soundpad, started from here, has this long to open its pipe


class SoundpadError(Exception):
    """Soundpad answered, but not R-200: its words are the message."""


def parse_soundlist(xml: str) -> list[dict]:
    """GetSoundlist()'s answer: [{index, title, path}] in Soundpad's order.

    Leppsoft documents it only as "xml formatted": each <Sound> carries its
    index and its file (url); a title when the file has tags, else the
    file's name stands in."""
    start = xml.find("<")
    if start < 0:
        return []
    root = ET.fromstring(xml[start:])
    out = []
    for el in root.iter("Sound"):
        try:
            index = int(el.get("index", ""))
        except ValueError:
            continue
        path = el.get("url") or ""
        title = (el.get("title") or "").strip() or os.path.splitext(ntpath.basename(path))[0] or f"Sound {index}"
        out.append({"index": index, "title": title, "path": path})
    return out


def _quoted(text: str) -> str:
    if '"' in text or "\n" in text:
        raise ValueError("Soundpad takes no quotes or line breaks in a text argument")
    return f'"{text}"'


class Soundpad:
    """Its remote control. `opener()` → a pipe (read/write/flush/close),
    `waiting(pipe)` → bytes waiting or None (closed): the tests pass stand-ins."""

    def __init__(self, opener=None, waiting=None, clock=time.monotonic, sleep=time.sleep):
        self._opener = opener or _open_pipe
        self._waiting = waiting or _pipe_waiting
        self._clock, self._sleep = clock, sleep
        self._pipe = None
        self._lock = threading.Lock()

    def _close(self) -> None:
        if self._pipe is not None:
            try:
                self._pipe.close()
            except OSError:
                pass
            self._pipe = None

    def _read(self, until: str | None) -> str:
        deadline = self._clock() + TIMEOUT_S
        out = bytearray()
        quiet = None
        while True:
            n = self._waiting(self._pipe)
            if n is None:
                raise ConnectionError("Soundpad closed its pipe")
            if n:
                out += self._pipe.read(n)
                quiet = self._clock() + QUIET_S
                if until and out.rstrip().endswith(until.encode()):
                    break
                continue
            now = self._clock()
            if out and now >= quiet:
                break
            if not out and now >= deadline:
                raise TimeoutError("Soundpad did not answer")
            self._sleep(0.005)
        return out.decode("utf-8", errors="replace").strip()

    def request(self, command: str, until: str | None = None) -> str:
        """Soundpad's reply to one command. OSError when it is not running or
        does not answer (a pipe it closed, as when it restarted, is tried once more)."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._pipe is None:
                        self._pipe = self._opener()
                    self._pipe.write(command.encode("utf-8"))
                    self._pipe.flush()
                    return self._read(until)
                except TimeoutError:            # it hangs: asking again would only wait as long again
                    self._close()
                    raise
                except OSError:
                    self._close()
                    if attempt == 2:
                        raise
        raise AssertionError("unreachable")

    def _do(self, command: str) -> None:
        reply = self.request(command)
        if not reply.startswith("R-200"):
            raise SoundpadError(reply or f"no answer to {command}")

    # ── what the dashboard asks ───────────────────────────────────────────

    def alive(self) -> bool:
        try:
            return self.request("IsAlive()").startswith("R-200")
        except OSError:
            return False

    def play(self, index: int, speakers: bool = True, mic: bool = True) -> None:
        """Play sound `index` (All sounds' numbering) to the speakers, the mic, or both."""
        self._do(f"DoPlaySound({int(index)}, {str(bool(speakers)).lower()}, {str(bool(mic)).lower()})")

    def stop(self) -> None:
        self._do("DoStopSound()")

    def set_volume(self, volume: int) -> None:
        self._do(f"SetVolume({max(0, min(100, int(volume)))})")

    def volume(self) -> int:
        return int(self.request("GetVolume()"))

    def is_trial(self) -> bool:
        try:
            return int(self.request("IsTrial()")) == 1
        except (OSError, ValueError):
            return False

    def add(self, path: str) -> None:
        """Add a file to Soundpad's list (not allowed in its trial)."""
        self._do(f"DoAddSound({_quoted(path)})")

    def sounds(self) -> list[dict]:
        """Soundpad's list as it is now. Read whole every time: no event says
        it changed, and a reorder keeps its count."""
        return parse_soundlist(self.request("GetSoundlist()", until="</Soundlist>"))

    def index_of(self, pad: dict) -> int:
        """The sound a pad plays now: found again by its file, since reordering
        Soundpad's list renumbers it; else the number it was saved with."""
        path = str(pad.get("path") or "")
        if path:
            for s in self.sounds():
                if s["path"].lower() == path.lower():
                    return s["index"]
        index = str(pad.get("id", ""))
        if not re.fullmatch(r"[0-9]{1,6}", index):
            raise ValueError("a Soundpad pad needs its sound's number")
        return int(index)


# ── Windows ──────────────────────────────────────────────────────────────────

def _open_pipe():
    if not sys.platform.startswith("win"):
        raise FileNotFoundError("Soundpad runs on Windows")
    return open(PIPE, "r+b", buffering=0)


def _pipe_waiting(pipe) -> int | None:
    import ctypes
    import msvcrt
    avail = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.PeekNamedPipe(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None,
                                              ctypes.byref(avail), None)
    return avail.value if ok else None


def _steam_libraries() -> list[str]:
    """Steam's library folders: its own, and those libraryfolders.vdf lists."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = str(winreg.QueryValueEx(key, "SteamPath")[0]).replace("/", "\\")
    except OSError:
        return []
    out = [steam]
    try:
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as f:
            out += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    except OSError:
        pass
    return out


def find_exe() -> str | None:
    """Soundpad.exe: the one running, else a Steam library's, else its own installer's folder."""
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            if (p.info.get("name") or "").lower() == EXE and p.info.get("exe"):
                return p.info["exe"]
    except ImportError:
        pass
    candidates = [os.path.join(lib, "steamapps", "common", "Soundpad", "Soundpad.exe") for lib in _steam_libraries()]
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(env):
            candidates.append(os.path.join(os.environ[env], "Soundpad", "Soundpad.exe"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def start(sp: Soundpad, wait: float = START_WAIT_S) -> bool:
    """Soundpad running and answering: started when it is not. False when it cannot be."""
    if sp.alive():
        return True
    exe = find_exe()
    if not exe:
        return False
    subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.25)
        if sp.alive():
            return True
    return False
