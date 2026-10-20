# Touch Dashboard — Design System

One place for the rules behind the UI. The pieces:

| What | Where | Enforced by |
|------|-------|-------------|
| Token **values** and theme presets | `static/themes.js` (the JSON block between the `TD-THEMES` markers) | `tests/test_themes.py` |
| Token **use** (components, layout) | `templates/index.html` — reads only `var(--token)` | `tests/test_themes.py::test_template_uses_tokens_only` |
| The **rules** (this file) | `docs/DESIGN.md` | review |

If a value is not in `themes.js`, it is not part of the design. If a rule here and the code disagree, fix the code or change the rule here, never only one of them.

---

## 1. The idea

Touch Dashboard is a **hardware control surface** that happens to be made of pixels: keys you press, faders you pull, status lights that tell you what is live. Every visual decision follows from that object, not from a generic "dashboard" look.

- **Keys** are solid tiles, one lightness step above their panel. Pressing one scales it to 0.96 and lifts it one more step, like a key going down.
- **Faders** are channel strips. Scale marks at 25 / 50 / 75 % segment the level fill, the way a console fader has printed ticks. The value sits above the strip, never on top of the fill.
- **State lights**: red means muted or destructive, green means live (camera, screen share, someone speaking), amber means attention (battery, load). Status colours are for status only.
- **The brand hue** (cyan in Graphite, taken from the app icon) goes on the main data element (the audio levels) and almost nothing else. The default theme carries about 3 % coloured pixels.

## 2. Principles (2026 baseline)

1. **Colour goes on data and state, not on chrome.** Navigation, panels and ordinary keys are neutral. One accent per theme.
2. **Elevation is a lightness step.** `ground` → `surface` (panels) → `surface-2` (keys, tracks, fields) → `surface-3` (pressed, active). No borders between surfaces unless a theme needs them (`key-line-w`, `panel-line-w`), no shadows on anything at rest.
3. **Real shadows only on what floats**: dialogs and the window menu (`--shadow`).
4. **Sentence case everywhere.** "Now playing", "System monitor", "Save & apply". Proper names keep their capitals ("Command Center"). No tracked uppercase micro-labels.
5. **Numbers are tabular** (`.num` → `font-variant-numeric: tabular-nums`) so updating figures never jitter. Load figures are whole percentages. A missing value is an em dash (`—`), a real zero is printed as `0%`. Units are a smaller, quieter suffix (`.unit`).
6. **State never relies on colour alone.** Mute swaps the icon (`mic` → `mic-off`) and adds a slash, and the fader prints "Muted". Discord mute/deafen draw a slash through the glyph.
7. **Motion is feedback**, not decoration: press, open, close, a panel arriving. Transform and opacity only. Everything honours `prefers-reduced-motion`.
8. **Touch first.** Targets are at least 44 CSS px on a tablet (the rem scale keeps them proportional), hover styles exist only for fine pointers, touch listeners are passive unless they cancel.
9. **Honest empty states.** "Nothing playing", an empty battery glyph with a dash, a dashed "Add" slot. No illustrations, no fake data.

## 3. What makes a UI look machine-made — and what replaces it

Each line pairs the habit with what this project does instead. Treat any of the left column appearing in a diff as a review comment.

| Machine-made habit | Here instead |
|---|---|
| Navy or slate dark theme with cyan/violet glow | True neutral darks (`#000`, `#111`, `#222`), no glow. A warm or cool tint only as a deliberate preset (Ember, Phosphor). |
| Glassmorphism, `backdrop-filter: blur()` | Opaque surfaces one lightness step apart. (Also mandatory for performance, see §8.) |
| Gradients on panels, buttons, text, fills | Flat fills. The fader is one colour; the segments come from scale marks. |
| Soft drop shadow under every card | No shadow at rest; dialogs only. |
| Tiny uppercase letter-spaced labels ("NOW PLAYING") | Sentence-case panel titles at a readable size (`--fs-sm`, weight 600). |
| Icon in a tinted circle on every tile | Plain 1.75 px outline icons, neutral unless they carry state. |
| Accent colour on every button and border | Accent on levels and the speed-test download arc. The primary action is an inverted key (white on dark, near-black on light). |
| Accent flash on every press | Press = scale 0.96 + one lightness step. |
| Gradient text headings | Plain ink, one weight. |
| Decorative particles, pulsing rings, infinite animations | Nothing moves unless something changed. The lock screen drifts once a minute, which is burn-in protection with a purpose. |
| Placeholder `--` at full size reading as "loading" | Em dash at the value's size, in the tertiary text colour. |
| Rainbow status (a hue per item) | Three status hues with fixed meanings (§1). |
| Default "Inter everywhere" | A system stack per theme (see §6), with tabular figures, and a monospaced figure face as a deliberate preset choice. |
| `outline: none` on everything | Visible `:focus-visible` ring in `--focus`. |
| Sticky hover on touch screens | Hover only inside `@media (hover: hover) and (pointer: fine)`. |

## 4. Tokens

### Colour roles (every preset defines every role)

| Token | Used for | Minimum contrast (tested) |
|---|---|---|
| `ground` | Page background, lock screen | — |
| `surface` | Panels, dialogs, top bar | — |
| `surface-2` | Keys, fader tracks, fields, grouped settings rows | — |
| `surface-3` | Pressed/active key, active tab pill, meter tracks | — |
| `line` | Faint dividers (settings rows, tab column edge) | — |
| `line-strong` | Field borders, dashed "Add" slot, battery outline | — |
| `panel-line` / `key-line` | Optional rings, drawn only when the width token is > 0 | — |
| `text` | Primary ink | 7:1 on ground, surface, surface-2 |
| `text-2` | Secondary text, labels, icons | 4.5:1 on surface, surface-2 |
| `text-3` | Meta, placeholders, empty states | 4.5:1 on surface, surface-2 |
| `accent` | Fader fills, progress, speed-test arc, brand mark | 3:1 on surface, surface-2 (graphic) |
| `accent-text` | Accent used as text or small icons | 4.5:1 on surface, surface-2 |
| `accent-ink` | Text on an accent fill, selection | 4.5:1 on accent |
| `inverse` / `inverse-ink` | Primary key (play, Save, Connect), "on" switch, weekday pill | 4.5:1 |
| `positive` / `positive-ink` | Live camera/screen share, speaking ring, battery high, feedback on | 3:1 graphic, 4.5:1 ink |
| `warning` | Battery mid, load ≥ 85 %, ping, notices | 3:1 graphic |
| `negative` / `negative-ink` | Muted keys, critical load ≥ 95 %, errors | 4.5:1 ink on fill |
| `negative-text` | Red as text or icon (Muted label, power icon, errors) | 4.5:1 on surface, surface-2, negative-soft |
| `negative-soft` | Destructive buttons at rest (Clear authorization, Leave, Stop audio) | — |
| `meter-gap` | Fader scale marks drawn over track and fill | — |
| `scrim` | Dialog backdrop (opaque-ish, never blurred) | — |
| `shadow` | Dialogs and the window menu only | — |
| `focus` | Keyboard focus ring | 3:1 on ground, surface |

### Base tokens (shared, a preset may override)

| Token | Value | Notes |
|---|---|---|
| `pad`, `gap`, `gap-sm` | 1.25rem, 1.25rem, 0.75rem | One gap for the grid, one inside groups of keys |
| `fs-xs` … `fs-xl` | 1 / 1.15 / 1.35 / 1.75 / 2.6 rem | Five steps. Nothing below `fs-xs` (≈ 11 px on a 1280×800 tablet). |
| `fs-clock` | 5.75rem | The one glance figure |
| `radius-panel`, `radius-key`, `radius-media`, `radius-pill` | 1.5, 1.05, 0.6 rem, 999px | Nested radii step down |
| `line-w`, `key-line-w`, `panel-line-w` | 1px, 0, 0 | High contrast sets the last two to 2px |
| `font-sans`, `font-num` | System stack; `font-num` defaults to `font-sans` | Phosphor sets a monospaced `font-num` |
| `weight-clock`, `weight-strong` | 500, 600 | High contrast uses 700 |
| `dur-fast`, `dur`, `ease` | 110ms, 180ms, `cubic-bezier(.2,.8,.2,1)` | |

The root font size scales with the viewport (reference 2560×1600 → 22 px), so every rem value scales the whole surface as one unit. Do not mix in px for layout.

## 5. Presets

| Preset | Why it exists |
|---|---|
| **Graphite** (default) | True black (OLED-friendly, calm at night), brand cyan on the levels only. |
| **Daylight** | A bright room: white keys with a hairline edge on cool grey. Deeper cyan so it holds contrast on white. |
| **Ember** | Late night: warm near-black and amber, low blue light. |
| **Phosphor** | Monitoring feel: cool black, green levels, monospaced figures. Green is also its positive colour. |
| **High contrast** | Glare or low vision: pure black and white, outlined keys and panels, yellow levels, heavier weights. |
| **Classic** | The original midnight-blue palette for anyone who liked it, minus the gradients and glow. |

### Adding a preset

1. Copy an existing preset object inside the `TD-THEMES` JSON block in `static/themes.js`.
2. Give it a unique lowercase `id`, a `name`, a one-sentence `note` and a `scheme` (`dark` or `light`).
3. Define **every** colour role. Base tokens are optional overrides.
4. Run `python -m pytest tests/test_themes.py -v`. It fails on a missing role, an unknown token or any contrast pair below its minimum.
5. The Themes tab lists it automatically and renders its preview from its own tokens.

## 6. Type

- **System stacks, deliberately.** The page is served over the LAN and often runs with no internet, and the CSP allows only `'self'`, so web fonts from a CDN never loaded. `system-ui` resolves to Roboto on Android, Segoe UI Variable on Windows and the desktop's sans on Linux, all of which have real tabular figures.
- To ship a bundled face later: add the `.woff2` and its licence under `static/fonts/`, declare an `@font-face` in the template, and point `font-sans` (or one preset's) at it.
- Five sizes do the work (§4). The clock is the only large figure; everything else is app-sized.

## 7. Components (quick reference)

| Component | Classes | Notes |
|---|---|---|
| Panel | `.card.panel` | `surface`, `radius-panel`, optional ring via `panel-line-w` |
| Key | `.btn` | `surface-2`, press scale + `surface-3` |
| Primary key | `.btn.btn-primary` | Inverted. One per view. |
| Destructive | `.btn.btn-danger` | `negative-soft` + `negative-text`; becomes a filled `negative` only as a state (`.muted`) |
| Secondary | `.btn.btn-ghost` | Transparent with a `line-strong` ring |
| Pill action | `.btn-pill` | Dialog and settings actions |
| Empty / add slot | `.btn-add` | Dashed `line-strong`, accent icon |
| Fader | `.pro-slider` › `.fader-val` + `.slider-track-area` (`.slider-fill`, `.fader-scale`) + `.slider-icon` | `touch-action: none` on the track |
| Meter | `.stat-meter` › `.stat-meter-fill` | Width via `transform: scaleX()`, `.is-high` / `.is-critical` |
| Switch | `.switch` › `input` + `.slider` | Inverted when on |
| Notice | `.notice` (+ `.is-danger`) | `surface-2` box with a status-coloured icon |
| Settings group | `.setting-group` › `.setting-row` | One container split by rules, not a box per row |

Icons: lucide outline, 1.75 px stroke on keys. Always render them with `renderIcons(root)` or `setIcon(el, name)` (see AGENTS.md, the bundled lucide rebuilds every icon on the page otherwise).

## 8. Performance rules

The desktop window runs WebKitGTK with GPU compositing disabled (NVIDIA/XWayland workaround), so every pixel that changes is software-rasterised. The tablet is often a low-end Android WebView. Therefore:

- No `backdrop-filter`, no 3D transforms (`translateZ`, `translate3d`) and no `will-change` at rest. GSAP keeps its default `force3D: "auto"`.
- No infinite animations on large areas. The charging battery steps 4 times per 2 s; the lock screen moves once a minute.
- Never CSS-transition a property GSAP also tweens (key opacity, transforms on animated layers).
- Write text with `textContent`, never read `innerText` in update paths (it forces layout). Meters use transforms, not width.
- DOM updates from `sys_data` run inside one `requestAnimationFrame` and skip unchanged values.

## 9. Before you ship a UI change

- [ ] No colour literal in `templates/index.html` (`pytest tests/test_themes.py` passes).
- [ ] Looked at it in Graphite, Daylight and High contrast.
- [ ] Every new icon-only control has an `aria-label`; every new state is readable without colour.
- [ ] Hover styles sit inside the fine-pointer media query.
- [ ] Nothing new animates forever, blurs, or uses a 3D transform at rest.
- [ ] Any user, media or device string reaching `innerHTML` goes through `escapeHtml()`.
