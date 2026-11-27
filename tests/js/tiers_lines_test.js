/* Line routing of the bus layout under Node (board/tiers.js + layout.js): rectangular lines, few bends, no crossings.
   Two cases of the report: the guest box under a hypervisor and the radio box of a repeater. Synthetic, neutral view. */
'use strict';
const path = require('path');
const B = path.join(__dirname, '..', '..', 'lanloom', 'web', 'board');
const T = require(path.join(B, 'tiers.js'));
const L = require(path.join(B, 'layout.js'));
const R = require(path.join(B, 'router.js'));
const S = require(path.join(B, 'bundles.js'));

function makeView(opts) {
  const net = 'net:192.168.50.0/24';
  const zones = [
    { id: 'edge', title: '', title_key: 'zone.edge', kind: 'edge', span: 'full', order: 0 },
    { id: 'cabling', title: '', title_key: 'zone.cabling', kind: 'cabling', span: 'full', order: 0.5 },
    { id: net, title: '192.168.50.0/24', title_key: null, kind: 'net', span: null, order: 1 }
  ];
  if (opts.hv) zones.push({ id: 'guests:hv', title: '', title_key: 'zone.guests', kind: 'guests', span: null, order: 1.4, vars: { host: 'Hypervisor' } });
  const comps = [], traces = [];
  const c = (id, zone, kind, title) => comps.push({ id, zone, kind, title, subtitle: '', status: 'up', grade: 'realtime', metric: null, badges: [] });
  const tr = (a, b, kind) => traces.push({ id: 'p:' + a + ':' + b, a, b, kind, measured: false });
  c('internet', 'edge', 'internet', ''); c('gw', 'edge', 'router', 'Gateway');
  traces.push({ id: 'u:internet:gw', a: 'internet', b: 'gw', kind: 'uplink', measured: false });
  c('sw', 'cabling', 'switch', 'Switch'); tr('gw', 'sw', 'cable');
  // the hypervisor and the access point sit among other devices of the switch (two rows)
  const kids = opts.kids || 8;
  for (let i = 1; i <= kids; i++) { c('pc' + i, net, 'computer', 'Computer ' + i); tr('sw', 'pc' + i, 'cable'); }
  if (opts.hv) {
    c('hv', net, 'server', 'Hypervisor'); tr('sw', 'hv', 'cable');
    for (let i = 1; i <= 3; i++) { c('g' + i, 'guests:hv', 'service', 'Guest ' + i); tr('hv', 'g' + i, 'virtual'); }
  }
  if (opts.rep) {
    // a repeater on an access point, with devices of its own (its radio box), and phones on the access point
    c('ap', net, 'ap', 'Access point'); tr('sw', 'ap', 'cable');
    c('rep', net, 'ap', 'Repeater'); tr('ap', 'rep', 'radio');
    for (let i = 1; i <= 5; i++) { c('ph' + i, net, 'phone', 'Phone ' + i); tr('ap', 'ph' + i, 'radio'); }
    for (let i = 1; i <= 6; i++) { c('rd' + i, net, 'iot', 'Sensor ' + i); tr('rep', 'rd' + i, 'radio'); }
  }
  return { view: 'network', zones, components: comps, traces };
}

const segs = pts => { const o = []; for (let i = 1; i < pts.length; i++) o.push([pts[i - 1], pts[i]]); return o; };
const bends = pts => { let n = 0; for (let i = 2; i < pts.length; i++) { const a = pts[i - 2], b = pts[i - 1], q = pts[i]; if (Math.abs((b[0] - a[0]) * (q[1] - b[1]) - (b[1] - a[1]) * (q[0] - b[0])) > 0.01) n++; } return n; };
const slanted = pts => segs(pts).filter(([a, b]) => Math.abs(a[0] - b[0]) > 0.5 && Math.abs(a[1] - b[1]) > 0.5).length;
const cross = (s, t) => {
  const d = (u, v, w) => (v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0]);
  const [p, q] = s, [a, b] = t;
  return d(p, q, a) * d(p, q, b) < -0.01 && d(a, b, p) * d(a, b, q) < -0.01;
};

function board(view, width) {
  const key = (t, X) => X + '|' + t.kind + '|';
  const kindOf = new Map(view.traces.map(t => [t.id, t.kind]));
  const ly = L.compute(view, { width });
  const spec = L.routerSpec(ly, view, { Router: R, groupOf: key });
  const res = R.routeAll(spec);
  const rects = Object.values(ly.tiles).map(k => [k.x + 0.6, k.y + 0.6, k.x + k.w - 0.6, k.y + k.h - 0.6]);
  const free = (x0, y0, x1, y1) => !rects.some(q => x0 < q[2] && x1 > q[0] && y0 < q[3] && y1 > q[1]);
  const bundled = S.bundleAll(res.traces, { keyOf: (rt, X) => key({ kind: kindOf.get(rt.id) }, X), free, chamfer: spec.p });
  return { ly, bundled, shaped: T.busRoutes(bundled, ly, view) };
}

function tileHits(ly, r) {
  const hits = [];
  segs(r.pts).forEach(([a, b]) => {
    const n = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / 2));
    for (let i = 0; i <= n; i++) {
      const x = a[0] + (b[0] - a[0]) * i / n, y = a[1] + (b[1] - a[1]) * i / n;
      const t = Object.values(ly.tiles).find(k => k.id !== r.a && k.id !== r.b && x > k.x + 1 && x < k.x + k.w - 1 && y > k.y + 1 && y < k.y + k.h - 1);
      if (t && !hits.includes(t.id)) hits.push(t.id);
    }
  });
  return hits;
}

function measure(opts, width) {
  const v = T.regroup(makeView(opts));
  const { ly, shaped } = board(v, width);
  const group = prefix => shaped.filter(r => r.bus && r.bus.startsWith(prefix));
  const stat = list => ({ n: list.length, maxBends: Math.max(0, ...list.map(r => bends(r.pts))), slanted: list.reduce((s, r) => s + slanted(r.pts), 0), tileHits: list.reduce((s, r) => s + tileHits(ly, r).length, 0), onBus: list.every(r => !!r.bus) });
  const busList = shaped.filter(r => r.bus);
  let crossings = 0;
  for (let i = 0; i < busList.length; i++) for (let j = i + 1; j < busList.length; j++) {
    if (busList[i].a === busList[j].a) continue;
    segs(busList[i].pts).forEach(s => segs(busList[j].pts).forEach(t => { if (cross(s, t)) crossings++; }));
  }
  return {
    guests: stat(group('guests:hv')),                     // the guest box under a hypervisor
    repeater: stat(group('bus:rep:radio')),               // the radio box of a repeater
    switchBox: stat(group('bus:sw:cable')),
    apRadio: stat(group('bus:ap:radio')),
    all: stat(busList), crossings,
    slantedAnywhere: shaped.reduce((s, r) => s + slanted(r.pts), 0),
    allCovered: shaped.length === v.traces.length,
    startsAtPad: busList.every(r => Math.hypot(r.pts[0][0] - r.padA.x, r.pts[0][1] - r.padA.y) < 0.01),
    endsOnTile: busList.every(r => { const k = ly.tiles[r.b], e = r.pts[r.pts.length - 1]; return Math.abs(e[1] - k.y) < 0.01 && e[0] > k.x && e[0] < k.x + k.w; })
  };
}

const out = { widths: {} };
[1512, 1280, 1920].forEach(width => {
  out.widths[width] = { hv: measure({ hv: true }, width), rep: measure({ rep: true }, width), both: measure({ hv: true, rep: true, kids: 6 }, width) };
});
// a plain view keeps the plain layout: the zones of a row stay side by side, nothing carries a parent
(function () {
  const v = makeView({ hv: true, rep: true });
  out.plain_has_parent = v.zones.some(z => z.parent);
  out.plain_rows = new Set(L.compute(v, { width: 1512 }).zones.map(z => z.y)).size;
})();

process.stdout.write(JSON.stringify(out));
