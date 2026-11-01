/*
 * Raid & dungeons: the group lead's plate. Pull, Break and Ready check keys;
 * the pull and break lengths behind a slim Lengths switch in the head (most
 * people send the same every time: each screen keeps its own, starting from
 * the defaults in WoW settings, and remembers whether they are open); the
 * timer display; who is not ready; and the boss mod bars the companion addon
 * sees. While its live link shows, the game decides: a pull or break bar
 * (yours, or the raid leader's) is the big timer, and none means none.
 * Without it, the last pull or break sent from here, and the bars from the
 * addon's last answer. Counted down on this screen's clock from the seconds
 * the PC said were left; it steps once a second with its figure, like the
 * progress bar: nothing animates meanwhile.
 *
 * The Mythic+ plate: the key's timer against the times its chests need (+3
 * at 60 % of the limit, +2 at 80 %, +1 in time), deaths and the time they
 * cost, enemy forces, each boss and when it fell, and the result once it is
 * over; from the companion's live link (the game module's view, `mplus`),
 * counted on this screen's clock, a step a second.
 */
(function () {
    'use strict';

    const PULL = [5, 25], BREAK = [2, 15];
    const BIG_BAR = /^(pull|break)\b/i;      // BigWigs' and DBM's pull and break bars, and the companion's countdown
    const OPEN_KEY = 'td_wow_raid_lengths';
    let pull = null, brk = null;             // chosen lengths; null: follow the default
    let pullKey = null, breakKey = null, rcKey = null, pullFader = null, breakFader = null;
    let fadersEl = null, lengthsKey = null, timerEl = null, rcEl = null, barsEl = null;
    let timer = null, tick = 0, rc = null;
    let bars = [], barsAt = 0, barsKey = '';

    function lengthsOpen() {
        try { return localStorage.getItem(OPEN_KEY) === '1'; } catch (e) { return false; }
    }

    function setLengthsOpen(on) {
        try { localStorage.setItem(OPEN_KEY, on ? '1' : '0'); } catch (e) { /* a private window: this visit only */ }
        if (fadersEl) fadersEl.hidden = !on;
        if (lengthsKey) {
            lengthsKey.setAttribute('aria-expanded', on ? 'true' : 'false');
            lengthsKey.classList.toggle('is-on', on);
        }
    }

    function lengths(api) {
        const s = api.settings();
        return { pull: pull ?? s.pull_s ?? 10, brk: brk ?? s.break_m ?? 5 };
    }

    function clock(s) {
        s = Math.max(0, Math.ceil(s));
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    function running(kind) {
        return !!(timer && timer.kind === kind && timer.endAt > performance.now());
    }

    function drawKeys(api) {
        const { pull: p, brk: b } = lengths(api);
        if (pullKey) {
            pullKey.setValue(running('pull') ? 'Cancel' : `${p} s`);
            pullKey.setOn(running('pull'));
        }
        if (breakKey) {
            breakKey.setValue(running('break') ? 'Cancel' : `${b} min`);
            breakKey.setOn(running('break'));
        }
        if (rcKey) {
            rcKey.setValue(rc ? `${rc.ready} / ${rc.total}` : '');
            rcKey.setOn(!!(rc && rc.state === 'running' && rcLeft() > 0));
        }
        if (pullFader) pullFader.setValue(p);
        if (breakFader) breakFader.setValue(b);
    }

    function drawTimer(api) {
        if (!timerEl || !timerEl.isConnected) return;
        const left = timer ? (timer.endAt - performance.now()) / 1000 : 0;
        drawBars(api);
        drawReady(api);
        if (!timer || left <= 0) {
            timer = null;
            timerEl.dataset.kind = 'idle';
            api.text(timerEl.querySelector('.wow-timer-label'), 'No timer');
            api.text(timerEl.querySelector('.live-v'), '—');
            timerEl.querySelector('.wow-timer-fill').style.transform = 'scaleX(0)';
        } else {
            timerEl.dataset.kind = timer.kind;
            api.text(timerEl.querySelector('.wow-timer-label'), timer.label);
            api.text(timerEl.querySelector('.live-v'), clock(left));
            const f = Math.min(1, Math.max(0, Math.ceil(left) / timer.duration));
            timerEl.querySelector('.wow-timer-fill').style.transform = `scaleX(${f.toFixed(4)})`;
        }
        drawKeys(api);
        // The next whole second, while anything counts down.
        clearTimeout(tick);
        const counting = timer || bars.length || (rc && rc.state === 'running' && rcLeft() > 0);
        if (counting) tick = setTimeout(() => drawTimer(api), (((left > 0 ? left : 1) % 1) || 1) * 1000 + 15);
    }

    // The companion's bars, counted down from when its answer came.
    function drawBars(api) {
        if (!barsEl) return;
        const gone = (performance.now() - barsAt) / 1000;
        const live = bars.map(b => ({ label: b.label, left: b.rem - gone })).filter(b => b.left > 0);
        if (!live.length) bars = [];
        barsEl.hidden = !live.length;
        barsEl.innerHTML = live.slice(0, 6).map(b => `<div class="wow-bar-row"><span class="wow-bar-name">${api.esc(b.label)}</span>`
            + `<span class="wow-bar-left num">${clock(b.left)}</span></div>`).join('');
    }

    function rcLeft() {
        return rc ? rc.rem - (performance.now() - barsAt) / 1000 : 0;
    }

    // Who has not said ready: the names, quietly, under one line of figures.
    function drawReady(api) {
        if (!rcEl) return;
        rcEl.hidden = !rc;
        if (!rc) return;
        const live = rc.state === 'running' && rcLeft() > 0;
        const all = rc.ready >= rc.total && rc.total > 0;
        api.text(rcEl.querySelector('.wow-rc-head'), live ? `Ready check · ${clock(rcLeft())}`
            : `Ready check · ${Math.max(1, Math.round(-rcLeft() / 60))} min ago`);
        api.text(rcEl.querySelector('.wow-rc-fig'), all ? 'Everyone is ready' : `${rc.ready} of ${rc.total} ready`);
        rcEl.dataset.tone = !live && !all ? 'warn' : '';
        // Over (or run out before the game said so): no answer yet is no answer.
        const names = (pick) => rc.missing.filter(pick).map(m => m.name).join(', ');
        const no = names(m => m.a === 2), rest = names(m => m.a !== 2);
        api.text(rcEl.querySelector('.wow-rc-who'), [no && `Not ready: ${no}`,
            rest && (live ? `Waiting for ${rest}` : `No answer: ${rest}`)].filter(Boolean).join(' · '));
    }

    function syncLive(api) {
        const g = api.view('game');
        const live = g && g.live;
        const key = live ? JSON.stringify([live.age, live.bars, live.rc]) : '';
        if (key === barsKey) return;
        barsKey = key;
        const fresh = live && live.age < 600;
        const all = fresh ? live.bars.map(b => ({ label: b.label, rem: b.rem - live.age, dur: b.dur })) : [];
        barsAt = performance.now();
        const big = all.find(b => BIG_BAR.test(b.label));
        bars = all.filter(b => !BIG_BAR.test(b.label));     // a boss mod's copy of the same countdown too
        rc = fresh && live.rc ? { ...live.rc, rem: (live.rc.rem || 0) - live.age } : null;
        if (live && live.link) {
            timer = big ? { kind: /^pull/i.test(big.label) ? 'pull' : 'break', label: big.label,
                            duration: big.dur || big.rem, endAt: barsAt + big.rem * 1000, fromGame: true } : null;
        } else if (timer && timer.fromGame) {
            timer = null;
        }
    }

    // The PC's last timer, from its seconds left when the state was sent;
    // while the live link shows, the game's own (syncLive).
    function syncTimer(api) {
        const t = api.state().timer;
        syncLive(api);
        const g = api.view('game');
        if (!(g && g.live && g.live.link)) {
            if (!t) {
                if (timer && timer.fromServer) timer = null;
            } else {
                timer = { kind: t.kind, label: t.label, duration: t.duration, endAt: performance.now() + t.remaining * 1000, fromServer: true };
            }
        }
        drawTimer(api);
    }

    function render(sec, api, head) {
        lengthsKey = document.createElement('button');
        lengthsKey.type = 'button';
        lengthsKey.className = 'btn wow-head-key wow-head-toggle';
        lengthsKey.innerHTML = '<i data-lucide="sliders-horizontal"></i><span class="wow-head-label">Lengths</span>';
        lengthsKey.setAttribute('aria-label', 'Pull and break lengths');
        lengthsKey.addEventListener('click', () => setLengthsOpen(fadersEl.hidden));
        head.appendChild(lengthsKey);
        api.icons(head);

        const keys = document.createElement('div');
        keys.className = 'wow-trio';
        pullKey = api.key({ glyph: 'timer', label: 'Pull', value: '' });
        pullKey.addEventListener('click', () => api.send('raid_pull', { seconds: running('pull') ? 0 : lengths(api).pull }, pullKey));
        breakKey = api.key({ glyph: 'coffee', label: 'Break', value: '' });
        breakKey.addEventListener('click', () => api.send('raid_break', { minutes: running('break') ? 0 : lengths(api).brk }, breakKey));
        rcKey = api.key({ glyph: 'check-circle-2', label: 'Ready check', value: '' });
        rcKey.addEventListener('click', () => api.send('wow_readycheck', null, rcKey));
        keys.append(pullKey, breakKey, rcKey);

        fadersEl = document.createElement('div');
        fadersEl.className = 'wow-faders';
        pullFader = api.fader({
            name: 'Pull', min: PULL[0], max: PULL[1], value: lengths(api).pull,
            format: (v) => `${v} s`, onChange: (v) => { pull = v; drawKeys(api); },
        });
        breakFader = api.fader({
            name: 'Break', min: BREAK[0], max: BREAK[1], value: lengths(api).brk,
            format: (v) => `${v} min`, onChange: (v) => { brk = v; drawKeys(api); },
        });
        fadersEl.append(pullFader, breakFader);

        timerEl = document.createElement('div');
        timerEl.className = 'scribble wow-timer';
        timerEl.dataset.kind = 'idle';
        timerEl.setAttribute('role', 'timer');
        timerEl.innerHTML = '<div class="wow-timer-top"><span class="wow-timer-label">No timer</span>'
            + '<span class="wow-timer-fig live num"><span class="live-v">—</span></span></div>'
            + '<div class="wow-timer-bar"><div class="wow-timer-fill"></div></div>';
        rcEl = document.createElement('div');
        rcEl.className = 'wow-rc';
        rcEl.hidden = true;
        rcEl.setAttribute('aria-live', 'polite');
        rcEl.innerHTML = '<div class="wow-rc-top"><span class="wow-rc-head"></span><span class="wow-rc-fig num"></span></div>'
            + '<p class="wow-rc-who"></p>';
        barsEl = document.createElement('div');
        barsEl.className = 'wow-bars';
        barsEl.hidden = true;
        barsEl.setAttribute('aria-label', 'Boss mod bars, as the addon last saw them');
        sec.append(keys, fadersEl, timerEl, rcEl, barsEl);
        barsKey = '';
        setLengthsOpen(lengthsOpen());
        syncTimer(api);
    }

    // ── Mythic+ ──────────────────────────────────────────────────────────
    const mp = { el: null, head: null, data: null, at: 0, key: '', tick: 0 };

    function syncRun(api) {
        const g = api.view('game');
        const live = g && g.live;
        const key = live ? JSON.stringify([live.age, live.mplus]) : '';
        if (key === mp.key) return;
        mp.key = key;
        mp.at = performance.now();
        const m = live && live.mplus;
        mp.data = m && live.age < 3600 ? { ...m, elapsed: m.elapsed + (m.counting ? live.age : 0) } : null;
    }

    // Time gone, as a stopwatch shows it (whole seconds passed); countdowns round up.
    function since(s) {
        s = Math.max(0, Math.floor(s));
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    function runElapsed() {
        const m = mp.data;
        return m ? m.elapsed + (m.counting ? (performance.now() - mp.at) / 1000 : 0) : 0;
    }

    function keyName(m) {
        return [m.level ? `+${m.level}` : '', m.name || ''].filter(Boolean).join(' · ');
    }

    // Built when the run's shape changes (a new key, a boss down, over);
    // between, only the figures' text and the bar's scale.
    function drawRun(api) {
        if (!mp.el) return;
        syncRun(api);
        const m = mp.data;
        const shape = JSON.stringify(m ? [m.state, m.name, m.level, m.limit, m.bosses.map(b => [b.name, b.done])] : api.view('game') ? 'idle' : 'none');
        if (shape !== mp.el.shape) {
            mp.el.shape = shape;
            mp.el.dataset.state = m ? m.state : 'idle';
            if (!m) {
                const me = ((api.view('game') || {}).chars || []).find(c => c.current);
                const ks = me && me.keystone;
                mp.el.innerHTML = `<p class="wow-cell-note">Not in a key.${ks ? ` Your keystone: ${api.esc(`+${ks.level} ${ks.name || ''}`)}.` : ''}</p>`;
                if (mp.head) api.text(mp.head, '');
            } else {
                const ticks = m.chests.slice(0, 2).map(t => `<span class="wow-mp-tick" style="left: ${(t / m.limit * 100).toFixed(2)}%"></span>`).join('');
                mp.el.innerHTML = '<div class="scribble wow-mp-timer">'
                    + '<div class="wow-mp-top"><span class="wow-mp-fig live num"><span class="live-v"></span></span>'
                    + `<span class="wow-mp-limit num">of ${since(m.limit)}</span></div>`
                    + `<div class="wow-mp-bar"><div class="wow-mp-fill"></div>${ticks}</div>`
                    + '<div class="wow-mp-chests"></div></div>'
                    + '<div class="wow-mp-stats">'
                    + '<div class="wow-mp-stat"><span class="wow-mp-name">Forces</span><span class="wow-mp-val num" data-f="forces"></span>'
                    + '<div class="wow-mp-forces"><div class="wow-mp-forces-fill"></div></div></div>'
                    + '<div class="wow-mp-stat"><span class="wow-mp-name">Deaths</span><span class="wow-mp-val num" data-f="deaths"></span></div></div>'
                    + (m.bosses.length ? '<div class="wow-mp-bosses">' + m.bosses.map(b => `<div class="wow-mp-boss"${b.done ? ' data-done=""' : ''}>`
                        + `<span class="wow-mp-boss-name">${api.esc(b.name)}</span><span class="num">${b.done && b.at ? since(b.at) : (b.done ? 'Down' : '')}</span></div>`).join('') + '</div>' : '');
                if (mp.head) api.text(mp.head, keyName(m));
            }
        }
        clearTimeout(mp.tick);
        if (!m) return;
        const e = runElapsed();
        const over = m.limit && e > m.limit;
        api.text(mp.el.querySelector('.live-v'), since(e));
        mp.el.querySelector('.wow-mp-fill').style.transform = `scaleX(${(m.limit ? Math.min(1, e / m.limit) : 0).toFixed(4)})`;
        mp.el.dataset.over = over ? 'true' : 'false';
        const chests = mp.el.querySelector('.wow-mp-chests');
        if (m.state === 'over') {
            chests.dataset.tone = m.onTime ? 'good' : 'warn';
            api.text(chests, m.onTime ? `Timed${m.upgrades ? ` · +${m.upgrades}` : ''}` : `Over time by ${since(e - m.limit)}`);
        } else if (!m.counting) {
            chests.dataset.tone = '';
            api.text(chests, 'Starting…');
        } else if (over) {
            chests.dataset.tone = 'warn';
            api.text(chests, `Over time by ${since(e - m.limit)}`);
        } else {
            // The best chest still in reach first, the others after it
            const left = m.chests.map((t, i) => ({ plus: 3 - i, left: t - e })).filter(c => c.left > 0);
            chests.dataset.tone = left.length && left[0].plus === 3 ? 'good' : '';
            api.text(chests, left.map(c => `+${c.plus} in ${clock(c.left)}`).join(' · '));
        }
        api.text(mp.el.querySelector('[data-f="forces"]'), `${m.forces.toFixed(m.forces >= 100 ? 0 : 1)} %`);
        mp.el.querySelector('.wow-mp-forces-fill').style.transform = `scaleX(${(m.forces / 100).toFixed(4)})`;
        api.text(mp.el.querySelector('[data-f="deaths"]'), m.deaths ? `${m.deaths} · −${since(m.lost)}` : '0');
        if (m.counting && m.state === 'running') mp.tick = setTimeout(() => drawRun(api), (1 - (e % 1)) * 1000 + 15);
    }

    function renderRun(sec, api, head) {
        mp.head = document.createElement('span');
        mp.head.className = 'wow-mp-key';
        head.appendChild(mp.head);
        mp.el = document.createElement('div');
        mp.el.className = 'wow-mp';
        mp.el.setAttribute('role', 'timer');
        sec.appendChild(mp.el);
        mp.key = '';
        drawRun(api);
    }

    AddonModules.register({
        id: 'raid',
        name: 'Raid & dungeons',
        widgets: { timers: { render }, mplus: { render: renderRun } },
        keyState(command, args, api) {
            if (command === 'raid_pull' || command === 'bw_pull') return { on: running('pull') };
            if (command === 'raid_break' || command === 'bw_break') return { on: running('break') };
            if (command === 'wow_readycheck') return { on: !!(rc && rc.state === 'running' && rcLeft() > 0) };
            return null;
        },
        update(api) {
            syncTimer(api);
            drawRun(api);
        },
        settings: {
            render(el, api) {
                const s = api.settings();
                el.innerHTML = '<div class="wow-set-grid">'
                    + `<div class="form-group"><label for="wow-raid-pull">Pull timer (seconds, ${PULL[0]}–${PULL[1]})</label>`
                    + `<input type="number" id="wow-raid-pull" min="${PULL[0]}" max="${PULL[1]}" step="1" value="${s.pull_s}"></div>`
                    + `<div class="form-group"><label for="wow-raid-break">Break (minutes, ${BREAK[0]}–${BREAK[1]})</label>`
                    + `<input type="number" id="wow-raid-break" min="${BREAK[0]}" max="${BREAK[1]}" step="1" value="${s.break_m}"></div></div>`;
            },
            collect() {
                return {
                    pull_s: Number(document.getElementById('wow-raid-pull').value),
                    break_m: Number(document.getElementById('wow-raid-break').value),
                };
            },
        },
    });
})();
