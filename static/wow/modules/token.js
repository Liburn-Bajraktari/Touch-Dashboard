/*
 * The WoW Token (wow_token_update): its price in the region, the change over a
 * day, the week's low and high and a trace of it, from Battle.net. The trace
 * is drawn once per price (every 20 minutes or so), never animated.
 */
(function () {
    'use strict';

    let ui = null, data = null, drawn = '';
    const fmt = (n) => Math.round(n).toLocaleString();

    function render(sec, api) {
        sec.insertAdjacentHTML('beforeend', '<div class="wow-token">'
            + '<div class="scribble wow-token-main"><span class="ch-name">Price</span><span class="live num wow-gold-fig"><span class="live-v">—</span></span></div>'
            + '<svg class="wow-token-trace" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true"><polyline points=""></polyline></svg>'
            + '<div class="wow-gold-lines"><div class="wow-gold-line"><span>A day</span><span class="num" data-f="change">—</span></div>'
            + '<div class="wow-gold-line"><span>Week</span><span class="num" data-f="range">—</span></div></div>'
            + '<p class="wow-cell-note"></p></div>');
        ui = sec.querySelector('.wow-token');
        drawn = '';
        draw(api);
    }

    function draw(api) {
        if (!ui || !ui.isConnected) return;
        const d = data || { state: 'loading' };
        const note = ui.querySelector('.wow-cell-note');
        if (d.state !== 'ok') {
            api.text(ui.querySelector('.live-v'), '—');
            api.text(note, d.state === 'no_key'
                ? (api.admin() ? 'Add your Battle.net API key in Settings → Battle.net.' : 'Needs the Battle.net API key on the PC (Settings → Battle.net).')
                : d.state === 'error' ? 'Battle.net did not answer; the PC tries again in a few minutes.' : 'Reading the price…');
            return;
        }
        api.text(ui.querySelector('.live-v'), `${fmt(d.price)} g`);
        const change = ui.querySelector('[data-f="change"]');
        api.text(change, d.change == null ? '—' : `${d.change >= 0 ? '+' : '−'}${fmt(Math.abs(d.change))} g`);
        change.dataset.tone = d.change > 0 ? 'up' : d.change < 0 ? 'down' : '';
        // One price read so far (a fresh install) has no range to show yet.
        api.text(ui.querySelector('[data-f="range"]'), d.high > d.low ? `${fmt(d.low)} – ${fmt(d.high)} g` : '—');
        const mins = Math.max(0, Math.round((Date.now() / 1000 - d.updated) / 60));
        api.text(note, `${(d.region || '').toUpperCase()} · priced ${mins < 1 ? 'just now' : mins < 120 ? `${mins} min ago` : `${Math.round(mins / 60)} h ago`}`);
        const key = JSON.stringify(d.points);
        if (key === drawn) return;
        drawn = key;
        const pts = d.points || [];
        const t0 = pts.length ? pts[0][0] : 0, t1 = pts.length ? pts[pts.length - 1][0] : 1;
        const lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
        ui.querySelector('polyline').setAttribute('points', pts.map(([t, g]) => {
            const x = t1 > t0 ? ((t - t0) / (t1 - t0)) * 100 : 100;
            const y = hi > lo ? 28 - ((g - lo) / (hi - lo)) * 26 : 15;
            return `${x.toFixed(2)},${y.toFixed(2)}`;
        }).join(' '));
        ui.querySelector('.wow-token-trace').hidden = pts.length < 2;
    }

    AddonModules.register({
        id: 'token',
        name: 'WoW Token',
        widgets: { price: { render } },
        update: draw,
        onUpdate(d, api) {
            data = d;
            draw(api);
        },
    });
})();
