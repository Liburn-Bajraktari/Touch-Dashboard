-- Runs addon/TouchDashboard/TouchDashboard.lua against a stand-in of the WoW
-- API (only what the addon touches), driven line by line from stdin by
-- tests/test_wow_companion.py. Each command answers one line on stdout.
--
--   press [ALT] [CTRL] [SHIFT] <KEY>   a key as the player's keyboard sends it: WoW
--                                      builds the chord ALT-CTRL-SHIFT-<KEY>; a focused
--                                      edit box takes it, else an override binding of
--                                      exactly that chord, else the game's binding of
--                                      the chord or, failing that, of the plain key
--   clip <json string> | key <WOW-CHORD> | event <NAME> [args…] | unload
--   group solo | group party|raid <CLASS>…: who else is in the group (a party of four by default;
--                each named after their class: "Priest", "Mage"…)
--   early 0|1: the first moments of a login, no realm yet | stray: a "Thrall-" left in the saved data
--   mplus start <level> | mplus go | mplus kill <n> | mplus forces <count> | mplus death |
--   mplus done <ms> <on time 0|1> <chests> | mplus reset: a Mythic+ key, from the keystone to its result
--   nsrt phase <n> <encounter id> | nsrt test | nsrt delay <s> | nsrt hide: NSRT starting a phase (a later one
--                stamped after its callback, as NSRT does)
--                (its schedule below), its test run, a boss's cast moving the phase, the encounter's end
--   combat 0|1 | secret 0|1 | proc 0|1 | rune 0|1 | bigwigs <label> <seconds> | timers | db | calls
--   restrict 0|1: the game hides auras out of combat too (a key, an encounter: C_Secrets)
--   ui 0|1: Alt+Z hides the interface (UIParent and every frame under it) or shows it again
--   rcstatus <unit> <ready|notready|waiting>: what GetReadyCheckStatus says of a unit
--   an event argument SECRET is a secret value (the game's numbers in a dungeon or raid)
--   timeline add|stop|pause|resume <event id> [seconds]: a bar from BigWigs' Timeline plugin (Blizzard's
--                encounter timeline, its spell's name secret), started, stopped, paused or resumed by event id
--   actions | said | printed | beacon | focused | link [frames]: run the strip's OnUpdate
--   (a frame each, 1/60 s apart) and answer its cells in physical pixels

local addonFile = arg[1]

-- ── a JSON writer and a reader for one string ─────────────────────────────
local function json(v)
    local t = type(v)
    if t == "table" then
        local n, isArr = 0, true
        for k in pairs(v) do n = n + 1; if type(k) ~= "number" then isArr = false end end
        local out = {}
        if isArr and n == #v then
            for i = 1, #v do out[i] = json(v[i]) end
            return "[" .. table.concat(out, ",") .. "]"
        end
        for k, x in pairs(v) do out[#out + 1] = json(tostring(k)) .. ":" .. json(x) end
        return "{" .. table.concat(out, ",") .. "}"
    elseif t == "string" then
        return '"' .. v:gsub('[%c"\\]', function(c)
            local m = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n" }
            return m[c] or string.format("\\u%04x", c:byte())
        end) .. '"'
    elseif t == "number" or t == "boolean" then
        return tostring(v)
    end
    return "null"
end
local function unjson(s)   -- a JSON string literal (what the test sends)
    s = s:match('^"(.*)"$') or s
    s = s:gsub("\\u(%x%x%x%x)", function(h) return string.char(tonumber(h, 16)) end)
    s = s:gsub("\\(.)", { n = "\n", ['"'] = '"', ["\\"] = "\\", ["/"] = "/" })
    return s
end

-- ── the game ─────────────────────────────────────────────────────────────
local clock, timers = 1000, {}
local combat, secretOn, chatOpen, chatText, clipboard = false, false, false, "", ""
local procOn, runeOn, restricted, rcStatus = false, false, false, {}
local bindings, calls, registered, bwHandlers = {}, {}, {}, {}
-- The player's own bindings: Bloodlust on Ctrl+C, an ability on C.
local gameBindings = { ["CTRL-C"] = "BLOODLUST", C = "ACTIONBUTTON9", HOME = "PREVVIEW", ENTER = "OPENCHAT" }
local actions, said = {}, {}
local SECRET = setmetatable({}, { __tostring = function() return "<secret>" end })
local frames = {}

function issecretvalue(v) return v == SECRET end
function GetTime() return clock end
function time() return 1700000000 + math.floor(clock) end
function InCombatLockdown() return combat end
function wipe(t) for k in pairs(t) do t[k] = nil end return t end
local printed = {}
function print(...) printed[#printed + 1] = table.concat({ ... }, " ") end
function geterrorhandler() return function(e) calls[#calls + 1] = { error = tostring(e) } end end
function GetCVarBool() return true end
local early = false
function UnitFullName() if early then return "Thrall", nil end return "Thrall", "Draenor" end
local CLASS_IDS = { WARRIOR = 1, PALADIN = 2, HUNTER = 3, ROGUE = 4, PRIEST = 5, DEATHKNIGHT = 6, SHAMAN = 7,
                    MAGE = 8, WARLOCK = 9, MONK = 10, DRUID = 11, DEMONHUNTER = 12, EVOKER = 13 }
local group, inRaid = { "PRIEST", "MAGE", "DRUID", "EVOKER" }, false   -- the others: party1-4, or raid2-n
local function classOf(unit)
    if unit == "player" or unit == "raid1" and inRaid then return "WARRIOR" end
    local i = tonumber(unit:match("^party(%d+)$") or "")
    if inRaid then i = tonumber(unit:match("^raid(%d+)$") or "") ; i = i and i - 1 end
    return i and group[i]
end
function UnitClass(unit)
    local token = classOf(unit or "player")
    if token then return token:sub(1, 1) .. token:sub(2):lower(), token, CLASS_IDS[token] end
end
function UnitName(unit)
    if unit == "player" or (inRaid and unit == "raid1") then return "Thrall", nil end
    local token = classOf(unit)
    if token then return token:sub(1, 1) .. token:sub(2):lower(), nil end
end
function hooksecurefunc(t, name, fn)
    local orig = t[name]
    t[name] = function(...) local r = orig(...); fn(...); return r end
end
C_Spell = { GetSpellInfo = function(id) if id == 740 then return { name = "Tranquility" } end end }

-- NSRT, as much as the companion reads of it: its public callbacks, its
-- schedule for each encounter and phase, when the phase began.
local nsCallbacks = {}
NSAPI = { RegisterCallback = function(_, event, fn) nsCallbacks[event] = fn end }
NorthernSkyRaidTools = {
    ProcessedReminder = {
        [3306] = { [1] = { { time = 13, dur = 8, text = "Ice Block the |TInterface\\TargetingFrame\\UI-RaidTargetingIcon_8:0|t soak" },
                           { time = 40, dur = 8, spellID = 740 },
                           { time = 25, dur = 8, text = "|cff00ff00Spread|r" } },
                   [2] = { { time = 5, dur = 5, text = "Stack" } } },
        [3307] = { [1] = { { time = 7, dur = 5, text = "Other boss" } } },
    },
    -- As NSRT 12.1 does: at ENCOUNTER_START it stamps PhaseSwapTime first; a
    -- later phase (its encounter alerts) calls StartReminders, then stamps it.
    StartReminders = function(self, phase, test)
        self.Phase = phase
        local first = phase == 1
        if first and not test then self.PhaseSwapTime = clock end
        if nsCallbacks.NSRT_PHASE then nsCallbacks.NSRT_PHASE("NSRT_PHASE", phase, self.EncounterID, test) end
        if not first and not test then self.PhaseSwapTime = clock end
    end,
    DelayAllReminders = function(self, delay)
        for _, info in ipairs(self.ProcessedReminder[self.EncounterID][self.Phase]) do info.time = info.time + delay end
    end,
}
function UnitLevel() return 80 end
function GetNormalizedRealmName() if not early then return "Draenor" end end
function GetSpecialization() return 1 end
function GetSpecializationInfo() return 71, "Arms" end
function GetAverageItemLevel() return 651.4, 648.25 end
function GetMoney() return 123456789012 end
function GetRealZoneText() return "Dornogal" end
function GetNumGroupMembers() return #group > 0 and #group + 1 or 0 end
function IsInRaid() return inRaid end
function GetWeaponEnchantInfo() return true, 1800000, 1, 6512, false end
function GetInventoryItemDurability(slot) if slot == 5 then return 40, 100 end if slot == 1 then return 100, 100 end end
NUM_TOTAL_EQUIPPED_BAG_SLOTS = 5
Enum = { BagIndex = { Backpack = 0, Bag_1 = 1, Bank = -1, CharacterBankTab_1 = 6, AccountBankTab_1 = 12 },
         BankType = { Account = 2 }, PlayerInteractionType = { Banker = 8, AccountBanker = 68 } }
C_AddOns = { GetAddOnMetadata = function() return "2.0.0" end }
C_Container = {
    GetContainerNumSlots = function(bag) if bag >= 0 and bag <= 4 or bag == 6 or bag == 12 then return 20 end return 0 end,
    GetContainerNumFreeSlots = function() return 5 end,
    GetContainerItemInfo = function(bag, slot)
        if slot == 1 or slot == 2 then
            return { itemID = 191 + (bag == 6 and 100 or 0), stackCount = 10, quality = 1,
                     hyperlink = "|cffffffff|Hitem:191::|h[Phial of Truth]|h|r" }
        end
    end,
}
C_Bank = { FetchDepositedMoney = function() return 5000000 end }
C_MythicPlus = { GetOwnedKeystoneChallengeMapID = function() return 503 end, GetOwnedKeystoneLevel = function() return 12 end }
-- A Mythic+ key as the game shows it (mplus …): Ara-Kara, 30 minutes, three bosses and enemy forces,
-- whose quantityString is the raw count with a "%" after it, as Blizzard's own tracker reads it.
local mkey = nil
C_ChallengeMode = {
    GetMapUIInfo = function() return "Ara-Kara", 503, 1800 end,
    GetActiveChallengeMapID = function() return mkey and mkey.map end,
    GetActiveKeystoneInfo = function() if mkey then return mkey.level, {} end return 0, {} end,
    GetDeathCount = function() if mkey then return mkey.deaths, mkey.deaths * 15 end return 0, 0 end,
    GetChallengeCompletionInfo = function() return mkey and mkey.result end,
}
function GetWorldElapsedTime() return 1, (mkey and mkey.started) and (clock - mkey.started) or 0, 2 end
C_Scenario = { GetStepInfo = function() if mkey then return "Ara-Kara", "", #mkey.criteria end end }
C_ScenarioInfo = { GetCriteriaInfo = function(i) return mkey and mkey.criteria[i] end }
C_WeeklyRewards = { GetActivities = function() return { { type = 1, index = 1, progress = 3, threshold = 1, level = 12 } } end }
C_ChatInfo = { InChatMessagingLockdown = function() return false end }
C_Secrets = { ShouldAurasBeSecret = function() return combat or restricted end }
function GetReadyCheckStatus(unit) return rcStatus[unit] end
C_UnitAuras = { GetAuraDataByIndex = function(_, i)
    local list = { { spellId = 1459, name = "Arcane Intellect", expirationTime = clock + 3000, duration = 3600 },
                   { spellId = secretOn and SECRET or 2, name = secretOn and SECRET or "Well Fed",
                     expirationTime = clock + 60, duration = 3600 } }
    if procOn then list[#list + 1] = { spellId = 2825, name = "Bloodlust", expirationTime = clock + 40, duration = 40 } end
    if runeOn then   -- Midnight's augment rune: its buff's name says nothing of a rune
        list[#list + 1] = { spellId = 1264426, name = "Void-Touched", expirationTime = clock + 3000, duration = 3600 }
    end
    return list[i]
end }
C_Timer = {
    After = function(s, f) timers[#timers + 1] = { at = clock + s, f = f } end,
    NewTimer = function(s, f)
        local t = { at = clock + s, f = f }
        function t:Cancel() self.cancelled = true end
        timers[#timers + 1] = t
        return t
    end,
}
ChatFontNormal = {}
BigWigsLoader = { RegisterMessage = function(_, msg, fn) bwHandlers[msg] = fn end }

-- Blizzard's own slash command (set by secure code) and an addon's.
SlashCmdList = {
    READYCHECK = function() calls[#calls + 1] = { cmd = "/readycheck" } end,
    BIGWIGSPULL = function(msg) calls[#calls + 1] = { cmd = "/pull", msg = msg } end,
}
SLASH_READYCHECK1 = "/readycheck"
SLASH_BIGWIGSPULL1 = "/pull"
function issecurevariable(t, k) return t == SlashCmdList and k == "READYCHECK" end

local focused = nil
local UI_SCALE = 0.71
local function newTexture()
    local t = { x = 0, y = 0, w = 0, h = 0, shown = true }
    function t:Show() self.shown = true end
    function t:Hide() self.shown = false end
    function t:SetColorTexture(r, g, b) self.rgb = { r, g, b } end
    function t:SetPoint(_, _, _, x, y) self.x, self.y = x or 0, y or 0 end
    function t:SetSize(w, h) self.w, self.h = w, h end
    function t:ClearAllPoints() end
    return t
end
local function newFrame(kind, name, parent)
    local f = { kind = kind, name = name, parent = parent, scripts = {}, attrs = {}, text = "", highlighted = false,
                shown = true, textures = {}, x = 0, y = 0 }
    function f:SetPoint(_, _, _, x, y) self.x, self.y = x or 0, y or 0 end
    function f:SetScript(s, fn) self.scripts[s] = fn end
    function f:HookScript(s, fn)
        local old = self.scripts[s]
        self.scripts[s] = function(...) if old then old(...) end fn(...) end
    end
    function f:HasScript(s) return s ~= "OnKeyDownMissing" end
    function f:RegisterEvent(e) registered[e] = registered[e] or {}; table.insert(registered[e], self) end
    function f:SetAttribute(k, v) self.attrs[k] = v end
    function f:GetName() return self.name end
    function f:SetText(t) self.text = t or ""; self.highlighted = false end
    function f:GetText() return self.text end
    function f:SetFocus()
        if focused == self or not self:IsVisible() then return end   -- a hidden edit box takes no focus
        local was = focused
        focused = self
        if was and was.scripts.OnEditFocusLost then was.scripts.OnEditFocusLost(was) end
        if self.scripts.OnEditFocusGained then self.scripts.OnEditFocusGained(self) end
    end
    function f:ClearFocus()
        self.highlighted = false
        if focused ~= self then return end
        focused = nil
        if self.scripts.OnEditFocusLost then self.scripts.OnEditFocusLost(self) end
    end
    function f:Show() self.shown = true end
    function f:Hide() self.shown = false end
    function f:IsShown() return self.shown end
    function f:IsVisible() return self.shown and (self.parent == nil or self.parent:IsVisible()) end
    function f:SetShown(on) self.shown = on and true or false end
    function f:GetEffectiveScale() return (self.parent or self == UIParent) and UI_SCALE or 1 end
    function f:RegisterUnitEvent(e) self:RegisterEvent(e) end
    function f:CreateTexture() local t = newTexture(); self.textures[#self.textures + 1] = t; return t end
    function f:HighlightText() self.highlighted = true end
    for _, m in ipairs({ "SetSize", "SetFontObject", "SetAutoFocus", "SetAlpha", "EnableMouse",
                         "SetMaxLetters", "RegisterForClicks", "SetFrameStrata", "SetFrameLevel",
                         "ClearAllPoints" }) do f[m] = function() end end
    if name then _G[name] = f; frames[name] = f end
    return f
end
UIParent = newFrame("Frame", "UIParent")
function CreateFrame(kind, name, parent) return newFrame(kind, name, parent) end
function SetOverrideBindingClick(owner, _, key, button) bindings[key] = button end
function ClearOverrideBindings() wipe(bindings) end
local ctrlDown = false
function IsControlKeyDown() return ctrlDown end
local screenW, screenH = 2560, 1440
function GetPhysicalScreenSize() return screenW, screenH end

local chunk = assert(loadfile(addonFile))
chunk("TouchDashboard", {})

local function fire(event, ...)
    for _, f in ipairs(registered[event] or {}) do
        if f.scripts.OnEvent then f.scripts.OnEvent(f, event, ...) end
    end
end
local function runTimers(upTo)
    local due = {}
    for i = #timers, 1, -1 do
        local t = timers[i]
        if t.at <= upTo then table.remove(timers, i); due[#due + 1] = t end
    end
    for _, t in ipairs(due) do if not t.cancelled then t.f() end end
end

-- Each answer is flushed: Microsoft's C runtime takes setvbuf's line mode as
-- full buffering, and the test waited for a line that never came.
for line in io.lines() do
    local cmd, rest = line:match("^(%S+)%s*(.*)$")
    local out = "ok"
    if cmd == "press" then
        local mods, key = {}, nil
        for w in rest:gmatch("%S+") do
            if w == "ALT" or w == "CTRL" or w == "SHIFT" then mods[w] = true else key = w end
        end
        local chord = (mods.ALT and "ALT-" or "") .. (mods.CTRL and "CTRL-" or "") .. (mods.SHIFT and "SHIFT-" or "") .. key
        ctrlDown = mods.CTRL or false
        local res = {}
        if focused then                                   -- an edit box has the keyboard
            if mods.CTRL and key == "C" then
                if focused.highlighted then clipboard = focused.text; res.copied = focused.text end
                if focused.scripts.OnKeyDown then focused.scripts.OnKeyDown(focused, "C") end
            elseif mods.CTRL and key == "V" then
                focused:SetText(clipboard)
            elseif key == "ENTER" then
                focused.scripts.OnEnterPressed(focused)
            end
        elseif chatOpen then                              -- chat's own box
            if mods.CTRL and key == "V" then chatText = chatText .. clipboard
            elseif key == "ENTER" then said[#said + 1] = chatText; chatText = ""; chatOpen = false end
        elseif bindings[chord] then                       -- an override binding of exactly this chord
            local b = frames[bindings[chord]]
            if b.attrs.macrotext then res.slot = b.attrs.macrotext elseif b.scripts.OnClick then b.scripts.OnClick(b) end
            if b.scripts.PostClick then b.scripts.PostClick(b) end
        else                                              -- the game's own, or the plain key's
            local action = gameBindings[chord] or gameBindings[key]
            if action == "OPENCHAT" then chatOpen = true elseif action then actions[#actions + 1] = action end
        end
        ctrlDown = false
        runTimers(clock)
        out = json(res)
    elseif cmd == "screen" then                         -- the window resized
        screenW, screenH = rest:match("^(%d+)%s+(%d+)$")
        screenW, screenH = tonumber(screenW), tonumber(screenH)
        fire("DISPLAY_SIZE_CHANGED")
    elseif cmd == "clip" then
        clipboard = unjson(rest)
    elseif cmd == "slash" then                          -- /tdc <rest>, typed in game
        SlashCmdList.TOUCHDASHBOARD(rest)
    elseif cmd == "unload" then                           -- the addon stops running (disabled, an error)
        wipe(bindings)
    elseif cmd == "key" then
        local b = bindings[rest] and frames[bindings[rest]]
        out = b and json(b.attrs.macrotext or "") or "unbound"
    elseif cmd == "event" then
        local e, args = nil, {}
        for w in rest:gmatch("%S+") do
            if e then
                if w == "true" then args[#args + 1] = true
                elseif w == "SECRET" then args[#args + 1] = SECRET
                elseif w == "false" then args[#args + 1] = false
                else args[#args + 1] = tonumber(w) or w end
            else
                e = w
            end
        end
        fire(e, unpack(args))
        runTimers(clock + 3)
    elseif cmd == "early" then
        early = rest == "1"
    elseif cmd == "stray" then
        TouchDashboardDB = TouchDashboardDB or {}
        TouchDashboardDB.chars = TouchDashboardDB.chars or {}
        TouchDashboardDB.chars["Thrall-"] = { name = "Thrall", gold = 0, ilvl = 0 }
    elseif cmd == "mplus" then
        local verb, a, b, c = rest:match("^(%S+)%s*(%S*)%s*(%S*)%s*(%S*)$")
        if verb == "start" then
            mkey = { map = 503, level = tonumber(a), deaths = 0, criteria = {
                { description = "Avanoxx defeated", completed = false },   -- as the game names them
                { description = "\226\156\147 Anub'zekt", completed = false },     -- Blizzard's check mark in front
                { description = "Ki'katal the Harvester", completed = false },
                { description = "Enemy Forces", isWeightedProgress = true, quantity = 0, totalQuantity = 460,
                  quantityString = "0%", completed = false } } }
            fire("CHALLENGE_MODE_START", 503)
        elseif verb == "go" then
            mkey.started = clock
            fire("WORLD_STATE_TIMER_START", 1)
        elseif verb == "kill" then
            mkey.criteria[tonumber(a)].completed = true
            fire("SCENARIO_CRITERIA_UPDATE")
        elseif verb == "forces" then
            mkey.criteria[4].quantityString = a .. "%"
            fire("SCENARIO_CRITERIA_UPDATE")
        elseif verb == "death" then
            mkey.deaths = mkey.deaths + 1
            fire("CHALLENGE_MODE_DEATH_COUNT_UPDATED")
        elseif verb == "done" then
            mkey.result = { time = tonumber(a), onTime = b == "1", keystoneUpgradeLevels = tonumber(c) }
            fire("CHALLENGE_MODE_COMPLETED")
        elseif verb == "reset" then
            mkey = nil
            fire("CHALLENGE_MODE_RESET")
        end
    elseif cmd == "nsrt" then
        local verb, a, b = rest:match("^(%S+)%s*(%S*)%s*(%S*)$")
        local ns = NorthernSkyRaidTools
        if verb == "phase" then
            ns.EncounterID = tonumber(b)
            ns:StartReminders(tonumber(a), false)
        elseif verb == "test" then
            ns:StartReminders(1, true)
        elseif verb == "delay" then
            ns:DelayAllReminders(tonumber(a))
        elseif verb == "hide" then
            nsCallbacks.NSRT_HIDE_REMINDERS("NSRT_HIDE_REMINDERS")
        end
    elseif cmd == "group" then
        local kind, list = rest:match("^(%S+)%s*(.*)$")
        group, inRaid = {}, kind == "raid"
        for w in list:gmatch("%S+") do group[#group + 1] = w end
        fire("GROUP_ROSTER_UPDATE")
    elseif cmd == "combat" then
        combat = rest == "1"
    elseif cmd == "proc" then
        procOn = rest == "1"
    elseif cmd == "rune" then
        runeOn = rest == "1"
    elseif cmd == "secret" then
        secretOn = rest == "1"
    elseif cmd == "restrict" then
        restricted = rest == "1"
    elseif cmd == "rcstatus" then
        local unit, status = rest:match("^(%S+)%s+(%S+)$")
        rcStatus[unit] = status
    elseif cmd == "timeline" then
        local verb, id, secs = rest:match("^(%S+)%s+(%S+)%s*(%S*)$")
        id = tonumber(id) or id
        if verb == "add" then
            bwHandlers.BigWigs_StartBar("BigWigs_StartBar", nil, nil, SECRET, tonumber(secs), SECRET, nil, nil, id, id)
        else
            bwHandlers[({ stop = "BigWigs_StopBar", pause = "BigWigs_PauseBar", resume = "BigWigs_ResumeBar" })[verb]](
                "BigWigs_" .. verb, nil, nil, id)
        end
    elseif cmd == "bigwigs" then
        local label, secs = rest:match("^(.-)%s+(%d+)$")
        bwHandlers.BigWigs_StartBar("BigWigs_StartBar", {}, "key", label, tonumber(secs))
    elseif cmd == "timers" then
        clock = clock + (tonumber(rest) or 2)
        runTimers(clock)
    elseif cmd == "db" then
        out = json(TouchDashboardDB or {})
    elseif cmd == "sv" then   -- TouchDashboardDB as WoW writes it to SavedVariables
        local function lua(v, indent)
            if type(v) == "string" then
                return '"' .. v:gsub('[\\"\n]', { ["\\"] = "\\\\", ['"'] = '\\"', ["\n"] = "\\n" }) .. '"'
            elseif type(v) ~= "table" then
                return tostring(v)
            end
            local pad, out = string.rep("\t", indent + 1), { "{" }
            for i, x in ipairs(v) do out[#out + 1] = pad .. lua(x, indent + 1) .. ", -- [" .. i .. "]" end
            for k, x in pairs(v) do
                if type(k) ~= "number" or k > #v then
                    local key = type(k) == "number" and "[" .. k .. "]" or '["' .. k .. '"]'
                    out[#out + 1] = pad .. key .. " = " .. lua(x, indent + 1) .. ","
                end
            end
            out[#out + 1] = string.rep("\t", indent) .. "}"
            return table.concat(out, "\n")
        end
        out = json("\nTouchDashboardDB = " .. lua(TouchDashboardDB or {}, 0) .. "\n")
    elseif cmd == "calls" then
        out = json(calls)
    elseif cmd == "actions" then
        out = json(actions)
    elseif cmd == "said" then
        out = json(said)
    elseif cmd == "printed" then                        -- what the addon printed to your chat frame
        out = json(printed)
    elseif cmd == "beacon" then   -- the beacon's cells in physical pixels, as WoW would draw them
        local b = frames.TouchDashboardBeacon
        local px = screenH / 768 * b:GetEffectiveScale()
        local cells = {}
        for i, t in ipairs(b.textures) do
            cells[i] = { rgb = t.rgb, x = (b.x + t.x) * px, w = t.w * px, h = t.h * px }
        end
        out = json({ shown = b:IsVisible(), cells = cells })
    elseif cmd == "link" then
        local f = frames.TouchDashboardLink
        for _ = 1, tonumber(rest) or 1 do
            clock = clock + 1 / 60
            runTimers(clock)
            if f:IsVisible() and f.scripts.OnUpdate then f.scripts.OnUpdate(f, 1 / 60) end
        end
        local px, syms, xs, ws, hs = screenH / 768, {}, {}, {}, {}
        for i, t in ipairs(f.textures) do
            syms[i] = t.shown and t.rgb and (t.rgb[1] * 4 + t.rgb[2] * 2 + t.rgb[3]) or -1
            xs[i], ws[i], hs[i] = (f.x + t.x) * px, t.w * px, t.h * px
        end
        out = json({ shown = f:IsVisible(), syms = syms, xs = xs, ws = ws, hs = hs })
    elseif cmd == "bench" then                          -- the strip's own cost, in ms a frame
        local f = frames.TouchDashboardLink
        local function run(n, change)
            local t0 = os.clock()
            for i = 1, n do
                clock = clock + (change and 0.3 or 1 / 60)
                if change then bwHandlers.BigWigs_StartBar("BigWigs_StartBar", {}, "key", "Bar" .. i % 8, 30 + i % 5) end
                f.scripts.OnUpdate(f, 1 / 60)
            end
            return (os.clock() - t0) / n * 1000
        end
        run(5, true)
        out = json({ idle = run(2000, false), rebuild = run(300, true) })
    elseif cmd == "ui" then
        if rest == "0" then UIParent:Hide() else UIParent:Show() end
        if focused and not focused:IsVisible() then focused:ClearFocus() end   -- hidden, it loses the keyboard
    elseif cmd == "focused" then
        out = tostring(focused ~= nil)
    end
    io.write(out, "\n")
    io.stdout:flush()
end
