"""
BigWigs: its commands for keys (pull and break, raid and local bars, the
version check). The Pull & break plate is Raid & dungeons' now (raid.py),
which uses BigWigs when it is installed; the bars it shows come from the
companion addon.
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, KeyTemplate

SECONDS = Arg("int", lo=1, hi=3600, default=30, label="Seconds")
LABEL = Arg("text", max_len=32, default="Timer", label="Label")

MODULE = AddonModule(
    id="bigwigs",
    name="BigWigs",
    description="Pull and break timers, raid and local bars, the version check",
    order=20,
    commands=(
        Command("bw_pull", "Pull timer", ("/pull {seconds}",),
                args={"seconds": Arg("int", lo=0, hi=60, default=10, label="Seconds (0 cancels)")},
                glyph="timer", via="addon"),
        Command("bw_break", "Break timer", ("/break {minutes}",),
                args={"minutes": Arg("int", lo=0, hi=60, default=5, label="Minutes (0 cancels)")},
                glyph="coffee", via="addon"),
        Command("bw_raidbar", "Raid bar", ('/raidbar {seconds} "{label}"',),
                args={"seconds": SECONDS, "label": LABEL},
                glyph="bar-chart-horizontal", via="addon", description="A bar every BigWigs user in the raid sees"),
        Command("bw_localbar", "Local bar", ('/localbar {seconds} "{label}"',),
                args={"seconds": SECONDS, "label": LABEL},
                glyph="bar-chart-horizontal", via="addon", description="A bar only you see"),
        Command("bw_version", "Version check", ("/bwv",), glyph="shield-check", key="tools", via="addon"),
        Command("bw_config", "BigWigs options", ("/bw",), glyph="settings-2", via="addon"),
    ),
    addon="BigWigs",
    templates=(
        KeyTemplate("bars", "BigWigs bars", "Raid bars to edit (the raid sees them), the version check, options",
                    glyph="bar-chart-horizontal",
                    keys=({"cmd": "bw_raidbar", "args": {"seconds": 30, "label": "Bloodlust"}, "label": "Lust bar"},
                          {"cmd": "bw_raidbar", "args": {"seconds": 60, "label": "Next pull"}, "label": "Next pull"},
                          {"cmd": "bw_localbar", "args": {"seconds": 120, "label": "Potion"}, "label": "Potion bar"},
                          {"cmd": "bw_version"}, {"cmd": "bw_config"})),
    ),
)
