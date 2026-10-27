"""
Combat logging and Warcraft Logs live logging.

/combatlog is a toggle in game, so the key follows the taps sent from here
(typing /combatlog in game itself leaves it showing the other state). Live
logging is done by the Warcraft Logs uploader app, not the game: going live
turns the combat log on first if it is off, then starts the uploader chosen in
Settings (if any). Warcraft Logs has no public API to start a live log, so
the stream key is kept on the PC (never sent to tablets) for the uploader.
"""
from __future__ import annotations

from wow_addons import AddonModule, Command, CommandError, Context, chat_text

APP_MAX = 1024


def _combatlog(ctx: Context, args: dict) -> None:
    ctx.send()
    on = not ctx.state.get("combatlog")
    ctx.update(combatlog=on, **({} if on else {"wcl_live": False}))


def _live(ctx: Context, args: dict) -> None:
    if ctx.state.get("wcl_live"):
        ctx.update(wcl_live=False)   # the combat log keeps running
        return
    if not ctx.settings.get("wcl_enabled"):
        raise CommandError("disabled")
    if not ctx.state.get("combatlog"):
        ctx.send("combatlog")
        ctx.update(combatlog=True)
    app = ctx.settings.get("wcl_app")
    if app:
        ctx.launch(app)
    ctx.update(wcl_live=True)


def clean(raw: dict) -> dict:
    app = raw.get("wcl_app")
    return {
        "wcl_enabled": raw.get("wcl_enabled") is not False,
        "wcl_key": chat_text(raw.get("wcl_key"), 128),
        "wcl_app": app.strip()[:APP_MAX] if isinstance(app, str) and "\0" not in app and "\n" not in app else "",
    }


MODULE = AddonModule(
    id="combatlog",
    name="Combat logging",
    description="The combat log and Warcraft Logs live logging",
    order=40,
    commands=(
        Command("combatlog", "Combat log", ("/combatlog",), run=_combatlog),
        Command("wcl_live_stream", "Live logging", run=_live),
    ),
    defaults={"wcl_enabled": True, "wcl_key": "", "wcl_app": ""},
    clean=clean,
    private=frozenset({"wcl_key", "wcl_app"}),
    state={"combatlog": False, "wcl_live": False},
)
