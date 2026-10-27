/*
 * Character (centre, above the action pad): the character the PC read from
 * Raider.IO and Battle.net (wow_character_update): avatar, spec, item level,
 * Mythic+ score, progress in the raids open now, and this week's affixes.
 * Set in Settings → Battle.net on the PC; Refresh asks the PC to read it again.
 */
(function () {
    'use strict';

    const REASONS = {
        not_found: 'Raider.IO and Battle.net do not know this character.',
        refused: 'Battle.net refused the API key.',
        busy: 'Rate limited; the PC tries again in a few minutes.',
        offline: 'Raider.IO and Battle.net could not be reached.',
        failed: 'The PC could not read the character.',
    };
    let ui = null, data = null, drawnAffixes = '';

    function render(sec, api, head) {
        head.innerHTML = '<button class="btn wow-head-key" type="button"><i data-lucide="refresh-cw"></i>'
            + '<span class="wow-head-label">Refresh</span><i data-lucide="alert-triangle" class="key-warn"></i></button>';
        const refresh = head.firstElementChild;
        refresh.setAttribute('aria-label', 'Read the character again');
        refresh.addEventListener('click', () => api.send('char_refresh', null, refresh));
        const body = document.createElement('div');
        body.className = 'wow-char';
        body.innerHTML = '<div class="wow-char-top">'
            + '<span class="wow-char-pic"><img alt="" referrerpolicy="no-referrer" hidden><i data-lucide="user"></i></span>'
            + '<div class="wow-char-id"><div class="wow-char-name">No character</div><div class="wow-char-sub"></div><div class="wow-char-sub"></div></div></div>'
            + '<div class="wow-char-stats">'
            + '<div class="scribble"><span class="ch-name">Item level</span><span class="live num wow-char-fig"><span class="live-v">—</span></span></div>'
            + '<div class="scribble"><span class="ch-name">M+ score</span><span class="live num wow-char-fig"><span class="live-v">—</span></span></div>'
            + '<div class="scribble"><span class="ch-name">Raid</span><span class="live num wow-char-fig"><span class="live-v">—</span></span></div>'
            + '</div>'
            + '<p class="wow-char-msg" hidden></p>'
            + '<div class="wow-affixes"><span class="wow-affix-label">This week</span><div class="wow-affix-list"></div></div>';
        sec.appendChild(body);
        const subs = body.querySelectorAll('.wow-char-sub');
        const cells = body.querySelectorAll('.wow-char-stats .scribble');
        ui = {
            img: body.querySelector('.wow-char-pic img'), name: body.querySelector('.wow-char-name'),
            sub: subs[0], sub2: subs[1], msg: body.querySelector('.wow-char-msg'),
            figs: [...cells].map(c => c.querySelector('.live-v')), raidName: cells[2].querySelector('.ch-name'),
            affixes: body.querySelector('.wow-affixes'), list: body.querySelector('.wow-affix-list'),
        };
        ui.img.addEventListener('error', () => { ui.img.hidden = true; });
        drawnAffixes = '';
        draw(api);
    }

    function setMsg(api, text, warn) {
        ui.msg.hidden = !text;
        ui.msg.dataset.tone = warn ? 'warn' : '';
        api.text(ui.msg, text || '');
    }

    function draw(api) {
        if (!ui) return;
        const d = data || { state: 'unset', character: null, affixes: [] };
        const c = d.character;
        const g = api.general();
        if (c) {
            api.text(ui.name, c.name || g.character);
            api.text(ui.sub, [[c.spec, c.class].filter(Boolean).join(' '), c.level ? `Level ${c.level}` : ''].filter(Boolean).join(' · '));
            api.text(ui.sub2, [c.realm, (c.region || '').toUpperCase(), c.guild ? `<${c.guild}>` : ''].filter(Boolean).join(' · '));
            api.text(ui.figs[0], c.ilvl != null ? String(Math.round(c.ilvl)) : '—');
            api.text(ui.figs[1], c.score != null ? String(c.score) : '—');
            const raid = (c.raids || [])[0];
            api.text(ui.raidName, raid ? raid.name : 'Raid');
            api.text(ui.figs[2], raid && raid.summary ? raid.summary : '—');
            if (c.avatar && ui.img.getAttribute('src') !== c.avatar) { ui.img.hidden = false; ui.img.src = c.avatar; }
            if (!c.avatar) ui.img.hidden = true;
            const more = (c.raids || []).slice(1).map(r => `${r.name} ${r.summary || ''}`.trim());
            if (d.stale) setMsg(api, `Not refreshed: ${REASONS[d.reason] || REASONS.failed}`, true);
            else setMsg(api, more.length ? `Also open: ${more.join(' · ')}` : '');
        } else {
            api.text(ui.name, d.state === 'unset' ? 'No character' : g.character || 'No character');
            api.text(ui.sub, d.state === 'unset'
                ? (api.admin() ? 'Find yours in Settings → Battle.net.' : 'Set it in Settings → Battle.net on the PC.')
                : d.state === 'loading' ? 'Looking it up…' : '');
            api.text(ui.sub2, '');
            ui.figs.forEach(f => api.text(f, '—'));
            api.text(ui.raidName, 'Raid');
            ui.img.hidden = true;
            setMsg(api, d.state === 'error' ? REASONS[d.reason] || REASONS.failed : '', d.state === 'error');
        }
        const affixes = d.affixes || [];
        const key = JSON.stringify(affixes);
        if (key !== drawnAffixes) {
            drawnAffixes = key;
            ui.list.innerHTML = affixes.map(a => `<span class="wow-affix">${a.icon ? `<img src="${api.esc(a.icon)}" alt="" referrerpolicy="no-referrer">` : ''}`
                + `<span class="wow-affix-name">${api.esc(a.name)}</span></span>`).join('');
            ui.list.querySelectorAll('img').forEach(i => i.addEventListener('error', () => i.remove(), { once: true }));
        }
        ui.affixes.hidden = !affixes.length;
    }

    AddonModules.register({
        id: 'char',
        name: 'Character',
        commands: ['char_refresh'],
        columns: [{ column: 'center', order: 40, title: 'Character', render }],
        update(api) {
            api.onAck = (d) => (d.command === 'char_refresh' && d.reason === 'empty' ? 'No character set: Settings → Battle.net' : null);
            draw(api);
        },
        onUpdate(d, api) {
            data = d;
            draw(api);
        },
    });
})();
