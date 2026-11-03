/*
 * The WoW plugin page: its core.
 *
 * Each addon's page side is a file in static/wow/modules/<id>.js, loaded when
 * the server lists a module with widgets or a view (wow_state), and registers
 * itself:
 *
 *   AddonModules.register({
 *     id, name,
 *     widgets: { <widget id>: { render(sec, api, head) } },   // its plates, as the server names them
 *     keyState(command, args, api),          // {on, rec, value, label, disabled} for a key sending its command
 *     update(api),                           // wow_state / wow_live changed
 *     onUpdate(data, api),                   // its wow_<id>_update (its view)
 *     settings: { render(el, api), collect(api) },   // its part of WoW settings
 *   });
 *
 * The layout is the server's (settings.layout): three columns of cells. A
 * cell is a module's widget, a keys widget (keys the player fills from every
 * command the server knows, custom ones included) or a blank. Each column is
 * a two-track grid: a full cell takes a row, two half cells share one (a half
 * alone takes the row's width outside edit mode), and rows with a growing
 * widget split the column's spare height. Any screen may rearrange the page
 * (wow_layout_save, checked on the server); command text, slots and custom
 * commands are the PC's (wow_settings_save).
 *
 * Uses the dashboard's own helpers (index.html's top-level functions):
 * renderIcons, setIcon, escapeHtml, setText, haptic, keyRefused, tdNotice,
 * tdConfirm, sendMessage, isAdmin, richMotion, holdDashboard via setCovering.
 */
(function () {
    'use strict';

    const MODULE_ID = /^[a-z][a-z0-9_]{0,31}$/;
    const ACK_TIMEOUT_MS = 6000;           // an exchange with the companion takes about a second
    const HOLD_MS = 550;                   // a held key opens its editor
    const COLUMNS = 3;
    const LABEL_MAX = 24;
    const REASONS = {
        unknown: 'Unknown command', disabled: 'That addon is off', invalid: 'Not a valid value',
        no_keyboard: 'No keyboard access on the PC', no_clipboard: 'No clipboard on the PC',
        empty: 'Nothing to send', failed: 'The PC could not do it', timeout: 'The PC did not answer',
        offline: 'Not connected', admin_only: 'Only on the PC',
        no_answer: 'The addon opened its box but gave nothing back',
        no_box: 'The addon did not open its box: is it on in game? (/tdc)',
        not_front: 'WoW is not the window in front on the PC',
        elevated: 'WoW runs as administrator, so Windows drops the PC’s keys: start it normally',
        no_check: 'The PC cannot see WoW’s window to check the addon',
        no_bossmod: 'A break needs BigWigs, DBM or NSRT in the game',
        in_combat: 'Key slots change after combat: try again then',
        no_companion: 'Install the Touch Dashboard addon (WoW settings on the PC)',
        no_folder: 'No AddOns folder: set the game folder first',
    };
    const OPEN_MS = 420, CLOSE_MS = 320;
    const EASE_OUT = 'cubic-bezier(0.22, 1, 0.36, 1)';
    const EASE_IN_OUT = 'cubic-bezier(0.65, 0, 0.35, 1)';
    // Glyphs offered for keys besides the commands' own.
    const GLYPHS = ['box', 'star', 'heart', 'flame', 'zap', 'skull', 'swords', 'sword', 'shield', 'shield-plus',
        'crosshair', 'target', 'timer', 'timer-reset', 'coffee', 'bell-ring', 'megaphone', 'message-circle',
        'sparkles', 'leaf', 'droplet', 'flask-conical', 'wand-2', 'map', 'map-pin', 'map-pin-off', 'compass',
        'check-circle-2', 'users', 'user', 'crown', 'trophy', 'gem', 'coins', 'backpack', 'scroll-text', 'book-open',
        'bar-chart-3', 'layers', 'layout-grid', 'settings-2', 'refresh-cw', 'rotate-ccw', 'move', 'eye', 'eye-off',
        'radio', 'cpu', 'terminal', 'hand', 'music-2', 'rabbit', 'dog', 'hourglass', 'cloud-lightning', 'moon', 'sun'];

    const defs = new Map();        // id → registered page side
    const apis = new Map();        // id → its api
    const loading = new Set();
    const views = new Map();       // id → its last wow_<id>_update
    let server = null;             // the last wow_state data (wow_live updates its state)
    let view = null, gridEl = null, colEls = [], lastEl = null, gearEl = null, editEl = null, offEl = null, barEl = null;
    let isOpen = false, run = 0, anims = [], openKey = null, signature = '';
    let draft = null;              // the layout being edited (null: not editing)
    let shownKeys = [];            // [{btn, key, meta}] of the keys widgets on the page
    const acks = [];               // {command, key, text, timer, resolve, reject}

    // ── the dashboard's helpers ──────────────────────────────────────────
    const esc = (v) => escapeHtml(v);
    const text = (el, v) => setText(el, String(v ?? ''));
    function admin() { return typeof isAdmin === 'function' && isAdmin(); }
    function connected() {
        return typeof socket !== 'undefined' && socket && socket.readyState === WebSocket.OPEN && authState === 'authed';
    }
    function rich() { return typeof richMotion === 'function' && richMotion(); }
    const clone = (v) => JSON.parse(JSON.stringify(v));

    // ── registry ─────────────────────────────────────────────────────────
    const AddonModules = {
        register(def) {
            if (!def || !MODULE_ID.test(def.id || '') || defs.has(def.id)) return;
            def.widgets = def.widgets && typeof def.widgets === 'object' ? def.widgets : {};
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
            if (c) return { ...c, module: m.id, moduleName: m.name, enabled: m.enabled, installed: c.addon ? addonInstalled(c.addon, m) : null };
        }
        return null;
    }

    // Whether an addon's folder is in Interface/AddOns (null: not known: no game folder).
    function addonInstalled(folder, m) {
        if (m && m.addon === folder) return m.installed;
        const owner = server.modules.find(x => x.addon === folder);
        return owner ? owner.installed : null;
    }

    function allCommands() {
        return server ? server.modules.flatMap(m => m.commands.map(c => ({ ...c, module: m.id, moduleName: m.name, enabled: m.enabled }))) : [];
    }

    // The chat text a command would type, its arguments filled in.
    function slash(id, args = {}) {
        const c = commandMeta(id);
        if (!c || !c.template.length) return '';
        const own = (server.settings[c.module] || {});
        const fill = (k) => args[k] ?? (c.args[k].setting ? own[c.args[k].setting] : undefined) ?? c.args[k].default ?? '';
        return c.template.map(line => line.replace(/\{([a-z_]+)\}/g, (m, k) => (k in c.args ? fill(k) : m))).join(' · ');
    }

    function makeApi(id) {
        return {
            id,
            send: (command, args, key) => send(command, args, key),
            request: (command, args) => request(command, args),
            state: (mid = id) => (server && server.state[mid]) || {},
            settings: (mid = id) => (server && server.settings[mid]) || {},
            general: () => ({
                character: (server && server.settings.character) || '',
                role: (server && server.settings.role) || '',
                region: (server && server.settings.region) || 'eu',
            }),
            companion: () => (server && server.companion) || {},
            command: commandMeta,
            slash,
            enabled,
            view: (mid = id) => views.get(mid) || null,
            key: makeKey,
            fader: makeFader,
            pick,
            admin,
            rich,
            esc,
            text,
            editing: () => !!draft,
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
            if (!(m.widgets.length || m.watch || views.has(m.id))) continue;
            if (defs.has(m.id) || loading.has(m.id) || !MODULE_ID.test(m.id)) continue;
            loading.add(m.id);
            const s = document.createElement('script');
            s.src = `static/wow/modules/${m.id}.js`;
            s.onerror = () => { loading.delete(m.id); console.warn('WoW module has no page side:', m.id); };
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

    // A list to choose from, as a dialog plate (the character switcher, the
    // widget picker): rows of a glyph, a name and a quiet line, the current one
    // ticked, a search field over a long list.
    // pick({title, items: [{id, label, sub, glyph, current, dim}], empty, onPick}).
    let pickerEl = null, pickerOpts = null;
    function pick(o) {
        if (!pickerEl) {
            pickerEl = document.createElement('div');
            pickerEl.id = 'wow-picker';
            pickerEl.className = 'modal-overlay';
            pickerEl.innerHTML = '<div class="modal-card is-compact wow-picker-card" role="dialog" aria-modal="true" aria-labelledby="wow-picker-title">'
                + '<h2 class="modal-title" id="wow-picker-title"></h2>'
                + '<input type="search" class="wow-picker-search" placeholder="Search" aria-label="Search" autocomplete="off" spellcheck="false">'
                + '<div class="wow-picker-list" role="listbox"></div><p class="wow-picker-empty" hidden></p>'
                + '<div class="modal-actions"><button class="btn btn-pill btn-ghost" type="button" data-act="close">Cancel</button></div></div>';
            document.body.appendChild(pickerEl);
            const closePicker = () => pickerEl.classList.remove('active');
            pickerEl.querySelector('[data-act="close"]').addEventListener('click', closePicker);
            pickerEl.addEventListener('click', (e) => { if (e.target === pickerEl) closePicker(); });
            pickerEl.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); closePicker(); } });
            pickerEl.querySelector('.wow-picker-search').addEventListener('input', (e) => {
                const q = e.target.value.trim().toLowerCase();
                pickerEl.querySelectorAll('.wow-pick-row').forEach(r => { r.hidden = q && !r.dataset.q.includes(q); });
            });
            pickerEl.querySelector('.wow-picker-list').addEventListener('click', (e) => {
                const row = e.target.closest('.wow-pick-row');
                if (!row) return;
                haptic(HAPTIC_KEY);
                closePicker();
                if (pickerOpts && pickerOpts.onPick && row.getAttribute('aria-selected') !== 'true') pickerOpts.onPick(row.dataset.id);
            });
            if (typeof dialogWatch !== 'undefined') dialogWatch.observe(pickerEl, { attributes: true, attributeFilter: ['class'] });
        }
        pickerOpts = o;
        const items = o.items || [];
        text(pickerEl.querySelector('#wow-picker-title'), o.title || '');
        const search = pickerEl.querySelector('.wow-picker-search');
        search.value = '';
        search.hidden = items.length <= 8;
        pickerEl.querySelector('.wow-picker-list').innerHTML = items.map(it => `<button class="wow-pick-row${it.dim ? ' is-dim' : ''}" type="button" role="option" aria-selected="${it.current ? 'true' : 'false'}" data-id="${esc(it.id)}" data-q="${esc(`${it.label} ${it.sub || ''}`.toLowerCase())}">`
            + (it.glyph ? `<i data-lucide="${esc(it.glyph)}" class="wow-pick-glyph"></i>` : '')
            + `<span class="wow-pick-text"><span class="wow-pick-name">${esc(it.label)}</span><span class="wow-pick-sub">${esc(it.sub || '')}</span></span>`
            + (it.current ? '<i data-lucide="check"></i>' : '') + '</button>').join('');
        pickerEl.querySelector('.wow-picker-list').scrollTop = 0;   // one dialog for every picker: not where the last one was left
        const empty = pickerEl.querySelector('.wow-picker-empty');
        empty.hidden = items.length > 0;
        text(empty, o.empty || '');
        renderIcons(pickerEl);
        pickerEl.classList.add('active');
        (search.hidden ? pickerEl.querySelector('.wow-pick-row, [data-act="close"]') : search).focus({ preventScroll: true });
    }

    // ── commands ─────────────────────────────────────────────────────────
    function send(command, args, key) {
        return post(command, args, key, null);
    }

    // A command whose answer carries data for this screen (an item search).
    function request(command, args) {
        return new Promise((resolve, reject) => post(command, args, null, { resolve, reject }));
    }

    function post(command, args, key, waiter) {
        const c = commandMeta(command);
        const label = (c && (slash(command, args || {}) || c.label)) || command;
        if (!connected()) {
            if (key) keyRefused(key);
            setLast('error', `Not sent · ${REASONS.offline}`);
            if (waiter) waiter.reject('offline');
            return;
        }
        sendMessage(args ? { type: 'wow_addon_action', command, args } : { type: 'wow_addon_action', command });
        if (!waiter) setLast('pending', `${label} · sending…`);
        const entry = { command, key, text: label, quiet: !!waiter, ...(waiter || {}) };
        entry.timer = setTimeout(() => answer({ command, status: 'error', reason: 'timeout' }), ACK_TIMEOUT_MS);
        acks.push(entry);
    }

    // Answers come in the order the PC took the commands.
    function answer(d) {
        const i = acks.findIndex(a => a.command === d.command);
        const entry = i >= 0 ? acks.splice(i, 1)[0] : null;
        if (entry) clearTimeout(entry.timer);
        if (entry && entry.resolve) {
            if (d.status === 'sent') entry.resolve(d.data ?? null); else entry.reject(d.reason || 'failed');
            return;
        }
        // A module may word the outcome itself ("No note on the PC's clipboard").
        const c = commandMeta(d.command);
        const api = c && apis.get(c.module);
        const own = api && api.onAck ? api.onAck(d) : null;
        if (d.status === 'sent') {
            setLast('ok', typeof own === 'string' ? own : `${entry ? entry.text : d.command} · sent`);
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
            + '<div class="wow-edit-acts"><button class="btn btn-pill btn-ghost" type="button" data-act="reset"><i data-lucide="rotate-ccw"></i>Start over</button>'
            + '<button class="btn btn-pill btn-primary" type="button" data-act="done"><i data-lucide="check"></i>Done</button></div>'
            + '<button class="btn btn-top wow-edit" type="button" aria-label="Arrange the page"><i data-lucide="pencil"></i></button>'
            + '<button class="btn btn-top wow-gear" type="button" aria-label="WoW settings" hidden><i data-lucide="settings"></i></button>'
            + '</div></header>'
            + '<div class="wow-grid">' + [0, 1, 2].map(c => `<div class="wow-col" data-col="${c}"></div>`).join('') + '</div>'
            + '<p class="wow-off" hidden>Nothing on this page. Tap the pencil to add plates.</p>';
        renderIcons(view);
        gridEl = view.querySelector('.wow-grid');
        colEls = [...view.querySelectorAll('.wow-col')];
        barEl = view.querySelector('.wow-bar');
        lastEl = view.querySelector('.wow-last');
        gearEl = view.querySelector('.wow-gear');
        editEl = view.querySelector('.wow-edit');
        offEl = view.querySelector('.wow-off');
        view.querySelector('.wow-back').addEventListener('click', () => close());
        gearEl.addEventListener('click', () => openSettings());
        editEl.addEventListener('click', () => startEdit());
        view.querySelector('[data-act="done"]').addEventListener('click', () => endEdit(true));
        view.querySelector('[data-act="reset"]').addEventListener('click', resetLayout);
        view.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape' || document.querySelector('.modal-overlay.active')) return;
            if (draft) endEdit(true); else close();
        });
        document.body.appendChild(view);
        layout();
    }

    function liveLayout() {
        return (server && server.settings.layout) || { columns: [[], [], []] };
    }
    function currentLayout() {
        return draft || liveLayout();
    }

    // What a cell holds: a module's widget, a keys widget or nothing.
    function widgetMeta(w) {
        if (w === 'keys') return { name: 'Keys', sizes: ['half', 'full'], grow: false, keys: true };
        if (!w || !server) return null;
        const [mid, wid] = w.split('.');
        const m = server.modules.find(x => x.id === mid);
        const meta = m && m.widgets.find(x => x.id === wid);
        return meta ? { ...meta, module: mid, moduleName: m.name, enabled: m.enabled } : null;
    }

    // Rows of a column: a full cell alone, two half cells side by side.
    function pack(cells) {
        const rows = [];
        for (let i = 0; i < cells.length; i++) {
            const c = cells[i];
            if (c.size === 'half' && cells[i + 1] && cells[i + 1].size === 'half') {
                rows.push([i, i + 1]);
                i++;
            } else {
                rows.push([i]);
            }
        }
        return rows;
    }

    // Lays every plate and key out again. Runs when a module registers, the
    // layout or the modules change, and on every edit; state changes only update.
    function layout() {
        if (!view) return;
        shownKeys = [];
        const editing = !!draft;
        view.dataset.editing = String(editing);
        const lay = currentLayout();
        let any = false;
        lay.columns.slice(0, COLUMNS).forEach((cells, c) => {
            const col = colEls[c];
            col.replaceChildren();
            // Outside edit mode a blank, or a widget whose module is off or unknown, takes no room.
            const shown = cells.map((cell, i) => ({ cell, i })).filter(({ cell }) => {
                if (editing) return true;
                if (!cell.w) return false;
                const meta = widgetMeta(cell.w);
                return meta && (meta.keys || meta.enabled);
            });
            const rows = pack(shown.map(s => s.cell));
            // A growing widget (a list) takes a share of the spare height and
            // scrolls inside; the others keep their content's height.
            const growRow = rows.map(r => (r.some(n => (widgetMeta(shown[n].cell.w) || {}).grow) ? 'minmax(var(--wow-grow-min), 1fr)' : ''));
            const tracks = [];
            rows.forEach((r, n) => {
                tracks.push(growRow[n] || 'auto');
                r.forEach((k, side) => {
                    const { cell, i } = shown[k];
                    const el = buildCell(c, i, cell);
                    el.style.gridRow = String(n + 1);
                    el.style.gridColumn = r.length === 2 ? String(side + 1) : (cell.size === 'half' && editing ? '1' : '1 / -1');
                    col.appendChild(el);
                });
                // A half alone in edit mode: its row's other half can take another.
                if (editing && r.length === 1 && shown[r[0]].cell.size === 'half') {
                    const fill = addTile('Add half', () => addCell(c, shown[r[0]].i + 1, 'half'));
                    fill.style.gridRow = String(n + 1);
                    fill.style.gridColumn = '2';
                    col.appendChild(fill);
                }
            });
            if (editing) {
                const add = addTile('Add a plate', () => addCell(c, cells.length, 'full'));
                add.style.gridRow = String(rows.length + 1);
                add.style.gridColumn = '1 / -1';
                col.appendChild(add);
                tracks.push('auto');
            }
            col.style.gridTemplateRows = tracks.join(' ');
            col.hidden = !editing && !shown.length;
            any = any || shown.length > 0;
        });
        offEl.hidden = any || editing || !server;
        renderIcons(view);
        refresh();
        if (server) for (const [id, data] of views) {
            const def = defs.get(id);
            if (def && def.onUpdate) def.onUpdate(data, apis.get(id));
        }
    }

    function addTile(label, onTap) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'wow-add-tile';
        b.innerHTML = `<i data-lucide="plus"></i><span>${esc(label)}</span>`;
        b.addEventListener('click', () => { haptic(HAPTIC_KEY); onTap(); });
        return b;
    }

    function plate(title) {
        const sec = document.createElement('section');
        sec.className = 'card panel wow-sec wow-cell';
        const h = document.createElement('div');
        h.className = 'panel-head';
        h.innerHTML = `<h2 class="panel-title">${esc(title)}</h2><div class="wow-head-keys"></div>`;
        sec.appendChild(h);
        sec.setAttribute('aria-label', title);
        return { sec, head: h.querySelector('.wow-head-keys') };
    }

    function buildCell(c, i, cell) {
        const editing = !!draft;
        if (!cell.w) {
            // A blank (edit mode only): where a plate was removed. Tapped, it takes another;
            // Done drops the ones left empty. Its tray above its tile, never over the words.
            const box = document.createElement('div');
            box.className = 'wow-cell is-blank';
            box.append(cellTools(c, i, cell, null), addTile('Empty: choose a plate', () => fillCell(c, i)));
            return box;
        }
        const meta = widgetMeta(cell.w);
        let title = meta ? meta.name : 'Not available';
        if (cell.w === 'keys') title = cell.title || 'Keys';
        const { sec, head } = plate(title);
        sec.dataset.w = cell.w;
        sec.classList.toggle('is-grow', !!(meta && meta.grow));
        if (cell.w === 'keys') {
            renderKeys(sec, head, c, i, cell);
        } else if (!meta) {
            sec.insertAdjacentHTML('beforeend', '<p class="wow-cell-note">This plate’s module is not on this PC any more.</p>');
        } else if (!meta.enabled) {
            sec.insertAdjacentHTML('beforeend', `<p class="wow-cell-note">${esc(meta.moduleName)} is off. Turn it on in WoW settings on the PC.</p>`);
        } else {
            const def = defs.get(meta.module);
            const part = def && def.widgets[cell.w.split('.')[1]];
            if (part) {
                sec.dataset.module = meta.module;
                part.render(sec, apis.get(meta.module), head);
            } else {
                sec.insertAdjacentHTML('beforeend', '<p class="wow-cell-note">Loading…</p>');
            }
        }
        if (editing) {
            if (cell.w !== 'keys') sec.insertAdjacentHTML('beforeend', '<div class="wow-cell-shield" aria-hidden="true"></div>');
            sec.querySelector('.panel-head').appendChild(cellTools(c, i, cell, meta));
        }
        return sec;
    }

    // Edit mode: a slim tray over the plate's head: move, size, remove.
    function cellTools(c, i, cell, meta) {
        const cells = draft.columns[c];
        const sizes = cell.w ? (meta ? meta.sizes : ['full']) : ['half', 'full'];
        const tools = document.createElement('div');
        tools.className = 'wow-cell-tools';
        const btn = (act, glyph, label, off) => `<button type="button" data-act="${act}" aria-label="${esc(label)}"${off ? ' disabled' : ''}><i data-lucide="${glyph}"></i></button>`;
        tools.innerHTML = btn('left', 'arrow-left', 'Move to the column on the left', c === 0)
            + btn('up', 'arrow-up', 'Move up', i === 0)
            + btn('down', 'arrow-down', 'Move down', i === cells.length - 1)
            + btn('right', 'arrow-right', 'Move to the column on the right', c === COLUMNS - 1)
            + (sizes.length > 1 ? btn('size', cell.size === 'half' ? 'maximize-2' : 'minimize-2', cell.size === 'half' ? 'Full width' : 'Half width') : '')
            + btn('remove', cell.w ? 'x' : 'trash-2', cell.w ? 'Remove (leaves room for another)' : 'Remove this space');
        tools.addEventListener('click', (e) => {
            const b = e.target.closest('button[data-act]');
            if (!b || b.disabled) return;
            e.stopPropagation();
            haptic(HAPTIC_KEY);
            editCell(c, i, b.dataset.act);
        });
        return tools;
    }

    function editCell(c, i, act) {
        const cells = draft.columns[c];
        const cell = cells[i];
        if (act === 'up' || act === 'down') {
            const j = act === 'up' ? i - 1 : i + 1;
            [cells[i], cells[j]] = [cells[j], cells[i]];
        } else if (act === 'left' || act === 'right') {
            const to = draft.columns[act === 'left' ? c - 1 : c + 1];
            cells.splice(i, 1);
            to.splice(Math.min(i, to.length), 0, cell);
        } else if (act === 'size') {
            cell.size = cell.size === 'half' ? 'full' : 'half';
        } else if (act === 'remove') {
            // A widget leaves a blank of its size; a blank goes.
            if (cell.w) cells[i] = { w: null, size: cell.size };
            else cells.splice(i, 1);
        }
        layout();
    }

    function startEdit() {
        if (!server || draft) return;
        draft = clone(liveLayout());
        haptic(HAPTIC_KEY);
        layout();
    }

    function endEdit(save) {
        if (!draft) return;
        const out = { ...draft, columns: draft.columns.map(col => col.filter(cell => cell.w)) };   // blanks were for editing
        draft = null;
        if (save && JSON.stringify(out) !== JSON.stringify(liveLayout())) saveLayout(out);
        layout();
    }

    function saveLayout(lay) {
        if (!connected()) {
            tdNotice({ title: 'Not saved', text: 'The PC is not connected. Arrange the page again once it is.', icon: 'wifi-off', tone: 'warning' });
            return;
        }
        server.settings.layout = lay;   // shown at once; the PC's cleaned copy follows
        server.settings.layout_default = false;
        sendMessage({ type: 'wow_layout_save', layout: lay });
    }

    async function resetLayout() {
        const ok = await tdConfirm({ title: 'Start the page over?', text: 'Every plate and key goes back to where it was at first. Custom commands and key slots stay.', icon: 'rotate-ccw', tone: 'danger', confirm: 'Start over' });
        if (!ok) return;
        draft = null;
        sendMessage({ type: 'wow_layout_save', layout: null });
        layout();
    }

    // ── choosing a plate ─────────────────────────────────────────────────
    function placedWidgets(lay) {
        return new Set(lay.columns.flat().map(c => c.w).filter(w => w && w !== 'keys'));
    }

    function widgetChoices(size) {
        const placed = placedWidgets(currentLayout());
        const out = [];
        for (const m of server.modules) {
            for (const w of m.widgets) {
                const id = `${m.id}.${w.id}`;
                if (placed.has(id) || (size === 'half' && !w.sizes.includes('half'))) continue;
                out.push({ id, label: w.name, glyph: 'layout-panel-top', dim: !m.enabled,
                           sub: (m.enabled ? '' : `${m.name} is off · `) + (w.description || m.name) });
            }
        }
        out.push({ id: 'keys', label: 'Keys', glyph: 'layout-grid', sub: 'A grid of keys you choose, empty or from a template: world markers, raid lead, callouts…' });
        return out;
    }

    function newCell(id, size) {
        if (id === 'keys') return { w: 'keys', size, id: `k${Date.now().toString(36)}`, title: 'Keys', keys: [null, null, null, null] };
        const meta = widgetMeta(id);
        return { w: id, size: meta && meta.sizes.includes(size) ? size : (meta ? meta.sizes[0] : 'full') };
    }

    // Outside edit mode a pick is saved at once.
    function withLayout(change) {
        const editing = !!draft;
        const lay = editing ? draft : clone(liveLayout());
        change(lay);
        if (editing) layout(); else { saveLayout(lay); layout(); }
    }

    // A keys plate starts empty or from a template: a module's premade keys
    // (world markers, raid lead, callouts…), edited from there like any.
    function chooseCell(id, size, done) {
        const templates = (server && server.templates) || [];
        if (id !== 'keys' || !templates.length) { done(newCell(id, size)); return; }
        pick({
            title: 'Start the keys from',
            items: [{ id: 'empty', label: 'Empty keys', glyph: 'layout-grid', sub: 'Four keys to fill with any command' },
                ...templates.map(t => ({ id: t.id, label: t.title, glyph: t.glyph || 'layout-grid',
                                         sub: `${t.description} · ${t.keys.length} keys` }))],
            onPick: (tid) => {
                const cell = newCell('keys', size);
                const t = templates.find(x => x.id === tid);
                if (t) {
                    cell.title = t.title.slice(0, 24);
                    cell.keys = t.keys.map(k => ({ ...k, args: k.args ? { ...k.args } : undefined }));
                }
                done(cell);
            },
        });
    }

    function fillCell(c, i) {
        const cell = currentLayout().columns[c][i];
        pick({
            title: 'Choose a plate', items: widgetChoices(cell.size),
            onPick: (id) => chooseCell(id, cell.size, (made) => withLayout(lay => { lay.columns[c][i] = made; })),
        });
    }

    function addCell(c, at, size) {
        pick({
            title: size === 'half' ? 'Choose a half-width plate' : 'Choose a plate', items: widgetChoices(size),
            onPick: (id) => chooseCell(id, size, (made) => withLayout(lay => { lay.columns[c].splice(at, 0, made); })),
        });
    }

    // ── keys widgets ─────────────────────────────────────────────────────
    function renderKeys(sec, head, c, i, cell) {
        const editing = !!draft;
        sec.classList.add('wow-keys-sec');
        sec.dataset.size = cell.size;
        if (editing) {
            const title = sec.querySelector('.panel-title');
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'wow-keys-title';
            input.maxLength = LABEL_MAX;
            input.value = cell.title || '';
            input.placeholder = 'Keys';
            input.setAttribute('aria-label', 'Name of these keys');
            input.addEventListener('input', () => { draft.columns[c][i].title = input.value.replace(/["\\|]/g, '').slice(0, LABEL_MAX); });
            title.replaceWith(input);
        }
        const grid = document.createElement('div');
        grid.className = 'wow-keys';
        (cell.keys || []).forEach((key, k) => grid.appendChild(keyButton(c, i, k, key)));
        if (editing && (cell.keys || []).length < 48) {
            const add = addTile('Add a key', () => {
                draft.columns[c][i].keys = [...(draft.columns[c][i].keys || []), null];
                openKeyEditor(c, i, draft.columns[c][i].keys.length - 1);
            });
            add.classList.add('wow-key-add');
            grid.appendChild(add);
        }
        sec.appendChild(grid);
    }

    function keyButton(c, i, k, key) {
        const meta = key ? commandMeta(key.cmd) : null;
        const b = makeKey({
            glyph: key ? (key.glyph || (meta && meta.glyph) || 'box') : 'plus',
            label: key ? (key.label || (meta ? meta.label : 'Unknown')) : (draft ? 'Empty' : ''),
            value: key && meta ? slash(key.cmd, key.args || {}) : '',
        });
        b.classList.toggle('is-empty', !key);
        if (meta && !meta.enabled) b.classList.add('is-off');
        let held = false, timer = 0;
        b.addEventListener('pointerdown', (e) => {
            if (!e.isPrimary || draft) return;
            held = false;
            timer = setTimeout(() => { held = true; haptic(HAPTIC_END); openKeyEditor(c, i, k); }, HOLD_MS);
        });
        const cancel = () => clearTimeout(timer);
        b.addEventListener('pointerup', cancel);
        b.addEventListener('pointerleave', cancel);
        b.addEventListener('pointercancel', cancel);
        b.addEventListener('click', () => {
            if (held) { held = false; return; }
            if (draft || !key) { openKeyEditor(c, i, k); return; }
            if (!meta) { keyRefused(b); setLast('error', `Not sent · ${REASONS.unknown}`); return; }
            if (!meta.enabled) { keyRefused(b); setLast('error', `Not sent · ${REASONS.disabled}`); return; }
            send(key.cmd, key.args && Object.keys(key.args).length ? key.args : null, b);
        });
        if (key && meta) shownKeys.push({ btn: b, key, meta });
        return b;
    }

    // Keys sending a module's command show its state (logging, unlocked, a length).
    function refreshKeys() {
        for (const { btn, key, meta } of shownKeys) {
            const def = defs.get(meta.module);
            const st = def && def.keyState ? def.keyState(key.cmd, key.args || {}, apis.get(meta.module)) : null;
            btn.setOn(!!(st && st.on));
            btn.setRec(!!(st && st.rec));
            if (st && st.mark) btn.dataset.mark = st.mark; else delete btn.dataset.mark;   // a marker's colour
            btn.setValue(st && st.value !== undefined ? st.value : slash(key.cmd, key.args || {}));
            if (st && st.label && !key.label) btn.setLabel(st.label);
            btn.disabled = !draft && !!(st && st.disabled);
        }
    }

    // ── the key editor ───────────────────────────────────────────────────
    let kedEl = null, ked = null;    // ked: {c, i, k, sel, args, label, glyph, slot, custom}

    function buildKeyEditor() {
        kedEl = document.createElement('div');
        kedEl.id = 'wow-keyed';
        kedEl.className = 'modal-overlay';
        kedEl.innerHTML = '<div class="modal-card wow-keyed-card" role="dialog" aria-modal="true" aria-labelledby="wow-keyed-title">'
            + '<h2 class="modal-title" id="wow-keyed-title">Key</h2>'
            + '<div class="wow-keyed">'
            + '<div class="wow-keyed-side">'
            + '<div class="wow-keyed-preview"></div>'
            + '<div class="form-group"><label for="wow-keyed-label">Name on the key</label><input type="text" id="wow-keyed-label" maxlength="24" autocomplete="off" spellcheck="false"></div>'
            + '<div class="wow-keyed-args"></div>'
            + '<div class="wow-keyed-custom" hidden>'
            + '<div class="form-group"><label for="wow-keyed-lines">Command (one a line)</label><textarea id="wow-keyed-lines" rows="3" spellcheck="false" placeholder="/details toggle"></textarea></div>'
            + '<div class="form-group"><label for="wow-keyed-via">Sent</label><select id="wow-keyed-via"><option value="auto">As its first word says</option><option value="addon">Through the Touch Dashboard addon</option><option value="game">Typed into chat (Blizzard’s commands)</option></select></div>'
            + '</div>'
            + '<div class="form-group wow-keyed-slot"><label for="wow-keyed-slot">Key slot</label><select id="wow-keyed-slot"></select><p class="settings-sub wow-keyed-slot-note"></p></div>'
            + '<div class="form-group"><span class="form-label">Glyph</span><div class="wow-glyphs" role="radiogroup" aria-label="Glyph"></div></div>'
            + '</div>'
            + '<div class="wow-keyed-main">'
            + '<input type="search" class="wow-keyed-search" placeholder="Search every command" aria-label="Search commands" autocomplete="off" spellcheck="false">'
            + '<div class="wow-keyed-cats" role="group" aria-label="Kinds of command"></div>'
            + '<div class="wow-keyed-list" role="listbox" aria-label="Commands"></div>'
            + '</div></div>'
            + '<div class="modal-actions"><button class="btn btn-pill btn-ghost wow-keyed-remove" type="button" data-act="remove"><i data-lucide="trash-2"></i>Empty this key</button>'
            + '<span class="wow-keyed-gap"></span>'
            + '<button class="btn btn-pill btn-ghost" type="button" data-act="cancel">Cancel</button>'
            + '<button class="btn btn-pill btn-primary" type="button" data-act="save"><i data-lucide="check"></i>Save</button></div></div>';
        document.body.appendChild(kedEl);
        const q = (s) => kedEl.querySelector(s);
        const closeEd = () => { kedEl.classList.remove('active'); dropEmptyNew(); };
        q('[data-act="cancel"]').addEventListener('click', closeEd);
        kedEl.addEventListener('click', (e) => { if (e.target === kedEl) closeEd(); });
        kedEl.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); closeEd(); } });
        q('[data-act="save"]').addEventListener('click', saveKey);
        q('[data-act="remove"]').addEventListener('click', () => { ked.sel = null; saveKey(true); });
        q('.wow-keyed-search').addEventListener('input', drawCommandList);
        q('.wow-keyed-cats').addEventListener('click', (e) => {
            const chip = e.target.closest('.wow-cat');
            if (!chip) return;
            ked.cat = chip.dataset.cat;
            drawCommandList();
        });
        q('.wow-keyed-list').addEventListener('click', (e) => {
            const row = e.target.closest('.wow-cmd-row');
            if (!row) return;
            haptic(HAPTIC_KEY);
            choose(row.dataset.id);
        });
        q('#wow-keyed-label').addEventListener('input', (e) => { ked.label = e.target.value; drawPreview(); });
        q('#wow-keyed-lines').addEventListener('input', drawPreview);
        q('.wow-glyphs').addEventListener('click', (e) => {
            const g = e.target.closest('.wow-glyph');
            if (!g) return;
            ked.glyph = g.dataset.glyph;
            kedEl.querySelectorAll('.wow-glyph').forEach(x => x.setAttribute('aria-checked', String(x === g)));
            drawPreview();
        });
        if (typeof dialogWatch !== 'undefined') dialogWatch.observe(kedEl, { attributes: true, attributeFilter: ['class'] });
    }

    // Commands a key can send: not the ones needing an id or a note from a list.
    function keyCommands() {
        return allCommands().filter(c => !Object.values(c.args).some(a => a.kind === 'id' || a.kind === 'note'));
    }

    function openKeyEditor(c, i, k) {
        if (!server) return;
        if (!kedEl) buildKeyEditor();
        const lay = currentLayout();
        const key = (lay.columns[c][i].keys || [])[k] || null;
        const meta = key ? commandMeta(key.cmd) : null;
        ked = {
            c, i, k, isNew: !key, cat: 'all', sel: key ? key.cmd : null, args: { ...(key && key.args) },
            label: key && key.label || '', glyph: key && key.glyph || '',
            slot: key ? slotOf(key.cmd, key.args || {}) : '',
        };
        const q = (s) => kedEl.querySelector(s);
        text(q('#wow-keyed-title'), key ? 'Edit key' : 'New key');
        q('.wow-keyed-search').value = '';
        q('#wow-keyed-label').value = ked.label;
        q('.wow-keyed-remove').hidden = !key;
        // Categories in the modules' order.
        const cats = [];
        for (const cmd of keyCommands()) if (!cats.includes(cmd.category)) cats.push(cmd.category);
        q('.wow-keyed-cats').innerHTML = ['all', ...cats].map(cat => `<button class="wow-cat" type="button" data-cat="${esc(cat)}" aria-pressed="${cat === 'all'}">${esc(cat === 'all' ? 'All' : cat)}</button>`).join('');
        const glyphs = [...new Set([...allCommands().map(x => x.glyph).filter(Boolean), ...GLYPHS])];
        q('.wow-glyphs').innerHTML = glyphs.map(g => `<button type="button" class="wow-glyph" role="radio" data-glyph="${esc(g)}" aria-label="${esc(g)}" aria-checked="${g === ked.glyph}"><i data-lucide="${esc(g)}"></i></button>`).join('');
        const slotSel = q('#wow-keyed-slot');
        slotSel.innerHTML = '<option value="">No slot</option>' + server.slots.map(s => {
            const kb = server.settings.keybinds[s];
            const taken = kb && !(ked.sel && kb.cmd === ked.sel && sameArgs(kb.args, ked.args));
            const m = kb && commandMeta(kb.cmd);
            return `<option value="${s}"${ked.slot === s ? ' selected' : ''}>Ctrl+Alt+Shift+${s}${taken ? ` (now ${esc(m ? m.label : kb.cmd)})` : ''}</option>`;
        }).join('');
        slotSel.disabled = !admin();
        slotSel.onchange = () => { ked.slot = slotSel.value; };
        text(q('.wow-keyed-slot-note'), admin()
            ? (server.companion && server.companion.installed ? 'One key press in game, in combat too: the Touch Dashboard addon holds the command.' : 'One key press in game, in combat too. In WoW, make a macro with this key’s command and bind it to these keys.')
            : 'Set on the PC.');
        drawCommandList();
        choose(ked.sel, true);
        renderIcons(kedEl);
        kedEl.classList.add('active');
        q('.wow-keyed-search').focus({ preventScroll: true });
        // The command the key sends now, in view: it was often far down the
        // list (a raid warning under Chat), so nothing showed it chosen.
        const list = q('.wow-keyed-list');
        const row = list.querySelector('.wow-cmd-row[aria-selected="true"]');
        if (row) list.scrollTop = Math.max(0, row.offsetTop - list.offsetTop - list.clientHeight / 3);
    }

    // A new key left empty is not kept.
    function dropEmptyNew() {
        if (!ked || !ked.isNew || !draft) return;
        const keys = draft.columns[ked.c][ked.i].keys;
        if (keys && keys[ked.k] === null && ked.k === keys.length - 1) keys.pop();
        layout();
    }

    function drawCommandList() {
        const q = kedEl.querySelector('.wow-keyed-search').value.trim().toLowerCase();
        kedEl.querySelectorAll('.wow-cat').forEach(p => p.setAttribute('aria-pressed', String(p.dataset.cat === ked.cat)));
        let rows = keyCommands().filter(c => (ked.cat === 'all' || c.category === ked.cat)
            && (!q || `${c.label} ${c.template.join(' ')} ${c.category} ${c.moduleName}`.toLowerCase().includes(q)));
        const html = [];
        if (admin() && (ked.cat === 'all' || ked.cat === 'Custom commands') && !q) {
            html.push('<button class="wow-cmd-row" type="button" role="option" data-id="__new"' + (ked.sel === '__new' ? ' aria-selected="true"' : '') + '>'
                + '<i data-lucide="terminal"></i><span class="wow-cmd-text"><span class="wow-cmd-name">New custom command</span>'
                + '<span class="wow-cmd-slash">Any slash command, for an addon not listed here</span></span></button>');
        }
        rows = rows.slice(0, 200);
        for (const c of rows) {
            const badges = [];
            if (!c.enabled) badges.push('Off');
            if (c.addon && addonInstalled(c.addon) === false) badges.push('Not installed');
            if (!c.verified) badges.push('Unchecked');
            html.push(`<button class="wow-cmd-row" type="button" role="option" data-id="${esc(c.id)}" aria-selected="${c.id === ked.sel}">`
                + `<i data-lucide="${esc(c.glyph || 'box')}"></i><span class="wow-cmd-text"><span class="wow-cmd-name">${esc(c.label)}</span>`
                + `<span class="wow-cmd-slash">${esc(c.template.join(' · ') || c.description || c.moduleName)}</span></span>`
                + badges.map(b => `<span class="wow-cmd-badge">${esc(b)}</span>`).join('') + '</button>');
        }
        if (!html.length) html.push('<p class="wow-picker-empty">No command matches.</p>');
        const list = kedEl.querySelector('.wow-keyed-list');
        list.innerHTML = html.join('');
        renderIcons(list);
    }

    function choose(id, keepFields) {
        const q = (s) => kedEl.querySelector(s);
        if (!keepFields && id !== ked.sel) ked.args = {};
        ked.sel = id;
        kedEl.querySelectorAll('.wow-cmd-row').forEach(r => r.setAttribute('aria-selected', String(r.dataset.id === id)));
        const meta = id && id !== '__new' ? commandMeta(id) : null;
        const isCustom = id === '__new' || (meta && meta.module === 'custom');
        q('.wow-keyed-custom').hidden = !isCustom;
        const lines = q('#wow-keyed-lines'), via = q('#wow-keyed-via');
        lines.disabled = via.disabled = !admin();
        if (isCustom) {
            const own = meta ? (server.settings.custom || []).find(x => x.id === id) : null;
            lines.value = own ? own.lines.join('\n') : (meta ? meta.template.join('\n') : '');
            via.value = own ? own.via : 'auto';
        }
        // Its arguments: a value of each kind, empty for the default.
        const argsEl = q('.wow-keyed-args');
        argsEl.innerHTML = '';
        for (const [name, a] of Object.entries(meta ? meta.args : {})) {
            const id2 = `wow-keyed-arg-${name}`;
            const label = a.label || name;
            const value = ked.args[name] ?? '';
            let input;
            if (a.kind === 'int') {
                input = `<input type="number" id="${id2}" min="${a.lo}" max="${a.hi}" step="1" value="${esc(value)}" placeholder="${esc(a.default ?? (a.setting ? 'From settings' : ''))}">`;
            } else if (a.kind === 'choice') {
                const choices = a.choices || (server.settings[meta.module] || {})[a.setting] || [];
                input = `<select id="${id2}"><option value="">Choose…</option>${choices.map(ch => `<option${ch === value ? ' selected' : ''}>${esc(ch)}</option>`).join('')}</select>`;
            } else if (a.kind === 'bool') {
                input = `<label class="switch"><input type="checkbox" id="${id2}"${value ? ' checked' : ''}><span class="slider"></span></label>`;
            } else {
                input = `<input type="text" id="${id2}" maxlength="${a.max_len || 40}" value="${esc(value)}" autocomplete="off" spellcheck="false" placeholder="${esc(a.default ?? '')}">`;
            }
            argsEl.insertAdjacentHTML('beforeend', `<div class="form-group"><label for="${id2}">${esc(label)}${a.kind === 'int' ? ` (${a.lo}–${a.hi})` : ''}</label>${input}</div>`);
            const el = argsEl.querySelector(`#${id2}`);
            el.addEventListener(a.kind === 'bool' || a.kind === 'choice' ? 'change' : 'input', () => {
                const v = a.kind === 'bool' ? el.checked : el.value.trim();
                if (v === '' || v === false) delete ked.args[name];
                else ked.args[name] = a.kind === 'int' ? Number(v) : v;
                drawPreview();
            });
        }
        q('#wow-keyed-label').placeholder = meta ? meta.label : (id === '__new' ? 'Name it' : '');
        if (!ked.glyph && meta) {
            kedEl.querySelectorAll('.wow-glyph').forEach(x => x.setAttribute('aria-checked', String(x.dataset.glyph === meta.glyph)));
        }
        drawPreview();
    }

    function drawPreview() {
        const box = kedEl.querySelector('.wow-keyed-preview');
        const meta = ked.sel && ked.sel !== '__new' ? commandMeta(ked.sel) : null;
        const glyph = ked.glyph || (meta && meta.glyph) || (ked.sel === '__new' ? 'terminal' : 'plus');
        const label = ked.label.trim() || (meta ? meta.label : (ked.sel === '__new' ? 'Custom' : 'Empty'));
        let value = meta ? slash(ked.sel, ked.args) : '';
        if (ked.sel === '__new' || (meta && meta.module === 'custom')) value = kedEl.querySelector('#wow-keyed-lines').value.split('\n')[0] || '';
        const k = makeKey({ glyph, label, value });
        k.tabIndex = -1;
        k.setAttribute('aria-hidden', 'true');
        box.replaceChildren(k);
    }

    function slotOf(cmd, args) {
        if (!server) return '';
        for (const [s, kb] of Object.entries(server.settings.keybinds || {})) if (kb.cmd === cmd && sameArgs(kb.args, args)) return s;
        return '';
    }
    function sameArgs(a, b) {
        const ka = Object.keys(a || {}).filter(k => a[k] !== '' && a[k] != null).sort();
        const kb = Object.keys(b || {}).filter(k => b[k] !== '' && b[k] != null).sort();
        return ka.length === kb.length && ka.every((k, n) => k === kb[n] && String(a[k]) === String(b[k]));
    }

    function saveKey(empty) {
        const q = (s) => kedEl.querySelector(s);
        let cmd = empty === true ? null : ked.sel;
        const settings = {};
        if (cmd === '__new' || (cmd && commandMeta(cmd) && commandMeta(cmd).module === 'custom' && admin())) {
            const lines = q('#wow-keyed-lines').value.split('\n').map(l => l.trim()).filter(Boolean);
            if (!lines.length || lines.some(l => !l.startsWith('/') || l.length > 255)) {
                tdNotice({ title: 'Not a command', text: 'Each line starts with / and is at most 255 characters, as you would type it in WoW.', icon: 'terminal', tone: 'warning' });
                return;
            }
            const custom = clone(server.settings.custom || []);
            const label = ked.label.replace(/["\\|]/g, '').trim().slice(0, LABEL_MAX) || lines[0].slice(0, LABEL_MAX);
            const entry = { id: cmd === '__new' ? `c_${Math.random().toString(36).slice(2, 10)}` : cmd, label, glyph: ked.glyph || 'terminal', lines, via: q('#wow-keyed-via').value };
            const at = custom.findIndex(x => x.id === entry.id);
            if (at >= 0) custom[at] = entry; else custom.push(entry);
            settings.custom = custom;
            cmd = entry.id;
        }
        if (cmd && cmd !== '__new' && !commandMeta(cmd) && !settings.custom) return;
        const meta = cmd && commandMeta(cmd);
        const key = cmd ? { cmd } : null;
        if (key) {
            const args = Object.fromEntries(Object.entries(ked.args).filter(([, v]) => v !== '' && v != null));
            if (Object.keys(args).length) key.args = args;
            const label = ked.label.replace(/["\\|]/g, '').trim().slice(0, LABEL_MAX);
            if (label && (!meta || label !== meta.label)) key.label = label;
            if (ked.glyph && (!meta || ked.glyph !== meta.glyph)) key.glyph = ked.glyph;
        }
        // The slot (PC only): this key's command and arguments on it, nowhere else.
        if (admin()) {
            const binds = clone(server.settings.keybinds || {});
            const before = JSON.stringify(binds);
            for (const [s, kb] of Object.entries(binds)) if (key && kb.cmd === key.cmd && sameArgs(kb.args, key.args)) delete binds[s];
            if (key && ked.slot) binds[ked.slot] = { cmd: key.cmd, args: key.args || {} };
            if (JSON.stringify(binds) !== before) settings.keybinds = binds;
        }
        if (Object.keys(settings).length) sendMessage({ type: 'wow_settings_save', wow_addons: settings });
        ked.isNew = false;
        kedEl.classList.remove('active');
        withLayout(lay => {
            const cell = lay.columns[ked.c][ked.i];
            cell.keys = cell.keys || [];
            cell.keys[ked.k] = key;
            // An emptied key at the end goes in edit mode.
            if (!key && draft && ked.k === cell.keys.length - 1) cell.keys.pop();
        });
    }

    // Shows what is on, then lets each module bring its parts up to date.
    function refresh() {
        if (!view) return;
        gearEl.hidden = !admin() || !!draft;
        editEl.hidden = !!draft || !server;
        for (const [id, def] of defs) if (def.update) def.update(apis.get(id));
        refreshKeys();
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
        if (draft) endEdit(true);
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
    function layoutSignature(d) {
        return JSON.stringify([d.modules.map(m => [m.id, m.enabled, m.widgets.map(w => w.id), m.commands.length]), d.settings.layout, d.settings.keybinds]);
    }

    function onMessage(msg) {
        const d = msg.data;
        if (msg.type === 'wow_state') {
            server = d;
            const sig = layoutSignature(d);
            const relayout = sig !== signature;
            signature = sig;
            loadModules();
            if (view) relayout ? layout() : refresh();
            if (settingsEl && settingsEl.classList.contains('active')) renderFiles();
            return;
        }
        if (msg.type === 'wow_live') {
            if (!server) return;
            server.state = d.state;
            server.companion = { ...server.companion, ...d.companion };
            refresh();
            return;
        }
        if (msg.type === 'wow_cmd_ack') {
            answer(d || {});
            return;
        }
        if (msg.type === 'wow_companion_result') {
            tdNotice(d.ok
                ? { title: 'Touch Dashboard addon installed', text: `Version ${d.version}. In WoW, /reload (or log in) and make sure it is on in the AddOns list.`, icon: 'check-circle-2', tone: 'positive' }
                : { title: 'Not installed', text: REASONS[d.reason] || REASONS.failed, icon: 'alert-triangle', tone: 'warning' });
            return;
        }
        const m = /^wow_([a-z][a-z0-9_]{0,31})_update$/.exec(msg.type);
        if (m) {
            views.set(m[1], d);
            const def = defs.get(m[1]);
            if (view && def && def.onUpdate) def.onUpdate(d, apis.get(m[1]));
            if (view) refresh();     // another module may show part of it (the boss mod bars)
            if (server && !defs.has(m[1])) loadModules();
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
    // game folder, the companion addon), Addons (each module's switch, its own
    // settings and the text of its commands), Key slots, Custom commands. PC
    // only: saving settings is admin.
    let settingsEl = null, setPanes = {}, slotDraft = {}, customDraft = [];
    const PANES = [['general', 'General'], ['addons', 'Addons'], ['keys', 'Key slots'], ['custom', 'Custom commands']];

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
                + PANES.map(([p, label], n) => `<button class="tab${n ? '' : ' active'}" type="button" data-tab="${p}">${label}</button>`).join('') + '</nav>'
                + '<div class="settings-content-wrapper">'
                + PANES.map(([p], n) => `<div class="tab-content${n ? '' : ' active'}" data-pane="${p}"></div>`).join('')
                + '<div class="wow-set-actions"><button class="btn btn-pill btn-ghost" type="button" data-act="cancel">Cancel</button>'
                + '<button class="btn btn-pill btn-primary" type="button" data-act="save"><i data-lucide="check"></i>Save</button></div>'
                + '</div></div>';
            document.body.appendChild(settingsEl);
            PANES.forEach(([p]) => { setPanes[p] = settingsEl.querySelector(`[data-pane="${p}"]`); });
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

    function companionText(cs) {
        if (!cs.installed) return server.settings.wow_path ? 'Not installed in this game folder.' : 'Set the game folder above first.';
        const old = cs.bundled && cs.installed !== cs.bundled;
        const seen = cs.link ? 'Its live link is on: this page shows the game as it changes.'
            : cs.answered == null ? 'It has not answered yet: /reload in WoW, and check it is on in the AddOns list.'
                : `It last answered ${cs.answered < 90 ? `${cs.answered} s` : `${Math.round(cs.answered / 60)} min`} ago; its live link is not showing (/tdc link on in WoW).`;
        // The dashboard keeps it up to date in the game folder; WoW loads a new one at /reload.
        const reload = cs.running && cs.running !== cs.installed ? ` WoW still runs ${cs.running}: /reload in WoW to use it.` : '';
        const release = cs.release ? ` Version ${cs.release} is out: it comes with the next Touch Dashboard update.` : '';
        return `Version ${cs.installed} installed${old ? ` (this PC has ${cs.bundled}: install it)` : ''}.${reload}${release} ${seen}`;
    }

    function renderSettings() {
        const s = server.settings;
        const cs = server.companion || {};
        setPanes.general.innerHTML = '<h3 class="settings-h">Game folder</h3>'
            + '<p class="settings-sub">The World of Warcraft folder, its _retail_ folder, or a SavedVariables folder. Checked every 5 seconds; WoW writes SavedVariables on /reload, logout and exit.</p>'
            + `<div class="form-group"><label for="wow-set-path">Folder</label><input type="text" id="wow-set-path" maxlength="1024" autocomplete="off" spellcheck="false" placeholder="${esc(server.folder_hint || 'The World of Warcraft folder')}" value="${esc(s.wow_path)}"></div>`
            // Where Battle.net installed it (Windows), offered, never saved unasked.
            + (server.folder_hint && !s.wow_path ? `<div class="setting-row wow-set-hint"><div><label class="title">WoW is installed here</label><label class="desc num">${esc(server.folder_hint)}</label></div><button class="btn btn-pill btn-ghost" type="button" data-act="use-hint"><i data-lucide="folder-check"></i>Use this folder</button></div>` : '')
            + `<div class="form-group"><label for="wow-set-account">Account folder (optional)</label><input type="text" id="wow-set-account" maxlength="64" autocomplete="off" spellcheck="false" placeholder="The newest file wins" value="${esc(s.account)}"></div>`
            + '<div class="wow-set-files"></div>'
            + '<h3 class="settings-h">Touch Dashboard addon</h3>'
            + '<p class="settings-sub">Runs addon commands and Blizzard’s without opening chat, holds the key slots’ commands, and shows this page the game as it changes (/tdc link off in WoW hides its line of dots). This PC keeps it up to date, and it reads only what the game lets addons see.</p>'
            + '<div class="setting-group">'
            + settingRow('Use the addon', 'When it is installed', switchHtml('id="wow-set-companion"', s.companion !== false))
            + `<div class="setting-row"><div><label class="title">Status</label><label class="desc wow-comp-status">${esc(companionText(cs))}</label></div>`
            + `<button class="btn btn-pill btn-ghost" type="button" data-act="install"${s.wow_path ? '' : ' disabled'}><i data-lucide="download"></i>${cs.installed ? 'Update' : 'Install'}</button></div>`
            + '</div>'
            + '<h3 class="settings-h">Your character</h3>'
            + '<p class="settings-sub">NSRT reminders tagged with your character or role tag are your personal ones. Settings → Battle.net finds your character and fills these in.</p>'
            + '<div class="wow-set-grid">'
            + `<div class="form-group"><label for="wow-set-char">Character name</label><input type="text" id="wow-set-char" maxlength="48" autocomplete="off" spellcheck="false" placeholder="Magicbane" value="${esc(s.character)}"></div>`
            + `<div class="form-group"><label for="wow-set-role">Role tag</label><input type="text" id="wow-set-role" maxlength="48" autocomplete="off" spellcheck="false" placeholder="ranged" value="${esc(s.role)}"></div>`
            + '<div class="setting-row wow-set-span" style="padding: 0">'
            + '<div><label class="title" for="wow-set-role-auto">Follow the character’s spec</label><label class="desc" for="wow-set-role-auto">The role tag becomes ranged, melee, tank or healer whenever the character is read, so switching to an alt filters NSRT for it</label></div>'
            + `<label class="switch"><input type="checkbox" id="wow-set-role-auto"${s.role_auto !== false ? ' checked' : ''}><span class="slider"></span></label></div>`
            + `<div class="form-group"><label for="wow-set-realm">Realm</label><input type="text" id="wow-set-realm" maxlength="64" autocomplete="off" spellcheck="false" placeholder="Draenor" value="${esc(s.realm)}"></div>`
            + '<div class="form-group"><label for="wow-set-region">Region</label><select id="wow-set-region">'
            + ['eu', 'us', 'kr', 'tw'].map(r => `<option value="${r}"${s.region === r ? ' selected' : ''}>${r.toUpperCase()}</option>`).join('')
            + '</select></div>'
            + '</div>'
            + '<h3 class="settings-h">How keys reach the game</h3>'
            + '<div class="notice"><i data-lucide="lightbulb"></i><p>WoW lets no addon talk to the PC, so the PC presses keys: WoW must be the window in front. '
            + 'A key with a key slot is one key press (Ctrl+Alt+Shift+F1–F12), in combat too. With the Touch Dashboard addon, everything else goes through it without opening chat: an addon’s command it runs itself, Blizzard’s it loads into its run button (Ctrl+Alt+Shift+End). '
            + 'In combat, or without the addon, Blizzard’s commands are typed into chat: Enter, Ctrl+V, Enter, and your clipboard is put back.</p></div>';
        // Install works from a folder typed but not saved yet: it is saved first
        // (the server takes messages in order), so a first setup needs no
        // round trip through Save.
        const install = setPanes.general.querySelector('[data-act="install"]');
        const pathIn = setPanes.general.querySelector('#wow-set-path');
        pathIn.addEventListener('input', () => { install.disabled = !pathIn.value.trim(); });
        const useHint = setPanes.general.querySelector('[data-act="use-hint"]');
        if (useHint) useHint.addEventListener('click', () => {
            pathIn.value = server.folder_hint;
            pathIn.dispatchEvent(new Event('input'));
            useHint.closest('.wow-set-hint').remove();
        });
        install.addEventListener('click', () => {
            const folder = { wow_path: pathIn.value.trim(), account: setPanes.general.querySelector('#wow-set-account').value.trim() };
            if (folder.wow_path !== server.settings.wow_path || folder.account !== server.settings.account) {
                sendMessage({ type: 'wow_settings_save', wow_addons: folder });
            }
            sendMessage({ type: 'wow_companion_install' });
        });
        renderFiles();

        // Addons: one group each, its switch first.
        setPanes.addons.innerHTML = '<h3 class="settings-h">Addons</h3><p class="settings-sub">Each one adds its plates and commands to the page. One you do not use can stay off.</p>';
        for (const m of server.modules.filter(x => x.id !== 'custom')) {
            const group = document.createElement('div');
            group.className = 'setting-group wow-set-module';
            group.dataset.module = m.id;
            group.dataset.on = String(m.enabled);
            const inst = m.installed === false ? ' · not in your AddOns folder' : '';
            group.innerHTML = settingRow(m.name, m.description + inst, switchHtml(`data-toggle="${esc(m.id)}" aria-label="${esc(m.name)}"`, m.enabled))
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

        slotDraft = clone(s.keybinds || {});
        renderSlots();
        customDraft = clone(s.custom || []);
        renderCustom();
        renderIcons(settingsEl);
    }

    // Key slots: Ctrl + Alt + Shift + F1–F12, each a key's command and arguments.
    function renderSlots() {
        const cs = server.companion || {};
        const texts = server.slot_text || {};
        setPanes.keys.innerHTML = '<h3 class="settings-h">Key slots</h3>'
            + `<p class="settings-sub">${cs.installed
                ? 'The Touch Dashboard addon holds each slot’s command and presses it in game: in combat too, and in chat lockdown. Give a key a slot in its editor (hold the key, or the pencil on the page).'
                : 'In WoW, make a macro with the text shown and bind it to the same keys; the key then presses them instead of typing into chat, in combat too. Give a key a slot in its editor.'}</p>`
            + '<div class="setting-group">' + server.slots.map(slot => {
                const kb = slotDraft[slot];
                const m = kb && commandMeta(kb.cmd);
                return '<div class="wow-slot">'
                    + `<span class="wow-caps" aria-label="Control Alt Shift ${slot}"><span class="wow-cap">Ctrl</span>+<span class="wow-cap">Alt</span>+<span class="wow-cap">Shift</span>+<span class="wow-cap">${slot}</span></span>`
                    + `<span class="wow-slot-name">${kb ? esc(m ? m.label : kb.cmd) : '<span class="wow-slot-free">Free</span>'}</span>`
                    + (kb ? `<button class="btn btn-ghost wow-slot-clear" type="button" data-slot="${slot}" aria-label="Free ${slot}"><i data-lucide="x"></i></button>` : '')
                    + `<span class="wow-slot-macro">${kb ? `Macro text: ${esc(texts[slot] || slash(kb.cmd, kb.args))}` : ''}</span></div>`;
            }).join('') + '</div>';
        setPanes.keys.querySelectorAll('.wow-slot-clear').forEach(b => b.addEventListener('click', () => { delete slotDraft[b.dataset.slot]; renderSlots(); }));
        renderIcons(setPanes.keys);
    }

    // Custom commands: any slash command, for an addon this page does not know.
    function renderCustom() {
        setPanes.custom.innerHTML = '<h3 class="settings-h">Custom commands</h3>'
            + '<p class="settings-sub">Slash commands of your own, one a line, as you would type them in WoW. They show in the key editor under Custom commands. An addon’s command goes through the Touch Dashboard addon; Blizzard’s (/cast, /p, /rw…) are typed into chat or need a key slot.</p>'
            + '<div class="wow-custom-list"></div>'
            + '<button class="btn btn-pill btn-ghost" type="button" data-act="add-custom"><i data-lucide="plus"></i>Add a command</button>';
        const list = setPanes.custom.querySelector('.wow-custom-list');
        list.innerHTML = customDraft.length ? customDraft.map((c, n) => '<div class="setting-group wow-custom" data-n="' + n + '">'
            + '<div class="wow-set-grid">'
            + `<div class="form-group"><label for="wow-cu-label-${n}">Name</label><input type="text" id="wow-cu-label-${n}" data-f="label" maxlength="24" value="${esc(c.label)}" autocomplete="off" spellcheck="false"></div>`
            + `<div class="form-group"><label for="wow-cu-via-${n}">Sent</label><select id="wow-cu-via-${n}" data-f="via">`
            + [['auto', 'As its first word says'], ['addon', 'Through the addon'], ['game', 'Typed into chat']].map(([v, l]) => `<option value="${v}"${c.via === v ? ' selected' : ''}>${l}</option>`).join('')
            + '</select></div>'
            + `<div class="form-group wow-set-span"><label for="wow-cu-lines-${n}">Command</label><textarea id="wow-cu-lines-${n}" data-f="lines" rows="2" spellcheck="false">${esc(c.lines.join('\n'))}</textarea></div>`
            + '</div>'
            + `<button class="btn btn-pill btn-ghost wow-custom-del" type="button" data-n="${n}"><i data-lucide="trash-2"></i>Delete</button></div>`).join('')
            : '<p class="settings-sub">None yet.</p>';
        list.addEventListener('input', (e) => {
            const box = e.target.closest('.wow-custom');
            if (!box) return;
            const c = customDraft[Number(box.dataset.n)];
            const f = e.target.dataset.f;
            if (f === 'lines') c.lines = e.target.value.split('\n').map(l => l.trim()).filter(Boolean);
            else if (f) c[f] = e.target.value;
        });
        list.addEventListener('change', (e) => {
            const box = e.target.closest('.wow-custom');
            if (box && e.target.dataset.f === 'via') customDraft[Number(box.dataset.n)].via = e.target.value;
        });
        list.querySelectorAll('.wow-custom-del').forEach(b => b.addEventListener('click', () => { customDraft.splice(Number(b.dataset.n), 1); renderCustom(); }));
        setPanes.custom.querySelector('[data-act="add-custom"]').addEventListener('click', () => {
            customDraft.push({ id: `c_${Math.random().toString(36).slice(2, 10)}`, label: '', glyph: 'terminal', lines: [], via: 'auto' });
            renderCustom();
            const inputs = setPanes.custom.querySelectorAll('[data-f="label"]');
            inputs[inputs.length - 1].focus();
        });
        renderIcons(setPanes.custom);
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
        const status = settingsEl.querySelector('.wow-comp-status');
        if (status) text(status, companionText(server.companion || {}));
    }

    function saveSettings() {
        const q = (sel) => settingsEl.querySelector(sel);
        const out = {
            character: q('#wow-set-char').value.trim(),
            role: q('#wow-set-role').value.trim(),
            role_auto: q('#wow-set-role-auto').checked,
            realm: q('#wow-set-realm').value.trim(),
            region: q('#wow-set-region').value,
            wow_path: q('#wow-set-path').value.trim(),
            account: q('#wow-set-account').value.trim(),
            companion: q('#wow-set-companion').checked,
            modules: {}, keybinds: slotDraft, slash: {},
        };
        settingsEl.querySelectorAll('[data-toggle]').forEach(i => { out.modules[i.dataset.toggle] = i.checked; });
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
        const bad = customDraft.find(c => !c.lines.length || c.lines.some(l => !l.startsWith('/')));
        if (bad) {
            tdNotice({ title: `“${bad.label || 'A custom command'}” cannot be sent`, text: 'Each line starts with /, as you would type it in WoW.', icon: 'terminal', tone: 'warning' });
            setTab('custom');
            return;
        }
        out.custom = customDraft.map(c => ({ ...c, label: c.label.trim() || c.lines[0].slice(0, LABEL_MAX) }));
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
