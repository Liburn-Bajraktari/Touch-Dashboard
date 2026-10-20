/*
 * Touch Dashboard — design tokens and theme presets.
 *
 * This file is the single source of every colour, radius, type and motion value
 * the UI uses. templates/index.html never hard-codes a design value; it reads
 * them through var(--token). The rules behind the tokens (what each role is
 * for, what is banned, how to add a preset) live in docs/DESIGN.md, and
 * tests/test_themes.py checks every preset's contrast and completeness.
 *
 * The block between the TD-THEMES markers must stay strict JSON: the test
 * parses it, so no comments, no trailing commas, double quotes only.
 *
 * Loaded synchronously in <head> so the saved theme is painted on the first
 * frame (no flash of the default theme before the WebSocket connects).
 */
(function () {
  'use strict';

  const REGISTRY = /* TD-THEMES:BEGIN */ {
    "default": "graphite",
    "base": {
      "pad": "1.25rem",
      "gap": "1.25rem",
      "gap-sm": "0.75rem",
      "fs-xs": "1rem",
      "fs-sm": "1.15rem",
      "fs-md": "1.35rem",
      "fs-lg": "1.75rem",
      "fs-xl": "2.6rem",
      "fs-clock": "5.75rem",
      "radius-panel": "1.5rem",
      "radius-key": "1.05rem",
      "radius-media": "0.6rem",
      "radius-pill": "999px",
      "line-w": "1px",
      "key-line-w": "0px",
      "panel-line-w": "0px",
      "font-sans": "system-ui, -apple-system, \"Segoe UI Variable Text\", \"Segoe UI\", Roboto, \"Noto Sans\", Ubuntu, Cantarell, sans-serif",
      "font-num": "var(--font-sans)",
      "weight-clock": "500",
      "weight-strong": "600",
      "dur-fast": "110ms",
      "dur": "180ms",
      "ease": "cubic-bezier(0.2, 0.8, 0.2, 1)"
    },
    "themes": [
      {
        "id": "graphite",
        "name": "Graphite",
        "scheme": "dark",
        "note": "True black with the brand cyan on your levels only.",
        "tokens": {
          "ground": "#000000",
          "surface": "#111111",
          "surface-2": "#222222",
          "surface-3": "#333333",
          "line": "#262626",
          "line-strong": "#444444",
          "panel-line": "#111111",
          "key-line": "#222222",
          "text": "#ffffff",
          "text-2": "#b4b4b4",
          "text-3": "#949494",
          "accent": "#14d3f5",
          "accent-text": "#14d3f5",
          "accent-ink": "#00222b",
          "inverse": "#ffffff",
          "inverse-ink": "#000000",
          "positive": "#3fc07a",
          "positive-ink": "#04210f",
          "warning": "#e5a93b",
          "negative": "#cf3238",
          "negative-ink": "#ffffff",
          "negative-text": "#ff6b70",
          "negative-soft": "#2c1a1d",
          "meter-gap": "rgba(0, 0, 0, 0.34)",
          "scrim": "rgba(0, 0, 0, 0.74)",
          "shadow": "0 1.5rem 4rem rgba(0, 0, 0, 0.6)",
          "focus": "#14d3f5"
        }
      },
      {
        "id": "daylight",
        "name": "Daylight",
        "scheme": "light",
        "note": "White keys on cool grey, for a bright room.",
        "tokens": {
          "ground": "#e8eaed",
          "surface": "#ffffff",
          "surface-2": "#f1f3f5",
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
          "warning": "#b7791f",
          "negative": "#c62828",
          "negative-ink": "#ffffff",
          "negative-text": "#b42318",
          "negative-soft": "#fdecea",
          "meter-gap": "rgba(255, 255, 255, 0.5)",
          "scrim": "rgba(17, 19, 22, 0.42)",
          "shadow": "0 1.5rem 4rem rgba(17, 19, 22, 0.18)",
          "focus": "#056a85",
          "key-line-w": "1px"
        }
      },
      {
        "id": "ember",
        "name": "Ember",
        "scheme": "dark",
        "note": "Warm near-black and amber, easy on the eyes at night.",
        "tokens": {
          "ground": "#0e0d0c",
          "surface": "#181614",
          "surface-2": "#25221f",
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
          "warning": "#f08a4b",
          "negative": "#ea5a50",
          "negative-ink": "#1a0504",
          "negative-text": "#f07067",
          "negative-soft": "#2e1b18",
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
          "surface-2": "#1a201d",
          "surface-3": "#26302b",
          "line": "#212824",
          "line-strong": "#3b4741",
          "panel-line": "#111513",
          "key-line": "#1a201d",
          "text": "#e8f3ec",
          "text-2": "#a9bdb1",
          "text-3": "#8aa395",
          "accent": "#4ade80",
          "accent-text": "#4ade80",
          "accent-ink": "#04210f",
          "inverse": "#e8f3ec",
          "inverse-ink": "#0a0c0b",
          "positive": "#4ade80",
          "positive-ink": "#04210f",
          "warning": "#e8c547",
          "negative": "#ff5f56",
          "negative-ink": "#1f0302",
          "negative-text": "#ff7a72",
          "negative-soft": "#2a1716",
          "meter-gap": "rgba(10, 12, 11, 0.4)",
          "scrim": "rgba(5, 7, 6, 0.76)",
          "shadow": "0 1.5rem 4rem rgba(0, 0, 0, 0.6)",
          "focus": "#4ade80",
          "font-num": "ui-monospace, \"JetBrains Mono\", \"Cascadia Mono\", \"Roboto Mono\", \"Noto Sans Mono\", \"DejaVu Sans Mono\", monospace",
          "weight-clock": "400"
        }
      },
      {
        "id": "contrast",
        "name": "High contrast",
        "scheme": "dark",
        "note": "Pure black and white with outlined keys, for glare or low vision.",
        "tokens": {
          "ground": "#000000",
          "surface": "#000000",
          "surface-2": "#000000",
          "surface-3": "#262626",
          "line": "#ffffff",
          "line-strong": "#ffffff",
          "panel-line": "#ffffff",
          "key-line": "#ffffff",
          "text": "#ffffff",
          "text-2": "#ffffff",
          "text-3": "#d9d9d9",
          "accent": "#ffd60a",
          "accent-text": "#ffd60a",
          "accent-ink": "#000000",
          "inverse": "#ffffff",
          "inverse-ink": "#000000",
          "positive": "#3dff8a",
          "positive-ink": "#000000",
          "warning": "#ffb020",
          "negative": "#ff4d4d",
          "negative-ink": "#000000",
          "negative-text": "#ff6b6b",
          "negative-soft": "#000000",
          "meter-gap": "rgba(0, 0, 0, 0.6)",
          "scrim": "rgba(0, 0, 0, 0.86)",
          "shadow": "none",
          "focus": "#ffd60a",
          "key-line-w": "2px",
          "panel-line-w": "2px",
          "weight-clock": "700",
          "weight-strong": "700"
        }
      },
      {
        "id": "classic",
        "name": "Classic",
        "scheme": "dark",
        "note": "The original midnight blue and cyan, without the glow.",
        "tokens": {
          "ground": "#070b12",
          "surface": "#0f1622",
          "surface-2": "#182233",
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
          "negative": "#fb7185",
          "negative-ink": "#2a0610",
          "negative-text": "#fb7185",
          "negative-soft": "#2a1420",
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

  function resolve(id) {
    return byId.get(id) || byId.get(REGISTRY.default);
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

    // One <style> swap invalidates styles once, instead of ~45 setProperty calls.
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

  window.TDTheme = {
    list: () => REGISTRY.themes.slice(),
    current: () => currentId,
    defaultId: REGISTRY.default,
    tokensOf,
    cssVars,
    ground: id => resolve(id).tokens.ground,
    apply
  };

  apply(readSaved(), { persist: false });
})();
