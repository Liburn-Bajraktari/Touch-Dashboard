"""
BigWigs: pull and break timers, raid and local bars, the version check.

The last timer sent is shared state, so every screen counts it down: the page
gets its remaining seconds at the moment the state is sent and runs the
countdown on its own clock (a tablet's clock need not match the PC's).
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, Context, clean_int

PULL_RANGE = (5, 25)      # the page's fader; a command accepts 1-60
BREAK_RANGE = (2, 15)     # minutes


def _timer(kind: str, label: str):
    """Send the command, then start the shared countdown."""
    def run(ctx: Context, args: dict) -> None:
        ctx.send()
        seconds = args["minutes"] * 60 if "minutes" in args else args["seconds"]
        ctx.update(timer={"kind": kind, "label": args.get("label") or label,
                          "duration": seconds, "started": ctx.now()})
    return run


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


SECONDS = Arg("int", lo=1, hi=3600, default=30)
LABEL = Arg("text", max_len=32, default="Timer")

MODULE = AddonModule(
    id="bigwigs",
    name="BigWigs",
    description="Pull and break timers, bars and the version check",
    order=20,
    commands=(
        Command("bw_pull", "Pull timer", ("/pull {seconds}",),
                args={"seconds": Arg("int", lo=1, hi=60, setting="pull_s")}, run=_timer("pull", "Pull")),
        Command("bw_break", "Break timer", ("/break {minutes}",),
                args={"minutes": Arg("int", lo=1, hi=60, setting="break_m")}, run=_timer("break", "Break")),
        Command("bw_raidbar", "Raid bar", ('/raidbar {seconds} "{label}"',),
                args={"seconds": SECONDS, "label": LABEL}, run=_timer("raidbar", "Raid bar")),
        Command("bw_localbar", "Local bar", ('/localbar {seconds} "{label}"',),
                args={"seconds": SECONDS, "label": LABEL}, run=_timer("localbar", "Local bar")),
        Command("bw_version", "Version check", ("/bwv",)),
    ),
    defaults={"pull_s": 10, "break_m": 5},
    clean=clean,
    state={"timer": None},
    public=public,
)
