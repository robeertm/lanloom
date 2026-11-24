/* Resilience (#20): the pure helpers of lanloom/web/js/resilience.js under Node — the word of a small square and the
   "Also affected" line. Prints one JSON object; tests/test_resilience_js.py checks it. */
'use strict';
const path = require('path');
const R = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'resilience.js'));

const tr = (k, v) => k + (v && v.n != null ? '(' + v.n + ')' : '');
const out = {
  steps: [{ weak: 0 }, { weak: 0.3 }, { weak: 0.6 }, { weak: 1 }, { weak: null }, { counts: false, weak: null }, null].map(R.axisStep),
  mains: R.cellLabel('power', { weak: 1, value: 'mains' }, tr),
  measured: R.cellLabel('health', { weak: 1, value: 'measured' }, tr),
  spare: R.cellLabel('spare', { weak: 1, value: 'none' }, tr),
  ups: R.cellLabel('power', { weak: 0.1, value: 'ups' }, tr),
  unknown: R.cellLabel('power', { weak: null, value: 'unknown' }, tr),
  notRated: R.cellLabel('backup', { weak: null, counts: false, value: 'not_rated' }, tr),
  bare: R.cellLabel('watch', { weak: 0.6 }, tr),
  parts: R.partsLine({ devices: 0, hub_devices: 1, automations: 0, entities: 3, connections: 0 }, tr),
  allParts: R.partsLine({ devices: 29, hub_devices: 41, automations: 0, entities: 457, connections: 4 }, tr),
  noParts: R.partsLine({ devices: 0, hub_devices: 0 }, tr),
  nullParts: R.partsLine(null, tr),
  order: R.PARTS,
};
process.stdout.write(JSON.stringify(out));
