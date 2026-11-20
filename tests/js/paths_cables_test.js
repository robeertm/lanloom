/* Real cables view (DW3) under Node: the view choice (default, stored, address, event) and the pure line helpers of
   lanloom/web/js/paths_cables.js with a minimal browser stand-in. Prints one JSON object; tests/test_paths_cables_js.py checks it. */
'use strict';
const path = require('path');
const store = {};
const events = [];
let hash = '#/paths';
const replaced = [];
global.window = global;
global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
global.location = { get hash() { return hash; } };
global.history = { state: null, replaceState: (s, _t, url) => { replaced.push(url); hash = url.slice(url.indexOf('#')); } };
global.CustomEvent = function (type, o) { this.type = type; this.detail = o && o.detail; };
global.document = { addEventListener() {}, querySelectorAll: () => [], dispatchEvent: e => events.push([e.type, e.detail]) };
global.addEventListener = () => {};
const cmds = [];
global.Lanloom = { h: () => ({}), t: k => k, api: () => Promise.resolve(null), base: '/', registerCommand: c => cmds.push(c.id), query: new URLSearchParams('') };
require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'paths_cables.js'));
const C = global.Lanloom.pathsCables;
const out = { first: C.view(), on: C.on(), cmds };
C.set('rain');
out.afterRain = { view: C.view(), stored: store['lanloom-paths-view'], hash, event: events[events.length - 1] };
global.Lanloom.query = new URLSearchParams('view=cables');
out.deep = { view: C.fromAddress(), stored: store['lanloom-paths-view'] };
C.set('cables');
out.chosen = { view: C.view(), stored: store['lanloom-paths-view'], hash, events: events.length };
C.set('nonsense');
out.ignored = C.view();
const L = C.line([[0, 0], [3, 4], [3, 10]]);
out.line = { len: L.len, l: L.l, mid: C.at(L, 0.5), start: C.at(L, 0), end: C.at(L, 1), over: C.at(L, 2) };
out.jump = C.line([[999, 10], [1, 10], [5, 10]]).len;          // the date line is no distance
const parts = [{ kind: 'air', p: [[10, 10], [20, 10]] }, { kind: 'cable', p: [[20, 10], [40, 10], [60, 10]] }, { kind: 'air', p: [[60, 10], [70, 12]] }];
out.pathLen = C.pathLine(parts).len;
out.gone = ['arcLine', 'withStart', 'cut', 'pathGroup'].filter(k => k in C);      // no arcs and no dashed parts any more
out.legendOff = C.legendItems();
out.d = C.pathD([[1, 2], [3.333, 4], [998, 4], [2, 4]]);
process.stdout.write(JSON.stringify(out));
