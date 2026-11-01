"""
The WoW Token's price in your region and its last week, from Battle.net
(needs the API key in Settings → Battle.net). Blizzard prices it about every
20 minutes; the server reads it that often while this module is on and keeps
eight days of prices with the spell cache.
"""
from __future__ import annotations

from wow_addons import AddonModule, Command, Context, Widget


def view(ctx: Context) -> dict:
    arm = ctx.armory
    if arm is None:
        return {"state": "no_key", "region": ctx.region()}
    return {**arm.token(ctx.region()), "region": ctx.region()}


MODULE = AddonModule(
    id="token",
    name="WoW Token",
    description="The token's price in your region and its last week, from Battle.net",
    order=60,
    view=view,
    commands=(),
    widgets=(Widget("price", "WoW Token", "Its price now, over a day and the last week",
                    sizes=("half", "full"), column=2, order=30),),
)
