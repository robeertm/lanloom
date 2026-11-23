/* Lanloom — energy (#/energy): the power the home-automation hub measures, drawn like the network: dots on traces, but the dots
   carry watts instead of bytes. Sources (grid, solar, battery) feed the home, the home feeds its consumers; every consumer is a lane
   with its watts, its cost per hour and its CO2 per hour. Only with a hub that has power sensors - without one the page says so and
   nothing else shows energy.

   Data: GET /api/energy (lanloom/energy.py: now, flows, lanes, today, price and CO2 with their source, freshness).
   Writes: PUT /api/energy/settings {price, currency, feed_in, co2_g_kwh, price_entity, co2_entity, home_entity, hidden} (operators; audit log).
   Lane names are the server's label (device name when the sensor's own name is generic); a lane can be hidden or marked as the whole
   home, and what the server left out (sums, means, doubles, hidden) is listed under "Not counted".
   Also here: the card on the start page, the card on "Now", the palette entry. A dot's number on a trace follows its watts
   (one dot per DOT_W, at most DOTS_MAX on a trace), all dots move at the same pace; with "reduce motion" they stand still. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const SVGNS = 'http://www.w3.org/2000/svg';
  const REFRESH_MS = 5000;
  const DOT_W = 150;                // watts per dot on a trace
  const DOTS_MAX = 12;              // dots on one trace
  const DOTS_TOTAL = 160;           // dots on the whole page (frame budget)
  const PACE = 42;                  // px per second
  const FIG_LANES = 8;              // consumers in the big figure (all of them are in the list)
  const fx = v => String(Math.round(v * 100) / 100);
  const svg = (tag, attrs, ...kids) => {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, String(v));
    kids.flat().forEach(k => { if (k != null) el.appendChild(typeof k === 'string' ? document.createTextNode(k) : k); });
    return el;
  };
  const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  LL.NAV_ENERGY_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2.5L5 13.5h6l-1 8 8-11h-6z"/></svg>';

  // ------------------------------------------------------------------ numbers in words
  LL.fmtW = function (w) {
    if (w == null || !isFinite(w)) return '—';
    const a = Math.abs(w);
    if (a >= 1000) return t('energy.unit.kw', { v: LL.fmtNum(w / 1000, a >= 10000 ? 1 : 2) });
    return t('energy.unit.w', { v: LL.fmtNum(w, a >= 10 ? 0 : 1) });
  };
  const kwh = v => (v == null ? '—' : t('energy.unit.kwh', { v: LL.fmtNum(v, v >= 100 ? 0 : v >= 10 ? 1 : 2) }));
  function money(v, cur, digits) {
    if (v == null || !isFinite(v)) return '—';
    try { return new Intl.NumberFormat(LL.locale(), { style: 'currency', currency: cur || 'EUR', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v); }
    catch (e) { return LL.fmtNum(v, digits) + ' ' + (cur || ''); }
  }
  const perHour = text => t('energy.unit.per_h', { v: text });
  const co2h = g => (g == null ? '—' : t('energy.unit.co2_g_h', { v: LL.fmtNum(g, g >= 100 ? 0 : 1) }));
  const co2kg = kg => (kg == null ? '—' : t('energy.unit.co2_kg', { v: LL.fmtNum(kg, kg >= 10 ? 1 : 2) }));
  const costH = (d, v) => (d.price ? perHour(money(v, d.price.currency, v != null && Math.abs(v) < 1 ? 3 : 2)) : '—');

  // ------------------------------------------------------------------ dots on traces (one loop for the page)
  const anim = { paths: [], raf: 0, last: 0 };
  function addDots(group, path, w, cls) {
    if (!w || w < 1) return;
    const n = Math.max(1, Math.min(DOTS_MAX, Math.round(w / DOT_W)));
    const used = anim.paths.reduce((a, p) => a + p.dots.length, 0);
    const take = Math.max(0, Math.min(n, DOTS_TOTAL - used));
    const dots = [];
    for (let i = 0; i < take; i++) {
      const c = svg('circle', { class: 'en-dot ' + (cls || ''), r: w >= 1000 ? 3.6 : 3 });
      group.appendChild(c);
      dots.push(c);
    }
    anim.paths.push({ path, dots, len: 0, off: Math.random() });
  }
  const gate = LL.frameGate();
  function tick(ts) {
    anim.raf = 0;
    if (document.hidden) { gate.reset(); return; }
    // frame ceiling by the motion setting (O14); the dots move by the clock (dt below), so skipped frames cost nothing in speed
    if (!gate.draw(ts) && !reduced()) { anim.raf = requestAnimationFrame(tick); return; }
    const live = anim.paths.filter(p => p.path.isConnected);
    if (!live.length) { anim.paths = []; return; }
    anim.paths = live;
    const dt = anim.last ? Math.min(0.1, (ts - anim.last) / 1000) : 0;
    anim.last = ts;
    const still = reduced();
    for (const p of live) {
      if (!p.len) {
        try { p.len = p.path.getTotalLength(); } catch (e) { p.len = 0; }
        const room = Math.max(1, Math.floor(p.len / 16));        // a short trace keeps a gap between its dots
        while (p.dots.length > room) p.dots.pop().remove();
      }
      if (!p.len) continue;
      if (!still) p.off = (p.off + dt * PACE / p.len) % 1;
      const n = p.dots.length;
      for (let i = 0; i < n; i++) {
        const pt = p.path.getPointAtLength(((p.off + i / n) % 1) * p.len);
        p.dots[i].setAttribute('cx', fx(pt.x)); p.dots[i].setAttribute('cy', fx(pt.y));
      }
    }
    if (!document.hidden && !still) anim.raf = requestAnimationFrame(tick);
  }
  function startDots() { if (!anim.raf) { anim.last = 0; anim.raf = requestAnimationFrame(tick); } }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && anim.paths.length) startDots(); });

  // ------------------------------------------------------------------ the flow figure: sources -> home -> consumers
  const SOURCES = ['solar', 'grid', 'battery'];
  function figure(d, wide) {
    const lanes = wide ? (d.lanes || []).slice(0, FIG_LANES) : [];
    const has = { solar: d.now.solar_w != null, grid: d.now.grid_w != null, battery: d.now.battery_w != null };
    // phone: solar in the middle of the row, so feeding in and charging are short straight traces to its neighbours
    const srcs = (wide ? SOURCES : ['grid', 'solar', 'battery']).filter(s => has[s]);
    const flowW = (from, to) => (d.flows || []).filter(f => f.from === from && f.to === to).reduce((a, f) => a + f.w, 0);
    // wide: sources in a column on the left, the home in the middle, the consumers on the right
    // phone: sources in a row on top, the home below them (the consumers are the list under the figure)
    let W, H, homeX, homeY;
    const pos = {};
    if (wide) {
      const rows = Math.max(srcs.length, lanes.length + (d.rest_w ? 1 : 0), 3);
      W = 820; H = Math.max(300, rows * 46 + 50); homeX = 380; homeY = H / 2;
      srcs.forEach((s, i) => { pos[s] = { x: 150, y: srcs.length === 1 ? homeY : 70 + i * (H - 120) / (srcs.length - 1) }; });
    } else {
      W = 340; H = 270; homeX = W / 2; homeY = H - 52;
      srcs.forEach((s, i) => { pos[s] = { x: srcs.length === 1 ? homeX : 60 + i * (W - 120) / (srcs.length - 1), y: 82 }; });
    }
    const g = svg('svg', { class: 'en-fig' + (wide ? ' en-fig--wide' : ''), viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': t('energy.fig.label', { w: LL.fmtW(d.now.home_w) }) });
    const traces = svg('g', { class: 'en-traces' }), dotsG = svg('g', { class: 'en-dots' }), pads = svg('g', { class: 'en-pads' });
    g.append(traces, dotsG, pads);
    const curve = (x1, y1, x2, y2, horizontal) => {
      if (horizontal) { const mx = (x1 + x2) / 2; return 'M' + fx(x1) + ' ' + fx(y1) + 'C' + fx(mx) + ' ' + fx(y1) + ' ' + fx(mx) + ' ' + fx(y2) + ' ' + fx(x2) + ' ' + fx(y2); }
      const my = (y1 + y2) / 2; return 'M' + fx(x1) + ' ' + fx(y1) + 'C' + fx(x1) + ' ' + fx(my) + ' ' + fx(x2) + ' ' + fx(my) + ' ' + fx(x2) + ' ' + fx(y2);
    };
    const trace = (dpath, w, kind, dotKind) => {
      const p = svg('path', { class: 'en-trace en-trace--' + kind + (w > 1 ? ' is-on' : ''), d: dpath });
      traces.appendChild(p);
      addDots(dotsG, p, w, 'en-dot--' + (dotKind || kind));
      return p;
    };
    // source -> home
    srcs.forEach(s => {
      const p = pos[s];
      trace(wide ? curve(p.x + 26, p.y, homeX - 36, homeY, true) : curve(p.x, p.y + 26, homeX, homeY - 36, false), flowW(s, 'home'), s);
    });
    // solar -> grid (feeding in) and into the battery (charging): short traces between the two pads
    // wide: along the left edge, outside the names; phone: a bow under the row of sources
    const between = (a, b, w, kind, dotKind, lane) => {
      if (!pos[a] || !pos[b] || w <= 1) return;
      const A = pos[a], B = pos[b], ex = 66 - lane * 22;
      trace(wide ? 'M' + fx(A.x - 24) + ' ' + fx(A.y) + 'H' + fx(ex + 10) + 'Q' + fx(ex) + ' ' + fx(A.y) + ' ' + fx(ex) + ' ' + fx(A.y + 10) + 'V' + fx(B.y - 10)
        + 'Q' + fx(ex) + ' ' + fx(B.y) + ' ' + fx(ex + 10) + ' ' + fx(B.y) + 'H' + fx(B.x - 24)
        : Math.abs(srcs.indexOf(a) - srcs.indexOf(b)) === 1 ? 'M' + fx(A.x + (B.x > A.x ? 25 : -25)) + ' ' + fx(A.y) + 'L' + fx(B.x + (B.x > A.x ? -25 : 25)) + ' ' + fx(B.y)
          : 'M' + fx(A.x + 20) + ' ' + fx(A.y + 18) + 'Q' + fx((A.x + B.x) / 2) + ' ' + fx(A.y + 70) + ' ' + fx(B.x - 20) + ' ' + fx(B.y + 18), w, kind, dotKind);
    };
    between('solar', 'grid', flowW('solar', 'grid'), 'export', 'solar', 0);
    between('solar', 'battery', flowW('solar', 'battery'), 'charge', 'solar', 1);
    between('grid', 'battery', flowW('grid', 'battery'), 'charge', 'grid', 0);
    // home -> consumers
    const nLanes = lanes.length + (d.rest_w ? 1 : 0);
    const laneY = i => (nLanes <= 1 ? homeY : 46 + i * (H - 92) / (nLanes - 1));
    const laneX = homeX + 200;
    lanes.forEach((l, i) => {
      const y = laneY(i);
      trace(curve(homeX + 36, homeY, laneX - 8, y, true), l.w, 'lane');
      pads.appendChild(svg('a', { href: l.net_id ? '#/devices/' + encodeURIComponent(l.net_id) : '#/energy', class: 'en-lane-pad', 'data-lane': l.id },
        svg('title', {}, l.label || l.name),
        svg('rect', { class: 'en-pad', x: fx(laneX - 8), y: fx(y - 15), width: 10, height: 30, rx: 3 }),
        svg('text', { class: 'en-pad__name', x: fx(laneX + 10), y: fx(y - 3) }, (l.label || l.name).length > 26 ? (l.label || l.name).slice(0, 25) + '…' : (l.label || l.name)),
        svg('text', { class: 'en-pad__meta', x: fx(laneX + 10), y: fx(y + 12) }, LL.fmtW(l.w) + ' · ' + costH(d, l.cost_h) + ' · ' + co2h(l.co2_h_g))));
    });
    if (wide && d.rest_w) {
      const y = laneY(lanes.length);
      trace(curve(homeX + 36, homeY, laneX - 8, y, true), d.rest_w, 'rest', 'lane');
      pads.appendChild(svg('g', { class: 'en-lane-pad is-rest' }, svg('rect', { class: 'en-pad en-pad--rest', x: fx(laneX - 8), y: fx(y - 15), width: 10, height: 30, rx: 3 }),
        svg('text', { class: 'en-pad__name', x: fx(laneX + 10), y: fx(y - 3) }, t('energy.rest')),
        svg('text', { class: 'en-pad__meta', x: fx(laneX + 10), y: fx(y + 12) }, LL.fmtW(d.rest_w))));
    }
    // the pads of the sources (name and watts above the circle, where no trace runs) and the home
    srcs.forEach(s => {
      const p = pos[s];
      const w = s === 'grid' ? d.now.grid_w : s === 'solar' ? d.now.solar_w : d.now.battery_w;
      const sub = s === 'grid' ? (w < -1 ? t('energy.grid.export') : t('energy.grid.import')) : s === 'battery' ? (w < -1 ? t('energy.battery.charge') : w > 1 ? t('energy.battery.discharge') : t('energy.battery.idle')) : (w > 1 ? t('energy.solar.now') : t('energy.solar.idle'));
      pads.appendChild(svg('g', { class: 'en-src en-src--' + s, 'data-src': s },
        svg('circle', { class: 'en-src__ring', cx: fx(p.x), cy: fx(p.y), r: 24 }),
        svg('path', { class: 'en-src__icon', d: ICON[s], transform: 'translate(' + fx(p.x - 12) + ' ' + fx(p.y - 12) + ')' }),
        svg('text', { class: 'en-src__w', x: fx(p.x), y: fx(p.y - 44), 'text-anchor': 'middle' }, LL.fmtW(Math.abs(w))),
        svg('text', { class: 'en-src__name', x: fx(p.x), y: fx(p.y - 30), 'text-anchor': 'middle' }, t('energy.src.' + s) + ' · ' + sub)));
    });
    pads.appendChild(svg('g', { class: 'en-home' },
      svg('rect', { class: 'en-home__tile', x: fx(homeX - 36), y: fx(homeY - 36), width: 72, height: 72, rx: 14 }),
      svg('path', { class: 'en-src__icon en-home__icon', d: ICON.home, transform: 'translate(' + fx(homeX - 12) + ' ' + fx(homeY - 22) + ')' }),
      svg('text', { class: 'en-home__w', x: fx(homeX), y: fx(homeY + 22), 'text-anchor': 'middle' }, LL.fmtW(d.now.home_w))));
    return g;
  }
  const ICON = {
    solar: 'M12 3v2M12 19v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M3 12h2M19 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8z',
    grid: 'M8 21l2.5-18h3L16 21M9 9h6M8.3 14h7.4M5 6h14M12 3v3',
    battery: 'M4 8h14v8H4zM18 10.5h2v3h-2M8 10.5v3M11 10.5v3',
    home: 'M3.5 11L12 4l8.5 7M6 9.5V19h12V9.5M10 19v-5h4v5'
  };

  // ------------------------------------------------------------------ the list of lanes (the same in words, each with its trace)
  function laneRow(d, l, max, act) {
    const tr = svg('svg', { class: 'en-mini', viewBox: '0 0 80 10', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
    const p = svg('path', { class: 'en-trace en-trace--lane is-on', d: 'M2 5H78' });
    const dg = svg('g', {});
    tr.append(p, dg);
    addDots(dg, p, l.w, 'en-dot--lane');
    const lname = l.label || l.name;
    const name = l.net_id ? h('a', { href: '#/devices/' + encodeURIComponent(l.net_id) }, lname) : lname;
    const acts = act ? h('span', { class: 'en-row__acts' },
      h('button', { class: 'en-act', type: 'button', 'aria-label': t('energy.row.use_home') + ': ' + lname, onclick: () => act({ home_entity: l.id }) }, t('energy.row.use_home')),
      h('button', { class: 'en-act', type: 'button', 'aria-label': t('energy.row.hide') + ': ' + lname, onclick: () => act({ hidden: [...act.hidden, l.id] }) }, t('energy.row.hide'))) : null;
    return h('li', { class: 'en-row', dataset: { lane: l.id } },
      h('span', { class: 'en-row__main' }, h('span', { class: 'en-row__name' }, name),
        h('span', { class: 'en-row__subline' }, h('span', { class: 'en-row__sub muted' }, [l.area, l.share != null ? t('energy.share', { pct: LL.fmtNum(l.share * 100, 0) }) : null].filter(Boolean).join(' · ')), acts)),
      tr,
      h('span', { class: 'en-row__bar', 'aria-hidden': 'true' }, h('i', { style: 'width:' + fx(max ? Math.max(2, l.w / max * 100) : 0) + '%' })),
      h('span', { class: 'en-row__w' }, LL.fmtW(l.w)),
      h('span', { class: 'en-row__cost muted' }, costH(d, l.cost_h)),
      h('span', { class: 'en-row__co2 muted' }, co2h(l.co2_h_g)));
  }

  // ------------------------------------------------------------------ key figures
  function kpis(d) {
    const n = d.now, td = d.today || {}, hs = d.home_source || {};
    const cur = d.price ? d.price.currency : null;
    const tile = (id, label, value, sub) => h('div', { class: 'en-kpi', dataset: { kpi: id } }, h('span', { class: 'en-kpi__label' }, label), h('b', { class: 'en-kpi__value' }, value), sub ? h('span', { class: 'en-kpi__sub muted' }, sub) : null);
    const nowCost = d.price && n.grid_w != null ? Math.max(0, n.grid_w) / 1000 * d.price.value : null;
    return h('div', { class: 'en-kpis' },
      tile('home', t('energy.kpi.home'), LL.fmtW(n.home_w), [hs.kind === 'sensor' && hs.label ? t('energy.home.from', { name: hs.label }) : null, n.solar_w > 1 ? t('energy.kpi.solar_now', { w: LL.fmtW(n.solar_w) }) : null].filter(Boolean).join(' · ') || null),
      tile('cost', t('energy.kpi.cost_now'), d.price ? perHour(money(nowCost, cur, 2)) : '—', d.price ? t('energy.kpi.price', { price: money(d.price.value, cur, 2, 4) }) : t('energy.kpi.no_price')),
      tile('today', t('energy.kpi.today'), kwh(td.kwh), td.cost != null ? money(td.cost, cur, 2) : null),
      tile('co2', t('energy.kpi.co2_today'), co2kg(td.co2_kg), t('energy.kpi.co2_rate', { g: LL.fmtNum(d.co2.g_kwh, 0) })));
  }

  // ------------------------------------------------------------------ what was left out (sums, means, doubles, hidden by a person)
  function skippedCard(d, act) {
    const list = d.skipped || [];
    if (!list.length) return null;
    const row = x => h('li', { class: 'en-skip__row', dataset: { skipped: x.id } },
      h('span', { class: 'en-skip__main' }, h('span', { class: 'en-row__name' }, x.label || x.id), h('span', { class: 'en-row__sub muted' }, t('energy.skipped.why.' + x.why))),
      h('span', { class: 'en-skip__w' }, x.w == null ? '—' : LL.fmtW(x.w)),
      x.why === 'hidden' && act ? h('button', { class: 'en-act', type: 'button', onclick: () => act({ hidden: act.hidden.filter(i => i !== x.id) }) }, t('energy.skipped.show')) : h('span', {}));
    return LL.keepOpen.details('en:skipped', false, h('span', { class: 'en-set__sum' }, t('energy.skipped.title', { n: LL.fmtNum(list.length, 0) })),
      h('div', { class: 'en-skip' }, h('p', { class: 'muted en-skip__lead' }, t('energy.skipped.lead')), h('ul', { class: 'en-list' }, list.map(row))));
  }

  // ------------------------------------------------------------------ settings (operators)
  function settingsCard(d, reload) {
    const s = d.settings || {};
    const can = LL.can('operate');
    const num = (v, step) => h('input', { class: 'input', type: 'number', inputmode: 'decimal', min: '0', step, value: v == null ? '' : String(v), disabled: !can });
    // step 'any': a tariff such as 0.2916 per kWh must pass the browser's own check (the server takes 0..10, six places)
    const price = num(s.price, 'any'), feed = num(s.feed_in, 'any'), co2 = num(s.co2_g_kwh, '1');
    const cur = h('input', { class: 'input', type: 'text', maxlength: '3', autocapitalize: 'characters', value: s.currency || 'EUR', disabled: !can, 'aria-label': t('energy.set.currency') });
    const msg = h('div', { class: 'msg', role: 'status' });
    const val = el => (el.value.trim() === '' ? null : Number(el.value.replace(',', '.')));
    const src = (what, x) => t('energy.set.source.' + (x ? x.source : 'none'), { what });
    const save = async ev => {
      ev.preventDefault();
      msg.className = 'msg'; msg.textContent = '';
      try {
        await api('/api/energy/settings', { method: 'PUT', body: { price: val(price), currency: cur.value.trim().toUpperCase() || 'EUR', feed_in: val(feed), co2_g_kwh: val(co2),
          price_entity: s.price_entity || null, co2_entity: s.co2_entity || null } });
        msg.className = 'msg msg--ok'; msg.textContent = t('settings.saved');
        reload();
      } catch (e) { msg.className = 'msg msg--error'; msg.textContent = LL.errText(e); }
    };
    return LL.keepOpen.details('en:settings', false, h('span', { class: 'en-set__sum' }, t('energy.set.title')),
      h('form', { class: 'en-set', onsubmit: save },
        h('p', { class: 'muted en-set__lead' }, t('energy.set.lead')),
        h('div', { class: 'en-set__grid' },
          LL.field(t('energy.set.price'), price, src(t('energy.set.price'), d.price)), LL.field(t('energy.set.currency'), cur),
          LL.field(t('energy.set.feed_in'), feed), LL.field(t('energy.set.co2'), co2, src(t('energy.set.co2'), d.co2))),
        can ? h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--primary btn--sm', type: 'submit' }, t('energy.set.save'))) : h('p', { class: 'muted' }, t('energy.set.viewer')),
        msg));
  }

  // ------------------------------------------------------------------ the page
  async function view(app) {
    const fr = LL.frame(app, 'energy', h('div', { class: 'page page--wide en-page' }));
    const page = fr.main.firstChild;
    const fresh = h('span', { class: 'muted en-fresh' });
    const body = h('div', { class: 'en-body' }, h('div', { class: 'skeleton' }));
    page.append(h('header', { class: 'page__head en-head' }, h('div', {}, h('h1', {}, t('nav.energy')), h('p', { class: 'muted en-lead' }, t('energy.lead'))), fresh), body);
    let seq = 0, timer = 0, first = true;
    const actMsg = h('p', { class: 'msg msg--error en-act-msg', role: 'status', hidden: true });
    let cur = null;                                  // the last picture (the hidden list a change starts from)
    let reloadNow = () => {};
    const act = async patch => {
      actMsg.hidden = true;
      try { await api('/api/energy/settings', { method: 'PUT', body: patch }); reloadNow(); } catch (e) { actMsg.textContent = LL.errText(e); actMsg.hidden = false; }
    };
    Object.defineProperty(act, 'hidden', { get: () => (cur && cur.settings && cur.settings.hidden) || [] });
    const wide = () => window.innerWidth > 860;
    async function load() {
      const my = ++seq;
      let d;
      try { d = await api('/api/energy'); } catch (e) { if (first) body.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (my !== seq || !page.isConnected) return;
      LL.energyPeek = d;
      cur = d;
      const ae = document.activeElement;
      if (!first && ae && body.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) return;     // never redraw under a typing hand
      const y = fr.main.scrollTop;
      first = false;
      if (!d.available) {
        body.replaceChildren(h('section', { class: 'card en-empty' }, h('h2', {}, t('energy.empty.title')), h('p', {}, t('energy.empty.text')),
          h('a', { class: 'btn btn--sm', href: '#/settings' }, t('energy.empty.connect'))));
        fresh.textContent = '';
        return;
      }
      anim.paths = [];
      const max = Math.max(1, ...(d.lanes || []).map(l => l.w));
      const fl = d.fresh || {};
      fresh.textContent = fl.age_s == null ? t('energy.fresh.none') : t('energy.fresh.' + (fl.level === 'fresh' ? 'fresh' : 'late'), { age: LL.fmtAge(fl.age_s) });
      fresh.dataset.level = fl.level || 'none';
      const skip = skippedCard(d, LL.can('operate') ? act : null);
      body.replaceChildren(
        kpis(d),
        h('section', { class: 'card en-flow', 'aria-labelledby': 'en-h-flow', dataset: { level: fl.level || 'none' } },
          h('header', { class: 'en-card__head' }, h('h2', { id: 'en-h-flow' }, t('energy.flow.title')), h('span', { class: 'muted en-flow__hint' }, t('energy.flow.hint', { w: LL.fmtNum(DOT_W, 0) }))),
          figure(d, wide())),
        h('section', { class: 'card en-lanes', 'aria-labelledby': 'en-h-lanes' },
          h('header', { class: 'en-card__head' }, h('h2', { id: 'en-h-lanes' }, t('energy.lanes.title')), h('span', { class: 'now-card__n' }, LL.fmtNum((d.lanes || []).length, 0))),
          d.grid_share != null && d.grid_share < 0.995 ? h('p', { class: 'muted en-share-hint' }, t('energy.lanes.share_hint', { pct: LL.fmtNum(d.grid_share * 100, 0) })) : null,
          (d.lanes || []).length ? h('ul', { class: 'en-list' }, d.lanes.map(l => laneRow(d, l, max, LL.can('operate') ? act : null))) : h('p', { class: 'muted' }, t('energy.lanes.none')),
          actMsg,
          LL.can('operate') && d.home_source && d.home_source.marked ? h('p', { class: 'muted en-rest' }, t('energy.home.from', { name: d.home_source.label }), ' ',
            h('button', { class: 'en-act', type: 'button', onclick: () => act({ home_entity: null }) }, t('energy.home.auto'))) : null,
          d.rest_w ? h('p', { class: 'muted en-rest' }, t('energy.rest_text', { w: LL.fmtW(d.rest_w) })) : null),
        ...(skip ? [h('section', { class: 'card en-settings en-skipped' }, skip)] : []),
        h('section', { class: 'card en-settings' }, settingsCard(d, load)));
      fr.main.scrollTop = y;
      startDots();
    }
    reloadNow = load;
    await load();
    timer = setInterval(() => { if (!page.isConnected) { clearInterval(timer); return; } if (!document.hidden) load(); }, REFRESH_MS);
    const onResize = () => { if (page.isConnected && LL.energyPeek) load(); };
    let rz = 0; const resize = () => { clearTimeout(rz); rz = setTimeout(onResize, 250); };
    window.addEventListener('resize', resize);
    const destroy = fr.destroy;
    fr.destroy = () => { clearInterval(timer); window.removeEventListener('resize', resize); anim.paths = []; if (destroy) destroy.call(fr); };
    return fr;
  }
  LL.view('energy', app => view(app));

  // ------------------------------------------------------------------ peek (start page, "Now"): at most every 30 s, only with hub power
  let peekAt = 0, peeking = false;
  function peek(redraw) {
    if (peeking || Date.now() - peekAt < 30000) return;
    peeking = true; peekAt = Date.now();
    api('/api/energy').then(d => {
      const had = !!(LL.energyPeek && LL.energyPeek.available);
      LL.energyPeek = d;
      if (had !== !!d.available && redraw) redraw();
    }).catch(() => null).finally(() => { peeking = false; });
  }
  function smallCard() {
    const d = LL.energyPeek;
    const td = d.today || {};
    const cur = d.price ? d.price.currency : null;
    return h('div', { class: 'en-small' },
      h('div', { class: 'en-small__row' },
        h('span', { class: 'en-small__fig' }, h('b', {}, LL.fmtW(d.now.home_w)), h('span', { class: 'muted' }, t('energy.kpi.home'))),
        d.now.solar_w != null ? h('span', { class: 'en-small__fig' }, h('b', {}, LL.fmtW(d.now.solar_w)), h('span', { class: 'muted' }, t('energy.src.solar'))) : null,
        h('span', { class: 'en-small__fig' }, h('b', {}, td.cost != null ? money(td.cost, cur, 2) : kwh(td.kwh)), h('span', { class: 'muted' }, t('energy.kpi.today')))),
      (d.lanes || []).length ? h('p', { class: 'muted en-small__top' }, t('energy.small.top', { name: d.lanes[0].label || d.lanes[0].name, w: LL.fmtW(d.lanes[0].w) })) : null,
      h('a', { class: 'btn btn--ghost btn--sm', href: '#/energy' }, t('energy.open')));
  }
  if (LL.registerOverviewCard) {
    LL.registerOverviewCard({ id: 'energy', order: 14, title: () => t('nav.energy'),
      available: () => { peek(LL.overviewRedraw); return !!(LL.energyPeek && LL.energyPeek.available); }, render: smallCard });
  }
  if (LL.registerNowCard) {
    LL.registerNowCard({ id: 'energy', order: 10,
      available: () => { peek(LL.nowRedraw); return !!(LL.energyPeek && LL.energyPeek.available); },
      render: () => h('section', { class: 'card now-card en-now', 'aria-labelledby': 'now-h-energy' },
        h('header', { class: 'now-card__head' }, h('h2', { id: 'now-h-energy' }, t('nav.energy'))), smallCard()) });
  }
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'go.energy', group: 'views', order: 3.7, label: () => t('cmdk.go', { name: t('nav.energy') }), keywords: () => t('energy.cmd.kw'),
      available: () => { peek(null); return !!(LL.energyPeek && LL.energyPeek.available); }, run: () => LL.go('#/energy') });
  }
})();
