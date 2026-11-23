/* Lanloom — frame gate for the animated map views (O14).
   One small shared piece for every loop that draws moving dots outside the board engine: the data path maps (rain arcs and real
   cables, also inside the country sheet) and the energy figure. A screen can deliver 60, 120 or 166 frames a second; drawing every
   one of them costs the GPU for nothing, because dots moving at a calm pace look the same at 30. The gate answers one question per
   animation frame: "draw this one?"
     * Ceiling by the motion setting of the board (`ll-motion`): auto 30, full 60, saver 15, off 0 frames per second - the same
       numbers as board/dots.js, whatever the speed of the screen.
     * Below the ceiling the old self-throttling stays: when frames arrive slowly (average > 22 ms) the rate steps down
       60 → 30 → 20 → 15 → 10, after 10 s of smooth frames it steps back up (never above the ceiling).
     * Movement belongs to the callers' clock (timestamp), never to a frame count, so the speed is the same at 15, 30 and 60.
     * Pausing (hidden tab, scrolled out of view) is the caller's business; it calls reset() so no stale interval is measured.
   LL.frameGate(opts?) -> { draw(ts), reset(), fps(), off() }. opts.motion: a function returning the setting (tests). */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) { const LL = root.Lanloom = root.Lanloom || {}; LL.frameGate = api.frameGate; LL.frameGate.STEPS = api.STEPS; LL.frameGate.MOTION = api.MOTION; }
})(typeof window !== 'undefined' ? window : null, function (root) {
  'use strict';
  const STEPS = [60, 30, 20, 15, 10];
  const MOTION = { auto: 30, full: 60, saver: 15, off: 0 };
  function setting() {
    try { const m = root && root.localStorage.getItem('ll-motion'); return MOTION[m] != null ? m : 'auto'; } catch (e) { return 'auto'; }
  }
  function frameGate(opts) {
    const getMotion = (opts && opts.motion) || setting;
    let idx = 0, last = -1e9, prev = 0, avg = 16.7, calmSince = 0;
    const cap = () => MOTION[getMotion()] != null ? MOTION[getMotion()] : MOTION.auto;
    const floor = c => { let i = 0; while (i < STEPS.length - 1 && STEPS[i] > c) i++; return i; };
    const fps = () => { const c = cap(); return c ? Math.min(c, STEPS[Math.max(idx, floor(c))]) : 0; };
    return {
      fps, off: () => cap() === 0,
      reset() { prev = 0; },
      draw(ts) {
        if (prev) {
          avg += (Math.min(200, ts - prev) - avg) * 0.05;
          if (avg > 22 && idx < STEPS.length - 1) { idx = Math.max(idx, floor(cap())) + 1; if (idx > STEPS.length - 1) idx = STEPS.length - 1; avg = 16.7; calmSince = ts; }
          else if (avg < 18.5 && idx > 0 && ts - calmSince > 10000) { idx--; calmSince = ts; }
        }
        prev = ts;
        const f = fps();
        if (!f) return false;
        if (ts - last < 1000 / f - 1) return false;
        last = ts;
        return true;
      },
    };
  }
  return { frameGate, STEPS, MOTION };
});
