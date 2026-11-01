"""
Popular addons' slash commands, for keys: meters, boss mods, nameplates, unit
frames, dungeon tools, the UI suites. Each names its addon's folder, so the key
editor can say which are installed. With the companion addon they run without
opening chat.

`verified=False`: the command as we understand it, not checked in game;
Settings flags it and the player can change its text. An addon this list does
not know is a custom command (WoW settings → Custom commands, or "Custom" in
the key editor).
"""
from __future__ import annotations

from wow_addons import AddonModule, Arg, Command, KeyTemplate

METERS, BOSS, UI, DUNGEON, TOOLS = "Meters", "Boss mods", "Interface addons", "Dungeons", "Addon tools"


def addon(cid, label, slash, glyph, category, folder, **kw):
    return Command(cid, label, (slash,), glyph=glyph, category=category, via="addon", addon=folder, **kw)


COMMANDS = (
    addon("det_toggle", "Details: show / hide", "/details toggle", "bar-chart-3", METERS, "Details"),
    addon("det_reset", "Details: reset", "/details reset", "rotate-ccw", METERS, "Details"),
    addon("det_options", "Details: options", "/details config", "settings-2", METERS, "Details", verified=False),
    addon("dbm_options", "DBM: options", "/dbm", "skull", BOSS, "DBM-Core"),
    addon("dbm_pull", "DBM: pull timer", "/dbm pull {seconds}", "timer", BOSS, "DBM-Core",
          args={"seconds": Arg("int", lo=1, hi=60, default=10, label="Seconds")}),
    addon("dbm_break", "DBM: break timer", "/dbm break {minutes}", "coffee", BOSS, "DBM-Core",
          args={"minutes": Arg("int", lo=1, hi=60, default=5, label="Minutes")}),
    addon("dbm_version", "DBM: version check", "/dbm ver", "shield-check", BOSS, "DBM-Core"),
    addon("wa_options", "WeakAuras", "/wa", "sparkles", UI, "WeakAuras"),
    addon("plater_options", "Plater", "/plater", "badge", UI, "Plater"),
    addon("elv_options", "ElvUI: options", "/ec", "settings", UI, "ElvUI"),
    addon("elv_move", "ElvUI: move frames", "/moveui", "move", UI, "ElvUI"),
    addon("cell_options", "Cell", "/cell", "grid-3x3", UI, "Cell"),
    addon("bt_options", "Bartender4", "/bt", "layout-dashboard", UI, "Bartender4"),
    addon("dominos_options", "Dominos", "/dominos", "layout-dashboard", UI, "Dominos"),
    addon("omnicd_options", "OmniCD", "/oc", "hourglass", UI, "OmniCD", verified=False),
    addon("hekili_options", "Hekili", "/hekili", "zap", UI, "Hekili"),
    addon("mdt_open", "Mythic Dungeon Tools", "/mdt", "map", DUNGEON, "MythicDungeonTools"),
    addon("rio_options", "Raider.IO", "/raiderio", "trophy", DUNGEON, "RaiderIO", verified=False),
    addon("mrt_open", "Method Raid Tools", "/rt", "clipboard-list", TOOLS, "MRT", verified=False),
    addon("simc_string", "SimulationCraft string", "/simc", "cpu", TOOLS, "SimulationCraft",
          description="Shows your SimC string: press Ctrl+C in game, then paste it on Raidbots"),
    addon("att_open", "All The Things", "/att", "book-open", TOOLS, "AllTheThings"),
    addon("tsm_open", "TradeSkillMaster", "/tsm", "coins", TOOLS, "TradeSkillMaster"),
    addon("baganator_open", "Baganator", "/baganator", "backpack", TOOLS, "Baganator", verified=False),
    addon("leatrix_options", "Leatrix Plus", "/ltp", "wrench", TOOLS, "Leatrix_Plus", verified=False),
)

MODULE = AddonModule(
    id="addons",
    name="Popular addons",
    description="Details!, DBM, WeakAuras, Plater, ElvUI, MDT, SimulationCraft and more",
    order=4,
    commands=COMMANDS,
    # Keys of addons that are not in the game folder are left out when a plate starts from it.
    templates=(KeyTemplate("panels", "Addon windows", "Open your addons: meters, boss mods, dungeon tools, "
                           "interface (those you have)", glyph="app-window",
                           keys=tuple({"cmd": c.id} for c in COMMANDS if c.id in (
                               "det_toggle", "det_reset", "dbm_options", "wa_options", "plater_options",
                               "elv_options", "elv_move", "cell_options", "mdt_open", "rio_options", "mrt_open",
                               "simc_string", "hekili_options", "omnicd_options", "baganator_open", "tsm_open"))),),
)
