"""
Character: the player's character from Raider.IO and Battle.net
(wow_addons.armory): spec, item level, Mythic+ score, progress in the raids
open now, and this week's affixes.

The character is the one in WoW settings (name, realm, region). Any screen
switches between the saved characters (signed in with Battle.net, or added by
name in Settings → Battle.net): char_switch takes only an id from that list.
The server reads the character every 30 minutes while this module is on,
when a screen's Refresh key asks (at most once a minute) and right after a
switch; Raider.IO alone is enough, Battle.net adds the avatar, level and
guild. Blizzard updates a character when it logs out.
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, CommandError, Context
from wow_addons.armory import character_id

ALTS_MAX = 60
SHOWN = ("name", "realm", "region", "class", "spec", "level", "ilvl", "score", "guild",
         "avatar", "profile_url", "role_tag", "updated", "sources")


def _refresh(ctx: Context, args: dict) -> None:
    if ctx.armory is None or not ctx.who():
        raise CommandError("empty")   # no character set yet
    ctx.armory.ask_refresh()


def _switch(ctx: Context, args: dict) -> None:
    char = ctx.armory.find_character(args["id"]) if ctx.armory else None
    if not char:
        raise CommandError("invalid")
    ctx.set_player(char["name"], char["realm"], char["region"])
    ctx.armory.mark_used(char["id"])


def _alts(ctx: Context) -> tuple[list, dict]:
    """The saved characters for the switcher, and the account's sign-in."""
    if ctx.armory is None:
        return [], {"signed_in": False, "battletag": None}
    acct = ctx.armory.account()
    who = ctx.who()
    current = None
    if who:
        current = character_id(*who)
    alts = [{"id": c["id"], "name": c["name"], "realm": c["realm"], "region": c["region"], "class": c["class"],
             "level": c["level"], "current": c["id"] == current} for c in acct["characters"][:ALTS_MAX]]
    return alts, {"signed_in": bool(acct["signed_in"]), "battletag": acct["battletag"]}


def view(ctx: Context) -> dict:
    who, arm = ctx.who(), ctx.armory
    alts, account = _alts(ctx)
    if not who or arm is None:
        return {"state": "unset", "character": None, "affixes": [], "alts": alts, "account": account}
    char = arm.character if arm.character and arm.character.get("_who") == who else None
    affixes = arm.cached_affixes(who[0])
    out = {
        "state": "ok" if char else ("error" if arm.error else "loading"),
        "reason": arm.error,
        "stale": bool(char and arm.error),       # shown from before a failed refresh
        "character": None,
        "affixes": affixes["list"] if affixes else [],
        "alts": alts,
        "account": account,
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
        Command("char_switch", "Switch character", args={"id": Arg("id")}, run=_switch),
    ),
    view=view,
)
