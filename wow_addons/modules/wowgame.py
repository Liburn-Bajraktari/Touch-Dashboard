"""
World of Warcraft's own commands, for keys: combat, targeting, chat, raid
buffs, the interface, emotes. The group's are Raid & dungeons' (raid.py),
the markers World markers' (markers.py).

All are Blizzard's (via "game"): typed into chat, or pressed as a key slot,
which is the only way to run the protected ones (/cast, /wm, /target…) in
combat and the chat ones in chat lockdown (M+, encounters). The companion
addon never runs them: an addon may not.

`verified=False`: not checked in game; Settings flags them and the player can
change their text.
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, KeyTemplate

COMBAT, TARGET, CHAT = "Combat", "Targeting", "Chat"
BUFFS, CHARACTER, INTERFACE, EMOTES = "Raid buffs", "Character", "Interface", "Emotes"

TEXT = Arg("text", max_len=200, label="Text")
NAME = Arg("text", max_len=60, label="Name")


def game(cid, label, slash, glyph, category, **kw):
    return Command(cid, label, slash if isinstance(slash, tuple) else (slash,), glyph=glyph,
                   category=category, via="game", **kw)


COMMANDS = (
    game("wow_startattack", "Start attack", "/startattack", "sword", COMBAT),
    game("wow_stopattack", "Stop attack", "/stopattack", "shield", COMBAT),
    game("wow_stopcasting", "Stop casting", "/stopcasting", "octagon", COMBAT),
    game("wow_cancelform", "Cancel form", "/cancelform", "user-x", COMBAT),
    game("wow_dismount", "Dismount", "/dismount", "arrow-down-to-line", COMBAT),
    game("wow_leavevehicle", "Leave vehicle", "/leavevehicle", "log-out", COMBAT),
    game("wow_petattack", "Pet attack", "/petattack", "paw-print", COMBAT),
    game("wow_petfollow", "Pet follow", "/petfollow", "footprints", COMBAT),
    game("wow_petpassive", "Pet passive", "/petpassive", "pause", COMBAT),
    game("wow_cast", "Cast a spell", "/cast {spell}", "wand-2", COMBAT, args={"spell": NAME},
         description="By its name in your game's language"),
    game("wow_use", "Use an item", "/use {item}", "flask-conical", COMBAT, args={"item": NAME}),
    game("wow_cancelaura", "Cancel a buff", "/cancelaura {aura}", "minus-circle", COMBAT, args={"aura": NAME}),
    game("wow_focus", "Focus target", "/focus", "eye", TARGET),
    game("wow_clearfocus", "Clear focus", "/clearfocus", "eye-off", TARGET),
    game("wow_targetfocus", "Target focus", "/target focus", "scan", TARGET),
    game("wow_assist", "Assist", "/assist", "user-check", TARGET),
    game("wow_targetenemy", "Next enemy", "/targetenemy", "target", TARGET),
    game("wow_cleartarget", "Clear target", "/cleartarget", "x", TARGET),
    game("wow_party", "Party message", "/p {text}", "message-circle", CHAT, args={"text": TEXT}),
    game("wow_raid", "Raid message", "/raid {text}", "message-circle", CHAT, args={"text": TEXT}),
    game("wow_rw", "Raid warning", "/rw {text}", "megaphone", CHAT, args={"text": TEXT}),
    game("wow_instance", "Instance message", "/i {text}", "message-circle", CHAT, args={"text": TEXT}),
    game("wow_guild", "Guild message", "/g {text}", "message-circle", CHAT, args={"text": TEXT}),
    game("wow_say", "Say", "/s {text}", "message-square", CHAT, args={"text": TEXT}),
    game("cast_intellect", "Arcane Intellect", "/cast Arcane Intellect", "sparkles", BUFFS),
    game("cast_wild", "Mark of the Wild", "/cast Mark of the Wild", "leaf", BUFFS),
    game("cast_shout", "Battle Shout", "/cast Battle Shout", "megaphone", BUFFS),
    game("cast_fortitude", "Power Word: Fortitude", "/cast Power Word: Fortitude", "shield-plus", BUFFS),
    game("cast_bronze", "Blessing of the Bronze", "/cast Blessing of the Bronze", "hourglass", BUFFS),
    game("cast_skyfury", "Skyfury", "/cast Skyfury", "cloud-lightning", BUFFS),
    game("wow_equipset", "Equipment set", "/equipset {name}", "shirt", CHARACTER, args={"name": NAME}),
    game("wow_mount", "Random favourite mount", "/run C_MountJournal.SummonByID(0)", "rabbit", CHARACTER,
         verified=False),
    game("wow_pet", "Random favourite pet", "/randomfavoritepet", "dog", CHARACTER),
    game("wow_afk", "Away", "/afk", "coffee", CHARACTER),
    game("wow_dnd", "Do not disturb", "/dnd", "bell-off", CHARACTER),
    game("wow_played", "Time played", "/played", "clock", CHARACTER),
    game("wow_logout", "Log out", "/logout", "log-out", CHARACTER),
    game("wow_reload", "Reload UI", "/reload", "refresh-cw", INTERFACE, key="tools",
         description="WoW saves SavedVariables now: the page reads them a moment later"),
    game("wow_stopwatch", "Stopwatch", "/stopwatch", "timer", INTERFACE),
    game("wow_calendar", "Calendar", "/calendar", "calendar", INTERFACE),
    game("wow_chatlog", "Chat log", "/chatlog", "message-square", INTERFACE),
    game("wow_fstack", "Frame stack", "/fstack", "layers", INTERFACE),
    game("wow_sound", "Sound on / off", "/run Sound_ToggleSound()", "volume-2", INTERFACE, verified=False),
    game("wow_music", "Music on / off", "/run Sound_ToggleMusic()", "music", INTERFACE, verified=False),
    game("wow_dance", "Dance", "/dance", "music-2", EMOTES),
    game("wow_wave", "Wave", "/wave", "hand", EMOTES),
    game("wow_cheer", "Cheer", "/cheer", "party-popper", EMOTES),
    game("wow_thank", "Thank", "/thank", "heart-handshake", EMOTES),
    game("wow_bow", "Bow", "/bow", "smile", EMOTES),
    game("wow_applaud", "Applaud", "/applaud", "hand-metal", EMOTES),
    game("wow_laugh", "Laugh", "/lol", "laugh", EMOTES),
    game("wow_roar", "Roar", "/roar", "flame", EMOTES),
)

def keys(*ids: str) -> tuple[dict, ...]:
    return tuple({"cmd": i} for i in ids)


def callout(text: str, label: str, glyph: str) -> dict:
    return {"cmd": "wow_rw", "args": {"text": text}, "label": label, "glyph": glyph}


MODULE = AddonModule(
    id="wowgame",
    name="World of Warcraft",
    description="The game's own commands: combat, targeting, chat, raid buffs, the interface, emotes",
    order=1,
    commands=COMMANDS,
    templates=(
        KeyTemplate("callouts", "Raid callouts", "Raid warnings to edit: stack, spread, defensives, Bloodlust",
                    glyph="megaphone", keys=(callout("Stack!", "Stack", "users"), callout("Spread!", "Spread", "move"),
                                             callout("Defensives now", "Defensives", "shield"),
                                             callout("Bloodlust now", "Bloodlust", "flame"),
                                             callout("Wipe, run back", "Wipe", "skull"),
                                             callout("Pull in 10", "Pull soon", "timer"))),
        KeyTemplate("buffs", "Raid buffs", "Each class's raid buff: cast yours",
                    glyph="sparkles", keys=keys("cast_intellect", "cast_fortitude", "cast_shout", "cast_wild",
                                                "cast_bronze", "cast_skyfury")),
        KeyTemplate("targeting", "Targeting", "Focus, assist, the next enemy, clear target",
                    glyph="target", keys=keys("wow_focus", "wow_clearfocus", "wow_targetfocus", "wow_assist",
                                              "wow_targetenemy", "wow_cleartarget")),
        KeyTemplate("pet", "Pet", "Attack, follow, passive", glyph="paw-print",
                    keys=keys("wow_petattack", "wow_petfollow", "wow_petpassive")),
        KeyTemplate("character", "Character", "Mount, pet, away, do not disturb, time played, log out",
                    glyph="user", keys=keys("wow_mount", "wow_pet", "wow_afk", "wow_dnd", "wow_played", "wow_logout")),
        KeyTemplate("interface", "Interface", "Reload, stopwatch, calendar, chat log, sound and music",
                    glyph="monitor", keys=keys("wow_reload", "wow_stopwatch", "wow_calendar", "wow_chatlog",
                                               "wow_sound", "wow_music")),
        KeyTemplate("emotes", "Emotes", "Dance, wave, cheer, thank, bow, applaud, laugh, roar",
                    glyph="smile", keys=keys("wow_dance", "wow_wave", "wow_cheer", "wow_thank", "wow_bow",
                                             "wow_applaud", "wow_laugh", "wow_roar")),
    ),
)
