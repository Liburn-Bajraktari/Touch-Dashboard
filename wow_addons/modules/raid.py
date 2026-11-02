"""
Raid & dungeons: the group lead's plate. Pull and break timers, the ready
check and how it went, the countdown and the boss mods' bars; and the
game's group commands (role check, raid info, reset instances...).

The pull is Blizzard's countdown, which BigWigs and DBM show as their own
pull bar (BigWigs' /pull is the same call in Midnight), so it needs no boss
mod: with BigWigs it goes as its /pull through the companion's box (no chat
opened), else /countdown is typed. 0 seconds cancels it. A break needs a
boss mod or NSRT: BigWigs' /break, DBM's /dbm break or NSRT's /ns break,
the first one installed.

The last timer sent is shared state, so every screen counts it down: the page
gets its remaining seconds at the moment the state is sent and runs the
countdown on its own clock (a tablet's clock need not match the PC's). While
the companion's live link shows, the game's own pull or break bar decides,
whoever started it. The ready check's answers come from the live link too,
and so does the Mythic+ plate's run (the game module's view: `mplus`) and the
Next alert plate's boss mod bars and NSRT alerts.
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, CommandError, Context, KeyTemplate, Widget, clean_int

PULL_RANGE = (5, 25)      # the page's fader; a command accepts 0-60 (0 cancels)
BREAK_RANGE = (2, 15)     # minutes
GROUP = "Group"
# Who can start a break, in the order they are tried.
BREAKS = (("BigWigs", "/break {minutes}"), ("DBM-Core", "/dbm break {minutes}"),
          ("NorthernSkyRaidTools", "/ns break {minutes}"))


def _start(ctx: Context, kind: str, label: str, seconds: int) -> None:
    ctx.update(timer={"kind": kind, "label": label, "duration": seconds, "started": ctx.now()} if seconds else None)


def _pull(ctx: Context, args: dict) -> None:
    if ctx.has_addon("BigWigs"):
        ctx.send_as(("/pull {seconds}",), "addon")
    else:
        ctx.send()
    _start(ctx, "pull", "Pull", args["seconds"])


def _break(ctx: Context, args: dict) -> None:
    found = [line for folder, line in BREAKS if ctx.has_addon(folder)]
    if found:
        ctx.send_as((found[0],), "addon")
    elif ctx.has_addon("BigWigs") is None:      # no game folder to look in: BigWigs' is the likeliest
        ctx.send()
    else:
        raise CommandError("no_bossmod")
    _start(ctx, "break", "Break", args["minutes"] * 60)


def public(state: dict, now: float) -> dict:
    timer = state.get("timer")
    if not timer:
        return {"timer": None}
    remaining = timer["duration"] - (now - timer["started"])
    if remaining <= 0:
        return {"timer": None}
    return {"timer": {"kind": timer["kind"], "label": timer["label"], "duration": timer["duration"],
                      "remaining": round(remaining, 2)}}


def clean(raw: dict) -> dict:
    return {
        "pull_s": clean_int(raw.get("pull_s"), *PULL_RANGE, 10),
        "break_m": clean_int(raw.get("break_m"), *BREAK_RANGE, 5),
    }


def game(cid, label, slash, glyph, **kw):
    return Command(cid, label, (slash,), glyph=glyph, category=GROUP, via="game", **kw)


MODULE = AddonModule(
    id="raid",
    name="Raid & dungeons",
    description="Pull and break timers, the ready check and its answers, the boss mods' bars, group tools",
    order=2,
    commands=(
        Command("raid_pull", "Pull timer", ("/countdown {seconds}",), category=GROUP,
                args={"seconds": Arg("int", lo=0, hi=60, setting="pull_s", label="Seconds (0 cancels)")},
                run=_pull, glyph="timer", via="game",
                description="Blizzard's countdown, which BigWigs and DBM show as their pull bar"),
        Command("raid_break", "Break timer", ("/break {minutes}",), category=GROUP,
                args={"minutes": Arg("int", lo=0, hi=60, setting="break_m", label="Minutes (0 cancels)")},
                run=_break, glyph="coffee", via="addon", description="Needs BigWigs, DBM or NSRT in game"),
        game("wow_readycheck", "Ready check", "/readycheck", "check-circle-2"),
        game("wow_countdown", "Countdown", "/countdown {seconds}", "timer-reset",
             args={"seconds": Arg("int", lo=1, hi=60, default=10, label="Seconds")},
             description="Blizzard's own pull countdown for the group"),
        game("wow_rolecheck", "Role check", "/rolecheck", "users", verified=False),
        game("wow_raidinfo", "Raid info", "/raidinfo", "info"),
        game("wow_lfg", "Group finder", "/lfg", "search"),
        game("wow_resetinstances", "Reset instances", "/run ResetInstances()", "rotate-ccw"),
    ),
    defaults={"pull_s": 10, "break_m": 5},
    clean=clean,
    state={"timer": None},
    public=public,
    widgets=(Widget("timers", "Raid & dungeons", "Pull, break and ready check keys, the countdown, the boss "
                    "mods' bars and who is not ready", column=1, order=20),
             Widget("mplus", "Mythic+", "The key's timer against its chests, deaths and the time they cost, enemy "
                    "forces and each boss, from the companion's live link", sizes=("full", "half")),
             # NSRT's until v0.16.2 (WIDGET_ALIASES keeps its place in saved layouts)
             Widget("next", "Next alert", "Whatever comes first: your boss mod's next ability (BigWigs, DBM, the "
                    "game's own timeline) or NSRT's next alert, with its countdown and the one after it",
                    sizes=("half", "full"))),
    templates=(
        KeyTemplate("lead", "Raid lead", "Ready check, pull, break, role check, clear the markers",
                    glyph="crown", keys=({"cmd": "wow_readycheck"}, {"cmd": "raid_pull", "args": {"seconds": 10}},
                                         {"cmd": "raid_break", "args": {"minutes": 5}}, {"cmd": "wow_rolecheck"},
                                         {"cmd": "wow_cwm"}, {"cmd": "wow_raidinfo"})),
        KeyTemplate("dungeon", "Dungeon", "Ready check, a short pull, reset instances, the group finder",
                    glyph="castle", keys=({"cmd": "wow_readycheck"},
                                          {"cmd": "raid_pull", "args": {"seconds": 5}, "label": "Pull 5"},
                                          {"cmd": "wow_resetinstances"}, {"cmd": "wow_lfg"},
                                          {"cmd": "wow_rolecheck"}, {"cmd": "mdt_open"})),
    ),
)
