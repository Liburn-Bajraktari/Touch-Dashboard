# Touch Dashboard — Design System

One place for the rules behind the UI.

| What | Where | Enforced by |
|------|-------|-------------|
| Token **values** and theme presets | `static/themes.js` (the JSON block between the `TD-THEMES` markers) | `tests/test_themes.py` |
| Token **use** (components, layout) | `templates/index.html`, which reads only `var(--token)` | `tests/test_themes.py::test_template_uses_tokens_only` |
| The **rules** (this file) | `docs/DESIGN.md` | review |

If a value is not in `themes.js`, it is not part of the design. If a rule here and the code disagree, fix the code or change the rule here, never only one of them.

---

## 1. The idea: a broadcast console

Touch Dashboard is a piece of studio hardware that happens to be made of pixels: the kind of desk unit a streamer keeps beside the PC (a RØDECaster, a GoXLR, a Stream Deck), built with the depth, light and motion of a 2026 interface. Every component is an object from that world:

| On the screen | The object it is |
|---|---|
| Panels | Machined module plates, lit from above |
| Volume | **Studio faders**: a recessed slot lit from inside, a printed scale, a grip cap with an index line |
| Output switch | A **rotary encoder** with an LED ring, one LED per output device |
| Transport | A **deck's transport keys**: ⏮ ⏯ ⏭ for music; for an episode, a film or a video the same row gains ↺10 and ↻30, the step printed inside the arrow. A key the player cannot use is unlit (`--text-3` ink) rather than dead. The progress bar is a scrub strip: drag it to a point |
| Macros | **Display keys**: an LCD screen set into each keycap (Stream Deck) |
| Soundboard | **Drum pads** that light when struck (MPC) |
| Levels, names, figures | **Scribble strips and displays**: recessed dark windows |
| Mute, live, speaking | **Lit keys and LEDs**: the state is light, and on rich displays it spills |
| System load | Display tiles with a live trace of the last minute |
| Choices (key type, icon, app, sound) | A **selector tray**: a recessed well of options; the chosen one rises out of it as a keycap |
| The clock, full screen | A **desk clock**: the time large, the weather in detail, what is playing and the call, straight on the dark ground, drifting a little each minute |
| Questions and notices | A **dialog plate**: its glyph lit in a small display window, a sentence-case title, then the keys that answer it |
| Key and pad editors | The key or pad itself, drawn exactly as on the console, beside the selectors that change it |

Two things make it feel alive rather than rendered:

- **The music lights the room.** The most vivid colour in the current cover becomes `--dyn`: the play key, the progress bar, the cover's shadow and the ambient light behind the modules. Nothing playing, and the theme's own glow takes over. The play key can keep the theme's accent instead (Settings → Themes → Cover colour on the play key); it also takes the accent whenever the cover gives no colour.
- **It answers touch like hardware.** Keys sink and spring back, the encoder overshoots and settles, fader caps lift while held, and on Android every press, detent and limit is a haptic tick.

## 2. The light model

One light, above the console. Every rule below follows from it, and nothing may contradict it.

1. **Raised things are lit on their top edge** (`--rim`, an inset 1px highlight) **and cast a shadow below** (`--depth`). Modules use `--elev-1`, keys `--elev-2`, caps and knobs `--elev-cap`.
2. **Keys have thickness**: a darker lip on the bottom edge (`--edge`). Pressing a key sinks it: the lip turns into an inner shadow (`--elev-press`) and the key drops 0.12rem.
3. **Recessed things are shaded from the top** and catch light on their bottom edge (`--well-inset`). There are two kinds:
   - **Wells** (`--well`): recessed containers such as key screens, the voice list, load tiles, fader slots and chips. A well is one gentle step from its plate, in the plate's own tint: walnut on Ember, navy on Midnight, light grey on Daylight. Never drop a tinted console's wells to near-black: below about 0.13 OKLCH lightness an 8-bit colour has too few steps to hold a hue, and the recess reads as a hole.
   - **Displays** (`--display`): small readouts, namely the fader values, the output device, the speed test and the battery. They are dark LCD windows on the dark consoles, a step deeper than the wells and still tinted; Daylight keeps them light, the same soft grey as its wells, so nothing on it is darker than text. A readout gets the display palette through one CSS rule that remaps `--well*` to `--display*` inside it, so a theme can give its readouts their own look.
4. **Gradients only model light on an object**: a module plate (`--surface-top` → `--surface`), a keycap (`--key-top` → `--surface-2`), a fader cap or knob (`--cap-hi` → `--cap` → `--cap-lo`). Never a decorative colour-to-colour gradient, never on text.
5. **Glow only comes from something that is lit**: an LED, a lit fader slot, a lit key, the cover's colour. A glow never decorates a resting object.
6. **No glass.** No `backdrop-filter`: it is meaningless over a dark plate and ruinous on the desktop window (§8).

## 3. Principles

1. **Colour belongs to data, state and light.** Plates, keys and text are neutral. One accent per theme carries the levels; status hues carry meaning (red muted, green live/speaking, amber attention); the cover's colour carries the music. The accent and the three status hues stay at least 0.11 apart in OKLab (tested), so a lit key, an LED and a load trace never look alike. When the accent takes a status hue's colour family (Phosphor's green, Ember's amber), the status hue moves, not the accent.
2. **Displays hold the numbers.** Values that change live (levels, battery, load, device names) sit in recessed display windows in tabular figures. Static labels sit on the plate.
3. **Sentence case.** "Now playing", "Output device", "Save & apply". Proper names keep their capitals ("Command Center").
4. **Value balance.** Large neighbouring surfaces (ground, plates, keys, wells, displays) stay within 1.5:1 of each other (tested); the extreme values belong to text and small details. On a light theme, near-black is for text and icons only, never containers or readouts (Daylight: under 1 % of the screen).
5. **Lit keys share one recipe.** A key that is on (live, muted, camera on) is a bright status fill with dark ink, like a backlit cap. Mute is no exception: it is a lit red key, not a dark red one with white ink.
6. **Honest data. Missing is an em dash; a real zero prints. The load traces are the actual last minute of samples. The progress bar appears only when the player reports a length.
7. **State never relies on colour alone.** Mute swaps the icon and the fader display prints "Muted" (the fader stays at its level, as on a desk: only its slot goes dark); Discord mute/deafen draw a slash through the glyph; the active encoder LED also has the knob pointing at it.
8. **Motion is physics, used for feedback.** Springs, not linear slides (§7). Nothing moves unless something changed, apart from one settle-in when the page first loads.
9. **Touch first.** Targets of at least 44 CSS px on a tablet; hover styles only for fine pointers; passive touch listeners unless they cancel; `touch-action: none` on faders; a grabbed fader cap never jumps.

## 4. What machine-made looks like, and what replaces it

| Machine-made habit | Here instead |
|---|---|
| Glassmorphism, `backdrop-filter: blur()` | Opaque plates lit from above; light from the cover tints the space between them |
| Decorative gradients, gradient text, purple-to-blue washes | Gradients only as light on an object (§2.4) |
| A soft drop shadow on every card, from nowhere in particular | One light source: rim on top, depth below, consistently |
| Glows on idle borders and icons | Glow only from something lit (§2.5) |
| A plain range slider | A studio fader: slot, scale, cap, index line |
| Generic flat buttons for every control | Each control is its object: encoder, display key, drum pad, lit key |
| Tiny uppercase letter-spaced labels | Sentence-case labels at a readable size; live values in display windows |
| A static accent colour everywhere | Theme accent on levels; the cover's colour on the media and the room |
| Percentage bars for load | A live trace of the real last minute |
| Big dark blocks dropped onto a light theme | Soft grey recesses; dark only for text and icons |
| Linear 200 ms transitions everywhere | Springs with a measured overshoot (§7) |
| Spinners, pulsing decoration, particles | A ring pulses only around someone who is speaking |
| `outline: none` on everything | A visible `:focus-visible` ring in `--focus` |
| Sticky hover on touch screens | Hover only inside `@media (hover: hover) and (pointer: fine)` |
| The browser's `alert()` / `confirm()` boxes (unthemed system windows in the desktop window and on Android) | `tdNotice()` / `tdConfirm()`: a themed dialog; a destructive answer is a lit red key and the safe one has focus |
| A dropdown `<select>` for a handful of choices, a raw text field for an icon name | A selector tray showing every choice, and an icon grid; a search field over long lists |
| Raw key codes (`KEY_LEFTMETA + KEY_L`) | Printed keycaps in a display: `Super` `+` `L` |

## 5. Tokens

### Colour and material roles (every preset defines every role)

| Token | Used for | Minimum contrast (tested) |
|---|---|---|
| `ground` | Page background, lock screen | — |
| `surface`, `surface-top` | Module plates (bottom, lit top) | text 7:1 on both |
| `surface-2`, `key-top` | Keycaps (bottom, lit top), grouped settings rows | text 7:1 on both |
| `surface-3` | Pressed keys, meter tracks, unlit LEDs | — |
| `line`, `line-strong` | Faint dividers; field borders, scale ticks, dashed slots | — |
| `panel-line`, `key-line` | Outlines, drawn only when the width token is > 0 (Daylight's keys) | — |
| `text`, `text-2`, `text-3` | Ink on plates and keys | 7 / 4.5 / 4.5 :1 |
| `well`, `well-ink`, `well-ink-2`, `well-accent` | Recessed containers: background, text, labels, lit values | 7 / 4.5 / 4.5 :1 on `well`; accent, status hues 3:1 |
| `display`, `display-ink`, `display-ink-2`, `display-accent` | Small readouts (dark LCD windows on dark consoles, light on Daylight) | 7 / 4.5 / 4.5 :1 on `display`; status hues 3:1 |
| `accent` | Fader light, encoder LEDs, progress, brand mark, the play key without a cover colour | 3:1 on surfaces and wells (graphic) |
| `accent-text`, `accent-ink` | Accent as text; ink on an accent fill | 4.5:1 |
| `inverse`, `inverse-ink` | Primary pill actions, weekday pill | 4.5:1 |
| `positive`, `positive-ink` | Live keys, speaking ring, link light, battery high | 3:1 graphic, 4.5:1 ink |
| `warning` | Battery mid, load ≥ 85 %, ping | 3:1 graphic |
| `negative`, `negative-ink`, `negative-text`, `negative-soft` | Mute, critical load, errors, destructive actions | 4.5:1 ink; 3:1 on wells |
| `cap-hi`, `cap`, `cap-lo`, `cap-index` | Fader caps, the encoder knob, switch knobs | index 3:1 on cap |
| `rim`, `edge`, `depth` | The light model: top highlight, bottom lip and inner shade, cast shadow | — |
| `glow` | The theme's own ambient light (8-digit hex with alpha) | — |
| `meter-gap`, `scrim`, `shadow`, `focus` | Spare, dialog backdrop, floating shadow, keyboard focus | focus 3:1 |

### Base tokens (shared, a preset may override)

| Token | Value | Notes |
|---|---|---|
| `pad`, `gap`, `gap-sm` | 1.25 / 1.25 / 0.75 rem | |
| `fs-xs` … `fs-xl`, `fs-clock` | 1 / 1.15 / 1.35 / 1.75 / 2.6 / 6.4 rem | Nothing below `fs-xs` (≈ 11 px on a 1280×800 tablet) |
| `radius-panel`, `radius-key`, `radius-media`, `radius-pill` | 1.6, 1.1, 0.9 rem, 999px | Multiplied by `--sq` (§6) |
| `elev-1`, `elev-2`, `elev-press`, `elev-cap`, `well-inset` | Composite shadows | Built from `rim`, `edge`, `depth`, so presets only set those three |
| `weight-clock`, `weight-strong` | 300, 600 | Phosphor 400 |
| `cap-h` | 3.4rem | Fader cap height; the travel ends at cap centres |
| `backdrop-opacity` | 0.5 | Daylight 0.32 |
| `on-bright`, `on-deep` | near-black, white | Ink chosen for the cover-coloured play key |
| `mask` | opaque black | The solid part of a `mask-image` gradient (only its alpha counts): the marquee's edge fade |
| `dim` | black | Laid over a screen at partial opacity to dim it for burn-in care (idle home screen, lock screen, full-screen clock); on a light theme it darkens the ground too |
| `spring`, `spring-bounce` | CSS `linear()` curves | §7 |
| `dur-fast`, `dur`, `dur-spring`, `ease` | 110, 180, 460 ms; ease-out | |

### Runtime values (set by the page, never by a theme)

| Variable | Set by | Meaning |
|---|---|---|
| `--dyn`, `--dyn-ink`, `--dyn-glow`, `--dyn-shadow` | `applyDyn()` from the cover, on the Now playing card, the full-screen clock and the lock screen only | The music's colour, fitted per scheme so `--dyn-ink` keeps ≥ 4.5:1; unset when nothing plays. Set on `:root`, a new colour restyled the whole page; the same colour again (the next track of an album) changes nothing |
| `--ambient` | CSS, on the lock screen and the full-screen clock | `--dyn-glow` if set, else `--glow`; their backgrounds change at once |
| `--light` | `applyAmbient()`, on one `.ambient` layer | The ambient light behind the dashboard: the colour that layer shows (unset: `--glow`) |
| `--v` | the fader | 0–1, drives the lit slot and the cap by transform |
| `--angle` | the encoder | Where the knob points |
| `--sq` | `@supports (corner-shape: squircle)` | 1.6 where continuous corners exist, else 1 |

## 6. Shape

- Continuous (squircle) corners on plates, keys, displays and dialogs where the engine supports `corner-shape` (Chromium 139+, so the Android app). Squircles need about 1.6× the radius to read the same, hence `--sq`. Round things (`.round`, knobs, LEDs, avatars) stay circles.
- Nested radii step down: plate → key → screen inside the key.
- Discord's voice keys are a connected group: inner corners tight, outer corners round; a key that turns on morphs into a pill.

## 7. Motion

| Use | Curve | Duration |
|---|---|---|
| Key release, encoder turn, switch knob, pad press, theme card | `spring-bounce` (ζ ≈ 0.55, 12.5 % overshoot) | 460–620 ms |
| Fader settle, modal open (rich only: on lite a dialog appears at once, §8), Now Playing scale, clock digits | `spring` (ζ ≈ 0.75, 3 %) | 280–560 ms |
| Press-in, colour changes | `ease`, `dur-fast` | 110 ms |
| Progress bar | steps once a second with the times, on every tier (§8.1) | — |
| Ambient light (rich) | two layers cross-fade by opacity, `ease` | 1.4 s |
| Now playing marquee | rests 2.5 s, then 32 px/s: linear on rich, whole-pixel steps on lite (§8) | one lap per copy width |
| First load | modules rise and fade in once (GSAP stagger 60 ms) | 700 ms |
| Soundboard (rich) | zooms evenly out of its key, fading in over it, and back into it, fading onto it; the dashboard dims under a `--ground` layer; the top bar rolls over like the clock digits (`spring`) | 520 ms open (ease-out, no overshoot: a full-size plate would bulge past its edges), 420 ms close (ease-in-out) |

- The springs are generated from a damped oscillator and written as `linear()`; engines without `linear()` fall back to `ease`.
- While a fader is held its transitions switch off, so the cap is glued to the finger.
- **Reduced motion**: CSS durations collapse to 1 ms and GSAP runs 40× faster through `gsap.matchMedia()`. The first-load entrance is skipped. The marquee runs one lap per track, then rests.
- **Haptics** (Android, coarse pointers): 8 ms on any key, 5 ms per 10 % fader detent, 14 ms at 0 %, 100 % and the mute pull.
- **Transport sizing.** The row is sized from the Now playing card, not the screen (`.spotify` is a size container): each key is its full size (play 6, ↺/↻ 4.2, ⏮/⏭ 3.6 rem with five keys; 6 and 4.6 with three) or its share of the card's width, whichever is smaller. The shares add up to the whole width and play's is the largest, so the row can never leave the card and play shrinks last. The rem-scaled layout keeps full sizes everywhere except at the 6 px root floor (a phone in portrait). Glyphs and the printed step scale with the key.
- **Seeking.** ↺/↻ and the scrub strip move the progress at once and hold it until the player reports the same place; quick taps add up and go out as one seek. While dragging, only the fill's transform and the times' text change (on rich the strip also thickens by `scaleY`), so it stays cheap in the Linux window.

## 8. Performance and effects tiers

The desktop window on Linux is WebKitGTK with GPU compositing disabled (NVIDIA/XWayland workaround), so every changing pixel is rasterised by the CPU. `themes.js` sets `html[data-fx]` before first paint:

| Tier | Where | What changes |
|---|---|---|
| `rich` | Android app, browsers, Windows WebView2 | Everything in this file |
| `lite` | Linux desktop window (WebKitGTK) | No light spill (slot, LED, key glows), no speaking pulse, no ambient cross-fade (the light changes at once, by visibility), no first-load entrance; keys press and light without changing their shadow; the marquee steps a whole pixel every few frames and has no edge fade |

### What costs a full repaint in the Linux window

Measured in a headless copy of the desktop window. Before these rules, the window idled at 27 % of a core, a fader drag took 97 % and three fast Discord taps 40 %. After them, idle is 1–4 %, the drag 15 % and the taps 4 %. With the console's shadows and gradients, a full-page repaint costs about 30 ms of CPU, and WebKitGTK does one for:

1. **A layout that reaches the page root.** Changing in-flow text does this: a figure, a time, a label. `contain` does not stop it here. **Live figures** (anything that changes every second, or on every frame of a drag) sit in a `.live` box: a fixed height `--live-h`, with the text in an absolutely positioned `.live-v` child. That box is a layout boundary, so a new figure repaints only itself. The fader readouts, track times, load figures and speed test figures use it.
2. **Any change to an element's `box-shadow`**, even a colour on an inset shadow. So on the lite tier a key presses by transform and lights by fill only. The press shade and the lit-key glow change shadows on the rich tier only, under `:where([data-fx="rich"])`, which keeps the specificity unchanged. A state that must show a ring uses a pseudo-element that is shown and hidden (the speaking ring), not a shadow.
3. **An `opacity` crossing 1**, which gives the element a layer of its own. Toggle `visibility` instead (the mute slash).
4. **Any change under a full-screen dialog** (or the full-screen clock, which holds the dashboard the same way). With a dialog open, even an 8 px box changing behind it costs a ~50 ms repaint of the whole window (measured: an empty transparent full-screen layer does it too). So on the lite tier, while any `.modal-overlay.active` is open, the dashboard behind it holds still (`holdDashboard()`, `html[data-held]`): `sys_data` waits in `pendingSysData` (only sign-in status and the lock screen still apply), the progress timer and the marquee pause, and CSS animations behind it stop. It catches up when the last dialog closes. Dialogs there also open without the fade and spring (each frame of those cost ~80 ms: Settings opened at ~12 fps for 0.6 s; now one ~57 ms frame) and without the card's two large blurred shadows (~15 ms of every frame).
5. **Scrolling a box.** WebKitGTK cannot move the pixels, so each frame repaints the whole scrolled box and everything under it, the dimmed dashboard included. While a dialog is open (`html[data-held]`) the dashboard is drawn flat: no shadows, plates without their lit top, no ambient light (under the scrim it barely shows). Inside a dialog, draw lines and rims with borders rather than inset shadows: WebKitGTK draws each inset shadow through a buffer the size of the area it repaints (the card's 1 px rim cost ~6 ms a frame); a zero-width shadow costs nothing. Settings scrolled at ~13 fps (75 ms a frame); now ~40.

Background, ink colour, corner radius, `visibility` and `transform` changes are cheap.

Always, on both tiers:

- Anything that moves uses `transform`; a fade uses `opacity` only where it animates anyway (entrances, GSAP), never as an on/off toggle. The fader and encoder are driven by one custom property each; a drag never triggers layout.
- Each live figure sits in a `.live` box (above).
- One sparkline point per server sample (`load_seq`): event-driven `sys_data` (Discord, volume read-backs) never adds history.
- No `backdrop-filter`, no 3D transforms at rest, no `will-change` at rest except the rich tier's deliberate layers (§8.1).
- No CSS transition on a custom property (test-enforced): it animates on the main thread and repaints all that paints it, every frame.
- No infinite animation except the speaking pulse (rich only, small), the charging battery (4 steps per 2 s), the Now playing marquee, which only runs while a title overflows and pauses under the lock screen, and the speed test's search sweep, waiting dots and (rich only) flowing arrows, which run only during a test (measured on the Linux window: 12–16 % of a core for those ~20 s, 3 % idle).
- Never CSS-transition a property GSAP also tweens (key opacity during the soundboard transition).

### 8.1 What costs on the tablet (rich tier)

The Android app draws with the GPU, but a mid-range tablet's GPU (Redmi Pad 2: 2560×1600) has little to spare: every frame of any animation recomposites the whole window, and GPU raster re-rasters tiles that are strips as wide as the screen. Traced in Chrome as that tablet (1280×800 at 2×, touch, Android app user agent), v0.11.14 against now:

| Per action | Before | After |
|---|---|---|
| ⏭ (new track and colour): raster | 1269 ms (1511 tiles, nearly all the page layer) | ~24 ms |
| ⏭: main-thread frames; longest at 6× CPU throttle | 112; one frame of 58–66 ms | ~38; nothing over 20 ms |
| ⏯: raster | 8–19 ms | ~1 ms |
| Idle while music plays: frames drawn | ~63 a second (the progress glide) | ~1–2 a second |

The rules that got there:

- **Nothing animates continuously by default.** The progress bar steps once a second with the times; its glide (back-to-back 1 s transitions) kept the compositor drawing every frame for as long as anything played. What still runs continuously: the marquee while a title overflows, and the speaking pulse while someone talks.
- **Cross-fade layers, never a background.** The ambient light is two fixed `.ambient` layers under the modules (`will-change: opacity`); a new colour goes to the hidden one, which fades in over the other. Fading it in `body`'s background repainted the whole page layer on every frame of the 1.4 s fade.
- **Shadows never animate.** A shadow can only animate on the main thread, repainting the element every frame: keys swap to the press shade in one repaint while the compositor sinks them, and the play key's cover colour fades but its coloured shadow changes at once.
- **What redraws every second is a small layer** (`will-change: transform`): the sparklines and the progress fill. So are the transport keys, which otherwise got a layer for each press's sink and lost it after the spring, re-rastering the card under them. Not the `.live` figure boxes: as layers they pulled ~25 overlapping elements into layers with them. Check the layer list after adding one (CDP `LayerTree`, compositing reason `Overlap`).
- **The cover's colour sits on its three consumers** (Now playing card, full-screen clock, lock screen), not `:root`: a new colour restyles those subtrees, not the page.
- The cover backdrop is the cover drawn into an 8×8 canvas and stretched: a blur with no filter cost.
- Write text with `textContent`; never read `innerText` in an update path.
- **Transitions are Web Animations of transform and opacity on few elements.** They run on the compositor and take the `linear()` springs as they are. Every element with a running animation is still restyled on the main thread each frame, so a stagger across many elements costs: pads rippling in one by one, even by opacity alone, halved the compositor's frames. Animate the container, not each item. A clip-path animation repaints its element every frame (Chrome cannot composite it). GSAP's CSS tweens read the target's computed style as each tween starts, staggered ones included, and convert `rem` through a temporary element, forcing style and layout mid-frame; keep GSAP for one-off moves of a few elements.

### Burn-in care (OLED)

A tablet can show the console for hours. What stays still for long burns in, so:

- **Full-screen views** (the lock screen, the full-screen clock) sit on the dark ground with no plates. Every minute their content moves to a new spot, spread evenly over the free space around it (a Halton sequence, never off screen), and after five minutes the screen dims by half. Nothing on them changes faster than the minute apart from the music.
- **The home screen**, on tablets: it shifts by a pixel or two every three minutes, dims after a few idle minutes (the first touch only wakes it; it never presses a key), and later shows the full-screen clock. Both delays are in Settings → General → Screen care.
- Dimming lays `--dim` over the screen, so a light theme's ground darkens too. On the lite tier these changes jump instead of gliding.

## 9. Presets

| Preset | Why it exists |
|---|---|
| **Graphite** (default) | True-black hardware for OLED and night use, entirely in greys: the levels, LEDs and brand mark are lit silver, so the only colour on screen is status (mute, live, warnings) and the music. |
| **Daylight** | A satin-white console with soft grey recesses and silver caps, all light, for a bright room. |
| **Ember** | Warm walnut-black and amber: low blue light late at night. Warnings are pumpkin orange and mute a deep red, so neither is mistaken for the amber. |
| **Phosphor** | A monitor's P1 green on the levels, with monospaced figures. Live states take a cooler sea green and warnings the amber of a P3 screen, so the green levels keep their own meaning. |
| **Amethyst** | Plum-black plates with orchid lights, mint for live and coral for mute. The purple sits in the material and the accent, never as a gradient wash. |
| **Midnight** | Deep navy plates with cyan lights (the original look, formerly "Classic"). |

Retired ids still resolve: `aliases` in the registry maps `classic` to `midnight` and `contrast` (the former High contrast preset) to `graphite`, so a saved config never falls through to an unexpected theme.

### Adding a preset

1. Copy a preset object inside the `TD-THEMES` JSON block in `static/themes.js`.
2. Give it a unique lowercase `id`, a `name`, a one-sentence `note` and a `scheme` (`dark` or `light`).
3. Define **every** colour and material role (§5). Base tokens are optional overrides.
4. Run `python -m pytest tests/test_themes.py -v`: it fails on a missing role, an unknown token, any contrast pair below its minimum, an accent and status hue closer than 0.11 in OKLab, or neighbouring surfaces further apart than 1.5:1.
5. Renaming or removing a preset: add its old id to `aliases`, pointing at the preset that replaces it.
6. The Themes tab lists it automatically and previews it as a miniature console from its own tokens.

## 10. Type

- System stacks, chosen deliberately: the server's CSP allows only `'self'` and the console often runs offline on the LAN. `system-ui` resolves to Roboto on Android, Segoe UI Variable on Windows and the desktop's sans on Linux, all with real tabular figures.
- The clock is the one display figure (light weight, tight tracking); everything else is app-sized. Phosphor swaps figures to monospace.
- To ship a bundled face later: add the `.woff2` and its licence under `static/fonts/`, declare an `@font-face`, and point `font-sans` (or one preset's) at it. The APK bundles `static/`, so it travels with the app.

## 11. Before you ship a UI change

- [ ] No colour literal in `templates/index.html` (`pytest tests/test_themes.py` passes).
- [ ] It obeys the light model (§2): lit top, shadow below, wells shaded from the top.
- [ ] Looked at it in Graphite, Daylight and a tinted dark preset (Ember or Amethyst), with something muted, and with `data-fx="lite"`.
- [ ] Every new icon-only control has an `aria-label`; every new state reads without colour.
- [ ] Anything that moves uses transform/opacity and a token curve; nothing new animates forever.
- [ ] Nothing that changes every second sits in the text flow (use `.live`), and no state toggle changes a `box-shadow` or crosses `opacity: 1` on the lite tier (§8).
- [ ] Any user, media or device string reaching `innerHTML` goes through `escapeHtml()`.
- [ ] UI changes reach tablets only through a new APK: `./scripts/release.sh <tag> --apk`.
