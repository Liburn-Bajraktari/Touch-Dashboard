"""Windows-only paths checked without Windows: the session lock's struct, the
Discord pipe, the launchers. (WoW's window: test_wow_screen_win.py.)"""
import ctypes
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord_ipc
import lock_monitor


# ── the session lock ─────────────────────────────────────────────────────────

def test_the_session_struct_is_laid_out_as_windows_hands_it_back():
    # WTSINFOEXW: a DWORD, then a union holding LARGE_INTEGERs (8-aligned)
    info, level1 = lock_monitor.WTSINFOEX, lock_monitor.WTSINFOEX_LEVEL1
    assert info.Data.offset == 8
    assert (level1.SessionId.offset, level1.SessionState.offset, level1.SessionFlags.offset) == (0, 4, 8)
    assert level1.LogonTime.offset == 160 and ctypes.sizeof(info) == 232
    assert ctypes.alignment(info) == 8


def test_locked_is_flags_zero_and_session_one_is_not_locked():
    # Up to v0.16.4 the session's id (1 on a desktop) was read as the flags
    # against a reversed constant: always "locked".
    raw = bytearray(ctypes.sizeof(lock_monitor.WTSINFOEX))
    struct.pack_into("<I", raw, 0, 1)                       # level 1
    struct.pack_into("<IiI", raw, 8, 1, 0, 1)               # session 1, active, unlocked
    info = lock_monitor.WTSINFOEX.from_buffer_copy(raw)
    assert info.Data.SessionId == 1 and not lock_monitor.session_locked(info.Data.SessionFlags)
    struct.pack_into("<i", raw, 16, 0)                      # locked
    assert lock_monitor.session_locked(lock_monitor.WTSINFOEX.from_buffer_copy(raw).Data.SessionFlags)
    assert not lock_monitor.session_locked(-1)              # unknown


# ── the Discord pipe ─────────────────────────────────────────────────────────

class Pipe:
    """A named pipe: bytes Discord wrote, read only as far as they go."""

    def __init__(self):
        self.data = bytearray()
        self.written = bytearray()
        self.broken = False
        self.lock = threading.Lock()

    def feed(self, opcode, payload: bytes):
        with self.lock:
            self.data += struct.pack("<II", opcode, len(payload)) + payload

    def read(self, n):
        with self.lock:
            assert self.data, "a read on an empty pipe would block"
            out, self.data = bytes(self.data[:n]), self.data[n:]
            return out

    def write(self, data):
        self.written += data

    def flush(self):
        pass


def ipc(pipe, monkeypatch):
    client = discord_ipc.DiscordIPC("id", "secret")
    client._win_pipe = pipe
    monkeypatch.setattr(discord_ipc.sys, "platform", "win32")
    monkeypatch.setattr(discord_ipc.DiscordIPC, "_pipe_waiting",
                        staticmethod(lambda p: None if p.broken else len(p.data)))
    return client


def test_a_quiet_pipe_times_out_instead_of_holding_every_write(monkeypatch):
    pipe = Pipe()
    client = ipc(pipe, monkeypatch)
    client._timeout = 0.2
    t = time.monotonic()
    assert client.recv() == {}                              # as a socket's timeout reads
    assert 0.15 < time.monotonic() - t < 1.0
    client.send(1, {"cmd": "SET_VOICE_SETTINGS"})           # nothing waits on a read
    assert b"SET_VOICE_SETTINGS" in pipe.written


def test_a_message_is_read_once_it_is_there_and_a_broken_pipe_ends_it(monkeypatch):
    pipe = Pipe()
    client = ipc(pipe, monkeypatch)
    client._timeout = 2.0
    threading.Timer(0.1, pipe.feed, args=(1, b'{"evt": "SPEAKING_START"}')).start()
    assert client.recv() == {"evt": "SPEAKING_START"}
    pipe.broken = True
    assert client.recv() is None                            # Discord went away: reconnect


def test_a_message_cut_off_halfway_is_a_broken_connection_not_a_timeout(monkeypatch):
    pipe = Pipe()
    client = ipc(pipe, monkeypatch)
    client._timeout = 0.2
    pipe.data += struct.pack("<II", 1, 40) + b'{"evt": '  # the rest never comes
    assert client.recv() is None


# ── launchers ────────────────────────────────────────────────────────────────

def test_the_browser_key_opens_the_default_browser_itself():
    import server
    firefox = r"C:\Program Files\Mozilla Firefox\firefox.exe"
    with patch("server.sys") as mock_sys, patch("server._popen") as popen, \
            patch("server._windows_default_browser", return_value=firefox):
        mock_sys.platform = "win32"
        server._launch_browser()
    popen.assert_called_once_with([firefox])                # not "http://", an address of nothing
    with patch("server.sys") as mock_sys, patch("server._popen") as popen, \
            patch("server._windows_default_browser", return_value=None):
        mock_sys.platform = "win32"
        server._launch_browser()
    popen.assert_called_once_with(["cmd.exe", "/c", "start", "", "msedge"])


def test_task_manager_goes_through_the_shell():
    import server
    with patch("server.sys") as mock_sys, patch("server.os.startfile", create=True) as start:
        mock_sys.platform = "win32"
        server._launch_task_manager()
    start.assert_called_once_with("taskmgr.exe")            # CreateProcess refuses it with 740


def test_console_programs_start_without_a_console_window(monkeypatch):
    import server
    seen = {}
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(server.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kw: seen.update(kw))
    server._popen(["wt.exe"])
    assert seen["creationflags"] == 0x08000000 and "start_new_session" not in seen


# ── start with Windows ───────────────────────────────────────────────────────

def test_start_with_windows_is_the_run_value_install_ps1_writes(monkeypatch, tmp_path):
    import types
    import server
    run = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    reg = types.ModuleType("winreg")
    reg.HKEY_CURRENT_USER, reg.KEY_QUERY_VALUE, reg.KEY_SET_VALUE, reg.REG_SZ = 1, 1, 2, 1
    reg.OpenKey = lambda root, path, res, access: Key()
    reg.SetValueEx = lambda key, name, res, kind, value: run.__setitem__(name, value)

    def delete(key, name):
        if name not in run:
            raise FileNotFoundError(name)
        del run[name]

    def query(key, name):
        if name not in run:
            raise FileNotFoundError(name)
        return run[name], 1
    reg.DeleteValue, reg.QueryValueEx = delete, query
    monkeypatch.setitem(sys.modules, "winreg", reg)
    monkeypatch.setattr(server, "get_os_target", lambda: "windows")
    monkeypatch.setattr(server, "BASE_DIR", str(tmp_path))
    (tmp_path / "TouchDashboard.vbs").write_text("")
    assert server._autostart() is False
    assert server._autostart(True) is True
    assert run == {"TouchDashboard": f'wscript.exe "{tmp_path / "TouchDashboard.vbs"}"'}   # install.ps1's launcher
    assert server._autostart(False) is False and run == {}
    assert server._autostart(False) is False                  # already off
    monkeypatch.setattr(server, "get_os_target", lambda: "linux")
    assert server._autostart() is None
