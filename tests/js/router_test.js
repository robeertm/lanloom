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
  const spec = L.routerSpec(lay, v, { Router: R, groupOf: (t, X) => X + '|' + t.kind + '|' });   // bundle groups as on the board
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
// stable core API
out.api = { api: R.API, version: R.VERSION, names: ['Grid', 'Heap', 'routeAll', 'buildGrid', 'buildNets', 'tracesFor', 'kindFromText', 'orderNets',
  'fallbackRoute', 'railRoutes', 'chamferPts', 'offset', 'isOctilinear', 'octilinear', 'octilinearize', 'simplify', 'length', 'decay', 'refresh',
  'startWorker', 'hash', 'cacheKey'].filter(k => typeof R[k] !== 'function') };
// bundled traces: members of one group share cells and the pad at their common target; other traces never do
{
  const tiles = [{ id: 'hub', x: 300, y: 40, w: 120, h: 40 }];
  for (let i = 0; i < 5; i++) tiles.push({ id: 's' + i, x: 40 + i * 150, y: 300, w: 100, h: 40 });
  const nets = [0, 1, 2, 3, 4].map(i => ({ id: 'n' + i, a: 's' + i, b: 'hub', kind: 'flow', groupB: 'hub|flow|' }));
  const spec = { w: 840, h: 400, p: 4, kl: 2, tiles, soft: [], lines: [], nets };
  const res = R.routeAll(spec);
  const sep = R.routeAll(Object.assign({}, spec, { nets: nets.map(n => ({ id: n.id, a: n.a, b: n.b, kind: n.kind })) }));
  const padsB = new Set(res.traces.map(r => r.padB.x + ',' + r.padB.y));
  const padsSep = new Set(sep.traces.map(r => r.padB.x + ',' + r.padB.y));
  out.groups = { routed: res.traces.length, failed: res.failed.length, sharedPads: padsB.size, separatePads: padsSep.size,
    issues: checkRoutes(res.traces, spec).concat(checkRoutes(sep.traces, spec)).slice(0, 10) };
}
// rim node: a zone frame to dock on; as target the result is turned back, the rim pad sits at the end
{
  const mk = () => { const g = new R.Grid({ w: 420, h: 300, p: 2, kl: 2 });
    g.tile('outer', 330, 100, 60, 30); g.tile('inner', 60, 150, 60, 30); g.tile('block', 150, 90, 40, 60);
    g.area(222, 80, 40, 50); g.rim('zone', 20, 20, 200, 220, 14); return g; };
  const a = mk().route({ id: 'a', a: 'zone', b: 'outer', kind: 'api', hardText: true });
  const u = mk().route({ id: 'u', a: 'outer', b: 'zone', kind: 'api' });
  out.rim = { padRim: !!(a && a.padA.rim), padX: a && a.padA.x, oct: !!(a && R.isOctilinear(a.pts)),
    turned: !!(u && u.a === 'outer' && u.b === 'zone' && u.padB.rim && !u.padA.rim && u.pts[u.pts.length - 1][0] === u.padB.x && u.pts[u.pts.length - 1][1] === u.padB.y) };
}
// worker: grids per uid are kept unless a route job is exclusive
{
  const msgs = []; const fake = { postMessage: m => msgs.push(m) };
  R.startWorker(fake);
  const spec = { w: 400, h: 200, p: 4, kl: 2, tiles: [{ id: 'x', x: 40, y: 40, w: 80, h: 40 }, { id: 'y', x: 260, y: 120, w: 80, h: 40 }], soft: [], lines: [], nets: [] };
  fake.onmessage({ data: { type: 'route', uid: 'u1', job: 1, spec } });
  fake.onmessage({ data: { type: 'route', uid: 'u2', job: 2, spec } });
  fake.onmessage({ data: { type: 'net', uid: 'u1', job: 3, net: { id: 'k', a: 'x', b: 'y', kind: 'lan' } } });
  fake.onmessage({ data: { type: 'route', uid: 'u3', job: 4, spec, exclusive: true } });
  fake.onmessage({ data: { type: 'net', uid: 'u1', job: 5, net: { id: 'k', a: 'x', b: 'y', kind: 'lan' } } });
  const tr = msgs.filter(m => m.type === 'trace');
  out.workerGrids = { keptBefore: !!tr[0].trace, droppedAfter: tr[1].trace === null };
}
// nets of a view: curated edges first, data edges with host vocabulary
{
  const nodes = { p: { x: 0, y: 0, w: 10, h: 10, sys: ['A'] }, q: { x: 100, y: 0, w: 10, h: 10, sys: ['B'] }, r: { x: 300, y: 0, w: 10, h: 10, sys: ['C'] } };
  const nb = R.buildNets({ nodes, curated: [{ from: 'p', to: 'q', kind: 'lan' }, { from: 'q', to: 'p', kind: 'lan' }],
    systems: [{ id: 'A', talksTo: [{ target: 'C', kind: 'Backup job' }, { target: 'B', kind: 'x' }] }, { id: 'C', host: 'cloud', talksTo: [] }],
    kindRules: [[/backup/, 'sync']], refineKind: (k, s) => (k === 'api' && s && s.host === 'cloud' ? 'cloud' : k) });
  out.nets = { n: nb.nets.length, first: nb.order[0].curated, both: nb.nets[0].both, kind: nb.nets[1].kind, data: nb.data.length,
    hits: R.tracesFor('A', 'C', nb.sysNodes, [{ a: 'r', b: 'p' }]).map(h => h[1]) };
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
