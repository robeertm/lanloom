/* Lanloom — phones you can use with a finger (O12): fingertip-sized hit areas and the keyboard guard.

   1. Hit areas. Every control smaller than 44 x 44 px (chips, icon buttons, close crosses, filters, small links) gets an invisible, larger
      hit area on a phone — a pseudo element of the control itself, so nothing looks different. The area never reaches further than half the gap to
      the next control and never through a clipping parent, so two neighbours never fight over the same fingertip. It is measured again for every
      node that appears later (live redraws, new sheets), and dropped when the window becomes wide. Where the gap is too small for 44 px the
      design has to make room (css/handy.css, the page's own rules); the phone walk of the test bench (shots/phone_usable.py) names those cases.
   2. Keyboard guard. A phone shrinks only the VISUAL viewport when the keyboard opens; a layer that fills the screen keeps the full height and
      its lower part, with the field in it, lies behind the keyboard. While the keyboard is up, the nearest screen-filling fixed layer around the
      focused field follows height and position of window.visualViewport (class ll-kb-layer, variables --ll-vvh / --ll-vvt on <html>), the
      page carries html.ll-kb-open (the page's own rules may make room for the history above the field), and a field outside the visible
      part is scrolled into view. Never tie such a rule to :focus of the field: a tap on "Send" takes the focus away and the layout jumps under the finger.

   Nothing here talks to the network and nothing is stored. */
(function () {
  'use strict';
  const PHONE = 700;                 // px: up to here the phone layout applies (the same limit as the CSS and the sheets)
  const MIN = 44;                    // px: fingertip size of the platform guidelines
  const KEYBOARD_MIN = 120;          // px: a smaller difference between window and visible area is a bar, not a keyboard
  const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab], [role=link], [role=switch], [role=menuitem], [role=menuitemradio], [role=option], [tabindex="0"]';
  const root = document.documentElement;
  const phone = () => window.innerWidth <= PHONE;

  // ------------------------------------------------------------------ hit areas
  const grown = new Set();           // controls that carry the class right now
  let queue = [];
  let timer = 0;

  function visible(el) {
    if (!el.isConnected || el.closest('svg, canvas, [hidden], [inert]')) return false;
    if (el.disabled) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0.5 && r.height > 0.5 && getComputedStyle(el).visibility !== 'hidden';
  }
  function clear(el) {
    el.classList.remove('ll-tz', 'll-tz-a', 'll-tz-b', 'll-tz-rel');
    el.style.removeProperty('--tz-t'); el.style.removeProperty('--tz-r'); el.style.removeProperty('--tz-b'); el.style.removeProperty('--tz-l');
    grown.delete(el);
  }
  // the room around a control: half the gap to its neighbours, the distance to a clipping parent, the window edge
  function room(el, r, others) {
    const o = { t: Infinity, r: Infinity, b: Infinity, l: Infinity };
    for (const n of others) {
      if (n.el === el || el.contains(n.el) || n.el.contains(el)) continue;
      const q = n.r;
      const overV = q.top < r.bottom && q.bottom > r.top, overH = q.left < r.right && q.right > r.left;
      if (overV && q.right <= r.left + 0.5) o.l = Math.min(o.l, (r.left - q.right) / 2);
      if (overV && q.left >= r.right - 0.5) o.r = Math.min(o.r, (q.left - r.right) / 2);
      if (overH && q.bottom <= r.top + 0.5) o.t = Math.min(o.t, (r.top - q.bottom) / 2);
      if (overH && q.top >= r.bottom - 0.5) o.b = Math.min(o.b, (q.top - r.bottom) / 2);
    }
    o.l = Math.min(o.l, r.left); o.r = Math.min(o.r, window.innerWidth - r.right);
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const q = a.getBoundingClientRect();
      o.l = Math.min(o.l, r.left - q.left); o.r = Math.min(o.r, q.right - r.right); o.t = Math.min(o.t, r.top - q.top); o.b = Math.min(o.b, q.bottom - r.bottom);
    }
    for (const k of ['t', 'r', 'b', 'l']) o[k] = Math.max(0, Math.floor(o[k] * 2) / 2);
    return o;
  }
  function pass() {
    timer = 0;
    const todo = queue; queue = [];
    if (!phone() || document.hidden) return;
    // read first (all rectangles), write after: one layout, not one per control
    const all = [...document.querySelectorAll(SEL)].filter(el => visible(el));
    const rects = all.filter(el => !el.matches('.skip-link')).map(el => ({ el, r: el.getBoundingClientRect() }));      // the skip link waits above the window: no neighbour
    const pending = new Set(todo.length ? todo.flatMap(n => n.nodeType === 1 ? [n, ...n.querySelectorAll(SEL)] : []) : all);
    const writes = [];
    for (const { el, r } of rects) {
      if (!pending.has(el)) continue;
      if (el.matches('input[type=checkbox], input[type=radio]') && el.labels && el.labels.length) continue;     // the label is the target
      const nx = Math.max(0, (MIN + 1 - r.width) / 2), ny = Math.max(0, (MIN + 1 - r.height) / 2);      // one px over: layout rounds to device pixels
      if (r.width >= MIN && r.height >= MIN) { if (grown.has(el)) writes.push([el, null]); continue; }
      if (nx === 0 && ny === 0) { if (grown.has(el)) writes.push([el, null]); continue; }
      const cs = getComputedStyle(el);
      const free = (e, p) => getComputedStyle(e, p).content === 'none';
      let which = null;
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') {
        // a pseudo element that is ours does not count as taken (else the area would swap between ::after and ::before on every pass)
        which = el.classList.contains('ll-tz-a') || free(el, '::after') ? 'a' : (el.classList.contains('ll-tz-b') || free(el, '::before') ? 'b' : null);
      }
      if (!which) { writes.push([el, null]); continue; }          // clipped by itself or both pseudo elements are in use: the page's own rules make room
      const o = room(el, r, rects);
      writes.push([el, { which, rel: el.classList.contains('ll-tz-rel') || cs.position === 'static', t: Math.min(ny, o.t), b: Math.min(ny, o.b), l: Math.min(nx, o.l), r: Math.min(nx, o.r) }]);
    }
    for (const [el, w] of writes) {
      if (!w) { if (grown.has(el)) clear(el); continue; }
      const val = ['t', 'r', 'b', 'l'].map(k => w[k] + 'px');
      const same = grown.has(el) && el.classList.contains(w.which === 'a' ? 'll-tz-a' : 'll-tz-b') && el.classList.contains('ll-tz-rel') === w.rel
        && ['t', 'r', 'b', 'l'].every((k, i) => el.style.getPropertyValue('--tz-' + k) === val[i]);
      if (same) continue;                                      // nothing changed: no style churn on every live redraw
      el.classList.add('ll-tz', w.which === 'a' ? 'll-tz-a' : 'll-tz-b');
      el.classList.remove(w.which === 'a' ? 'll-tz-b' : 'll-tz-a');
      el.classList.toggle('ll-tz-rel', w.rel);
      ['t', 'r', 'b', 'l'].forEach((k, i) => el.style.setProperty('--tz-' + k, val[i]));
      grown.add(el);
    }
  }
  function schedule(nodes) {
    if (nodes) queue.push(...nodes);
    if (timer) return;
    timer = window.setTimeout(() => { (window.requestAnimationFrame || (f => f()))(pass); }, 90);
  }
  function everything() {
    if (!phone()) { grown.forEach(clear); return; }
    queue = [];
    schedule();
  }
  // new nodes are measured at once; any change of the page (also text that makes a neighbour wider or narrower) measures everything again
  // a moment after the last change, so the areas of two neighbours never overlap after a live redraw
  let settle = 0;
  new MutationObserver(ms => {
    if (!phone()) return;
    const nodes = [];
    for (const m of ms) m.addedNodes.forEach(n => { if (n.nodeType === 1) nodes.push(n); });
    if (nodes.length) schedule(nodes);
    if (!settle) settle = window.setTimeout(() => { settle = 0; schedule(); }, 1200);      // at most one full pass per 1.2 s, however busy the live redraws are
  }).observe(document.documentElement, { childList: true, characterData: true, subtree: true });
  window.addEventListener('resize', everything);
  window.addEventListener('orientationchange', everything);
  window.addEventListener('hashchange', () => schedule());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(); });
  window.addEventListener('load', everything);
  schedule();

  // ------------------------------------------------------------------ keyboard guard
  const vv = window.visualViewport;
  let layer = null;
  const fieldOf = el => (el && el.matches && el.matches('input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=button]):not([type=submit]):not([type=file]), textarea, select, [contenteditable=""], [contenteditable="true"]')) ? el : null;
  // the nearest fixed layer that fills the window: that is the one the keyboard hides
  function layerOf(field) {
    for (let a = field.parentElement; a && a !== document.body; a = a.parentElement) {
      if (getComputedStyle(a).position !== 'fixed') continue;
      const r = a.getBoundingClientRect();
      if (r.width >= window.innerWidth - 2 && r.height >= window.innerHeight * 0.5) return a;
    }
    return null;
  }
  function release() {
    if (layer) layer.classList.remove('ll-kb-layer');
    layer = null;
    root.classList.remove('ll-kb-open');
    root.style.removeProperty('--ll-vvh'); root.style.removeProperty('--ll-vvt');
  }
  function guard() {
    if (!vv || !phone()) { release(); return; }
    const hidden = window.innerHeight - vv.height - vv.offsetTop;      // what lies behind the keyboard
    const field = fieldOf(document.activeElement);
    if (hidden < KEYBOARD_MIN || !field) { if (hidden < KEYBOARD_MIN) release(); return; }
    root.style.setProperty('--ll-vvh', Math.round(vv.height) + 'px');
    root.style.setProperty('--ll-vvt', Math.round(vv.offsetTop) + 'px');
    root.classList.add('ll-kb-open');
    const l = layerOf(field);
    if (l !== layer) { if (layer) layer.classList.remove('ll-kb-layer'); layer = l; }
    if (layer) layer.classList.add('ll-kb-layer');
    // a field outside the visible part comes into view (the phone does that for the page itself, not for a layer that follows the viewport)
    window.requestAnimationFrame(() => {
      const f = fieldOf(document.activeElement); if (!f) return;
      const r = f.getBoundingClientRect(), top = vv.offsetTop + 8, bottom = vv.offsetTop + vv.height - 8;
      if (r.bottom > bottom || r.top < top) { try { f.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (e) { /* old engine */ } }
    });
  }
  if (vv) { vv.addEventListener('resize', guard); vv.addEventListener('scroll', guard); }
  document.addEventListener('focusin', () => window.setTimeout(guard, 0));
  document.addEventListener('focusout', () => window.setTimeout(guard, 60));
  window.addEventListener('resize', guard);
})();
