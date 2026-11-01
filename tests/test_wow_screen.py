"""Reading the companion's beacon from WoW's window (wow_addons/screen.py):
the pixel check, and what is answered when the window cannot be seen."""
from wow_addons import screen
from wow_addons.screen import BEACON, CELL_PX, NoScreen, X11Screen, beacon_seen, find_beacon


def corner(cells, *, shift=0, lit=255, dark=0, height=CELL_PX):
    """The window's top-left corner with `cells` drawn `shift` pixels to the right."""
    width = len(BEACON) * CELL_PX
    row = bytearray(width * 3)
    for x in range(width):
        i = (x - shift) // CELL_PX
        if 0 <= x - shift and i < len(cells):
            row[x * 3:x * 3 + 3] = bytes(lit if v else dark for v in cells[i])
    return bytes(row) * height, width


def test_the_beacon_is_seen_when_every_cell_shows_its_colour():
    assert beacon_seen(*corner(BEACON))
    assert beacon_seen(*corner(BEACON, shift=1))                # a pixel off: the centres still hit
    assert beacon_seen(*corner(BEACON, lit=214, dark=38))        # the game's gamma and contrast


def test_anything_else_is_not_the_beacon():
    assert not beacon_seen(*corner(BEACON[:-1] + ((0, 0, 1),)))  # one cell wrong
    assert not beacon_seen(*corner(((0, 0, 0),) * len(BEACON)))  # the game's own dark corner
    assert not beacon_seen(*corner(((1, 1, 1),) * len(BEACON)))  # a white one
    assert not beacon_seen(*corner(BEACON, lit=150))             # washed out
    assert not beacon_seen(b"", 0)


def edge(width=600, at=None, strip=None, rows=CELL_PX):
    """The window's top rows: the beacon at `at`, and 3-pixel cells (the live
    link's height) from `strip`, a list of (x, colours)."""
    out = []
    for y in range(rows):
        row = bytearray(width * 3)
        if at is not None:
            for i, cell in enumerate(BEACON):
                for x in range(at + i * CELL_PX, at + (i + 1) * CELL_PX):
                    row[x * 3:x * 3 + 3] = bytes(255 if v else 0 for v in cell)
        if strip and y < 3:
            x0, cells = strip
            for i, cell in enumerate(cells):
                for x in range(x0 + i * 3, x0 + i * 3 + 3):
                    row[x * 3:x * 3 + 3] = bytes(255 if v else 0 for v in cell)
        out.append(bytes(row))
    return b"".join(out), width


def test_the_beacon_is_found_anywhere_along_the_top_edge():
    assert find_beacon(*edge(at=480)) == 480
    assert find_beacon(*edge(at=0)) == 0
    assert find_beacon(*edge()) is None


def test_the_live_links_cells_do_not_pass_for_the_beacon():
    # The beacon's colours, in the strip's 3-pixel cells, doubled to 6: one
    # pixel short of the beacon's height, and not 4 pixels a cell
    cells = [c for c in BEACON for _ in range(2)] + list(BEACON)
    assert find_beacon(*edge(strip=(100, cells))) is None
    assert find_beacon(*edge(at=40, strip=(100, cells))) == 40


def test_without_a_display_nothing_can_be_told(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    s = X11Screen()
    assert s.front() is None and s.beacon() is None
    assert NoScreen().front() is None and NoScreen().beacon() is None


def test_other_platforms_check_nothing(monkeypatch):
    monkeypatch.setattr(screen.sys, "platform", "win32")
    assert isinstance(screen.for_platform(), NoScreen)
