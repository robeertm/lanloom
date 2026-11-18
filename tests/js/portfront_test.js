/* Front view of a switch under Node: the pure layout and wording helpers (lanloom/web/js/portfront.js).
   Prints one JSON object; tests/test_portfront_ui.py checks it. */
'use strict';
const path = require('path');
const P = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'portfront.js'));

const mk = (n, extra) => Array.from({ length: n }, (_, i) => Object.assign({ id: 'p' + (i + 1), idx: i + 1, sfp: false }, extra && extra(i + 1)));
const ids = rows => rows.map(r => r.map(p => p.idx));
const lay = (ports, cols) => { const l = P.portLayout(ports, cols); return { single: l.single, bands: l.bands.map(b => ({ cols: b.cols, rows: ids(b.rows) })), sfp: l.sfp.map(p => p.idx) }; };
const tr = (k, v) => k + (v ? JSON.stringify(v) : '');
const fmt = n => String(n);

const sixteenSfp = mk(16, i => ({ sfp: i === 15 || i === 16 }));
const out = {
  small: lay(mk(5), 12),
  eight: lay(mk(8), 12),
  sixteen: lay(mk(16), 12),
  sixteenNarrow: lay(mk(16), 4),
  twentyFour: lay(mk(24), 8),
  eightNarrow: lay(mk(8), 6),
  withSfp: lay(sixteenSfp, 12),
  shuffled: lay(mk(10).reverse(), 12),
  empty: lay([], 12),
  speeds: [10, 100, 1000, 2500, 10000, 25000, 0, null, undefined, -5].map(v => P.speedLabel(v, fmt, tr)),
  speedsFull: [100, 1000, 2500, 0].map(v => P.speedFull(v, fmt, tr)),
  levelKeys: ['ok', 'warn', 'bad', 'nonsense', undefined].map(P.levelKey),
  marks: ['ok', 'warn', 'bad', 'nonsense', undefined].map(P.levelMark),
  health: [{ flaps: 0, errors: 0 }, { flaps: 3, errors: 0 }, { flaps: 0, errors: 2 }, { flaps: 1, errors: 1 }].map(p => P.healthText(p, tr)),
  cols: [0, 300, 360, 420, 600, 2000].map(w => [P.colsFor(w, false), P.colsFor(w, true)]),
  beside: [0, 300, 419, 420, 800].map(P.sfpBeside)
};
process.stdout.write(JSON.stringify(out));
