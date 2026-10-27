/*
 * The WoW plugin page: its core.
 *
 * Each addon's page side is a file in static/wow/modules/<id>.js, loaded when
 * the server lists a module with that id (wow_state), and registers itself:
 *
 *   AddonModules.register({
 *     id, name,
 *     commands: ['bw_pull', ...],            // what its keys send
 *     columns: [{ column: 'left'|'center'|'right', slot, order, title, grow,
 *                 render(el, api, head) }],  // without slot: a plate of its own
 *     update(api),                           // wow_state changed
 *     onUpdate(data, api),                   // its wow_<id>_update (a watched file's view)
 *     settings: { render(el, api), collect(api) },   // its part of WoW settings
 *   });
 *
 * Slots are rows of shared plates, filled by several modules: the action pad's
 * top, faders and bottom rows (centre), the utilities grid (right). A module
 * switched off in WoW settings leaves its plates and keys hidden; a row,
 * plate or column with nothing left drops out and the rest take its room.
 * Nothing here names an addon.
 *
 * Uses the dashboard's own helpers (index.html's top-level functions):
 * renderIcons, setIcon, escapeHtml, setText, haptic, keyRefused, tdNotice,
 * sendMessage, isAdmin, richMotion, holdDashboard via setCovering.
 */
(function () {
    'use strict';

    const MODULE_ID = /^[a-z][a-z0-9_]{0,31}$/;
    const ACK_TIMEOUT_MS = 4000;
    const COLUMNS = ['left', 'center', 'right'];
    // Shared plates: [column, order, title, slots, grows to fill its column].
    const SHARED = [
        ['center', 50, 'Action pad', ['top', 'faders', 'bottom'], true],
        ['right', 10, 'Utilities', ['grid'], false],
    ];
    const REASONS = {
        unknown: 'Unknown command', disabled: 'That addon is off', invalid: 'Not a valid value',
        no_keyboard: 'No keyboard access on the PC', no_clipboard: 'No clipboard on the PC',
        empty: 'Nothing to send', failed: 'The PC could not do it', timeout: 'The PC did not answer',
        offline: 'Not connected', admin_only: 'Only on the PC',
    };
    const OPEN_MS = 420, CLOSE_MS = 320;
    const EASE_OUT = 'cubic-bezier(0.22, 1, 0.36, 1)';
    const EASE_IN_OUT = 'cubic-bezier(0.65, 0, 0.35, 1)';

    const defs = new Map();        // id → registered page side
    const apis = new Map();        // id → its api
    const loading = new Set();
    const views = new Map();       // id → its last wow_<id>_update
    let server = null;             // the last wow_state data
    let view = null, cols = {}, rows = {}, sharedPlates = [], lastEl = null, gearEl = null, offEl = null;
    let isOpen = false, run = 0, anims = [], openKey = null;
    const acks = [];               // {command, key, text, timer}

    // ── the dashboard's helpers ──────────────────────────────────────────
    const esc = (v) => escapeHtml(v);
    const text = (el, v) => setText(el, String(v ?? ''));
    function admin() { return typeof isAdmin === 'function' && isAdmin(); }
    function connected() {
        return typeof socket !== 'undefined' && socket && socket.readyState === WebSocket.OPEN && authState === 'authed';
    }
    function rich() { return typeof richMotion === 'function' && richMotion(); }

    // ── registry ─────────────────────────────────────────────────────────
    const AddonModules = {
        register(def) {
            if (!def || !MODULE_ID.test(def.id || '') || defs.has(def.id)) return;
            def.columns = Array.isArray(def.columns) ? def.columns : [];
            defs.set(def.id, def);
            apis.set(def.id, makeApi(def.id));
            loading.delete(def.id);
            if (view) layout();
        },
        get: (id) => defs.get(id),
        list: () => [...defs.values()],
        enabled,
    };

    function enabled(id) {
        const m = server && server.modules.find(x => x.id === id);
        return !!(m && m.enabled);
    }

    function commandMeta(id) {
        if (!server) return null;
        for (const m of server.modules) {
            const c = m.commands.find(x => x.id === id);
            if (c) return { ...c, module: m.id, enabled: m.enabled };
        }
        return null;
    }

    // The chat text a command would type, its arguments filled in.
    function slash(id, args = {}) {
        const c = commandMeta(id);
        if (!c || !c.template.length) return '';
        return c.template.map(line => line.replace(/\{([a-z_]+)\}/g, (m, k) => args[k] ?? '')).join(' · ');
    }

    function makeApi(id) {
        return {
            id,
            send: (command, args, key) => send(command, args, key),
            state: (mid = id) => (server && server.state[mid]) || {},
            settings: (mid = id) => (server && server.settings[mid]) || {},
            general: () => ({
                character: (server && server.settings.character) || '',
                role: (server && server.settings.role) || '',
            }),
            command: commandMeta,
            slash,
            enabled,
            view: (mid = id) => views.get(mid) || null,
            key: makeKey,
            fader: makeFader,
            admin,
            rich,
            esc,
            text,
            icons: (root) => renderIcons(root),
            setIcon: (el, name) => setIcon(el, name),
            haptic: (ms) => haptic(ms ?? HAPTIC_KEY),
            notice: (o) => tdNotice(o),
            refused: (el) => keyRefused(el),
            onAck: null,
        };
    }

    function loadModules() {
        for (const m of server.modules) {
            if (defs.has(m.id) || loading.has(m.id) || !MODULE_ID.test(m.id)) continue;
            loading.add(m.id);
            const s = document.createElement('script');
            s.src = `static/wow/modules/${m.id}.js`;
            s.onerror = () => { console.warn('WoW module has no page side:', m.id); };
            document.head.appendChild(s);
        }
    }

    // ── parts: display keys and faders ───────────────────────────────────
    // A display key (the macro matrix's): glyph, a quiet legend, the command it
    // sends or its state; an optional on-air LED. The ⚠ of a refused tap is
    // drawn in with the glyph (keyRefused).
    function makeKey(o) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn wow-key';
        b.innerHTML = `<span class="key-screen"><span class="key-glyph"><i data-lucide="${esc(o.glyph || 'box')}"></i>${KEY_WARN}</span>`
            + '<span class="key-label"></span>'
            + (o.value !== undefined ? '<span class="wow-key-val"></span>' : '')
            + (o.led ? '<span class="wow-led" aria-hidden="true"></span>' : '')
            + '</span>';
        renderIcons(b);
        const label = b.querySelector('.key-label');
        const val = b.querySelector('.wow-key-val');
        const glyph = b.querySelector('.key-glyph');
        const aria = () => b.setAttribute('aria-label', [label.textContent, val ? val.textContent : ''].filter(Boolean).join(', '));
        b.setLabel = (t) => { text(label, t); aria(); };
        b.setValue = (t) => { if (val) { text(val, t); aria(); } };
        b.setOn = (on) => { b.classList.toggle('is-on', !!on); b.setAttribute('aria-pressed', on ? 'true' : 'false'); };
        b.setRec = (on) => b.classList.toggle('is-rec', !!on);
        b.setGlyph = (name) => {
            if (glyph.dataset.icon === name) return;
            glyph.dataset.icon = name;
            const old = glyph.querySelector('svg:not(.key-warn), i:not(.key-warn)');
            const i = document.createElement('i');
            i.setAttribute('data-lucide', name);
            if (old) old.replaceWith(i); else glyph.prepend(i);
            renderIcons(glyph);
        };
        b.setLabel(o.label || '');
        if (o.value !== undefined) b.setValue(o.value);
        if (o.onTap) b.addEventListener('click', () => o.onTap(b));
        return b;
    }

    // A horizontal studio fader: slot, printed ticks (one per step), a cap that
    // never jumps under the finger, a readout display. Whole steps only, a
    // detent tick on each and a firmer one at the ends.
    function makeFader(o) {
        const min = o.min, max = o.max, steps = max - min;
        const el = document.createElement('div');
        el.className = 'wow-fader';
        el.innerHTML = `<span class="wow-fader-name">${esc(o.name)}</span>`
            + `<div class="wow-fader-track" role="slider" tabindex="0" aria-label="${esc(o.name)}" aria-valuemin="${min}" aria-valuemax="${max}" style="--steps: ${steps}">`
            + '<div class="wow-fader-slot"><div class="wow-fader-fill"></div></div><div class="wow-fader-ticks"></div>'
            + '<div class="wow-fader-carrier"><div class="wow-fader-cap"></div></div></div>'
            + '<div class="scribble wow-fader-read"><span class="live num"><span class="live-v"></span></span></div>';
        const track = el.querySelector('.wow-fader-track');
        const read = el.querySelector('.live-v');
        let value = min;

        function show(v) {
            track.style.setProperty('--v', ((v - min) / steps).toFixed(4));
            track.setAttribute('aria-valuenow', v);
            const t = o.format ? o.format(v) : String(v);
            track.setAttribute('aria-valuetext', t);
            text(read, t);
        }
        function set(v, user) {
            v = Math.min(max, Math.max(min, Math.round(v)));
            if (v === value) return;
            value = v;
            show(v);
            if (user) {
                haptic(v === min || v === max ? HAPTIC_END : HAPTIC_DETENT);
                if (o.onChange) o.onChange(v);
            }
        }

        let drag = null;
        function geometry() {
            const r = track.getBoundingClientRect();
            const cw = el.querySelector('.wow-fader-cap').getBoundingClientRect().width;
            return { left: r.left + cw / 2, span: Math.max(1, r.width - cw), cw };
        }
        track.addEventListener('pointerdown', (e) => {
            if (!e.isPrimary) return;
            const g = geometry();
            const capX = g.left + ((value - min) / steps) * g.span;
            // On the cap: keep the finger's offset; elsewhere: jump there.
            const offset = Math.abs(e.clientX - capX) <= g.cw / 2 ? e.clientX - capX : 0;
            drag = { id: e.pointerId, g, offset };
            track.setPointerCapture(e.pointerId);
            track.classList.add('is-dragging');
            set(min + ((e.clientX - offset - g.left) / g.span) * steps, true);
        });
        track.addEventListener('pointermove', (e) => {
            if (!drag || e.pointerId !== drag.id) return;
            set(min + ((e.clientX - drag.offset - drag.g.left) / drag.g.span) * steps, true);
        });
        const end = (e) => {
            if (!drag || e.pointerId !== drag.id) return;
            drag = null;
            track.classList.remove('is-dragging');
        };
        track.addEventListener('pointerup', end);
        track.addEventListener('pointercancel', end);
        track.addEventListener('keydown', (e) => {
            const d = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[e.key];
            if (!d) return;
            e.preventDefault();
            set(value + d, true);
        });

        value = Math.min(max, Math.max(min, Math.round(o.value ?? min)));
        show(value);
        el.setValue = (v) => { if (!drag) set(v, false); };
        el.value = () => value;
        return el;
    }

    // ── commands ─────────────────────────────────────────────────────────
    function send(command, args, key) {
        const c = commandMeta(command);
        const label = (c && (slash(command, args || {}) || c.label)) || command;
        if (!connected()) {
            if (key) keyRefused(key);
            setLast('error', `Not sent · ${REASONS.offline}`);
            return;
        }
        sendMessage(args ? { type: 'wow_addon_action', command, args } : { type: 'wow_addon_action', command });
        setLast('pending', label);
        const entry = { command, key, text: label };
        entry.timer = setTimeout(() => answer({ command, status: 'error', reason: 'timeout' }), ACK_TIMEOUT_MS);
        acks.push(entry);
    }

    // Answers come in the order the PC took the commands.
    function answer(d) {
        const i = acks.findIndex(a => a.command === d.command);
        const entry = i >= 0 ? acks.splice(i, 1)[0] : null;
        if (entry) clearTimeout(entry.timer);
        // A module may word a failure itself ("No note on the PC's clipboard").
        const c = commandMeta(d.command);
        const api = c && apis.get(c.module);
        const own = api && api.onAck ? api.onAck(d) : null;
        if (d.status === 'sent') {
            setLast('ok', entry ? entry.text : d.command);
        } else {
            if (entry && entry.key) keyRefused(entry.key);
            setLast('error', typeof own === 'string' ? own : `Not sent · ${REASONS[d.reason] || REASONS.failed}`);
        }
    }

    function setLast(state, t) {
        if (!lastEl) return;
        lastEl.dataset.state = state;
        text(lastEl.querySelector('.live-v'), t);
    }

    // ── the page ─────────────────────────────────────────────────────────
    function build() {
        if (view) return;
        view = document.createElement('div');
        view.id = 'wow-view';
        view.hidden = true;
        view.tabIndex = -1;
        view.setAttribute('role', 'dialog');
        view.setAttribute('aria-modal', 'true');
        view.setAttribute('aria-label', 'World of Warcraft');
        view.innerHTML = '<div class="wow-ground" aria-hidden="true"></div>'
            + '<header class="wow-bar card">'
            + '<div class="wow-bar-side"><button class="btn btn-top wow-back" type="button" aria-label="Back to the dashboard"><i data-lucide="arrow-left"></i></button></div>'
            + '<div class="wow-bar-title"><i data-lucide="swords"></i><h1>World of Warcraft</h1></div>'
            + '<div class="wow-bar-side wow-bar-end">'
            + '<div class="scribble wow-last" data-state="idle"><span class="ch-name">Last command</span><span class="live num"><span class="live-v">&mdash;</span></span></div>'
            + '<button class="btn btn-top wow-gear" type="button" aria-label="WoW settings" hidden><i data-lucide="settings"></i></button>'
            + '</div></header>'
            + '<div class="wow-grid">' + COLUMNS.map(c => `<div class="wow-col" data-col="${c}"></div>`).join('') + '</div>'
            + '<p class="wow-off" hidden>Every addon is off. Turn them on in WoW settings on the PC.</p>';
        renderIcons(view);
        COLUMNS.forEach(c => { cols[c] = view.querySelector(`[data-col="${c}"]`); });
        lastEl = view.querySelector('.wow-last');
        gearEl = view.querySelector('.wow-gear');
        offEl = view.querySelector('.wow-off');
        view.querySelector('.wow-back').addEventListener('click', () => close());
        gearEl.addEventListener('click', () => openSettings());
        view.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !document.querySelector('.modal-overlay.active')) close();
        });
        document.body.appendChild(view);
        layout();
    }

    function plate(column, order, title, grow) {
        const sec = document.createElement('section');
        sec.className = 'card panel wow-sec' + (grow ? ' is-grow' : '');
        sec.dataset.order = order;
        let head = null;
        if (title) {
            const h = document.createElement('div');
            h.className = 'panel-head';
            h.innerHTML = `<h2 class="panel-title">${esc(title)}</h2><div class="wow-head-keys"></div>`;
            sec.appendChild(h);
            head = h.querySelector('.wow-head-keys');
            sec.setAttribute('aria-label', title);
        }
        return { sec, head };
    }

    // Lays every plate and key out again: the server's module order, then each
    // part's order. Runs when a module registers; state changes only update.
    function layout() {
        if (!view) return;
        COLUMNS.forEach(c => cols[c].replaceChildren());
        rows = {};
        sharedPlates = [];
        const placed = [];   // [column, order, element]
        for (const [column, order, title, slots, grow] of SHARED) {
            const { sec } = plate(column, order, title, grow);
            sec.dataset.shared = '';
            for (const s of slots) {
                const row = document.createElement('div');
                row.className = 'wow-row';
                row.dataset.slot = s;
                sec.appendChild(row);
                rows[s] = row;
            }
            sharedPlates.push(sec);
            placed.push([column, order, sec]);
        }
        const order = server ? server.modules.map(m => m.id) : [...defs.keys()];
        const items = [];   // [slot, order, element]
        for (const id of order) {
            const def = defs.get(id);
            if (!def) continue;
            const api = apis.get(id);
            def.columns.forEach((part, n) => {
                const o = typeof part.order === 'number' ? part.order : 100 + n;
                if (part.slot && rows[part.slot]) {
                    const item = document.createElement('div');
                    item.className = 'wow-item';
                    item.dataset.module = id;
                    part.render(item, api);
                    items.push([part.slot, o, item]);
                } else if (COLUMNS.includes(part.column)) {
                    const { sec, head } = plate(part.column, o, part.title, part.grow);
                    sec.dataset.module = id;
                    part.render(sec, api, head);
                    placed.push([part.column, o, sec]);
                }
            });
        }
        items.sort((a, b) => a[1] - b[1]).forEach(([slot, , el]) => rows[slot].appendChild(el));
        placed.sort((a, b) => a[1] - b[1]).forEach(([column, , el]) => cols[column].appendChild(el));
        renderIcons(view);
        refresh();
        if (server) for (const [id, data] of views) {
            const def = defs.get(id);
            if (def && def.onUpdate) def.onUpdate(data, apis.get(id));
        }
    }

    // Shows what is on, then lets each module bring its parts up to date.
    function refresh() {
        if (!view) return;
        view.querySelectorAll('[data-module]').forEach(el => { el.hidden = !enabled(el.dataset.module); });
        Object.values(rows).forEach(row => { row.hidden = ![...row.children].some(c => !c.hidden); });
        sharedPlates.forEach(sec => { sec.hidden = ![...sec.querySelectorAll('.wow-row')].some(r => !r.hidden); });
        let any = false;
        COLUMNS.forEach(c => {
            const shown = [...cols[c].children].some(s => !s.hidden);
            cols[c].hidden = !shown;
            any = any || shown;
        });
        offEl.hidden = any || !server;
        gearEl.hidden = !admin();
        for (const [id, def] of defs) if (def.update) def.update(apis.get(id));
    }

    // ── open and close ───────────────────────────────────────────────────
    // Rich: the page grows evenly out of the key that opened it and fades in,
    // and back into it on close: one Web Animation of transform and opacity on
    // the page, run on the compositor. Lite and reduced motion: one frame.
    function keyTransform(key) {
        const k = key && key.isConnected ? key.getBoundingClientRect() : null;
        const p = view.getBoundingClientRect();
        if (!k || !k.width || !p.width) return 'scale(0.92)';
        const dx = k.left + k.width / 2 - (p.left + p.width / 2);
        const dy = k.top + k.height / 2 - (p.top + p.height / 2);
        return `translate(${dx}px, ${dy}px) scale(${Math.max(0.05, k.width / p.width)})`;
    }

    function freeze() {
        anims.forEach(a => { try { a.commitStyles(); } catch (err) { /* not rendered */ } a.cancel(); });
        anims = [];
    }

    function settle(r, show) {
        if (r !== run) return;
        freeze();
        view.classList.remove('is-moving');
        view.style.transform = '';
        view.style.opacity = '';
        if (!show) view.hidden = true;
    }

    function open(key) {
        build();
        if (isOpen) return;
        isOpen = true;
        openKey = key || null;
        const r = ++run;
        // The desktop window's title bar stays above it.
        const bar = document.getElementById('desktop-titlebar');
        view.style.top = bar && bar.offsetHeight ? bar.offsetHeight + 'px' : '';
        const fresh = view.hidden;
        view.hidden = false;
        refresh();
        if (typeof setCovering === 'function') setCovering('wow', true);
        if (typeof npMarquee !== 'undefined') npMarquee.pause(true, 'wow');
        view.focus({ preventScroll: true });
        if (!rich()) return;
        if (fresh) {
            view.style.transform = keyTransform(openKey);
            view.style.opacity = '0';
        } else {
            freeze();
        }
        view.classList.add('is-moving');
        anims.push(view.animate([{ transform: 'none' }], { duration: OPEN_MS, easing: EASE_OUT, fill: 'both' }));
        anims.push(view.animate([{ opacity: 1 }], { duration: 200, easing: EASE_OUT, fill: 'both' }));
        setTimeout(() => settle(r, true), OPEN_MS + 20);
    }

    function close() {
        if (!isOpen) return;
        isOpen = false;
        const r = ++run;
        if (typeof setCovering === 'function') setCovering('wow', false);
        if (typeof npMarquee !== 'undefined') npMarquee.pause(false, 'wow');
        if (openKey && openKey.isConnected) openKey.focus({ preventScroll: true });
        if (!rich()) {
            settle(r, false);
            return;
        }
        freeze();
        view.classList.add('is-moving');
        anims.push(view.animate([{ transform: keyTransform(openKey) }], { duration: CLOSE_MS, easing: EASE_IN_OUT, fill: 'both' }));
        anims.push(view.animate([{ opacity: 0 }], { duration: 200, delay: CLOSE_MS - 200, easing: EASE_IN_OUT, fill: 'both' }));
        setTimeout(() => settle(r, false), CLOSE_MS + 20);
    }

    // ── messages ─────────────────────────────────────────────────────────
    function onMessage(msg) {
        const d = msg.data;
        if (msg.type === 'wow_state') {
            const order = d.modules.map(m => m.id).join();
            const relayout = !server || server.modules.map(m => m.id).join() !== order;
            server = d;
            loadModules();
            if (view) relayout ? layout() : refresh();
            if (settingsEl && settingsEl.classList.contains('active')) renderFiles();
            return;
        }
        if (msg.type === 'wow_cmd_ack') {
            answer(d || {});
            return;
        }
        const m = /^wow_([a-z][a-z0-9_]{0,31})_update$/.exec(msg.type);
        if (m) {
            views.set(m[1], d);
            const def = defs.get(m[1]);
            if (view && def && def.onUpdate) def.onUpdate(d, apis.get(m[1]));
        }
    }

    // An older server refuses the message type itself.
    function onRefused(d) {
        if (d && d.action === 'wow_addon_action' && acks.length) {
            answer({ command: acks[0].command, status: 'error', reason: d.reason });
        }
    }

    // ── settings ─────────────────────────────────────────────────────────
    // The console's Settings dialog, for the plugin: General (the player, the
    // game folder), Addons (each module's switch, its own settings and the
    // text of its commands), Key slots. PC only: saving settings is admin.
    let settingsEl = null, setPanes = {};

    function settingRow(title, desc, input) {
        return `<div class="setting-row"><div><label class="title">${esc(title)}</label>`
            + (desc ? `<label class="desc">${esc(desc)}</label>` : '') + `</div>${input}</div>`;
    }
    function switchHtml(attrs, on) {
        return `<label class="switch"><input type="checkbox" ${attrs}${on ? ' checked' : ''}><span class="slider"></span></label>`;
    }

    function openSettings() {
        if (!admin() || !server) return;
        if (!settingsEl) {
            settingsEl = document.createElement('div');
            settingsEl.id = 'wow-settings';
            settingsEl.className = 'modal-overlay';
            settingsEl.innerHTML = '<div class="modal-card" id="wow-settings-card" role="dialog" aria-modal="true" aria-label="WoW settings">'
                + '<button class="btn modal-close" type="button" aria-label="Close WoW settings"><i data-lucide="x"></i></button>'
                + '<nav class="tabs" aria-label="WoW settings sections"><div class="tabs-title"><i data-lucide="swords"></i>WoW settings</div>'
                + '<button class="tab active" type="button" data-tab="general">General</button>'
                + '<button class="tab" type="button" data-tab="addons">Addons</button>'
                + '<button class="tab" type="button" data-tab="keys">Key slots</button></nav>'
                + '<div class="settings-content-wrapper">'
                + '<div class="tab-content active" data-pane="general"></div>'
                + '<div class="tab-content" data-pane="addons"></div>'
                + '<div class="tab-content" data-pane="keys"></div>'
                + '<div class="wow-set-actions"><button class="btn btn-pill btn-ghost" type="button" data-act="cancel">Cancel</button>'
                + '<button class="btn btn-pill btn-primary" type="button" data-act="save"><i data-lucide="check"></i>Save</button></div>'
                + '</div></div>';
            document.body.appendChild(settingsEl);
            ['general', 'addons', 'keys'].forEach(p => { setPanes[p] = settingsEl.querySelector(`[data-pane="${p}"]`); });
            settingsEl.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => setTab(t.dataset.tab)));
            settingsEl.querySelector('.modal-close').addEventListener('click', closeSettings);
            settingsEl.querySelector('[data-act="cancel"]').addEventListener('click', closeSettings);
            settingsEl.querySelector('[data-act="save"]').addEventListener('click', saveSettings);
            settingsEl.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); closeSettings(); } });
            // The Linux window holds the dashboard still under a dialog.
            if (typeof dialogWatch !== 'undefined') dialogWatch.observe(settingsEl, { attributes: true, attributeFilter: ['class'] });
        }
        renderSettings();
        setTab('general');
        settingsEl.classList.add('active');
        renderIcons(settingsEl);
        settingsEl.querySelector('.tab.active').focus({ preventScroll: true });
    }

    function closeSettings() {
        if (settingsEl) settingsEl.classList.remove('active');
        if (gearEl && !gearEl.hidden) gearEl.focus({ preventScroll: true });
    }

    function setTab(tab) {
        settingsEl.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
        Object.entries(setPanes).forEach(([p, el]) => el.classList.toggle('active', p === tab));
        settingsEl.querySelector('.settings-content-wrapper').scrollTop = 0;
    }

    function renderSettings() {
        const s = server.settings;
        setPanes.general.innerHTML = '<h3 class="settings-h">How keys reach the game</h3>'
            + '<div class="notice"><i data-lucide="lightbulb"></i><p>A key types its command into WoW\'s chat: the PC puts it on the clipboard and presses Enter, Ctrl+V, Enter, then puts your clipboard back. WoW must be the window in front. A command with a key slot presses that key instead.</p></div>'
            + '<h3 class="settings-h">Your character</h3>'
            + '<p class="settings-sub">NSRT reminders tagged with your character or role tag are your personal ones. Settings → Battle.net finds your character and fills these in.</p>'
            + '<div class="wow-set-grid">'
            + `<div class="form-group"><label for="wow-set-char">Character name</label><input type="text" id="wow-set-char" maxlength="48" autocomplete="off" spellcheck="false" placeholder="Magicbane" value="${esc(s.character)}"></div>`
            + `<div class="form-group"><label for="wow-set-role">Role tag</label><input type="text" id="wow-set-role" maxlength="48" autocomplete="off" spellcheck="false" placeholder="ranged" value="${esc(s.role)}"></div>`
            + `<div class="form-group"><label for="wow-set-realm">Realm</label><input type="text" id="wow-set-realm" maxlength="64" autocomplete="off" spellcheck="false" placeholder="Draenor" value="${esc(s.realm)}"></div>`
            + '<div class="form-group"><label for="wow-set-region">Region</label><select id="wow-set-region">'
            + ['eu', 'us', 'kr', 'tw'].map(r => `<option value="${r}"${s.region === r ? ' selected' : ''}>${r.toUpperCase()}</option>`).join('')
            + '</select></div>'
            + '</div>'
            + '<h3 class="settings-h">Game folder</h3>'
            + '<p class="settings-sub">The World of Warcraft folder, its _retail_ folder, or a SavedVariables folder. Checked every 5 seconds; WoW writes SavedVariables on /reload, logout and exit.</p>'
            + `<div class="form-group"><label for="wow-set-path">Folder</label><input type="text" id="wow-set-path" maxlength="1024" autocomplete="off" spellcheck="false" placeholder="~/Games/battlenet/drive_c/Program Files (x86)/World of Warcraft/_retail_" value="${esc(s.wow_path)}"></div>`
            + `<div class="form-group"><label for="wow-set-account">Account folder (optional)</label><input type="text" id="wow-set-account" maxlength="64" autocomplete="off" spellcheck="false" placeholder="The newest file wins" value="${esc(s.account)}"></div>`
            + '<div class="wow-set-files"></div>';
        renderFiles();

        // Addons: one group each, its switch first.
        setPanes.addons.innerHTML = '<h3 class="settings-h">Addons</h3><p class="settings-sub">Each one adds its keys and plates to the page. A key you never use can stay off.</p>';
        for (const m of server.modules) {
            const group = document.createElement('div');
            group.className = 'setting-group wow-set-module';
            group.dataset.module = m.id;
            group.dataset.on = String(m.enabled);
            group.innerHTML = settingRow(m.name, m.description, switchHtml(`data-toggle="${esc(m.id)}" aria-label="${esc(m.name)}"`, m.enabled))
                + '<div class="wow-set-body"></div>';
            const body = group.querySelector('.wow-set-body');
            const def = defs.get(m.id);
            if (def && def.settings && def.settings.render) {
                const own = document.createElement('div');
                own.className = 'wow-set-own';
                own.style.display = 'contents';
                def.settings.render(own, apis.get(m.id));
                body.appendChild(own);
            }
            for (const c of m.commands.filter(c => c.default.length === 1)) {
                const row = document.createElement('div');
                row.className = 'wow-set-cmd';
                const own = c.template[0] !== c.default[0] ? c.template[0] : '';
                row.innerHTML = `<label for="wow-cmd-${esc(c.id)}">${esc(c.label)}</label>`
                    + `<input type="text" id="wow-cmd-${esc(c.id)}" data-cmd="${esc(c.id)}" maxlength="255" autocomplete="off" spellcheck="false" placeholder="${esc(c.default[0])}" value="${esc(own)}">`
                    + (c.verified ? '' : `<span class="wow-set-hint">Check this command in ${esc(m.name)}: it may be named differently.</span>`);
                body.appendChild(row);
            }
            if (!body.children.length) body.remove();
            group.querySelector('[data-toggle]').addEventListener('change', (e) => { group.dataset.on = String(e.target.checked); });
            setPanes.addons.appendChild(group);
        }

        // Key slots: Ctrl + Alt + Shift + F1–F12, each a command without arguments.
        const bindable = server.modules.flatMap(m => m.commands.filter(c => c.bindable).map(c => ({ ...c, module: m })));
        const options = (slot) => '<option value="">Nothing</option>' + bindable.map(c =>
            `<option value="${esc(c.id)}"${s.keybinds[slot] === c.id ? ' selected' : ''}>${esc(c.module.name)}: ${esc(c.label)}</option>`).join('');
        setPanes.keys.innerHTML = '<h3 class="settings-h">Key slots</h3>'
            + '<p class="settings-sub">In WoW, make a macro with the text shown and bind it to the same keys. A command with a slot then presses them instead of typing into chat: quicker, and it works while chat is busy. Commands with a value (pull length, profile) are always typed.</p>'
            + '<div class="setting-group">' + server.slots.map(slot => '<div class="wow-slot">'
                + `<span class="wow-caps" aria-label="Control Alt Shift ${slot}"><span class="wow-cap">Ctrl</span>+<span class="wow-cap">Alt</span>+<span class="wow-cap">Shift</span>+<span class="wow-cap">${slot}</span></span>`
                + `<select data-slot="${slot}" aria-label="Command for Control Alt Shift ${slot}">${options(slot)}</select>`
                + '<span class="wow-slot-macro"></span></div>').join('') + '</div>';
        const macroText = (sel) => {
            const c = bindable.find(x => x.id === sel.value);
            text(sel.parentElement.querySelector('.wow-slot-macro'), c ? `Macro text: ${c.template[0]}` : '');
        };
        setPanes.keys.querySelectorAll('select').forEach(sel => {
            macroText(sel);
            sel.addEventListener('change', () => macroText(sel));
        });
        renderIcons(settingsEl);
    }

    function renderFiles() {
        const box = settingsEl && settingsEl.querySelector('.wow-set-files');
        if (!box || !server) return;
        const watched = server.modules.filter(m => m.watch);
        box.innerHTML = watched.map(m => {
            const file = server.files && server.files[m.id];
            return `<p class="wow-set-file" data-found="${file ? 'yes' : 'no'}">${esc(m.name)}: `
                + (file ? esc(file) : (server.settings.wow_path ? `${esc(m.watch)} not found` : 'no folder set')) + '</p>';
        }).join('');
    }

    function saveSettings() {
        const q = (sel) => settingsEl.querySelector(sel);
        const out = {
            character: q('#wow-set-char').value.trim(),
            role: q('#wow-set-role').value.trim(),
            realm: q('#wow-set-realm').value.trim(),
            region: q('#wow-set-region').value,
            wow_path: q('#wow-set-path').value.trim(),
            account: q('#wow-set-account').value.trim(),
            modules: {}, keybinds: {}, slash: {},
        };
        settingsEl.querySelectorAll('[data-toggle]').forEach(i => { out.modules[i.dataset.toggle] = i.checked; });
        settingsEl.querySelectorAll('select[data-slot]').forEach(sel => { if (sel.value) out.keybinds[sel.dataset.slot] = sel.value; });
        const used = Object.values(out.keybinds);
        if (new Set(used).size !== used.length) {
            tdNotice({ title: 'One slot per command', text: 'A command can have one key slot. Pick another command for the second slot.', icon: 'keyboard', tone: 'warning' });
            setTab('keys');
            return;
        }
        for (const input of settingsEl.querySelectorAll('input[data-cmd]')) {
            const v = input.value.trim();
            if (!v) continue;
            const c = commandMeta(input.dataset.cmd);
            const names = [...v.matchAll(/\{([a-z_]+)\}/g)].map(m => m[1]);
            const rest = v.replace(/\{([a-z_]+)\}/g, '');
            if (!v.startsWith('/') || names.some(n => !(n in c.args)) || /[{}]/.test(rest)) {
                const allowed = Object.keys(c.args).map(n => `{${n}}`).join(', ');
                tdNotice({
                    title: `“${c.label}” cannot be sent`,
                    text: 'A command starts with / and can only fill in ' + (allowed ? allowed : 'nothing') + '.',
                    icon: 'alert-triangle', tone: 'warning',
                });
                setTab('addons');
                input.focus();
                return;
            }
            if (v !== c.default[0]) out.slash[c.id] = v;
        }
        for (const [id, def] of defs) {
            if (def.settings && def.settings.collect) {
                const own = def.settings.collect(apis.get(id));
                if (own && typeof own === 'object') out[id] = own;
            }
        }
        sendMessage({ type: 'wow_settings_save', wow_addons: out });
        closeSettings();
    }

    window.AddonModules = AddonModules;
    window.TDWow = {
        open, close, onMessage, onRefused,
        isOpen: () => isOpen,
        settings: () => (server ? server.settings : null),   // Settings → Battle.net fills its fields from these
    };
})();
