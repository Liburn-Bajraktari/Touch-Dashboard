/*
 * The game as the Touch Dashboard addon shows it (wow_game_update): the buff
 * check, gold, bags with a search through every character's items, keystone
 * and Great Vault, and the characters the addon has seen. Live figures come
 * from its live link (read from WoW's window ten times a second, on every
 * change in game) or else its last answer (a key sent through it, or Read);
 * the others from its SavedVariables, as of the character's last /reload or
 * logout.
 */
(function () {
    'use strict';

    const GLYPHS = {
        intellect: 'sparkles', fortitude: 'shield-plus', shout: 'megaphone', wild: 'leaf', bronze: 'hourglass',
        skyfury: 'cloud-lightning', flask: 'flask-conical', food: 'utensils', rune: 'gem', weapon: 'droplet',
    };
    const VAULT = [['raid', 'Raid'], ['dungeons', 'Dungeons'], ['world', 'World']];
    let data = null;
    let gotAt = 0;          // when `data` came, on this screen's clock
    const ui = {};

    const fmt = (n) => (n == null ? '—' : Math.round(n).toLocaleString());
    function ago(seconds) {
        if (seconds == null) return '';
        if (seconds < 60) return 'just now';
        if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
        if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
        return `${Math.round(seconds / 86400)} d ago`;
    }
    const sinceEpoch = (t) => (t ? Math.max(0, Date.now() / 1000 - t) : null);
    function left(rem) {
        if (rem == null || rem < 0) return 'Ready';
        return rem >= 3600 ? `${Math.floor(rem / 3600)} h ${Math.round((rem % 3600) / 60)} min` : `${Math.max(1, Math.round(rem / 60))} min`;
    }

    // A time left as of now: the view's figure less its age when sent and the
    // time since it came. The PC sends the view only when something changes in
    // game, so the buff check counts its times down itself.
    function remNow(rem) {
        if (rem == null || rem < 0 || !data || !data.live) return rem;
        return Math.max(0, rem - (data.live.age || 0) - (performance.now() - gotAt) / 1000);
    }

    // Why there are no live figures, in a line.
    function status(api) {
        const cs = (data && data.companion) || api.companion();
        if (!cs.installed) return api.admin() ? 'Install the Touch Dashboard addon: WoW settings → General.' : 'Needs the Touch Dashboard addon on the PC (WoW settings).';
        if (cs.enabled === false) return 'The addon is switched off in WoW settings.';
        const live = data && data.live;
        if (!live) return 'Nothing from the game yet: log in with the addon on, or tap Read with WoW in front.';
        const when = live.link ? 'live' : `read ${ago(live.age)}`;
        if (live.combat || live.lockdown) return `In combat the game keeps buffs secret · ${when}`;
        if (live.mplus && live.mplus.state === 'running') return `In a Mythic+ key the game keeps buffs secret · ${when}`;
        return live.link ? 'Live from the game' : `From the game · ${ago(live.age)}`;
    }

    function readKey(head, api) {
        head.innerHTML = '<button class="btn wow-head-key" type="button"><i data-lucide="refresh-ccw"></i>'
            + '<span class="wow-head-label">Read</span><i data-lucide="alert-triangle" class="key-warn"></i></button>';
        const b = head.firstElementChild;
        b.setAttribute('aria-label', 'Read the game now');
        b.addEventListener('click', () => api.send('game_sync', null, b));
        api.icons(head);
    }

    function current() {
        return data ? (data.chars || []).find(c => c.current) || null : null;
    }

    // ── the buff check ───────────────────────────────────────────────────
    function renderBuffs(sec, api, head) {
        readKey(head, api);
        ui.buffs = document.createElement('div');
        ui.buffs.className = 'wow-buffs';
        ui.buffs.setAttribute('role', 'group');
        ui.buffs.setAttribute('aria-label', 'Buffs');
        ui.buffs.addEventListener('click', (e) => {
            const b = e.target.closest('.wow-buff[data-cast]');
            if (b) api.send(b.dataset.cast, null, b);
        });
        ui.buffsNote = document.createElement('p');
        ui.buffsNote.className = 'wow-cell-note';
        sec.append(ui.buffs, ui.buffsNote);
        ui.buffsDrawn = '';
        drawBuffs(api);
    }

    function drawBuffs(api) {
        if (!ui.buffs || !ui.buffs.isConnected) return;
        const live = data && data.live;
        const checks = (live ? live.checks : []).map(c => ({ ...c, rem: remNow(c.rem) }));
        const key = JSON.stringify(checks.map(c => [c.id, c.ok, c.hidden, c.cast, c.label, left(c.rem)]));
        if (key !== ui.buffsDrawn) {
            ui.buffsDrawn = key;
            ui.buffs.innerHTML = checks.map(c => {
                // While the game hides auras, one not seen is hidden, not missing
                const cast = !c.ok && !c.hidden && c.cast;
                const word = c.ok ? left(c.rem) : (c.hidden ? 'Hidden' : (cast ? 'Cast' : 'Missing'));
                return `<${cast ? 'button' : 'div'} class="${cast ? 'btn ' : ''}wow-buff" ${cast ? `type="button" data-cast="${api.esc(c.cast)}"` : 'role="img"'} data-state="${c.ok ? 'on' : (c.hidden ? 'hidden' : 'off')}" aria-label="${api.esc(`${c.name}: ${cast ? 'missing, tap to cast' : word.toLowerCase()}`)}">`
                    + `<span class="key-glyph"><i data-lucide="${GLYPHS[c.id] || 'circle'}"></i>${KEY_WARN}</span><span class="wow-buff-name">${api.esc(c.label || c.name)}</span>`
                    + `<span class="wow-buff-state"><span class="wow-buff-led" aria-hidden="true"></span><span class="wow-buff-word">${word}</span></span></${cast ? 'button' : 'div'}>`;
            }).join('');
            api.icons(ui.buffs);
        }
        ui.buffs.hidden = !checks.length;
        api.text(ui.buffsNote, status(api));
        // Again when the soonest figure changes: minutes round at the half minute.
        clearTimeout(ui.buffTick);
        const next = Math.min(...checks.filter(c => c.ok && c.rem > 0).map(c => (((c.rem - 30) % 60) + 60) % 60 || 60));
        if (Number.isFinite(next)) ui.buffTick = setTimeout(() => drawBuffs(api), next * 1000 + 50);
    }

    // ── gold ─────────────────────────────────────────────────────────────
    function renderWallet(sec, api) {
        sec.insertAdjacentHTML('beforeend', '<div class="wow-wallet">'
            + '<div class="scribble wow-gold-main"><span class="ch-name">This character</span><span class="live num wow-gold-fig"><span class="live-v">—</span></span></div>'
            + '<div class="wow-gold-lines"><div class="wow-gold-line"><span>All characters</span><span class="num" data-f="all">—</span></div>'
            + '<div class="wow-gold-line"><span>Warband bank</span><span class="num" data-f="warband">—</span></div></div>'
            + '<p class="wow-cell-note"></p></div>');
        ui.wallet = sec.querySelector('.wow-wallet');
        drawWallet(api);
    }

    function drawWallet(api) {
        if (!ui.wallet || !ui.wallet.isConnected) return;
        const me = current();
        const t = (data && data.totals) || {};
        api.text(ui.wallet.querySelector('.live-v'), me && me.gold != null ? `${fmt(me.gold)} g` : '—');
        api.text(ui.wallet.querySelector('[data-f="all"]'), t.chars ? `${fmt(t.gold)} g` : '—');
        api.text(ui.wallet.querySelector('[data-f="warband"]'), t.warband != null ? `${fmt(t.warband)} g` : '—');
        api.text(ui.wallet.querySelector('.wow-cell-note'), me ? (me.live ? status(api) : `As of ${me.name}’s last logout · ${ago(sinceEpoch(me.updated))}`) : status(api));
    }

    // ── bags ─────────────────────────────────────────────────────────────
    function renderBags(sec, api) {
        sec.insertAdjacentHTML('beforeend', '<div class="wow-bags">'
            + '<div class="wow-bag-figs">'
            + '<div class="scribble"><span class="ch-name">Free slots</span><span class="live num"><span class="live-v" data-f="free">—</span></span></div>'
            + '<div class="scribble" data-f="durbox"><span class="ch-name">Durability</span><span class="live num"><span class="live-v" data-f="dur">—</span></span></div>'
            + '</div>'
            + '<input type="search" class="wow-bag-search" placeholder="Find an item on any character" aria-label="Find an item" autocomplete="off" spellcheck="false">'
            + '<div class="wow-bag-found" aria-live="polite"></div></div>');
        ui.bags = sec.querySelector('.wow-bags');
        const input = ui.bags.querySelector('.wow-bag-search');
        let timer = 0, asked = 0;
        input.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                const q = input.value.trim();
                const n = ++asked;
                const box = ui.bags.querySelector('.wow-bag-found');
                if (!q) { box.replaceChildren(); return; }
                api.request('game_find', { q }).then((r) => {
                    if (n !== asked) return;
                    box.innerHTML = r.items.length ? r.items.map(it => '<div class="wow-found">'
                        + `<span class="wow-found-name">${api.esc(it.name || `Item ${it.id}`)}</span><span class="wow-found-n num">${fmt(it.n)}</span>`
                        + `<span class="wow-found-where">${it.where.map(w => api.esc(`${w.who} ${w.loc} ${w.n}`)).join(' · ')}</span></div>`).join('')
                        + (r.more ? `<p class="wow-cell-note">${r.more} more: narrow the search</p>` : '')
                        : `<p class="wow-cell-note">Nothing called “${api.esc(q)}” in the bags the addon has seen. Banks count once you open them.</p>`;
                }, (reason) => {
                    if (n === asked) box.innerHTML = `<p class="wow-cell-note">Not searched (${api.esc(reason)}).</p>`;
                });
            }, 250);
        });
        drawBags(api);
    }

    function drawBags(api) {
        if (!ui.bags || !ui.bags.isConnected) return;
        const me = current();
        const live = data && data.live;
        const bags = me && me.bags;
        api.text(ui.bags.querySelector('[data-f="free"]'), bags && bags.total ? `${bags.free} / ${bags.total}` : '—');
        const dur = live && live.dur ? live.dur.low : me && me.dur;
        api.text(ui.bags.querySelector('[data-f="dur"]'), dur != null ? `${dur} %${live && live.dur && live.dur.broken ? ` · ${live.dur.broken} broken` : ''}` : '—');
        ui.bags.querySelector('[data-f="durbox"]').dataset.tone = dur != null && dur <= 25 ? 'warn' : '';
    }

    // ── keystone and vault ───────────────────────────────────────────────
    function renderKeystone(sec, api) {
        ui.key = document.createElement('div');
        ui.key.className = 'wow-keystone';
        sec.appendChild(ui.key);
        drawKeystone(api);
    }

    function drawKeystone(api) {
        if (!ui.key || !ui.key.isConnected) return;
        const me = current();
        const ks = me && me.keystone;
        const v = (me && me.vault) || {};
        const others = ((data && data.chars) || []).filter(c => !c.current && c.keystone);
        ui.key.innerHTML = `<div class="scribble"><span class="ch-name">Keystone</span><span class="live num"><span class="live-v">${ks ? api.esc(`+${ks.level} ${ks.name || ''}`) : '—'}</span></span></div>`
            + '<div class="wow-vault">' + VAULT.map(([k, name]) => `<div class="wow-vault-row"><span>${name}</span><span class="wow-vault-pips" data-n="${v[k] || 0}">`
                + [1, 2, 3].map(n => `<span class="wow-pip${(v[k] || 0) >= n ? ' is-on' : ''}"></span>`).join('') + '</span></div>').join('') + '</div>'
            + (others.length ? '<div class="wow-key-alts">' + others.slice(0, 8).map(c => `<span>${api.esc(c.name)} <span class="num">+${c.keystone.level}</span> ${api.esc(c.keystone.name || '')}</span>`).join('') + '</div>' : '')
            + (me ? '' : `<p class="wow-cell-note">${api.esc(status(api))}</p>`);
    }

    // ── the characters ───────────────────────────────────────────────────
    function renderAlts(sec, api) {
        ui.alts = document.createElement('div');
        ui.alts.className = 'wow-alts';
        sec.appendChild(ui.alts);
        drawAlts(api);
    }

    function drawAlts(api) {
        if (!ui.alts || !ui.alts.isConnected) return;
        const chars = (data && data.chars) || [];
        if (!chars.length) {
            ui.alts.innerHTML = `<p class="wow-cell-note">${api.esc(api.companion().installed ? 'The addon lists each character once you log in with it: /reload or log out to save.' : status(api))}</p>`;
            return;
        }
        ui.alts.innerHTML = '<div class="wow-alt is-head"><span>Character</span><span>Item level</span><span>Gold</span><span>Key</span><span>Vault</span></div>'
            + chars.map(c => `<div class="wow-alt${c.current ? ' is-current' : ''}">`
                + `<span class="wow-alt-name">${api.esc(c.name)}<span class="wow-alt-sub">${api.esc([c.level ? `${c.level}` : '', c.realm || ''].filter(Boolean).join(' · '))}</span></span>`
                + `<span class="num">${c.ilvl != null ? Math.round(c.ilvl) : '—'}</span><span class="num">${c.gold != null ? fmt(c.gold) : '—'}</span>`
                + `<span class="num">${c.keystone ? `+${c.keystone.level}` : '—'}</span>`
                + `<span class="num">${Object.values(c.vault || {}).reduce((a, b) => a + b, 0)} / 9</span></div>`).join('');
    }

    function drawAll(api) {
        drawBuffs(api);
        drawWallet(api);
        drawBags(api);
        drawKeystone(api);
        drawAlts(api);
    }

    AddonModules.register({
        id: 'game',
        name: 'Game state',
        widgets: {
            buffs: { render: renderBuffs },
            wallet: { render: renderWallet },
            bags: { render: renderBags },
            keystone: { render: renderKeystone },
            alts: { render: renderAlts },
        },
        update(api) {
            api.onAck = (d) => {
                if (d.command !== 'game_sync') return null;
                if (d.status === 'sent') return 'Read · up to date';
                return `Not read · ${{
                    no_answer: 'the addon opened its box but gave nothing back',
                    no_box: 'the addon did not open its box: is it on in game? (/tdc)',
                    not_front: 'WoW is not the window in front on the PC',
                    no_check: 'the PC cannot see WoW’s window to check the addon',
                    no_companion: 'the Touch Dashboard addon is not installed',
                }[d.reason] || 'the PC could not do it'}`;
            };
            drawAll(api);
        },
        onUpdate(d, api) {
            data = d;
            gotAt = performance.now();
            drawAll(api);
        },
    });
})();
