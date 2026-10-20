"""Theme registry checks: completeness, contrast, and no hard-coded colours.

static/themes.js is the single source of design tokens (see docs/DESIGN.md).
These tests keep every preset legible and keep the template on tokens only.
"""
import json
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
]


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


def test_template_uses_tokens_only():
    """Every colour in the UI must come from a theme token."""
    html = TEMPLATE.read_text(encoding="utf-8")
    hex_colours = re.findall(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b(?![\w-])", html)
    functional = re.findall(r"\b(?:rgba?|hsla?)\(", html)
    assert not hex_colours, f"hard-coded hex colours in template: {sorted(set(hex_colours))[:10]}"
    assert not functional, "hard-coded rgb()/hsl() colours in template"
