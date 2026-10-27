/*
 * EllesmereUI: Unlock mode and Profile keys (action pad, top) and the buff
 * check (left). WoW gives the PC no live view of buffs, so the strip is a
 * checklist every screen shares; Arcane Intellect is cast from its pill.
 */
(function () {
    'use strict';

    const BUFFS = [
        ['flask', 'Flask', 'flask-conical'],
        ['food', 'Food', 'utensils'],
        ['oil', 'Weapon oil', 'droplet'],
        ['intellect', 'Arcane Intellect', 'sparkles'],
    ];
    let unlockKey = null, profileKey = null, strip = null;
    let profilesEl = null, draft = [];

    function nextProfile(api) {
        const list = api.settings().profiles || [];
        if (!list.length) return null;
        const i = list.indexOf(api.state().profile);
        return list[(i + 1) % list.length];
    }

    function draw(api) {
        const st = api.state();
        if (unlockKey) {
            unlockKey.setOn(st.unlocked);
            unlockKey.setValue(st.unlocked ? 'Unlocked' : 'Locked');
        }
        if (profileKey) {
            const list = api.settings().profiles || [];
            profileKey.disabled = !list.length;
            profileKey.setValue(list.length ? (st.profile || `Next: ${nextProfile(api)}`) : 'No profiles');
        }
        if (strip) {
            const buffs = st.buffs || {};
            strip.querySelectorAll('.wow-buff').forEach(b => {
                const on = !!buffs[b.dataset.buff];
                b.dataset.state = on ? 'on' : 'off';
                b.setAttribute('aria-pressed', String(on));
                api.text(b.querySelector('.wow-buff-word'), on ? 'Ready' : 'Missing');
            });
        }
    }

    function renderProfiles(api) {
        profilesEl.innerHTML = draft.length
            ? draft.map((p, i) => `<span class="wow-profile">${api.esc(p)}<button type="button" data-i="${i}" aria-label="Remove ${api.esc(p)}"><i data-lucide="x"></i></button></span>`).join('')
            : '<span class="settings-sub">No profiles yet.</span>';
        api.icons(profilesEl);
    }

    AddonModules.register({
        id: 'ellesmere',
        name: 'EllesmereUI',
        commands: ['eui_unlock', 'eui_profile', 'cast_intellect', 'eui_buff', 'eui_buffs_clear'],
        columns: [
            {
                slot: 'top', order: 30, render(el, api) {
                    unlockKey = api.key({ glyph: 'unlock', label: 'Unlock mode', value: 'Locked' });
                    unlockKey.addEventListener('click', () => api.send('eui_unlock', null, unlockKey));
                    el.appendChild(unlockKey);
                    draw(api);
                },
            },
            {
                slot: 'top', order: 40, render(el, api) {
                    profileKey = api.key({ glyph: 'layers', label: 'Profile', value: '' });
                    profileKey.addEventListener('click', () => {
                        const name = nextProfile(api);
                        if (name) api.send('eui_profile', { name }, profileKey);
                    });
                    el.appendChild(profileKey);
                    draw(api);
                },
            },
            {
                column: 'left', order: 20, title: 'Buff check', render(sec, api, head) {
                    head.innerHTML = '<button class="btn wow-head-key" type="button"><i data-lucide="eraser"></i>'
                        + '<span class="wow-head-label">Clear</span><i data-lucide="alert-triangle" class="key-warn"></i></button>';
                    const clear = head.firstElementChild;
                    clear.setAttribute('aria-label', 'Clear the buff check');
                    clear.addEventListener('click', () => api.send('eui_buffs_clear', null, clear));
                    strip = document.createElement('div');
                    strip.className = 'wow-buffs';
                    strip.setAttribute('role', 'group');
                    strip.setAttribute('aria-label', 'Buffs');
                    strip.innerHTML = BUFFS.map(([id, name, glyph]) => `<button class="btn wow-buff" type="button" data-buff="${id}" data-state="off" aria-pressed="false">`
                        + `<span class="key-glyph"><i data-lucide="${glyph}"></i>${KEY_WARN}</span><span class="wow-buff-name">${name}</span>`
                        + '<span class="wow-buff-state"><span class="wow-buff-led" aria-hidden="true"></span><span class="wow-buff-word">Missing</span></span></button>').join('');
                    strip.addEventListener('click', (e) => {
                        const b = e.target.closest('.wow-buff');
                        if (!b) return;
                        const on = b.dataset.state === 'on';
                        // A missing Arcane Intellect is cast; the rest are ticked off.
                        if (b.dataset.buff === 'intellect' && !on) api.send('cast_intellect', null, b);
                        else api.send('eui_buff', { buff: b.dataset.buff, on: !on }, b);
                    });
                    sec.appendChild(strip);
                    draw(api);
                },
            },
        ],
        update: draw,
        settings: {
            render(el, api) {
                draft = [...(api.settings().profiles || [])];
                el.innerHTML = '<div class="form-group"><span class="form-label">Profiles the Profile key steps through</span>'
                    + '<div class="wow-profiles"></div>'
                    + '<div class="wow-add-row"><input type="text" maxlength="32" autocomplete="off" spellcheck="false" placeholder="Profile name, as in EllesmereUI" aria-label="New profile name">'
                    + '<button class="btn btn-ghost" type="button">Add</button></div></div>';
                profilesEl = el.querySelector('.wow-profiles');
                const input = el.querySelector('input');
                const add = () => {
                    const name = input.value.replace(/["\\|]/g, '').trim();
                    if (name && !draft.includes(name) && draft.length < 12) draft.push(name);
                    input.value = '';
                    renderProfiles(api);
                };
                el.querySelector('.wow-add-row .btn').addEventListener('click', add);
                input.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
                profilesEl.addEventListener('click', (e) => {
                    const b = e.target.closest('button[data-i]');
                    if (!b) return;
                    draft.splice(Number(b.dataset.i), 1);
                    renderProfiles(api);
                });
                renderProfiles(api);
            },
            collect() {
                return { profiles: draft };
            },
        },
    });
})();
