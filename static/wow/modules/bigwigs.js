/*
 * BigWigs: Pull and Break keys (action pad, top), their faders (each screen
 * keeps its own lengths, starting from the defaults in WoW settings) and the
 * timer display (right): the last pull, break or bar sent, counted down on
 * this screen's clock from the seconds the PC said were left. It steps once a
 * second with its figure, like the progress bar: nothing animates meanwhile.
 */
(function () {
    'use strict';

    const PULL = [5, 25], BREAK = [2, 15];
    let pull = null, brk = null;             // chosen lengths; null: follow the default
    let pullKey = null, breakKey = null, pullFader = null, breakFader = null;
    let timerEl = null, timer = null, tick = 0;

    function lengths(api) {
        const s = api.settings();
        return { pull: pull ?? s.pull_s ?? 10, brk: brk ?? s.break_m ?? 5 };
    }

    function drawKeys(api) {
        const { pull: p, brk: b } = lengths(api);
        if (pullKey) pullKey.setValue(api.slash('bw_pull', { seconds: p }));
        if (breakKey) breakKey.setValue(api.slash('bw_break', { minutes: b }));
        if (pullFader) pullFader.setValue(p);
        if (breakFader) breakFader.setValue(b);
    }

    function clock(s) {
        s = Math.max(0, Math.ceil(s));
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    function drawTimer(api) {
        if (!timerEl) return;
        const left = timer ? (timer.endAt - performance.now()) / 1000 : 0;
        if (!timer || left <= 0) {
            timer = null;
            clearTimeout(tick);
            timerEl.dataset.kind = 'idle';
            api.text(timerEl.querySelector('.wow-timer-label'), 'No timer');
            api.text(timerEl.querySelector('.live-v'), '—');
            timerEl.querySelector('.wow-timer-fill').style.transform = 'scaleX(0)';
            return;
        }
        timerEl.dataset.kind = timer.kind;
        api.text(timerEl.querySelector('.wow-timer-label'), timer.label);
        api.text(timerEl.querySelector('.live-v'), clock(left));
        const f = Math.min(1, Math.max(0, Math.ceil(left) / timer.duration));
        timerEl.querySelector('.wow-timer-fill').style.transform = `scaleX(${f.toFixed(4)})`;
        // The next whole second.
        clearTimeout(tick);
        tick = setTimeout(() => drawTimer(api), ((left % 1) || 1) * 1000 + 15);
    }

    // The PC's last timer, from its seconds left when the state was sent.
    function syncTimer(api) {
        const t = api.state().timer;
        if (!t) {
            if (timer && timer.fromServer) timer = null;
        } else {
            timer = { kind: t.kind, label: t.label, duration: t.duration, endAt: performance.now() + t.remaining * 1000, fromServer: true };
        }
        drawTimer(api);
    }

    AddonModules.register({
        id: 'bigwigs',
        name: 'BigWigs',
        commands: ['bw_pull', 'bw_break', 'bw_raidbar', 'bw_localbar', 'bw_version'],
        columns: [
            {
                slot: 'top', order: 10, render(el, api) {
                    pullKey = api.key({ glyph: 'timer', label: 'Pull', value: '' });
                    pullKey.addEventListener('click', () => api.send('bw_pull', { seconds: lengths(api).pull }, pullKey));
                    el.appendChild(pullKey);
                    drawKeys(api);
                },
            },
            {
                slot: 'top', order: 20, render(el, api) {
                    breakKey = api.key({ glyph: 'coffee', label: 'Break', value: '' });
                    breakKey.addEventListener('click', () => api.send('bw_break', { minutes: lengths(api).brk }, breakKey));
                    el.appendChild(breakKey);
                    drawKeys(api);
                },
            },
            {
                slot: 'faders', order: 10, render(el, api) {
                    pullFader = api.fader({
                        name: 'Pull', min: PULL[0], max: PULL[1], value: lengths(api).pull,
                        format: (v) => `${v} s`, onChange: (v) => { pull = v; drawKeys(api); },
                    });
                    breakFader = api.fader({
                        name: 'Break', min: BREAK[0], max: BREAK[1], value: lengths(api).brk,
                        format: (v) => `${v} min`, onChange: (v) => { brk = v; drawKeys(api); },
                    });
                    el.append(pullFader, breakFader);
                },
            },
            {
                column: 'right', order: 60, title: 'BigWigs timer', grow: true, render(sec, api) {
                    timerEl = document.createElement('div');
                    timerEl.className = 'scribble wow-timer';
                    timerEl.dataset.kind = 'idle';
                    timerEl.setAttribute('role', 'timer');
                    timerEl.innerHTML = '<div class="wow-timer-top"><span class="wow-timer-label">No timer</span>'
                        + '<span class="wow-timer-fig live num"><span class="live-v">—</span></span></div>'
                        + '<div class="wow-timer-bar"><div class="wow-timer-fill"></div></div>';
                    sec.appendChild(timerEl);
                    syncTimer(api);
                },
            },
        ],
        update(api) {
            drawKeys(api);
            syncTimer(api);
        },
        settings: {
            render(el, api) {
                const s = api.settings();
                el.innerHTML = '<div class="wow-set-grid">'
                    + `<div class="form-group"><label for="wow-bw-pull">Pull timer (seconds, ${PULL[0]}–${PULL[1]})</label>`
                    + `<input type="number" id="wow-bw-pull" min="${PULL[0]}" max="${PULL[1]}" step="1" value="${s.pull_s}"></div>`
                    + `<div class="form-group"><label for="wow-bw-break">Break (minutes, ${BREAK[0]}–${BREAK[1]})</label>`
                    + `<input type="number" id="wow-bw-break" min="${BREAK[0]}" max="${BREAK[1]}" step="1" value="${s.break_m}"></div></div>`;
            },
            collect() {
                return {
                    pull_s: Number(document.getElementById('wow-bw-pull').value),
                    break_m: Number(document.getElementById('wow-bw-break').value),
                };
            },
        },
    });
})();
