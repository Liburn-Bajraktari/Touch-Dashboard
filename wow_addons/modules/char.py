"""
Character: the player's character from Raider.IO and Battle.net
(wow_addons.armory): spec, item level, Mythic+ score, progress in the raids
open now, and this week's affixes.

The character is the one in WoW settings (name, realm, region), set by the
lookup in Settings → Battle.net on the PC. The server reads it every 30
minutes while this module is on, and when a screen's Refresh key asks (at
most once a minute); Raider.IO alone is enough, Battle.net adds the avatar,
level and guild. Blizzard updates a character when it logs out.
"""
from __future__ import annotations

from wow_addons import AddonModule, Command, CommandError, Context

SHOWN = ("name", "realm", "region", "class", "spec", "level", "ilvl", "score", "guild",
         "avatar", "profile_url", "role_tag", "updated", "sources")


def _refresh(ctx: Context, args: dict) -> None:
    if ctx.armory is None or not ctx.who():
        raise CommandError("empty")   # no character set yet
    ctx.armory.ask_refresh()


def view(ctx: Context) -> dict:
    who, arm = ctx.who(), ctx.armory
    if not who or arm is None:
        return {"state": "unset", "character": None, "affixes": []}
    char = arm.character if arm.character and arm.character.get("_who") == who else None
    affixes = arm.cached_affixes(who[0])
    out = {
        "state": "ok" if char else ("error" if arm.error else "loading"),
        "reason": arm.error,
        "stale": bool(char and arm.error),       # shown from before a failed refresh
        "character": None,
        "affixes": affixes["list"] if affixes else [],
    }
    if char:
        out["character"] = {**{k: char.get(k) for k in SHOWN}, "raids": char.get("raid_list") or []}
    return out


MODULE = AddonModule(
    id="char",
    name="Character",
    description="Item level, Mythic+ score, raid progress and this week's affixes, from Raider.IO and Battle.net",
    order=5,
    commands=(
        Command("char_refresh", "Refresh the character", run=_refresh),
    ),
    view=view,
)
