/* The scan progress meter (LL.scanMeter in js/wizard.js) under Node, shared by the setup wizard and Settings > General >
   Network scan. Loads wizard.js with a stand-in page object, feeds the scan states from argv[2] (JSON list of lists, one
   list per watcher) and prints the percentages as JSON. */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const noop = () => {};
const LL = new Proxy({ h: noop, t: k => k, api: () => Promise.resolve({}) }, {
  get: (o, k) => (k in o ? o[k] : noop),
});
const ctx = { window: { Lanloom: LL }, document: { addEventListener: noop }, sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop, console, Date };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'wizard.js'), 'utf8'), ctx);
const out = JSON.parse(process.argv[2]).map(seq => { const m = LL.scanMeter(); return seq.map(s => m(s)); });
// remaining-time estimate: needs 5 % and 5 s, then linear; none when done
const real = Date.now; let now = 1000000; Date.now = () => now;
const m = LL.scanMeter(); const eta = [];
m({ id: 1, state: 'running', phase: 'neighbors', done: 0, total: 0 }); eta.push(m.eta());   // too early
now += 20000; m({ id: 1, state: 'running', phase: 'mdns', done: 0, total: 100 }); eta.push(m.eta());   // 16 % in 20 s
now += 1000; m({ id: 1, state: 'done' }); eta.push(m.eta());
Date.now = real;
process.stdout.write(JSON.stringify({ phases: LL.SCAN_PHASES, out, eta }));
