/*
 * Touch Dashboard — design tokens and theme presets.
 *
 * This file is the single source of every colour, radius, type, depth and
 * motion value the UI uses. templates/index.html never hard-codes a design
 * value; it reads them through var(--token). The rules behind the tokens
 * (what each role is for, the light model, how to add a preset) live in
 * docs/DESIGN.md, and tests/test_themes.py checks every preset's contrast
 * and completeness.
 *
 * The block between the TD-THEMES markers must stay strict JSON: the test
 * parses it, so no comments, no trailing commas, double quotes only.
 *
 * Loaded synchronously in <head> so the saved theme and the effects tier are
 * in place on the first frame (no flash before the WebSocket connects).
 */
(function () {
  'use strict';

  const REGISTRY = /* TD-THEMES:BEGIN */ {
    "default": "graphite",
    "aliases": {
      "classic": "midnight",
      "contrast": "graphite"
    },
    "base": {
      "pad": "1.25rem",
      "gap": "1.25rem",
      "gap-sm": "0.75rem",
      "fs-xs": "1rem",
      "fs-sm": "1.15rem",
      "fs-md": "1.35rem",
      "fs-lg": "1.75rem",
      "fs-xl": "2.6rem",
      "fs-clock": "6.4rem",
      "radius-panel": "1.6rem",
      "radius-key": "1.1rem",
      "radius-media": "0.9rem",
      "radius-pill": "999px",
      "line-w": "1px",
      "key-line-w": "0px",
      "panel-line-w": "0px",
      "font-sans": "system-ui, -apple-system, \"Segoe UI Variable Text\", \"Segoe UI\", Roboto, \"Noto Sans\", Ubuntu, Cantarell, sans-serif",
      "font-num": "var(--font-sans)",
      "weight-clock": "300",
      "weight-strong": "600",
      "elev-1": "inset 0 1px 0 var(--rim), inset 0 0 0 var(--panel-line-w) var(--panel-line), 0 1.6rem 3.2rem -1.8rem var(--depth)",
      "elev-2": "inset 0 1px 0 var(--rim), inset 0 -0.2rem 0 var(--edge), inset 0 0 0 var(--key-line-w) var(--key-line), 0 0.45rem 0.9rem -0.45rem var(--depth)",
      "elev-press": "inset 0 0.22rem 0.5rem var(--edge), inset 0 0 0 var(--key-line-w) var(--key-line)",
      "elev-lit": "inset 0 1px 0 var(--rim), inset 0 -0.2rem 0 var(--edge), 0 0.45rem 0.9rem -0.45rem var(--depth)",
      "elev-cap": "inset 0 1px 0 var(--rim), 0 0.16rem 0 var(--cap-lo), 0 0.8rem 1.3rem -0.35rem var(--depth)",
      "well-inset": "inset 0 0.2rem 0.6rem var(--edge), inset 0 -1px 0 var(--rim), inset 0 0 0 var(--key-line-w) var(--key-line)",
      "on-bright": "#0a0a0b",
      "on-deep": "#ffffff",
      "mask": "#000000",
      "dim": "#000000",
      "cap-h": "3.4rem",
      "backdrop-opacity": "0.5",
      "dur-fast": "110ms",
      "dur": "180ms",
      "dur-spring": "460ms",
      "ease": "cubic-bezier(0.2, 0.8, 0.2, 1)",
      "spring": "linear(0, 0.016, 0.06, 0.122, 0.196, 0.277, 0.361, 0.444, 0.524, 0.599, 0.668, 0.73, 0.786, 0.834, 0.875, 0.91, 0.94, 0.964, 0.983, 0.997, 1.009, 1.017, 1.023, 1.026, 1.028, 1.028, 1.028, 1.027, 1)",
      "spring-bounce": "linear(0, 0.031, 0.11, 0.222, 0.351, 0.486, 0.618, 0.739, 0.845, 0.934, 1.005, 1.057, 1.094, 1.116, 1.125, 1.125, 1.118, 1.105, 1.09, 1.073, 1.056, 1.04, 1.026, 1.013, 1.003, 0.996, 0.99, 0.987, 1)"
    },
    "themes": [
      {
        "id": "graphite",
        "name": "Graphite",
        "scheme": "dark",
        "note": "True-black hardware with silver lights.",
        "tokens": {
          "ground": "#000000",
          "surface": "#111111",
          "surface-top": "#1a1a1a",
          "surface-2": "#222222",
          "key-top": "#2d2d2d",
          "surface-3": "#333333",
          "line": "#262626",
          "line-strong": "#444444",
          "panel-line": "#111111",
          "key-line": "#222222",
          "text": "#ffffff",
          "text-2": "#b4b4b4",
          "text-3": "#949494",
          "accent": "#d6d6d6",
          "accent-text": "#d6d6d6",
          "accent-ink": "#111111",
          "inverse": "#ffffff",
          "inverse-ink": "#000000",
          "positive": "#3fc07a",
          "positive-ink": "#04210f",
          "warning": "#e5a93b",
          "negative": "#f84b4b",
          "negative-ink": "#1f0405",
          "negative-text": "#ff6b70",
          "negative-soft": "#2c1a1d",
          "well": "#060606",
          "well-ink": "#f4f4f4",
          "well-ink-2": "#8f8f8f",
          "well-accent": "#d6d6d6",
          "display": "#060606",
          "display-ink": "#f4f4f4",
          "display-ink-2": "#8f8f8f",
          "display-accent": "#d6d6d6",
          "cap-hi": "#6a6a6a",
          "cap": "#3d3d3d",
          "cap-lo": "#1a1a1a",
          "cap-index": "#ffffff",
          "rim": "rgba(255, 255, 255, 0.07)",
          "edge": "rgba(0, 0, 0, 0.55)",
          "depth": "rgba(0, 0, 0, 0.8)",
          "glow": "#ffffff14",
          "meter-gap": "rgba(0, 0, 0, 0.34)",
          "scrim": "rgba(0, 0, 0, 0.74)",
          "shadow": "0 1.5rem 4rem rgba(0, 0, 0, 0.6)",
          "focus": "#d6d6d6"
        }
      },
      {
        "id": "daylight",
        "name": "Daylight",
        "scheme": "light",
        "note": "Satin white console with soft grey recesses, for a bright room.",
        "tokens": {
          "ground": "#e4e7eb",
          "surface": "#ffffff",
          "surface-top": "#ffffff",
          "surface-2": "#f1f3f5",
          "key-top": "#fbfcfd",
          "surface-3": "#e3e6ea",
          "line": "#e2e5e9",
          "line-strong": "#c9ced4",
          "panel-line": "#ffffff",
          "key-line": "#d7dbe0",
          "text": "#111316",
          "text-2": "#4a4f57",
          "text-3": "#5f6670",
          "accent": "#067a99",
          "accent-text": "#056a85",
          "accent-ink": "#ffffff",
          "inverse": "#111316",
          "inverse-ink": "#ffffff",
          "positive": "#1a7f45",
          "positive-ink": "#ffffff",
          "warning": "#a66b14",
          "negative": "#c62828",
          "negative-ink": "#ffffff",
          "negative-text": "#b42318",
          "negative-soft": "#fdecea",
          "well": "#e3e9ec",
          "well-ink": "#111316",
          "well-ink-2": "#535b64",
          "well-accent": "#056a85",
          "display": "#e3e9ec",
          "display-ink": "#111316",
          "display-ink-2": "#535b64",
          "display-accent": "#056a85",
          "cap-hi": "#ffffff",
          "cap": "#e6e9ed",
          "cap-lo": "#b9c0c8",
          "cap-index": "#111316",
          "rim": "rgba(255, 255, 255, 0.95)",
          "edge": "rgba(17, 19, 22, 0.16)",
          "depth": "rgba(17, 19, 22, 0.28)",
          "glow": "#067a991a",
          "meter-gap": "rgba(255, 255, 255, 0.5)",
          "scrim": "rgba(17, 19, 22, 0.42)",
          "shadow": "0 1.5rem 4rem rgba(17, 19, 22, 0.18)",
          "focus": "#056a85",
          "key-line-w": "1px",
          "backdrop-opacity": "0.32"
        }
      },
      {
        "id": "ember",
        "name": "Ember",
        "scheme": "dark",
        "note": "Warm walnut-black and amber, easy on the eyes at night.",
        "tokens": {
          "ground": "#0e0d0c",
          "surface": "#181614",
          "surface-top": "#211e1b",
          "surface-2": "#25221f",
          "key-top": "#312d29",
          "surface-3": "#34302c",
          "line": "#2b2825",
          "line-strong": "#4a4540",
          "panel-line": "#181614",
          "key-line": "#25221f",
          "text": "#f6f1ea",
          "text-2": "#c4b9ad",
          "text-3": "#a39889",
          "accent": "#f2b33d",
          "accent-text": "#f2b33d",
          "accent-ink": "#1d1405",
          "inverse": "#f6f1ea",
          "inverse-ink": "#0e0d0c",
          "positive": "#7fc28a",
          "positive-ink": "#0d1f10",
          "warning": "#f17633",
          "negative": "#e54151",
          "negative-ink": "#1a0504",
          "negative-text": "#f07067",
          "negative-soft": "#2e1b18",
          "well": "#110d09",
          "well-ink": "#f6efe6",
          "well-ink-2": "#9c9184",
          "well-accent": "#f2b33d",
          "display": "#0b0805",
          "display-ink": "#f6efe6",
          "display-ink-2": "#9c9184",
          "display-accent": "#f2b33d",
          "cap-hi": "#6b6158",
          "cap": "#3e3833",
          "cap-lo": "#1b1815",
          "cap-index": "#f6f1ea",
          "rim": "rgba(255, 236, 214, 0.07)",
          "edge": "rgba(0, 0, 0, 0.55)",
          "depth": "rgba(0, 0, 0, 0.8)",
          "glow": "#f2b33d1a",
          "meter-gap": "rgba(14, 13, 12, 0.34)",
          "scrim": "rgba(8, 7, 6, 0.74)",
          "shadow": "0 1.5rem 4rem rgba(0, 0, 0, 0.6)",
          "focus": "#f2b33d"
        }
      },
      {
        "id": "phosphor",
        "name": "Phosphor",
        "scheme": "dark",
        "note": "Monitor green with monospaced figures.",
        "tokens": {
          "ground": "#0a0c0b",
          "surface": "#111513",
          "surface-top": "#181e1b",
          "surface-2": "#1a201d",
          "key-top": "#242c28",
          "surface-3": "#26302b",
          "line": "#212824",
          "line-strong": "#3b4741",
          "panel-line": "#111513",
          "key-line": "#1a201d",
          "text": "#e8f3ec",
          "text-2": "#a9bdb1",
          "text-3": "#8aa395",
          "accent": "#6eeb62",
          "accent-text": "#6eeb62",
          "accent-ink": "#04210f",
          "inverse": "#e8f3ec",
          "inverse-ink": "#0a0c0b",
          "positive": "#34dbb9",
          "positive-ink": "#03211b",
          "warning": "#fcaa2b",
          "negative": "#ff5f56",
          "negative-ink": "#1f0302",
          "negative-text": "#ff7a72",
          "negative-soft": "#2a1716",
          "well": "#070f0b",
          "well-ink": "#dcf7e6",
          "well-ink-2": "#7f9a8a",
          "well-accent": "#6eeb62",
          "display": "#050a07",
          "display-ink": "#dcf7e6",
          "display-ink-2": "#7f9a8a",
          "display-accent": "#6eeb62",
          "cap-hi": "#56635c",
          "cap": "#313b36",
          "cap-lo": "#141a17",
          "cap-index": "#e8f3ec",
          "rim": "rgba(214, 255, 232, 0.06)",
          "edge": "rgba(0, 0, 0, 0.6)",
          "depth": "rgba(0, 0, 0, 0.8)",
          "glow": "#6eeb621a",
          "meter-gap": "rgba(10, 12, 11, 0.4)",
          "scrim": "rgba(5, 7, 6, 0.76)",
          "shadow": "0 1.5rem 4rem rgba(0, 0, 0, 0.6)",
          "focus": "#6eeb62",
          "font-num": "ui-monospace, \"JetBrains Mono\", \"Cascadia Mono\", \"Roboto Mono\", \"Noto Sans Mono\", \"DejaVu Sans Mono\", monospace",
          "weight-clock": "400"
        }
      },
      {
        "id": "amethyst",
        "name": "Amethyst",
        "scheme": "dark",
        "note": "Plum-black plates with orchid lights.",
        "tokens": {
          "ground": "#0d0911",
          "surface": "#191320",
          "surface-top": "#221b2b",
          "surface-2": "#261e2f",
          "key-top": "#32283d",
          "surface-3": "#352c41",
          "line": "#2c2436",
          "line-strong": "#4b4059",
          "panel-line": "#191320",
          "key-line": "#261e2f",
          "text": "#f6f3fa",
          "text-2": "#beb6c9",
          "text-3": "#a096ae",
          "accent": "#cc95fa",
          "accent-text": "#cc95fa",
          "accent-ink": "#1a0f2b",
          "inverse": "#f6f3fa",
          "inverse-ink": "#0d0911",
          "positive": "#5ed8a9",
          "positive-ink": "#03211a",
          "warning": "#efb146",
          "negative": "#f36164",
          "negative-ink": "#2a0508",
          "negative-text": "#f97676",
          "negative-soft": "#2d1215",
          "well": "#110c17",
          "well-ink": "#f6f3fa",
          "well-ink-2": "#9d93aa",
          "well-accent": "#cc95fa",
          "display": "#0b070f",
          "display-ink": "#f6f3fa",
          "display-ink-2": "#9d93aa",
          "display-accent": "#cc95fa",
          "cap-hi": "#6b6078",
          "cap": "#40364c",
          "cap-lo": "#1d1725",
          "cap-index": "#f6f3fa",
          "rim": "rgba(240, 226, 255, 0.07)",
          "edge": "rgba(0, 0, 0, 0.55)",
          "depth": "rgba(0, 0, 0, 0.8)",
          "glow": "#cc95fa1c",
          "meter-gap": "rgba(13, 9, 17, 0.34)",
          "scrim": "rgba(7, 5, 10, 0.76)",
          "shadow": "0 1.5rem 4rem rgba(0, 0, 0, 0.6)",
          "focus": "#cc95fa"
        }
      },
      {
        "id": "midnight",
        "name": "Midnight",
        "scheme": "dark",
        "note": "Deep navy plates with cyan lights.",
        "tokens": {
          "ground": "#070b12",
          "surface": "#0f1622",
          "surface-top": "#162131",
          "surface-2": "#182233",
          "key-top": "#202d42",
          "surface-3": "#223047",
          "line": "#1e2a3c",
          "line-strong": "#34445e",
          "panel-line": "#0f1622",
          "key-line": "#182233",
          "text": "#f8fafc",
          "text-2": "#a8b5c8",
          "text-3": "#8b9bb2",
          "accent": "#22d3ee",
          "accent-text": "#22d3ee",
          "accent-ink": "#03222a",
          "inverse": "#f8fafc",
          "inverse-ink": "#070b12",
          "positive": "#4ade80",
          "positive-ink": "#04210f",
          "warning": "#f59e0b",
          "negative": "#f66c6d",
          "negative-ink": "#2a0610",
          "negative-text": "#fa7c7b",
          "negative-soft": "#2e1214",
          "well": "#060e1a",
          "well-ink": "#f8fafc",
          "well-ink-2": "#8b9bb2",
          "well-accent": "#22d3ee",
          "display": "#030813",
          "display-ink": "#f8fafc",
          "display-ink-2": "#8b9bb2",
          "display-accent": "#22d3ee",
          "cap-hi": "#56667f",
          "cap": "#2f3d54",
          "cap-lo": "#131c2a",
          "cap-index": "#f8fafc",
          "rim": "rgba(200, 225, 255, 0.07)",
          "edge": "rgba(0, 0, 0, 0.5)",
          "depth": "rgba(0, 0, 0, 0.75)",
          "glow": "#22d3ee1f",
          "meter-gap": "rgba(7, 11, 18, 0.34)",
          "scrim": "rgba(3, 6, 11, 0.76)",
          "shadow": "0 1.5rem 4rem rgba(0, 0, 0, 0.55)",
          "focus": "#22d3ee"
        }
      }
    ]
  } /* TD-THEMES:END */;

  const STORAGE_KEY = 'td_theme';
  const STYLE_ID = 'td-theme-tokens';
  const byId = new Map(REGISTRY.themes.map(t => [t.id, t]));
  let currentId = null;

  // Retired ids (a renamed or removed preset) map to their replacement.
  function resolve(id) {
    return byId.get(id) || byId.get(REGISTRY.aliases[id]) || byId.get(REGISTRY.default);
  }

  function tokensOf(id) {
    return { ...REGISTRY.base, ...resolve(id).tokens };
  }

  function cssVars(id) {
    const tokens = tokensOf(id);
    let out = '';
    for (const key in tokens) out += `--${key}:${tokens[key]};`;
    return out;
  }

  function readSaved() {
    try { return localStorage.getItem(STORAGE_KEY); } catch (err) { return null; }
  }

  function writeSaved(id) {
    try { localStorage.setItem(STORAGE_KEY, id); } catch (err) { /* private mode: theme still applies */ }
  }

  function apply(id, opts) {
    const theme = resolve(id);
    if (theme.id === currentId) return theme.id;
    currentId = theme.id;

    // One <style> swap invalidates styles once, instead of ~60 setProperty calls.
    let style = document.getElementById(STYLE_ID);
    if (!style) {
      style = document.createElement('style');
      style.id = STYLE_ID;
      document.head.appendChild(style);
    }
    style.textContent = `:root{${cssVars(theme.id)}color-scheme:${theme.scheme};}`;

    const root = document.documentElement;
    root.dataset.theme = theme.id;
    root.dataset.scheme = theme.scheme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme.tokens.ground);

    if (!opts || opts.persist !== false) writeSaved(theme.id);
    document.dispatchEvent(new CustomEvent('td-theme-change', { detail: theme.id }));
    return theme.id;
  }

  // Effects tier. The Linux desktop window is WebKitGTK with GPU compositing
  // disabled (NVIDIA workaround), so every animated pixel is rasterised on the
  // CPU: it gets "lite" (no grain, no ambient cross-fades, no idle pulses).
  // Chromium (Android WebView, WebView2, browsers) and Safari get "rich".
  function effectsTier() {
    const ua = navigator.userAgent;
    const webkitGtk = /AppleWebKit/.test(ua) && !/Chrome|Chromium|Edg\//.test(ua) && /Linux|X11/.test(ua) && !/Android/.test(ua);
    return webkitGtk ? 'lite' : 'rich';
  }

  document.documentElement.dataset.fx = effectsTier();

  // Corner shape: squircles (corner-shape) cost a phone or tablet GPU far more
  // than round corners (index.html, the @supports rule), so Android gets round.
  document.documentElement.dataset.corners = /Android/.test(navigator.userAgent) ? 'round' : 'squircle';

  window.TDTheme = {
    list: () => REGISTRY.themes.slice(),
    current: () => currentId,
    defaultId: REGISTRY.default,
    tokensOf,
    cssVars,
    ground: id => resolve(id).tokens.ground,
    scheme: id => resolve(id).scheme,
    apply
  };

  apply(readSaved(), { persist: false });
})();
