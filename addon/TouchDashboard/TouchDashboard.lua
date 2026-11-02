-- Touch Dashboard companion addon.
--
-- WoW lets no addon talk to anything outside the game, so the dashboard on
-- this PC presses keys, and this addon answers through a hidden edit box:
--
--   Ctrl+Alt+Shift+Home  focuses the box, which then holds "TDS1 - <json>":
--                        the game's state, highlighted for the PC to copy.
--                        While the box has the keyboard, a beacon lights on
--                        the window's top edge: the PC reads it from the
--                        window before it presses Ctrl+C, Ctrl+V or Enter,
--                        which without the box would reach the game's own
--                        bindings (Bloodlust on Ctrl+C, say).
--   The PC pastes        "TD1 <nonce> <flags> <percent-encoded lines>" and
--                        presses Enter: each line is an addon's slash command,
--                        run here without opening chat. With flag "r" the box
--                        then holds "TDS1 <nonce> <json>" with the result.
--   Ctrl+Alt+Shift+F1-12 click secure buttons holding the dashboard's key
--                        slots ("/tdc slots 1=<text>;2=<text>"), loaded out of
--                        combat. They work in combat and in chat lockdown.
--   Ctrl+Alt+Shift+End   clicks the run button: with flag "b" the box loads the
--                        lines into it instead (out of combat), and the PC
--                        presses its chord at once. Blizzard's own commands
--                        (/readycheck, /wm…) then never go through chat.
--
--   The live link       paints the game's state into one row of cells along
--                        the top edge of the window; the dashboard reads it
--                        from the window several times a second (see below).
--
-- Blizzard's own commands (/cast, chat, /readycheck…) are never run from here:
-- an addon may not, and the dashboard types those into chat instead. Nothing
-- that is secret (auras in combat, encounters, M+) leaves the game.
-- TouchDashboardDB keeps each character's gold, bags, keystone, vault and
-- items, which WoW writes on /reload, logout and exit for the dashboard.

local ADDON = ...
local VERSION = (C_AddOns and C_AddOns.GetAddOnMetadata(ADDON, "Version")) or "0"
-- WoW writes chords as ALT-CTRL-SHIFT-<key>, and a binding in any other
-- order never fires: the key then falls through to its plain binding.
local FOCUS_KEY = "ALT-CTRL-SHIFT-HOME"
local SLOT_KEY = "ALT-CTRL-SHIFT-F%d"
local RUN_KEY = "ALT-CTRL-SHIFT-END"
local SLOTS = 12

local issecret = issecretvalue or function() return false end
local function plain(v)
    if v == nil or issecret(v) then return nil end
    return v
end
local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
end

-- ── encoding ─────────────────────────────────────────────────────────────

local function unpct(s)
    return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

local function adler32(s)
    local a, b = 1, 0
    for i = 1, #s do
        a = (a + s:byte(i)) % 65521
        b = (b + a) % 65521
    end
    return b * 65536 + a
end

local ESC = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }
local function jsonString(s)
    return '"' .. tostring(s):gsub('[%c"\\]', function(c)
        return ESC[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
end

local function isArray(t)
    local n = 0
    for k in pairs(t) do
        if type(k) ~= "number" then return false end
        n = n + 1
    end
    return n == #t
end

local function encode(v)
    local t = type(v)
    if t == "table" then
        local out = {}
        if isArray(v) then
            for i = 1, #v do out[i] = encode(v[i]) end
            return "[" .. table.concat(out, ",") .. "]"
        end
        for k, x in pairs(v) do out[#out + 1] = jsonString(k) .. ":" .. encode(x) end
        return "{" .. table.concat(out, ",") .. "}"
    elseif t == "string" then
        return jsonString(v)
    elseif t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        -- %.0f, not %d: a 32-bit long would overflow on gold in copper
        if v == math.floor(v) then return string.format("%.0f", v) end
        return string.format("%.2f", v)
    elseif t == "boolean" then
        return v and "true" or "false"
    end
    return "null"
end

-- ── the game's state ─────────────────────────────────────────────────────

-- The player's name and realm, or nil this early in a login: v2.1.0 saved a
-- second "Name-" entry then, with no realm, gold or item level yet.
local function whoAmI()
    local name, realm = UnitFullName("player")
    realm = realm or GetNormalizedRealmName()
    if type(name) ~= "string" or name == "" or name == (UNKNOWNOBJECT or "Unknown")
            or type(realm) ~= "string" or realm == "" then
        return nil
    end
    return name, realm
end

local function bagSummary()
    local free, total = 0, 0
    for bag = 0, (NUM_TOTAL_EQUIPPED_BAG_SLOTS or 5) do
        local n = C_Container.GetContainerNumSlots(bag) or 0
        total = total + n
        if n > 0 then free = free + (C_Container.GetContainerNumFreeSlots(bag) or 0) end
    end
    return { free = free, total = total }
end

local function durability()
    local low, broken = 100, 0
    for slot = 1, 19 do
        local cur, max = GetInventoryItemDurability(slot)
        if cur and max and max > 0 then
            local pct = math.floor(cur / max * 100)
            if pct < low then low = pct end
            if cur == 0 then broken = broken + 1 end
        end
    end
    return { low = low, broken = broken }
end

local function buffs()
    local out = {}
    local now = GetTime()
    for i = 1, 80 do
        local a = try(C_UnitAuras.GetAuraDataByIndex, "player", i, "HELPFUL")
        if not a then break end
        local id, name = plain(a.spellId), plain(a.name)
        local exp, dur = plain(a.expirationTime), plain(a.duration)
        if type(id) == "number" and type(name) == "string" then
            local rem = -1
            if type(exp) == "number" and exp > 0 then rem = math.max(0, math.floor(exp - now)) end
            out[#out + 1] = { id = id, name = name, rem = rem, exp = type(exp) == "number" and exp or 0,
                              dur = type(dur) == "number" and math.floor(dur) or 0 }
        end
    end
    return out
end

local function keystone()
    if not C_MythicPlus then return nil end
    local map = try(C_MythicPlus.GetOwnedKeystoneChallengeMapID)
    local level = try(C_MythicPlus.GetOwnedKeystoneLevel)
    if not map or not level or level == 0 then return nil end
    local name = C_ChallengeMode and try(C_ChallengeMode.GetMapUIInfo, map)
    return { map = map, level = level, name = name }
end

local function vault()
    if not (C_WeeklyRewards and C_WeeklyRewards.GetActivities) then return nil end
    local out = {}
    for _, a in ipairs(try(C_WeeklyRewards.GetActivities) or {}) do
        out[#out + 1] = { t = a.type, i = a.index, p = a.progress, th = a.threshold, l = a.level }
    end
    return out
end

local function bagIndexes(kind)
    if kind == "bags" then return { 0, 1, 2, 3, 4, 5 } end
    local out = {}
    for name, idx in pairs(Enum.BagIndex or {}) do
        if kind == "bank" and (name == "Bank" or name == "Reagentbank" or name:match("^BankBag_")
                or name:match("^CharacterBankTab_")) then
            out[#out + 1] = idx
        elseif kind == "warband" and name:match("^AccountBankTab_") then
            out[#out + 1] = idx
        end
    end
    return out
end

-- {id, n, q, name}, one per item, counts added up
local function scanItems(kind)
    local byId = {}
    for _, bag in ipairs(bagIndexes(kind)) do
        for slot = 1, (C_Container.GetContainerNumSlots(bag) or 0) do
            local info = C_Container.GetContainerItemInfo(bag, slot)
            if info and info.itemID then
                local e = byId[info.itemID]
                if not e then
                    e = { id = info.itemID, n = 0, q = info.quality,
                          name = info.hyperlink and info.hyperlink:match("%[(.-)%]") }
                    byId[info.itemID] = e
                end
                e.n = e.n + (info.stackCount or 1)
            end
        end
    end
    local out = {}
    for _, e in pairs(byId) do out[#out + 1] = e end
    return out
end

-- Boss mods' bars: what they started and have not stopped, by name. Their
-- length is taken only when it is not secret, and so is their name; a bar
-- whose name is secret but whose time is not (BigWigs' Timeline plugin in
-- Midnight: Blizzard's encounter timeline for every boss with no module of
-- its own, e.g. a dungeon's) is kept unnamed, by its timeline event id.
-- A paused bar (an intermission) keeps its time left until it resumes.
local bars, barLen, barName, barPaused, dbmIds = {}, {}, {}, {}, {}
local linkDirty = function() end          -- the live link's, once it exists
local function barKey(label, eventId)
    label, eventId = plain(label), plain(eventId)
    if type(label) == "string" and label ~= "" then return label, label end
    if type(eventId) == "number" or type(eventId) == "string" then return "\1" .. eventId, nil end
end
local function addBar(label, seconds, eventId)
    seconds = plain(seconds)
    local key, name = barKey(label, eventId)
    if not key or type(seconds) ~= "number" or seconds <= 0 then return end
    bars[key], barLen[key], barName[key], barPaused[key] = GetTime() + seconds, seconds, name, nil
    linkDirty()
end
local function stopBar(key)
    if not key then return end
    bars[key], barLen[key], barName[key], barPaused[key] = nil, nil, nil, nil
    linkDirty()
end
local function pauseBar(key, on)
    local now = GetTime()
    if on and key and bars[key] then
        barPaused[key], bars[key] = bars[key] - now, nil
    elseif not on and key and barPaused[key] then
        bars[key], barPaused[key] = now + barPaused[key], nil
    else
        return
    end
    linkDirty()
end
local function barList()
    local out, now = {}, GetTime()
    for key, exp in pairs(bars) do
        if exp > now then
            out[#out + 1] = { label = barName[key] or "", rem = math.floor((exp - now) * 10) / 10 }
        else
            bars[key], barLen[key], barName[key] = nil, nil, nil
        end
    end
    table.sort(out, function(a, b) return a.rem < b.rem end)
    return out
end

local function hookBossMods()
    if BigWigsLoader and BigWigsLoader.RegisterMessage then
        local owner = {}
        -- (module, key, text, time, icon, maxQueueDuration, maxTime, eventId)
        BigWigsLoader.RegisterMessage(owner, "BigWigs_StartBar", function(_, _, _, text, time, _, _, _, eventId)
            addBar(text, time, eventId)
        end)
        -- (module, text[, eventId]): a boss module stops a bar by its text, the Timeline plugin by its event
        BigWigsLoader.RegisterMessage(owner, "BigWigs_StopBar", function(_, _, text, eventId) stopBar((barKey(text, eventId))) end)
        BigWigsLoader.RegisterMessage(owner, "BigWigs_PauseBar", function(_, _, text, eventId) pauseBar((barKey(text, eventId)), true) end)
        BigWigsLoader.RegisterMessage(owner, "BigWigs_ResumeBar", function(_, _, text, eventId) pauseBar((barKey(text, eventId)), false) end)
        BigWigsLoader.RegisterMessage(owner, "BigWigs_StopBars", function()
            wipe(bars); wipe(barLen); wipe(barName); wipe(barPaused); linkDirty()
        end)
    end
    if DBM and DBM.RegisterCallback then
        local function start(_, id, msg, timer)
            id, msg = plain(id), plain(msg)
            if id and msg then dbmIds[id] = msg end
            addBar(msg, timer)
        end
        DBM:RegisterCallback("DBM_TimerStart", start)
        DBM:RegisterCallback("DBM_TimerBegin", start)
        DBM:RegisterCallback("DBM_TimerStop", function(_, id)
            id = plain(id)
            if id and dbmIds[id] then stopBar(dbmIds[id]); dbmIds[id] = nil end
            linkDirty()
        end)
    end
end

-- Blizzard's own countdown: the pull timer anyone in the group starts, from
-- the raid frames, /countdown or a boss mod (BigWigs and DBM start theirs
-- through it in Midnight), with or without a boss mod here. In a dungeon or
-- raid, an encounter, a key or a PvP match its numbers are secret
-- (SecretInChatMessagingLockdown): then only that one runs is known, until
-- it is cancelled, combat starts or a minute has passed.
local PULL, PULL_HIDDEN_S = "Pull", 60
local pullHidden = nil                  -- GetTime() when a countdown with secret numbers started
local countdown = CreateFrame("Frame")
countdown:SetScript("OnEvent", function(_, event, _, seconds)
    if event == "START_PLAYER_COUNTDOWN" then
        pullHidden = issecret(seconds) and GetTime() or nil
        addBar(PULL, seconds)
    elseif event == "CANCEL_PLAYER_COUNTDOWN" then
        stopBar(PULL)
        pullHidden = nil
    elseif pullHidden then              -- combat or the encounter began: that was the pull
        pullHidden = nil
    else
        return
    end
    linkDirty()
end)
for _, e in ipairs({ "START_PLAYER_COUNTDOWN", "CANCEL_PLAYER_COUNTDOWN", "PLAYER_REGEN_DISABLED", "ENCOUNTER_START" }) do
    pcall(countdown.RegisterEvent, countdown, e)
end
local function pullHiddenNow(now)
    if pullHidden and now - pullHidden > PULL_HIDDEN_S then pullHidden = nil end
    return pullHidden ~= nil
end

-- Whether the game hides aura data from addons now (combat, an encounter, a
-- key, a PvP match; a spell flagged never secret still reads), as Blizzard's
-- C_Secrets says. nil from a client without it: the PC then guesses.
local function aurasSecret()
    if not (C_Secrets and C_Secrets.ShouldAurasBeSecret) then return nil end
    local v = plain(try(C_Secrets.ShouldAurasBeSecret))
    if type(v) == "boolean" then return v end
end

-- The units of the group, you first.
local function groupUnits()
    local out = { "player" }
    local n = GetNumGroupMembers()
    if IsInRaid() then
        out = {}
        for i = 1, n do out[#out + 1] = "raid" .. i end
    else
        for i = 1, n - 1 do out[#out + 1] = "party" .. i end
    end
    return out
end

-- The last ready check: who answered what (1 ready, 2 not ready, 3 no
-- answer when it ended). Its result stays for five minutes. In a dungeon or
-- raid READY_CHECK's asker and time left are secret: the asker is then the
-- one the game already counts as ready (GetReadyCheckStatus, as Blizzard's
-- raid frames read it), and the time Blizzard's default.
local readyCheck = { state = 0, ends = 0, answers = {} }    -- state: 0 none, 1 running, 2 over
local rcEvents = CreateFrame("Frame")
rcEvents:SetScript("OnEvent", function(_, event, a1, a2)
    local rc, now = readyCheck, GetTime()
    if event == "READY_CHECK" then
        rc.state, rc.ends, rc.answers = 1, now + (plain(a2) or 35), {}
        local who = plain(a1)
        if type(who) == "string" then rc.answers[who] = 1 end        -- who asked is ready
        for _, unit in ipairs(GetReadyCheckStatus and groupUnits() or {}) do
            local name = plain(UnitName(unit))
            if type(name) == "string" and plain(try(GetReadyCheckStatus, unit)) == "ready" then rc.answers[name] = 1 end
        end
    elseif event == "READY_CHECK_CONFIRM" then
        local name = plain(UnitName(a1 or ""))
        if type(name) == "string" and rc.state == 1 then rc.answers[name] = plain(a2) and 1 or 2 end
    elseif event == "READY_CHECK_FINISHED" then
        if rc.state ~= 1 then return end
        for _, unit in ipairs(groupUnits()) do
            local name = plain(UnitName(unit))
            if type(name) == "string" and not rc.answers[name] then rc.answers[name] = 3 end
        end
        rc.state, rc.ends = 2, now
    end
    linkDirty()
end)
for _, e in ipairs({ "READY_CHECK", "READY_CHECK_CONFIRM", "READY_CHECK_FINISHED" }) do
    pcall(rcEvents.RegisterEvent, rcEvents, e)
end

-- NSRT's encounter alerts and note reminders for the phase that runs: each
-- with when it happens. NSRT says when a phase starts (NSRT_PHASE, its public
-- callbacks) and keeps the phase's schedule in ProcessedReminder; its own
-- test run (/tdc nstest) counts too. Plain text: colour codes and textures
-- go, raid marks become {star} … {skull}.
local RT = { "star", "circle", "diamond", "triangle", "moon", "square", "cross", "skull" }
local alerts, alertsFor = {}, nil        -- {at, text, spell}; {enc, phase, test, from}
local encounterName = nil

local function alertText(info)
    local s = plain(info.text)
    if type(s) ~= "string" or s == "" then
        local spell = info.spellID and C_Spell and try(C_Spell.GetSpellInfo, info.spellID)
        s = spell and plain(spell.name)
    end
    if type(s) ~= "string" then return nil end
    s = s:gsub("|T[^|]*RaidTargetingIcon_(%d)[^|]*|t", function(n) return "{" .. (RT[tonumber(n)] or "") .. "}" end)
    s = s:gsub("|T.-|t", ""):gsub("|A.-|a", ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", "")
    s = s:gsub("|H.-|h(.-)|h", "%1"):gsub("%s+", " "):gsub("^ ", ""):gsub(" $", "")
    return s ~= "" and s or nil
end

local function readAlerts()
    wipe(alerts)
    local ns, f = _G.NorthernSkyRaidTools, alertsFor
    local all = f and ns and ns.ProcessedReminder
    if type(all) == "table" then
        local function add(list)
            for _, info in ipairs(type(list) == "table" and list or {}) do
                local t = type(info) == "table" and plain(info.time)
                local text = t and alertText(info)
                if type(t) == "number" and text then
                    alerts[#alerts + 1] = { at = f.from + t, text = text, spell = plain(info.spellID) }
                end
            end
        end
        if f.test then
            for _, enc in pairs(all) do add(type(enc) == "table" and enc[f.phase]) end
        elseif type(all[f.enc]) == "table" then
            add(all[f.enc][f.phase])
        end
        table.sort(alerts, function(a, b) return a.at < b.at end)
    end
    linkDirty()
end

local function hookNsrt()
    local ns = _G.NorthernSkyRaidTools
    if not (NSAPI and NSAPI.RegisterCallback and ns) then return end
    local owner = {}
    NSAPI.RegisterCallback(owner, "NSRT_PHASE", function(_, phase, encID, test)
        phase, encID = plain(phase), plain(encID)
        -- The phase begins now: NSRT fires this before it stamps a later
        -- phase's PhaseSwapTime (its encounter alerts call StartReminders
        -- first), so that one would still be the last phase's start.
        local from = GetTime()
        alertsFor = { enc = encID, phase = phase, test = test and true or false, from = from }
        readAlerts()
    end)
    NSAPI.RegisterCallback(owner, "NSRT_HIDE_REMINDERS", function()
        alertsFor = nil
        readAlerts()
    end)
    -- A boss's cast moved the rest of the phase: NSRT shifted its times in place.
    if type(ns.DelayAllReminders) == "function" then hooksecurefunc(ns, "DelayAllReminders", readAlerts) end
end

-- What the two sections below say, for the snapshot and the live link alike.
local function readyCheckNow(now)
    local rc = readyCheck
    if rc.state == 2 and now - rc.ends > 300 then rc.state = 0 end
    local out = { state = rc.state, ends = rc.ends, total = 0, ready = 0, missing = {} }
    if rc.state == 0 then return out end
    for _, unit in ipairs(groupUnits()) do
        local name = plain(UnitName(unit))
        if type(name) == "string" then
            out.total = out.total + 1
            local a = rc.answers[name] or 0              -- 0: not answered yet
            if a == 1 then out.ready = out.ready + 1
            elseif #out.missing < 8 then out.missing[#out.missing + 1] = { name = name, a = a } end
        end
    end
    return out
end

local function alertsNow(now)
    local soon = {}
    for _, a in ipairs(alerts) do
        if a.at > now and #soon < 8 then soon[#soon + 1] = a end
    end
    local f = alertsFor
    return soon, f and (f.test and 255 or f.phase or 0) or 0, f and (f.test and "Test" or encounterName) or nil
end

-- The Mythic+ run: the dungeon, the key, the timer (its start in GetTime(),
-- so a screen counts it), deaths and the time they cost, enemy forces, each
-- boss and when it fell. After the last boss, the result, for 15 minutes or
-- until the key is reset. Read as EllesmereUI's timer reads it in Midnight:
-- GetWorldElapsedTime(1), C_ChallengeMode, the scenario's criteria (the
-- weighted one is enemy forces, its percent in quantityString); a time the
-- game made secret (it may after a depleted key) is left out.
local run = { state = 0 }                 -- 0 none, 1 in a key (start 0 until the timer runs), 2 over
local function runElapsed()
    local ok, _, elapsed = pcall(GetWorldElapsedTime, 1)
    elapsed = ok and plain(elapsed)
    return type(elapsed) == "number" and elapsed or nil
end

-- pcall's values, all of them (try keeps the first): nothing if it failed.
local function all(f, ...)
    if type(f) ~= "function" then return end
    local r = { pcall(f, ...) }
    if r[1] then return unpack(r, 2) end
end

local function readRun()
    local now = GetTime()
    local map = C_ChallengeMode and plain(try(C_ChallengeMode.GetActiveChallengeMapID))
    if run.state ~= 2 then
        if not map then
            run.state = 0
            return
        end
        if run.map ~= map then        -- a new key (or a reload in one)
            local name, _, limit = all(C_ChallengeMode.GetMapUIInfo, map)
            run = { state = 1, map = map, name = plain(name), limit = plain(limit) or 0,
                    level = plain(try(C_ChallengeMode.GetActiveKeystoneInfo)) or 0, start = 0, at = {} }
        end
        local elapsed = runElapsed()
        -- The start in our clock, once the timer runs; kept unless it moved by over a second
        if elapsed and elapsed > 0 and (run.start == 0 or math.abs(now - elapsed - run.start) > 1.5) then
            run.start = now - elapsed
        end
    end
    local deaths, lost = all(C_ChallengeMode and C_ChallengeMode.GetDeathCount)
    run.deaths, run.lost = plain(deaths) or run.deaths or 0, plain(lost) or run.lost or 0
    local steps = C_Scenario and select(3, all(C_Scenario.GetStepInfo))
    run.bosses, run.forces = {}, run.forces or 0
    for i = 1, math.min(plain(steps) or 0, 12) do
        local c = C_ScenarioInfo and try(C_ScenarioInfo.GetCriteriaInfo, i)
        if type(c) == "table" then
            local done = plain(c.completed) and true or false
            if plain(c.isWeightedProgress) then
                local total, n = plain(c.totalQuantity), tonumber(((plain(c.quantityString) or ""):gsub("%%", ""):gsub(",", ".")))
                n = n or plain(c.quantity)
                if done then run.forces = 100
                elseif type(n) == "number" and type(total) == "number" and total > 0 then run.forces = n / total * 100 end
            else
                local name = plain(c.description)
                -- Blizzard's ✓ in front, and the " defeated" the criteria end with (English)
                name = type(name) == "string" and name:gsub("^\226\156\147%s*", ""):gsub("^%-%s*", "")
                    :gsub("%s+[Dd]efeated$", "") or ("Boss " .. i)
                -- When it fell: stamped the first time it reads as done while the timer is known
                if done and not run.at[i] and run.start > 0 and run.state == 1 then run.at[i] = now - run.start end
                run.bosses[#run.bosses + 1] = { name = name, done = done, at = done and run.at[i] or nil }
            end
        end
    end
end

local runEvents = CreateFrame("Frame")
runEvents:SetScript("OnEvent", function(_, event)
    if event == "CHALLENGE_MODE_RESET" then
        run = { state = 0 }
    elseif event == "CHALLENGE_MODE_COMPLETED" then
        readRun()
        local info = C_ChallengeMode.GetChallengeCompletionInfo and try(C_ChallengeMode.GetChallengeCompletionInfo)
        local ms = type(info) == "table" and plain(info.time)
        run.state, run.ended = 2, GetTime()
        run.final = (type(ms) == "number" and ms > 0) and ms / 1000 or (run.start > 0 and GetTime() - run.start) or 0
        run.onTime = type(info) == "table" and plain(info.onTime) and true or false
        run.upgrades = type(info) == "table" and plain(info.keystoneUpgradeLevels) or 0
    else
        if event == "CHALLENGE_MODE_START" then run = { state = 0 } end
        readRun()
    end
    linkDirty()
end)
for _, e in ipairs({ "CHALLENGE_MODE_START", "CHALLENGE_MODE_COMPLETED", "CHALLENGE_MODE_RESET",
                     "CHALLENGE_MODE_DEATH_COUNT_UPDATED", "SCENARIO_CRITERIA_UPDATE", "WORLD_STATE_TIMER_START",
                     "PLAYER_ENTERING_WORLD" }) do
    pcall(runEvents.RegisterEvent, runEvents, e)
end

local function runNow(now)
    if run.state == 2 and now - run.ended > 900 then run = { state = 0 } end
    if run.state == 1 and run.start == 0 then readRun() end      -- the countdown before the timer runs
    return run
end

local encEvents = CreateFrame("Frame")
encEvents:SetScript("OnEvent", function(_, event, _, name)
    encounterName = event == "ENCOUNTER_START" and plain(name) or nil
    linkDirty()
end)
pcall(encEvents.RegisterEvent, encEvents, "ENCOUNTER_START")
pcall(encEvents.RegisterEvent, encEvents, "ENCOUNTER_END")

-- The classes in the group, you included: {token = class id}. The buff check
-- asks for another class's raid buff only when that class is here to cast it.
local function groupClasses()
    local out = {}
    local function add(unit)
        local _, token, id = UnitClass(unit)
        token, id = plain(token), plain(id)
        if type(token) == "string" and type(id) == "number" then out[token] = id end
    end
    add("player")
    for _, unit in ipairs(groupUnits()) do add(unit) end
    return out
end

local slotsRaw, slotsHash = "", adler32("")

local function snapshot(full, result)
    local s = { v = 1, ver = VERSION, t = time(), sh = slotsHash, result = result,
                combat = InCombatLockdown() and true or false }
    s.lockdown = (C_ChatInfo and C_ChatInfo.InChatMessagingLockdown and try(C_ChatInfo.InChatMessagingLockdown)) or false
    s.aura_secret, s.pull_hidden = aurasSecret(), pullHiddenNow(GetTime())
    local name, realm = UnitFullName("player")
    local _, class = UnitClass("player")
    local spec = GetSpecialization and try(GetSpecialization)
    local _, equipped = GetAverageItemLevel()
    s.char = { name = name, realm = realm or GetNormalizedRealmName(), class = class, level = UnitLevel("player"),
               spec = spec and select(2, GetSpecializationInfo(spec)) or nil,
               ilvl = equipped and math.floor(equipped * 10) / 10 or nil }
    s.gold = GetMoney()
    s.bags = try(bagSummary)
    s.dur = try(durability)
    s.zone = GetRealZoneText()
    s.key = try(keystone)
    s.buffs = try(buffs) or {}
    local hasMH, mhExp, _, _, hasOH, ohExp = GetWeaponEnchantInfo()
    hasMH, mhExp, hasOH, ohExp = plain(hasMH), plain(mhExp), plain(hasOH), plain(ohExp)
    s.weapon = { mh = hasMH and math.floor((mhExp or 0) / 1000) or false,
                 oh = hasOH and math.floor((ohExp or 0) / 1000) or false }
    s.bars = barList()
    local now = GetTime()
    local rc = readyCheckNow(now)
    s.rc = { state = rc.state, rem = math.floor((rc.ends - now) * 10) / 10, total = rc.total, ready = rc.ready,
             missing = rc.missing }
    local r = runNow(now)
    if r.state > 0 then
        s.mplus = { state = r.state, map = r.map, name = r.name, level = r.level, limit = r.limit,
                    elapsed = r.state == 2 and r.final or (r.start > 0 and now - r.start or 0),
                    onTime = r.onTime, upgrades = r.upgrades, deaths = r.deaths, lost = r.lost,
                    forces = math.floor((r.forces or 0) * 10 + 0.5) / 10, bosses = r.bosses }
    end
    local soon, phase, enc = alertsNow(now)
    s.alerts = { phase = phase, enc = enc, list = {} }
    for i, a in ipairs(soon) do
        s.alerts.list[i] = { text = a.text, rem = math.floor((a.at - now) * 10) / 10, spell = a.spell }
    end
    s.group = { n = GetNumGroupMembers(), raid = IsInRaid() and true or false, classes = {} }
    for token in pairs(try(groupClasses) or {}) do s.group.classes[#s.group.classes + 1] = token end
    table.sort(s.group.classes)
    if full then
        s.items = try(scanItems, "bags")
        s.vault = try(vault)
    end
    return s
end

-- ── the live link ────────────────────────────────────────────────────────
-- The game's state, painted into one row of cells along the top edge of the
-- window, CELL physical pixels wide and one tall (a hairline: the least the
-- game can show and still be read), from a fifth of its width to four
-- fifths: overlays draw into the game's own image in the corners (Steam's FPS
-- counter covered v2.1.0's first cells, at 32 pixels, and nothing was read). The dashboard
-- (wow_addons/link.py) reads the row straight from the window several times a
-- second: no keys, no focus, in combat too. Only what an addon may read goes
-- in (nothing secret: plain() above), and the dashboard never presses a key
-- because of what it reads here.
--
-- A frame: CALIB cells in colours 0-7 (the dashboard learns them as this
-- screen draws them: gamma, brightness), then bytes as 3-bit cells, red green
-- blue each lit or dark, high bits first:
--   "TL" 4 seq(2) page(1) pages(1) tick(4) len(2) payload(len) fletcher16(2)
-- tick is GetTime() in whole seconds; times in the payload are tenths of
-- GetTime(), so a running timer leaves the strip as it is. A payload longer
-- than a row is cut into pages, one page a frame. `/tdc link off` hides it.
local LINK_FROM, LINK_SPAN, CELL, ROWS, CALIB, HEADER, PROTOCOL = 0.2, 0.6, 3, 1, 8, 13, 4
-- {kind, words in the buff's English name, its spell ids}. Midnight's augment
-- rune buff is called just "Void-Touched" (1264426).
local CONSUMABLES = { { 1, { "Flask", "Phial" } }, { 2, { "Well Fed" } }, { 3, { "Augment" }, { 1264426 } } }

local link = CreateFrame("Frame", "TouchDashboardLink")   -- no parent: the UI scale and Alt+Z leave it be
link:SetFrameStrata("TOOLTIP")
link:SetFrameLevel(9999)
link:Hide()                                                -- shown at login, unless switched off
local cells, painted = {}, {}
local count, content, seq, pages, page = 0, {}, 0, 1, 1
local dirty, builtAt, paintedTick, paintedSeq = true, -100, -1, -1
local alertsUntil = nil                   -- the next NSRT alert's time: the list moves on then
linkDirty = function() dirty = true end

local function u8(out, v) out[#out + 1] = math.floor(v or 0) % 256 end
local function uint(out, v, n)            -- big-endian, n bytes
    v = math.max(0, math.floor(v or 0))
    for i = n - 1, 0, -1 do out[#out + 1] = math.floor(v / 256 ^ i) % 256 end
end
local function str(out, s, max)           -- a length byte, then at most `max` (48) bytes, cut between characters
    s = type(s) == "string" and s or ""
    local n = math.min(#s, max or 48)
    while n > 0 and n < #s and s:byte(n + 1) >= 128 and s:byte(n + 1) < 192 do n = n - 1 end
    out[#out + 1] = n
    for i = 1, n do out[#out + 1] = s:byte(i) end
end
local function tenths(t) return (t and t > 0) and math.floor(t * 10 + 0.5) or 0 end

local function consumable(id, name)
    for _, k in ipairs(CONSUMABLES) do
        for _, sid in ipairs(k[3] or {}) do
            if sid == id then return k[1] end
        end
        for _, w in ipairs(type(name) == "string" and k[2] or {}) do
            if name:find(w, 1, true) then return k[1] end
        end
    end
    return 0
end

local function weaponEnd(has, ms, now)
    has, ms = plain(has), plain(ms)
    if not has or type(ms) ~= "number" then return 0 end
    return math.floor(now + ms / 1000 + 0.5) * 10          -- whole seconds: steady between reads
end

local function build()
    local s, out, now = snapshot(false), {}, GetTime()
    local c, bags, dur, key = s.char or {}, s.bags or {}, s.dur, s.key
    -- flags: 1 combat, 2 chat lockdown, 4 raid, 8 auras secret (16: the game said whether), 32 a hidden pull runs
    u8(out, (s.combat and 1 or 0) + (s.lockdown and 2 or 0) + (s.group.raid and 4 or 0) + (s.aura_secret and 8 or 0)
        + (s.aura_secret ~= nil and 16 or 0) + (s.pull_hidden and 32 or 0))
    u8(out, s.group.n)
    local mask = 0
    for _, id in pairs(try(groupClasses) or {}) do
        if id >= 1 and id <= 16 then mask = mask + 2 ^ (id - 1) end
    end
    uint(out, mask, 2)
    str(out, c.name); str(out, c.realm); str(out, c.class); str(out, c.spec)
    u8(out, c.level); uint(out, (c.ilvl or 0) * 10 + 0.5, 2)
    uint(out, s.gold, 6)
    uint(out, bags.free, 2); uint(out, bags.total, 2)
    u8(out, dur and dur.low or 255); u8(out, dur and dur.broken or 0)
    local hasMH, mhMs, _, _, hasOH, ohMs = GetWeaponEnchantInfo()
    uint(out, weaponEnd(hasMH, mhMs, now), 4); uint(out, weaponEnd(hasOH, ohMs, now), 4)
    u8(out, key and key.level or 0); uint(out, key and key.map or 0, 2); str(out, key and key.name)
    str(out, s.zone)
    uint(out, slotsHash, 4)
    str(out, VERSION)
    -- What the buff check uses: consumables, and buffs of ten minutes or more
    -- (raid buffs are an hour). Procs and passives would only make the row longer.
    local kept = {}
    for _, b in ipairs(s.buffs) do
        local kind = consumable(b.id, b.name)
        if (kind > 0 or b.dur >= 600) and #kept < 24 then kept[#kept + 1] = { b, kind } end
    end
    u8(out, #kept)
    for _, k in ipairs(kept) do
        local b, kind = k[1], k[2]
        uint(out, b.id, 3); uint(out, tenths(b.exp), 4)
        u8(out, kind)
        if kind > 0 then str(out, b.name) end
    end
    local running = {}
    for label, exp in pairs(bars) do
        if exp > now then running[#running + 1] = label end
    end
    table.sort(running, function(a, b) return bars[a] < bars[b] end)   -- keys: a name, or an unnamed bar's event
    local n = math.min(#running, 8)
    u8(out, n)
    for i = 1, n do
        local label = running[i]
        str(out, barName[label] or ""); uint(out, tenths(bars[label]), 4); uint(out, (barLen[label] or 0) * 10 + 0.5, 2)
    end
    -- The last ready check: running or how it ended, and who is not ready
    local rc = readyCheckNow(now)
    u8(out, rc.state); uint(out, tenths(rc.ends), 4)
    u8(out, rc.total); u8(out, rc.ready); u8(out, #rc.missing)
    for _, m in ipairs(rc.missing) do u8(out, m.a); str(out, m.name, 16) end
    -- NSRT's alerts still to come in this phase, the next eight
    local soon, phase, enc = alertsNow(now)
    alertsUntil = soon[1] and soon[1].at or nil
    u8(out, phase); str(out, enc, 32)
    u8(out, #soon)
    for _, a in ipairs(soon) do str(out, a.text, 40); uint(out, tenths(a.at), 4); uint(out, a.spell or 0, 3) end
    -- The Mythic+ run (state 0 ends it): its start in tenths of GetTime() so the
    -- strip stays as it is while the timer runs; over, the final time instead
    local r = runNow(now)
    u8(out, r.state)
    if r.state > 0 then
        uint(out, r.map, 2); str(out, r.name, 32); u8(out, r.level); uint(out, r.limit, 2)
        uint(out, r.state == 2 and math.floor(r.final * 10 + 0.5) or tenths(r.start), 4)
        u8(out, (r.onTime and 1 or 0) + 2 * math.min(r.upgrades or 0, 3))
        u8(out, r.deaths); uint(out, r.lost, 2); uint(out, (r.forces or 0) * 10 + 0.5, 2)
        u8(out, math.min(#r.bosses, 8))
        for i = 1, math.min(#r.bosses, 8) do
            local b = r.bosses[i]
            str(out, b.name, 32); u8(out, b.done and 1 or 0); uint(out, b.at or 0, 2)
        end
    end
    return out
end

local function capacity() return math.floor((count - CALIB) * 3 / 8) - HEADER - 2 end

local function layout()
    local w, h = GetPhysicalScreenSize()
    local px = 768 / (h * link:GetEffectiveScale())      -- one physical pixel in the strip's units
    count = math.max(0, math.floor(w * LINK_SPAN / CELL))
    link:ClearAllPoints()
    link:SetPoint("TOPLEFT", UIParent, "TOPLEFT", math.floor(w * LINK_FROM) * px, 0)
    link:SetSize(count * CELL * px, ROWS * px)
    for i = 1, count do
        local t = cells[i] or link:CreateTexture(nil, "OVERLAY")
        cells[i] = t
        t:ClearAllPoints()
        t:SetPoint("TOPLEFT", link, "TOPLEFT", (i - 1) * CELL * px, 0)
        t:SetSize(CELL * px, ROWS * px)
        t:Hide()
        painted[i] = nil
    end
    for i = count + 1, #cells do cells[i]:Hide(); painted[i] = nil end
    paintedSeq = -1
end

local function fletcher(bytes)
    local a, b = 0, 0
    for i = 1, #bytes do
        a = (a + bytes[i]) % 255
        b = (b + a) % 255
    end
    return b * 256 + a
end

local function symbols(p, tick)
    local cap = capacity()
    local from = (p - 1) * cap
    local len = math.max(0, math.min(cap, #content - from))
    local bytes = { 84, 76, PROTOCOL }                    -- "TL"
    uint(bytes, seq, 2); u8(bytes, p); u8(bytes, pages); uint(bytes, tick, 4); uint(bytes, len, 2)
    for i = 1, len do bytes[#bytes + 1] = content[from + i] end
    uint(bytes, fletcher(bytes), 2)
    local out = {}
    for i = 0, CALIB - 1 do out[#out + 1] = i end
    local acc, bits = 0, 0
    for i = 1, #bytes do
        acc, bits = acc * 256 + bytes[i], bits + 8
        while bits >= 3 do
            bits = bits - 3
            local d = 2 ^ bits
            out[#out + 1] = math.floor(acc / d)
            acc = acc % d
        end
    end
    if bits > 0 then out[#out + 1] = acc * 2 ^ (3 - bits) end
    return out
end

local function paint(syms)
    for i = 1, count do
        local v = syms[i]
        if v ~= painted[i] then
            local t = cells[i]
            if v then
                t:SetColorTexture(math.floor(v / 4) % 2, math.floor(v / 2) % 2, v % 2, 1)
                t:Show()
            else
                t:Hide()
            end
            painted[i] = v
        end
    end
end

local function same(a, b)
    if #a ~= #b then return false end
    for i = 1, #a do
        if a[i] ~= b[i] then return false end
    end
    return true
end

link:SetScript("OnUpdate", function()
    if count == 0 then layout() end
    if capacity() < 1 then return end
    local now = GetTime()
    if alertsUntil and now >= alertsUntil then dirty = true end
    if (dirty and now - builtAt >= 0.25) or now - builtAt >= 2 then   -- at most 4 times a second
        dirty, builtAt = false, now
        local new = try(build)
        if new and not same(new, content) then
            content, seq = new, (seq + 1) % 65536
        end
        pages = math.max(1, math.ceil(#content / capacity()))
    end
    local tick = math.floor(now)
    if pages > 1 or tick ~= paintedTick or seq ~= paintedSeq then
        page = page % pages + 1
        paintedTick, paintedSeq = tick, seq
        paint(symbols(page, tick))
    end
end)

local function linkOn() return not (TouchDashboardDB and TouchDashboardDB.link == false) end
local function setLink(on)
    TouchDashboardDB = TouchDashboardDB or {}
    TouchDashboardDB.link = on
    link:SetShown(on)
end

local linkEvents = CreateFrame("Frame")
linkEvents:SetScript("OnEvent", function(_, event)
    if event == "DISPLAY_SIZE_CHANGED" or event == "UI_SCALE_CHANGED" then count = 0 end
    dirty = true
end)
for _, e in ipairs({ "PLAYER_ENTERING_WORLD", "PLAYER_REGEN_DISABLED", "PLAYER_REGEN_ENABLED", "PLAYER_MONEY",
                     "BAG_UPDATE_DELAYED", "UPDATE_INVENTORY_DURABILITY", "PLAYER_EQUIPMENT_CHANGED",
                     "GROUP_ROSTER_UPDATE", "ZONE_CHANGED_NEW_AREA", "PLAYER_SPECIALIZATION_CHANGED",
                     "CHALLENGE_MODE_START", "CHALLENGE_MODE_COMPLETED", "DISPLAY_SIZE_CHANGED", "UI_SCALE_CHANGED",
                     "ADDON_RESTRICTION_STATE_CHANGED" }) do
    pcall(linkEvents.RegisterEvent, linkEvents, e)
end
pcall(linkEvents.RegisterUnitEvent, linkEvents, "UNIT_AURA", "player")
pcall(linkEvents.RegisterUnitEvent, linkEvents, "UNIT_INVENTORY_CHANGED", "player")

-- ── SavedVariables: each character as of its last save ───────────────────

local bankOpen, warbandOpen, saveQueued = false, false, false

local function save()
    saveQueued = false
    local name, realm = whoAmI()
    if not name then return end                     -- too early: saved again once the game knows
    TouchDashboardDB = TouchDashboardDB or {}
    local db = TouchDashboardDB
    db.v = 1
    db.chars = db.chars or {}
    db.chars[name .. "-"] = nil                     -- the realm-less copy v2.1.0 left
    local key = name .. "-" .. realm
    local c = db.chars[key] or {}
    db.chars[key] = c
    local _, class = UnitClass("player")
    local _, equipped = GetAverageItemLevel()
    c.name, c.realm, c.class, c.level = name, realm, class, UnitLevel("player")
    c.ilvl = (equipped and equipped > 0) and math.floor(equipped * 10) / 10 or c.ilvl
    c.gold = GetMoney()
    c.bags = try(bagSummary) or c.bags
    c.dur = try(durability) or c.dur
    c.key = try(keystone)
    c.vault = try(vault) or c.vault
    c.items = c.items or {}
    c.items.bags = try(scanItems, "bags") or c.items.bags
    if bankOpen then c.items.bank = try(scanItems, "bank") or c.items.bank end
    c.updated = time()
    if warbandOpen or C_Bank then
        db.warband = db.warband or {}
        if C_Bank and C_Bank.FetchDepositedMoney and Enum.BankType then
            local gold = try(C_Bank.FetchDepositedMoney, Enum.BankType.Account)
            if gold and (gold > 0 or warbandOpen) then db.warband.gold = gold end   -- 0 until the game has it
        end
        if warbandOpen then db.warband.items = try(scanItems, "warband") or db.warband.items end
        db.warband.updated = time()
    end
end

local function saveSoon()
    if not saveQueued then
        saveQueued = true
        C_Timer.After(2, save)
    end
end

-- ── key slots: secure buttons the dashboard's keys click ─────────────────

local owner = CreateFrame("Frame")
local buttons, pendingSlots = {}, nil

local focusButton = CreateFrame("Button", "TouchDashboardFocus", UIParent)

-- The run button: whatever the box loaded into it last (one command, its
-- lines), clicked by the PC's next chord, then emptied so a stray press runs
-- nothing. A real key press clicks it, so it may run what an addon may not.
local runButton = CreateFrame("Button", "TouchDashboardRun", UIParent, "SecureActionButtonTemplate")
runButton:SetAttribute("type", "macro")
runButton:RegisterForClicks(GetCVarBool("ActionButtonUseKeyDown") and "AnyDown" or "AnyUp")
runButton:HookScript("PostClick", function(self)
    if not InCombatLockdown() then self:SetAttribute("macrotext", nil) end
end)

-- Attributes cannot change in combat: then the PC types the command instead.
local function loadRun(text)
    if InCombatLockdown() then return "combat" end
    text = text:gsub("^%s+", ""):gsub("%s+$", "")
    if text == "" then return "unknown" end
    if #text > 255 then return "long" end       -- a macro's limit
    runButton:SetAttribute("macrotext", text)
    return "loaded"
end

local function slotButton(n)
    local b = buttons[n]
    if not b then
        b = CreateFrame("Button", "TouchDashboardSlot" .. n, UIParent, "SecureActionButtonTemplate")
        b:SetAttribute("type", "macro")
        b:RegisterForClicks(GetCVarBool("ActionButtonUseKeyDown") and "AnyDown" or "AnyUp")
        buttons[n] = b
    end
    return b
end

-- Bindings and attributes cannot change in combat: then it waits for its end.
local function applySlots(raw)
    if InCombatLockdown() then
        pendingSlots = raw
        return "queued"
    end
    pendingSlots = nil
    local texts = {}
    for digits, enc in raw:gmatch("(%d+)=([^;]*)") do
        local n = tonumber(digits)
        if n and n >= 1 and n <= SLOTS then texts[n] = unpct(enc):sub(1, 255) end
    end
    ClearOverrideBindings(owner)
    SetOverrideBindingClick(owner, true, FOCUS_KEY, "TouchDashboardFocus")
    SetOverrideBindingClick(owner, true, RUN_KEY, "TouchDashboardRun", "LeftButton")
    for n = 1, SLOTS do
        local text = texts[n]
        if text and text:find("%S") then
            local b = slotButton(n)
            b:SetAttribute("macrotext", text)
            -- only slots the dashboard fills: a macro the player bound to an empty one keeps working
            SetOverrideBindingClick(owner, true, SLOT_KEY:format(n), b:GetName(), "LeftButton")
        elseif buttons[n] then
            buttons[n]:SetAttribute("macrotext", nil)
        end
    end
    slotsRaw, slotsHash = raw, adler32(raw)
    TouchDashboardDB = TouchDashboardDB or {}
    TouchDashboardDB.slots = raw
    return "ok"
end

-- ── the input box ────────────────────────────────────────────────────────

local box = CreateFrame("EditBox", "TouchDashboardBox", UIParent)
box:SetSize(240, 20)
box:SetPoint("BOTTOMLEFT", UIParent, "TOPLEFT", 0, 200)   -- above the screen
box:SetFontObject(ChatFontNormal)
box:SetAutoFocus(false)
box:SetAlpha(0)
box:EnableMouse(false)
if box.SetMaxLetters then box:SetMaxLetters(0) end

-- ── the beacon ───────────────────────────────────────────────────────────
-- One row of solid cells, CELL_PX physical pixels each, on the window's top
-- edge just left of the live link's strip (not in the corner, where overlays
-- draw), above everything. wow_addons/screen.py looks for the same colours
-- (BEACON) along the edge.
local BEACON = { { 1, 0, 1 }, { 0, 1, 0 }, { 1, 1, 1 }, { 0, 0, 0 }, { 0, 1, 1 }, { 1, 1, 0 } }
local CELL_PX, BEACON_GAP = 4, 8
local beacon = CreateFrame("Frame", "TouchDashboardBeacon", UIParent)
beacon:SetFrameStrata("TOOLTIP")
beacon:SetFrameLevel(9999)
beacon:Hide()
local cells = {}
for i, c in ipairs(BEACON) do
    local t = beacon:CreateTexture(nil, "OVERLAY")
    t:SetColorTexture(c[1], c[2], c[3], 1)
    cells[i] = t
end

-- Sized when shown: the UI scale or the window may have changed since.
local function showBeacon()
    local w, h = GetPhysicalScreenSize()
    local px = 768 / (h * beacon:GetEffectiveScale())   -- one physical pixel in the beacon's units
    local x = math.floor(w * LINK_FROM) - BEACON_GAP - #cells * CELL_PX
    beacon:ClearAllPoints()
    beacon:SetPoint("TOPLEFT", UIParent, "TOPLEFT", x * px, 0)
    beacon:SetSize(#cells * CELL_PX * px, CELL_PX * px)
    for i, t in ipairs(cells) do
        t:ClearAllPoints()
        t:SetPoint("TOPLEFT", beacon, "TOPLEFT", (i - 1) * CELL_PX * px, 0)
        t:SetSize(CELL_PX * px, CELL_PX * px)
    end
    beacon:Show()
end

local phase, closeTimer = nil, nil

local function close()
    phase = nil
    if closeTimer then closeTimer:Cancel(); closeTimer = nil end
    box:SetText("")
    box:ClearFocus()
end

-- Never left holding the keyboard: closed after `seconds` whatever happens.
local function show(text, newPhase, seconds)
    phase = newPhase
    box:SetText(text)
    box:SetFocus()
    box:HighlightText()
    if closeTimer then closeTimer:Cancel() end
    closeTimer = C_Timer.NewTimer(seconds, close)
end

focusButton:SetScript("OnClick", function()
    show("TDS1 - " .. encode(snapshot(false)), "open", 1.5)
end)

-- An addon's slash command, never one of Blizzard's: those are secure or
-- protected, and the dashboard types them into chat.
local slashKeys = {}
local function findSlash(cmd)
    local key = slashKeys[cmd]
    if not key then
        for k in pairs(SlashCmdList) do
            for i = 1, 20 do
                local s = _G["SLASH_" .. k .. i]
                if type(s) ~= "string" then break end
                slashKeys[s:lower()] = k
            end
        end
        key = slashKeys[cmd]
    end
    if not key or issecurevariable(SlashCmdList, key) then return nil end
    return SlashCmdList[key]
end

-- NSRT's own test run of phase 1's reminders: they show in game, and the
-- dashboard's alert list follows them (NSRT_PHASE says it is a test).
local function nsrtTest()
    local ns = _G.NorthernSkyRaidTools
    if not (ns and type(ns.StartReminders) == "function") then return "unknown" end
    local ok, err = pcall(ns.StartReminders, ns, 1, true)
    if not ok then geterrorhandler()(err); return "error" end
    return "ok"
end

-- The dashboard put a newer version of this addon in the AddOns folder (it
-- keeps it up to date there): WoW loads it at the next /reload or login.
-- Said in chat, only to you, once a session; the dashboard's next exchange
-- with the box carries it (flag "n<version>").
local toldNewer = false
local function newerInstalled(version)
    if toldNewer or version == VERSION then return end
    toldNewer = true
    print(("|cff5db8a6Touch Dashboard|r %s is installed (this is %s): type /reload to use it."):format(version, VERSION))
end

-- A newer version of this addon is out, with a newer Touch Dashboard: the
-- dashboard updates itself, then this addon in the AddOns folder (flag
-- "u<version>", from the dashboard's own check for updates). Once a session.
local toldReleased = false
local function newerReleased(version)
    if toldReleased or version == VERSION then return end
    toldReleased = true
    print(("|cff5db8a6Touch Dashboard|r addon %s is out (this is %s): update Touch Dashboard on your PC, and it updates this addon."):format(version, VERSION))
end

local function tdc(msg)
    local verb, rest = msg:match("^(%S*)%s*(.-)$")
    if verb == "slots" then return applySlots(rest) end
    if verb == "link" and (rest == "on" or rest == "off") then setLink(rest == "on"); return "ok" end
    if verb == "nstest" then return nsrtTest() end
    if verb == "" or verb == "sync" or verb == "full" then return "ok" end
    return "unknown"
end

local function runLine(line)
    local cmd, msg = line:match("^(/%S+)%s*(.-)%s*$")
    if not cmd then return "unknown" end
    cmd = cmd:lower()
    if cmd == "/tdc" then return tdc(msg) end
    local fn = findSlash(cmd)
    if not fn then return "unknown" end
    local ok, err = pcall(fn, msg, box)
    if not ok then
        geterrorhandler()(err)
        return "error"
    end
    return "ok"
end

box:SetScript("OnEnterPressed", function(self)
    local nonce, flags, payload = (self:GetText() or ""):match("^TD1 (%x+) (%S+) ?(.*)$")
    if not nonce then return close() end
    payload = unpct(payload)
    local newer = flags:match("n([%d%.]+)")
    if newer then newerInstalled(newer) end
    local released = flags:match("u([%d%.]+)")
    if released then newerReleased(released) end
    local result = "ok"
    if flags:find("b", 1, true) then            -- for the run button, not run here
        result = loadRun(payload)
    else
        for line in (payload .. "\n"):gmatch("(.-)\n") do
            if line:find("%S") then
                local r = runLine(line)
                if r ~= "ok" then result = r end
            end
        end
    end
    if flags:find("r", 1, true) then
        show("TDS1 " .. nonce .. " " .. encode(snapshot(payload:find("^/tdc full") ~= nil, result)), "answer", 0.8)
    else
        close()
    end
end)
box:SetScript("OnEscapePressed", close)
box:SetScript("OnEditFocusGained", showBeacon)
box:SetScript("OnEditFocusLost", function(self)
    phase = nil
    beacon:Hide()
    self:SetText("")
end)
if box:HasScript("OnKeyDown") then
    box:SetScript("OnKeyDown", function(_, key)
        -- the answer has been copied: give the keyboard back at once
        if phase == "answer" and key == "C" and IsControlKeyDown() then C_Timer.After(0, close) end
    end)
end

-- ── events ───────────────────────────────────────────────────────────────

local events = CreateFrame("Frame")
events:SetScript("OnEvent", function(_, event, arg1)
    if event == "PLAYER_LOGIN" then
        TouchDashboardDB = TouchDashboardDB or {}
        link:SetShown(linkOn())
        hookBossMods()
        hookNsrt()
        applySlots(type(TouchDashboardDB.slots) == "string" and TouchDashboardDB.slots or "")
        saveSoon()
    elseif event == "PLAYER_REGEN_ENABLED" then
        if pendingSlots then applySlots(pendingSlots) end
    elseif event == "PLAYER_LOGOUT" then
        save()
    elseif event == "BANKFRAME_OPENED" then
        bankOpen = true
        saveSoon()
    elseif event == "BANKFRAME_CLOSED" then
        save()
        bankOpen, warbandOpen = false, false
    elseif event == "PLAYER_INTERACTION_MANAGER_FRAME_SHOW" or event == "PLAYER_INTERACTION_MANAGER_FRAME_HIDE" then
        local types = Enum.PlayerInteractionType or {}
        if arg1 == types.Banker or arg1 == types.AccountBanker then
            local open = event == "PLAYER_INTERACTION_MANAGER_FRAME_SHOW"
            if not open then save() end
            bankOpen = open
            warbandOpen = open
            if open then saveSoon() end
        end
    else
        saveSoon()
    end
end)
for _, e in ipairs({ "PLAYER_LOGIN", "PLAYER_LOGOUT", "PLAYER_REGEN_ENABLED", "PLAYER_MONEY",
                     "BAG_UPDATE_DELAYED", "UPDATE_INVENTORY_DURABILITY", "PLAYER_EQUIPMENT_CHANGED",
                     "WEEKLY_REWARDS_UPDATE", "CHALLENGE_MODE_COMPLETED", "BANKFRAME_OPENED", "BANKFRAME_CLOSED",
                     "PLAYER_INTERACTION_MANAGER_FRAME_SHOW", "PLAYER_INTERACTION_MANAGER_FRAME_HIDE" }) do
    pcall(events.RegisterEvent, events, e)   -- an event a client no longer has must not stop the rest
end

SLASH_TOUCHDASHBOARD1 = "/tdc"
SlashCmdList.TOUCHDASHBOARD = function(msg)
    local verb, rest = (msg or ""):match("^(%S*)%s*(.-)%s*$")
    if verb == "link" and (rest == "on" or rest == "off") then setLink(rest == "on") end
    if verb == "nstest" then nsrtTest(); return end
    local n = 0
    for _ in slotsRaw:gmatch("%d+=") do n = n + 1 end
    print(("|cff5db8a6Touch Dashboard|r %s: %d key slot%s loaded%s. The dashboard focuses its box with %s. Live link %s (/tdc link %s).")
        :format(VERSION, n, n == 1 and "" or "s", pendingSlots and " (more after combat)" or "", FOCUS_KEY,
                linkOn() and "on" or "off", linkOn() and "off" or "on"))
end
