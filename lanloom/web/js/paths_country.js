/* Lanloom — data paths: the country detail sheet (DW1).

   Click a country (map or list) and a large sheet opens above the view: the map zoomed to the country (fine base map 1:50m,
   loaded on demand from the app's own files), key figures, history, cities, network operators, devices and destinations.
   Data: GET /api/paths/country/<CC>?window=24h|7d|30d. Only country, city (the peer's, approximate), operator, device and bytes,
   never an address. Cities and operators exist only with the option switched on (Settings > Data paths); without it the sheet
   still shows everything about the country and says so.
   Sum rule: the bubbles of the map, the device list and the header add up to the same number; traffic without a city is drawn
   as an approximate (dashed) bubble, never dropped.
   Closing: button, Esc, click beside, the back gesture (one history entry). Deep link: #/paths/<ISO> (with ?view=cables|rain).
   Real cables (DW3, the default view): ONLY lines (paths_infra.js). Per bubble the way over the infrastructure up to the first hub of
   the country (else the first point inside), on over the country's inland net to the hub nearest the bubble and a short feeder to the
   bubble; satellite networks run from the earth station straight to the bubble; a chosen route lights up. The Rain view is the map as
   before. The sum of the bubbles stays the same in both views. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const NS = 'http://www.w3.org/2000/svg';
  const PERIODS = ['24h', '7d', '30d'];
  const COARSE = '(pointer: coarse)';
  const MAX_Z = 64;                                        // how far the sheet's map zooms in (the world map stops at 16)
  const Z = { el: null, cc: null, window: '24h', d: null, loading: false, error: null, pushed: false, timer: 0, showAll: false, zv: null, cleanup: null, closing: 0 };
  const C = LL.pathsCountry = {};
  const phone = () => window.innerWidth < 700;
  const coarsePointer = () => !!(window.matchMedia && window.matchMedia(COARSE).matches);
  const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const pct = x => { const v = x * 100; return (v > 0 && v < 1 ? LL.fmtNum(v, 1) : LL.fmtNum(Math.round(v), 0)) + ' %'; };
  const name = cc => (LL.countryName ? LL.countryName(cc) : cc);
  const svgEl = (tag, attrs, kids) => {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
    (kids || []).forEach(k => k && el.appendChild(k));
    return el;
  };
  const titled = (el, text) => { if (text) el.appendChild(svgEl('title', {}, [document.createTextNode(text)])); return el; };
  const world = () => LL.worldmap || { w: 1000, h: 438, countries: {}, points: {} };
  const labelPoint = cc => { const m = world(), c = m.countries[cc]; return c ? [c.x, c.y] : (m.points[cc] || null); };
  const place = (lon, lat) => (LL.worldmap && LL.worldmap.project ? LL.worldmap.project(lon, lat) : null);
  const CB = () => LL.pathsCables || null;
  const cablesOn = () => !!(CB() && CB().on());

  // ------------------------------------------------------------------ files loaded on demand (the app's own, never a foreign server)
  let fineLoad = null;
  function loadFine() {
    if (fineLoad) return fineLoad;
    const one = src => new Promise(resolve => {
      const s = document.createElement('script');
      s.src = LL.url('js/' + src);
      s.onload = () => resolve(true);
      s.onerror = () => resolve(false);
      document.head.appendChild(s);
    });
    fineLoad = Promise.all([one('worldmap_fine.js'), one('worldplaces.js')]).then(() => !!(LL.worldmapFine && LL.worldmapFine.countries));
    return fineLoad;
  }

  // ------------------------------------------------------------------ the part of the map to start with
  /* Rectangles of the sub-shapes of a path (M absolute, l/h/v relative, z); the start view is the largest piece plus the pieces
     close to it (islands), so the USA appear without Hawaii and France without its far overseas parts. */
  function pieces(d) {
    const out = [], re = /([MmLlHhVvZz])|(-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?)/g;
    let cmd = null, x = 0, y = 0, sx = 0, sy = 0, num = [], cur = null, m;
    const pt = (px, py) => { x = px; y = py; if (!cur) { cur = { x0: x, y0: y, x1: x, y1: y }; out.push(cur); } cur.x0 = Math.min(cur.x0, x); cur.y0 = Math.min(cur.y0, y); cur.x1 = Math.max(cur.x1, x); cur.y1 = Math.max(cur.y1, y); };
    const flow = () => {
      while (num.length) {
        if (cmd === 'M') { cur = null; pt(num.shift(), num.shift()); sx = x; sy = y; cmd = 'L'; }
        else if (cmd === 'm') { cur = null; pt(x + num.shift(), y + num.shift()); sx = x; sy = y; cmd = 'l'; }
        else if (cmd === 'L') pt(num.shift(), num.shift());
        else if (cmd === 'l') pt(x + num.shift(), y + num.shift());
        else if (cmd === 'H') pt(num.shift(), y);
        else if (cmd === 'h') pt(x + num.shift(), y);
        else if (cmd === 'V') pt(x, num.shift());
        else if (cmd === 'v') pt(x, y + num.shift());
        else num.shift();
        if (num.length === 1 && 'MmLl'.includes(cmd)) num = [];
      }
    };
    while ((m = re.exec(d || ''))) {
      if (m[1]) { flow(); cmd = m[1]; if (cmd === 'z' || cmd === 'Z') { x = sx; y = sy; cur = null; } }
      else { num.push(parseFloat(m[2])); if (('MmLl'.includes(cmd) && num.length >= 2) || ('HhVv'.includes(cmd) && num.length >= 1)) flow(); }
    }
    flow();
    return out;
  }
  function frame(cc) {
    const m = world(), c = m.countries[cc];
    if (!c) { const p = m.points[cc]; return p ? { x0: p[0] - 3, y0: p[1] - 3, x1: p[0] + 3, y1: p[1] + 3 } : null; }
    const ps = pieces(c.d).filter(r => r.x1 > r.x0 || r.y1 > r.y0);
    if (!ps.length) return null;
    const area = r => (r.x1 - r.x0) * (r.y1 - r.y0);
    ps.sort((a, b) => area(b) - area(a));
    const g = ps[0], diag = Math.hypot(g.x1 - g.x0, g.y1 - g.y0), cx = (g.x0 + g.x1) / 2, cy = (g.y0 + g.y1) / 2, r = Object.assign({}, g);
    for (const q of ps.slice(1)) {
      if (area(q) < area(g) * 0.004) continue;
      if (Math.hypot((q.x0 + q.x1) / 2 - cx, (q.y0 + q.y1) / 2 - cy) > diag * 0.6) continue;
      r.x0 = Math.min(r.x0, q.x0); r.y0 = Math.min(r.y0, q.y0); r.x1 = Math.max(r.x1, q.x1); r.y1 = Math.max(r.y1, q.y1);
    }
    return r;
  }
  function baseView(cc, ratio, pts) {
    const m = world(), r = Object.assign({}, frame(cc) || { x0: 0, y0: 0, x1: m.w, y1: m.h });
    for (const [x, y] of pts) { r.x0 = Math.min(r.x0, x - 4); r.y0 = Math.min(r.y0, y - 4); r.x1 = Math.max(r.x1, x + 4); r.y1 = Math.max(r.y1, y + 4); }
    const bw = Math.max(4, r.x1 - r.x0), bh = Math.max(4, r.y1 - r.y0);
    const w = Math.min(m.w, Math.max(bw * 1.3, bh * 1.3 / ratio, m.w / MAX_Z));
    return { x: (r.x0 + r.x1) / 2 - w / 2, y: (r.y0 + r.y1) / 2 - w * ratio / 2, w };
  }
  C._probe = { pieces, frame, baseView };

  // ------------------------------------------------------------------ the map
  function bubbleRadius(bytes, max) { return 4 + 13 * Math.sqrt(bytes / Math.max(1, max)); }
  function mapParts(d) {
    const m = world(), fine = LL.worldmapFine && LL.worldmapFine.countries;
    const side = d.home ? 'is-home' : d.eu ? 'is-eu' : 'is-out';
    const shapes = svgEl('g', { class: 'pc-shapes' });
    for (const [cc, c] of Object.entries(m.countries)) {
      const f = fine && fine[cc];
      const p = svgEl('path', { class: 'pc-land' + (cc === d.cc ? ' is-focus ' + side : ''), d: (f && f.d) || c.d, 'data-cc': cc });
      shapes.appendChild(titled(p, name(cc)));
      const o = (f && f.o) || c.o;
      if (o) shapes.appendChild(svgEl('path', { class: 'pc-land pc-overseas', d: o }));
    }
    for (const [cc, f] of Object.entries(fine || {})) {          // small states that are only points on the coarse map
      if (!m.countries[cc]) shapes.appendChild(titled(svgEl('path', { class: 'pc-land' + (cc === d.cc ? ' is-focus ' + side : ''), d: f.d, 'data-cc': cc }), name(cc)));
    }
    const centre = labelPoint(d.cc);
    const bubbles = [];
    for (const p of d.points) {
      let xy = p.lat != null && p.lon != null ? place(p.lon, p.lat) : centre;
      if (!xy) continue;
      bubbles.push({ p, x: xy[0], y: xy[1] });
    }
    const max = Math.max(1, ...bubbles.map(b => b.p.bytes));
    const g = svgEl('g', { class: 'pc-bubbles' });
    for (const b of bubbles.slice().sort((a, c) => (c.p.approx ? 1 : 0) - (a.p.approx ? 1 : 0) || c.p.bytes - a.p.bytes)) {
      const label = (b.p.name ? b.p.name + (b.p.region && b.p.region !== b.p.name ? ', ' + b.p.region : '') : t('pc.bubble.country', { country: name(d.cc) })) + ' — ' + LL.fmtBytes(b.p.bytes) + (b.p.approx ? ' (' + t(b.p.via === 'route' ? 'pc.approx_route' : 'pc.approx') + ')' : '');
      b.r = bubbleRadius(b.p.bytes, max);
      b.label = label;
      b.el = titled(svgEl('circle', { class: 'pc-bubble ' + side + (b.p.approx ? ' is-approx' : ''), cx: b.x.toFixed(2), cy: b.y.toFixed(2), r: b.r, 'data-r': b.r.toFixed(1), 'data-b': b.p.bytes }), label);
      g.appendChild(b.el);
    }
    const ways = cablesOn() ? cableWays(d, bubbles, side) : null;
    const svg = svgEl('svg', { class: 'pc-map', viewBox: '0 0 ' + m.w + ' ' + m.h, role: 'img', 'aria-label': t('pc.map.aria', { country: name(d.cc) }), preserveAspectRatio: 'xMidYMid meet' },
      [shapes, ways ? ways.g : null, g]);
    Z.sums = { view: ways ? 'cables' : 'rain', bubbles: bubbles.reduce((a, b) => a + b.p.bytes, 0), streams: ways ? ways.streams.reduce((a, x) => a + x.rx + x.tx, 0) : null,
      routes: ways ? ways.streams.filter(x => x.via === 'route').length : 0, estimated: 0 };
    return { svg, bubbles, max, side, streams: ways ? ways.streams : [], hot: ways ? ways.hot : [], ways };
  }
  /* Real cables: per bubble one strand and one stream (paths_infra.js); the bytes of a bubble are split into received and sent like the
     country's totals. Without the ways (still loading) only the bubbles are drawn. */
  function cableWays(d, bubbles, side) {
    const IN = LL.pathsInfra;
    if (!IN) return null;
    const N = IN.data();
    IN.need(d.infra_stand);
    const g = svgEl('g', { class: 'pc-ways' });
    let streams = [], extra = [];
    if (N) {
      const dr = IN.draw(N, IN.sheet(N, d, bubbles, { frame: frame(d.cc), out: side === 'is-out' }), { maxW: 3.4 });
      g.appendChild(dr.g);
      streams = dr.streams;
      extra = dr.extra;
    }
    return { g, streams, extra };
  }

  /* Wheel, drag, pinch, keys and double click on one map; calls onView after every change. Returns a cleanup function. */
  function zoomer(stage, svg, base, ratio, onView) {
    const m = world(), minW = m.w / MAX_Z;
    stage.classList.add('pc-zoomable');
    const reset = h('button', { type: 'button', class: 'icon-btn pc-zoom-reset', title: t('paths.zoom.reset'), 'aria-label': t('paths.zoom.reset'), onclick: () => set(base) });
    reset.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L20 20M8 10.5h5"/></svg>';
    stage.appendChild(reset);
    const cur = () => { const vb = svg.viewBox.baseVal; return { x: vb.x, y: vb.y, w: vb.width }; };
    const set = v => {
      const w = Math.max(minW, Math.min(m.w, v.w)), hh = w * ratio;
      const x = Math.max(-w * 0.2, Math.min(m.w - w * 0.8, v.x)), y = Math.max(-hh * 0.2, Math.min(m.h - hh * 0.8, v.y));
      svg.setAttribute('viewBox', x.toFixed(2) + ' ' + y.toFixed(2) + ' ' + w.toFixed(2) + ' ' + hh.toFixed(2));
      const away = Math.abs(w - base.w) > 0.01 || Math.abs(x - base.x) > 0.5 || Math.abs(y - base.y) > 0.5;
      reset.hidden = !away;
      Z.zv = { x, y, w };
      onView({ x, y, w });
    };
    const toMap = (cx, cy) => { const b = svg.getBoundingClientRect(), v = cur(); return [v.x + (cx - b.left) / b.width * v.w, v.y + (cy - b.top) / b.height * v.w * ratio]; };
    const zoomAt = (cx, cy, f) => { const v = cur(), [mx, my] = toMap(cx, cy), nw = Math.max(minW, Math.min(m.w, v.w / f)), s = nw / v.w; set({ x: mx - (mx - v.x) * s, y: my - (my - v.y) * s, w: nw }); };
    const panBy = (dx, dy) => { const v = cur(), s = v.w / svg.getBoundingClientRect().width; set({ x: v.x - dx * s, y: v.y - dy * s, w: v.w }); };
    const on = [];
    const listen = (el, type, fn, opt) => { el.addEventListener(type, fn, opt); on.push([el, type, fn, opt]); };
    svg.setAttribute('tabindex', '0');
    listen(svg, 'keydown', ev => {
      if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
      const b = svg.getBoundingClientRect(), cx = b.left + b.width / 2, cy = b.top + b.height / 2;
      if (ev.key === '+' || ev.key === '=') zoomAt(cx, cy, 1.6);
      else if (ev.key === '-' || ev.key === '_') zoomAt(cx, cy, 1 / 1.6);
      else if (ev.key === '0') set(base);
      else return;
      ev.preventDefault();
    });
    listen(svg, 'wheel', ev => { if (!ev.deltaY) return; ev.preventDefault(); zoomAt(ev.clientX, ev.clientY, Math.exp(-ev.deltaY * (ev.deltaMode === 1 ? 0.05 : 0.0018))); }, { passive: false });
    let drag = null, moved = false;
    const mm = ev => { if (!drag) return; const dx = ev.clientX - drag.lx, dy = ev.clientY - drag.ly; drag.lx = ev.clientX; drag.ly = ev.clientY; drag.sum += Math.abs(dx) + Math.abs(dy); if (drag.sum > 3) { moved = true; svg.classList.add('is-panning'); panBy(dx, dy); } };
    const mu = () => { drag = null; svg.classList.remove('is-panning'); window.removeEventListener('mousemove', mm); window.removeEventListener('mouseup', mu); };
    listen(svg, 'mousedown', ev => { moved = false; if (ev.button !== 0) return; drag = { lx: ev.clientX, ly: ev.clientY, sum: 0 }; ev.preventDefault(); window.addEventListener('mousemove', mm); window.addEventListener('mouseup', mu); });
    listen(svg, 'click', ev => { if (moved) { moved = false; ev.stopImmediatePropagation(); ev.preventDefault(); } }, true);
    listen(svg, 'dblclick', ev => { ev.preventDefault(); set(base); });
    let tp = null;
    const mid = ts => [(ts[0].clientX + ts[1].clientX) / 2, (ts[0].clientY + ts[1].clientY) / 2];
    const dist = ts => Math.hypot(ts[0].clientX - ts[1].clientX, ts[0].clientY - ts[1].clientY) || 1;
    listen(svg, 'touchstart', ev => {
      const ts = ev.touches;
      if (ev.cancelable) ev.preventDefault();
      if (ts.length === 2) { const c = mid(ts); tp = { mode: 2, d: dist(ts), v: cur(), m0: toMap(c[0], c[1]) }; }
      else if (ts.length === 1) tp = { mode: 1, lx: ts[0].clientX, ly: ts[0].clientY };
      else tp = null;
    }, { passive: false });
    listen(svg, 'touchmove', ev => {
      if (!tp) return;
      if (ev.cancelable) ev.preventDefault();
      const ts = ev.touches, b = svg.getBoundingClientRect();
      if (tp.mode === 2 && ts.length === 2) {
        const nw = Math.max(minW, Math.min(m.w, tp.v.w * tp.d / dist(ts))), c = mid(ts);
        set({ x: tp.m0[0] - (c[0] - b.left) / b.width * nw, y: tp.m0[1] - (c[1] - b.top) / b.height * nw * ratio, w: nw });
      } else if (tp.mode === 1 && ts.length === 1) {
        const dx = ts[0].clientX - tp.lx, dy = ts[0].clientY - tp.ly;
        tp.lx = ts[0].clientX; tp.ly = ts[0].clientY;
        panBy(dx, dy);
      }
    }, { passive: false });
    const end = ev => { if (ev.touches.length === 0) tp = null; else if (ev.touches.length === 1 && tp && tp.mode === 2) tp = { mode: 1, lx: ev.touches[0].clientX, ly: ev.touches[0].clientY }; };
    listen(svg, 'touchend', end);
    listen(svg, 'touchcancel', end);
    set(Z.zv && Z.zvCc === Z.cc ? Z.zv : base);
    stage._set = set;                                       // a chosen route frames its whole way (DW2)
    stage._base = base;
    return () => { mu(); on.forEach(([el, type, fn, opt]) => el.removeEventListener(type, fn, opt)); reset.remove(); };
  }

  /* HTML labels above the SVG: bubbles with the most traffic first, then orientation places by rank (more with every zoom step);
     nothing is placed over another label or over a bubble. */
  function placeLabels(stage, svg, parts, v) {
    const box = stage.querySelector('.pc-labels');
    if (!box) return;
    const W = stage.clientWidth, Hh = stage.clientHeight;
    if (!W || !Hh) return;
    const sx = W / v.w, px = x => (x - v.x) * sx, py = y => (y - v.y) * sx;
    const kids = [], used = [];
    const k = Math.max(0.6, Math.min(1, W / 700));
    if (parts.routes) parts.routes.scale(sx / k);            // route points keep their screen size (DW2)
    parts.bubbles.forEach(b => {                                       // sizes in screen pixels, whatever the zoom
      b.el.setAttribute('r', (b.r * k / sx).toFixed(3));
      const x = px(b.x), y = py(b.y), r = b.r * k;
      used.push([x - r, y - r, x + r, y + r]);
    });
    const fits = (x, y, w, hh) => x >= 2 && y >= 2 && x + w <= W - 2 && y + hh <= Hh - 2 && !used.some(r => x < r[2] && x + w > r[0] && y < r[3] && y + hh > r[1]);
    const add = (cls, text, sub, x, y, w, hh) => { used.push([x, y, x + w, y + hh]); kids.push(h('span', { class: 'pc-label ' + cls, style: 'left:' + x.toFixed(1) + 'px;top:' + y.toFixed(1) + 'px' }, h('b', {}, text), sub ? h('i', {}, sub) : null)); };
    let n = 0;
    for (const b of parts.bubbles.slice().sort((a, c) => c.p.bytes - a.p.bytes)) {
      if (n >= 8) break;
      const text = b.p.name || name(parts.cc), sub = LL.fmtBytes(b.p.bytes) + (b.p.approx ? ' ~' : '');
      const w = Math.max(text.length * 6.2, sub.length * 5.8) + 8, hh = 26, x0 = px(b.x), y0 = py(b.y), r = b.r * k;
      if (x0 < 0 || y0 < 0 || x0 > W || y0 > Hh) continue;
      const spots = [[x0 + r + 3, y0 - 13], [x0 - r - 3 - w, y0 - 13], [x0 - w / 2, y0 + r + 2], [x0 - w / 2, y0 - r - 2 - hh]];
      const s = spots.find(([x, y]) => fits(x, y, w, hh));
      if (!s) continue;
      add('is-data', text, sub, s[0], s[1], w, hh);
      n++;
    }
    const places = LL.worldplaces || [], z = world().w / v.w, maxRank = z < 6 ? 0 : z < 12 ? 1 : z < 24 ? 2 : 3;
    const m = world();
    let shown = 0;
    for (const p of places) {
      if (p[4] > maxRank || shown > 40) continue;
      const x = px(p[0]), y = py(p[1]);
      if (x < 4 || y < 4 || x > W - 4 || y > Hh - 4) continue;
      const w = p[2].length * 5.6 + 6, hh = 13;
      if (!fits(x + 4, y - 7, w, hh)) continue;
      kids.push(h('i', { class: 'pc-dot', style: 'left:' + x.toFixed(1) + 'px;top:' + y.toFixed(1) + 'px' }));
      add('is-place', p[2], '', x + 4, y - 7, w, hh);
      shown++;
    }
    void m;
    box.replaceChildren(...kids.filter(Boolean));
  }

  // ------------------------------------------------------------------ pieces of the sheet
  const bar = (share, cls) => h('span', { class: 'pc-bar' }, h('i', { class: cls, style: 'width:' + Math.max(2, Math.round(share * 100)) + '%' }));
  const when = (tt, step) => new Date(tt * 1000).toLocaleString(LL.lang, step === 'hour' ? { weekday: 'short', hour: '2-digit', minute: '2-digit' } : { weekday: 'short', day: '2-digit', month: '2-digit' });
  function kpis(d) {
    const tt = d.totals, tile = (value, label, sub) => h('div', { class: 'pc-kpi' }, h('b', {}, value), h('span', {}, label), sub ? h('small', {}, sub) : null);
    return h('div', { class: 'pc-kpis' },
      tile(LL.fmtBytes(tt.rx), t('pc.kpi.received'), '↓'),
      tile(LL.fmtBytes(tt.tx), t('pc.kpi.sent'), '↑'),
      tile(pct(tt.share), t('pc.kpi.share')),
      tile(String(tt.devices), t('paths.kpi.devices', { n: tt.devices })),
      tile(String(tt.operators), t('pc.kpi.operators', { n: tt.operators })),
      tile(String(tt.cities), t('pc.kpi.cities', { n: tt.cities })));
  }
  function historyCard(d, side) {
    const pts = d.series.points, max = Math.max(1, ...pts.map(p => p.tx + p.rx));
    const step = d.series.step, every = step === 'hour' ? 6 : pts.length > 10 ? 7 : 1;
    const cols = pts.map(p => {
      const b = p.tx + p.rx;
      return h('i', { class: b ? '' : 'is-empty', title: t('pc.history.tip', { when: when(p.t, step), bytes: LL.fmtBytes(b), rx: LL.fmtBytes(p.rx), tx: LL.fmtBytes(p.tx) }) },
        h('b', { style: 'height:' + (b / max * 100).toFixed(1) + '%' }, p.tx ? h('u', { style: 'height:' + (p.tx / b * 100).toFixed(1) + '%' }) : null));
    });
    const axis = pts.map((p, i) => h('span', {}, i % every === 0 ? new Date(p.t * 1000).toLocaleString(LL.lang, step === 'hour' ? { hour: '2-digit', minute: '2-digit' } : { day: '2-digit', month: '2-digit' }) : ''));
    return h('section', { class: 'card pc-card pc-history' }, h('header', { class: 'pc-card__head' }, h('h3', {}, t('pc.history')),
      h('span', { class: 'muted' }, t(step === 'hour' ? 'pc.history.hour' : 'pc.history.day') + ' · ' + t('pc.history.peak', { bytes: LL.fmtBytes(max) }))),
    h('div', { class: 'pc-cols ' + side, style: '--n:' + pts.length }, cols), h('div', { class: 'pc-axis', style: '--n:' + pts.length }, axis));
  }
  function detailNote(d) {
    const dt = (d.geo && d.geo.detail) || {}, have = d.cities.length || d.operators.length;
    if (dt.status === 'unavailable') return h('p', { class: 'msg msg--info pc-note' }, t('pc.detail.unavailable'));
    if (dt.status === 'off') {
      return h('p', { class: 'msg msg--info pc-note' }, t(have ? 'pc.detail.off_kept' : 'pc.detail.off'), ' ',
        LL.can('admin') ? h('a', { href: '#/settings' }, t('pc.detail.open_settings')) : null);
    }
    if (dt.status === 'loading') {
      const p = dt.progress || {};
      return h('p', { class: 'msg msg--info pc-note' }, t('pc.detail.loading'), p.step === 'download' && p.total ? ' ' + LL.fmtBytes(p.bytes) + ' / ' + LL.fmtBytes(p.total) : (p.rows ? ' ' + LL.fmtNum(p.rows, 0) : ''));
    }
    if (dt.status === 'error') return h('p', { class: 'msg msg--warn pc-note' }, t('pc.detail.error'));
    if (dt.status === 'waiting') return h('p', { class: 'msg msg--info pc-note' }, t('pc.detail.waiting'));
    if (!have && d.totals.bytes) return h('p', { class: 'msg msg--info pc-note' }, t('pc.detail.from_now'));
    return null;
  }
  function citiesCard(d, side) {
    const max = Math.max(1, ...d.cities.map(c => c.bytes));
    const rows = d.cities.slice(0, 20).map(c => h('li', { class: 'pc-row' }, h('span', { class: 'pc-row__name' }, c.name),
      h('span', { class: 'pc-row__meta muted' }, [c.region && c.region !== c.name ? c.region : '', t('paths.n_devices', { n: c.devices }), c.operators.slice(0, 2).map(o => o.name).join(', ')].filter(Boolean).join(' · ')),
      h('span', { class: 'pc-row__value' }, LL.fmtBytes(c.bytes)), bar(c.bytes / max, side)));
    const rest = d.cities.length - rows.length;
    return h('section', { class: 'card pc-card' }, h('header', { class: 'pc-card__head' }, h('h3', {}, t('pc.cities')), h('span', { class: 'muted' }, t('pc.cities.sub'))),
      rows.length ? h('ul', { class: 'pc-list' }, rows) : h('p', { class: 'muted pc-empty' }, t('pc.cities.none')),
      rest > 0 ? h('p', { class: 'muted pc-more' }, t('pc.cities.more', { n: rest })) : null,
      d.totals.no_city > 0 && d.totals.bytes ? h('p', { class: 'muted pc-more', title: t('pc.no_city.hint') }, t('pc.no_city', { bytes: LL.fmtBytes(d.totals.no_city) })) : null);
  }
  function operatorsCard(d, side) {
    const all = d.operators, real = all.filter(o => !o.other), n = d.totals.operators, limit = 40;
    const list = Z.showAll ? all : all.slice(0, limit);
    const max = Math.max(1, ...all.map(o => o.bytes));
    const rows = list.map(o => h('li', { class: 'pc-row' }, h('span', { class: 'pc-row__name' }, o.other ? t('pc.operators.other') : o.name),
      h('span', { class: 'pc-row__meta muted' }, [o.other ? '' : 'AS' + o.asn, o.cities.map(c => c.name).join(', '), t('paths.n_devices', { n: o.devices })].filter(Boolean).join(' · ')),
      h('span', { class: 'pc-row__value' }, LL.fmtBytes(o.bytes)), bar(o.bytes / max, side)));
    const toggle = all.length > limit ? h('button', { type: 'button', class: 'btn btn--ghost btn--sm pc-toggle', onclick: () => { Z.showAll = !Z.showAll; draw(); } },
      Z.showAll ? t('pc.operators.less') : t('pc.operators.all', { n: all.length })) : null;
    return h('section', { class: 'card pc-card' }, h('header', { class: 'pc-card__head' }, h('h3', {}, t('pc.operators')),
      h('span', { class: 'muted' }, all.length > limit && !Z.showAll ? t('pc.operators.of', { shown: limit, n: all.length }) : t('pc.operators.count', { n: real.length || n }))),
    rows.length ? h('ul', { class: 'pc-list' }, rows) : h('p', { class: 'muted pc-empty' }, t('pc.operators.none')), toggle);
  }
  function devicesCard(d, side) {
    const max = Math.max(1, ...d.devices.map(x => x.bytes));
    const rows = d.devices.map(x => h('li', {}, h('a', { class: 'pc-row pc-row--link', href: '#/devices/' + encodeURIComponent(x.id), onclick: () => close(true) },
      h('span', { class: 'pc-row__name' }, x.title || (x.self ? t('paths.this_host') : x.id === '~unknown' ? t('paths.unknown_device') : t('kind.' + (x.kind || 'unknown')))),
      h('span', { class: 'pc-row__meta muted' }, [x.operators.map(o => o.name).slice(0, 2).join(', '), x.cities.slice(0, 2).join(', ')].filter(Boolean).join(' · ')),
      h('span', { class: 'pc-row__value' }, LL.fmtBytes(x.bytes)), bar(x.bytes / max, side))));
    return h('section', { class: 'card pc-card' }, h('header', { class: 'pc-card__head' }, h('h3', {}, t('pc.devices')),
      h('span', { class: 'muted' }, t('pc.devices.sub', { n: d.devices.length, country: name(d.cc) }))),
    rows.length ? h('ul', { class: 'pc-list' }, rows) : h('p', { class: 'muted pc-empty' }, t('pc.devices.none')));
  }
  function targetsCard(d) {
    const rows = d.targets.slice(0, 16).map(x => h('li', { class: 'pc-target' }, h('b', {}, x.name || t('pc.operators.unknown')),
      h('small', {}, [x.asn > 0 ? 'AS' + x.asn : '', x.city || t('pc.target.unknown_city')].filter(Boolean).join(' · ')), h('em', {}, LL.fmtBytes(x.bytes))));
    return h('section', { class: 'card pc-card' }, h('header', { class: 'pc-card__head' }, h('h3', {}, t('pc.targets')), h('span', { class: 'muted' }, t('pc.targets.hint'))),
      rows.length ? h('ul', { class: 'pc-targets' }, rows) : h('p', { class: 'muted pc-empty' }, t('pc.targets.none')));
  }
  function attribution(d) {
    const a = (d.geo && d.geo.attribution) || { text: 'IP Geolocation by DB-IP', url: 'https://db-ip.com', license: 'CC BY 4.0', license_url: 'https://creativecommons.org/licenses/by/4.0/' };
    const dt = (d.geo && d.geo.detail) || {};
    return h('p', { class: 'pc-attr muted' }, t('pc.attr.approx'), ' ', h('a', { href: a.url, target: '_blank', rel: 'noopener noreferrer' }, a.text), ' (',
      h('a', { href: a.license_url, target: '_blank', rel: 'noopener noreferrer' }, a.license), dt.month && dt.status === 'ready' ? ', ' + dt.month : '', ') · ', t('pc.attr.map'));
  }

  // ------------------------------------------------------------------ the sheet
  function build() {
    const closeBtn = h('button', { type: 'button', class: 'icon-btn pc-close', title: t('pc.close'), 'aria-label': t('pc.close'), onclick: () => close() }, '✕');
    const el = h('div', { class: 'pc-scrim' }, h('section', { class: 'pc-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('pc.title.aria', { country: name(Z.cc) }) },
      h('header', { class: 'pc-head' }, h('span', { class: 'pc-cc mono' }), h('div', { class: 'pc-titles' }, h('h2', {}), h('small', { class: 'muted' })), h('div', { class: 'pc-seg' }), closeBtn),
      h('div', { class: 'pc-body' }, h('p', { class: 'muted' }, t('paths.loading')))));
    el.addEventListener('click', ev => { if (ev.target === el) close(); });
    document.body.appendChild(el);
    document.body.classList.add('pw-noscroll');
    Z.el = el;
    Z.key = ev => { if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); close(); } };
    Z.pop = () => { Z.closing = Date.now(); teardown(true); };      // the hashchange of this step must not redraw the view
    document.addEventListener('keydown', Z.key, true);
    window.addEventListener('popstate', Z.pop);
    window.addEventListener('resize', onResize);
    requestAnimationFrame(() => el.classList.add('is-open'));
    closeBtn.focus();
  }
  document.addEventListener('lanloom-paths-infra', () => { if (Z.el && Z.d && cablesOn()) draw(); });      // the ways of the Real cables view arrived
  let lastPhone = phone();
  function onResize() { if (Z.el && lastPhone !== phone()) { lastPhone = phone(); draw(); } }
  function segment() {
    return h('div', { class: 'seg', role: 'group', 'aria-label': t('paths.window.label') }, PERIODS.map(w =>
      h('button', { type: 'button', class: 'seg__btn' + (w === Z.window ? ' is-active' : ''), 'aria-pressed': String(w === Z.window), onclick: () => { if (w !== Z.window) { Z.window = w; try { localStorage.setItem('lanloom-paths-window', w); } catch (e) { /* private mode */ } Z.d = null; Z.error = null; draw(); load(); } } }, t('paths.window.' + w))));
  }
  function draw() {
    if (!Z.el) return;
    const el = Z.el, d = Z.d;
    if (Z.cleanup) { Z.cleanup(); Z.cleanup = null; }
    el.querySelector('.pc-cc').textContent = Z.cc;
    el.querySelector('.pc-titles h2').textContent = name(Z.cc);
    el.querySelector('.pc-seg').replaceChildren(segment());
    const body = el.querySelector('.pc-body'), scroll = body.scrollTop;
    if (!d) { el.querySelector('.pc-titles small').textContent = ''; body.replaceChildren(h('p', { class: Z.error ? 'msg msg--error' : 'muted' }, Z.error || t('paths.loading'))); return; }
    const where = d.home ? t('pc.sub.home') : d.eu ? t('pc.sub.eu') : t('pc.sub.out');
    el.querySelector('.pc-titles small').textContent = t('pc.sub', { bytes: LL.fmtBytes(d.totals.bytes), share: pct(d.totals.share), where });
    lastPhone = phone();
    if (!d.totals.bytes) { body.replaceChildren(...[detailNote(d), h('p', { class: 'muted pc-empty' }, t('pc.empty', { country: name(Z.cc) })), attribution(d)].filter(Boolean)); return; }
    const parts = mapParts(d);
    parts.cc = Z.cc;
    const homeCc = d.home_cc || (LL.pathsHomeCc && LL.pathsHomeCc()) || '';
    const routes = (d.routes || []).length && LL.pathsRoutes;
    if (parts.ways) {
      // Real cables: the routes are the strands the dots run on; a chosen route lights up and frames its whole way
      parts.routes = { scale(f) { LL.pathsInfra.scale(parts.svg, f); }, box: key => LL.pathsInfra.box(key) };
      CB().layer(parts.svg, { before: '.pc-ways', detached: true });
    } else parts.routes = routes ? LL.pathsRoutes.drawOn(parts.svg, d.routes, { project: place, point: labelPoint, home: homeCc }) : null;
    const ratio = phone() ? 0.95 : 0.56;
    const stage = h('div', { class: 'pc-stage', style: '--pc-ratio:' + ratio }, parts.svg, h('div', { class: 'pc-labels', 'aria-hidden': 'true' }));
    const legend = h('div', { class: 'pc-legend' }, h('span', {}, h('i', { class: 'pc-lg pc-lg--city ' + parts.side }), t('pc.legend.city')),
      h('span', {}, h('i', { class: 'pc-lg pc-lg--approx ' + parts.side }), t('pc.legend.approx')), h('span', {}, h('i', { class: 'pc-lg pc-lg--place' }), t('pc.legend.place')),
      routes && !parts.ways ? h('span', {}, h('i', { class: 'pc-lg pc-lg--route' }), t('routes.legend.route')) : null,
      routes && !parts.ways ? h('span', {}, h('i', { class: 'pc-lg pc-lg--route pc-lg--route-est' }), t('routes.legend.estimated')) : null,
      ...(parts.ways ? CB().legendItems('pc-legend__cb') : []),
      h('span', { class: 'pc-legend__hint' }, t(coarsePointer() ? 'pc.map.hint_touch' : 'pc.map.hint')));
    const note = detailNote(d);
    /* a chosen route frames its whole way on the map; choosing it again returns to the country */
    const fit = r => {
      if (!stage._set) return;
      const b = r && parts.routes ? parts.routes.box(r.key) : null;
      if (!b) { stage._set(stage._base); return; }
      const m = world(), w = Math.min(m.w, Math.max((b.x1 - b.x0) * 1.3, (b.y1 - b.y0) * 1.3 / ratio, m.w / MAX_Z));
      stage._set({ x: (b.x0 + b.x1) / 2 - w / 2, y: (b.y0 + b.y1) / 2 - w * ratio / 2, w });
      const box = stage.getBoundingClientRect();                // the list sits below the map: bring the map into view
      if (box.bottom < 60 || box.top > window.innerHeight - 60) stage.scrollIntoView({ block: 'nearest', behavior: reduced() ? 'auto' : 'smooth' });
    };
    const routeCard = LL.pathsRoutes ? LL.pathsRoutes.card(Object.assign({ home_cc: homeCc }, d), { onSelect: fit }) : null;
    const viewbar = CB() ? h('div', { class: 'pc-viewbar' }, CB().control('pv-seg--sheet')) : null;
    const left = h('div', { class: 'pc-col' }, h('section', { class: 'card pc-card pc-mapcard' + (parts.ways ? ' is-cables' : ''), 'data-view': CB() ? CB().view() : 'rain' },
      viewbar, stage, legend, CB() ? CB().attribution('pc-attr') : null), historyCard(d, parts.side), targetsCard(d));
    const right = h('div', { class: 'pc-col' }, kpis(d), note, citiesCard(d, parts.side), operatorsCard(d, parts.side), devicesCard(d, parts.side));
    body.replaceChildren(...[h('div', { class: 'pc-grid' }, left, right), routeCard, attribution(d)].filter(Boolean));     // routes: the whole width, a chain has room
    body.scrollTop = scroll;
    const pts = parts.bubbles.map(b => [b.x, b.y]).concat(parts.ways ? parts.ways.extra || [] : []);      // the satellite hop stays in the picture
    Z.zvCc = Z.zvCc || Z.cc;
    if (Z.zvCc !== Z.cc) { Z.zv = null; Z.zvCc = Z.cc; }
    const base = baseView(Z.cc, ratio, pts);
    let raf = 0;
    const onView = v => { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; if (stage.isConnected) placeLabels(stage, parts.svg, parts, v); }); };
    const stop = zoomer(stage, parts.svg, base, ratio, onView);
    let dots = null;
    if (parts.ways) requestAnimationFrame(() => { if (stage.isConnected && Z.el === el) dots = CB().runDots(stage, parts.svg, parts.streams, { width: world().w }); });
    if (parts.ways) LL.pathsInfra.mark();
    Z.cleanup = () => { stop(); if (raf) cancelAnimationFrame(raf); if (dots) dots.stop(); };
    loadFine().then(ok => { if (ok && Z.el === el && Z.d === d && stage.isConnected && !stage.dataset.fine) { stage.dataset.fine = '1'; upgrade(stage, parts, d, base, ratio, onView); } });
  }
  /* The fine map and the places arrived: swap the shapes in place, the view stays where it is. */
  function upgrade(stage, parts, d) {
    const fine = LL.worldmapFine && LL.worldmapFine.countries;
    if (!fine) return;
    parts.svg.querySelectorAll('path.pc-land[data-cc]').forEach(p => { const f = fine[p.dataset.cc]; if (f && f.d) p.setAttribute('d', f.d); });
    const v = Z.zv || parts.svg.viewBox.baseVal;
    placeLabels(stage, parts.svg, parts, { x: v.x, y: v.y, w: v.w != null ? v.w : v.width });
  }
  const gate = LL.fresh.gate(60000);        // one request at a time, a request that never ends is given up (O10)
  async function load() {
    if (!Z.cc) return;
    const k = gate.enter();
    if (!k) return;
    const cc = Z.cc, w = Z.window;
    let good = false;
    try {
      const d = await api('/api/paths/country/' + encodeURIComponent(cc) + '?window=' + w);
      if (gate.live(k) && cc === Z.cc && w === Z.window) { Z.d = d; Z.error = null; good = true; }
    } catch (e) { if (gate.live(k)) Z.error = LL.errText(e); }
    gate.leave(k);
    if (!gate.live(k)) return;
    if (cc !== Z.cc || w !== Z.window) { load(); return; }
    if (Z.fw) { if (good) Z.fw.ok(); else Z.fw.fail(); }
    draw();
  }
  function teardown(fromPop) {
    if (!Z.el) return;
    const el = Z.el;
    Z.el = null;
    document.removeEventListener('keydown', Z.key, true);
    window.removeEventListener('popstate', Z.pop);
    window.removeEventListener('resize', onResize);
    clearInterval(Z.timer);
    if (Z.fw) { Z.fw.destroy(); Z.fw = null; }
    if (Z.cleanup) { Z.cleanup(); Z.cleanup = null; }
    el.remove();
    document.body.classList.remove('pw-noscroll');
    Z.d = null;
    if (fromPop) Z.pushed = false;
    if (CB() && cablesOn() !== (LL.query && LL.query.get('view') === 'cables')) setTimeout(() => CB().sync(), 0);     // the view chosen in the sheet stays in the address
    if (!Z.pushed && /^#\/paths\/[A-Za-z]{2}$/.test(location.hash) && !fromPop) { try { history.replaceState(null, '', LL.base + '#/paths'); } catch (e) { /* ignore */ } }
  }
  /* Opening adds one history entry with the deep link; the back gesture and the close button take it away again, and the
     view below stays as it is (the route hold keeps the hashchange of that step from redrawing the page). */
  C.open = function (cc, win, replace) {
    cc = String(cc || '').toUpperCase();
    if (!/^[A-Z]{2}$/.test(cc)) return;
    if (Z.cc !== cc) { Z.d = null; Z.zv = null; Z.showAll = false; }
    Z.cc = cc;
    Z.window = PERIODS.includes(win) ? win : Z.window;
    const fresh = !Z.el;
    if (fresh) {
      build();
      if (!replace) { try { history.pushState({ lanloomPathsCountry: 1 }, '', LL.base + '#/paths/' + cc); Z.pushed = true; } catch (e) { Z.pushed = false; } }
      else Z.pushed = false;
    }
    draw();
    if (fresh && Z.el) Z.fw = LL.fresh.watch(Z.el.querySelector('.pc-body'), 60000, load);
    load();
    clearInterval(Z.timer);
    Z.timer = setInterval(() => { if (Z.el && !document.hidden) load(); }, 60000);
  };
  function close(silent) {
    if (!Z.el) return;
    if (Z.pushed && history.state && history.state.lanloomPathsCountry && !silent) {
      Z.closing = Date.now();
      history.back();                                      // popstate tears down
      setTimeout(() => { if (Z.el) teardown(true); }, 600);
      return;
    }
    teardown(false);
  }
  C.close = () => close(true);
  C.isOpen = () => !!Z.el;
  C.state = () => ({ cc: Z.cc, window: Z.window, loaded: !!Z.d, sums: Object.assign({}, Z.sums || {}) });
  document.addEventListener('lanloom-paths-view', () => { if (Z.el && Z.d) draw(); });
  C.data = () => Z.d;
  C.refresh = () => load();
  /* the router asks before it redraws the view for a hashchange: the step that only closes the sheet changes nothing below */
  LL.holdRoute = () => {
    const held = Z.closing && Date.now() - Z.closing < 1500 && /^#\/paths(\?.*)?$/.test(location.hash);
    if (held) Z.closing = 0;
    return !!held;
  };

  // ------------------------------------------------------------------ settings card: cities and network operators (off by default)
  /* The consent is in the text above the switch: which two files are fetched from where, how big, how often, what is stored. */
  LL.pathsDetailSettingsCard = function (s) {
    const on = h('input', { type: 'checkbox', checked: !!s.paths_detail });
    const status = h('div', { class: 'pc-set-status' });
    const msg = h('div', { class: 'msg' });
    const admin = LL.can('admin');
    on.disabled = !admin;
    async function refresh() {
      let p;
      try { p = await api('/api/paths?window=24h'); } catch (e) { status.replaceChildren(); return; }
      const dt = (p.geo && p.geo.detail) || {};
      const state = t('settings.pathsdetail.state.' + (dt.status || 'off'));
      const prog = dt.status === 'loading' && dt.progress ? (dt.progress.step === 'download' && dt.progress.total ? ' (' + LL.fmtBytes(dt.progress.bytes) + ' / ' + LL.fmtBytes(dt.progress.total) + ')' : dt.progress.rows ? ' (' + LL.fmtNum(dt.progress.rows, 0) + ')' : '') : '';
      status.replaceChildren(LL.facts([
        [t('settings.pathsdetail.state'), h('span', { class: dt.status === 'error' ? 'nf-bad' : dt.status === 'ready' ? 'nf-ok' : '' }, state + prog)],
        dt.month ? [t('settings.pathsdetail.month'), dt.month + ' · ' + t('settings.pathsdetail.counts', { cities: LL.fmtNum(dt.cities, 0), operators: LL.fmtNum(dt.operators, 0), size: LL.fmtBytes(dt.size) })] : null,
        dt.last_check ? [t('settings.pathsdetail.checked'), new Date(dt.last_check * 1000).toLocaleString(LL.lang)] : null,
        dt.error ? [t('settings.pathsdetail.error'), dt.error] : null]));
      if (dt.status === 'loading') setTimeout(() => { if (status.isConnected) refresh(); }, 4000);
    }
    const act = async (action, confirmKey) => {
      if (confirmKey && !window.confirm(t(confirmKey))) return;
      LL.msg(msg, '');
      try { await api('/api/paths/detail/' + action, { method: 'POST', body: {} }); LL.msg(msg, t('settings.saved'), 'ok'); if (action === 'clear') on.checked = false; setTimeout(refresh, 600); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    };
    const save = h('button', { class: 'btn btn--ghost', type: 'button', disabled: admin ? null : true, onclick: async () => {
      LL.msg(msg, '');
      try { await api('/api/settings', { method: 'PUT', body: { paths_detail: on.checked } }); LL.msg(msg, t('settings.saved'), 'ok'); setTimeout(refresh, 800); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('settings.paths.save'));
    refresh();
    return h('section', { class: 'card settings-card pc-set' }, h('h2', {}, t('settings.pathsdetail.title')),
      h('p', { class: 'muted' }, t('settings.pathsdetail.text')),
      h('p', { class: 'muted fineprint' }, t('settings.pathsdetail.consent')),
      h('label', { class: 'check' }, on, h('span', {}, t('settings.pathsdetail.enable'))),
      status, msg,
      h('div', { class: 'btn-row' }, save,
        admin ? h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => act('refresh') }, t('settings.pathsdetail.refresh')) : null,
        admin ? h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => act('clear', 'settings.pathsdetail.clear_confirm') }, t('settings.pathsdetail.clear')) : null),
      h('p', { class: 'muted fineprint' }, 'IP Geolocation by ', h('a', { href: 'https://db-ip.com', target: '_blank', rel: 'noopener noreferrer' }, 'DB-IP'), ' (',
        h('a', { href: 'https://creativecommons.org/licenses/by/4.0/', target: '_blank', rel: 'noopener noreferrer' }, 'CC BY 4.0'), ')'));
  };

  // ------------------------------------------------------------------ command palette: "Data paths of <country>", typed only
  const m = world();
  Object.keys(Object.assign({}, m.countries, m.points)).filter(c => /^[A-Z]{2}$/.test(c)).forEach(cc => LL.registerCommand({
    id: 'paths.country.' + cc, group: 'views', order: 40, typedOnly: true, label: () => t('cmdk.paths_country', { name: name(cc) }),
    keywords: () => cc + ' ' + t('cmdk.kw.paths_country'), run: () => LL.go('#/paths/' + cc) }));
})();
