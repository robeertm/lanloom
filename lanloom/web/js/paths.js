/* Lanloom — data paths: where the devices send data on the internet, by country, on a calm world map.

   Data: GET /api/paths?window=24h|7d|30d (countries, devices, device x country matrix, unusual behaviour). Only countries and bytes,
   no addresses. Base map: Lanloom.worldmap (Natural Earth, offline). Arcs (SVG) from the home country to every destination, width by
   bytes; outside the EU amber, in the EU teal. Dots travel on the arcs on a canvas above the map: only while the map is on screen and
   the tab is visible, never with "reduce motion", and the drawing rate steps down by itself when frames come slowly (no GPU).
   Full screen: button at the map, Esc / close / back gesture close it; window switch, legend and selection stay usable. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const WINDOWS = ['24h', '7d', '30d'];
  const NS = 'http://www.w3.org/2000/svg';
  const FULL_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
  const ICON = {
    new_country: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.8 3.9 5.8 3.9 9s-1.3 6.2-3.9 9c-2.6-2.8-3.9-5.8-3.9-9S9.4 5.8 12 3z"/></svg>',
    heavy: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V5M6 11l6-6 6 6"/><path d="M5 20h14"/></svg>',
    offline_net: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.4-3 8.3-7 10-4-1.7-7-5.6-7-10V6z"/><path d="M9 9l6 6M15 9l-6 6"/></svg>'
  };
  LL.NAV_PATHS_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.8 3.9 5.8 3.9 9s-1.3 6.2-3.9 9c-2.6-2.8-3.9-5.8-3.9-9S9.4 5.8 12 3z"/></svg>';

  const map = () => LL.worldmap || { w: 1000, h: 438, countries: {}, points: {} };
  const point = cc => { const m = map(), c = m.countries[cc]; return c ? [c.x, c.y] : (m.points[cc] || null); };
  let names = null;
  function countryName(cc) {
    if (!cc) return '';
    if (cc === '~unassigned') return t('paths.unassigned');
    try {
      if (!names || names.lang !== LL.lang) names = { lang: LL.lang, dn: new Intl.DisplayNames([LL.lang], { type: 'region' }) };
      return names.dn.of(cc) || cc;
    } catch (e) { return cc; }
  }
  LL.countryName = countryName;
  function browserRegion() {
    for (const l of (navigator.languages || [navigator.language || ''])) {
      const m = /^[a-z]{2,3}[-_]([A-Z]{2})\b/i.exec(l || '');
      if (m) return m[1].toUpperCase();
    }
    return '';
  }
  const pct = x => {
    const v = x * 100;
    return (v > 0 && v < 1 ? v.toFixed(1) : String(Math.round(v))) + ' %';
  };
  const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ------------------------------------------------------------------ view state (kept across live redraws)
  const S = { window: '24h', d: null, sel: null, el: null, full: null, anims: new Set(), timer: 0, loading: false, error: null };
  try { const w = localStorage.getItem('lanloom-paths-window'); if (WINDOWS.includes(w)) S.window = w; } catch (e) { /* private mode */ }

  const home = () => (S.d && S.d.home) || browserRegion();
  const devTitle = d => d ? (d.title || (d.self ? t('paths.this_host') : d.id)) : '';
  const deviceById = id => (S.d && S.d.devices.find(d => d.id === id)) || null;
  const countryRow = cc => (S.d && S.d.countries.find(c => c.cc === cc)) || null;
  const isEU = cc => !!(countryRow(cc) && countryRow(cc).eu);

  async function load() {
    if (S.loading) return;
    S.loading = true;
    try { S.d = await api('/api/paths?window=' + S.window); S.error = null; }
    catch (e) { S.error = LL.errText(e); }
    S.loading = false;
    render();
  }
  function setWindow(w) {
    if (!WINDOWS.includes(w) || w === S.window) return;
    S.window = w;
    try { localStorage.setItem('lanloom-paths-window', w); } catch (e) { /* ignore */ }
    load();
  }
  function select(sel) {
    if (sel && S.sel && sel.type === S.sel.type && sel.id === S.sel.id) sel = null;
    S.sel = sel;
    render();
  }

  // ------------------------------------------------------------------ what the map shows for the current selection
  function targets() {
    const d = S.d;
    if (!d) return [];
    const sel = S.sel;
    if (sel && sel.type === 'device') {
      return d.matrix.filter(m => m[0] === sel.id && !m[1].startsWith('~')).map(m => ({ cc: m[1], tx: m[2], rx: m[3] }));
    }
    return d.countries.map(c => ({ cc: c.cc, tx: c.tx, rx: c.rx }));
  }
  function arcGeom(a, b) {
    const [x0, y0] = a, [x1, y1] = b, dx = x1 - x0, dy = y1 - y0, dist = Math.hypot(dx, dy) || 1;
    let nx = -dy / dist, ny = dx / dist;
    if (ny > 0) { nx = -nx; ny = -ny; }                    // bulge towards the north
    const lift = Math.max(6, Math.min(150, dist * 0.3));
    return { x0, y0, x1, y1, cx: (x0 + x1) / 2 + nx * lift, cy: (y0 + y1) / 2 + ny * lift };
  }
  const onArc = (g, u) => { const v = 1 - u; return [v * v * g.x0 + 2 * v * u * g.cx + u * u * g.x1, v * v * g.y0 + 2 * v * u * g.cy + u * u * g.y1]; };

  function svgEl(tag, attrs, kids) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
    (kids || []).forEach(k => k && el.appendChild(k));
    return el;
  }
  function titled(el, text) { if (text) el.appendChild(svgEl('title', {}, [document.createTextNode(text)])); return el; }

  /* Builds the map SVG. Returns {svg, arcs} — arcs feed the dot canvas. */
  function buildMap(labels) {
    const m = map(), hc = home(), hp = point(hc);
    const list = targets(), by = new Map(list.map(x => [x.cc, x]));
    const others = list.filter(x => x.cc !== hc && point(x.cc));
    const max = Math.max(1, ...others.map(x => x.tx + x.rx));
    const selCC = S.sel && S.sel.type === 'country' ? S.sel.id : null;
    const shapes = svgEl('g', { class: 'pw-shapes' });
    for (const [cc, c] of Object.entries(m.countries)) {
      const x = by.get(cc), cls = ['pw-land'];
      let style = null;
      if (cc === hc) cls.push('pw-home');
      else if (x) { cls.push('pw-dest', isEU(cc) ? 'pw-eu' : 'pw-out'); style = '--pw-a:' + (0.2 + 0.6 * Math.sqrt((x.tx + x.rx) / max)).toFixed(2); }
      if (selCC === cc) cls.push('is-selected');
      const p = svgEl('path', { class: cls.join(' '), d: c.d, 'data-cc': cc, style });
      shapes.appendChild(titled(p, countryName(cc) + (x ? ' — ' + LL.fmtBytes(x.tx + x.rx) : '')));
    }
    const small = svgEl('g', { class: 'pw-small' });
    for (const x of others) {
      if (m.countries[x.cc]) continue;
      const [px, py] = m.points[x.cc];
      small.appendChild(titled(svgEl('circle', { class: 'pw-dot ' + (isEU(x.cc) ? 'pw-eu' : 'pw-out') + (selCC === x.cc ? ' is-selected' : ''),
        cx: px, cy: py, r: 3.2, 'data-cc': x.cc }), countryName(x.cc) + ' — ' + LL.fmtBytes(x.tx + x.rx)));
    }
    const arcs = [];
    const arcG = svgEl('g', { class: 'pw-arcs' });
    if (hp) {
      for (const x of others.slice().sort((a, b) => (a.tx + a.rx) - (b.tx + b.rx))) {
        const g = arcGeom(hp, point(x.cc)), share = (x.tx + x.rx) / max, w = 1 + 4.6 * Math.sqrt(share);
        const quiet = !!(selCC && selCC !== x.cc), side = isEU(x.cc) ? 'pw-eu' : 'pw-out';
        arcs.push(Object.assign(g, { cc: x.cc, share, out: !isEU(x.cc), inbound: x.rx >= x.tx, quiet }));
        arcG.appendChild(svgEl('path', { class: 'pw-arc ' + side + (quiet ? ' is-quiet' : '') + (selCC === x.cc ? ' is-selected' : ''), 'data-cc': x.cc,
          d: 'M' + g.x0.toFixed(1) + ' ' + g.y0.toFixed(1) + 'Q' + g.cx.toFixed(1) + ' ' + g.cy.toFixed(1) + ' ' + g.x1.toFixed(1) + ' ' + g.y1.toFixed(1),
          style: 'stroke-width:' + w.toFixed(2) }));
        arcG.appendChild(svgEl('circle', { class: 'pw-end ' + side + (quiet ? ' is-quiet' : ''), cx: g.x1.toFixed(1), cy: g.y1.toFixed(1), r: (1.6 + w * 0.45).toFixed(1) }));
      }
    }
    const homeG = hp ? svgEl('g', { class: 'pw-homemark' }, [svgEl('circle', { class: 'pw-home-ring', cx: hp[0], cy: hp[1], r: 7 }),
      svgEl('circle', { class: 'pw-home-core', cx: hp[0], cy: hp[1], r: 3.4 })]) : null;
    const labelG = svgEl('g', { class: 'pw-labels' });
    if (labels && hp) {
      const used = [[hp[0] - 6, hp[1] - 6, hp[0] + 6, hp[1] + 6]];
      const near = cc => { const q = point(cc); return Math.hypot(q[0] - hp[0], q[1] - hp[1]) < 75; };
      const cand = others.filter(x => !near(x.cc)).sort((a, b) => (b.tx + b.rx) - (a.tx + a.rx)).slice(0, 9);
      if (selCC && by.get(selCC) && !cand.some(x => x.cc === selCC) && selCC !== hc) cand.unshift(by.get(selCC));
      for (const x of cand) {
        const [px, py] = point(x.cc), t1 = countryName(x.cc), t2 = LL.fmtBytes(x.tx + x.rx);
        const bw = Math.max(t1.length * 6.1, t2.length * 5.6) + 6, bh = 24;
        const spots = [[px + 7, py - 20], [px - 7 - bw, py - 20], [px + 7, py + 4], [px - 7 - bw, py + 4], [px - bw / 2, py - 30], [px - bw / 2, py + 8]];
        const spot = spots.find(([lx, ly]) => lx > 2 && lx + bw < m.w - 2 && ly > 2 && ly + bh < m.h - 2
          && !used.some(r => lx < r[2] && lx + bw > r[0] && ly < r[3] && ly + bh > r[1]));
        if (!spot) continue;
        used.push([spot[0], spot[1], spot[0] + bw, spot[1] + bh]);
        const tx = (spot[0] + 3).toFixed(1);
        labelG.appendChild(svgEl('text', { class: 'pw-label' + (isEU(x.cc) ? '' : ' pw-out'), x: tx, y: (spot[1] + 10).toFixed(1), 'data-cc': x.cc }, [
          svgEl('tspan', { class: 'pw-label__name' }, [document.createTextNode(t1)]),
          svgEl('tspan', { class: 'pw-label__value', x: tx, dy: 12 }, [document.createTextNode(t2)])]));
      }
      const hx = by.get(hc);
      if (hx) {
        labelG.appendChild(svgEl('text', { class: 'pw-label pw-label--home', x: (hp[0] - 9).toFixed(1), y: (hp[1] + 20).toFixed(1), 'text-anchor': 'end', 'data-cc': hc }, [
          svgEl('tspan', { class: 'pw-label__name' }, [document.createTextNode(countryName(hc))]),
          svgEl('tspan', { class: 'pw-label__value', x: (hp[0] - 9).toFixed(1), dy: 12 }, [document.createTextNode(LL.fmtBytes(hx.tx + hx.rx))])]));
      }
    }
    const svg = svgEl('svg', { class: 'pw-map', viewBox: '0 0 ' + m.w + ' ' + m.h, role: 'img', 'aria-label': t('paths.map.aria'), preserveAspectRatio: 'xMidYMid meet' },
      [shapes, small, arcG, homeG, labelG]);
    svg.addEventListener('click', ev => {
      const el = ev.target.closest('[data-cc]');
      if (el && (by.has(el.dataset.cc))) select({ type: 'country', id: el.dataset.cc });
    });
    return { svg, arcs };
  }

  // ------------------------------------------------------------------ dots on the arcs (canvas, only while visible)
  function startDots(stage, svg, arcs) {
    if (reduced() || !arcs.length) return null;
    const m = map();
    const cv = h('canvas', { class: 'pw-canvas', 'aria-hidden': 'true' });
    stage.appendChild(cv);
    const css = getComputedStyle(stage);
    const color = { out: css.getPropertyValue('--pw-dot-out').trim() || '#C98600', eu: css.getPropertyValue('--pw-dot-eu').trim() || '#0C7A6A' };
    const anim = { raf: 0, ro: null, io: null, visible: true, level: 1, cv };
    let scale = 1, dpr = 1, last = 0, avg = 16.7, prev = 0, calmSince = 0;
    const size = () => {
      const r = stage.getBoundingClientRect(), rs = svg.getBoundingClientRect();
      dpr = Math.min(2, window.devicePixelRatio || 1);
      cv.style.left = (rs.left - r.left + stage.scrollLeft) + 'px'; cv.style.top = (rs.top - r.top + stage.scrollTop) + 'px';
      cv.style.width = rs.width + 'px'; cv.style.height = rs.height + 'px';
      cv.width = Math.max(1, Math.round(rs.width * dpr)); cv.height = Math.max(1, Math.round(rs.height * dpr));
      scale = rs.width / m.w;
    };
    size();
    anim.ro = new ResizeObserver(size); anim.ro.observe(stage);
    anim.io = new IntersectionObserver(e => { anim.visible = e[0].isIntersecting; }); anim.io.observe(stage);
    const plan = arcs.filter(a => !a.quiet).map(a => ({ a, n: Math.max(1, Math.min(5, Math.round(1 + 4 * Math.sqrt(a.share)))),
      dur: 2600 + Math.hypot(a.x1 - a.x0, a.y1 - a.y0) * 9, r: 1.4 + 1.3 * Math.sqrt(a.share) }));
    /* Self-throttling: without a GPU every canvas frame costs the whole area. When page frames come slower than ~45/s on
       average, draw only every 2nd (then 3rd) frame; after 10 s of smooth frames step back up. */
    const frame = ts => {
      anim.raf = requestAnimationFrame(frame);
      if (!cv.isConnected) { stopDots(anim); return; }
      if (document.hidden || !anim.visible) { prev = 0; return; }
      if (prev) {
        avg += (Math.min(200, ts - prev) - avg) * 0.05;
        if (avg > 22 && anim.level < 3) { anim.level++; avg = 16.7; calmSince = ts; }
        else if (avg < 18.5 && anim.level > 1 && ts - calmSince > 10000) { anim.level--; calmSince = ts; }
      }
      prev = ts;
      if (anim.level > 1 && ts - last < 1000 / (60 / anim.level) - 1) return;
      last = ts;
      const c = cv.getContext('2d');
      c.setTransform(dpr * scale, 0, 0, dpr * scale, 0, 0);
      c.clearRect(0, 0, m.w, m.h);
      for (const p of plan) {
        c.fillStyle = p.a.out ? color.out : color.eu;
        for (let i = 0; i < p.n; i++) {
          let u = ((ts / p.dur) + i / p.n) % 1;
          if (p.a.inbound) u = 1 - u;
          const [x, y] = onArc(p.a, u);
          c.globalAlpha = 0.25 + 0.75 * Math.sin(Math.PI * u);
          c.beginPath(); c.arc(x, y, p.r / Math.max(0.6, scale) * 0.9, 0, Math.PI * 2); c.fill();
        }
      }
      c.globalAlpha = 1;
    };
    anim.raf = requestAnimationFrame(frame);
    S.anims.add(anim);
    return anim;
  }
  function stopDots(anim) {
    if (!anim) return;
    if (anim.raf) cancelAnimationFrame(anim.raf);
    if (anim.ro) anim.ro.disconnect();
    if (anim.io) anim.io.disconnect();
    if (anim.cv) anim.cv.remove();
    S.anims.delete(anim);
  }
  function stopAllDots() { [...S.anims].forEach(stopDots); }
  LL.pathsAnimLevel = () => [...S.anims].map(a => a.level);       // for tests: current drawing level per running canvas

  // ------------------------------------------------------------------ building blocks
  function seg() {
    return h('div', { class: 'seg pw-seg', role: 'group', 'aria-label': t('paths.window.label') }, WINDOWS.map(w =>
      h('button', { type: 'button', class: 'seg__btn' + (w === S.window ? ' is-active' : ''), 'aria-pressed': String(w === S.window), onclick: () => setWindow(w) }, t('paths.window.' + w))));
  }
  function legend() {
    const hc = home();
    return h('div', { class: 'pw-legend' },
      h('span', { class: 'pw-legend__item' }, h('i', { class: 'pw-swatch pw-swatch--out' }), t('paths.legend.out')),
      h('span', { class: 'pw-legend__item' }, h('i', { class: 'pw-swatch pw-swatch--eu' }), t('paths.legend.eu')),
      hc ? h('span', { class: 'pw-legend__item' }, h('i', { class: 'pw-swatch pw-swatch--home' }), t('paths.legend.home', { country: countryName(hc) })) : null,
      h('span', { class: 'pw-legend__item pw-legend__hint' }, t('paths.legend.width')));
  }
  function selChip() {
    if (!S.sel) return null;
    const label = S.sel.type === 'country' ? countryName(S.sel.id) : devTitle(deviceById(S.sel.id));
    return h('span', { class: 'chip chip--accent pw-selchip' }, h('span', { class: 'pw-selchip__text' }, t('paths.selection', { name: label })),
      h('button', { type: 'button', class: 'pw-selchip__x', 'aria-label': t('paths.selection.clear'), onclick: () => select(null) }, '×'));
  }
  function kpis(d) {
    const tt = d.totals, total = tt.tx + tt.rx + tt.unassigned;
    const tile = (value, label, cls) => h('div', { class: 'pw-kpi' + (cls ? ' ' + cls : '') }, h('b', {}, value), h('span', {}, label));
    const share = tt.outside_eu_share == null ? t('paths.no_data') : pct(tt.outside_eu_share);
    return h('div', { class: 'pw-kpis' },
      tile(total ? LL.fmtBytes(total) : '–', t('paths.kpi.exchanged')),
      tile(String(tt.countries), t(tt.countries === 1 ? 'paths.kpi.country' : 'paths.kpi.countries')),
      tile(share, t('paths.kpi.outside_eu'), tt.outside_eu_share > 0 ? 'pw-kpi--out' : (tt.outside_eu_share == null ? 'pw-kpi--none' : '')),
      tile(String(tt.devices), t(tt.devices === 1 ? 'paths.kpi.device' : 'paths.kpi.devices')),
      tile(String(d.unusual.length), t('paths.kpi.unusual'), d.unusual.length ? 'pw-kpi--warn' : ''));
  }
  function note(d) {
    const tt = d.totals, parts = [];
    if (tt.own > 0 || tt.unassigned > 0) parts.push(t('paths.note.share', { bytes: LL.fmtBytes(tt.attributable) }));
    if (tt.own > 0) parts.push(t('paths.note.own', { bytes: LL.fmtBytes(tt.own) }));
    if (tt.unassigned > 0) parts.push(t('paths.note.unassigned', { bytes: LL.fmtBytes(tt.unassigned) }));
    return parts.length ? h('p', { class: 'pw-note' }, parts.join(' ')) : null;
  }
  function bar(share, out) {
    return h('span', { class: 'pw-bar' }, h('i', { class: out ? 'pw-out' : 'pw-eu', style: 'width:' + Math.max(2, Math.round(share * 100)) + '%' }));
  }
  function unusualText(u) {
    const dev = devTitle(deviceById(u.device)) || u.device;
    if (u.kind === 'new_country') return t('paths.unusual.new_country', { device: dev, country: countryName(u.cc), bytes: LL.fmtBytes(u.bytes) });
    if (u.kind === 'heavy') return t('paths.unusual.heavy', { device: dev, bytes: LL.fmtBytes(u.bytes), factor: u.typical ? Math.round(u.bytes / u.typical) : '>3', typical: LL.fmtBytes(u.typical) });
    return t('paths.unusual.offline_net', { device: dev, network: u.network, bytes: LL.fmtBytes(u.bytes) });
  }
  function unusualCard(d) {
    if (!d.unusual.length) return null;
    return h('section', { class: 'card pw-card pw-unusual' }, h('header', { class: 'pw-card__head' }, h('h2', {}, t('paths.unusual.title')),
      h('span', { class: 'muted pw-card__hint' }, t('paths.unusual.hint'))),
    h('ul', { class: 'pw-unusual__list' }, d.unusual.map(u => {
      const li = h('li', {}, h('button', { type: 'button', class: 'pw-unusual__row', onclick: () => select({ type: 'device', id: u.device }) },
        h('span', { class: 'pw-unusual__icon pw-unusual__icon--' + u.kind, 'aria-hidden': 'true' }),
        h('span', { class: 'pw-unusual__text' }, h('b', {}, t('paths.unusual.kind.' + u.kind)), h('span', {}, unusualText(u)))));
      li.querySelector('.pw-unusual__icon').innerHTML = ICON[u.kind] || '';
      return li;
    })));
  }
  function countriesCard(d) {
    let rows = d.countries.map(c => ({ cc: c.cc, b: c.tx + c.rx, devices: c.devices, eu: c.eu }));
    if (S.sel && S.sel.type === 'device') {
      rows = d.matrix.filter(m => m[0] === S.sel.id && m[1] !== '~own').map(m => ({ cc: m[1], b: m[2] + m[3], devices: null, eu: isEU(m[1]) }));
    }
    rows.sort((a, b) => b.b - a.b);
    const max = Math.max(1, ...rows.map(r => r.b));
    const selCC = S.sel && S.sel.type === 'country' ? S.sel.id : null;
    const title = S.sel && S.sel.type === 'device' ? t('paths.countries.of', { device: devTitle(deviceById(S.sel.id)) }) : t('paths.countries.title');
    return h('section', { class: 'card pw-card' }, h('header', { class: 'pw-card__head' }, h('h2', {}, title)),
      rows.length ? h('ul', { class: 'pw-list' }, rows.slice(0, 40).map(r => h('li', {}, h('button', { type: 'button',
        class: 'pw-row' + (selCC === r.cc ? ' is-selected' : ''), disabled: r.cc.startsWith('~') ? true : null,
        onclick: () => select({ type: 'country', id: r.cc }) },
      h('span', { class: 'pw-row__cc mono' }, r.cc.startsWith('~') ? '–' : r.cc),
      h('span', { class: 'pw-row__name' }, countryName(r.cc)),
      h('span', { class: 'pw-row__meta muted' }, r.devices != null ? t(r.devices === 1 ? 'paths.n_device' : 'paths.n_devices', { n: r.devices }) : ''),
      h('span', { class: 'pw-row__value' }, LL.fmtBytes(r.b)),
      bar(r.b / max, !r.eu && !r.cc.startsWith('~')))))) : h('p', { class: 'muted pw-empty' }, t('paths.empty.list')));
  }
  function devicesCard(d) {
    let rows = d.devices.map(x => ({ id: x.id, b: x.tx + x.rx, n: x.countries, dev: x }));
    if (S.sel && S.sel.type === 'country') {
      rows = d.matrix.filter(m => m[1] === S.sel.id).map(m => ({ id: m[0], b: m[2] + m[3], n: null, dev: deviceById(m[0]) }));
    }
    rows.sort((a, b) => b.b - a.b);
    const max = Math.max(1, ...rows.map(r => r.b));
    const selDev = S.sel && S.sel.type === 'device' ? S.sel.id : null;
    const title = S.sel && S.sel.type === 'country' ? t('paths.devices.in', { country: countryName(S.sel.id) }) : t('paths.devices.title');
    return h('section', { class: 'card pw-card' }, h('header', { class: 'pw-card__head' }, h('h2', {}, title)),
      rows.length ? h('ul', { class: 'pw-list' }, rows.slice(0, 40).map(r => h('li', {}, h('button', { type: 'button',
        class: 'pw-row pw-row--device' + (selDev === r.id ? ' is-selected' : ''), onclick: () => select({ type: 'device', id: r.id }) },
      h('span', { class: 'pw-row__name' }, devTitle(r.dev) || r.id),
      h('span', { class: 'pw-row__meta muted' }, r.n != null ? t(r.n === 1 ? 'paths.n_country' : 'paths.n_countries', { n: r.n }) : (r.dev && r.dev.ip) || ''),
      h('span', { class: 'pw-row__value' }, LL.fmtBytes(r.b)),
      bar(r.b / max, false))))) : h('p', { class: 'muted pw-empty' }, t('paths.empty.list')));
  }
  function attribution(d) {
    const g = (d && d.geo) || {}, a = g.attribution || { text: 'IP Geolocation by DB-IP', url: 'https://db-ip.com', license: 'CC BY 4.0', license_url: 'https://creativecommons.org/licenses/by/4.0/' };
    return h('p', { class: 'pw-attr muted' },
      h('a', { href: a.url, target: '_blank', rel: 'noopener noreferrer' }, a.text), ' (', h('a', { href: a.license_url, target: '_blank', rel: 'noopener noreferrer' }, a.license),
      g.month ? ', ' + g.month : '', ') · ', t('paths.attr.map'), ' · ', t('paths.attr.source'), ' · ',
      h('a', { href: '#/settings', class: 'pw-attr__settings' }, t('paths.attr.settings')));
  }

  // ------------------------------------------------------------------ page
  function render() {
    if (!S.el) return;
    stopAllDots();
    const page = S.el, d = S.d;
    const head = h('div', { class: 'page__head pw-head' }, h('h1', {}, t('nav.paths')), seg());
    const intro = h('p', { class: 'muted pw-intro' }, t('paths.intro'));
    if (!d) {
      page.replaceChildren(head, intro, h('p', { class: S.error ? 'msg msg--error' : 'muted' }, S.error || t('paths.loading')));
      return;
    }
    const kids = [head, intro];
    if (!d.geo.loaded) kids.push(h('p', { class: 'msg msg--info' }, t(d.geo.error ? 'paths.geo.broken' : 'paths.geo.loading')));
    kids.push(kpis(d));
    const n = note(d);
    if (n) kids.push(n);
    const wide = window.innerWidth >= 700;
    const built = buildMap(wide);
    const stage = h('div', { class: 'pw-stage' }, built.svg);
    const fullBtn = h('button', { type: 'button', class: 'icon-btn pw-full-btn', title: t('paths.full.open'), 'aria-label': t('paths.full.open'), onclick: () => openFull() });
    fullBtn.innerHTML = FULL_ICON;
    const empty = !d.countries.length ? h('p', { class: 'pw-map-empty' }, t('paths.empty.map')) : null;
    kids.push(h('section', { class: 'card pw-board' }, h('div', { class: 'pw-board__top' }, legend(), h('div', { class: 'pw-board__tools' }, selChip(), fullBtn)), stage, empty));
    const un = unusualCard(d);
    if (un) kids.push(un);
    kids.push(h('div', { class: 'pw-grid' }, countriesCard(d), devicesCard(d)));
    kids.push(attribution(d));
    page.replaceChildren(...kids);
    if (!S.full) requestAnimationFrame(() => { if (stage.isConnected && !S.full) startDots(stage, built.svg, built.arcs); });
    if (S.full) renderFull();
  }

  // ------------------------------------------------------------------ full screen
  function openFull() {
    if (S.full || !S.d) return;
    stopAllDots();                                      // the map below rests while the full screen is open
    const close = h('button', { type: 'button', class: 'icon-btn pw-full__close', 'aria-label': t('paths.full.close'), title: t('paths.full.close'), onclick: () => closeFull() }, '✕');
    const el = h('div', { class: 'pw-full', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('paths.full.aria') },
      h('header', { class: 'pw-full__head' }, h('h2', {}, t('nav.paths')), h('div', { class: 'pw-full__seg' }), close),
      h('div', { class: 'pw-full__area' }, h('div', { class: 'pw-stage pw-full__stage' })),
      h('footer', { class: 'pw-full__foot' }, h('div', { class: 'pw-full__legend' }), h('div', { class: 'pw-full__sel' })));
    const f = S.full = { el, pushed: false, centred: false };
    f.key = ev => { if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closeFull(); } };
    f.pop = () => teardownFull();
    f.hash = () => teardownFull();
    f.resize = () => fitFull();
    document.addEventListener('keydown', f.key, true);
    window.addEventListener('popstate', f.pop);
    window.addEventListener('hashchange', f.hash);
    window.addEventListener('resize', f.resize);
    try { history.pushState({ lanloomPathsFull: 1 }, '', location.href); f.pushed = true; } catch (e) { /* Esc and the button still close */ }
    document.body.appendChild(el);
    document.body.classList.add('pw-noscroll');
    renderFull();
    close.focus();
  }
  function closeFull() {
    const f = S.full;
    if (!f) return;
    if (f.pushed && history.state && history.state.lanloomPathsFull) { history.back(); return; }     // popstate tears down
    teardownFull();
  }
  function teardownFull() {
    const f = S.full;
    if (!f) return;
    S.full = null;
    document.removeEventListener('keydown', f.key, true);
    window.removeEventListener('popstate', f.pop);
    window.removeEventListener('hashchange', f.hash);
    window.removeEventListener('resize', f.resize);
    stopAllDots();
    f.el.remove();
    document.body.classList.remove('pw-noscroll');
    if (S.el && S.el.isConnected) render();
  }
  function fitFull() {
    const f = S.full;
    if (!f) return;
    const area = f.el.querySelector('.pw-full__area'), stage = f.el.querySelector('.pw-full__stage'), m = map();
    const r = area.getBoundingClientRect(), ratio = m.w / m.h;
    const portrait = r.width < r.height && r.width < 700;
    let w = portrait ? Math.min(r.width * 2.6, r.height * ratio) : Math.min(r.width, r.height * ratio);
    w = Math.max(200, Math.floor(w));
    stage.style.width = w + 'px';
    stage.style.height = Math.floor(w / ratio) + 'px';
    area.classList.toggle('is-wide', portrait);
    if (portrait && !f.centred) {                       // start at the home country
      const hp = point(home());
      if (hp) area.scrollLeft = Math.max(0, hp[0] / m.w * w - r.width / 2);
      f.centred = true;
    }
  }
  function renderFull() {
    const f = S.full;
    if (!f) return;
    stopAllDots();
    f.el.querySelector('.pw-full__seg').replaceChildren(seg());
    f.el.querySelector('.pw-full__legend').replaceChildren(legend());
    f.el.querySelector('.pw-full__sel').replaceChildren(...[selChip()].filter(Boolean));
    const stage = f.el.querySelector('.pw-full__stage');
    const built = buildMap(true);
    stage.replaceChildren(built.svg);
    fitFull();
    requestAnimationFrame(() => { if (S.full === f && stage.isConnected) startDots(stage, built.svg, built.arcs); });
  }

  // ------------------------------------------------------------------ view
  async function pathsView(app) {
    const fr = LL.frame(app, 'paths');
    const page = S.el = h('div', { class: 'page page--wide pw-page' });
    fr.main.append(page);
    S.d = null; S.sel = null;
    render();
    await load();
    const onKey = ev => { if (ev.key === 'Escape' && S.sel && !S.full) select(null); };
    document.addEventListener('keydown', onKey);
    clearInterval(S.timer);
    S.timer = setInterval(() => { if (!document.hidden && !S.full) load(); else if (!document.hidden && S.full) load(); }, 60000);
    let lastW = window.innerWidth;
    const onResize = () => { if ((lastW >= 700) !== (window.innerWidth >= 700)) { lastW = window.innerWidth; render(); } };
    window.addEventListener('resize', onResize);
    return { destroy() {
      fr.destroy && fr.destroy();
      clearInterval(S.timer); S.timer = 0;
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onResize);
      teardownFull();
      stopAllDots();
      S.el = null;
    } };
  }
  // ------------------------------------------------------------------ settings card (home country, own networks, networks without internet)
  LL.pathsSettingsCard = function (s) {
    const m = map();
    const codes = [...new Set(Object.keys(m.countries).concat(Object.keys(m.points)))].filter(c => /^[A-Z]{2}$/.test(c))
      .map(c => [c, countryName(c)]).sort((a, b) => a[1].localeCompare(b[1], LL.lang));
    const region = browserRegion();
    const homeSel = h('select', { class: 'select' }, h('option', { value: '' }, t('settings.paths.home.auto', { country: region ? countryName(region) : '\u2013' })),
      codes.map(([c, n]) => h('option', { value: c }, n)));
    homeSel.value = s.home_country || '';
    const own = h('textarea', { class: 'textarea mono', rows: '3', spellcheck: 'false', placeholder: '198.51.100.0/24' });
    own.value = (s.paths_own_networks || []).join('\n');
    const off = h('textarea', { class: 'textarea mono', rows: '3', spellcheck: 'false', placeholder: '192.168.50.48/28' });
    off.value = (s.paths_no_internet || []).join('\n');
    const msg = h('div', { class: 'msg' });
    const lines = v => v.split(/[\s,;]+/).map(x => x.trim()).filter(Boolean);
    const save = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
      LL.msg(msg, '');
      try {
        await api('/api/settings', { method: 'PUT', body: { home_country: homeSel.value, paths_own_networks: lines(own.value), paths_no_internet: lines(off.value) } });
        LL.msg(msg, t('settings.saved'), 'ok');
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('settings.paths.save'));
    return h('section', { class: 'card settings-card' }, h('h2', {}, t('settings.paths.title')),
      h('p', { class: 'muted' }, t('settings.paths.text')),
      LL.field(t('settings.paths.home'), homeSel, t('settings.paths.home.hint')),
      h('div', { class: 'grid-2' }, LL.field(t('settings.paths.own'), own, t('settings.paths.own.hint')), LL.field(t('settings.paths.offline'), off, t('settings.paths.offline.hint'))),
      msg, h('div', { class: 'btn-row' }, save));
  };
  LL.view('paths', pathsView);
})();
