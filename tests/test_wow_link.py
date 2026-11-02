"""The live link's decoder without the game: pages put together, WoW's clock,
the checksum and a short payload (the addon's side runs in test_wow_companion)."""
import pytest

from wow_addons.link import CALIB, CELL, Link, Page, fletcher16, locate, parse, symbols


def test_fletcher16_matches_the_published_values():
    assert fletcher16(b"abcde") == 0xC8F0
    assert fletcher16(b"abcdef") == 0x2057
    assert fletcher16(b"abcdefgh") == 0x0627


def test_pages_make_a_payload_in_any_order_and_only_once():
    k = Link()
    assert k.feed(Page(7, 2, 3, 100, b"BB"), 1.0) is None
    assert k.feed(Page(7, 3, 3, 100, b"C"), 1.1) is None
    assert k.feed(Page(7, 1, 3, 100, b"AA"), 1.2) == b"AABBC"
    assert k.feed(Page(7, 2, 3, 100, b"BB"), 1.3) is None          # the same payload again
    assert k.feed(Page(8, 1, 1, 100, b"new"), 1.4) == b"new"        # the next one


def test_pages_of_another_payload_are_not_mixed_in():
    k = Link()
    k.feed(Page(1, 1, 2, 100, b"old-"), 1.0)
    assert k.feed(Page(2, 2, 2, 100, b"tail"), 1.1) is None         # seq moved on: start again
    assert k.feed(Page(2, 1, 2, 100, b"head-"), 1.2) == b"head-tail"


def test_wows_clock_is_set_when_a_second_turns_and_again_after_a_restart():
    k = Link()
    k.feed(Page(1, 1, 1, 100, b"x"), 5100.3)
    assert k.wow_time(5100.3) == pytest.approx(100.5)               # until a second turns: its middle
    k.feed(Page(1, 1, 1, 101, b"x"), 5101.02)                       # seen 20 ms after it turned
    assert k.wow_time(5101.02) == pytest.approx(101.0)
    k.feed(Page(1, 1, 1, 102, b"x"), 5102.08)                       # a later, slower sighting changes nothing
    assert k.wow_time(5102.08) == pytest.approx(102.06)
    k.feed(Page(1, 1, 1, 7, b"x"), 5103.0)                          # WoW restarted: its clock began again
    assert k.wow_time(5103.0) == pytest.approx(7.5)


def test_a_short_payload_is_an_error():
    with pytest.raises(ValueError):
        parse(b"\x00\x05\x06Thr", 0.0)


def calibration(width, at, lit=255, dark=0):
    """CELL rows with the calibration cells drawn from `at`."""
    row = bytearray(width * 3)
    for s in range(CALIB):
        for x in range(at + s * CELL, at + (s + 1) * CELL):
            row[x * 3:x * 3 + 3] = bytes(lit if s & bit else dark for bit in (4, 2, 1))
    return bytes(row) * CELL


def test_symbols_reads_each_pixel_as_a_colour():
    assert symbols(bytes([0, 0, 0, 255, 0, 255, 200, 200, 200, 0, 129, 0])) == bytes([0, 5, 7, 2])
    assert symbols(bytes([0, 0, 0, 255, 0, 255, 200, 200, 200, 0, 129, 0]), cut=160) == bytes([0, 5, 7, 0])


def test_the_strip_is_found_wherever_it_starts():
    assert locate(calibration(900, 512), 900) == [512]
    assert locate(calibration(900, 0), 900) == [0]
    # v2.1.0's strip at 32 pixels under Steam's FPS counter (52 pixels of dark glass): not there
    under = bytearray(calibration(900, 32))
    for y in range(CELL):
        o = y * 900 * 3
        under[o:o + 52 * 3] = bytes(v // 10 for v in under[o:o + 52 * 3])
    assert locate(bytes(under), 900) == []
    assert locate(calibration(900, 512, lit=150, dark=40), 900) == [512]   # dim: another cut finds it
    assert locate(bytes(900 * 3 * CELL), 900) == []


def text(s: str) -> bytes:
    b = s.encode()
    return bytes([len(b)]) + b


def payload(flags=0, members=0, classes=0, ready=b"\x00" * 8, alerts=b"\x00\x00\x00", mplus=b"\x00") -> bytes:
    """A payload as the companion builds it, nothing set but what is given."""
    out = bytes([flags, members]) + classes.to_bytes(2, "big")
    out += b"\x00" * 4 + bytes([80, 0, 0]) + b"\x00" * 6 + b"\x00" * 4 + bytes([255, 0]) + b"\x00" * 8
    out += bytes([0, 0, 0, 0, 0]) + b"\x00" * 4 + bytes([0])      # keystone, zone, slots, version
    out += bytes([0, 0])                                             # no buffs, no bars
    return out + ready + alerts + mplus


def test_the_group_and_its_classes_are_read():
    snap = parse(payload(4, 3, 1 << 0 | 1 << 7 | 1 << 12), 0.0)       # a raid: warrior, mage, evoker
    assert snap["group"] == {"n": 3, "raid": True, "classes": ["WARRIOR", "MAGE", "EVOKER"]}
    assert snap["rc"]["state"] == 0 and snap["alerts"] == {"phase": 0, "enc": None, "list": []}
    assert snap["mplus"] is None


def test_a_ready_check_and_nsrts_alerts_are_read():
    ready = bytes([1]) + (1234).to_bytes(4, "big") + bytes([5, 3, 2]) + bytes([2]) + text("Bob") + bytes([0]) + text("Kim")
    alerts = bytes([2]) + text("Dimensius")
    alerts += bytes([2]) + text("Soak {skull}") + (1300).to_bytes(4, "big") + (370597).to_bytes(3, "big")
    alerts += text("Spread") + (1450).to_bytes(4, "big") + (0).to_bytes(3, "big")
    snap = parse(payload(ready=ready, alerts=alerts), 120.0)            # WoW's clock at 120 s
    assert snap["rc"] == {"state": 1, "rem": 3.4, "total": 5, "ready": 3,
                          "missing": [{"name": "Bob", "a": 2}, {"name": "Kim", "a": 0}]}
    assert snap["alerts"] == {"phase": 2, "enc": "Dimensius", "list": [
        {"text": "Soak {skull}", "rem": 10.0, "spell": 370597}, {"text": "Spread", "rem": 25.0, "spell": None}]}


# A +12 Ara-Kara: its timer started at 100 s, 2 deaths (30 s), 72.4 % forces, the first boss down at 5:12
RUN = (bytes([1]) + (503).to_bytes(2, "big") + text("Ara-Kara") + bytes([12]) + (1800).to_bytes(2, "big")
       + (1000).to_bytes(4, "big") + bytes([0, 2]) + (30).to_bytes(2, "big") + (724).to_bytes(2, "big")
       + bytes([2]) + text("Avanoxx") + bytes([1]) + (312).to_bytes(2, "big") + text("Anub'zekt") + bytes([0, 0, 0]))


def test_a_mythic_plus_run_is_read():
    m = parse(payload(mplus=RUN), 1520.5)["mplus"]                     # WoW's clock: 23:40 into the key
    assert m == {"state": 1, "map": 503, "name": "Ara-Kara", "level": 12, "limit": 1800, "elapsed": 1420.5,
                 "running": True, "onTime": False, "upgrades": 0, "deaths": 2, "lost": 30, "forces": 72.4,
                 "bosses": [{"name": "Avanoxx", "done": True, "at": 312}, {"name": "Anub'zekt", "done": False, "at": None}]}
    over = bytes([2]) + (503).to_bytes(2, "big") + text("Ara-Kara") + bytes([12]) + (1800).to_bytes(2, "big")
    over += (15000).to_bytes(4, "big") + bytes([1 | 2 << 1, 0]) + bytes(4) + bytes([0])   # 25:00, timed, +2
    m = parse(payload(mplus=over), 9999.0)["mplus"]
    assert (m["state"], m["elapsed"], m["onTime"], m["upgrades"], m["running"]) == (2, 1500.0, True, 2, False)


def test_a_key_leaves_the_frames_flags_and_the_character_be():
    # Up to v0.16.0 the run's result, its last boss and its level overwrote the frame's
    # flags (combat read as "timed"), the character's name and level.
    snap = parse(payload(1 | 2 | 8 | 16 | 32, mplus=RUN), 1520.5)
    assert (snap["combat"], snap["lockdown"], snap["aura_secret"], snap["pull_hidden"]) == (True, True, True, True)
    assert snap["char"]["name"] is None and snap["char"]["level"] == 80 and snap["mplus"]["level"] == 12
    assert snap["mplus"]["onTime"] is False


def test_whether_the_game_hides_auras_is_read_only_when_it_said():
    assert parse(payload(1), 0.0)["aura_secret"] is None              # an addon before 2.6.0: the PC guesses
    assert parse(payload(16), 0.0)["aura_secret"] is False
    assert parse(payload(8 | 16), 0.0)["aura_secret"] is True
