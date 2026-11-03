/* Lanloom — live layers of the board: a glow canvas (trace intensity from measured rates) and a dot canvas
   (one dot ≈ q bytes travelling along the routed trace). Canvas2D, devicePixelRatio-correct, viewport-sized
   (the board scrolls underneath; only visible dots are drawn). Handles several thousand dots at 60 fps,
   degrades by merging dots into larger size classes and by a global cap. With prefers-reduced-motion only the
   soft glow is shown. */
(function (root) {
  'use strict';
  const LL = root.Lanloom = root.Lanloom || {};

  const KINDS = ['uplink', 'lan', 'service', 'flow', 'integration'];
  const SIZES = [1, 8, 64];             // dot size classes in quanta (larger dots carry 8× / 64× q)

  function decompose(n, cap) {
    // n quanta → counts per size class with at most `cap` dots; returns {counts:[c1,c8,c64], rest}
    n = Math.max(0, Math.floor(n));
    if (cap <= 0) return { counts: [0, 0, 0], rest: n };
    if (n <= cap) return { counts: [n, 0, 0], rest: 0 };
    let c64 = 0, c8 = Math.floor(n / 8), c1 = n % 8;
    if (c8 + c1 > cap) { c64 = Math.floor(c8 / 8); c8 = c8 % 8; }
    let rest = 0;
    while (c64 + c8 + c1 > cap) {   // still too many: keep the big ones, carry the remainder to the next second
      if (c1) { rest += c1; c1 = 0; continue; }
      if (c8) { rest += c8 * 8; c8 = 0; continue; }
      const drop = c64 - cap; rest += drop * 64; c64 = cap;
    }
    return { counts: [c1, c8, c64], rest };
  }

  class Dots {
    constructor(glow, dots, o) {
      this.glowC = glow; this.dotC = dots; this.o = o || {};
      this.gctx = glow.getContext('2d'); this.dctx = dots.getContext('2d');
      this.cap = this.o.compact ? 1200 : 5000;
      this.perTrace = this.o.compact ? 16 : 40;   // max dots per trace, direction and second
      this.speed = this.o.compact ? 150 : 210;     // px per second
      // dots as struct of arrays
      this.tr = new Int32Array(this.cap); this.t0 = new Float64Array(this.cap); this.du = new Float32Array(this.cap);
      this.rev = new Uint8Array(this.cap); this.sz = new Uint8Array(this.cap); this.n = 0;
      this.geo = []; this.idx = new Map();         // trace id → index into geo
      this.rest = new Map();                       // carried quanta per trace+direction
      this.glowTarget = new Float32Array(0); this.glowNow = new Float32Array(0); this.glowSeen = new Float64Array(0);
      this.focus = null;                           // Set of trace ids or null
      this.scrollY = 0; this.vw = 1; this.vh = 1; this.dpr = 1;
      this.colors = null; this.sprites = new Map();
      this.reduced = false; this._mq = null;
      this.stats = { active: 0, spawned: 0, dropped: 0, fps: 0, drawMs: 0, frames: 0 };
      this._raf = 0; this._last = 0; this._fpsT = 0; this._fpsN = 0; this._glowAnim = 0; this._dead = false;
      try {
        this._mq = root.matchMedia('(prefers-reduced-motion: reduce)');
        this.reduced = !!this._mq.matches;
        this._onMq = () => { this.reduced = !!this._mq.matches; if (this.reduced) this.n = 0; this.wake(); };
        if (this._mq.addEventListener) this._mq.addEventListener('change', this._onMq);
      } catch (e) { /* no matchMedia */ }
      this._onVis = () => { if (!document.hidden) { this.prune(performance.now()); this.wake(); } };
      document.addEventListener('visibilitychange', this._onVis);
    }

    // ------------------------------------------------------------ geometry
    setGeometry(list) {
      // list: [{id, pts, kind, measured, members}] (members: traces sharing the busiest trunk of this trace)
      const oldIdx = this.idx, oldTarget = this.glowTarget, oldNow = this.glowNow, oldSeen = this.glowSeen;
      this.geo = []; this.idx = new Map();
      list.forEach(t => {
        if (!t.pts || t.pts.length < 2) return;
        const n = t.pts.length, a = new Float32Array(n * 2), c = new Float32Array(n);
        let L = 0, x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        t.pts.forEach((q, i) => {
          a[i * 2] = q[0]; a[i * 2 + 1] = q[1];
          if (i) L += Math.hypot(q[0] - t.pts[i - 1][0], q[1] - t.pts[i - 1][1]);
          c[i] = L;
          x0 = Math.min(x0, q[0]); y0 = Math.min(y0, q[1]); x1 = Math.max(x1, q[0]); y1 = Math.max(y1, q[1]);
        });
        this.idx.set(t.id, this.geo.length);
        this.geo.push({ id: t.id, a, c, n, L, kind: Math.max(0, KINDS.indexOf(t.kind)), measured: !!t.measured, members: Math.max(1, t.members || 1), box: [x0, y0, x1, y1] });
      });
      const N = this.geo.length;
      this.glowTarget = new Float32Array(N); this.glowNow = new Float32Array(N); this.glowSeen = new Float64Array(N);
      this.geo.forEach((g, i) => {
        const o = oldIdx.get(g.id);
        if (o != null && o < oldTarget.length) { this.glowTarget[i] = oldTarget[o]; this.glowNow[i] = oldNow[o]; this.glowSeen[i] = oldSeen[o]; }
      });
      // dots of traces that changed geometry are dropped (structure changed → new routes)
      this.n = 0;
      this.wake();
    }

    resize(w, h) {
      const dpr = Math.min(2, root.devicePixelRatio || 1);
      this.vw = Math.max(1, Math.round(w)); this.vh = Math.max(1, Math.round(h)); this.dpr = dpr;
      for (const c of [this.glowC, this.dotC]) {
        const W = Math.max(1, Math.round(this.vw * dpr)), H = Math.max(1, Math.round(this.vh * dpr));
        if (c.width !== W) c.width = W;
        if (c.height !== H) c.height = H;
      }
      this.sprites.clear();
      this.drawGlow(); this.wake();
    }
    setScroll(y) { if (y !== this.scrollY) { this.scrollY = y; this.drawGlow(); this.wake(); } }
    setFocus(set) { this.focus = set && set.size ? set : null; this.drawGlow(); this.wake(); }
    setColors(c) { this.colors = c; this.sprites.clear(); this.drawGlow(); this.wake(); }

    // ------------------------------------------------------------ live frames
    frame(f) {
      if (!f || this._dead) return;
      const now = performance.now();
      const q = f.q || 65536;
      // glow targets from rates (bytes per second, log scale); traces without a rate fall back to their dot count
      const seen = new Set();
      if (f.rates) for (const id in f.rates) {
        const i = this.idx.get(id); if (i == null) continue;
        const r = f.rates[id] || [0, 0];
        const v = (+r[0] || 0) + (+r[1] || 0);
        this.glowTarget[i] = Math.min(1, Math.log10(1 + v / 1024) / 4.6);
        this.glowSeen[i] = now; seen.add(i);
      }
      if (f.dots) for (const d of f.dots) {
        const i = this.idx.get(d[0]); if (i == null || seen.has(i)) continue;
        this.glowTarget[i] = Math.max(this.glowTarget[i] * 0.5, Math.min(1, Math.log10(1 + (d[2] || 0) * q / 1024) / 4.6));
        this.glowSeen[i] = now;
      }
      for (let i = 0; i < this.glowTarget.length; i++) if (now - this.glowSeen[i] > 2500) this.glowTarget[i] *= 0.6;   // fades without reports
      this._glowAnim = now;
      if (this.reduced) { this.glowNow.set(this.glowTarget); this.drawGlow(); }
      // dots
      if (!this.reduced && !document.hidden && f.dots) this.spawn(f.dots, now);
      this.wake();
    }

    spawn(list, now) {
      let free = this.cap - this.n;
      for (const d of list) {
        const gi = this.idx.get(d[0]); if (gi == null) continue;
        const g = this.geo[gi], dir = d[1] ? 1 : 0;
        const key = gi * 2 + dir;
        const total = (d[2] || 0) + (this.rest.get(key) || 0);
        if (total < 1) { if (total > 0) this.rest.set(key, total); else this.rest.delete(key); continue; }
        // bundled traces share the dot budget of their trunk (√n instead of n): more and, past the cap, larger dots on it
        const per = g.members > 1 ? Math.max(3, Math.round(this.perTrace / Math.sqrt(g.members))) : this.perTrace;
        const z = decompose(total, Math.min(per, free));
        const count = z.counts[0] + z.counts[1] + z.counts[2];
        // carry at most ~10 s worth, so a long overload never grows without bound
        const carry = Math.min(z.rest + (total - Math.floor(total)), Math.max(64, (d[2] || 0) * 10));
        if (z.rest + (total - Math.floor(total)) > carry) this.stats.dropped++;
        if (carry > 0) this.rest.set(key, carry); else this.rest.delete(key);
        if (!count) continue;
        free -= count;
        const dur = Math.max(700, Math.min(4500, g.L / this.speed * 1000));
        // shuffle size classes so large dots do not all run at the start of the second
        const order = [];
        z.counts.forEach((c, s) => { for (let k = 0; k < c; k++) order.push(s); });
        for (let i = order.length - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; const t = order[i]; order[i] = order[j]; order[j] = t; }
        for (let i = 0; i < order.length; i++) {
          const k = this.n++;
          this.tr[k] = gi; this.rev[k] = dir; this.sz[k] = order[i];
          this.t0[k] = now + (i + 0.15 + Math.random() * 0.7) / order.length * 1000;
          this.du[k] = dur;
          this.stats.spawned++;
        }
        if (free <= 0) free = 0;
      }
    }

    prune(now) {
      let w = 0;
      for (let k = 0; k < this.n; k++) {
        if (now >= this.t0[k] + this.du[k]) continue;
        if (w !== k) { this.tr[w] = this.tr[k]; this.t0[w] = this.t0[k]; this.du[w] = this.du[k]; this.rev[w] = this.rev[k]; this.sz[w] = this.sz[k]; }
        w++;
      }
      this.n = w;
    }

    // ------------------------------------------------------------ drawing
    wake() { if (!this._raf && !this._dead && !document.hidden) this._raf = root.requestAnimationFrame(t => this.loop(t)); }
    loop(now) {
      this._raf = 0;
      if (this._dead || document.hidden) return;
      // at most ~60 frames per second (120 Hz screens do not need every frame drawn twice)
      if (now - this._last < 14) { this._raf = root.requestAnimationFrame(t => this.loop(t)); return; }
      this._last = now;
      const t0 = performance.now();
      let busy = false;
      if (!this.reduced && now - this._glowAnim < 900) {
        let moving = false;
        for (let i = 0; i < this.glowNow.length; i++) {
          const d = this.glowTarget[i] - this.glowNow[i];
          if (Math.abs(d) > 0.004) { this.glowNow[i] += d * 0.12; moving = true; }
        }
        if (moving || !this._glowDrawn) { this.drawGlow(); busy = true; }
      }
      if (this.n) { this.drawDots(now); busy = true; }
      else if (this._dotsDrawn) { this.dctx.setTransform(1, 0, 0, 1, 0, 0); this.dctx.clearRect(0, 0, this.dotC.width, this.dotC.height); this._dotsDrawn = false; }
      const ms = performance.now() - t0;
      this.stats.drawMs = this.stats.drawMs * 0.9 + ms * 0.1;
      this._fpsN++;
      if (now - this._fpsT >= 1000) { this.stats.fps = Math.round(this._fpsN * 1000 / (now - this._fpsT)); this._fpsT = now; this._fpsN = 0; }
      this.stats.active = this.n; this.stats.frames++;
      if (busy || now - this._glowAnim < 900) this._raf = root.requestAnimationFrame(t => this.loop(t));
      else this.stats.fps = 0;
    }

    color(kind) { return (this.colors && this.colors.kinds[KINDS[kind]]) || '#7f8c86'; }

    drawGlow() {
      const ctx = this.gctx, dpr = this.dpr, W = this.glowC.width, H = this.glowC.height;
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, H);
      this._glowDrawn = true;
      if (!this.colors || !this.geo.length) return;
      const top = this.scrollY - 12, bottom = this.scrollY + this.vh + 12;
      ctx.setTransform(dpr, 0, 0, dpr, 0, -this.scrollY * dpr);
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      const dark = this.colors.dark;
            for (let i = 0; i < this.geo.length; i++) {
        const v = this.glowNow[i]; if (v < 0.015) continue;
        const g = this.geo[i]; if (g.box[3] < top || g.box[1] > bottom) continue;
        const dim = this.focus && !this.focus.has(g.id) ? 0.15 : 1;
        const a = Math.pow(v, 0.7) * dim;
        ctx.strokeStyle = this.color(g.kind);
        ctx.beginPath();
        ctx.moveTo(g.a[0], g.a[1]);
        for (let k = 1; k < g.n; k++) ctx.lineTo(g.a[k * 2], g.a[k * 2 + 1]);
        ctx.globalAlpha = a * (dark ? 0.2 : 0.14); ctx.lineWidth = this.o.compact ? 3.5 : 6; ctx.stroke();
        ctx.globalAlpha = Math.min(1, (dark ? 0.3 : 0.28) + a * 0.55) * dim; ctx.lineWidth = this.o.compact ? 1.6 : 2; ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    sprite(kind, size) {
      const key = kind * 4 + size;
      let s = this.sprites.get(key);
      if (s) return s;
      const dpr = this.dpr, dark = this.colors ? this.colors.dark : false;
      const base = (this.o.compact ? 2.1 : 2.4) * [1, 1.45, 2.0][size];
      const r = base * dpr, halo = r * (dark ? 2.6 : 1.9), gr = Math.ceil(halo + 2);
      const c = document.createElement('canvas'); c.width = c.height = gr * 2;
      const x = c.getContext('2d'), col = this.color(kind);
      const grad = x.createRadialGradient(gr, gr, r * 0.5, gr, gr, halo);
      grad.addColorStop(0, col); grad.addColorStop(1, 'rgba(0,0,0,0)');
      x.globalAlpha = dark ? 0.45 : 0.28; x.fillStyle = grad; x.beginPath(); x.arc(gr, gr, halo, 0, 6.2832); x.fill();
      x.globalAlpha = 1; x.fillStyle = col; x.beginPath(); x.arc(gr, gr, r, 0, 6.2832); x.fill();
      x.fillStyle = (this.colors && this.colors.core) || '#fff';
      x.globalAlpha = dark ? 0.9 : 0.85; x.beginPath(); x.arc(gr, gr, r * 0.42, 0, 6.2832); x.fill();
      s = { c, gr };
      this.sprites.set(key, s);
      return s;
    }

    drawDots(now) {
      const ctx = this.dctx, dpr = this.dpr, W = this.dotC.width, H = this.dotC.height;
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, H);
      this._dotsDrawn = true;
      if (!this.colors) return;
      const sy = this.scrollY, lane = this.o.compact ? 1.3 : 1.6;
      let w = 0, alpha = 1;
      ctx.globalAlpha = 1;
      for (let k = 0; k < this.n; k++) {
        const t0 = this.t0[k], du = this.du[k];
        if (now >= t0 + du) continue;   // expired: compacted away below
        if (w !== k) { this.tr[w] = this.tr[k]; this.t0[w] = t0; this.du[w] = du; this.rev[w] = this.rev[k]; this.sz[w] = this.sz[k]; }
        w++;
        if (now < t0) continue;
        const g = this.geo[this.tr[k]]; if (!g) continue;
        if (g.box[3] < sy - 10 || g.box[1] > sy + this.vh + 10) continue;
        const p = (now - t0) / du, rv = this.rev[k];
        const s = (rv ? 1 - p : p) * g.L;
        // segment by binary search on the cumulative length
        let lo = 0, hi = g.n - 1;
        while (hi - lo > 1) { const m = (lo + hi) >> 1; if (g.c[m] < s) lo = m; else hi = m; }
        const sl = (g.c[hi] - g.c[lo]) || 1, f = (s - g.c[lo]) / sl;
        const ax = g.a[lo * 2], ay = g.a[lo * 2 + 1], bx = g.a[hi * 2], by = g.a[hi * 2 + 1];
        const x = ax + (bx - ax) * f, y = ay + (by - ay) * f;
        // lane: right of the travel direction, so both directions run side by side
        const dx = (bx - ax) / sl * (rv ? -1 : 1), dy = (by - ay) / sl * (rv ? -1 : 1);
        const X = (x - dy * lane) * dpr, Y = (y + dx * lane - sy) * dpr;
        if (X < -20 || Y < -20 || X > W + 20 || Y > H + 20) continue;
        let a = Math.min(1, p / 0.06, (1 - p) / 0.06);
        if (this.focus && !this.focus.has(g.id)) a *= 0.12;
        if (a !== alpha) { ctx.globalAlpha = a; alpha = a; }
        const sp = this.sprite(g.kind, this.sz[k]);
        ctx.drawImage(sp.c, X - sp.gr, Y - sp.gr);
      }
      this.n = w;
      ctx.globalAlpha = 1;
    }

    clear() { this.n = 0; this.rest.clear(); this.glowTarget.fill(0); this.glowNow.fill(0); this.drawGlow(); this.wake(); }

    destroy() {
      this._dead = true;
      if (this._raf) root.cancelAnimationFrame(this._raf);
      document.removeEventListener('visibilitychange', this._onVis);
      try { if (this._mq && this._mq.removeEventListener) this._mq.removeEventListener('change', this._onMq); } catch (e) { /* */ }
      this.n = 0; this.sprites.clear();
    }
  }

  LL.Dots = Dots;
  LL.Dots.decompose = decompose;
  LL.Dots.SIZES = SIZES;
  LL.Dots.KINDS = KINDS;
})(typeof self !== 'undefined' ? self : this);
