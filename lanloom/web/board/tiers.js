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
    // a device with children of its own goes to the end of its box: in the last row nothing stands below it, so its line to the
    // children runs straight down (the order of the others stays the order of the server)
    const lead = c => (String(c.zone).startsWith('bus:') && kids.has(c.id) ? 1 : 0);
    const keyed = out.map((c, i) => ({ c, i, k: lead(c) }));
    keyed.sort((p, q) => (p.k - q.k) || (p.i - q.i));
    out.length = 0;
    keyed.forEach(e => out.push(e.c));

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

  // ------------------------------------------------------------------ geometry helpers of the bus lines
  const bendsOf = pts => { let n = 0; for (let i = 2; i < pts.length; i++) { const a = pts[i - 2], b = pts[i - 1], c = pts[i]; if (Math.abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 0.01) n++; } return n; };
  const orthogonal = pts => pts.every((q, i) => i === 0 || Math.abs(q[0] - pts[i - 1][0]) < 0.01 || Math.abs(q[1] - pts[i - 1][1]) < 0.01);
  const median = a => { const b = a.slice().sort((p, q) => p - q); return b[Math.floor((b.length - 1) / 2)]; };

  /* One bus line = the way of ALL children of one parent: a trunk from the parent down to a vertical line (the "spine") in the box,
     and per child a branch off the spine at the height of its row and a short drop onto the top of its tile (an upside-down T).
     The trunk is the same for every child, so the lines lie exactly on top of each other and the dots of all children add up on it.
     All lines are horizontal and vertical (no diagonal). The search below chooses where the trunk leaves the parent and where the
     spine runs, so that the line has as few bends as possible: the straight way (parent pad above the spine) comes first, then
     one detour through the gap above the box (2 more bends), then one out of the side of the parent. */
  function planBus(ctx, z, box, P, members) {
    const { m, tiles, snap, committed, laneUse } = ctx;
    const padM = 14;                                              // pads keep this distance from the corners of a tile
    const pb = P.y + P.h;
    const ks = members.map(t => ctx.layout.tiles[t.b]);
    const tops = [...new Set(ks.map(t => t.y))].sort((a, b) => a - b);
    const lineY = tops.map((y, r) => (r === 0 ? y - Math.round((m.padTop || 40) * 0.35) : y - Math.round((m.gapY || 36) / 2)));
    if (pb + 6 > lineY[0]) return null;                           // the parent is not above the box
    const last = lineY[lineY.length - 1];
    const skip = new Set([P.id]);
    const rects = Object.values(tiles).filter(t => !skip.has(t.id));
    const padG = 3;
    const segFree = (a, b) => {
      const x0 = Math.min(a[0], b[0]), x1 = Math.max(a[0], b[0]), y0 = Math.min(a[1], b[1]), y1 = Math.max(a[1], b[1]);
      return !rects.some(t => x1 > t.x - padG && x0 < t.x + t.w + padG && y1 > t.y - padG && y0 < t.y + t.h + padG);
    };
    const free = pts => pts.every((q, i) => i === 0 || segFree(pts[i - 1], q));
    // soft costs: title chips of the boxes, frame lines, other lines (overlap, crossings)
    const chips = ctx.layout.zones.map(zz => ({ x0: zz.x, x1: zz.x + Math.min(zz.w - 8, 250), y0: zz.y - 10, y1: zz.y + 14 }));
    const softCost = pts => {
      let c = 0;
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i - 1], b = pts[i], v = Math.abs(a[0] - b[0]) < 0.01;
        const x0 = Math.min(a[0], b[0]), x1 = Math.max(a[0], b[0]), y0 = Math.min(a[1], b[1]), y1 = Math.max(a[1], b[1]);
        chips.forEach(q => { if (x1 > q.x0 && x0 < q.x1 && y1 > q.y0 && y0 < q.y1) c += 300; });
        ctx.layout.zones.forEach(zz => {
          if (v && y1 > zz.y && y0 < zz.y + zz.h && (Math.abs(a[0] - zz.x) < 6 || Math.abs(a[0] - zz.x - zz.w) < 6)) c += 200;
          if (!v && x1 > zz.x && x0 < zz.x + zz.w && (Math.abs(a[1] - zz.y) < 6 || Math.abs(a[1] - zz.y - zz.h) < 6)) c += 200;
        });
        committed.forEach(s2 => {
          if (s2.pid === P.id) return;                            // the lines of one parent may share a stretch
          const sv = Math.abs(s2.a[0] - s2.b[0]) < 0.01;
          const sx0 = Math.min(s2.a[0], s2.b[0]), sx1 = Math.max(s2.a[0], s2.b[0]), sy0 = Math.min(s2.a[1], s2.b[1]), sy1 = Math.max(s2.a[1], s2.b[1]);
          if (v === sv) {
            const same = v ? Math.abs(a[0] - s2.a[0]) < 3 && y1 > sy0 && y0 < sy1 : Math.abs(a[1] - s2.a[1]) < 3 && x1 > sx0 && x0 < sx1;
            if (same) c += 400;
          } else if (v ? (a[0] > sx0 && a[0] < sx1 && s2.a[1] > y0 && s2.a[1] < y1) : (s2.a[0] > x0 && s2.a[0] < x1 && a[1] > sy0 && a[1] < sy1)) c += 60;
        });
      }
      return c;
    };

    // places for the spine: a block of one row may hang on any x of the box, several rows need a column gap (or the margin) next to the block
    const cxs = ks.map(k => snap(k.x + k.w / 2));
    const boxLo = box.x + padM, boxHi = box.x + box.w - padM;
    let spines;
    if (tops.length === 1) {
      spines = new Set(cxs);
      spines.add(snap(median(cxs)));
      spines.add(snap((Math.min(...cxs) + Math.max(...cxs)) / 2));
      for (let x = Math.ceil(boxLo / 8) * 8; x <= boxHi; x += 8) spines.add(x);
    } else {
      const row0 = ks.filter(t => t.y === tops[0]).sort((a, b) => a.x - b.x);
      spines = new Set([row0[0].x - (m.padX || 24) / 2, row0[row0.length - 1].x + row0[row0.length - 1].w + (m.padX || 24) / 2]);
      row0.slice(1).forEach((t, i) => spines.add((row0[i].x + row0[i].w + t.x) / 2));
    }
    spines = [...spines].map(snap).filter(x => x >= boxLo && x <= boxHi);

    // where the trunk may leave the parent: the bottom edge (several x), or the sides (centre height) with a free corridor
    const lo = P.x + padM, hi = P.x + P.w - padM;
    const starts = [];
    for (let x = Math.ceil(lo / 4) * 4; x <= hi; x += 4) starts.push({ s: 'b', x, y: pb, lead: [[x, pb]] });
    const cy = snap(P.y + P.h / 2);
    [['r', P.x + P.w, 1], ['l', P.x, -1]].forEach(([s, ex, dir]) => {
      for (let d = 10; d <= 120; d += 6) { const gx = ex + dir * d; starts.push({ s, x: ex, y: cy, lead: [[ex, cy], [gx, cy]], gx }); }
    });
    spines.forEach(sx => { if (sx >= lo && sx <= hi && !starts.some(st => st.s === 'b' && st.x === sx)) starts.push({ s: 'b', x: sx, y: pb, lead: [[sx, pb]] }); });
    const laneYs = [box.y - 12, box.y - 8, box.y - 16].filter(y => y > pb);
    const branches = (T, sx) => ks.map((k, i) => {
      const r = tops.indexOf(k.y), cx = cxs[i];
      const trunk = [];
      for (const q of T) { if (q[1] >= lineY[r] - 0.01 && trunk.length && T.indexOf(q) === T.length - 1) { trunk.push([q[0], lineY[r]]); break; } trunk.push(q); }
      return clean(trunk.concat([[cx, lineY[r]], [cx, k.y]]));
    });

    let best = null;
    const tryT = (T, sx, extra) => {
      if (!free(T)) return;
      const paths = branches(T, sx);
      if (!paths.every(orthogonal)) return;
      const bends = paths.reduce((s2, p) => s2 + bendsOf(p), 0), worst = Math.max(...paths.map(bendsOf));
      const len = paths.reduce((s2, p) => s2 + lengthOf(p), 0);
      const score = worst * 10000 + bends * 100 + len / 50 + softCost(T) + (extra || 0);
      if (!best || score < best.score) best = { score, T, sx, paths, start: T[0] };
    };
    spines.forEach(sx => {
      starts.forEach(st => {
        if (st.s === 'b') {
          if (st.x === sx) tryT(clean([[sx, pb], [sx, last]]), sx);                                  // straight
          else laneYs.forEach(ly => { if (!laneUse(ly, st.x, sx, P.id)) tryT(clean([[st.x, pb], [st.x, ly], [sx, ly], [sx, last]]), sx, 5); });
        } else {
          laneYs.forEach(ly => tryT(clean(st.lead.concat([[st.gx, ly], [sx, ly], [sx, last]])), sx, 40));
          tryT(clean(st.lead.concat([[st.gx, last], [sx, last]])), sx, 80);                           // along the line of the last row (rare)
        }
      });
    });
    if (!best) return null;
    const pad = best.start.length ? best.T[0] : null;
    return { best, pad: { x: best.T[0][0], y: best.T[0][1], s: (starts.find(st => st.x === best.T[0][0] && st.y === best.T[0][1]) || { s: 'b' }).s } };
  }

  /* busRoutes(routes, layout, view, o) → routes, where the traces of every box with one parent (bus boxes, guests, containers) run
     as ONE line (see planBus). A box whose line cannot be placed (no free way from the parent) keeps the routed traces — nothing
     is forced.   o = { grid: px (pad positions snap to it) } */
  function busRoutes(routes, layout, view, o) {
    o = o || {};
    if (!layout || layout.compact || !view || !Array.isArray(routes)) return routes;
    const m = layout.m || {}, grid = o.grid || m.grid || 4;
    const snap = v => Math.round(v / grid) * grid;
    const zoneBox = new Map((layout.zones || []).map(z => [z.id, z]));
    const byId = new Map(routes.map(r => [r.id, r]));
    const out = new Map();
    const committed = [], lanes = [];
    const laneUse = (y, xa, xb, pid) => lanes.some(l => l.pid !== pid && Math.abs(l.y - y) < 3 && Math.max(xa, xb) > l.x0 - 6 && Math.min(xa, xb) < l.x1 + 6);
    const ctx = { layout, m, tiles: layout.tiles, snap, committed, laneUse };
    (view.zones || []).forEach(z => {
      const box = zoneBox.get(z.id), P = z.parent && layout.tiles[z.parent];
      if (!box || !P || P.zone === z.id) return;
      const members = (view.traces || []).filter(t => t && t.a === z.parent && PHYS.includes(t.kind) && layout.tiles[t.b] && layout.tiles[t.b].zone === z.id && byId.has(t.id));
      if (!members.length) return;
      const plan = planBus(ctx, z, box, P, members);
      if (!plan) return;
      const { best, pad } = plan;
      members.forEach((t, i) => {
        const r = byId.get(t.id), k = layout.tiles[t.b], cx = snap(k.x + k.w / 2);
        const n = Object.assign({}, r, { pts: best.paths[i], padA: pad, padB: { x: cx, y: k.y, s: 't' }, vias: [], members: members.length, bus: z.id });
        delete n.bundles; delete n.trunk; delete n.fallback;
        out.set(t.id, n);
      });
      const T = best.T;
      for (let q = 1; q < T.length; q++) if (Math.abs(T[q][1] - T[q - 1][1]) < 0.01) lanes.push({ pid: P.id, y: T[q][1], x0: Math.min(T[q][0], T[q - 1][0]), x1: Math.max(T[q][0], T[q - 1][0]) });
      best.paths.forEach(p => { for (let q = 1; q < p.length; q++) committed.push({ pid: P.id, a: p[q - 1], b: p[q] }); });
    });
    // every other trace of the board (radio networks, loose devices, whatever no bus line could be placed for): no diagonals
    const rects = Object.values(layout.tiles);
    routes.forEach(r => { if (!out.has(r.id)) { const q = rectify(r, rects); if (q !== r) out.set(r.id, q); } });
    return out.size ? routes.map(r => out.get(r.id) || r) : routes;
  }

  /* A routed trace without diagonals: every slanted piece (the chamfer of a corner or a long diagonal) becomes a corner, on the
     side that keeps the neighbouring pieces straight; a replacement that would run through a tile is not used (that diagonal stays). */
  function rectify(r, rects) {
    const pts = r.pts;
    if (!pts || pts.length < 2 || orthogonal(pts)) return r;
    const hit = (a, b) => {
      const x0 = Math.min(a[0], b[0]), x1 = Math.max(a[0], b[0]), y0 = Math.min(a[1], b[1]), y1 = Math.max(a[1], b[1]);
      return rects.some(t => t.id !== r.a && t.id !== r.b && x1 > t.x + 1 && x0 < t.x + t.w - 1 && y1 > t.y + 1 && y0 < t.y + t.h - 1);
    };
    const out = [pts[0].slice()];
    for (let i = 1; i < pts.length; i++) {
      const a = out[out.length - 1], b = pts[i];
      if (Math.abs(a[0] - b[0]) < 0.01 || Math.abs(a[1] - b[1]) < 0.01) { out.push(b.slice()); continue; }
      const c1 = [b[0], a[1]], c2 = [a[0], b[1]];                       // horizontal first | vertical first
      const prev = out.length > 1 ? out[out.length - 2] : null, next = i + 1 < pts.length ? pts[i + 1] : null;
      let first = prev && Math.abs(prev[1] - a[1]) < 0.01 ? 1 : prev && Math.abs(prev[0] - a[0]) < 0.01 ? 2 : next && Math.abs(next[0] - b[0]) < 0.01 ? 1 : next && Math.abs(next[1] - b[1]) < 0.01 ? 2 : 1;
      const order = first === 1 ? [c1, c2] : [c2, c1];
      const ok = order.find(c => !hit(a, c) && !hit(c, b));
      if (ok) out.push(ok.slice());
      out.push(b.slice());
    }
    const res = clean(out);
    return orthogonal(res) ? Object.assign({}, r, { pts: res }) : r;
  }

  return { regroup, busRoutes, prefixTo, FAR, REST };
});
