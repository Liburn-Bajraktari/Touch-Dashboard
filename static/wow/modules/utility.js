/*
 * Raid utilities: Quick sim (utilities grid) opens Raidbots' Quick Sim on the
 * PC, where the string from the SimulationCraft addon (/simc) is pasted.
 * Clearing world markers has no key of its own: give it a key slot.
 */
(function () {
    'use strict';

    AddonModules.register({
        id: 'utility',
        name: 'Raid utilities',
        commands: ['sim_quick', 'cwm_clear'],
        columns: [
            {
                slot: 'grid', order: 40, render(el, api) {
                    const key = api.key({ glyph: 'calculator', label: 'Quick sim', value: 'Raidbots on the PC' });
                    key.addEventListener('click', () => api.send('sim_quick', null, key));
                    el.appendChild(key);
                },
            },
        ],
    });
})();
