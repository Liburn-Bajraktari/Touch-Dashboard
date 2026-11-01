/*
 * EllesmereUI: a plate of profile keys (one for each profile saved in WoW
 * settings, the one sent last lit), and the state of any Unlock mode key: lit
 * and "Unlocked" while it is. Both follow the taps sent from here: unlocking
 * in game itself leaves the key showing the other state.
 */
(function () {
    'use strict';

    let profilesBox = null, profilesEl = null, draft = [];

    function drawProfiles(api) {
        if (!profilesBox || !profilesBox.isConnected) return;
        const list = api.settings().profiles || [];
        const current = api.state().profile;
        const key = JSON.stringify([list, current]);
        if (profilesBox.dataset.drawn !== key) {
            profilesBox.dataset.drawn = key;
            profilesBox.replaceChildren();
            for (const name of list) {
                const k = api.key({ glyph: 'user-cog', label: name, value: '' });
                k.setValue(api.slash('eui_profile', { name }));
                k.addEventListener('click', () => api.send('eui_profile', { name }, k));
                k.setOn(name === current);
                profilesBox.appendChild(k);
            }
            if (!list.length) {
                profilesBox.innerHTML = `<p class="wow-cell-note">${api.admin() ? 'Add your profiles in WoW settings → Addons → EllesmereUI.' : 'Profiles are set in WoW settings on the PC.'}</p>`;
            }
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
        widgets: {
            profiles: {
                render(sec, api) {
                    profilesBox = document.createElement('div');
                    profilesBox.className = 'wow-keys wow-profile-keys';
                    sec.appendChild(profilesBox);
                    drawProfiles(api);
                },
            },
        },
        keyState(command, args, api) {
            const st = api.state();
            if (command === 'eui_unlock') return { on: st.unlocked, value: st.unlocked ? 'Unlocked' : 'Locked' };
            if (command === 'eui_profile') return { on: !!args.name && st.profile === args.name };
            return null;
        },
        update: drawProfiles,
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
