"""
EllesmereUI: unlock mode and profile switching.

Unlock mode is a toggle in game; the key follows the taps sent from here, so
unlocking in game itself leaves it showing the other state. The buff check
that was here is the companion's (game.py): real buffs instead of a checklist.
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, Context, Widget, chat_text

PROFILE_MAX = 12


def _unlock(ctx: Context, args: dict) -> None:
    ctx.send()
    ctx.update(unlocked=not ctx.state.get("unlocked"))


def _profile(ctx: Context, args: dict) -> None:
    ctx.send()
    ctx.update(profile=args["name"])


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
    description="Unlock mode and profiles",
    order=30,
    commands=(
        Command("eui_unlock", "Unlock mode", ("/eui unlock",), run=_unlock, glyph="move", via="addon"),
        Command("eui_profile", "Profile", ("/eui profile {name}",),
                args={"name": Arg("choice", setting="profiles", label="Profile")}, run=_profile,
                glyph="user-cog", via="addon"),
        Command("eui_options", "EllesmereUI options", ("/eui",), glyph="settings-2", via="addon"),
    ),
    defaults={"profiles": []},
    clean=clean,
    state={"unlocked": False, "profile": None},
    addon="EllesmereUI",
    widgets=(Widget("profiles", "EllesmereUI profiles", "A key for each profile saved in its settings",
                    sizes=("half", "full")),),
)
