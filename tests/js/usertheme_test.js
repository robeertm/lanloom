/* User colour themes (lanloom/web/js/usertheme.js). One JSON object; tests/test_usertheme_js.py checks it. */
'use strict';
const fs = require('fs');
const path = require('path');
const U = require('../../lanloom/web/js/usertheme.js');
const WEB = path.join(__dirname, '..', '..', 'lanloom', 'web');
const out = {};

const css = fs.readFileSync(path.join(WEB, 'css', 'palettes.css'), 'utf8');
const decls = body => { const m = {}; for (const x of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) m[x[1]] = x[2].trim(); return m; };
const blocks = {};
for (const pid of ['aurora', 'summerglass', 'winterglass']) {
  const sel = '[data-palette="' + pid + '"]';
  const lm = css.indexOf('\n' + sel + ',\n' + sel + ' [data-theme="light"]');
  const dm = css.indexOf('\n' + sel + '[data-theme="dark"],');
  if (lm < 0 || dm < 0) continue;
  blocks[pid] = { light: decls(css.slice(lm, css.indexOf('\n}', lm))), dark: decls(css.slice(dm, css.indexOf('\n}', dm))) };
}
out.shippedPalettes = Object.keys(blocks).sort();
// every value of every shipped palette passes the strict check (the export of any palette can be imported again)
out.shippedBad = [];
for (const pid of Object.keys(blocks)) for (const mode of ['light', 'dark']) for (const k of Object.keys(blocks[pid][mode])) {
  if (k === 'color-scheme') continue;
  if (!U.valueOk(k, blocks[pid][mode][k])) out.shippedBad.push(pid + '/' + mode + '/' + k);
}
out.tokens = U.TOKENS.slice();

const aurora = blocks.aurora || { light: {}, dark: {} };
const file = (extra, name) => JSON.stringify(Object.assign({ format: 'lanloom-theme/1', name: name === undefined ? 'Test theme (1)' : name, light: aurora.light, dark: aurora.dark }, extra || {}));
const parsed = U.parse(file());
out.valid = { ok: parsed.ok, name: parsed.theme && parsed.theme.name, n: parsed.theme && Object.keys(parsed.theme.light).length };
const withLight = patch => file({ light: Object.assign({}, aurora.light, patch) });
const refused = text => { const r = U.parse(text); return { ok: r.ok, code: r.errors[0] && r.errors[0].code }; };
out.refused = {
  url: refused(withLight({ '--ll-bg': 'url(http://x/y)' })),
  semicolon: refused(withLight({ '--ll-bg': '#fff;}body{display:none' })),
  styleTag: refused(withLight({ '--ll-bg': '</style><script>' })),
  varRef: refused(withLight({ '--ll-bg': 'var(--ll-text)' })),
  expression: refused(withLight({ '--ll-bg': 'expression(alert(1))' })),
  backslash: refused(withLight({ '--ll-bg': '#ff\\66' })),
  quote: refused(withLight({ '--ll-bg': '"red"' })),
  comment: refused(withLight({ '--ll-bg': '#fff/*x*/' })),
  named: refused(withLight({ '--ll-bg': 'red' })),
  range: refused(withLight({ '--ll-bg': 'rgb(300, 0, 0)' })),
  alpha: refused(withLight({ '--ll-overlay': 'rgba(0, 0, 0, 1.5)' })),
  rest: refused(withLight({ '--ll-pcb-trace-rest': '2' })),
  glow: refused(withLight({ '--ll-pcb-led-glow': '50px' })),
  shadowUrl: refused(withLight({ '--ll-shadow-1': '0 1px 2px url(x)' })),
  shadowPlain: refused(withLight({ '--ll-shadow-1': 'red' })),
  oversize: refused(file({ pad: 'x'.repeat(70000) })),
  badName: refused(file({}, 'Bad <name>')),
  longName: refused(file({}, 'x'.repeat(41))),
  emptyName: refused(file({}, '')),
  noFormat: refused(JSON.stringify({ name: 'A', light: {} })),
  notJson: refused('{nope'),
  array: refused('[]'),
  empty: refused(JSON.stringify({ format: 'lanloom-theme/1', name: 'A' })),
  notObject: refused(JSON.stringify({ format: 'lanloom-theme/1', name: 'A', light: 'x' }))
};
const unknown = U.parse(withLight({ '--evil': '#fff', 'background-image': 'url(x)', '__proto__': '#fff', '--ll-text': '#101010' }));
out.unknown = { ok: unknown.ok, dropped: unknown.dropped.slice().sort(), hasEvil: !!(unknown.theme && ('--evil' in unknown.theme.light)), text: unknown.theme && unknown.theme.light['--ll-text'] };

// partial theme is filled from the base
const partial = U.parse(JSON.stringify({ format: 'lanloom-theme/1', name: 'Partial', light: { '--ll-accent': '#0A7C95' } }));
const filled = U.fill(partial.theme, { light: aurora.light, dark: aurora.dark });
out.fill = { ok: partial.ok, light: Object.keys(filled.light).length, dark: Object.keys(filled.dark).length, accent: filled.light['--ll-accent'], bg: filled.light['--ll-bg'] === aurora.light['--ll-bg'], all: U.TOKENS.length };

// CSS
const theme = U.fill(parsed.theme, { light: aurora.light, dark: aurora.dark });
const text = U.toCss(theme, 'custom');
out.css = text;
out.cssTokens = Array.from(text.matchAll(/^\s+(--[\w-]+):/gm)).map(m => m[1]).filter(k => !U.TOKENS.includes(k));
out.cssBad = text.replace(/\[data-(?:palette|theme)="[a-z-]+"\]/g, '').match(/[<>\\"']|url\(|var\(|expression|\/\*/g) || [];
let badId = false; try { U.toCss(theme, 'a"]{}'); } catch (e) { badId = true; }
out.badId = badId;
// export round trip
const exp = U.exportTheme('Weird <name> here', () => theme.light, () => theme.dark);
const back = U.parse(exp);
out.exportRound = { ok: back.ok, name: back.theme && back.theme.name, same: back.ok && JSON.stringify(back.theme.light) === JSON.stringify(theme.light) };

// contrast
out.contrast = { bw: U.contrast('#000000', '#ffffff'), same: U.contrast('#777777', '#777777') };
out.goodWarnings = U.check(theme).length;
const poor = JSON.parse(JSON.stringify(theme));
poor.light['--ll-text'] = '#B0B0B0'; poor.light['--ll-bg'] = '#202020'; poor.dark['--ll-text-3'] = '#222233';
const bad = U.check(poor);
out.poor = { n: bad.length, kinds: Array.from(new Set(bad.map(w => w.kind))).sort(), modes: Array.from(new Set(bad.map(w => w.mode))).sort(), first: bad[0] };
console.log(JSON.stringify(out));
