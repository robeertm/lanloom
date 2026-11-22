/* User colour themes: a theme file (JSON, only colour values) is checked strictly, filled up from the base palette, warned about when text or
   graphics would be hard to read, and turned into the CSS of one extra palette. Pure functions, no DOM; the wiring (storage, style tag, picker) lives in
   app.js and views.js. Nothing is sent anywhere: the theme stays in this browser. */
(function () {
  'use strict';
  const U = {};
  U.FORMAT = 'lanloom-theme/1';
  U.MAX_BYTES = 65536;
  U.MAX_NAME = 40;
  /* the colour tokens a theme may set (the keys of a palette block in css/palettes.css); anything else is dropped */
  U.TOKENS = [
    '--ll-bg', '--ll-surface', '--ll-surface-2', '--ll-surface-3', '--ll-border', '--ll-border-strong', '--ll-text', '--ll-text-2', '--ll-text-3',
    '--ll-text-disabled', '--ll-accent', '--ll-accent-hover', '--ll-accent-text', '--ll-accent-soft', '--ll-on-accent', '--ll-focus', '--ll-overlay',
    '--ll-shadow-1', '--ll-shadow-2', '--ll-ok', '--ll-ok-text', '--ll-ok-soft', '--ll-warn', '--ll-warn-text', '--ll-warn-soft', '--ll-danger',
    '--ll-danger-text', '--ll-danger-soft', '--ll-muted', '--ll-grade-realtime', '--ll-grade-near', '--ll-grade-periodic', '--ll-grade-sluggish',
    '--ll-grade-interrupted', '--ll-grade-unknown', '--ll-pcb-board', '--ll-pcb-grid', '--ll-pcb-zone', '--ll-pcb-zone-line', '--ll-pcb-silk',
    '--ll-pcb-silk-2', '--ll-pcb-tile', '--ll-pcb-tile-line', '--ll-pcb-tile-hover', '--ll-pcb-tile-shadow', '--ll-pcb-tile-down',
    '--ll-pcb-icon-bg', '--ll-pcb-icon', '--ll-pcb-copper', '--ll-pcb-via', '--ll-pcb-trace-rest', '--ll-pcb-trace-uplink', '--ll-pcb-trace-lan',
    '--ll-pcb-trace-service', '--ll-pcb-trace-flow', '--ll-pcb-trace-integration', '--ll-pcb-trace-cable', '--ll-pcb-trace-radio',
    '--ll-pcb-trace-virtual', '--ll-pcb-dot-core', '--ll-pcb-badge-bg', '--ll-pcb-badge-line', '--ll-pcb-badge-text', '--ll-pcb-led-ring',
    '--ll-pcb-led-glow', '--ll-pcb-legend-bg'
  ];
  const ALLOWED = new Set(U.TOKENS);
  const SHADOWS = new Set(['--ll-shadow-1', '--ll-shadow-2', '--ll-pcb-tile-shadow']);
  const NAME_RE = /^[A-Za-z0-9 _.()-]+$/;
  const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
  const NUM = '(?:\\d{1,3}(?:\\.\\d{1,3})?|\\.\\d{1,3})';
  const FN_RE = new RegExp('^rgba?\\(\\s*(\\d{1,3})\\s*,\\s*(\\d{1,3})\\s*,\\s*(\\d{1,3})\\s*(?:,\\s*(' + NUM + ')\\s*)?\\)$');
  const LEN = '(-?\\d{1,3}(?:\\.\\d{1,2})?(?:px)?)';
  const SHADOW_RE = new RegExp('^(?:inset\\s+)?' + LEN + '\\s+' + LEN + '(?:\\s+' + LEN + '(?:\\s+' + LEN + ')?)?\\s+(rgba?\\([^()]*\\))(?:\\s+inset)?$');
  const NUMBER_RE = /^(?:[01](?:\.\d{1,3})?|\.\d{1,3})$/;
  const GLOW_RE = /^(\d{1,2}(?:\.\d{1,2})?)px$/;

  const colourOk = v => {
    if (HEX_RE.test(v)) return true;
    const m = FN_RE.exec(v);
    if (!m) return false;
    if (m.slice(1, 4).some(x => Number(x) > 255)) return false;
    return m[4] === undefined || Number(m[4]) <= 1;
  };
  /* the parts of a shadow list, split at the commas outside the brackets */
  const splitList = v => {
    const parts = []; let depth = 0, cur = '';
    for (const ch of v) {
      if (ch === '(') depth++;
      if (ch === ')') depth--;
      if (ch === ',' && depth === 0) { parts.push(cur.trim()); cur = ''; } else cur += ch;
    }
    parts.push(cur.trim());
    return depth === 0 ? parts : null;
  };
  const shadowOk = v => {
    if (v.length > 400) return false;
    const parts = splitList(v);
    if (!parts || parts.length > 4) return false;
    return parts.every(p => { const m = SHADOW_RE.exec(p); return !!m && colourOk(m[5]); });
  };
  /* one value of one token: true when it is a plain colour / shadow / number of the kind the token takes */
  U.valueOk = function (token, value) {
    if (!ALLOWED.has(token) || typeof value !== 'string') return false;
    const v = value.trim();
    if (!v || v.length > 400) return false;
    if (token === '--ll-pcb-trace-rest') return NUMBER_RE.test(v) && Number(v) >= 0 && Number(v) <= 1;
    if (token === '--ll-pcb-led-glow') { const m = GLOW_RE.exec(v); return !!m && Number(m[1]) <= 20; }
    if (SHADOWS.has(token)) return shadowOk(v);
    return colourOk(v);
  };

  /* parse(text) -> { ok, theme, errors, dropped }.  errors: [{ code, mode, token }] (code: size | json | format | name | empty | value),
     dropped: [token names that are not part of the allow-list]. A theme with an error is refused as a whole. */
  U.parse = function (text) {
    const errors = [], dropped = [];
    const fail = (code, mode, token) => { errors.push({ code, mode: mode || '', token: token || '' }); return { ok: false, theme: null, errors, dropped }; };
    if (typeof text !== 'string') return fail('json');
    const bytes = typeof TextEncoder === 'function' ? new TextEncoder().encode(text).length : text.length;
    if (bytes > U.MAX_BYTES) return fail('size');
    let raw;
    try { raw = JSON.parse(text); } catch (e) { return fail('json'); }
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return fail('json');
    if (raw.format !== U.FORMAT) return fail('format');
    const name = typeof raw.name === 'string' ? raw.name.trim() : '';
    if (name.length < 1 || name.length > U.MAX_NAME || !NAME_RE.test(name)) return fail('name');
    const theme = { format: U.FORMAT, name, light: {}, dark: {} };
    let seen = 0;
    for (const mode of ['light', 'dark']) {
      const src = raw[mode];
      if (src === undefined) continue;
      if (!src || typeof src !== 'object' || Array.isArray(src)) return fail('json', mode);
      seen++;
      for (const key of Object.keys(src)) {
        if (!ALLOWED.has(key)) { if (!dropped.includes(key)) dropped.push(key.slice(0, 60)); continue; }
        if (!U.valueOk(key, src[key])) errors.push({ code: 'value', mode, token: key });
        else theme[mode][key] = src[key].trim();
      }
    }
    if (errors.length) return { ok: false, theme: null, errors, dropped };
    if (!seen || (!Object.keys(theme.light).length && !Object.keys(theme.dark).length)) return fail('empty');
    return { ok: true, theme, errors, dropped };
  };

  /* fill(theme, base) -> a complete theme: every missing token comes from base ({ light, dark } token maps), values of base are checked too */
  U.fill = function (theme, base) {
    const out = { format: U.FORMAT, name: theme.name, light: {}, dark: {} };
    for (const mode of ['light', 'dark']) {
      for (const k of U.TOKENS) {
        const own = theme[mode] && theme[mode][k], from = base && base[mode] && base[mode][k];
        if (typeof own === 'string' && U.valueOk(k, own)) out[mode][k] = own.trim();
        else if (typeof from === 'string' && U.valueOk(k, from)) out[mode][k] = from.trim();
      }
    }
    return out;
  };

  // ------------------------------------------------------------------ contrast (WCAG 2.x), the same rules as the palette tests
  const toRgb = v => {
    v = v.trim();
    if (v.charAt(0) === '#') {
      let h = v.slice(1);
      if (h.length === 3) h = h.split('').map(c => c + c).join('');
      return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1];
    }
    const m = FN_RE.exec(v);
    if (!m) return null;
    return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] === undefined ? 1 : Number(m[4])];
  };
  const hex2 = n => ('0' + Math.round(n).toString(16)).slice(-2);
  /* a (maybe translucent) colour composited over an opaque ground -> a six-digit colour text */
  const over = (fg, bg) => {
    const f = toRgb(fg), b = toRgb(bg);
    if (!f || !b) return null;
    const a = f[3], c = [0, 1, 2].map(i => f[i] * a + b[i] * (1 - a));
    return '#' + hex2(c[0]) + hex2(c[1]) + hex2(c[2]);
  };
  const lum = v => {
    const c = toRgb(v).slice(0, 3).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  U.contrast = function (a, b) {
    const la = lum(a), lb = lum(b), hi = Math.max(la, lb), lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  };
  U.over = over;
  U.luminance = lum;

  const TEXT_ON = {
    '--ll-text': ['--ll-bg', '--ll-surface', '--ll-surface-2', '--ll-surface-3', '--ll-pcb-tile', '--ll-pcb-zone', '--ll-pcb-board'],
    '--ll-text-2': ['--ll-bg', '--ll-surface', '--ll-surface-2', '--ll-surface-3', '--ll-pcb-tile'],
    '--ll-text-3': ['--ll-bg', '--ll-surface', '--ll-surface-2', '--ll-pcb-tile'],
    '--ll-pcb-silk': ['--ll-pcb-zone', '--ll-pcb-board'], '--ll-pcb-silk-2': ['--ll-pcb-zone', '--ll-pcb-board'],
    '--ll-accent-text': ['--ll-bg', '--ll-surface', '--ll-surface-2'],
    '--ll-ok-text': ['--ll-surface', '--ll-bg'], '--ll-warn-text': ['--ll-surface', '--ll-bg'], '--ll-danger-text': ['--ll-surface', '--ll-bg'],
    '--ll-pcb-badge-text': ['--ll-pcb-badge-bg'], '--ll-on-accent': ['--ll-accent', '--ll-accent-hover']
  };
  const SOFT_TEXT = { '--ll-accent-soft': '--ll-accent-text', '--ll-ok-soft': '--ll-ok-text', '--ll-warn-soft': '--ll-warn-text', '--ll-danger-soft': '--ll-danger-text' };
  const GRAPHICS = ['--ll-accent', '--ll-focus', '--ll-ok', '--ll-warn', '--ll-danger', '--ll-grade-realtime', '--ll-grade-near', '--ll-grade-periodic',
    '--ll-grade-sluggish', '--ll-grade-interrupted', '--ll-grade-unknown', '--ll-pcb-trace-uplink', '--ll-pcb-trace-lan', '--ll-pcb-trace-service',
    '--ll-pcb-trace-flow', '--ll-pcb-trace-integration', '--ll-pcb-trace-cable', '--ll-pcb-trace-radio', '--ll-pcb-trace-virtual', '--ll-pcb-copper'];
  const GRAPHIC_GROUNDS = ['--ll-surface', '--ll-pcb-board', '--ll-pcb-zone', '--ll-pcb-tile'];
  const SURFACES = ['--ll-bg', '--ll-surface', '--ll-surface-2', '--ll-surface-3', '--ll-pcb-board', '--ll-pcb-zone', '--ll-pcb-tile', '--ll-pcb-icon-bg',
    '--ll-pcb-badge-bg', '--ll-pcb-legend-bg'];
  const round1 = x => Math.round(x * 10) / 10;

  /* check(theme) -> warnings: [{ kind: 'contrast', mode, fg, bg, ratio, need }, { kind: 'surface', mode, token, side: 'light' | 'dark' }].
     Only pairs where both colours are present are judged, so check a filled theme (U.fill) for the whole picture. */
  U.check = function (theme) {
    const out = [];
    for (const mode of ['light', 'dark']) {
      const tok = (theme && theme[mode]) || {};
      const surface = tok['--ll-surface'];
      if (!surface || !toRgb(surface)) continue;
      const have = k => typeof tok[k] === 'string' && !!toRgb(tok[k]);
      const flat = k => over(tok[k], surface);
      const need = (fg, bg, min) => {
        if (!have(fg) || !have(bg)) return;
        const r = U.contrast(flat(fg), flat(bg));
        if (r < min) out.push({ kind: 'contrast', mode, fg, bg, ratio: round1(r), need: min });
      };
      for (const fg of Object.keys(TEXT_ON)) for (const bg of TEXT_ON[fg]) need(fg, bg, 4.5);
      for (const soft of Object.keys(SOFT_TEXT)) {
        const fg = SOFT_TEXT[soft];
        if (have(soft) && have(fg)) { const r = U.contrast(tok[fg], flat(soft)); if (r < 4.5) out.push({ kind: 'contrast', mode, fg, bg: soft, ratio: round1(r), need: 4.5 }); }
      }
      for (const fg of GRAPHICS) for (const bg of GRAPHIC_GROUNDS) need(fg, bg, 3);
      const ground = tok['--ll-bg'];
      if (ground && toRgb(ground)) {
        for (const k of SURFACES) {
          if (!have(k)) continue;
          const flatK = over(tok[k], ground), l = lum(flatK);
          if (mode === 'light' ? l < 0.6 : l > 0.06) out.push({ kind: 'surface', mode, token: k, side: mode });
          else if (have('--ll-text')) { const r = U.contrast(over(tok['--ll-text'], surface), flatK); if (r < 7) out.push({ kind: 'contrast', mode, fg: '--ll-text', bg: k, ratio: round1(r), need: 7 }); }
        }
      }
    }
    return out;
  };

  // ------------------------------------------------------------------ CSS of the extra palette (same selector form as css/palettes.css)
  const ID_RE = /^[a-z][a-z0-9-]{0,30}$/;
  const block = (map, scheme, indent) => {
    let s = indent + 'color-scheme: ' + scheme + ';\n';
    for (const k of U.TOKENS) if (typeof map[k] === 'string' && U.valueOk(k, map[k])) s += indent + k + ': ' + map[k].trim() + ';\n';
    return s;
  };
  /* toCss(theme, id) -> CSS text; only allow-listed tokens whose value passed the strict check are written */
  U.toCss = function (theme, id) {
    if (!ID_RE.test(id)) throw new Error('bad palette id');
    const sel = '[data-palette="' + id + '"]';
    const inner = ' [data-theme="%"]:not([data-palette]) {\n';
    const light = theme.light || {}, dark = theme.dark || {};
    return sel + ',\n' + sel + inner.replace('%', 'light') + block(light, 'light', '  ') + '}\n\n' +
      sel + '[data-theme="dark"],\n' + sel + inner.replace('%', 'dark') + block(dark, 'dark', '  ') + '}\n\n' +
      '@media (prefers-color-scheme: dark) {\n    ' + sel + '[data-theme="auto"],\n  ' + sel + inner.replace('%', 'auto') + block(dark, 'dark', '    ') + '  }\n}\n';
  };

  /* a name that passes the name rule: other characters become spaces */
  U.cleanName = function (name, fallback) {
    let n = String(name == null ? '' : name).replace(/[^A-Za-z0-9 _.()-]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, U.MAX_NAME).trim();
    return n || fallback || 'My theme';
  };
  /* exportTheme(name, getLight, getDark) -> the theme file as text; the getters return the token map of one mode */
  U.exportTheme = function (name, getLight, getDark) {
    const pick = get => {
      const src = (typeof get === 'function' ? get() : get) || {}, out = {};
      for (const k of U.TOKENS) if (typeof src[k] === 'string' && U.valueOk(k, src[k])) out[k] = src[k].trim();
      return out;
    };
    return JSON.stringify({ format: U.FORMAT, name: U.cleanName(name), light: pick(getLight), dark: pick(getDark) }, null, 2) + '\n';
  };

  if (typeof module === 'object' && module.exports) module.exports = U;
  else { window.Lanloom = window.Lanloom || {}; window.Lanloom.usertheme = U; }
})();
