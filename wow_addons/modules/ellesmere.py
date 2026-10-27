"""
EllesmereUI: unlock mode, profile switching and the pre-pull buff check.

WoW gives the PC no live view of your buffs, so the buff strip is a checklist
every screen shares: tap a buff once you have it. Arcane Intellect is cast
from its pill. Unlock mode is a toggle in game; the key follows the taps sent
from here, so unlocking in game itself leaves it showing the other state.
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, Context, chat_text

BUFFS = ("flask", "food", "oil", "intellect")
PROFILE_MAX = 12


def _unlock(ctx: Context, args: dict) -> None:
    ctx.send()
    ctx.update(unlocked=not ctx.state.get("unlocked"))


def _profile(ctx: Context, args: dict) -> None:
    ctx.send()
    ctx.update(profile=args["name"])


def _intellect(ctx: Context, args: dict) -> None:
    ctx.send()
    ctx.update(buffs={**ctx.state["buffs"], "intellect": True})


def _buff(ctx: Context, args: dict) -> None:
    ctx.update(buffs={**ctx.state["buffs"], args["buff"]: args["on"]})


def _clear(ctx: Context, args: dict) -> None:
    ctx.update(buffs=dict.fromkeys(BUFFS, False))


def clean(raw: dict) -> dict:
    profiles: list[str] = []
    for name in raw.get("profiles") if isinstance(raw.get("profiles"), list) else []:
        name = chat_text(name, 32)
        if name and name not in profiles:
            profiles.append(name)
    return {"profiles": profiles[:PROFILE_MAX]}


MODULE = AddonModule(
    id="ellesmere",
    name="EllesmereUI",
    description="Unlock mode, profiles and the buff check",
    order=30,
    commands=(
        Command("eui_unlock", "Unlock mode", ("/eui unlock",), run=_unlock),
        Command("eui_profile", "Profile", ("/eui profile {name}",),
                args={"name": Arg("choice", setting="profiles")}, run=_profile),
        Command("cast_intellect", "Arcane Intellect", ("/cast Arcane Intellect",), run=_intellect),
        Command("eui_buff", "Mark a buff", args={"buff": Arg("choice", choices=BUFFS), "on": Arg("bool")},
                run=_buff),
        Command("eui_buffs_clear", "Clear the buff check", run=_clear),
    ),
    defaults={"profiles": []},
    clean=clean,
    state={"unlocked": False, "profile": None, "buffs": dict.fromkeys(BUFFS, False)},
)
