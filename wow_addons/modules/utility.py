"""
Utilities: Quick sim, and the Tools keys on the first layout.

Quick sim opens Raidbots' Quick Sim on the PC; paste the string the
SimulationCraft addon gives you (/simc) there. Raidbots takes no input from a
link, so the string stays on the clipboard for you to paste.
"""
from __future__ import annotations

from wow_addons import AddonModule, Command, Context, KeyGroup, KeyTemplate

RAIDBOTS_QUICK_SIM = "https://www.raidbots.com/simbot/quick"


def _quick_sim(ctx: Context, args: dict) -> None:
    ctx.open_url(RAIDBOTS_QUICK_SIM)


MODULE = AddonModule(
    id="utility",
    name="Utilities",
    description="Quick sim on Raidbots",
    order=50,
    commands=(
        Command("sim_quick", "Quick sim", run=_quick_sim, glyph="cpu", key="tools",
                description="Opens Raidbots' Quick Sim on the PC; paste your /simc string there"),
    ),
    key_groups=(KeyGroup("tools", "Tools", column=2, order=10),),
    templates=(KeyTemplate("tools", "Tools", "Reload, read the game, your SimC string, Quick sim, the frame stack",
                           glyph="wrench", keys=({"cmd": "wow_reload"}, {"cmd": "game_sync"}, {"cmd": "simc_string"},
                                                 {"cmd": "sim_quick"}, {"cmd": "wow_fstack"})),),
)
