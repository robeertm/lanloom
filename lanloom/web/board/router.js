/* Lanloom — circuit-board trace router (core module, API = 1).
   Routes traces like on a printed circuit board: horizontal/vertical runs with 45° chamfers, inside the channels
   between tiles (tiles are keep-out areas), parallel traces on a fixed grid pitch form buses, every trace starts and
   ends on a pad at a tile edge, crossings are marked as vias. Runs in a Web Worker (main path), in the window
   (fallback) and under Node (tests). No dependencies, no DOM.

   Stable API (Lanloom.Router, API = 1):
     VERSION, API, COST, KIND_RANK, DX, DY, DECAY_TAU
     Grid            new Grid({w, h, p, kl, cost, weight, smooth})
                       .tile(id, x, y, w, h, shape)  .rim(id, x, y, w, h, r)  .area(x, y, w, h, cost)  .line(x1, y1, x2, y2)
                       .route(net) → trace | null    .register(trace, net)    .padSites(node)  .reason
     Heap
     routeAll(spec, cache)        → {grid, traces, failed: [{id, reason}], ms}
     buildGrid(spec)              → Grid with tiles, rims, soft areas and frame lines of a spec
     buildNets(o)                 → {nets, order, data, sysNodes}
     tracesFor(from, to, sysNodes, traces) → [[trace, reversed]]
     kindFromText(text, rules, fallback)
     orderNets(nets, tiles)       fallbackRoute(net, A, B)       railRoutes(spec, o)       chamferPts(pts, r)
     offset(pts, d)  isOctilinear(pts, tol)  octilinear(a, b, diagonalFirst)  octilinearize(pts)  simplify(raw)  length(pts)
     decay(level, dt, tau)  refresh(level, n)  startWorker(scope)  hash(s)  cacheKey(spec)

   Data shapes:
     spec  = {w, h, p, kl, weight?, cost?, smooth?, tiles: [{id, x, y, w, h, shape?, r?}], soft: [{x, y, w, h, k?}],
              lines: [[x1, y1, x2, y2]], nets: [net]}
     net   = {id, a, b, kind, groupA?, groupB?, hardText?, avoid?: [[x, y, w, h]]}
             groupA/groupB: bundle key at end a/b; members of one group may share cells and pads (bundled traces)
     trace = {id, a, b, pts: [[x, y]], padA: {x, y, s, rim?}, padB, vias: [[x, y, dir]], length}
     shape 'rect' | 'pill' | 'rim' (rim node: a zone frame to dock on, no keep-out; pads sit on its frame line)
   Worker protocol:
     in:  {type:'route', uid, job, spec, cache, exclusive?} → {type:'part', uid, job, traces} … {type:'done', uid, job, traces, all, failed, ms}
          {type:'net', uid, job, net}                       → {type:'trace', uid, job, net, trace}
          {type:'forget', uid}
     err: {type:'error', uid, job, text}
     exclusive: drop every other grid of the worker first (one board per worker keeps memory bounded) */
(function (root, factory) {
  const R = factory();
  if (typeof module === 'object' && module.exports) module.exports = R;
  else {
    root.Lanloom = root.Lanloom || {};
    root.Lanloom.Router = R;
  }
  // Inside a worker: accept messages
  if (typeof WorkerGlobalScope !== 'undefined' && typeof self !== 'undefined' && self instanceof WorkerGlobalScope) R.startWorker(self);
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const API = 1;
  const VERSION = 6;            // part of the cache key: when the router changes, everything is routed again (5: rim nodes, 6: bundled traces)
  // Directions: 0=E 1=SE 2=S 3=SW 4=W 5=NW 6=N 7=NE
  const DX = [1, 1, 0, -1, -1, -1, 0, 1], DY = [0, 1, 1, 1, 0, -1, -1, -1];
  const SIDE_OUT = { r: 0, b: 2, l: 4, t: 6 };
  const SQ2 = Math.SQRT2;

  // Costs (in grid steps)
  const COST = {
    bend: 0.55,         // a 45° bend
    diag: 1.25,         // diagonals are slightly more expensive (traces stay mostly horizontal/vertical)
    foreign: 16,        // running on top of another trace (overlap — practically forbidden)
    cross: 2.5,         // crossing another trace (on a board: through vias onto the other layer)
    ring: 2.2,          // running close along foreign tiles
    soft: 7,            // running over labels (text)
    frame: 1.4,         // running along a frame line
    neighbour: -0.12,   // next to another trace (bus bundling)
    weight: 1.12,       // A* weighting (slightly greedy, much faster)
    rimBand: 3,         // rim nodes: pads up to 3 cells (6 px) beside the frame line
    rimOffset: 0.6,     // … a small surcharge per cell of distance to the line
    maxSteps: 600000,
    share: 0.55         // bundled traces: running on the trace of a group member (same target, same port) — cheaper than free
  };
  // Trace kinds in routing order (orderNets): backbone first, so it gets the best channels
  const KIND_RANK = { uplink: 0, lan: 1, integration: 2, service: 3, flow: 4 };

  // ------------------------------------------------------------------ binary heap (f, state)
  class Heap {
    constructor() { this.f = new Float64Array(1024); this.s = new Int32Array(1024); this.n = 0; }
    empty() { return this.n === 0; }
    push(f, s) {
      if (this.n === this.f.length) { const f2 = new Float64Array(this.n * 2); f2.set(this.f); this.f = f2; const s2 = new Int32Array(this.n * 2); s2.set(this.s); this.s = s2; }
      let i = this.n++;
      while (i > 0) { const p = (i - 1) >> 1; if (this.f[p] <= f) break; this.f[i] = this.f[p]; this.s[i] = this.s[p]; i = p; }
      this.f[i] = f; this.s[i] = s;
    }
    pop() {
      const s = this.s[0], n = --this.n;
      if (n > 0) {
        const f = this.f[n], x = this.s[n]; let i = 0;
        for (;;) {
          let c = 2 * i + 1; if (c >= n) break;
          if (c + 1 < n && this.f[c + 1] < this.f[c]) c++;
          if (this.f[c] >= f) break;
          this.f[i] = this.f[c]; this.s[i] = this.s[c]; i = c;
        }
        this.f[i] = f; this.s[i] = x;
      }
      return s;
    }
  }

  // ------------------------------------------------------------------ grid
  class Grid {
    constructor(o) {
      this.p = o.p || 3; this.kl = o.kl == null ? 1.8 : o.kl;
      this.K = Object.assign({}, COST, o.cost || {}); if (o.weight) this.K.weight = o.weight;   // tuning hook (defaults: COST)
      this.smoothing = !!o.smooth;                                                          // optional straightening pass (off by default)
      this.w = o.w; this.h = o.h;
      this.nx = Math.floor(o.w / this.p) + 1; this.ny = Math.floor(o.h / this.p) + 1;
      const N = this.N = this.nx * this.ny;
      this.blocked = new Uint8Array(N);      // 1 = tile (hard)
      this.soft = new Float32Array(N);       // extra cost (text, frames, tile proximity)
      this.used = new Int32Array(N);         // net index + 1
      this.usedDir = new Int8Array(N).fill(-1);
      this.diag = new Int32Array(N * 2);     // used diagonals per 2×2 block (\ and /)
      this.pad = new Uint8Array(N);          // 1 = pad (hard for others), 2 = pad neighbour (no new pad)
      this.text = new Uint8Array(N);         // 1 = label (blocked for live traces with hardText)
      this.ringOf = new Int32Array(N);       // tile index + 1 whose edge the cell lies on
      this.nodes = new Map(); this.nodeList = [];
      this.nets = [];
      this.groups = new Map();               // bundled traces: group key → Set(cells) of all members
      this.padGroup = new Map();             // pad cell → group key (members may use the same pad)
      // A* memory (state = cell·8 + direction)
      this.g = new Float32Array(N * 8); this.gen = new Uint32Array(N * 8); this.prev = new Int32Array(N * 8);
      this.goalGen = new Uint32Array(N); this.goalPad = new Int32Array(N);
      this.run = 0;
      for (let i = 0; i < this.nx; i++) { this.blocked[i] = 1; this.blocked[(this.ny - 1) * this.nx + i] = 1; }
      for (let j = 0; j < this.ny; j++) { this.blocked[j * this.nx] = 1; this.blocked[j * this.nx + this.nx - 1] = 1; }
    }
    cell(i, j) { return j * this.nx + i; }
    xy(c) { return [(c % this.nx) * this.p, Math.floor(c / this.nx) * this.p]; }

    // Tile: interior (plus clearance) is blocked, the first free ring of cells around it are pad sites
    tile(id, x, y, w, h, shape) {
      const p = this.p, kl = this.kl, K = this.K, k = { id, x, y, w, h, shape: shape || 'rect', idx: this.nodeList.length };
      this.nodes.set(id, k); this.nodeList.push(k);
      const i0 = Math.max(0, Math.ceil((x - kl) / p)), i1 = Math.min(this.nx - 1, Math.floor((x + w + kl) / p));
      const j0 = Math.max(0, Math.ceil((y - kl) / p)), j1 = Math.min(this.ny - 1, Math.floor((y + h + kl) / p));
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) this.blocked[j * this.nx + i] = 1;
      // ring + proximity cost (two cells wide)
      for (let j = j0 - 2; j <= j1 + 2; j++) for (let i = i0 - 2; i <= i1 + 2; i++) {
        if (i < 0 || j < 0 || i >= this.nx || j >= this.ny) continue;
        const c = j * this.nx + i; if (this.blocked[c]) continue;
        const near = (i >= i0 - 1 && i <= i1 + 1 && j >= j0 - 1 && j <= j1 + 1);
        if (near) { if (!this.ringOf[c]) this.ringOf[c] = k.idx + 1; this.soft[c] += K.ring; }
        else this.soft[c] += K.ring * 0.25;
      }
      k.i0 = i0; k.i1 = i1; k.j0 = j0; k.j1 = j1;
      return k;
    }
    // Rim node: a zone as a docking area without keep-out — pads sit on its frame line (without the rounded corners),
    // the trace leaves the pad inwards or outwards. So a trace of "the host itself" starts on the frame of its zone
    // where the way to the target is shortest, instead of running around the zone to a pad at its title.
    rim(id, x, y, w, h, r) {
      const k = { id, x, y, w, h, shape: 'rim', r: r == null ? 14 : r, idx: this.nodeList.length, rim: true };
      this.nodes.set(id, k); this.nodeList.push(k);
      return k;
    }
    // Pad sites of a rim node: a band of ±BAND cells around the frame line (buses of other traces often run on the line
    // itself), two start directions per cell across the frame (out and in). g0 = small surcharge per cell of distance
    // to the line, so the pad sits on the frame where possible.
    rimSites(k) {
      const p = this.p, K = this.K, out = [], corner = k.r + p, BAND = K.rimBand;
      const add = (i, j, side, dir, o) => {
        if (i < 1 || j < 1 || i >= this.nx - 1 || j >= this.ny - 1) return;
        const c = j * this.nx + i; if (this.blocked[c]) return;
        const g0 = Math.abs(o) * K.rimOffset;
        out.push({ c, side, out: dir, edge: [i * p, j * p], rimPad: true, g0 });
        out.push({ c, side, out: (dir + 4) & 7, edge: [i * p, j * p], rimPad: true, g0 });
      };
      const io = Math.round(k.x / p), ir = Math.round((k.x + k.w) / p), jo = Math.round(k.y / p), ju = Math.round((k.y + k.h) / p);
      for (let o = -BAND; o <= BAND; o++) {
        for (let i = io; i <= ir; i++) {
          const x = i * p; if (x < k.x + corner || x > k.x + k.w - corner) continue;
          add(i, jo + o, 't', SIDE_OUT.t, o); add(i, ju + o, 'b', SIDE_OUT.b, o);
        }
        for (let j = jo; j <= ju; j++) {
          const y = j * p; if (y < k.y + corner || y > k.y + k.h - corner) continue;
          add(io + o, j, 'l', SIDE_OUT.l, o); add(ir + o, j, 'r', SIDE_OUT.r, o);
        }
      }
      return out;
    }
    // soft area (label): may be crossed, but is avoided
    area(x, y, w, h, cost) {
      const p = this.p;
      const i0 = Math.max(0, Math.floor(x / p)), i1 = Math.min(this.nx - 1, Math.ceil((x + w) / p));
      const j0 = Math.max(0, Math.floor(y / p)), j1 = Math.min(this.ny - 1, Math.ceil((y + h) / p));
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) { this.soft[j * this.nx + i] += cost == null ? this.K.soft : cost; if (cost == null) this.text[j * this.nx + i] = 1; }
    }
    // frame line (horizontal or vertical): running along costs, crossing hardly
    line(x1, y1, x2, y2) {
      const p = this.p;
      if (Math.abs(y1 - y2) < 0.5) {
        const j = Math.round(y1 / p); if (j < 0 || j >= this.ny) return;
        for (let i = Math.max(0, Math.floor(Math.min(x1, x2) / p)); i <= Math.min(this.nx - 1, Math.ceil(Math.max(x1, x2) / p)); i++) this.soft[j * this.nx + i] += this.K.frame;
      } else if (Math.abs(x1 - x2) < 0.5) {
        const i = Math.round(x1 / p); if (i < 0 || i >= this.nx) return;
        for (let j = Math.max(0, Math.floor(Math.min(y1, y2) / p)); j <= Math.min(this.ny - 1, Math.ceil(Math.max(y1, y2) / p)); j++) this.soft[j * this.nx + i] += this.K.frame;
      }
    }

    // Pad sites of a tile: [{c, side, out, edge:[x,y]}]
    padSites(k) {
      if (k._sites) return k._sites;
      if (k.rim) return (k._sites = this.rimSites(k));
      const p = this.p, out = [];
      const pill = k.shape === 'pill';
      const corner = pill ? k.h / 2 : Math.min(8, k.w / 4);
      const cornerV = pill ? k.h / 2 - 0.5 : Math.min(6, k.h / 3);
      const add = (i, j, side, ex, ey) => {
        if (i < 0 || j < 0 || i >= this.nx || j >= this.ny) return;
        const c = j * this.nx + i; if (this.blocked[c]) return;
        out.push({ c, side, out: SIDE_OUT[side], edge: [ex, ey] });
      };
      const jt = k.j0 - 1, jb = k.j1 + 1, il = k.i0 - 1, ir = k.i1 + 1;
      for (let i = k.i0; i <= k.i1; i++) {
        const x = i * p; if (x < k.x + corner || x > k.x + k.w - corner) continue;
        add(i, jt, 't', x, k.y); add(i, jb, 'b', x, k.y + k.h);
      }
      for (let j = k.j0; j <= k.j1; j++) {
        const y = j * p;
        if (pill) { if (Math.abs(y - (k.y + k.h / 2)) > p * 0.51) continue; }
        else if (y < k.y + cornerV || y > k.y + k.h - cornerV) continue;
        add(il, j, 'l', k.x, y); add(ir, j, 'r', k.x + k.w, y);
      }
      k._sites = out;
      return out;
    }

    // ------------------------------------------------------------------ route one trace
    // net = { id, a, b (tile ids), kind }  →  { id, a, b, pts, padA, padB, vias, length } | null
    route(net) {
      // rim node as target: route the other way round (start with all rim pads, A* picks the shortest), turn the result back
      const A = this.nodes.get(net.a), B = this.nodes.get(net.b);
      if (B && B.rim && A && !A.rim) {
        const r = this.route({ id: net.id, a: net.b, b: net.a, kind: net.kind, avoid: net.avoid, groupA: net.groupB, groupB: net.groupA });
        if (!r) return null;
        Object.assign(r, { a: net.a, b: net.b, pts: r.pts.slice().reverse(), padA: r.padB, padB: r.padA, vias: r.vias });
        return r;
      }
      // Avoided areas (e.g. foreign zones a trace must not enter): first a path that enters neither them nor any label —
      // otherwise the trace would run straight through another zone and look as if it belonged there
      if (net.avoid && net.avoid.length) {
        const old = this.text, p = this.p, m = old.slice();
        for (const [x, y, w, h] of net.avoid) {
          const i0 = Math.max(0, Math.floor(x / p)), i1 = Math.min(this.nx - 1, Math.ceil((x + w) / p));
          const j0 = Math.max(0, Math.floor(y / p)), j1 = Math.min(this.ny - 1, Math.ceil((y + h) / p));
          for (let j = j0; j <= j1; j++) m.fill(1, j * this.nx + i0, j * this.nx + i1 + 1);
        }
        this.text = m;
        let r0 = null;
        try { r0 = this._route(net, false, true); } finally { this.text = old; }
        if (r0) return r0;
      }
      // live traces (hardText): first without crossing any label, otherwise like all others
      const r = (net.hardText && this._route(net, false, true)) || this._route(net, false);
      return r || this._route(net, true);
    }
    _route(net, loose, hard) {
      const A = this.nodes.get(net.a), B = this.nodes.get(net.b);
      if (!A || !B || A === B) return null;
      const K = this.K;
      const ni = this.nets.length + 1;
      const nx = this.nx, g = this.g, gen = this.gen, prev = this.prev;
      const run = ++this.run;
      if (run >= 0xFFFFFFF0) { this.gen.fill(0); this.goalGen.fill(0); this.run = 1; }
      const L = this.run;
      const heap = new Heap();
      const kForeign = loose ? K.foreign * 0.4 : K.foreign, hWeight = loose ? 1.6 : 1, text = hard ? this.text : null;
      // bundled traces: cells of the own groups (at target b or start a) may be shared, their pads as well
      const gA = net.groupA ? this.groups.get(net.groupA) : null, gB = net.groupB ? this.groups.get(net.groupB) : null;
      const shareable = (gA || gB) ? (c => (gA && gA.has(c)) || (gB && gB.has(c))) : null;
      // Goals: the cell IN FRONT OF a free pad of B, arriving perpendicular (± 45°)
      let gi0 = Infinity, gi1 = -Infinity, gj0 = Infinity, gj1 = -Infinity, goals = 0;
      for (const q of this.padSites(B)) {
        const shared = net.groupB && this.padGroup.get(q.c) === net.groupB;
        if (!shared && (this.pad[q.c] || this.used[q.c])) continue;
        const inward = (q.out + 4) & 7;
        const before = q.c - DX[inward] - DY[inward] * nx;   // one cell outside the pad
        if (before < 0 || before >= this.N || this.blocked[before] || (this.pad[before] === 1 && !shared) || (text && (text[before] || text[q.c]))) continue;
        this.goalGen[before] = L; this.goalPad[before] = q.c * 8 + inward;
        const i = before % nx, j = (before / nx) | 0;
        gi0 = Math.min(gi0, i); gi1 = Math.max(gi1, i); gj0 = Math.min(gj0, j); gj1 = Math.max(gj1, j); goals++;
      }
      if (!goals) { this.reason = 'goal without pad'; return null; }
      const hz = c => { const i = c % nx, j = (c / nx) | 0; const dx = Math.max(0, gi0 - i, i - gi1), dy = Math.max(0, gj0 - j, j - gj1); return (dx + dy + (SQ2 - 2) * Math.min(dx, dy)) * K.weight * hWeight; };
      // Starts: free pads of A, first step straight out
      let starts = 0;
      for (const q of this.padSites(A)) {
        const shared = net.groupA && this.padGroup.get(q.c) === net.groupA;
        if (!shared && (this.pad[q.c] || this.used[q.c])) continue;
        const d = q.out, c1 = q.c + DX[d] + DY[d] * nx;
        if (c1 < 0 || c1 >= this.N || this.blocked[c1] || (this.pad[c1] === 1 && !shared) || (text && (text[c1] || text[q.c]))) continue;
        const s0 = q.c * 8 + d, s1 = c1 * 8 + d;
        const k1 = 1 + (q.g0 || 0) + this.soft[c1] + (this.used[c1] && !(shareable && shareable(c1)) ? K.foreign : 0);
        if (gen[s1] === L && g[s1] <= k1) continue;   // rim node: the same first cell already cheaper from another pad
        gen[s0] = L; g[s0] = 0; prev[s0] = -1;
        gen[s1] = L; g[s1] = k1; prev[s1] = s0;
        heap.push(k1 + hz(c1), s1); starts++;
      }
      if (!starts) { this.reason = 'start without pad'; return null; }
      let end = -1, steps = 0; const limit = loose ? this.N * 8 : Math.max(K.maxSteps, this.N * 3);
      while (!heap.empty()) {
        const s = heap.pop(), c = s >> 3, d = s & 7;
        if (++steps > limit) break;
        const gs = g[s];
        if (this.goalGen[c] === L) {
          const gp = this.goalPad[c], inward = gp & 7;
          if (d === inward || d === ((inward + 1) & 7) || d === ((inward + 7) & 7)) { end = s; break; }
        }
        const i = c % nx, j = (c / nx) | 0;
        for (let t = -1; t <= 1; t++) {
          const nd = (d + t + 8) & 7;
          const ii = i + DX[nd], jj = j + DY[nd];
          if (ii < 0 || jj < 0 || ii >= nx || jj >= this.ny) continue;
          const n = jj * nx + ii;
          if (this.blocked[n] || (this.pad[n] === 1 && !(shareable && shareable(n))) || (text && text[n])) continue;
          const diagonal = nd & 1;
          // no diagonal between two blocked cells (never cut a corner)
          if (diagonal && (this.blocked[j * nx + ii] || this.blocked[jj * nx + i] || (text && (text[j * nx + ii] || text[jj * nx + i])))) continue;
          let k = (diagonal ? SQ2 * K.diag : 1) * (1 + this.soft[n]);
          if (t) k += K.bend;
          const un = this.used[n];
          if (un && un !== ni) {
            const parallel = (this.usedDir[n] & 3) === (nd & 3);
            if (parallel && shareable && shareable(n)) k *= K.share;      // trunk of the own group: run on it instead of beside it
            else k += parallel ? kForeign : K.cross;
          } else if (this.besideTrace(ii, jj)) k += K.neighbour;
          if (diagonal) {
            // crossing a foreign diagonal inside the same 2×2 block?
            const bi = Math.min(i, ii), bj = Math.min(j, jj), bc = bj * nx + bi;
            const type = (DX[nd] === DY[nd]) ? 0 : 1;   // 0 = \ , 1 = /
            if (this.diag[bc * 2 + (1 - type)]) k += K.cross;
          }
          const ns = n * 8 + nd, gn = gs + k;
          if (gen[ns] === L && g[ns] <= gn) continue;
          gen[ns] = L; g[ns] = gn; prev[ns] = s;
          heap.push(gn + hz(n), ns);
        }
      }
      if (end < 0) { this.reason = steps > limit ? 'too far' : 'no path'; return null; }
      // trace the path back
      const gp = this.goalPad[end >> 3];
      const states = [gp];
      for (let s = end; s >= 0; s = prev[s]) states.push(s);
      states.reverse();
      let cells = states.map(s => s >> 3), dirs = states.map(s => s & 7);
      if (this.smoothing) {
        cells = this.smooth(cells, ni);
        dirs = cells.map((c, x) => x ? dirOf(cells[x - 1], c, nx) : (states[0] & 7));
      }
      const padAc = cells[0], padBc = cells[cells.length - 1];
      const qa = this.padSites(A).find(q => q.c === padAc), qb = this.padSites(B).find(q => q.c === padBc);
      // occupy cells + collect crossings
      const cross = [];
      for (let x = 0; x < cells.length; x++) {
        const c = cells[x], u = this.used[c];
        if (u && u !== ni) {
          const fd = this.usedDir[c];
          if (fd >= 0 && (fd & 3) !== (dirs[x] & 3) && !(shareable && shareable(c))) cross.push(x);
        } else if (!u) { this.used[c] = ni; this.usedDir[c] = dirs[x]; }
        if (x > 0 && (dirs[x] & 1)) {
          const c0 = cells[x - 1], i0 = c0 % nx, j0 = (c0 / nx) | 0, i1 = c % nx, j1 = (c / nx) | 0;
          const bc = Math.min(j0, j1) * nx + Math.min(i0, i1), type = (DX[dirs[x]] === DY[dirs[x]]) ? 0 : 1;
          if (this.diag[bc * 2 + (1 - type)] && this.diag[bc * 2 + (1 - type)] !== ni) cross.push(x - 0.5);
          this.diag[bc * 2 + type] = ni;
        }
      }
      // bundled traces: assign the cells and pads of the trace to its groups
      this.addToGroups(net, cells, padAc, padBc);
      // reserve pads (+ neighbours along the side, so pads keep a distance)
      for (const [pc, q] of [[padAc, qa], [padBc, qb]]) this.reservePad(pc, q);
      // points: edge → pad → … → pad → edge, merge collinear points
      const raw = [qa.edge.slice()].concat(cells.map(c => this.xy(c))).concat([qb.edge.slice()]);
      const pts = simplify(raw);
      const vias = cross.map(x => {
        const a = this.xy(cells[Math.floor(x)]), b = this.xy(cells[Math.ceil(x)]);
        return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, dirs[Math.ceil(x)]];
      });
      const r = { id: net.id, a: net.a, b: net.b, pts, padA: padInfo(qa), padB: padInfo(qb), vias, length: length(pts) };
      this.nets.push(r);
      return r;
    }
    addToGroups(net, cells, padAc, padBc) {
      for (const [key, pc] of [[net.groupA, padAc], [net.groupB, padBc]]) {
        if (!key) continue;
        let g = this.groups.get(key); if (!g) { g = new Set(); this.groups.set(key, g); }
        for (const c of cells) g.add(c);
        if (pc != null && !this.padGroup.has(pc)) this.padGroup.set(pc, key);
      }
    }
    reservePad(pc, q) {
      this.pad[pc] = 1;
      const across = (q.side === 't' || q.side === 'b') ? 1 : this.nx;
      for (const nb of [pc - across, pc + across]) if (nb >= 0 && nb < this.N && !this.pad[nb]) this.pad[nb] = 2;
    }
    besideTrace(i, j) {
      const nx = this.nx, u = this.used;
      return (i > 0 && u[j * nx + i - 1]) || (i < nx - 1 && u[j * nx + i + 1]) || (j > 0 && u[(j - 1) * nx + i]) || (j < this.ny - 1 && u[(j + 1) * nx + i]);
    }
    // Optional (Grid option smooth): pull the path straight — replace stretches with one or two octilinear segments where
    // the cells are free, not more expensive and the stretch had more bends (removes stair-steps a greedy search leaves).
    smooth(cells, ni) {
      const n = cells.length;
      if (n < 6) return cells;
      const nx = this.nx, LOOK = 90;
      const dirs = new Int8Array(n), bends = new Int32Array(n);
      for (let x = 1; x < n; x++) dirs[x] = dirOf(cells[x - 1], cells[x], nx);
      for (let x = 2; x < n; x++) bends[x] = bends[x - 1] + (dirs[x] !== dirs[x - 1] ? 1 : 0);
      const out = [cells[0], cells[1]];
      const last = n - 2;   // cells[last] = in front of pad B (fixed), cells[n-1] = pad B
      const maxSoft = new Float32Array(LOOK + 2);
      let i = 1;
      while (i < last) {
        const top = Math.min(last, i + LOOK);
        let m = 0;
        for (let j = i; j <= top; j++) { m = Math.max(m, this.soft[cells[j]]); maxSoft[j - i] = m; }
        let moved = false;
        for (let j = top; j > i + 1; j--) {
          const had = bends[j] - bends[i + 1];
          if (had < 1) continue;
          const seg = this.straightCells(cells[i], cells[j], maxSoft[j - i] + 0.01, ni, had);
          if (seg) { for (const c of seg) out.push(c); i = j; moved = true; break; }
        }
        if (!moved) { out.push(cells[i + 1]); i++; }
      }
      out.push(cells[n - 1]);
      return out;
    }
    // cells from a (exclusive) to b (inclusive) along one or two octilinear segments with fewer than `had` bends, or null
    straightCells(a, b, softMax, ni, had) {
      const nx = this.nx, ai = a % nx, aj = (a / nx) | 0, bi = b % nx, bj = (b / nx) | 0;
      const dx = bi - ai, dy = bj - aj, adx = Math.abs(dx), ady = Math.abs(dy);
      const sx = Math.sign(dx), sy = Math.sign(dy);
      const variants = [];
      if (!dx || !dy || adx === ady) variants.push([[bi, bj]]);
      else if (had > 1) {
        const m = Math.min(adx, ady);
        variants.push([[ai + sx * m, aj + sy * m], [bi, bj]]);     // diagonal first
        variants.push([[bi - sx * m, bj - sy * m], [bi, bj]]);     // straight first
      }
      for (const v of variants) {
        const out = [];
        let ci = ai, cj = aj, ok = true;
        for (const [ti, tj] of v) {
          const si = Math.sign(ti - ci), sj = Math.sign(tj - cj);
          while ((ci !== ti || cj !== tj) && ok) {
            const ni2 = ci + si, nj2 = cj + sj, c = nj2 * nx + ni2;
            if (this.blocked[c] || this.pad[c] === 1 || (this.used[c] && this.used[c] !== ni) || this.soft[c] > softMax) ok = false;
            else if (si && sj && (this.blocked[cj * nx + ni2] || this.blocked[nj2 * nx + ci])) ok = false;
            else if (si && sj) { const bc = Math.min(cj, nj2) * nx + Math.min(ci, ni2), type = si === sj ? 0 : 1; if (this.diag[bc * 2 + (1 - type)]) ok = false; }
            ci = ni2; cj = nj2;
            if (ok) out.push(c);
          }
          if (!ok) break;
        }
        if (ok) return out;
      }
      return null;
    }

    // Register an already known trace (from the cache), so later traces avoid it
    register(r, net) {
      const ni = this.nets.length + 1, p = this.p, nx = this.nx;
      const pts = r.pts, cellsG = [];
      for (let x = 1; x < pts.length; x++) {
        const [x0, y0] = pts[x - 1], [x1, y1] = pts[x];
        const i0 = Math.round(x0 / p), j0 = Math.round(y0 / p), i1 = Math.round(x1 / p), j1 = Math.round(y1 / p);
        const n = Math.max(Math.abs(i1 - i0), Math.abs(j1 - j0));
        const si = Math.sign(i1 - i0), sj = Math.sign(j1 - j0);
        const d = DX.findIndex((v, k) => v === si && DY[k] === sj);
        for (let s = 0; s <= n; s++) {
          const i = i0 + si * s, j = j0 + sj * s; if (i < 0 || j < 0 || i >= nx || j >= this.ny) continue;
          const c = j * nx + i; if (this.blocked[c]) continue;
          cellsG.push(c);
          if (!this.used[c]) { this.used[c] = ni; this.usedDir[c] = d < 0 ? 0 : d; }
          if (s > 0 && si && sj) { const bc = Math.min(j, j - sj) * nx + Math.min(i, i - si); this.diag[bc * 2 + (si === sj ? 0 : 1)] = ni; }
        }
      }
      const padC = [null, null];
      for (const pd of [r.padA, r.padB]) {
        const k = this.nodes.get(pd === r.padA ? r.a : r.b); if (!k) continue;
        const q = this.padSites(k).find(q => q.side === pd.s && Math.abs(q.edge[0] - pd.x) < 0.01 && Math.abs(q.edge[1] - pd.y) < 0.01);
        if (!q) continue;
        padC[pd === r.padA ? 0 : 1] = q.c;
        this.reservePad(q.c, q);
      }
      if (net && (net.groupA || net.groupB)) this.addToGroups(net, cellsG, padC[0], padC[1]);
      this.nets.push(r);
    }
  }

  function padInfo(q) { const o = { x: q.edge[0], y: q.edge[1], s: q.side }; if (q.rimPad) o.rim = true; return o; }
  function simplify(raw) {
    const pts = [];
    for (const q of raw) {
      const l = pts[pts.length - 1];
      if (l && Math.abs(l[0] - q[0]) < 1e-6 && Math.abs(l[1] - q[1]) < 1e-6) continue;
      if (pts.length >= 2) {
        const a = pts[pts.length - 2], b = l;
        const c1 = (b[0] - a[0]) * (q[1] - b[1]) - (b[1] - a[1]) * (q[0] - b[0]);
        const same = (b[0] - a[0]) * (q[0] - b[0]) + (b[1] - a[1]) * (q[1] - b[1]) > 0;
        if (Math.abs(c1) < 1e-6 && same) { pts[pts.length - 1] = q.slice(); continue; }
      }
      pts.push(q.slice());
    }
    return pts.map(q => [Math.round(q[0] * 100) / 100, Math.round(q[1] * 100) / 100]);
  }
  function dirOf(c0, c1, nx) {
    const di = (c1 % nx) - (c0 % nx), dj = ((c1 / nx) | 0) - ((c0 / nx) | 0);
    for (let k = 0; k < 8; k++) if (DX[k] === Math.sign(di) && DY[k] === Math.sign(dj)) return k;
    return 0;
  }
  function length(pts) { let L = 0; for (let i = 1; i < pts.length; i++) L += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); return L; }

  // Offset a polyline sideways (two lanes on one trace); corners via the intersection of the offset segments
  function offset(pts, d) {
    if (pts.length < 2) return pts.slice();
    const n = [];
    for (let i = 1; i < pts.length; i++) {
      const dx = pts[i][0] - pts[i - 1][0], dy = pts[i][1] - pts[i - 1][1], l = Math.hypot(dx, dy) || 1;
      n.push([-dy / l * d, dx / l * d]);
    }
    const out = [[pts[0][0] + n[0][0], pts[0][1] + n[0][1]]];
    for (let i = 1; i < pts.length - 1; i++) {
      const a = n[i - 1], b = n[i];
      const mx = a[0] + b[0], my = a[1] + b[1], ml = mx * a[0] + my * a[1];
      const f = ml ? (d * d) / ml : 1;
      out.push([pts[i][0] + mx * f, pts[i][1] + my * f]);
    }
    const l = n[n.length - 1];
    out.push([pts[pts.length - 1][0] + l[0], pts[pts.length - 1][1] + l[1]]);
    return out;
  }

  // Every segment only 0/45/90°? (tests and as a guard)
  function isOctilinear(pts, tol) {
    tol = tol == null ? 0.02 : tol;
    for (let i = 1; i < pts.length; i++) {
      const dx = Math.abs(pts[i][0] - pts[i - 1][0]), dy = Math.abs(pts[i][1] - pts[i - 1][1]);
      if (dx < tol || dy < tol) continue;
      if (Math.abs(dx - dy) <= tol * Math.max(1, dx)) continue;
      return false;
    }
    return true;
  }
  // Any segment as diagonal + straight (octilinear)
  function octilinear(a, b, diagonalFirst) {
    const dx = b[0] - a[0], dy = b[1] - a[1], m = Math.min(Math.abs(dx), Math.abs(dy));
    if (m < 0.01 || Math.abs(Math.abs(dx) - Math.abs(dy)) < 0.01) return [a.slice(), b.slice()];
    const sx = Math.sign(dx), sy = Math.sign(dy);
    if (diagonalFirst) return [a.slice(), [a[0] + sx * m, a[1] + sy * m], b.slice()];
    return [a.slice(), [b[0] - sx * m, b[1] - sy * m], b.slice()];
  }
  // Make a polyline octilinear (segments at other angles get a bend)
  function octilinearize(pts) {
    const out = [pts[0].slice()];
    for (let i = 1; i < pts.length; i++) {
      const a = out[out.length - 1], b = pts[i];
      if (isOctilinear([a, b], 0.05)) out.push(b.slice());
      else octilinear(a, b, false).slice(1).forEach(q => out.push(q));
    }
    return simplify(out);
  }

  // Simple fallback geometry when the router finds no path: leave tile A on the side facing B, enter B on the
  // side facing A, 1–3 octilinear segments. Always produces a drawable trace.
  function fallbackRoute(net, A, B) {
    const ca = [A.x + A.w / 2, A.y + A.h / 2], cb = [B.x + B.w / 2, B.y + B.h / 2];
    const dx = cb[0] - ca[0], dy = cb[1] - ca[1];
    let pa, pb, sa, sb;
    if (Math.abs(dy) * A.w > Math.abs(dx) * A.h) {   // mostly vertical
      sa = dy > 0 ? 'b' : 't'; sb = dy > 0 ? 't' : 'b';
      pa = [ca[0], dy > 0 ? A.y + A.h : A.y]; pb = [cb[0], dy > 0 ? B.y : B.y + B.h];
      const midY = (pa[1] + pb[1]) / 2;
      const pts = simplify([pa, [pa[0], midY], [pb[0], midY], pb]);
      return { id: net.id, a: net.a, b: net.b, pts, padA: { x: pa[0], y: pa[1], s: sa }, padB: { x: pb[0], y: pb[1], s: sb }, vias: [], length: length(pts), fallback: true };
    }
    sa = dx > 0 ? 'r' : 'l'; sb = dx > 0 ? 'l' : 'r';
    pa = [dx > 0 ? A.x + A.w : A.x, ca[1]]; pb = [dx > 0 ? B.x : B.x + B.w, cb[1]];
    const midX = (pa[0] + pb[0]) / 2;
    const pts = simplify([pa, [midX, pa[1]], [midX, pb[1]], pb]);
    return { id: net.id, a: net.a, b: net.b, pts, padA: { x: pa[0], y: pa[1], s: sa }, padB: { x: pb[0], y: pb[1], s: sb }, vias: [], length: length(pts), fallback: true };
  }

  // ------------------------------------------------------------------ rail routing (compact / phone layout)
  // Tiles stand in columns; every trace leaves its tile sideways, runs vertically on a lane in the margin or gutter
  // next to the column(s) and enters the other tile sideways: 1–3 straight segments with 45° chamfers. Lanes are
  // assigned like intervals on a track (no two vertical runs share a lane where they overlap, as long as lanes last).
  function railRoutes(spec, o) {
    o = o || {};
    const pitch = o.pitch || 3, chamfer = o.chamfer == null ? 3 : o.chamfer;
    const tiles = new Map((spec.tiles || []).map(t => [t.id, t]));
    const lefts = [...new Set((spec.tiles || []).map(t => t.x))].sort((a, b) => a - b);
    const colOf = t => lefts.indexOf(t.x);
    const rightOf = k => Math.max(...spec.tiles.filter(t => t.x === lefts[k]).map(t => t.x + t.w));
    // rails: 0 = left margin, k = gutter left of column k, last = right margin
    const rails = [];
    for (let k = 0; k <= lefts.length; k++) {
      const x0 = k === 0 ? 2 : rightOf(k - 1) + 3, x1 = k === lefts.length ? spec.w - 2 : lefts[k] - 3;
      const n = Math.max(1, Math.floor((x1 - x0) / pitch) + 1);
      const lanes = [];
      // lanes ordered from the middle of the rail outwards (first traces sit centred)
      const mid = (x0 + x1) / 2;
      for (let i = 0; i < n; i++) lanes.push(Math.round((x0 + i * pitch) * 2) / 2);
      lanes.sort((a, b) => Math.abs(a - mid) - Math.abs(b - mid));
      rails.push({ x0, x1, lanes, used: lanes.map(() => []) });
    }
    const slots = new Map();   // tile|side → count
    const padY = (t, side) => {
      const key = t.id + '|' + side, k = slots.get(key) || 0; slots.set(key, k + 1);
      const step = 6, offs = [0, -1, 1, -2, 2, -3, 3, -4, 4];
      const lim = Math.max(0, Math.floor((t.h / 2 - 10) / step));
      const o2 = offs[k % offs.length], off = Math.max(-lim, Math.min(lim, o2)) * step;
      return t.y + t.h / 2 + off;
    };
    const out = [];
    for (const net of spec.nets || []) {
      const A = tiles.get(net.a), B = tiles.get(net.b);
      if (!A || !B || A === B) continue;
      const ca = colOf(A), cb = colOf(B);
      let rail, sa, sb;
      if (ca === cb) { rail = ca < lefts.length / 2 ? ca : ca + 1; }
      else rail = Math.max(ca, cb) === Math.min(ca, cb) + 1 ? Math.max(ca, cb) : Math.min(ca, cb) + 1;
      sa = rail <= ca ? 'l' : 'r'; sb = rail <= cb ? 'l' : 'r';
      const ya = padY(A, sa), yb = padY(B, sb);
      const xa = sa === 'l' ? A.x : A.x + A.w, xb = sb === 'l' ? B.x : B.x + B.w;
      let pts;
      if (Math.abs(ya - yb) < 0.5 && ca !== cb) pts = [[xa, ya], [xb, yb]];
      else {
        const R = rails[rail], lo = Math.min(ya, yb) - 4, hi = Math.max(ya, yb) + 4;
        let best = 0, bestOver = Infinity;
        for (let i = 0; i < R.lanes.length; i++) {
          const over = R.used[i].reduce((s, iv) => s + Math.max(0, Math.min(hi, iv[1]) - Math.max(lo, iv[0])), 0);
          if (over < bestOver) { bestOver = over; best = i; if (!over) break; }
        }
        R.used[best].push([lo, hi]);
        const x = R.lanes[best];
        pts = [[xa, ya], [x, ya], [x, yb], [xb, yb]];
      }
      pts = chamferPts(simplify(pts), chamfer);
      out.push({ id: net.id, a: net.a, b: net.b, pts, padA: { x: xa, y: ya, s: sa }, padB: { x: xb, y: yb, s: sb }, vias: [], length: length(pts), rail: true });
    }
    return out;
  }
  // cut every corner by r (45° chamfer on right angles)
  function chamferPts(pts, r) {
    if (!r || pts.length < 3) return pts;
    const out = [pts[0]];
    for (let i = 1; i < pts.length - 1; i++) {
      const a = pts[i - 1], b = pts[i], c = pts[i + 1];
      const l1 = Math.hypot(b[0] - a[0], b[1] - a[1]), l2 = Math.hypot(c[0] - b[0], c[1] - b[1]);
      const d = Math.min(r, l1 / 2, l2 / 2);
      if (d < 0.5) { out.push(b); continue; }
      out.push([b[0] - (b[0] - a[0]) / l1 * d, b[1] - (b[1] - a[1]) / l1 * d], [b[0] + (c[0] - b[0]) / l2 * d, b[1] + (c[1] - b[1]) / l2 * d]);
    }
    out.push(pts[pts.length - 1]);
    return out.map(q => [Math.round(q[0] * 100) / 100, Math.round(q[1] * 100) / 100]);
  }

  // Glow decay: falls exponentially, every new report refreshes it (capped), the rate raises the strength
  const DECAY_TAU = 26;   // s → after ~30 s still ~30 %, after 90 s ~3 %
  function decay(level, dt, tau) { return level * Math.exp(-Math.max(0, dt) / (tau || DECAY_TAU)); }
  function refresh(level, n) { const gain = 0.42 + 0.16 * Math.log2(1 + Math.max(1, n || 1)); return Math.min(1, level + gain * (1 - level * 0.55)); }

  // ------------------------------------------------------------------ nets of a view
  // Kind of a trace from a free text: the first rule [RegExp, kind] that matches the lower-cased text, else the fallback.
  // The rules are host data (vocabulary of the host application).
  function kindFromText(t, rules, fallback) {
    const s = String(t || '').toLowerCase();
    for (const [re, kind] of rules || []) if (re.test(s)) return kind;
    return fallback == null ? 'api' : fallback;
  }

  // Curated edges (tile ↔ tile) + every "talks to" relation from the data whose both ends are visible.
  // o = { nodes: {nid: {x, y, w, h, sys: [], shape}}, curated: [{from, to, kind, both}], systems: [{id, talksTo: [{target, kind}]}],
  //       kindRules: [[RegExp, kind]], refineKind(kind, targetSystem) → kind }
  function buildNets(o) {
    const nodes = o.nodes, sysNodes = new Map();
    Object.entries(nodes).forEach(([nid, k]) => (k.sys || []).forEach(s => { if (!sysNodes.has(s)) sysNodes.set(s, []); sysNodes.get(s).push(nid); }));
    const nets = [], pair = new Map();
    const key = (a, b) => a < b ? a + '|' + b : b + '|' + a;
    const add = (n) => { n.id = 'n' + nets.length; nets.push(n); const k = key(n.a, n.b); if (!pair.has(k)) pair.set(k, []); pair.get(k).push(n); return n; };
    (o.curated || []).forEach((e, i) => {
      if (!e.from || !e.to || e.from === e.to || !nodes[e.from] || !nodes[e.to]) return;
      const have = (pair.get(key(e.from, e.to)) || []).find(n => n.curated && n.kind === e.kind);
      if (have) { if (have.a !== e.from) have.both = true; if (e.both) have.both = true; (have.edges = have.edges || []).push(i); return; }
      add({ a: e.from, b: e.to, kind: e.kind || 'api', both: !!e.both, curated: true, edges: [i] });
    });
    const centre = nid => { const k = nodes[nid]; return [k.x + k.w / 2, k.y + k.h / 2]; };
    const sysIndex = new Map((o.systems || []).map(s => [s.id, s]));
    const data = [];   // every data edge with both ends visible (for coverage checks)
    (o.systems || []).forEach(s => {
      const A = sysNodes.get(s.id); if (!A) return;
      (s.talksTo || []).forEach(z => {
        const B = sysNodes.get(z.target); if (!B) return;
        // best tile pair candidate: shortest distance, never the same tile
        let best = null, bd = Infinity;
        for (const a of A) for (const b of B) {
          if (a === b) continue;
          const [x1, y1] = centre(a), [x2, y2] = centre(b), dd = Math.hypot(x2 - x1, y2 - y1);
          if (dd < bd) { bd = dd; best = [a, b]; }
        }
        if (!best) return;   // both only in the same tile → no trace needed
        data.push({ from: s.id, to: z.target });
        // already covered by a curated or earlier trace between any tile pair?
        for (const a of A) for (const b of B) {
          if (a === b) continue;
          const have = pair.get(key(a, b));
          if (have && have.length) { const n = have[0]; if (n.a === b && !n.curated) n.both = true; return; }
        }
        let kind = kindFromText(z.kind, o.kindRules);
        if (o.refineKind) kind = o.refineKind(kind, sysIndex.get(z.target));
        add({ a: best[0], b: best[1], kind, both: false, curated: false, sys: [s.id, z.target] });
      });
    });
    // order: curated first (they get the best channels), then short before long
    const order = nets.slice().sort((x, y) => (y.curated - x.curated) || (dist(x) - dist(y)));
    function dist(n) { const [a, b] = centre(n.a), [c, d] = centre(n.b); return Math.abs(a - c) + Math.abs(b - d); }
    return { nets, order, data, sysNodes };
  }

  // Live conversation (systems) → matching traces
  function tracesFor(from, to, sysNodes, traces) {
    const A = sysNodes.get(from) || [], B = sysNodes.get(to) || [];
    const hits = [];
    for (const r of traces) {
      if (A.includes(r.a) && B.includes(r.b)) hits.push([r, false]);
      else if (A.includes(r.b) && B.includes(r.a)) hits.push([r, true]);
    }
    return hits;
  }

  // ------------------------------------------------------------------ order nets: backbone first, then short before long
  function orderNets(nets, tiles) {
    const centre = id => { const k = tiles[id]; return k ? [k.x + k.w / 2, k.y + k.h / 2] : [0, 0]; };
    const dist = n => { const [a, b] = centre(n.a), [c, d] = centre(n.b); return Math.abs(a - c) + Math.abs(b - d); };
    return nets.slice().sort((x, y) => ((KIND_RANK[x.kind] ?? 9) - (KIND_RANK[y.kind] ?? 9)) || (dist(x) - dist(y)) || (x.id < y.id ? -1 : x.id > y.id ? 1 : 0));
  }

  // Tiles first (keep-out areas), rim nodes afterwards — their pads only appear on free cells
  function addNodes(r, list) {
    (list || []).forEach(k => { if (k.shape !== 'rim') r.tile(k.id, k.x, k.y, k.w, k.h, k.shape); });
    (list || []).forEach(k => { if (k.shape === 'rim') r.rim(k.id, k.x, k.y, k.w, k.h, k.r); });
  }
  function buildGrid(spec) {
    const r = new Grid({ w: spec.w, h: spec.h, p: spec.p || 3, kl: spec.kl, weight: spec.weight, cost: spec.cost, smooth: spec.smooth });
    addNodes(r, spec.tiles);
    (spec.soft || []).forEach(f => r.area(f.x, f.y, f.w, f.h, f.k));
    (spec.lines || []).forEach(l => r.line(l[0], l[1], l[2], l[3]));
    return r;
  }

  // ------------------------------------------------------------------ everything in one go (window fallback / tests)
  function routeAll(spec, cache) {
    const t0 = Date.now();
    const r = buildGrid(spec);
    const out = [], failed = [];
    const byId = new Map((spec.nets || []).map(n => [n.id, n]));
    if (cache && cache.length) cache.forEach(b => { r.register(b, byId.get(b.id)); out.push(b); });
    const done = new Set(out.map(b => b.id));
    for (const n of spec.nets || []) {
      if (done.has(n.id)) continue;
      const b = r.route(n);
      if (b) out.push(b); else failed.push({ id: n.id, reason: r.reason });
    }
    return { grid: r, traces: out, failed, ms: Date.now() - t0 };
  }

  // ------------------------------------------------------------------ worker
  function startWorker(w) {
    const grids = new Map();   // uid → Grid (for later live traces)
    w.onmessage = ev => {
      const m = ev.data || {};
      try {
        if (m.type === 'route') {
          const t0 = Date.now();
          const r = buildGrid(m.spec);
          if (m.exclusive) grids.clear();
          grids.set(m.uid, r);
          const all = [], failed = [];
          const byId = new Map((m.spec.nets || []).map(n => [n.id, n]));
          (m.cache || []).forEach(b => { r.register(b, byId.get(b.id)); all.push(b); });
          const done = new Set(all.map(b => b.id));
          let part = [];
          for (const n of m.spec.nets || []) {
            if (done.has(n.id)) continue;
            const b = r.route(n);
            if (b) { all.push(b); part.push(b); } else failed.push({ id: n.id, reason: r.reason });
            if (part.length >= 24) { w.postMessage({ type: 'part', uid: m.uid, job: m.job, traces: part }); part = []; }
          }
          w.postMessage({ type: 'done', uid: m.uid, job: m.job, traces: part, all, failed, ms: Date.now() - t0 });
        } else if (m.type === 'net') {
          const r = grids.get(m.uid);
          const b = r ? r.route(m.net) : null;
          w.postMessage({ type: 'trace', uid: m.uid, job: m.job, net: m.net, trace: b });
        } else if (m.type === 'forget') grids.delete(m.uid);
      } catch (e) { w.postMessage({ type: 'error', uid: m.uid, job: m.job, text: String(e && e.message || e) }); }
    };
  }

  // small, stable hash (cache key)
  function hash(s) { let h = 2166136261 >>> 0; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; } return h.toString(36); }
  function cacheKey(spec) { return 'v' + VERSION + '-' + hash(JSON.stringify(spec)); }

  return { API, VERSION, COST, KIND_RANK, Grid, Heap, routeAll, buildGrid, buildNets, tracesFor, kindFromText, orderNets, fallbackRoute, railRoutes, chamferPts, offset, isOctilinear, octilinear, octilinearize, simplify, length, decay, refresh, DECAY_TAU, startWorker, hash, cacheKey, DX, DY };
});
