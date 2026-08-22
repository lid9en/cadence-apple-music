/* Built-in colour themes.
   "Adaptive" in settings blends any of these toward colours sampled from
   the current album artwork, so the theme picks the typography and the
   base mood while the sleeve drives the hue. */

const THEMES = {
  midnight: {
    label: "Midnight", dark: true,
    bg: "#0d0d12", bg2: "#16161e", fg: "#f2f2f5",
    muted: "#93939f", accent: "#fa2d48", accentFg: "#ffffff",
  },
  aurora: {
    label: "Aurora", dark: true,
    bg: "#07131a", bg2: "#0d2029", fg: "#e9fbff",
    muted: "#7ea8b4", accent: "#2de1c2", accentFg: "#04241f",
  },
  vinyl: {
    label: "Vinyl", dark: true,
    bg: "#161310", bg2: "#221d18", fg: "#f6efe6",
    muted: "#a89880", accent: "#e0a03c", accentFg: "#231703",
  },
  neon: {
    label: "Neon", dark: true,
    bg: "#0b0716", bg2: "#150d26", fg: "#f4ecff",
    muted: "#9c8bc4", accent: "#b14cff", accentFg: "#ffffff",
  },
  ember: {
    label: "Ember", dark: true,
    bg: "#140b0b", bg2: "#211010", fg: "#ffeee9",
    muted: "#bf8f83", accent: "#ff5f39", accentFg: "#2a0b02",
  },
  slate: {
    label: "Slate", dark: true,
    bg: "#111418", bg2: "#1a1f25", fg: "#e8edf2",
    muted: "#8d9aa8", accent: "#4c8dff", accentFg: "#ffffff",
  },
  paper: {
    label: "Paper", dark: false,
    bg: "#f7f6f3", bg2: "#ffffff", fg: "#1a1a1e",
    muted: "#6b6b76", accent: "#d6295a", accentFg: "#ffffff",
  },
  linen: {
    label: "Linen", dark: false,
    bg: "#f4f1ea", bg2: "#fffdf8", fg: "#2a2620",
    muted: "#7a7166", accent: "#1f7a5c", accentFg: "#ffffff",
  },
  terminal: {
    label: "Terminal", dark: true,
    bg: "#000000", bg2: "#0b0f0b", fg: "#c8f5c8",
    muted: "#5c8f5c", accent: "#39ff5a", accentFg: "#001a03",
    mono: true,
  },
};

const FONT_STACKS = {
  system:
    '-apple-system, BlinkMacSystemFont, "Segoe UI Variable Display", "Segoe UI",' +
    ' system-ui, sans-serif',
  inter: '"Inter", "Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif',
  rounded:
    '"SF Pro Rounded", "Segoe UI Variable Display", "Nunito", "Segoe UI",' +
    " system-ui, sans-serif",
  serif: 'Georgia, "Iowan Old Style", "Times New Roman", serif',
  mono: '"Cascadia Code", "JetBrains Mono", Consolas, "Courier New", monospace',
};

/* --- small colour helpers ------------------------------------------- */

function hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
  return m
    ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)]
    : [0, 0, 0];
}

function rgbToHex(rgb) {
  return (
    "#" +
    rgb
      .map((c) => Math.max(0, Math.min(255, Math.round(c)))
        .toString(16)
        .padStart(2, "0"))
      .join("")
  );
}

function mixHex(a, b, t) {
  const x = hexToRgb(a), y = hexToRgb(b);
  return rgbToHex([0, 1, 2].map((i) => x[i] + (y[i] - x[i]) * t));
}

function withAlpha(hex, alpha) {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/* Blend a base theme with an artwork palette. */
function resolveTheme(themeName, cfg, artPalette) {
  const base = THEMES[themeName] || THEMES.midnight;
  const t = { ...base };

  const adaptive = cfg && cfg.theme && cfg.theme.adaptive;
  const strength = cfg && cfg.theme ? Number(cfg.theme.adaptive_strength) : 0.85;

  if (adaptive && artPalette && artPalette.bg) {
    const k = Math.max(0, Math.min(1, isNaN(strength) ? 0.85 : strength));
    t.bg = mixHex(base.bg, artPalette.bg, k);
    t.bg2 = mixHex(base.bg2, artPalette.bg2 || artPalette.bg, k);
    t.accent = mixHex(base.accent, artPalette.accent, k);
    t.accentFg = artPalette.accent_fg || base.accentFg;
    t.muted = mixHex(base.muted, artPalette.muted || base.muted, k * 0.6);
  }

  const override = cfg && cfg.theme ? (cfg.theme.accent_override || "").trim() : "";
  if (/^#[0-9a-f]{6}$/i.test(override)) {
    t.accent = override;
    const [r, g, b] = hexToRgb(override);
    t.accentFg = 0.2126 * r + 0.7152 * g + 0.0722 * b > 140 ? "#0a0a0c" : "#ffffff";
  }

  return t;
}

function applyTheme(theme, cfg) {
  const root = document.documentElement.style;
  root.setProperty("--bg", theme.bg);
  root.setProperty("--bg2", theme.bg2);
  root.setProperty("--fg", theme.fg);
  root.setProperty("--muted", theme.muted);
  root.setProperty("--accent", theme.accent);
  root.setProperty("--accent-fg", theme.accentFg);
  root.setProperty("--accent-soft", withAlpha(theme.accent, 0.16));
  root.setProperty("--accent-glow", withAlpha(theme.accent, 0.34));
  root.setProperty("--line", withAlpha(theme.fg, theme.dark ? 0.1 : 0.12));
  root.setProperty("--surface", withAlpha(theme.fg, theme.dark ? 0.06 : 0.05));
  root.setProperty("--surface-hi", withAlpha(theme.fg, theme.dark ? 0.11 : 0.09));

  const th = (cfg && cfg.theme) || {};
  const fontKey = theme.mono ? "mono" : th.font || "system";
  root.setProperty("--font", FONT_STACKS[fontKey] || FONT_STACKS.system);
  root.setProperty("--font-scale", th.font_scale || 1);

  const win = (cfg && cfg.window) || {};
  root.setProperty("--radius", (win.corner_radius ?? 18) + "px");
  root.setProperty("--scale", win.scale || 1);

  root.setProperty("--art-blur", (th.background_art_blur ?? 42) + "px");
  root.setProperty("--art-opacity", th.background_art_opacity ?? 0.5);

  document.documentElement.dataset.dark = theme.dark ? "1" : "0";
}
