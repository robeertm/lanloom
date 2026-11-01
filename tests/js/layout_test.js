/* Layout checks under Node. Prints one JSON object; the pytest wrapper asserts on it. */
'use strict';
const path = require('path');
const L = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'board', 'layout.js'));
const F = require(path.join(__dirname, 'fixtures.js'));
const out = [];
for (const w of [393, 700, 701, 1100, 1512, 2560]) for (const n of [1, 10, 40, 120]) {
  const v = F.makeView(Math.max(n, 3));
  const lay = L.compute(v, { width: w });
  const m = lay.m, issues = [];
  const tiles = Object.values(lay.tiles);
  if (tiles.length !== v.components.length) issues.push('missing tiles');
  for (let i = 0; i < tiles.length; i++) for (let j = i + 1; j < tiles.length; j++) {
    const a = tiles[i], b = tiles[j];
    if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) issues.push('overlap ' + a.id + '/' + b.id);
  }
  const zones = new Map(lay.zones.map(z => [z.id, z]));
  tiles.forEach(t => {
    const z = zones.get(t.zone);
    if (!z || t.x < z.x + 4 || t.x + t.w > z.x + z.w - 4 || t.y < z.y + 20 || t.y + t.h > z.y + z.h - 4) issues.push('tile outside zone ' + t.id);
  });
  lay.zones.forEach(z => { if (z.x < m.margin - 0.5 || z.x + z.w > w - m.margin + 0.5) issues.push('zone outside board ' + z.id); });
  // aligned right edges: every zone row ends at the right margin
  const rows = new Map(); lay.zones.forEach(z => { const r = rows.get(z.y) || []; r.push(z); rows.set(z.y, r); });
  rows.forEach(r => { const right = Math.max(...r.map(z => z.x + z.w)); if (Math.abs(right - (w - m.margin)) > 1) issues.push('row not aligned at y=' + r[0].y); const hs = new Set(r.map(z => z.h)); if (hs.size > 1) issues.push('row heights differ'); });
  const full = lay.zones.find(z => z.id === 'edge');
  if (full && Math.abs(full.w - (w - 2 * m.margin)) > 0.5) issues.push('full span zone not full width');
  if (lay.compact) { const xs = new Set(tiles.map(t => t.x)); if (xs.size > 2) issues.push('compact has more than two columns'); }
  out.push({ w, n, compact: lay.compact, h: lay.h, zones: lay.zones.length, issues: issues.slice(0, 5) });
}
console.log(JSON.stringify(out));
