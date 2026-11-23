/* Lanloom — data paths: the "Real cables" view (DW3).

   Two views of the data paths map (world, full screen, phone, country sheet), switched with a two-part control:
     * Real cables (the default): ONLY lines, never an arc and never a dashed line. Every data path runs over infrastructure
       (paths_infra.js; the server computes the ways, lanloom/infraroute.py): sea cables, land lines, radio links, satellite hops.
       Equal strands are bundled and get thicker with the traffic; measured ways are drawn full, estimated ones lighter. The
       cables of the map lie faintly below. No traffic disappears: the sum of the map is the same in both views.
     * Rain: the arcs from the home country with travelling dots, unchanged.
   The choice is kept per device (localStorage `lanloom-paths-view`, like the period) and in the address: `#/paths?view=cables|rain`
   (also with a country: `#/paths/DE?view=rain`). With "reduce motion" no dots run in either view.
   Cable data: GET /api/paths/cables (OpenStreetMap, ODbL; bundled with the app or an administrator's import), loaded only when the
   view needs it. Attribution and "incomplete - community-mapped" stay visible beside the map whenever cables are drawn.
   Hooks: LL.pathsCables.view(), .set(v), .control(), .layer(svg, opts), .legendItems(), .attribution(), .line(points),
   .at(L, u), .pathLine(parts), .pathD(points), .runDots(stage, svg, streams), .settingsCard(s).
   Event `lanloom-paths-view` on document after every switch. No inline handler, colours only from tokens. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const NS = 'http://www.w3.org/2000/svg';
  const VIEWS = ['cables', 'rain'];
  const KEY = 'lanloom-paths-view';
  const JUMP = 400;                                  // a step longer than this (map units) is the date line: never drawn, never travelled
  const C = LL.pathsCables = {};
  const S = { view: 'cables', data: null, loading: null, failed: false, tip: null, html: null };
  try { const v = localStorage.getItem(KEY); if (VIEWS.includes(v)) S.view = v; } catch (e) { /* private mode */ }
  const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fmtDay = d => { try { return new Intl.DateTimeFormat(LL.locale ? LL.locale() : LL.lang, { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(d + 'T12:00:00Z')); } catch (e) { return d; } };
  const svgEl = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs || {}) if (attrs[k] != null) e.setAttribute(k, attrs[k]); return e; };

  // ------------------------------------------------------------------ view and address
  C.view = () => S.view;
  C.on = () => S.view === 'cables';
  /* the address wins once when a view opens (deep link), without changing the remembered choice */
  C.fromAddress = () => { const v = LL.query && LL.query.get('view'); if (VIEWS.includes(v)) S.view = v; return S.view; };
  function writeAddress() {
    const raw = (location.hash || '').replace(/^#\/?/, ''), q = raw.indexOf('?');
    const path = q >= 0 ? raw.slice(0, q) : raw;
    if (!/^paths(\/|$)/.test(path)) return;
    const params = new URLSearchParams(q >= 0 ? raw.slice(q + 1) : '');
    params.set('view', S.view);
    try { history.replaceState(history.state, '', LL.base + '#/' + path + '?' + params.toString()); } catch (e) { /* ignore */ }
    if (LL.query) LL.query.set('view', S.view);
  }
  C.sync = writeAddress;
  C.set = function (v) {
    if (!VIEWS.includes(v)) return;
    const same = v === S.view;
    S.view = v;
    try { localStorage.setItem(KEY, v); } catch (e) { /* private mode */ }      // also when a deep link showed it already: now it is chosen
    if (same) { writeAddress(); return; }
    hideTip();
    writeAddress();
    document.querySelectorAll('[data-pv]').forEach(b => { const on = b.dataset.pv === v; b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', String(on)); });
    if (v === 'cables') load();
    document.dispatchEvent(new CustomEvent('lanloom-paths-view', { detail: { view: v } }));
  };
  /* the two-part control: Real cables | Rain */
  C.control = function (cls) {
    return h('div', { class: 'seg pv-seg' + (cls ? ' ' + cls : ''), role: 'group', 'aria-label': t('cables.view.label') }, VIEWS.map(v =>
      h('button', { type: 'button', class: 'seg__btn' + (v === S.view ? ' is-active' : ''), 'data-pv': v, 'aria-pressed': String(v === S.view),
        title: t('cables.view.' + v + '.hint'), onclick: () => C.set(v) }, h('i', { class: 'pv-ico pv-ico--' + v, 'aria-hidden': 'true' }), t('cables.view.' + v))));
  };

  // ------------------------------------------------------------------ data (loaded on demand, never from a foreign server)
  function load() {
    if (S.data) return Promise.resolve(S.data);
    if (S.loading) return S.loading;
    S.loading = api('/api/paths/cables').then(d => {
      if (!d || !Array.isArray(d.sea)) throw new Error('format');
      S.data = d; S.html = null; S.failed = false;
      document.dispatchEvent(new CustomEvent('lanloom-paths-cables', { detail: {} }));
      return d;
    }).catch(() => { S.failed = true; return null; }).then(d => { S.loading = null; return d; });
    return S.loading;
  }
  C.load = load;
  C.data = () => S.data;
  C.reload = () => { S.data = null; S.html = null; return load(); };

  // ------------------------------------------------------------------ the layer
  /* Cables grouped into tiles (16 x 4 cells by their start point): a few long paths instead of hundreds of elements - every zoom step
     would otherwise lay out each path again, a small computer without GPU draws half as fast. Named sea cables get thin invisible
     hit paths of their own (hover and tap show name, operator and length). */
  function tiles(list) {
    const g = new Map();
    for (const e of list || []) {
      const m = /^M(-?[\d.]+)[ ,]?(-?[\d.]+)/.exec(e.d || ''), x = m ? parseFloat(m[1]) : 0, y = m ? parseFloat(m[2]) : 0;
      const k = Math.max(0, Math.min(15, Math.floor(x / 62.5))) * 4 + Math.max(0, Math.min(3, Math.floor(y / 110)));
      if (!g.has(k)) g.set(k, []);
      g.get(k).push(e.d);
    }
    return [...g.keys()].sort((a, b) => a - b).map(k => g.get(k).join(''));
  }
  function layerNodes() {
    const d = S.data;
    const root = svgEl('g', { class: 'cb-layer', 'aria-hidden': 'true' });
    const land = svgEl('g', { class: 'cb-land' }), sea = svgEl('g', { class: 'cb-sea' }), hit = svgEl('g', { class: 'cb-hits' });
    tiles(d.land).forEach(p => land.appendChild(svgEl('path', { d: p })));
    tiles(d.sea).forEach(p => sea.appendChild(svgEl('path', { d: p })));
    d.sea.forEach((e, i) => { if (e.n) hit.appendChild(svgEl('path', { class: 'cb-hit', d: e.d, 'data-cb': String(i) })); });
    root.append(land, sea, hit);
    return root;
  }
  /* Puts the cable layer into a map (or takes it out); `opts.before` = selector of the group the layer goes in front of. Called again
     after the data arrived. */
  C.layer = function (svg, opts) {
    if (!svg) return;
    opts = opts || {};
    svg.querySelectorAll('.cb-layer').forEach(e => e.remove());
    svg.classList.toggle('cb-on', C.on());
    if (!C.on()) return;
    const put = () => {
      if (!svg.isConnected && !opts.detached) return;
      if (!S.data || svg.querySelector('.cb-layer')) return;
      const before = opts.before ? svg.querySelector(opts.before) : null;
      svg.insertBefore(layerNodes(), before);
      if (opts.onReady) opts.onReady();
    };
    if (S.data) put(); else load().then(d => { if (d && C.on()) put(); });
  };

  // ------------------------------------------------------------------ legend and attribution
  /* the lines drawn right now (paths_infra.js), neutral patterns */
  C.legendItems = function (cls, arts) { return C.on() && LL.pathsInfra ? LL.pathsInfra.legendItems(cls, arts) : []; };
  /* "(c) OpenStreetMap contributors (ODbL) - incomplete, community-mapped; which cable a route used is always an estimate" - the
     words of the source come from the data itself */
  C.attribution = function (cls) {
    if (!C.on()) return null;
    const d = S.data;
    const a = (d && d.attribution) || null, url = (d && d.attribution_url) || null, lic = (d && d.license) || null, lurl = (d && d.license_url) || null;
    const date = d && d.date ? t('cables.attr.date', { date: fmtDay(d.date) }) : '';
    return h('p', { class: 'cb-attr ' + (cls || '') }, h('i', { class: 'cb-sw cb-sw--cable', 'aria-hidden': 'true' }),
      h('span', {}, t('cables.attr.title'), ': ',
        a ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, a) : t('cables.attr.loading'),
        lic ? [' (', h('a', { href: lurl, target: '_blank', rel: 'noopener noreferrer' }, lic), ')'] : null,
        openSources(d),
        ' — ', t('cables.attr.incomplete'), ' · ', t('infra.attr.estimated'), date ? ' · ' + date : ''));
  };
  /* the open sources of the second cable file (lines of national waters, cable facts; CC0): ", <source> (CC0 1.0)" each */
  function openSources(d, first) {
    const list = (d && Array.isArray(d.sources) ? d.sources : []).filter(x => x && x.short && x.url);
    return list.length ? list.map((x, k) => [k ? ', ' : (first == null ? ', ' : first), h('span', { class: 'cb-src' },
      h('a', { href: x.url, target: '_blank', rel: 'noopener noreferrer' }, x.short),
      x.license ? [' (', h('a', { href: x.license_url, target: '_blank', rel: 'noopener noreferrer' }, x.license), ')'] : null)]) : null;
  }
  C.openSources = openSources;
  document.addEventListener('lanloom-paths-cables', () => {
    document.querySelectorAll('.cb-attr').forEach(p => { const n = C.attribution(p.className.replace('cb-attr', '').trim()); if (n) p.replaceWith(n); });
  });

  // ------------------------------------------------------------------ tooltip (hover, focus, tap)
  /* The lines of a cable's tip. A missing operator (null, undefined, empty or the text "null") drops its line: replaceChildren turns a
     null child into the text "null" (lanloom#12), so only real nodes are returned. */
  const real = v => { const x = typeof v === 'string' ? v.trim() : ''; return x && !/^(null|undefined|none|nan)$/i.test(x) ? x : ''; };
  const num = v => (typeof v === 'number' && isFinite(v) && v > 0 ? v : 0);
  function tipNodes(e) {
    const op = real(e.op), own = real(e.own), yr = num(e.yr), cap = num(e.cap);
    const src = e.src === 'noaa' ? t(e.part ? 'cables.tip.source.noaa_part' : 'cables.tip.source.noaa') : t('cables.tip.source');
    return [h('b', {}, e.n || t('cables.unnamed')),
      h('span', {}, t('cables.tip.meta', { km: LL.fmtNum(e.km || 0, 0) })),
      op ? h('span', {}, t('cables.tip.operator', { operator: op })) : null,
      own && own !== op ? h('span', {}, t('cables.tip.owners', { owners: own })) : null,
      yr ? h('span', {}, t('cables.tip.year', { year: String(yr) })) : null,
      cap ? h('span', {}, t('cables.tip.capacity', { tbps: LL.fmtNum(cap, cap < 1 ? 2 : (cap < 10 ? 1 : 0)) })) : null,
      h('small', {}, src),
      e.wd ? h('small', {}, t('cables.tip.facts')) : null].filter(Boolean);
  }
  C.tipNodes = tipNodes;
  function hideTip() { if (S.tip) { S.tip.remove(); S.tip = null; } }
  function showTip(path, x, y) {
    const e = S.data && S.data.sea[Number(path.dataset.cb)];
    const stage = path.closest('.pw-stage, .pc-stage');
    if (!e || !stage) return;
    if (!S.tip || S.tip.parentNode !== stage) { hideTip(); S.tip = h('div', { class: 'cb-tip', role: 'tooltip' }); stage.appendChild(S.tip); }
    S.tip.replaceChildren(...tipNodes(e));
    const r = stage.getBoundingClientRect(), s = S.tip;
    s.style.left = Math.max(4, Math.min(x - r.left + 12, r.width - s.offsetWidth - 4)) + 'px';
    s.style.top = Math.max(4, Math.min(y - r.top + 14, r.height - s.offsetHeight - 4)) + 'px';
  }
  let away = 0;
  const target = ev => ev.target && ev.target.closest && ev.target.closest('.cb-hit');
  document.addEventListener('mouseover', ev => { const p = target(ev); if (p) { clearTimeout(away); showTip(p, ev.clientX, ev.clientY); } });
  document.addEventListener('mousemove', ev => { const p = target(ev); if (p && S.tip) showTip(p, ev.clientX, ev.clientY); });
  document.addEventListener('mouseout', ev => { if (target(ev)) hideTip(); });
  document.addEventListener('focusin', ev => { const p = target(ev); if (p) { const b = p.getBoundingClientRect(); showTip(p, b.left + b.width / 2, b.top + b.height / 2); } });
  document.addEventListener('focusout', ev => { if (target(ev)) hideTip(); });
  document.addEventListener('click', ev => {
    const p = target(ev);
    if (p) { clearTimeout(away); showTip(p, ev.clientX, ev.clientY); away = setTimeout(hideTip, 4000); } else if (S.tip && !(ev.target.closest && ev.target.closest('.cb-tip'))) hideTip();
  }, true);
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && S.tip) hideTip(); });
  window.addEventListener('scroll', () => { if (S.tip) hideTip(); }, true);

  // ------------------------------------------------------------------ lines for the paths and the dots
  /* points [[x, y], ...] -> {p, l (running length per point), len}; date-line jumps do not count */
  C.line = function (points) {
    const p = (points || []).filter(q => Array.isArray(q) && isFinite(q[0]) && isFinite(q[1]));
    const l = [0];
    for (let i = 1; i < p.length; i++) { const dx = p[i][0] - p[i - 1][0], dy = p[i][1] - p[i - 1][1]; l.push(l[i - 1] + (Math.abs(dx) > JUMP ? 0 : Math.hypot(dx, dy))); }
    return { p, l, len: l[l.length - 1] || 0 };
  };
  /* point at share u (0..1) of a line */
  C.at = function (L, u) {
    const goal = Math.max(0, Math.min(1, u)) * L.len, l = L.l, p = L.p;
    if (!p.length) return [0, 0];
    if (p.length === 1) return p[0];
    let a = 0, b = l.length - 1;
    while (b - a > 1) { const m = (a + b) >> 1; if (l[m] < goal) a = m; else b = m; }
    const seg = l[b] - l[a], f = seg > 0 ? (goal - l[a]) / seg : 0;
    return [p[a][0] + (p[b][0] - p[a][0]) * f, p[a][1] + (p[b][1] - p[a][1]) * f];
  };
  C.pathLine = parts => C.line((parts || []).reduce((a, x) => a.concat(a.length ? x.p.slice(1) : x.p), []));
  C.pathD = function (points) {
    let s = '', prev = null;
    for (const [x, y] of points) { s += (prev && Math.abs(x - prev[0]) <= JUMP ? 'L' : 'M') + x.toFixed(2) + ' ' + y.toFixed(2); prev = [x, y]; }
    return s;
  };
  // ------------------------------------------------------------------ dots on the lines (canvas, only while visible)
  /* streams: [{L, rx, tx, out, quiet}] - dots towards home (received, full colour) and away from it (sent, lighter); number and speed
     by bytes; at most ~360 dots per frame (no GPU: every dot costs), dots outside the view are not drawn. Self-throttling like the
     rain view: slow frames -> every 2nd/3rd frame, back after 10 s of smooth frames. Nothing at all with "reduce motion". */
  C.runDots = function (stage, svg, streams, opts) {
    opts = opts || {};
    const list = (streams || []).filter(x => !x.quiet && x.L && x.L.len > 0 && (x.rx + x.tx) > 0);
    if (reduced() || !list.length || !stage || !svg) return null;
    const W = opts.width || 1000;
    const cv = h('canvas', { class: 'pw-canvas cb-canvas', 'aria-hidden': 'true' });
    stage.appendChild(cv);
    const css = getComputedStyle(stage);
    const col = { out: css.getPropertyValue('--pw-dot-out').trim() || '#C98600', eu: css.getPropertyValue('--pw-dot-eu').trim() || '#0C7A6A' };
    const anim = { raf: 0, ro: null, io: null, visible: true, cv, stop: null };
    let scale = 1, dpr = 1, wiped = false;
    const gate = anim.gate = LL.frameGate();
    const size = () => {
      const r = stage.getBoundingClientRect(), rs = svg.getBoundingClientRect();
      dpr = Math.min(2, window.devicePixelRatio || 1);
      cv.style.left = (rs.left - r.left + stage.scrollLeft) + 'px'; cv.style.top = (rs.top - r.top + stage.scrollTop) + 'px';
      cv.style.width = rs.width + 'px'; cv.style.height = rs.height + 'px';
      cv.width = Math.max(1, Math.round(rs.width * dpr)); cv.height = Math.max(1, Math.round(rs.height * dpr));
      scale = rs.width / W;
    };
    size();
    anim.ro = new ResizeObserver(size); anim.ro.observe(stage);
    anim.io = new IntersectionObserver(e => { anim.visible = e[0].isIntersecting; }); anim.io.observe(stage);
    const max = Math.max(1, ...list.map(x => x.rx + x.tx));
    const count = b => (b > 0 ? Math.max(1, Math.min(5, Math.round(1 + 4 * Math.sqrt(b / max)))) : 0);
    const plan = list.map(x => { const a = Math.sqrt((x.rx + x.tx) / max);
      return { L: x.L, nr: count(x.rx), nt: count(x.tx), dur: (2600 + x.L.len * 9) * (1.25 - 0.5 * a), r: 1.3 + 1.4 * a, out: !!x.out }; });
    const BUDGET = 360, total = plan.reduce((a, p) => a + p.nr + p.nt, 0);
    if (total > BUDGET) { const f = BUDGET / total; plan.forEach(p => { p.nr = p.nr ? Math.max(1, Math.round(p.nr * f)) : 0; p.nt = p.nt ? Math.max(1, Math.round(p.nt * f)) : 0; }); }
    const frame = ts => {
      anim.raf = requestAnimationFrame(frame);
      if (!cv.isConnected) { anim.stop(); return; }
      if (document.hidden || !anim.visible) { gate.reset(); return; }
      if (!gate.draw(ts)) {
        if (gate.off() && !wiped) { wiped = true; cv.getContext('2d').clearRect(0, 0, cv.width, cv.height); }
        return;
      }
      wiped = false;
      const c = cv.getContext('2d'), vb = svg.viewBox.baseVal, kz = scale * W / vb.width;
      c.setTransform(1, 0, 0, 1, 0, 0);
      c.clearRect(0, 0, cv.width, cv.height);
      c.setTransform(dpr * kz, 0, 0, dpr * kz, -vb.x * dpr * kz, -vb.y * dpr * kz);
      const edge = 20 / kz, x0 = vb.x - edge, y0 = vb.y - edge, x1 = vb.x + vb.width + edge, y1 = vb.y + vb.height + edge;
      for (const p of plan) {
        const rr = p.r / Math.max(0.6, kz) * 0.9;
        for (let i = 0; i < p.nr + p.nt; i++) {
          const home = i < p.nr, n = home ? p.nr : p.nt, j = home ? i : i - p.nr;
          let u = ((ts / p.dur) + j / n + (home ? 0 : 0.5 / n)) % 1;
          if (home) u = 1 - u;
          const [x, y] = C.at(p.L, u);
          if (x < x0 || x > x1 || y < y0 || y > y1) continue;
          c.globalAlpha = (home ? 0.3 + 0.7 * Math.sin(Math.PI * u) : 0.2 + 0.45 * Math.sin(Math.PI * u));
          c.fillStyle = p.out ? col.out : col.eu;
          c.beginPath(); c.arc(x, y, home ? rr : rr * 0.85, 0, Math.PI * 2); c.fill();
        }
      }
      c.globalAlpha = 1;
    };
    anim.stop = () => {
      if (anim.raf) cancelAnimationFrame(anim.raf);
      anim.raf = 0;
      if (anim.ro) anim.ro.disconnect();
      if (anim.io) anim.io.disconnect();
      cv.remove();
    };
    anim.raf = requestAnimationFrame(frame);
    anim.dots = () => plan.reduce((a, p) => a + p.nr + p.nt, 0);
    return anim;
  };

  // ------------------------------------------------------------------ settings card (cable data: source, date, own import)
  /* Viewers see where the cables come from; an administrator may import an Overpass answer of their own (off until used - nothing is
     fetched for it, the file comes from this browser) and go back to the bundled data. */
  C.settingsCard = function () {
    const admin = LL.can('admin');
    const facts = h('div', { class: 'cb-set-facts' });
    const msg = h('div', { class: 'msg' });
    const file = h('input', { type: 'file', accept: '.json,application/json', class: 'cb-set-file', hidden: true });
    const pick = admin ? h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => file.click() }, t('cables.set.import')) : null;
    const reset = admin ? h('button', { class: 'btn btn--ghost', type: 'button', hidden: true, onclick: async () => {
      if (!window.confirm(t('cables.set.reset_confirm'))) return;
      LL.msg(msg, '');
      try { await api('/api/paths/cables/reset', { method: 'POST', body: {} }); LL.msg(msg, t('cables.set.reset_done'), 'ok'); C.reload(); refresh(); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('cables.set.reset')) : null;
    file.addEventListener('change', async () => {
      const f = file.files && file.files[0];
      file.value = '';
      if (!f) return;
      LL.msg(msg, t('cables.set.importing'));
      try {
        const head = Object.assign({ 'Content-Type': 'application/json', 'X-Lanloom': '1', 'Accept': 'application/json' }, LL.siteHeaders());
        const res = LL.siteChecked(await fetch(LL.url('/api/paths/cables/import'), { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: head, body: f }));
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(t(body.error || 'paths.cables.err.no_cables'));
        LL.msg(msg, t('cables.set.imported', { n: body.osm_sea || 0 }), 'ok');
        C.reload();
        refresh();
      } catch (e) { LL.msg(msg, e.message || LL.errText(e), 'error'); }
    });
    async function refresh() {
      let i;
      try { i = await api('/api/paths/cables/info'); } catch (e) { facts.replaceChildren(); return; }
      facts.replaceChildren(LL.facts([
        [t('cables.set.source'), i.source === 'import' ? t('cables.set.source.import') : t('cables.set.source.bundled')],
        [t('cables.set.date'), i.date ? fmtDay(i.date) : '–'],
        [t('cables.set.count'), t('cables.set.count_value', { sea: LL.fmtNum(i.sea || 0, 0), named: LL.fmtNum(i.named_sea || 0, 0), land: LL.fmtNum(i.land || 0, 0) })],
        [t('cables.set.licence'), h('span', {}, h('a', { href: i.attribution_url, target: '_blank', rel: 'noopener noreferrer' }, i.attribution), ' (',
          h('a', { href: i.license_url, target: '_blank', rel: 'noopener noreferrer' }, i.license), ')')],
        (i.sources || []).length ? [t('cables.set.open'), h('span', {}, t('cables.set.open_value', { lines: LL.fmtNum(i.open_sea || 0, 0),
          facts: LL.fmtNum(i.facts || 0, 0), retired: LL.fmtNum(i.retired || 0, 0) }), openSources({ sources: i.sources }, ' '))] : null,
        i.error ? [t('cables.set.problem'), h('span', { class: 'nf-bad' }, t(i.error))] : null]));
      if (reset) reset.hidden = i.source !== 'import';
    }
    refresh();
    return h('section', { class: 'card settings-card cb-set', id: 'settings-cables' }, h('h2', {}, t('cables.set.title')),
      h('p', { class: 'muted' }, t('cables.set.text')),
      facts,
      admin ? h('p', { class: 'muted fineprint' }, t('cables.set.import_hint', { mb: 40 })) : null,
      msg, file,
      admin ? h('div', { class: 'btn-row btn-row--wrap' }, pick, reset) : null);
  };

  // ------------------------------------------------------------------ command palette
  LL.registerCommand({ id: 'paths-view-cables', group: 'views', order: 42, label: () => t('cmdk.paths_cables'), keywords: () => t('cmdk.kw.paths_cables'),
    run: () => { C.set('cables'); LL.go('#/paths?view=cables'); } });
  LL.registerCommand({ id: 'paths-view-rain', group: 'views', order: 43, label: () => t('cmdk.paths_rain'), keywords: () => t('cmdk.kw.paths_rain'),
    run: () => { C.set('rain'); LL.go('#/paths?view=rain'); } });
})();
