"""
World markers and target marks: their own plate (the eight world markers in
their colours, and Clear), and their commands for keys.

/wm and /tm number the same eight symbols in different orders
(warcraft.wiki.gg: MACRO worldmarker, SetRaidTarget):

    /wm  1 square  2 triangle  3 diamond  4 cross  5 star  6 circle  7 moon  8 skull
    /tm  1 star  2 circle  3 diamond  4 triangle  5 moon  6 square  7 cross  8 skull

A world marker lands where the mouse points in game, as a key bound to it
would place it. In a raid only the leader and assistants may place them.
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, KeyTemplate, Widget

MARKERS = "Markers"
# Symbol → (its /wm number, its /tm number), in the order players know them (the target marks').
SYMBOLS = {"star": (5, 1), "circle": (6, 2), "diamond": (3, 3), "triangle": (2, 4),
           "moon": (7, 5), "square": (1, 6), "cross": (4, 7), "skull": (8, 8)}
GLYPHS = {"cross": "x"}

WORLD = Arg("int", lo=1, hi=8, default=5, label="Marker (1 square, 2 triangle, 3 diamond, 4 cross, 5 star, "
                                                "6 circle, 7 moon, 8 skull)")
TARGET = Arg("int", lo=1, hi=8, default=1, label="Mark (1 star, 2 circle, 3 diamond, 4 triangle, 5 moon, "
                                                 "6 square, 7 cross, 8 skull)")


def game(cid, label, slash, glyph, **kw):
    return Command(cid, label, (slash,), glyph=glyph, category=MARKERS, via="game", **kw)


def _keys(cmd: str, which: int, clear: str) -> tuple[dict, ...]:
    return tuple({"cmd": cmd, "args": {"marker": nums[which]}, "label": name.capitalize(),
                  "glyph": GLYPHS.get(name, name)} for name, nums in SYMBOLS.items()) + ({"cmd": clear},)


MODULE = AddonModule(
    id="markers",
    name="World markers",
    description="The eight world markers, placed where the mouse points, and target marks",
    order=3,
    commands=(
        game("wow_wm", "World marker", "/wm [@cursor] {marker}", "map-pin", args={"marker": WORLD},
             description="Placed where the mouse points"),
        game("wow_cwm_one", "Clear a world marker", "/cwm {marker}", "map-pin-off", args={"marker": WORLD}),
        game("wow_cwm", "Clear world markers", "/cwm all", "map-pin-off"),
        game("wow_tm", "Mark target", "/tm {marker}", "crosshair", args={"marker": TARGET}),
        game("wow_tm_clear", "Clear target's mark", "/tm 0", "circle-off"),
    ),
    widgets=(Widget("world", "World markers", "The eight world markers in their colours, placed where the "
                    "mouse points, and Clear", sizes=("full", "half"), column=1, order=30),),
    templates=(
        KeyTemplate("world", "World markers", "The eight world markers, placed where the mouse points, and Clear",
                    glyph="map-pin", keys=_keys("wow_wm", 0, "wow_cwm")),
        KeyTemplate("target", "Target marks", "Mark your target with any of the eight, or clear its mark",
                    glyph="crosshair", keys=_keys("wow_tm", 1, "wow_tm_clear")),
    ),
)
