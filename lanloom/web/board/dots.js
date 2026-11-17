/* Lanloom — live layers of the board: a glow canvas (trace intensity from measured rates) and a dot canvas
   (one dot ≈ q bytes travelling along the routed trace). Canvas2D, devicePixelRatio-correct, viewport-sized
   (the board scrolls underneath; only visible dots are drawn). Handles several thousand dots at 60 fps,
   degrades by merging dots into larger size classes and by a global cap. With prefers-reduced-motion only the
   soft glow is shown.
   Frame budget (small hosts, browsers without graphics acceleration): the frame rate is capped by the motion setting
   (auto 30, full 60, saver 15 frames per second, off = glow only) and steps down on its own (30 → 20 → 15 → 10) when
   drawing takes more than half of a frame, and back up when there is room again. Nothing is drawn while the tab is
   hidden or the board is scrolled out of view, and nothing moves without a live frame (no idle animation).
   Canvas2D only; without real acceleration (software renderer) "auto" starts at 20 frames per second. */
(function (root) {
  'use strict';
  const LL = root.Lanloom = root.Lanloom || {};

  const KINDS = ['uplink', 'lan', 'service', 'flow', 'integration', 'cable', 'radio', 'virtual'];
  /* Event rings: one ring = one state change of a home-automation entity on the lane of its radio network or area.
     Drawn as a small hollow ring (not a filled dot) so events are never mistaken for byte traffic; they travel from
     end a to end b at ~0.6x the dot speed, at most RING_PER_TRACE per trace and second (extra events are merged),
     in their own small pool of RING_CAP so the byte dot budget is untouched. */
  const RING_CAP = 300, RING_PER_TRACE = 3, RING_SPEED = 0.6, RING_R = 3.5, RING_W = 1.6;
  const SIZES = [1, 8, 64];             // dot size classes in quanta (larger dots carry 8× / 64× q)
  const STEPS = [60, 30, 20, 15, 10];   // frame rate steps of the budget
  const MOTION = { auto: 30, full: 60, saver: 15, off: 0 };

  function motion() {
    try { const m = root.localStorage.getItem('ll-motion'); return MOTION[m] != null ? m : 'auto'; } catch (e) { return 'auto'; }
  }
  // real graphics acceleration? (a software renderer draws every canvas pixel on the CPU) - asked once per page
  let gpuCache = null;
  function hasGpu() {
    if (gpuCache != null) return gpuCache;
    gpuCache = true;
    try {
      const c = document.createElement('canvas');
      const gl = c.getContext('webgl', { failIfMajorPerformanceCaveat: true });
      if (!gl) { gpuCache = false; return gpuCache; }
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      const name = String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) || '');
      if (/swiftshader|llvmpipe|softpipe|software|basic render/i.test(name)) gpuCache = false;
      const lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();
    } catch (e) { gpuCache = false; }
    return gpuCache;
  }
  function capFor(m) {
    if (m === 'auto' && !hasGpu()) return 20;
    return MOTION[m];
  }

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
      this.rtr = new Int32Array(RING_CAP); this.rt0 = new Float64Array(RING_CAP); this.rdu = new Float32Array(RING_CAP); this.rn = 0;   // event rings
      this.geo = []; this.idx = new Map();         // trace id → index into geo
      this.rest = new Map();                       // carried quanta per trace+direction
      this.glowTarget = new Float32Array(0); this.glowNow = new Float32Array(0); this.glowSeen = new Float64Array(0);
      this.focus = null;                           // Set of trace ids or null
      this.scrollY = 0; this.vw = 1; this.vh = 1; this.dpr = 1;
      this.colors = null; this.sprites = new Map();
      this.reduced = false; this._mq = null;
      this.stats = { active: 0, spawned: 0, dropped: 0, fps: 0, drawMs: 0, frames: 0, cap: 0, steps: 0 };
      this.motion = motion();
      this.fpsCap = capFor(this.motion);           // highest frame rate allowed by the setting
      this.fpsNow = this.fpsCap;                   // current step of the budget (≤ fpsCap)
      this.capScale = this.motion === 'saver' ? 0.4 : 1;
      this._slowT = 0; this._fastT = 0;
      this.inView = true;
      try {
        if (root.IntersectionObserver) {
          this._io = new root.IntersectionObserver(es => {
            const v = es.some(e => e.isIntersecting);
            if (v === this.inView) return;
            this.inView = v;
            if (v) { this.prune(performance.now()); this.drawGlow(); this.wake(); }
          });
          this._io.observe(dots);
        }
      } catch (e) { /* no observer: always drawn while the tab is visible */ }
      this._raf = 0; this._last = 0; this._fpsT = 0; this._fpsN = 0; this._glowAnim = 0; this._dead = false;
      try {
        this._mq = root.matchMedia('(prefers-reduced-motion: reduce)');
        this.reduced = !!this._mq.matches;
        this._onMq = () => { this.reduced = !!this._mq.matches; if (this.reduced) { this.n = 0; this.rn = 0; } this.wake(); };
        if (this._mq.addEventListener) this._mq.addEventListener('change', this._onMq);
      } catch (e) { /* no matchMedia */ }
      this._onVis = () => { if (!document.hidden) { this.prune(performance.now()); this.wake(); } };
      document.addEventListener('visibilitychange', this._onVis);
      this._onMotion = ev => this.setMotion(ev.detail);
      document.addEventListener('ll-motion', this._onMotion);
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
      this.n = 0; this.rn = 0;
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
      if (this.reduced || !this.fpsCap) { this.glowNow.set(this.glowTarget); if (this.inView) this.drawGlow(); }
      // dots
      if (!this.reduced && this.fpsCap && this.inView && !document.hidden && f.dots) this.spawn(f.dots, now);
      if (!this.reduced && this.fpsCap && this.inView && !document.hidden && f.rings) this.spawnRings(f.rings, now);
      this.wake();
    }

    spawn(list, now) {
      let free = Math.max(0, Math.round(this.cap * this.capScale) - this.n);
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

    spawnRings(list, now) {
      for (const r of list) {
        const gi = this.idx.get(r[0]); if (gi == null) continue;
        const k = Math.min(RING_PER_TRACE, Math.max(0, Math.floor(+r[1] || 0)));
        const g = this.geo[gi], dur = Math.max(900, Math.min(6500, g.L / (this.speed * RING_SPEED) * 1000));
        for (let i = 0; i < k && this.rn < RING_CAP; i++) {
          const j = this.rn++;
          this.rtr[j] = gi; this.rdu[j] = dur;
          this.rt0[j] = now + (i + 0.15 + Math.random() * 0.7) / k * 1000;
        }
      }
    }

    prune(now) {
      let rw = 0;
      for (let k = 0; k < this.rn; k++) {
        if (now >= this.rt0[k] + this.rdu[k]) continue;
        if (rw !== k) { this.rtr[rw] = this.rtr[k]; this.rt0[rw] = this.rt0[k]; this.rdu[rw] = this.rdu[k]; }
        rw++;
      }
      this.rn = rw;
      let w = 0;
      for (let k = 0; k < this.n; k++) {
        if (now >= this.t0[k] + this.du[k]) continue;
        if (w !== k) { this.tr[w] = this.tr[k]; this.t0[w] = this.t0[k]; this.du[w] = this.du[k]; this.rev[w] = this.rev[k]; this.sz[w] = this.sz[k]; }
        w++;
      }
      this.n = w;
    }

    // ------------------------------------------------------------ drawing
    setMotion(m) {
      this.motion = MOTION[m] != null ? m : 'auto';
      this.fpsCap = capFor(this.motion); this.fpsNow = this.fpsCap; this.capScale = this.motion === 'saver' ? 0.4 : 1;
      if (!this.fpsCap) { this.n = 0; this.rn = 0; }
      this.wake();
    }
    budget(now, ms) {
      // step the frame rate down when drawing eats more than half a frame for 2 s, up again after 10 s with room
      const frame = 1000 / Math.max(1, this.fpsNow);
      if (ms > frame * 0.5) { this._fastT = 0; if (!this._slowT) this._slowT = now; }
      else if (ms < frame * 0.2) { this._slowT = 0; if (!this._fastT) this._fastT = now; }
      else { this._slowT = 0; this._fastT = 0; }
      const i = STEPS.indexOf(this.fpsNow);
      if (this._slowT && now - this._slowT > 2000) {
        this._slowT = 0;
        if (i >= 0 && i < STEPS.length - 1) { this.fpsNow = STEPS[i + 1]; this.stats.steps++; }
        else this.capScale = Math.max(0.25, this.capScale * 0.7);      // already at the lowest rate: fewer dots
      } else if (this._fastT && now - this._fastT > 10000) {
        this._fastT = 0;
        if (this.capScale < (this.motion === 'saver' ? 0.4 : 1)) this.capScale = Math.min(this.motion === 'saver' ? 0.4 : 1, this.capScale / 0.7);
        else if (i > 0 && STEPS[i - 1] <= this.fpsCap) this.fpsNow = STEPS[i - 1];
      }
      this.stats.cap = this.fpsNow;
    }
    wake() { if (!this._raf && !this._dead && !document.hidden && this.inView) this._raf = root.requestAnimationFrame(t => this.loop(t)); }
    loop(now) {
      this._raf = 0;
      if (this._dead || document.hidden || !this.inView) return;
      // frame budget: at most fpsNow frames per second (a 120 Hz screen never draws every frame)
      const minGap = this.fpsNow ? 1000 / this.fpsNow - 2 : 1000;
      if (now - this._last < minGap) { this._raf = root.requestAnimationFrame(t => this.loop(t)); return; }
      this._last = now;
      const t0 = performance.now();
      let busy = false;
      if (!this.reduced && this.fpsCap && now - this._glowAnim < 900) {
        let moving = false;
        for (let i = 0; i < this.glowNow.length; i++) {
          const d = this.glowTarget[i] - this.glowNow[i];
          if (Math.abs(d) > 0.004) { this.glowNow[i] += d * 0.12; moving = true; }
        }
        if (moving || !this._glowDrawn) { this.drawGlow(); busy = true; }
      }
      if (this.n || this.rn) { this.drawDots(now); busy = true; }
      else if (this._dotsDrawn) { this.dctx.setTransform(1, 0, 0, 1, 0, 0); this.dctx.clearRect(0, 0, this.dotC.width, this.dotC.height); this._dotsDrawn = false; }
      const ms = performance.now() - t0;
      this.stats.drawMs = this.stats.drawMs * 0.9 + ms * 0.1;
      if (busy) this.budget(now, this.stats.drawMs);
      this._fpsN++;
      if (now - this._fpsT >= 1000) { this.stats.fps = Math.round(this._fpsN * 1000 / (now - this._fpsT)); this._fpsT = now; this._fpsN = 0; }
      this.stats.active = this.n + this.rn; this.stats.frames++;
      if (busy || (this.fpsCap && now - this._glowAnim < 900)) this._raf = root.requestAnimationFrame(t => this.loop(t));
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
      this.drawRings(now);
      ctx.globalAlpha = 1;
    }

    drawRings(now) {
      const ctx = this.dctx, dpr = this.dpr, W = this.dotC.width, H = this.dotC.height, sy = this.scrollY;
      let w = 0;
      ctx.lineWidth = RING_W * dpr;
      for (let k = 0; k < this.rn; k++) {
        const t0 = this.rt0[k], du = this.rdu[k];
        if (now >= t0 + du) continue;
        if (w !== k) { this.rtr[w] = this.rtr[k]; this.rt0[w] = t0; this.rdu[w] = du; }
        w++;
        if (now < t0) continue;
        const g = this.geo[this.rtr[k]]; if (!g) continue;
        if (g.box[3] < sy - 10 || g.box[1] > sy + this.vh + 10) continue;
        const p = (now - t0) / du, s = p * g.L;
        let lo = 0, hi = g.n - 1;
        while (hi - lo > 1) { const m = (lo + hi) >> 1; if (g.c[m] < s) lo = m; else hi = m; }
        const sl = (g.c[hi] - g.c[lo]) || 1, f = (s - g.c[lo]) / sl;
        const X = (g.a[lo * 2] + (g.a[hi * 2] - g.a[lo * 2]) * f) * dpr, Y = (g.a[lo * 2 + 1] + (g.a[hi * 2 + 1] - g.a[lo * 2 + 1]) * f - sy) * dpr;
        if (X < -20 || Y < -20 || X > W + 20 || Y > H + 20) continue;
        let a = 0.9 * Math.min(1, p / 0.06, (1 - p) / 0.06);
        if (this.focus && !this.focus.has(g.id)) a *= 0.12;
        ctx.globalAlpha = a; ctx.strokeStyle = this.color(g.kind);
        ctx.beginPath(); ctx.arc(X, Y, RING_R * dpr, 0, 6.2832); ctx.stroke();
      }
      this.rn = w;
    }

    clear() { this.n = 0; this.rn = 0; this.rest.clear(); this.glowTarget.fill(0); this.glowNow.fill(0); this.drawGlow(); this.wake(); }

    destroy() {
      this._dead = true;
      if (this._raf) root.cancelAnimationFrame(this._raf);
      document.removeEventListener('visibilitychange', this._onVis);
      try { if (this._io) this._io.disconnect(); } catch (e) { /* */ }
      document.removeEventListener('ll-motion', this._onMotion);
      try { if (this._mq && this._mq.removeEventListener) this._mq.removeEventListener('change', this._onMq); } catch (e) { /* */ }
      this.n = 0; this.rn = 0; this.sprites.clear();
    }
  }

  LL.Dots = Dots;
  LL.Dots.decompose = decompose;
  LL.Dots.SIZES = SIZES;
  LL.Dots.KINDS = KINDS;
  LL.Dots.STEPS = STEPS;
  LL.Dots.MOTION = MOTION;
  LL.motion = motion;
  LL.setMotion = function (m) {
    try { if (MOTION[m] != null) root.localStorage.setItem('ll-motion', m); } catch (e) { /* private mode */ }
    try { document.dispatchEvent(new CustomEvent('ll-motion', { detail: m })); } catch (e) { /* */ }
  };
})(typeof self !== 'undefined' ? self : this);
