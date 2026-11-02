/*
 * Northern Sky Raid Tools: the live note viewer plate, and two plates for its
 * encounter alerts as they come. NSRT's commands (Group split, Test alerts,
 * the hub, kick macro sync, Captain Zeynith) are keys like any other, in the
 * keys plates. The note comes parsed from the server (wow_nsrt_update): from
 * NSRT's SavedVariables, or pasted from the PC's clipboard. The alerts come
 * with the game's live state (wow_game_update): NSRT's schedule for the phase
 * that runs, read by the companion addon when the phase starts. Their
 * countdowns step once a second on this screen's clock.
 */
(function () {
    'use strict';

    const CATS = [['personal', 'Personal'], ['shared', 'Shared CDs'], ['notes', 'Notes']];
    const EMPTY = {
        personal: 'Nothing tagged for you in this note.',
        shared: 'No shared cooldowns in this note.',
        notes: 'No notes in this note.',
    };
    let ui = null;          // the viewer's elements
    let tab = 'personal';
    let note = null;        // the last wow_nsrt_update
    let drawn = '';

    function esc(v) { return escapeHtml(v); }

    function entryHtml(e, cat) {
        if (cat === 'notes') {
            return `<div class="wow-entry is-note" data-cat="notes"><span class="wow-entry-text">${esc(e.text)}</span></div>`;
        }
        const ph = e.phase ? `<span class="wow-ph">P${esc(e.phase)}</span>` : '';
        // The spell by name and icon once Battle.net has named it (Settings → Battle.net), else its id.
        const spell = e.spellName
            ? (e.icon ? `<img class="wow-spell-icon" src="${esc(e.icon)}" alt="" referrerpolicy="no-referrer">` : '') + esc(e.spellName)
            : e.spell ? `Spell ${esc(e.spell)}` : '';
        const meta = [spell, cat === 'shared' && e.tags.length ? esc(e.tags.join(', ')) : ''].filter(Boolean).join(' · ');
        return `<div class="wow-entry" data-cat="${cat}"><span class="wow-time">${esc(e.time)}${ph}</span>`
            + `<span class="wow-entry-text">${esc(e.text || '—')}</span>`
            + (meta ? `<span class="wow-entry-meta">${meta}</span>` : '') + '</div>';
    }

    function draw(api) {
        if (!ui) return;
        const g = api.general();
        const n = note;
        api.text(ui.name, n && n.encounterName ? n.encounterName : 'No note');
        const tag = [g.character, g.role].filter(Boolean).join(' · ');
        api.text(ui.sub, [n && n.difficulty, tag ? `Tag: ${tag}` : 'No character set'].filter(Boolean).join(' · '));
        for (const [cat] of CATS) {
            const pick = ui.tabs.querySelector(`[data-cat="${cat}"]`);
            pick.setAttribute('aria-checked', String(cat === tab));
            api.text(pick.querySelector('.wow-tab-n'), n ? n[cat].length : 0);
        }
        ui.well.dataset.cat = tab;

        // The picker: which of the notes found (or pasted) is shown.
        const choices = n ? n.choices : [];
        ui.chip.hidden = choices.length < 2;
        ui.chip.disabled = choices.length < 2;
        if (choices.length > 1) {
            const i = Math.max(0, choices.findIndex(c => c.id === n.active));
            api.text(ui.chip.querySelector('.chip-name'), `Note ${i + 1} of ${choices.length}`);
            ui.chip.setAttribute('aria-label', `Showing ${choices[i].name}, note ${i + 1} of ${choices.length}. Show the next one.`);
        }

        let html;
        if (!n || !n.active) {
            html = `<p class="wow-empty">No note yet. Load one in NSRT and /reload, or copy it from WoWUtils and tap Paste note.${api.admin() ? '' : ' The game folder is set in WoW settings on the PC.'}</p>`;
        } else if (!n[tab].length) {
            html = `<p class="wow-empty">${tab === 'personal' && !g.character && !g.role
                ? 'Set your character name or role tag in WoW settings (on the PC) to see your own reminders.'
                : EMPTY[tab]}</p>`;
        } else {
            html = n[tab].map(e => entryHtml(e, tab)).join('');
        }
        if (html !== drawn) {
            const sameList = drawn && ui.list.dataset.tab === tab && ui.list.dataset.note === (n && n.active || '');
            drawn = html;
            ui.list.innerHTML = html;
            ui.list.querySelectorAll('img').forEach(i => i.addEventListener('error', () => i.remove(), { once: true }));
            // Spell names arriving keep the reader's place; another note or tab starts at the top.
            if (!sameList) ui.list.scrollTop = 0;
            ui.list.dataset.tab = tab;
            ui.list.dataset.note = (n && n.active) || '';
        }
    }

    // Paste Note: the note copied on the PC (from WoWUtils, a Discord post) is
    // read from the PC's clipboard and parsed there, then every screen shows it.
    function pasteWoWUtilsNote(api, key) {
        api.send('nsrt_paste', null, key);
    }

    function renderViewer(sec, api, head) {
        head.innerHTML = '<button class="chip" type="button" data-act="pick" hidden disabled><span class="chip-name"></span></button>'
            + '<button class="btn wow-head-key" type="button" data-act="paste"><i data-lucide="clipboard-paste"></i>'
            + '<span class="wow-head-label">Paste note</span><i data-lucide="alert-triangle" class="key-warn"></i></button>';
        const body = document.createElement('div');
        body.style.display = 'contents';
        body.innerHTML = '<div class="scribble wow-banner"><span class="wow-banner-name">No note</span><span class="wow-banner-sub"></span></div>'
            + '<div class="picker is-segmented wow-tabs" role="radiogroup" aria-label="Reminders">'
            + CATS.map(([cat, label]) => `<button class="pick" type="button" role="radio" data-cat="${cat}" aria-checked="false">`
                + `<span class="wow-dot" aria-hidden="true"></span><span class="pick-text">${label}</span><span class="wow-tab-n num">0</span></button>`).join('')
            + '</div><div class="wow-well" data-cat="personal"><div class="wow-list" aria-live="polite"></div></div>';
        sec.appendChild(body);
        ui = {
            name: sec.querySelector('.wow-banner-name'), sub: sec.querySelector('.wow-banner-sub'),
            tabs: sec.querySelector('.wow-tabs'), well: sec.querySelector('.wow-well'),
            list: sec.querySelector('.wow-list'), chip: head.querySelector('[data-act="pick"]'),
        };
        drawn = '';
        ui.tabs.addEventListener('click', (e) => {
            const pick = e.target.closest('.pick');
            if (!pick || pick.dataset.cat === tab) return;
            tab = pick.dataset.cat;
            draw(api);
        });
        ui.chip.addEventListener('click', () => {
            if (!note || note.choices.length < 2) return;
            const i = note.choices.findIndex(c => c.id === note.active);
            api.send('nsrt_pick', { id: note.choices[(i + 1) % note.choices.length].id }, ui.chip);
        });
        const paste = head.querySelector('[data-act="paste"]');
        paste.addEventListener('click', () => pasteWoWUtilsNote(api, paste));
        draw(api);
    }

    // ── encounter alerts ─────────────────────────────────────────────────
    const MARK = /\{(star|circle|diamond|triangle|moon|square|cross|skull)\}/g;
    const MARK_GLYPH = { cross: 'x' };
    const al = { list: null, where: null, data: null, at: 0, key: '', tick: 0 };

    function clock(s) {
        s = Math.max(0, Math.ceil(s));
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    // {skull} and the like as the marker in its colour.
    function alertHtml(api, text) {
        return api.esc(text).replace(MARK, (_, m) => `<i data-lucide="${MARK_GLYPH[m] || m}" class="wow-alert-mark" data-mark="${m}"></i>`);
    }

    function syncAlerts(api) {
        const g = api.view('game');
        const live = g && g.live;
        const key = live ? JSON.stringify([live.age, live.alerts]) : '';
        if (key === al.key) return;
        al.key = key;
        al.at = performance.now();
        al.data = live && live.alerts && live.age < 600
            ? { ...live.alerts, link: live.link, list: live.alerts.list.map(a => ({ ...a, rem: a.rem - live.age })) } : null;
    }

    function upcoming() {
        if (!al.data) return [];
        const gone = (performance.now() - al.at) / 1000;
        return al.data.list.map(a => ({ ...a, left: a.rem - gone })).filter(a => a.left > 0);
    }

    function where() {
        const d = al.data;
        if (!d) return '';
        return d.test ? 'Test run' : [d.enc, d.phase ? `Phase ${d.phase}` : ''].filter(Boolean).join(' · ');
    }

    function idleText(api) {
        const g = api.view('game');
        if (!(g && g.live && g.live.link)) return 'Alerts come with the live link: the Touch Dashboard addon on, WoW running.';
        return al.data ? 'No more alerts this phase.' : 'NSRT’s alerts show here once an encounter starts. Test runs phase 1 now.';
    }

    // The list: the next alert large, the rest below it smaller, rebuilt when
    // the alerts change (el.shape: what it shows); between, only the
    // countdowns' text. A plate is drawn before the page places it. (The Next
    // alert plate is Raid & dungeons' now: raid.js, with the boss mods' bars.)
    function drawAlerts(api) {
        syncAlerts(api);
        const soon = upcoming();
        const shape = JSON.stringify([where(), soon.map(a => a.text), idleText(api)]);
        if (al.list) {
            if (shape !== al.list.shape) {
                al.list.shape = shape;
                al.list.innerHTML = soon.length
                    ? soon.map((a, i) => `<div class="wow-alert${i ? '' : ' is-next scribble'}"><span class="wow-alert-text">${alertHtml(api, a.text)}</span>`
                        + `<span class="wow-alert-left num${i ? '' : ' live'}"><span class="live-v"></span></span></div>`).join('')
                    : `<p class="wow-cell-note">${api.esc(idleText(api))}</p>`;
                api.icons(al.list);
            }
            al.list.querySelectorAll('.wow-alert .live-v').forEach((el, i) => api.text(el, clock(soon[i].left)));
            if (al.where) api.text(al.where, where());
        }
        clearTimeout(al.tick);
        if (soon.length) al.tick = setTimeout(() => drawAlerts(api), ((soon[0].left % 1) || 1) * 1000 + 15);
    }

    function testKey(head, api) {
        head.insertAdjacentHTML('beforeend', '<button class="btn wow-head-key" type="button"><i data-lucide="bell-ring"></i>'
            + '<span class="wow-head-label">Test</span><i data-lucide="alert-triangle" class="key-warn"></i></button>');
        const b = head.lastElementChild;
        b.setAttribute('aria-label', 'Run NSRT’s test of phase 1');
        b.addEventListener('click', () => api.send('nsrt_test', null, b));
        api.icons(head);
    }

    function renderAlerts(sec, api, head) {
        testKey(head, api);
        al.where = document.createElement('p');
        al.where.className = 'wow-alerts-where';
        al.list = document.createElement('div');
        al.list.className = 'wow-alerts';
        al.list.setAttribute('role', 'timer');
        sec.append(al.where, al.list);
        drawAlerts(api);
    }

    AddonModules.register({
        id: 'nsrt',
        name: 'Northern Sky Raid Tools',
        widgets: { note: { render: renderViewer }, alerts: { render: renderAlerts } },
        update(api) {
            api.onAck = (d) => (d.command === 'nsrt_paste' && d.reason === 'empty' ? "No note on the PC's clipboard" : null);
            draw(api);
            drawAlerts(api);
        },
        onUpdate(data, api) {
            note = data;
            draw(api);
        },
    });
})();
