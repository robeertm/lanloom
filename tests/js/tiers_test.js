/* Bus layout checks under Node (board/tiers.js). Prints one JSON object; the pytest wrapper asserts on it.
   The view is synthetic and neutral (RFC 1918 example range, no real names). */
'use strict';
const path = require('path');
const B = path.join(__dirname, '..', '..', 'lanloom', 'web', 'board');
const T = require(path.join(B, 'tiers.js'));
const L = require(path.join(B, 'layout.js'));
const R = require(path.join(B, 'router.js'));
const S = require(path.join(B, 'bundles.js'));

// a network board as the server builds it: one box per network, the switches and access points on the cabling bank
function makeView() {
  const zones = [
    { id: 'edge', title: '', title_key: 'zone.edge', kind: 'edge', span: 'full', order: 0 },
    { id: 'cabling', title: '', title_key: 'zone.cabling', kind: 'cabling', span: 'full', order: 0.5 },
    { id: 'net:192.168.50.0/24', title: '192.168.50.0/24', title_key: null, kind: 'net', span: null, order: 1 },
    { id: 'net:192.168.60.0/24', title: '192.168.60.0/24', title_key: null, kind: 'net', span: null, order: 2 },
    { id: 'guests:vh', title: '', title_key: 'zone.guests', kind: 'guests', span: null, order: 1.4, vars: { host: 'Host' } },
    { id: 'containers:nas', title: '', title_key: 'zone.containers', kind: 'containers', span: null, order: 1.45, vars: { host: 'Storage' }, fold: 'nas' }
  ];
  const comps = [], traces = [];
  const c = (id, zone, kind, title, extra) => comps.push(Object.assign({ id, zone, kind, title, subtitle: '', status: 'up', grade: 'realtime', metric: null, badges: [] }, extra || {}));
  const tr = (a, b, kind) => traces.push({ id: (kind === 'lan' ? 'l:' : 'p:') + a + ':' + b, a, b, kind, measured: false });
  c('internet', 'edge', 'internet', ''); c('gw', 'edge', 'router', 'Gateway');
  traces.push({ id: 'u:internet:gw', a: 'internet', b: 'gw', kind: 'uplink', measured: false });
  c('sw1', 'cabling', 'switch', 'Switch A'); tr('gw', 'sw1', 'cable');
  c('sw2', 'cabling', 'switch', 'Switch B'); tr('sw1', 'sw2', 'cable');
  c('ap', 'cabling', 'ap', 'Access point'); tr('sw1', 'ap', 'cable');
  // children of switch A, spread over both networks (port order = order of the list)
  const nets = ['net:192.168.50.0/24', 'net:192.168.60.0/24'];
  ['nas', 'vh', 'pc1', 'pc2', 'tv'].forEach((id, i) => { c(id, nets[i % 2], i === 1 ? 'server' : 'computer', 'Device ' + id); tr('sw1', id, 'cable'); });
  ['pc3', 'pc4', 'cam1'].forEach((id, i) => { c(id, nets[(i + 1) % 2], 'computer', 'Device ' + id); tr('sw2', id, 'cable'); });
  ['ph1', 'ph2', 'tab', 'iot1', 'iot2', 'iot3'].forEach((id, i) => {
    c(id, nets[i % 2], i < 3 ? 'phone' : 'iot', 'Device ' + id, i >= 3 ? { fold: 'quiet:' + nets[i % 2] } : null); tr('ap', id, 'radio');
  });
  c('qm:' + nets[1], nets[1], 'iot', '', { title_key: 'comp.quiet.more', vars: { n: 2 }, quiet_more: nets[1], fold_key: 'quiet:' + nets[1] });
  c('g1', 'guests:vh', 'service', 'Guest 1'); tr('vh', 'g1', 'virtual');
  c('g2', 'guests:vh', 'service', 'Guest 2'); tr('vh', 'g2', 'virtual');
  c('ct1', 'containers:nas', 'container', 'Box 1', { fold: 'nas' }); tr('nas', 'ct1', 'virtual');
  c('lone', nets[0], 'unknown', 'Loose device'); tr('gw', 'lone', 'lan');
  return { view: 'network', zones, components: comps, traces };
}

const out = {};
const v = makeView();
const before = JSON.stringify(v);
const r = T.regroup(v);
out.unchanged = JSON.stringify(v) === before;
const zoneOf = id => (r.components.find(x => x.id === id) || {}).zone;
const members = zid => r.components.filter(x => x.zone === zid).map(x => x.id);
out.zones = r.zones.map(z => ({ id: z.id, order: z.order, tier: z.tier, title_key: z.title_key || null, host: (z.vars && z.vars.host) || null, span: z.span || null, items: members(z.id) }));
out.zone_of = Object.fromEntries(r.components.map(x => [x.id, x.zone]));
out.quiet_tile = r.components.some(x => x.quiet_more);
out.quiet_folds = r.components.filter(x => typeof x.fold === 'string' && x.fold.startsWith('quiet:')).length;
out.container_fold = (r.components.find(x => x.id === 'ct1') || {}).fold || null;
out.traces_same = JSON.stringify(r.traces) === JSON.stringify(v.traces);
out.fields_same = r.components.every(x => { const o = v.components.find(y => y.id === x.id); return ['id', 'kind', 'status', 'grade', 'metric', 'title'].every(k => JSON.stringify(o[k]) === JSON.stringify(x[k])); });
out.name_option = T.regroup(v, { name: comp => 'N:' + comp.id }).zones.find(z => z.id === 'bus:sw1:cable').vars.host;
out.empty = T.regroup({ components: [], zones: [], traces: [] }).zones.length;

// layout: every tier starts a row of its own (a tier never shares a row with another tier)
const lay = L.compute(r, { width: 1512 });
const tierOf = new Map(r.zones.map(z => [z.id, z.tier]));
const rows = new Map();
lay.zones.forEach(z => { const k = z.y; if (!rows.has(k)) rows.set(k, new Set()); rows.get(k).add(tierOf.get(z.id)); });
out.mixed_rows = [...rows.values()].filter(s => s.size > 1).length;
out.rows_in_order = [...rows.keys()].sort((a, b) => a - b).map(y => [...rows.get(y)][0]);
const tiles = Object.values(lay.tiles);
out.overlaps = 0;
for (let i = 0; i < tiles.length; i++) for (let j = i + 1; j < tiles.length; j++) {
  const a = tiles[i], b = tiles[j];
  if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) out.overlaps++;
}
// without tiers (the usual board) the layout does not change: zones of one row keep packing side by side
const plain = L.compute(v, { width: 1512 });
out.plain_rows = new Set(plain.zones.map(z => z.y)).size;

// trunk: route and bundle the board like the window does (key = end tile + kind), count the children of switch A that ride
// on a trunk at the switch — the usual board versus the bus layout
function bundledAt(view, parent) {
  const kindOf = new Map(view.traces.map(t => [t.id, t.kind]));
  const key = (t, X) => X + '|' + t.kind + '|';
  const ly = L.compute(view, { width: 1512 });
  const spec = L.routerSpec(ly, view, { Router: R, groupOf: key });
  const res = R.routeAll(spec);
  const rects = Object.values(ly.tiles).map(k => [k.x + 0.6, k.y + 0.6, k.x + k.w - 0.6, k.y + k.h - 0.6]);
  const free = (x0, y0, x1, y1) => !rects.some(q => x0 < q[2] && x1 > q[0] && y0 < q[3] && y1 > q[1]);
  const b = S.bundleAll(res.traces, { keyOf: (rt, X) => key({ kind: kindOf.get(rt.id) }, X), free, chamfer: spec.p });
  const kids = view.traces.filter(t => t.a === parent && t.kind === 'cable').map(t => t.id);
  const on = b.filter(rt => kids.includes(rt.id) && (rt.trunk || (rt.bundles || []).some(s => s.key.startsWith(parent + '|')))).length;
  return { kids: kids.length, on, failed: res.failed ? res.failed.length : 0 };
}
out.trunk_plain = bundledAt(v, 'sw1');
out.trunk_bus = bundledAt(r, 'sw1');

// bus lines: route and bundle the bus layout, then shape it; every child of switch A hangs on one line
(function () {
  const kindOf = new Map(r.traces.map(t => [t.id, t.kind]));
  const key = (t, X) => X + '|' + t.kind + '|';
  const ly = L.compute(r, { width: 1512 });
  const spec = L.routerSpec(ly, r, { Router: R, groupOf: key });
  const res = R.routeAll(spec);
  const rects = Object.values(ly.tiles).map(k => [k.x + 0.6, k.y + 0.6, k.x + k.w - 0.6, k.y + k.h - 0.6]);
  const free = (x0, y0, x1, y1) => !rects.some(q => x0 < q[2] && x1 > q[0] && y0 < q[3] && y1 > q[1]);
  const bundled = S.bundleAll(res.traces, { keyOf: (rt, X) => key({ kind: kindOf.get(rt.id) }, X), free, chamfer: spec.p });
  const frozen = JSON.stringify(bundled);
  const shaped = T.busRoutes(bundled, ly, r);
  const issues = [];
  if (JSON.stringify(bundled) !== frozen) issues.push('input changed');
  const kids = r.traces.filter(t => t.a === 'sw1' && t.kind === 'cable').map(t => t.id);
  const mine = shaped.filter(x => kids.includes(x.id));
  // one way out of the parent: all children start with the same stretch, down to the bus line of the box
  const box = ly.zones.find(z => z.id === 'bus:sw1:cable');
  const firstTop = Math.min(...mine.map(x => ly.tiles[x.b].y));
  // one way out of the parent: the pad and the vertical line below it are the same for every child
  const shared = pts => JSON.stringify([pts[0], pts[1][0]]);
  const ways = new Set(mine.map(x => shared(x.pts)));
  for (const x of mine) {
    const k = ly.tiles[x.b], e = x.pts[x.pts.length - 1];
    if (x.bus !== 'bus:sw1:cable') issues.push(x.id + ': not on the bus');
    if (!(Math.abs(e[1] - k.y) < 0.01 && e[0] > k.x && e[0] < k.x + k.w) || x.padB.s !== 't') issues.push(x.id + ': drop does not end on the top of its tile');
    if (Math.hypot(x.pts[0][0] - x.padA.x, x.pts[0][1] - x.padA.y) > 0.01) issues.push(x.id + ': does not start at its pad');
    if (!R.isOctilinear(x.pts, 0.05)) issues.push(x.id + ': not octilinear');
    // the part inside the box never crosses a foreign tile
    for (let i = 1; i < x.pts.length; i++) {
      const [x0, y0] = x.pts[i - 1], [x1, y1] = x.pts[i];
      if (y0 < box.y && y1 < box.y) continue;
      const n = Math.ceil(Math.hypot(x1 - x0, y1 - y0) / 2);
      for (let s2 = 0; s2 <= n; s2++) {
        const px = x0 + (x1 - x0) * s2 / n, py = y0 + (y1 - y0) * s2 / n;
        const hit = Object.values(ly.tiles).find(t => t.id !== x.a && t.id !== x.b && px > t.x + 1 && px < t.x + t.w - 1 && py > t.y + 1 && py < t.y + t.h - 1);
        if (hit) { issues.push(x.id + ': crosses ' + hit.id); s2 = n; i = x.pts.length; }
      }
    }
  }
  const others = shaped.filter(x => !x.bus).map(x => x.id).sort();
  // the other traces keep their ends and pads; at most their slanted pieces became corners (rectify)
  const untouched = bundled.filter(x => !shaped.find(y => y.id === x.id && y.bus)).every(x => {
    const y = shaped.find(q => q.id === x.id);
    return JSON.stringify(x.padA) === JSON.stringify(y.padA) && JSON.stringify(x.padB) === JSON.stringify(y.padB) && JSON.stringify(x.pts[0]) === JSON.stringify(y.pts[0]) && JSON.stringify(x.pts[x.pts.length - 1]) === JSON.stringify(y.pts[y.pts.length - 1]);
  });
  out.bus = { kids: kids.length, on_bus: mine.filter(x => x.bus).length, ways: ways.size, rows: new Set(mine.map(x => ly.tiles[x.b].y)).size,
    members: [...new Set(mine.map(x => x.members))], issues: issues.slice(0, 8), untouched, others: others.length,
    guests: shaped.filter(x => x.bus === 'guests:vh').length, phone: T.busRoutes(bundled, Object.assign({}, ly, { compact: true }), r) === bundled };
})();

process.stdout.write(JSON.stringify(out));
