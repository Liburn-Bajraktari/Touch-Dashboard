/*
 * Combat logging: Start logging and Live logging (action pad, bottom), each
 * with an on-air tally lit while it runs (red, as on any broadcast) and its
 * legend saying so. Both follow the taps sent from here: /combatlog typed in
 * game itself leaves them showing the other state.
 */
(function () {
    'use strict';

    let logKey = null, liveKey = null, ui = null;

    function draw(api) {
        const st = api.state();
        if (logKey) {
            logKey.setRec(st.combatlog);
            logKey.setLabel(st.combatlog ? 'Logging' : 'Start logging');
            logKey.setAttribute('aria-pressed', String(!!st.combatlog));
        }
        if (liveKey) {
            const allowed = api.settings().wcl_enabled !== false;
            liveKey.disabled = !allowed && !st.wcl_live;
            liveKey.setRec(st.wcl_live);
            liveKey.setLabel(st.wcl_live ? 'Live' : 'Live logging');
            liveKey.setValue(allowed ? 'Warcraft Logs' : 'Off in settings');
            liveKey.setAttribute('aria-pressed', String(!!st.wcl_live));
        }
    }

    AddonModules.register({
        id: 'combatlog',
        name: 'Combat logging',
        commands: ['combatlog', 'wcl_live_stream'],
        columns: [
            {
                slot: 'bottom', order: 30, render(el, api) {
                    logKey = api.key({ glyph: 'circle-dot', label: 'Start logging', value: api.slash('combatlog'), led: true });
                    logKey.addEventListener('click', () => api.send('combatlog', null, logKey));
                    el.appendChild(logKey);
                    draw(api);
                },
            },
            {
                slot: 'bottom', order: 40, render(el, api) {
                    liveKey = api.key({ glyph: 'radio', label: 'Live logging', value: '', led: true });
                    liveKey.addEventListener('click', () => api.send('wcl_live_stream', null, liveKey));
                    el.appendChild(liveKey);
                    draw(api);
                },
            },
        ],
        update(api) {
            if (logKey) logKey.setValue(api.slash('combatlog'));
            draw(api);
        },
        settings: {
            render(el, api) {
                const s = api.settings();
                const apps = typeof osApps !== 'undefined' && Array.isArray(osApps) ? osApps : [];
                const known = apps.some(a => a.exec === s.wcl_app);
                el.innerHTML = '<div class="setting-row" style="padding: 0">'
                    + '<div><label class="title" for="wow-wcl-on">Live logging key</label><label class="desc" for="wow-wcl-on">Turns the combat log on if needed, then starts the uploader</label></div>'
                    + `<label class="switch"><input type="checkbox" id="wow-wcl-on"${s.wcl_enabled !== false ? ' checked' : ''}><span class="slider"></span></label></div>`
                    + '<div class="wow-set-grid">'
                    + '<div class="form-group"><label for="wow-wcl-app">Warcraft Logs uploader</label><select id="wow-wcl-app">'
                    + '<option value="">Do not start an app</option>'
                    + (s.wcl_app && !known ? `<option value="${api.esc(s.wcl_app)}" selected>${api.esc(s.wcl_app)}</option>` : '')
                    + apps.map(a => `<option value="${api.esc(a.exec)}"${a.exec === s.wcl_app ? ' selected' : ''}>${api.esc(a.name)}</option>`).join('')
                    + '</select></div>'
                    + `<div class="form-group"><label for="wow-wcl-key">Stream key</label><input type="password" id="wow-wcl-key" maxlength="128" autocomplete="off" spellcheck="false" value="${api.esc(s.wcl_key || '')}"></div>`
                    + '</div><p class="settings-sub">Kept on this PC; tablets never receive it.</p>';
                ui = el;
            },
            collect() {
                return {
                    wcl_enabled: ui.querySelector('#wow-wcl-on').checked,
                    wcl_app: ui.querySelector('#wow-wcl-app').value,
                    wcl_key: ui.querySelector('#wow-wcl-key').value.trim(),
                };
            },
        },
    });
})();
