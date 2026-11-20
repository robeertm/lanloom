/* Lanloom — sheets you can drag (phones above all), anchors between full sheets, pull to refresh.

   LL.sheets.register(id, {title(arg), render(body, arg, sheet)})   a kind of sheet, e.g. 'changes', 'more', 'lane'
   LL.sheets.open(id, arg)                                          opens it; the address gets ?sheet=<id>[:<arg>] (back key closes it)
   LL.sheets.close()                                                closes the top sheet (one step back)
   LL.sheets.top()                                                  {id, arg} of the open sheet or null
   LL.pullToRefresh(scroller, run)                                  pull down at the top of a scroller: run() (returns a promise)

   A sheet sits at the bottom of a phone in two heights (half, full) and moves with the finger: drag the handle up for the full
   height, down to the half height, further down to close. On a wide screen it is a panel at the right edge (no dragging).
   A link from one full sheet to another (an anchor) follows in both directions: the address carries the sheet, so the back key
   returns to the sheet before and the forward key opens the next one again; a cold start with ?sheet=... opens it. The router
   asks LL.holdRoute before it draws the view again; a step that only changes the sheet is handled here and draws nothing below. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t } = LL;
  const PHONE = 700;                 // px: up to here a sheet sits at the bottom and can be dragged
  const HALF = 0.55;                 // share of the window a half sheet covers
  const CLOSE_PULL = 0.28;           // dragged below this share of its height: the sheet closes
  const kinds = new Map();
  const S = { el: null, id: null, arg: null, view: null, height: 'half', drag: null };

  const phone = () => window.innerWidth <= PHONE;
  const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ------------------------------------------------------------------ the address: #/<view>[/<part>]?...&sheet=<id>[:<arg>]
  function split(hash) {
    const raw = String(hash || '').replace(/^#\/?/, '');
    const q = raw.indexOf('?');
    const path = q >= 0 ? raw.slice(0, q) : raw;
    const params = new URLSearchParams(q >= 0 ? raw.slice(q + 1) : '');
    const sheet = params.get('sheet');
    params.delete('sheet');
    const rest = params.toString();
    return { base: '#/' + path + (rest ? '?' + rest : ''), sheet };
  }
  function parseSheet(v) {
    if (!v) return null;
    const i = v.indexOf(':');
    const id = i >= 0 ? v.slice(0, i) : v;
    return kinds.has(id) ? { id, arg: i >= 0 ? v.slice(i + 1) : null } : null;
  }
  function withSheet(base, id, arg) {
    const v = id + (arg != null && arg !== '' ? ':' + arg : '');
    return base + (base.indexOf('?') >= 0 ? '&' : '?') + 'sheet=' + encodeURIComponent(v);
  }

  // ------------------------------------------------------------------ drawing
  function setHeight(which, animate) {
    S.height = which;
    if (!S.el) return;
    const card = S.el.querySelector('.sheet');
    card.classList.toggle('is-full', which === 'full');
    card.style.transition = animate && !reduced() ? '' : 'none';
    card.style.transform = '';
    S.el.dataset.height = which;
    const grip = S.el.querySelector('.sheet__grip');
    if (grip) grip.setAttribute('aria-label', t(which === 'full' ? 'sheet.grip.half' : 'sheet.grip.full'));
  }

  function draw(id, arg) {
    const kind = kinds.get(id);
    if (!kind) return;
    S.id = id; S.arg = arg;
    if (!S.el) build();
    const title = typeof kind.title === 'function' ? kind.title(arg) : t(kind.title);
    const body = S.el.querySelector('.sheet__body');
    S.el.querySelector('.sheet__title').textContent = title;
    S.el.querySelector('.sheet').setAttribute('aria-label', title);
    S.el.dataset.sheet = id;
    body.replaceChildren(h('div', { class: 'skeleton' }));
    body.scrollTop = 0;
    if (kind.full) setHeight('full', false);
    Promise.resolve(kind.render(body, arg, api)).catch(e => { body.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); });
  }

  function build() {
    const grip = h('button', { class: 'sheet__grip', type: 'button', onclick: () => setHeight(S.height === 'full' ? 'half' : 'full', true) }, h('i', { 'aria-hidden': 'true' }));
    const head = h('header', { class: 'sheet__head' }, grip,
      h('h2', { class: 'sheet__title' }),
      h('button', { class: 'icon-btn sheet__close', type: 'button', 'aria-label': t('detail.close'), onclick: () => api.close() }, '✕'));
    const card = h('section', { class: 'sheet', role: 'dialog', 'aria-modal': 'false' }, head, h('div', { class: 'sheet__body' }));
    const scrim = h('div', { class: 'sheet-scrim', onclick: () => api.close() });
    S.el = h('div', { class: 'sheet-host' }, scrim, card);
    document.body.appendChild(S.el);
    dragging(head, card);
    setHeight(phone() ? 'half' : 'full', false);
    requestAnimationFrame(() => { if (S.el) S.el.classList.add('is-open'); });
    document.addEventListener('keydown', onKey);
    S.opener = document.activeElement;
    setTimeout(() => { const c = S.el && S.el.querySelector('.sheet__close'); if (c) c.focus({ preventScroll: true }); }, 0);
  }

  function teardown() {
    if (!S.el) return;
    const el = S.el;
    S.el = null; S.id = null; S.arg = null;
    document.removeEventListener('keydown', onKey);
    el.classList.remove('is-open');
    setTimeout(() => el.remove(), reduced() ? 0 : 220);
    if (S.opener && S.opener.isConnected) { try { S.opener.focus({ preventScroll: true }); } catch (e) { /* gone */ } }
  }
  function onKey(ev) {
    if (ev.key === 'Escape' && S.el && !document.querySelector('.modal')) { ev.preventDefault(); api.close(); }
  }

  // the finger moves the sheet; the head (handle and title) is the place to grab it, the body scrolls
  function dragging(head, card) {
    head.addEventListener('pointerdown', ev => {
      if (!phone() || ev.button > 0 || ev.target.closest('.sheet__close')) return;
      S.drag = { y0: ev.clientY, dy: 0, h: card.getBoundingClientRect().height, id: ev.pointerId, moved: false };
    });
    head.addEventListener('pointermove', ev => {
      const d = S.drag;
      if (!d || ev.pointerId !== d.id) return;
      d.dy = ev.clientY - d.y0;
      if (!d.moved && Math.abs(d.dy) > 4) {
        d.moved = true;                       // a real drag: from now on the head keeps the finger (a tap stays a click on the handle)
        card.style.transition = 'none';
        try { head.setPointerCapture(ev.pointerId); } catch (e) { /* older engines */ }
      }
      if (!d.moved) return;
      // up from the half height grows the sheet (shown as a smaller offset), down moves it out of the window
      const off = S.height === 'half' ? Math.max(-d.h * 0.6, d.dy) : Math.max(0, d.dy);
      card.style.transform = 'translateY(' + Math.round(off) + 'px)';
    });
    const end = ev => {
      const d = S.drag;
      if (!d || (ev && ev.pointerId !== d.id)) return;
      S.drag = null;
      S.dragEnded = d.moved;
      setTimeout(() => { S.dragEnded = false; }, 0);
      card.style.transition = '';
      if (!d.moved) { card.style.transform = ''; return; }
      if (d.dy > d.h * CLOSE_PULL && (S.height === 'half' || d.dy > d.h * 0.5)) { card.style.transform = 'translateY(100%)'; api.close(); return; }
      if (d.dy > 60 && S.height === 'full') { setHeight('half', true); return; }
      if (d.dy < -60) { setHeight('full', true); return; }
      card.style.transform = '';
    };
    head.addEventListener('pointerup', end);
    head.addEventListener('pointercancel', end);
    head.querySelector('.sheet__grip').addEventListener('click', ev => { if (S.dragEnded) { ev.preventDefault(); ev.stopImmediatePropagation(); } }, true);
  }

  // ------------------------------------------------------------------ the interface
  const api = {
    register(id, kind) { kinds.set(id, kind); },
    // opens a sheet; from a full sheet the next one opens full as well (an anchor), the back key returns
    open(id, arg) {
      if (!kinds.has(id)) return;
      const parts = split(location.hash);
      const target = withSheet(parts.base, id, arg);
      if (location.hash !== target) history.pushState({ llSheet: true }, '', LL.base + target);
      S.view = parts.base;
      draw(id, arg == null ? null : String(arg));
    },
    close() {
      if (!S.el) return;
      const parts = split(location.hash);
      if (parts.sheet && history.state && history.state.llSheet) { history.back(); return; }   // the sheet was opened here: one step back
      if (parts.sheet) history.replaceState(null, '', LL.base + parts.base);
      teardown();
    },
    top: () => (S.el ? { id: S.id, arg: S.arg } : null),
    height: () => (S.el ? S.height : null),
    setHeight: which => setHeight(which === 'full' ? 'full' : 'half', true),
    // the view that owns the sheets was drawn: open the sheet the address names (cold start, reload, deep link)
    fromAddress() {
      const p = split(location.hash);
      S.view = p.base;
      const want = parseSheet(p.sheet);
      if (want) draw(want.id, want.arg); else teardown();
    },
    // the view goes away
    reset() { teardown(); S.view = null; }
  };
  LL.sheets = api;

  // a step in the history that only changes the sheet of the same view: follow it here, nothing below is drawn again
  const before = LL.holdRoute;
  LL.holdRoute = function () {
    if (S.view != null) {
      const p = split(location.hash);
      if (p.base === S.view) {
        const want = parseSheet(p.sheet);
        if (!want) teardown();
        else if (!S.el || want.id !== S.id || want.arg !== S.arg) draw(want.id, want.arg);
        return true;
      }
    }
    return before ? before() : false;
  };

  // ------------------------------------------------------------------ pull to refresh
  LL.pullToRefresh = function (scroller, run) {
    const THRESHOLD = 72;
    const tip = h('div', { class: 'ptr', 'aria-hidden': 'true' }, h('i', { class: 'ptr__spin' }), h('span', { class: 'ptr__text' }));
    scroller.prepend(tip);
    let y0 = null, dy = 0, busy = false;
    const text = tip.querySelector('.ptr__text');
    const show = px => {
      tip.style.height = Math.round(px) + 'px';
      tip.classList.toggle('is-ready', px >= THRESHOLD);
      text.textContent = t(busy ? 'ptr.busy' : px >= THRESHOLD ? 'ptr.release' : 'ptr.pull');
    };
    scroller.addEventListener('touchstart', ev => {
      if (busy || scroller.scrollTop > 0 || ev.touches.length !== 1 || document.querySelector('.sheet-host')) { y0 = null; return; }
      y0 = ev.touches[0].clientY; dy = 0;
    }, { passive: true });
    scroller.addEventListener('touchmove', ev => {
      if (y0 == null) return;
      dy = ev.touches[0].clientY - y0;
      if (dy <= 0) { show(0); return; }
      show(Math.min(110, dy * 0.5));
    }, { passive: true });
    const finish = async () => {
      if (y0 == null) return;
      y0 = null;
      if (dy * 0.5 < THRESHOLD) { show(0); return; }
      busy = true; tip.classList.add('is-busy'); show(48);
      try { await run(); } catch (e) { /* the view says what went wrong */ }
      busy = false; tip.classList.remove('is-busy'); show(0);
    };
    scroller.addEventListener('touchend', finish);
    scroller.addEventListener('touchcancel', () => { y0 = null; show(0); });
    return { refresh: async () => { if (busy) return; busy = true; tip.classList.add('is-busy'); show(48); try { await run(); } finally { busy = false; tip.classList.remove('is-busy'); show(0); } } };
  };
})();
