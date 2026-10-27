/*
 * Northern Sky Raid Tools: the live note viewer (left), Group split and Test
 * alerts (action pad), NSRT hub, kick macro sync and the raid audit
 * (utilities). The note comes parsed from the server (wow_nsrt_update): from
 * NSRT's SavedVariables, or pasted from the PC's clipboard.
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

    const keys = [];     // the keys showing a command's text, kept current

    function commandKey(el, api, o) {
        const key = api.key({ glyph: o.glyph, label: o.label, value: o.value || api.slash(o.command) });
        key.addEventListener('click', () => (o.tap ? o.tap(key) : api.send(o.command, null, key)));
        key.refresh = () => key.setValue(o.value || api.slash(o.command));
        el.appendChild(key);
        for (let i = keys.length - 1; i >= 0; i--) if (!keys[i].isConnected) keys.splice(i, 1);
        keys.push(key);
    }

    AddonModules.register({
        id: 'nsrt',
        name: 'Northern Sky Raid Tools',
        commands: ['open_nsrt', 'nkm_refresh', 'nsrt_test', 'nsrt_arrange', 'cz_check', 'cz_announce', 'nsrt_paste', 'nsrt_pick'],
        columns: [
            { column: 'left', order: 10, title: 'NSRT note', grow: true, render: renderViewer },
            { slot: 'bottom', order: 10, render: (el, api) => commandKey(el, api, { command: 'nsrt_arrange', glyph: 'split', label: 'Group split' }) },
            { slot: 'bottom', order: 20, render: (el, api) => commandKey(el, api, { command: 'nsrt_test', glyph: 'bell-ring', label: 'Test alerts' }) },
            { slot: 'grid', order: 10, render: (el, api) => commandKey(el, api, { command: 'open_nsrt', glyph: 'layout-grid', label: 'NSRT hub' }) },
            { slot: 'grid', order: 20, render: (el, api) => commandKey(el, api, { command: 'nkm_refresh', glyph: 'refresh-cw', label: 'Sync kick macro' }) },
            {
                // BigWigs' version check rides along when BigWigs is on.
                slot: 'grid', order: 30, render: (el, api) => commandKey(el, api, {
                    command: 'cz_check', glyph: 'shield-check', label: 'Raid audit',
                    get value() { return [api.slash('cz_check'), api.enabled('bigwigs') ? api.slash('bw_version') : ''].filter(Boolean).join(' · '); },
                    tap: (key) => {
                        api.send('cz_check', null, key);
                        if (api.enabled('bigwigs')) api.send('bw_version', null, key);
                    },
                }),
            },
        ],
        update(api) {
            keys.forEach(k => k.refresh());
            api.onAck = (d) => (d.command === 'nsrt_paste' && d.reason === 'empty' ? "No note on the PC's clipboard" : null);
            draw(api);
        },
        onUpdate(data, api) {
            note = data;
            draw(api);
        },
    });
})();
