"""Theme registry checks: completeness, contrast, and no hard-coded colours.

static/themes.js is the single source of design tokens (see docs/DESIGN.md).
These tests keep every preset legible and keep the template on tokens only.
"""
import itertools
import json
import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
THEMES_JS = ROOT / "static" / "themes.js"
TEMPLATE = ROOT / "templates" / "index.html"

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# (foreground, background, minimum ratio). 4.5 is WCAG AA for text; 3.0 is
# WCAG 1.4.11 for graphics such as fader fills, battery levels and icons.
CONTRAST_PAIRS = [
    ("text", "ground", 7.0),
    ("text", "surface", 7.0),
    ("text", "surface-2", 7.0),
    ("text-2", "surface", 4.5),
    ("text-2", "surface-2", 4.5),
    ("text-3", "surface", 4.5),
    ("text-3", "surface-2", 4.5),
    ("accent-text", "surface", 4.5),
    ("accent-text", "surface-2", 4.5),
    ("accent", "surface", 3.0),
    ("accent", "surface-2", 3.0),
    ("accent-ink", "accent", 4.5),
    ("inverse-ink", "inverse", 4.5),
    ("positive", "surface", 3.0),
    ("positive", "surface-2", 3.0),
    ("positive-ink", "positive", 4.5),
    ("warning", "surface", 3.0),
    ("warning", "surface-2", 3.0),
    ("negative-ink", "negative", 4.5),
    ("negative-text", "surface", 4.5),
    ("negative-text", "surface-2", 4.5),
    ("negative-text", "negative-soft", 4.5),
    ("focus", "ground", 3.0),
    ("focus", "surface", 3.0),
    # Lit surfaces: panel and key tops carry the same text as their base.
    ("text", "surface-top", 7.0),
    ("text", "key-top", 7.0),
    ("text-2", "key-top", 4.5),
    # Wells: recessed containers (key screens, voice list, load tiles, fader slots).
    ("well-ink", "well", 7.0),
    ("well-ink-2", "well", 4.5),
    ("well-accent", "well", 4.5),
    ("accent", "well", 3.0),
    ("negative", "well", 3.0),
    ("negative-text", "well", 4.5),
    ("positive", "well", 3.0),
    ("warning", "well", 3.0),
    # Displays: small readouts (fader values, device name, speed test, battery).
    ("display-ink", "display", 7.0),
    ("display-ink-2", "display", 4.5),
    ("display-accent", "display", 4.5),
    ("positive", "display", 3.0),
    ("warning", "display", 3.0),
    ("negative", "display", 3.0),
    # The fader cap's index line must read against the cap.
    ("cap-index", "cap", 3.0),
]

# The accent and the status hues sit side by side as small marks (an LED, a
# load trace, a lit key), so each pair must stay apart in OKLab.
STATUS_HUES = ("accent", "positive", "warning", "negative")
MIN_SEPARATION = 0.11

# Value balance (docs/DESIGN.md §3): large neighbouring surfaces stay within
# 1.5:1 of each other, so a recess never reads as a hole in the console.
BALANCE_PAIRS = [
    ("ground", "surface"),
    ("surface", "surface-2"),
    ("surface", "well"),
    ("surface-2", "well"),
    ("surface", "display"),
    ("surface-2", "display"),
]
MAX_BALANCE = 1.5


def _registry() -> dict:
    src = THEMES_JS.read_text(encoding="utf-8")
    match = re.search(r"/\* TD-THEMES:BEGIN \*/(.*?)/\* TD-THEMES:END \*/", src, re.S)
    assert match, "TD-THEMES markers missing from static/themes.js"
    return json.loads(match.group(1))


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _oklab(hex_colour: str) -> tuple:
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
               for c in (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)))
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


REGISTRY = _registry()
THEMES = REGISTRY["themes"]
REQUIRED_COLOUR_ROLES = set(next(t for t in THEMES if t["id"] == REGISTRY["default"])["tokens"])


def test_registry_shape():
    ids = [t["id"] for t in THEMES]
    assert len(ids) == len(set(ids)), "theme ids must be unique"
    assert REGISTRY["default"] in ids
    for theme in THEMES:
        assert re.fullmatch(r"[a-z0-9-]{1,32}", theme["id"]), theme["id"]
        assert theme["scheme"] in ("dark", "light")
        assert theme["name"] and theme["note"]


def test_aliases_point_at_presets():
    """A retired id (renamed or removed preset) must resolve to a live one."""
    ids = {t["id"] for t in THEMES}
    for old, new in REGISTRY.get("aliases", {}).items():
        assert old not in ids, f"alias {old} shadows a preset"
        assert new in ids, f"alias {old} points at missing preset {new}"


@pytest.mark.parametrize("theme", THEMES, ids=lambda t: t["id"])
def test_theme_defines_every_role(theme):
    colour_roles = {k for k in REQUIRED_COLOUR_ROLES if k not in REGISTRY["base"]}
    missing = colour_roles - set(theme["tokens"])
    assert not missing, f"{theme['id']} is missing {sorted(missing)}"
    unknown = set(theme["tokens"]) - REQUIRED_COLOUR_ROLES - set(REGISTRY["base"])
    assert not unknown, f"{theme['id']} defines tokens no theme uses: {sorted(unknown)}"


@pytest.mark.parametrize("theme", THEMES, ids=lambda t: t["id"])
def test_theme_contrast(theme):
    tokens = theme["tokens"]
    failures = []
    for fg, bg, minimum in CONTRAST_PAIRS:
        assert HEX.match(tokens[fg]) and HEX.match(tokens[bg]), f"{fg}/{bg} must be #rrggbb"
        ratio = _contrast(tokens[fg], tokens[bg])
        if ratio < minimum:
            failures.append(f"{fg} on {bg}: {ratio:.2f} < {minimum}")
    assert not failures, f"{theme['id']}: " + "; ".join(failures)


@pytest.mark.parametrize("theme", THEMES, ids=lambda t: t["id"])
def test_status_hues_distinct(theme):
    tokens = theme["tokens"]
    close = [f"{a}/{b}: {math.dist(_oklab(tokens[a]), _oklab(tokens[b])):.3f}"
             for a, b in itertools.combinations(STATUS_HUES, 2)
             if math.dist(_oklab(tokens[a]), _oklab(tokens[b])) < MIN_SEPARATION]
    assert not close, f"{theme['id']}: too close to tell apart: " + "; ".join(close)


@pytest.mark.parametrize("theme", THEMES, ids=lambda t: t["id"])
def test_value_balance(theme):
    tokens = theme["tokens"]
    harsh = [f"{a}/{b}: {_contrast(tokens[a], tokens[b]):.2f}" for a, b in BALANCE_PAIRS
             if _contrast(tokens[a], tokens[b]) > MAX_BALANCE]
    assert not harsh, f"{theme['id']}: neighbouring surfaces too far apart: " + "; ".join(harsh)


def test_template_uses_tokens_only():
    """Every colour in the UI must come from a theme token."""
    html = TEMPLATE.read_text(encoding="utf-8")
    hex_colours = re.findall(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b(?![\w-])", html)
    functional = re.findall(r"\b(?:rgba?|hsla?)\(", html)
    assert not hex_colours, f"hard-coded hex colours in template: {sorted(set(hex_colours))[:10]}"
    assert not functional, "hard-coded rgb()/hsl() colours in template"
