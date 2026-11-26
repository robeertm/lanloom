/* Lanloom — bus layout of the Network board (optional, chosen per browser).
   The server cuts the network board into one box per network or VLAN, so the children of one switch or access point are spread
   over several boxes and their traces cannot share a trunk. regroup() cuts the same view into one box per parent and medium
   ("LAN on <switch>", "Wireless on <access point>"), sorted by the depth of the parent in the chain (internet edge first). The
   boxes of one depth start a row of their own, so the rows follow the topology. The traces stay exactly as they are: the
   bundling of the board then pulls the traces of one parent onto one trunk by itself, and the dots of all children add up on it.

   Pure model code (no DOM), usable in the window and under Node (tests). The input view is never changed.

   regroup(view, opts) → new view
     opts = { name(component) → text for the box title (default: title, subtitle or id) }
     - a child whose box is a network ("net:*") or the cabling bank moves into "bus:<parent>:cable" or "bus:<parent>:radio"
       (title_key zone.bus_lan / zone.bus_wireless, vars.host); virtual links keep their "guests:<host>" box
     - guests, containers and the radio networks of a hub keep their boxes, sorted behind their host's depth
     - a device without a known parent stays in its network box; those boxes come last (nothing is guessed)
     - the "+N quiet devices" fold belongs to the network boxes, so it is dissolved: every device is shown
     - every zone carries tier (row of the board); the order inside a box stays the order of the server (switch port first) */
(function (root, factory) {
  const T = factory();
  if (typeof module === 'object' && module.exports) module.exports = T;
  else { root.Lanloom = root.Lanloom || {}; root.Lanloom.Tiers = T; }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const PHYS = ['cable', 'radio', 'virtual'];
  const FAR = 50;                    // depth of a device that cannot be reached from the internet edge
  const REST = 1000;                 // network boxes of devices without a known parent come last
  const RADIO_NETS = 900;            // radio networks of a hub (keep their own order among themselves)

  const movable = zone => zone === 'cabling' || String(zone || '').startsWith('net:');
  const defaultName = c => String((c && (c.title || c.subtitle || c.id)) || '');

  function regroup(view, opts) {
    opts = opts || {};
    const name = typeof opts.name === 'function' ? opts.name : defaultName;
    const comps = (view && view.components) || [], traces = (view && view.traces) || [], zones = (view && view.zones) || [];
    const byId = new Map(comps.map(c => [c.id, c]));
    // parent of every child: the first physical trace (a = parent, b = child) whose both ends are on the board
    const parent = new Map(), kids = new Map();
    traces.forEach(t => {
      if (!t || !PHYS.includes(t.kind) || t.a === t.b || !byId.has(t.a) || !byId.has(t.b) || parent.has(t.b)) return;
      parent.set(t.b, { id: t.a, kind: t.kind });
      if (!kids.has(t.a)) kids.set(t.a, []);
      kids.get(t.a).push(t.b);
    });
    // depth: breadth-first from the tiles of the internet edge
    const depth = new Map(), queue = [];
    comps.forEach(c => { if (c.zone === 'edge') { depth.set(c.id, 0); queue.push(c.id); } });
    for (let i = 0; i < queue.length; i++) {
      const id = queue[i];
      (kids.get(id) || []).forEach(k => { if (!depth.has(k)) { depth.set(k, depth.get(id) + 1); queue.push(k); } });
    }
    const depthOf = id => (depth.has(id) ? depth.get(id) : FAR);

    const out = [], made = new Map();
    const busZone = (pid, medium) => {
      const zid = 'bus:' + pid + ':' + medium;
      if (!made.has(zid)) {
        const d = depthOf(pid);
        made.set(zid, { id: zid, title: '', title_key: medium === 'cable' ? 'zone.bus_lan' : 'zone.bus_wireless', kind: 'bus', span: null,
          vars: { host: name(byId.get(pid)) }, parent: pid, order: 10 * d + (medium === 'cable' ? 0.1 : 0.2), tier: d + 1 });
      }
      return zid;
    };
    comps.forEach(c => {
      if (c.quiet_more) return;                                               // the fold tile of a network box
      let n = c;
      if (typeof c.fold === 'string' && c.fold.startsWith('quiet:')) { n = Object.assign({}, c); delete n.fold; }
      const p = parent.get(c.id);
      if (p && p.kind !== 'virtual' && movable(c.zone)) {
        if (n === c) n = Object.assign({}, c);
        n.zone = busZone(p.id, p.kind);
      }
      out.push(n);
    });

    // the boxes that stay: same fields, a new order and row
    const used = new Set(out.map(c => String(c.zone)));
    const hostZone = (prefix, zid) => zid.slice(prefix.length);
    const kept = [];
    zones.forEach((z, i) => {
      const zid = String(z.id);
      if (!used.has(zid)) return;
      const nz = Object.assign({}, z);
      const o = Number.isFinite(+z.order) ? +z.order : i;
      if (zid === 'edge') { nz.order = 0; nz.tier = 0; }
      else if (zid === 'cabling') { nz.order = 0.05; nz.tier = 0.5; }      // switches without a known parent stay on their bank
      else if (zid.startsWith('guests:')) { const d = depthOf(hostZone('guests:', zid)); nz.order = 10 * d + 0.6; nz.tier = d + 1; nz.parent = hostZone('guests:', zid); }
      else if (zid.startsWith('containers:')) { const d = depthOf(hostZone('containers:', zid)); nz.order = 10 * d + 0.65; nz.tier = d + 1; nz.parent = hostZone('containers:', zid); }
      else if (zid.startsWith('radio:') || zid.startsWith('radios:')) { nz.order = RADIO_NETS + o; nz.tier = RADIO_NETS; }
      else { nz.order = REST + o; nz.tier = REST; }
      kept.push({ z: nz, i });
    });
    const all = kept.concat([...made.values()].filter(z => used.has(z.id)).map(z => ({ z, i: zones.length })));
    all.sort((p, q) => (p.z.order - q.z.order) || (p.i - q.i) || (String(p.z.id) < String(q.z.id) ? -1 : 1));
    return Object.assign({}, view, { components: out, zones: all.map(x => x.z), traces: traces.slice(), bus: true });
  }

  // ------------------------------------------------------------------ bus lines
  const EPS = 0.5;
  function clean(pts) {
    const out = [];
    for (const q of pts) {
      const p = out[out.length - 1];
      if (p && Math.abs(p[0] - q[0]) < 0.01 && Math.abs(p[1] - q[1]) < 0.01) continue;
      if (out.length >= 2) {
        const a = out[out.length - 2];
        const h = Math.abs(a[1] - p[1]) < 0.01 && Math.abs(p[1] - q[1]) < 0.01, v = Math.abs(a[0] - p[0]) < 0.01 && Math.abs(p[0] - q[0]) < 0.01;
        if (h || v) { out[out.length - 1] = q.slice(); continue; }
      }
      out.push(q.slice());
    }
    return out;
  }
  const lengthOf = pts => { let L = 0; for (let i = 1; i < pts.length; i++) L += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); return L; };

  /* The way of a routed trace down to the bus line of its box: walking from the parent, the first point where the route meets the
     height y inside the box (x within the frame), without having passed through the box below that height before.
     → { pts: prefix ending in the meeting point, L } or null */
  function prefixTo(pts, y, box) {
    const out = [pts[0].slice()];
    const inside = q => q[0] > box.x + EPS && q[0] < box.x + box.w - EPS && q[1] > y + EPS && q[1] < box.y + box.h;
    if (inside(pts[0])) return null;
    for (let i = 1; i < pts.length; i++) {
      const p = pts[i - 1], q = pts[i];
      const lo = Math.min(p[1], q[1]), hi = Math.max(p[1], q[1]);
      if (lo <= y + EPS && hi >= y - EPS) {
        const f = Math.abs(q[1] - p[1]) < 0.01 ? 0 : (y - p[1]) / (q[1] - p[1]);
        const x = p[0] + (q[0] - p[0]) * Math.max(0, Math.min(1, f));
        if (x > box.x + EPS && x < box.x + box.w - EPS) { out.push([x, y]); return { pts: clean(out), L: lengthOf(out) }; }
      }
      if (inside(q)) return null;
      out.push(q.slice());
    }
    return null;
  }

  /* busRoutes(routes, layout, view, o) → routes, where the traces of every box with one parent (bus boxes, guests, containers) run
     as ONE line: the way of one routed child (the shortest) from the parent down to the top channel of the box, a bus line along it,
     and a short drop to the top of every tile ("upside-down T"); further tile rows hang on a spine in a column gap and a line in
     the gap above their row. The parts the children share lie exactly on top of each other, so the board draws one line and the
     dots of all children add up on it. A box whose way in cannot be found keeps the routed traces (nothing is forced).
     o = { grid: px (pad positions snap to it) } */
  function busRoutes(routes, layout, view, o) {
    o = o || {};
    if (!layout || layout.compact || !view || !Array.isArray(routes)) return routes;
    const m = layout.m || {}, grid = o.grid || m.grid || 4;
    const snap = v => Math.round(v / grid) * grid;
    const zoneBox = new Map((layout.zones || []).map(z => [z.id, z]));
    const byId = new Map(routes.map(r => [r.id, r]));
    const out = new Map();
    (view.zones || []).forEach(z => {
      const box = zoneBox.get(z.id);
      if (!z.parent || !box) return;
      const members = (view.traces || []).filter(t => t && t.a === z.parent && PHYS.includes(t.kind) && layout.tiles[t.b] && layout.tiles[t.b].zone === z.id && byId.has(t.id));
      if (!members.length) return;
      const tiles = members.map(t => layout.tiles[t.b]);
      const tops = [...new Set(tiles.map(t => t.y))].sort((a, b) => a - b);
      const lineY = tops.map((y, r) => (r === 0 ? y - Math.round((m.padTop || 40) * 0.35) : y - Math.round((m.gapY || 36) / 2)));
      // the way in: the shortest routed child that reaches the top channel of the box from above
      let lead = null;
      members.forEach(t => {
        const r = byId.get(t.id);
        if (!r || r.fallback || !r.pts || r.pts.length < 2) return;
        const pre = prefixTo(r.pts, lineY[0], box);
        if (pre && (!lead || pre.L < lead.pre.L)) lead = { r, pre };
      });
      if (!lead) return;
      const J = lead.pre.pts[lead.pre.pts.length - 1];
      // spine for the lower rows: the column gap (or box margin) next to the way in
      let spine = J[0];
      if (tops.length > 1) {
        const row0 = Object.values(layout.tiles).filter(t => t.zone === z.id && t.y === tops[0]).sort((a, b) => a.x - b.x);
        const gaps = [row0[0].x - (m.padX || 24) / 2].concat(row0.slice(1).map((t, i) => (row0[i].x + row0[i].w + t.x) / 2), [row0[row0.length - 1].x + row0[row0.length - 1].w + (m.padX || 24) / 2]);
        spine = gaps.reduce((best, g) => (Math.abs(g - J[0]) < Math.abs(best - J[0]) ? g : best), gaps[0]);
      }
      members.forEach(t => {
        const r = byId.get(t.id), k = layout.tiles[t.b], row = tops.indexOf(k.y);
        const cx = snap(k.x + k.w / 2);
        const tail = row === 0 ? [[cx, lineY[0]], [cx, k.y]] : [[spine, lineY[0]], [spine, lineY[row]], [cx, lineY[row]], [cx, k.y]];
        const pts = clean(lead.pre.pts.concat(tail));
        const n = Object.assign({}, r, { pts, padA: Object.assign({}, lead.r.padA), padB: { x: cx, y: k.y, s: 't' }, vias: [], members: members.length, bus: z.id });
        delete n.bundles; delete n.trunk; delete n.fallback;
        out.set(t.id, n);
      });
    });
    return out.size ? routes.map(r => out.get(r.id) || r) : routes;
  }

  return { regroup, busRoutes, prefixTo, FAR, REST };
});
