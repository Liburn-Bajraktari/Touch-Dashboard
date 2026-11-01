/*
 * World markers: the eight in their colours, in the order players know them
 * (the target marks', star first; each sends its own /wm number, which counts
 * them differently), and Clear all. A marker lands where the mouse points in
 * game, as a key bound to it would place it.
 */
(function () {
    'use strict';

    // Symbol, its /wm number (warcraft.wiki.gg, MACRO worldmarker)
    const MARKS = [['star', 5], ['circle', 6], ['diamond', 3], ['triangle', 2],
                   ['moon', 7], ['square', 1], ['cross', 4], ['skull', 8]];
    const GLYPH = { cross: 'x' };

    function render(sec, api) {
        const grid = document.createElement('div');
        grid.className = 'wow-marks';
        for (const [name, n] of MARKS) {
            const k = api.key({ glyph: GLYPH[name] || name, label: name[0].toUpperCase() + name.slice(1) });
            k.classList.add('wow-mark');
            k.dataset.mark = name;
            k.addEventListener('click', () => api.send('wow_wm', { marker: n }, k));
            grid.appendChild(k);
        }
        const clear = api.key({ glyph: 'map-pin-off', label: 'Clear all' });
        clear.classList.add('wow-mark', 'wow-mark-clear');
        clear.addEventListener('click', () => api.send('wow_cwm', null, clear));
        grid.appendChild(clear);
        sec.appendChild(grid);
    }

    // /tm counts them the target marks' way, 1 star … 8 skull.
    const BY_WM = Object.fromEntries(MARKS.map(([name, n]) => [n, name]));
    const BY_TM = Object.fromEntries(MARKS.map(([name], i) => [i + 1, name]));

    AddonModules.register({
        id: 'markers',
        name: 'World markers',
        widgets: { world: { render } },
        // Keys plates show a marker key in its marker's colour too.
        keyState(command, args) {
            const mark = (command === 'wow_wm' || command === 'wow_cwm_one') ? BY_WM[args.marker]
                : command === 'wow_tm' ? BY_TM[args.marker] : null;
            return mark ? { mark } : null;
        },
    });
})();
