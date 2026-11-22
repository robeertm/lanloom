/* Axis block for every history chart: the highest value of the chosen range with head room, fine steps (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8 x 10^k),
   and a single spike (a kettle, one burst of traffic) does not press the rest of the range onto the zero line: when the highest value lies far
   above the 98th percentile the axis follows the percentile, the spike is cut off at the top and marked. Percent charts stay 0-100. */
(function () {
  'use strict';
  const STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
  const A = {};
  /* the next fine step at or above v (v > 0) */
  A.niceCeil = function (v) {
    if (!(v > 0)) return 1;
    const k = Math.pow(10, Math.floor(Math.log10(v))), f = v / k;
    for (const s of STEPS) if (f <= s * (1 + 1e-9)) return Math.round(s * k * 1e12) / 1e12;
    return 10 * k;
  };
  /* percentile q (0..1) of a list of numbers, linear between neighbours */
  A.percentile = function (vals, q) {
    const a = vals.filter(Number.isFinite).sort((x, y) => x - y);
    if (!a.length) return 0;
    const i = (a.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i);
    return a[lo] + (a[hi] - a[lo]) * (i - lo);
  };
  /* scale(values, opts) -> { lo, hi, top, clipped }.  opts.pct: percent chart (0-100); opts.minZero (default true for non-negative data) */
  A.scale = function (vals, opts) {
    const o = opts || {}, v = (vals || []).filter(Number.isFinite);
    if (o.pct) return { lo: 0, hi: 100, top: v.length ? Math.max(...v) : 0, clipped: false };
    if (!v.length) return { lo: 0, hi: 1, top: 0, clipped: false };
    const max = Math.max(...v), min = Math.min(...v), p98 = A.percentile(v, 0.98);
    let ceil = max, clipped = false;
    if (max > 0 && p98 > 0 && max > p98 * 2.5 && v.length >= 8) { ceil = p98; clipped = true; }       // a lone spike: the axis follows the percentile
    else if (max > 0 && p98 <= 0 && max > 0 && v.length >= 8 && v.filter(x => x > 0).length <= v.length * 0.02) { ceil = max; }
    let hi, lo;
    if (min >= 0) {
      lo = min <= ceil * 0.6 ? 0 : min - (ceil - min) * 0.08;
      hi = ceil > 0 ? A.niceCeil(ceil * 1.05) : 1;
    } else {
      const span = (ceil - min) || 1; lo = min - span * 0.08; hi = ceil + span * 0.08;
    }
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    return { lo, hi, top: max, clipped };
  };
  /* tick values for n intervals (n + 1 values), the middle ones dropped on a very small chart (small = true: only both ends) */
  A.ticks = function (sc, n, small) {
    if (small) return [sc.lo, sc.hi];
    const out = [];
    for (let i = 0; i <= n; i++) out.push(sc.lo + (sc.hi - sc.lo) * i / n);
    return out;
  };
  if (typeof module === 'object' && module.exports) module.exports = A;
  else { window.Lanloom = window.Lanloom || {}; window.Lanloom.axis = A; }
})();
