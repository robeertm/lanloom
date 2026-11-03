/* Lanloom — bundled traces (core module, API = 1): edge bundling on the circuit board.
   Several traces that arrive at the same target through the same port share one trunk; the dots of all members add up
   on it (few become many, and many become larger dots).

   Pure geometry (no DOM), so board, tests and measurements compute the same:
   - Traces that arrive at the same target at the same port (same tile, same kind/colour, same port) usually run side
     by side as a bus already (lanes on the grid pitch). These lanes are pulled together into ONE trunk: starting at the
     target, every segment of a member is laid onto its partner's line as long as both run parallel and close (≤ NEAR).
     Where the ways part, the junction sits (T branch on the trunk line); from there the feeder runs unchanged to its
     source. Everything stays orthogonal (with 45° chamfers like the router), shifts happen only inside the channel
     (tiles are checked) — no new detours, no rebuild.
   - Every edge keeps its own trace (feeder + trunk) and its own dots: byte conservation stays exact per edge, the dots
     add up on the trunk by themselves; a host can show the own share by dimming the other members.
   - Separate ports stay separate: the port key comes from the host view (switch port, radio/cable, VLAN, service port);
     null means: never bundle at this end. When in doubt, separate.

   Stable API (Lanloom.Bundles, API = 1):
     NEAR, axis(p, q), clean(pts), ortho(pts, maxChamfer), chamfer(pts, r), merge(M, L, free, near), fromTarget(pts, end)
     bundleAll(traces, o) → copies of the traces, bundled
       o = { keyOf(trace, end) → key | null, free(x0, y0, x1, y1) → bool, chamfer: px, near: px }
       every copy: pts/padA/padB/vias adjusted; bundles: [{key, partner, junction, shared}] on members, trunk: true on
       partners, members: number of traces on the busiest trunk the trace belongs to (≥ 1) */
(function (root, factory) {
  const B = factory();
  if (typeof module === 'object' && module.exports) module.exports = B;
  else {
    root.Lanloom = root.Lanloom || {};
    root.Lanloom.Bundles = B;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const API = 1;
  const NEAR = 9;         // largest lane distance that is still pulled together (map units; buses lie 2–7 px apart)
  const EPS = 0.05;

  const axis = (p, q) => Math.abs(p[1] - q[1]) < EPS ? 'h' : Math.abs(p[0] - q[0]) < EPS ? 'v' : 'd';

  // remove collinear and duplicate points
  function clean(pts) {
    const out = [];
    for (const q of pts) {
      if (out.length && Math.abs(out[out.length - 1][0] - q[0]) < EPS && Math.abs(out[out.length - 1][1] - q[1]) < EPS) continue;
      if (out.length >= 2) {
        const a = out[out.length - 2], b = out[out.length - 1];
        const l1 = axis(a, b), l2 = axis(b, q);
        if (l1 !== 'd' && l1 === l2) { out[out.length - 1] = q.slice(); continue; }
      }
      out.push(q.slice());
    }
    return out;
  }
  /* Remove chamfers: short diagonals between a horizontal and a vertical segment become a corner (long diagonals stay).
     → orthogonal line (apart from long diagonals) */
  function ortho(pts, maxChamfer) {
    const p = clean(pts);
    if (p.length < 4) return p;
    const out = [p[0].slice()];
    for (let i = 1; i < p.length; i++) {
      const a = p[i - 1], b = p[i];
      if (axis(a, b) === 'd' && i >= 2 && i < p.length - 1 && Math.hypot(b[0] - a[0], b[1] - a[1]) <= maxChamfer) {
        const before = axis(p[i - 2], a), after = axis(b, p[i + 1]);
        if (before === 'h' && after === 'v') { out[out.length - 1] = [b[0], a[1]]; continue; }
        if (before === 'v' && after === 'h') { out[out.length - 1] = [a[0], b[1]]; continue; }
      }
      out.push(b.slice());
    }
    return clean(out);
  }
  // Set chamfers (45°, length r per leg) — like the router (r = grid pitch) or a schematic view (r = 5)
  function chamfer(pts, r) {
    if (pts.length < 3) return pts.map(q => q.slice());
    const out = [pts[0].slice()];
    for (let i = 1; i < pts.length - 1; i++) {
      const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], [x2, y2] = pts[i + 1];
      if (axis(pts[i - 1], pts[i]) === 'd' || axis(pts[i], pts[i + 1]) === 'd') { out.push([x1, y1]); continue; }
      const l1 = Math.hypot(x1 - x0, y1 - y0), l2 = Math.hypot(x2 - x1, y2 - y1);
      const f = Math.min(r, l1 / 2, l2 / 2);
      if (f < 0.5) { out.push([x1, y1]); continue; }
      out.push([x1 - (x1 - x0) / l1 * f, y1 - (y1 - y0) / l1 * f], [x1 + (x2 - x1) / l2 * f, y1 + (y2 - y1) / l2 * f]);
    }
    out.push(pts[pts.length - 1].slice());
    return out.filter((q, i, l) => !i || Math.hypot(q[0] - l[i - 1][0], q[1] - l[i - 1][1]) > 0.01);
  }

  /* Lay M onto L. Both orthogonal, both start at the common target (index 0). free(x0, y0, x1, y1) checks whether a
     rectangle is free of foreign tiles. → { pts, shared (length of the common stretch), junction ([x, y]) } or null */
  function merge(M, L, free, near) {
    near = near || NEAR;
    const out = M.map(q => q.slice());
    // the last segment of M ends at its source (pad) — never shift it; L may be shared up to its end
    const n = Math.min(out.length - 2, L.length - 1);
    let shared = 0, i = 0, junction = null, last = -1, runAxis = 0;
    for (; i < n; i++) {
      const om = axis(out[i], out[i + 1]), ol = axis(L[i], L[i + 1]);
      if (om !== ol || om === 'd') break;
      const k = om === 'h' ? 1 : 0, a = 1 - k;                 // k: fixed coordinate of the segment, a: running axis
      // same running direction (not opposite)
      if (Math.sign(out[i + 1][a] - out[i][a]) !== Math.sign(L[i + 1][a] - L[i][a])) break;
      const d = L[i][k] - out[i][k];
      if (Math.abs(d) > near) break;
      // a diagonal following in M would no longer be 45° after the shift — do not shift here
      if (Math.abs(d) > EPS && i + 2 < out.length && axis(out[i + 1], out[i + 2]) === 'd') break;
      // the last segment of L runs into its source: share only if M turns off before (≥ 4 px) — never into the foreign tile
      if (i === L.length - 2) {
        const lo = Math.min(L[i][a], L[i + 1][a]), hi = Math.max(L[i][a], L[i + 1][a]), m = out[i + 1][a];
        if (!(m > lo && m < hi && Math.abs(m - L[i + 1][a]) >= 4)) break;
      }
      if (Math.abs(d) > EPS) {
        const lo = Math.min(out[i][a], out[i + 1][a]), hi = Math.max(out[i][a], out[i + 1][a]);
        const c0 = Math.min(out[i][k], L[i][k]), c1 = Math.max(out[i][k], L[i][k]);
        const ok = k === 1 ? free(lo, c0, hi, c1) : free(c0, lo, c1, hi);
        if (!ok) break;
      }
      out[i][k] = L[i][k]; out[i + 1][k] = L[i][k]; last = i; runAxis = a;
      // common length of this segment: overlap of both running ranges
      const ml = Math.min(out[i][a], out[i + 1][a]), mh = Math.max(out[i][a], out[i + 1][a]);
      const ll = Math.min(L[i][a], L[i + 1][a]), lh = Math.max(L[i][a], L[i + 1][a]);
      shared += Math.max(0, Math.min(mh, lh) - Math.max(ml, ll));
      // bend of M not at the bend of L → the ways part here (junction on the trunk line)
      if (Math.abs(out[i + 1][a] - L[i + 1][a]) > near || i + 1 >= L.length - 1) {
        junction = Math.abs(out[i + 1][a] - L[i + 1][a]) <= EPS ? out[i + 1].slice()
          : (Math.abs(out[i + 1][a] - out[i][a]) < Math.abs(L[i + 1][a] - out[i][a]) ? out[i + 1].slice() : L[i + 1].slice());
        i++;
        break;
      }
    }
    if (shared < 2) return null;
    if (!junction) {   // junction: the earlier of both bends (seen from the target) on the trunk line
      const j = last, a = runAxis;
      junction = Math.abs(out[j + 1][a] - out[j][a]) <= Math.abs(L[j + 1][a] - out[j][a]) ? out[j + 1].slice() : L[j + 1].slice();
    }
    return { pts: clean(out), shared, junction };
  }

  /* Align a trace (pts, target at the start = 'a' or at the end = 'b') to its target end: → orthogonal list from the target */
  function fromTarget(pts, end) { return end === 'a' ? pts : pts.slice().reverse(); }

  // ------------------------------------------------------------------ whole board
  const polyLength = pts => { let L = 0; for (let i = 1; i < pts.length; i++) L += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); return L; };
  function distToLine(p, pts) {
    let best = Infinity;
    for (let i = 1; i < pts.length; i++) {
      const [x1, y1] = pts[i - 1], [x2, y2] = pts[i], dx = x2 - x1, dy = y2 - y1, l2 = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, ((p[0] - x1) * dx + (p[1] - y1) * dy) / l2));
      best = Math.min(best, Math.hypot(x1 + t * dx - p[0], y1 + t * dy - p[1]));
    }
    return best;
  }

  /* Bundle all traces of a board: per group (same key at one end) the longest trace goes first (longest trunk), the others
     lay themselves onto their best partner. Works on shallow copies; the input stays untouched. */
  function bundleAll(traces, o) {
    o = o || {};
    const fa = o.chamfer || 2, near = o.near || NEAR, free = o.free || (() => true);
    const keyOf = (t, X) => (o.keyOf ? o.keyOf(t, X) : null);
    const list = traces.map(t => Object.assign({}, t));
    const orthoOf = t => { if (!t._o) t._o = ortho(t.pts, fa * 3.2); return t._o; };
    const lengthOf = t => (t._L == null ? (t._L = polyLength(t.pts)) : t._L);
    const set = (t, op) => {
      t._o = op; t.pts = chamfer(op, fa); t._L = null;
      if (t.padA) t.padA = Object.assign({}, t.padA, { x: t.pts[0][0], y: t.pts[0][1] });
      if (t.padB) t.padB = Object.assign({}, t.padB, { x: t.pts[t.pts.length - 1][0], y: t.pts[t.pts.length - 1][1] });
      if (t.vias && t.vias.length) t.vias = t.vias.filter(v => distToLine(v, t.pts) < 1.5);
    };
    // lay M (end X) onto the best partner of its group → true when shared
    const attach = (m, X, partners) => {
      const em = X === m.a ? 'a' : 'b';
      const Mo = fromTarget(orthoOf(m), em);
      let best = null;
      for (const p of partners) {
        if (p === m) continue;
        const ep = X === p.a ? 'a' : 'b';
        const r = merge(Mo, fromTarget(orthoOf(p), ep), free, near);
        if (r && (!best || r.shared > best.r.shared)) best = { r, p };
      }
      if (!best) return false;
      const np = em === 'a' ? best.r.pts : best.r.pts.slice().reverse();
      [m, best.p].forEach(t => { if (!t._norm) { t._norm = true; if (t !== m) set(t, t._o); } });
      set(m, np);
      // common length on the drawn line (with chamfers): from the target to the junction along the trace
      const from = em === 'a' ? m.pts : m.pts.slice().reverse();
      let run = 0, bestD = Infinity, bestS = best.r.shared;
      for (let i = 1; i < from.length; i++) {
        const [x1, y1] = from[i - 1], [x2, y2] = from[i], dx = x2 - x1, dy = y2 - y1, l = Math.hypot(dx, dy) || 1;
        const t = Math.max(0, Math.min(1, ((best.r.junction[0] - x1) * dx + (best.r.junction[1] - y1) * dy) / (l * l)));
        const d = Math.hypot(x1 + t * dx - best.r.junction[0], y1 + t * dy - best.r.junction[1]);
        if (d < bestD - 1e-6) { bestD = d; bestS = run + t * l; }
        run += l;
      }
      (m.bundles = m.bundles || []).push({ key: keyOf(m, X), partner: best.p.id, junction: best.r.junction, shared: Math.min(best.r.shared, bestS) });
      best.p.trunk = true;
      return true;
    };
    const groups = new Map();
    for (const t of list) {
      if (!t.pts || t.pts.length < 2) continue;
      for (const X of [t.a, t.b]) { const k = keyOf(t, X); if (!k) continue; if (!groups.has(k)) groups.set(k, { X, t: [] }); groups.get(k).t.push(t); }
    }
    const order = [...groups.values()].filter(g => g.t.length > 1).sort((p, q) => q.t.length - p.t.length);
    for (const g of order) {
      const members = g.t.slice().sort((p, q) => lengthOf(q) - lengthOf(p)), laid = [members[0]];
      for (const m of members.slice(1)) { attach(m, g.X, laid); laid.push(m); }
    }
    // members per trunk (dot sizes): trace + every trace that laid itself onto it or onto the same partners
    const n = new Map();
    for (const t of list) if (t.bundles) for (const s of t.bundles) n.set(s.key, (n.get(s.key) || 1) + 1);
    for (const t of list) {
      let m = 1;
      if (t.bundles) for (const s of t.bundles) m = Math.max(m, n.get(s.key) || 1);
      if (t.trunk) for (const X of [t.a, t.b]) { const k = keyOf(t, X); if (k) m = Math.max(m, n.get(k) || 1); }
      t.members = m;
      delete t._o; delete t._L; delete t._norm;
    }
    return list;
  }

  return { API, NEAR, axis, clean, ortho, chamfer, merge, fromTarget, bundleAll };
});
