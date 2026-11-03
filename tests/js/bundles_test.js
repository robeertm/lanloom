/* Bundled-trace checks under Node. Prints one JSON object; the pytest wrapper asserts on it. */
'use strict';
const path = require('path');
const B = path.join(__dirname, '..', '..', 'lanloom', 'web', 'board');
const R = require(path.join(B, 'router.js'));
const S = require(path.join(B, 'bundles.js'));
const L = require(path.join(B, 'layout.js'));
const F = require(path.join(__dirname, 'fixtures.js'));

const out = {};
const free = () => true;
// geometry: target at the top (y=0); L comes up vertically and turns left at y=50, M runs 3 px beside it and turns at y=60
const Lp = [[100, 0], [100, 50], [0, 50]], M = [[103, 0], [103, 60], [10, 60]];
const chamfered = [[0, 0], [0, 48], [2, 50], [60, 50]], o = S.ortho(chamfered, 6);
out.geometry = {
  a: S.merge(M, Lp, free),
  b: S.merge([[104, 0], [104, 52], [20, 52], [20, 90]], Lp, free),   // same bend (≤ NEAR): fully on L until M turns to its own source
  blocked: S.merge(M, Lp, () => false),                              // tile in the way → no shift
  far: S.merge([[130, 0], [130, 60], [10, 60]], Lp, free),           // too far apart → separate
  ortho: o, chamfer: S.chamfer(o, 2),
  fromTarget: S.fromTarget([[1, 1], [2, 2]], 'b')
};

// whole board: route the fixture with bundle groups, then bundle
function insideAnyTile(x, y, tiles) {
  for (const t of tiles) if (x > t.x + 1 && x < t.x + t.w - 1 && y > t.y + 1 && y < t.y + t.h - 1) return t.id;
  return null;
}
const v = F.makeView(40);
const kindOf = new Map(v.traces.map(t => [t.id, t.kind]));
const key = (t, X) => X + '|' + t.kind + '|';
const lay = L.compute(v, { width: 1512 });
const spec = L.routerSpec(lay, v, { Router: R, groupOf: key });
const res = R.routeAll(spec);
const rects = Object.values(lay.tiles).map(k => [k.x + 0.6, k.y + 0.6, k.x + k.w - 0.6, k.y + k.h - 0.6]);
const freeTiles = (x0, y0, x1, y1) => !rects.some(q => x0 < q[2] && x1 > q[0] && y0 < q[3] && y1 > q[1]);
const before = JSON.stringify(res.traces);
const bundled = S.bundleAll(res.traces, { keyOf: (r, X) => key({ kind: kindOf.get(r.id) }, X), free: freeTiles, chamfer: spec.p });
const issues = [];
const byId = new Map(bundled.map(r => [r.id, r]));
for (const r of bundled) {
  if (!R.isOctilinear(r.pts, 0.05)) issues.push(r.id + ': not octilinear');
  const s = r.pts[0], e = r.pts[r.pts.length - 1];
  if (Math.hypot(s[0] - r.padA.x, s[1] - r.padA.y) > 0.01 || Math.hypot(e[0] - r.padB.x, e[1] - r.padB.y) > 0.01) issues.push(r.id + ': ends not at pads');
  for (let i = 1; i < r.pts.length; i++) {
    const [x0, y0] = r.pts[i - 1], [x1, y1] = r.pts[i], n = Math.ceil(Math.hypot(x1 - x0, y1 - y0) / 2);
    for (let k = 0; k <= n; k++) {
      const hit = insideAnyTile(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n, spec.tiles.filter(t => t.id !== r.a && t.id !== r.b));
      if (hit) { issues.push(r.id + ': crosses tile ' + hit); k = n; i = r.pts.length; }
    }
  }
  // a member only ever bundles with a partner of the same key (same end tile, same kind)
  for (const b of r.bundles || []) {
    const p = byId.get(b.partner), end = b.key.split('|')[0];
    if (!p || kindOf.get(p.id) !== kindOf.get(r.id) || (p.a !== end && p.b !== end) || (r.a !== end && r.b !== end)) issues.push(r.id + ': wrong partner ' + b.partner);
    if (!(b.shared >= 2)) issues.push(r.id + ': no shared stretch');
  }
}
out.board = {
  traces: bundled.length, routed: res.traces.length, nets: spec.nets.length,
  members: bundled.filter(r => r.bundles).length, trunks: bundled.filter(r => r.trunk).length,
  maxMembers: Math.max(...bundled.map(r => r.members)), inputUntouched: JSON.stringify(res.traces) === before,
  issues: issues.slice(0, 10)
};
// null key = never bundle; different kinds never bundle
const none = S.bundleAll(res.traces, { keyOf: () => null, free: freeTiles, chamfer: spec.p });
out.none = { members: none.filter(r => r.bundles).length, maxMembers: Math.max(...none.map(r => r.members)) };
console.log(JSON.stringify(out));
