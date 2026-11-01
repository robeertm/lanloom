/* Router checks under Node. Prints one JSON object; the pytest wrapper asserts on it. */
'use strict';
const path = require('path');
const B = path.join(__dirname, '..', '..', 'lanloom', 'web', 'board');
const R = require(path.join(B, 'router.js'));
const L = require(path.join(B, 'layout.js'));
const F = require(path.join(__dirname, 'fixtures.js'));
const D = require(path.join(B, 'dots.js'));
const Dots = (D.Lanloom || (global.Lanloom)).Dots;

function insideAnyTile(x, y, tiles) {
  for (const t of tiles) if (x > t.x + 1 && x < t.x + t.w - 1 && y > t.y + 1 && y < t.y + t.h - 1) return t.id;
  return null;
}
function onEdge(p, t) {
  const tol = 0.6;
  const inX = p.x >= t.x - tol && p.x <= t.x + t.w + tol, inY = p.y >= t.y - tol && p.y <= t.y + t.h + tol;
  return (inX && (Math.abs(p.y - t.y) < tol || Math.abs(p.y - (t.y + t.h)) < tol)) || (inY && (Math.abs(p.x - t.x) < tol || Math.abs(p.x - (t.x + t.w)) < tol));
}
function checkRoutes(routes, spec) {
  const tiles = new Map(spec.tiles.map(t => [t.id, t]));
  const issues = [];
  for (const r of routes) {
    if (!R.isOctilinear(r.pts, 0.05)) issues.push(r.id + ': not octilinear');
    if (!onEdge(r.padA, tiles.get(r.a))) issues.push(r.id + ': padA not on tile edge');
    if (!onEdge(r.padB, tiles.get(r.b))) issues.push(r.id + ': padB not on tile edge');
    const s = r.pts[0], e = r.pts[r.pts.length - 1];
    if (Math.hypot(s[0] - r.padA.x, s[1] - r.padA.y) > 0.6 || Math.hypot(e[0] - r.padB.x, e[1] - r.padB.y) > 0.6) issues.push(r.id + ': ends not at pads');
    for (let i = 1; i < r.pts.length; i++) {
      const [x0, y0] = r.pts[i - 1], [x1, y1] = r.pts[i], n = Math.ceil(Math.hypot(x1 - x0, y1 - y0) / 2);
      for (let k = 0; k <= n; k++) {
        const hit = insideAnyTile(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n, spec.tiles);
        if (hit) { issues.push(r.id + ': crosses tile ' + hit); k = n; i = r.pts.length; }
      }
    }
  }
  return issues;
}

const out = { cases: [] };
for (const [n, w, traces] of [[10, 1512, 0], [40, 1512, 0], [120, 1512, 200], [120, 2560, 200]]) {
  const v = F.makeView(n, { traces });
  const lay = L.compute(v, { width: w });
  const spec = L.routerSpec(lay, v, { Router: R });
  const t0 = Date.now();
  const res = R.routeAll(spec);
  const ms = Date.now() - t0;
  out.cases.push({ n, w, nets: spec.nets.length, routed: res.traces.length, failed: res.failed.length, ms, issues: checkRoutes(res.traces, spec).slice(0, 10) });
  if (n === 40) {
    // cache: registering known routes and routing again returns the same set without searching
    const again = R.routeAll(spec, res.traces);
    out.cache = { same: again.traces.length === res.traces.length && again.failed.length === 0, key1: R.cacheKey(spec), key2: R.cacheKey(spec) };
    const moved = JSON.parse(JSON.stringify(spec)); moved.tiles[0].x += 4;
    out.cache.keyChanges = R.cacheKey(moved) !== out.cache.key1;
    // worker protocol with a fake worker scope
    const msgs = []; const fake = { postMessage: m => msgs.push(m) };
    R.startWorker(fake);
    fake.onmessage({ data: { type: 'route', uid: 'u', job: 1, spec } });
    const done = msgs.find(m => m.type === 'done');
    fake.onmessage({ data: { type: 'net', uid: 'u', job: 2, net: { id: 'extra', a: spec.nets[0].a, b: spec.nets[1].b, kind: 'flow' } } });
    const one = msgs.find(m => m.type === 'trace');
    out.worker = { done: !!done, all: done ? done.all.length : 0, nets: spec.nets.length, parts: msgs.filter(m => m.type === 'part').length, single: !!(one && one.trace) };
  }
}
// compact rail routes
out.rail = [];
for (const n of [10, 40, 120]) {
  const v = F.makeView(n);
  const lay = L.compute(v, { width: 393 });
  const spec = L.routerSpec(lay, v, { Router: R });
  const t0 = Date.now();
  const routes = R.railRoutes(spec);
  out.rail.push({ n, nets: spec.nets.length, routed: routes.length, ms: Date.now() - t0, issues: checkRoutes(routes, spec).slice(0, 10) });
}
// fallback geometry is always drawable
const fb = R.fallbackRoute({ id: 'f', a: 'a', b: 'b' }, { x: 0, y: 0, w: 100, h: 40 }, { x: 300, y: 200, w: 100, h: 40 });
out.fallback = { octilinear: R.isOctilinear(fb.pts), points: fb.pts.length };
// dot decomposition conserves quanta and respects the cap
out.decompose = [];
for (const [n, cap] of [[5, 40], [40, 40], [41, 40], [700, 40], [100000, 40], [9, 1], [3, 0]]) {
  const z = Dots.decompose(n, cap);
  const count = z.counts[0] + z.counts[1] + z.counts[2];
  out.decompose.push({ n, cap, count, conserved: z.counts[0] + z.counts[1] * 8 + z.counts[2] * 64 + z.rest === n, withinCap: count <= cap });
}
console.log(JSON.stringify(out));
