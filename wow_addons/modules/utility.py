"""
Raid utilities: Quick sim and clearing world markers.

Quick sim opens Raidbots' Quick Sim on the PC; paste the string the
SimulationCraft addon gives you (/simc) there. Raidbots takes no input from a
link, so the string stays on the clipboard for you to paste.
"""
from __future__ import annotations

from wow_addons import AddonModule, Command, Context

RAIDBOTS_QUICK_SIM = "https://www.raidbots.com/simbot/quick"


def _quick_sim(ctx: Context, args: dict) -> None:
    ctx.open_url(RAIDBOTS_QUICK_SIM)


MODULE = AddonModule(
    id="utility",
    name="Raid utilities",
    description="Quick sim on Raidbots and clearing world markers",
    order=50,
    commands=(
        Command("sim_quick", "Quick sim", run=_quick_sim),
        Command("cwm_clear", "Clear world markers", ("/cwm clear",), verified=False),
    ),
)
