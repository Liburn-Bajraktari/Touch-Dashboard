"""WinScreen against real Windows (the Windows CI runner, a Windows PC): a
fake WoW window (fake_wow_window.py) read back through Windows.Graphics.Capture
and through GDI, and Direct3D 11 on whatever adapter there is. Skipped
elsewhere. What a runner cannot do (no WGC on a server edition, no foreground
window) is skipped with the reason, and written to the job's summary."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

HERE = Path(__file__).resolve().parent
PAYLOAD = "Touch Dashboard probe"
# Inside the hosted runner's 1024 x 768 screen: Windows shrinks a wider window
# to fit (1280 came out 1028).
W, H = 800, 450


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- {line}\n")


class Window:
    def __init__(self, *args):
        self.p = subprocess.Popen([sys.executable, str(HERE / "fake_wow_window.py"), *args],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        line = self.p.stdout.readline()
        assert line.startswith("ready "), line
        self.hwnd = int(line.split()[1])

    def cmd(self, line: str) -> None:
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def close(self) -> None:
        if self.p.poll() is None:
            self.cmd("quit")
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()


@pytest.fixture
def wow():
    w = Window("--width", str(W), "--height", str(H), "--payload", PAYLOAD)
    yield w
    w.close()


def until(fn, timeout=3.0):
    end = time.monotonic() + timeout
    while True:
        got = fn()
        if got or time.monotonic() > end:
            return got
        time.sleep(0.05)


def read_link(s):
    from wow_addons import link
    size = s.size()
    if not size:
        return None
    rgb = s.grab(0, 0, size[0], link.ROWS)
    for x in link.locate(rgb, size[0]) if rgb else []:
        page = link.read_row(rgb, size[0], x)
        if page:
            return page
    return None


def test_direct3d11_makes_a_device_and_a_staging_texture():
    from wow_addons.screen_win import _D3D11
    try:
        gpu = _D3D11()
    except ImportError as e:
        pytest.skip(f"pywinrt is not installed: {e}")
    try:
        assert gpu._staging_for(1280, 4) and gpu._staging_for(800, 4)     # made again for a new width
    finally:
        gpu.close()


@pytest.mark.parametrize("via", ["wgc", "gdi"])
def test_the_strip_and_the_beacon_are_read_from_the_window(wow, monkeypatch, via):
    from wow_addons.screen_win import FORCE, WinScreen, _wgc_capture
    if via == "gdi":
        monkeypatch.setenv(FORCE, "gdi")
    else:
        try:
            from winrt.windows.graphics.capture import GraphicsCaptureSession
            supported = GraphicsCaptureSession.is_supported()
        except ImportError as e:
            pytest.skip(f"pywinrt is not installed: {e}")
        if not supported:
            summary("WGC: not supported on this machine")
            pytest.skip("Windows.Graphics.Capture is not supported here")

        def wgc_only(hwnd, api):
            return _wgc_capture(hwnd, api)

        def no_fallback(hwnd, api):
            raise RuntimeError("the WGC test takes no fallback")
    s = WinScreen() if via == "gdi" else WinScreen(capture=wgc_only, fallback=no_fallback)
    assert until(lambda: s.size() == (W, H)), s.size()
    page = until(lambda: read_link(s))
    if page is None and via == "wgc":
        summary("WGC: the capture started but no frame was read")
    assert page is not None and page.payload == PAYLOAD.encode()
    assert until(lambda: s.beacon() is True)
    wow.cmd("beacon 0")
    assert until(lambda: s.beacon() is False)
    summary(f"{via.upper()}: strip and beacon read")


def test_wgc_reads_the_window_under_another_one(wow):
    from wow_addons.screen_win import WinScreen, _wgc_capture
    try:
        from winrt.windows.graphics.capture import GraphicsCaptureSession
        if not GraphicsCaptureSession.is_supported():
            pytest.skip("Windows.Graphics.Capture is not supported here")
    except ImportError as e:
        pytest.skip(f"pywinrt is not installed: {e}")
    cover = Window("--title", "Cover", "--cls", "TDCoverWindow", "--topmost", "--at", "100", "100",
                   "--width", str(W), "--height", "200")
    try:
        s = WinScreen(capture=lambda h, api: _wgc_capture(h, api),
                      fallback=lambda h, api: (_ for _ in ()).throw(RuntimeError("no fallback")))
        page = until(lambda: read_link(s))
        summary(f"WGC under a covering window: {'read' if page else 'not read'}")
        assert page is not None and page.payload == PAYLOAD.encode()
    finally:
        cover.close()


def test_a_window_that_goes_cannot_be_seen_and_is_not_in_front(wow):
    from wow_addons.screen_win import WinScreen
    s = WinScreen()
    assert until(lambda: s.size() is not None)
    front = s.front()
    summary(f"front() with the fake window just opened: {front}")   # Windows may refuse it the foreground
    wow.close()
    assert until(lambda: s.size() is None)
    assert s.front() in (False, None) and s.beacon() is None
