/* Lanloom — data paths: infrastructure routes, the drawing of the "Real cables" view (only lines, never an arc).

   Every data path runs over infrastructure: sea cables (mapped, or known corridors with an estimated course), land lines along long
   routes, radio links to islands and, for remote destinations, a satellite hop via an earth station. The server computes the ways
   (GET /api/paths/infra, lanloom/infraroute.py; data only, no sentences); this file shares the bytes of every country over them,
   bundles equal strands (the line gets thicker with the traffic), draws them and words the tooltips and the legend.
     * measured ways (hops) are drawn full, everything estimated lighter; sea cables a little stronger; never dashed
     * the sum of the strands is the sum of the map (every country's bytes are shared over its ways, nothing is lost)
     * satellite: a thin line from the earth station up to a small satellite and down to the destination
     * anycast: a latency that excludes the database country ends the way where it was measured
   The data is fetched only while the Real cables view is shown, once per `infra_stand` (the field of /api/paths and the country
   sheet that says when the ways changed), and kept in memory.
   Hooks: LL.pathsInfra.on(), .need(stand), .data(), .shares(N, targets, routes, opts), .sheet(N, d, bubbles, opts), .draw(N, shares,
   opts), .scale(svg, pxPerUnit), .mark(), .legendItems(cls), .endText(cc, shares), .wayLine(N, ids), .box(key).
   Event `lanloom-paths-infra` on document when the ways arrived. No inline handler, colours only from tokens. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const NS = 'http://www.w3.org/2000/svg';
  const I = LL.pathsInfra = {};
  const S = { data: null, asked: null, loading: null, lines: new Map(), arts: null, hubAt: null };
  const CB = () => LL.pathsCables || null;
  const svgEl = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs || {}) if (attrs[k] != null) e.setAttribute(k, attrs[k]); return e; };
  const cname = cc => (LL.countryName ? LL.countryName(cc) : cc);
  const bare = id => (id.charAt(0) === '~' ? id.slice(1) : id);
  const key2 = p => Number(p[0]).toFixed(2) + ',' + Number(p[1]).toFixed(2);
  const pathD = pts => (CB() && CB().pathD ? CB().pathD(pts) : pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(2) + ' ' + p[1].toFixed(2)).join(''));
  const ORDER = ['cable', 'sea', 'land', 'measured', 'radio', 'sat'];
  const ART = { cable: 'cable', sea: 'sea', land: 'land', landing: 'land', access: 'land', radio: 'radio' };

  // ------------------------------------------------------------------ data
  I.on = () => !!(CB() && CB().on());
  I.data = () => S.data;
  /* Fetches the ways for this stand unless they are in memory (or were asked for already); the URL carries the stand, so the browser's
     hour of caching never shows an older answer. */
  I.need = function (stand) {
    if (!I.on() || !stand) return Promise.resolve(S.data);
    if (S.data && S.data.stand === stand) return Promise.resolve(S.data);
    if (S.asked === stand) return S.loading || Promise.resolve(S.data);
    S.asked = stand;
    S.loading = api('/api/paths/infra?stand=' + encodeURIComponent(stand)).then(d => {
      if (!d || !d.edges || !d.countries) throw new Error('format');
      S.data = d; S.lines = new Map(); S.hubAt = null;
      document.dispatchEvent(new CustomEvent('lanloom-paths-infra', { detail: { stand: d.stand } }));
      return d;
    }).catch(() => S.data).then(d => { S.loading = null; return d; });
    return S.loading;
  };
  function hubAt(N) {
    if (!S.hubAt || S.hubAt.N !== N) {
      const m = new Map();
      for (const k of Object.keys(N.hubs || {})) m.set(key2(N.hubs[k].p), N.hubs[k].name);
      S.hubAt = { N, m };
    }
    return S.hubAt.m;
  }

  // ------------------------------------------------------------------ lines
  /* the edges of a way in running direction -> polyline (map units); a satellite edge is no line on the ground */
  function wayLine(N, ids) {
    const sk = (ids || []).join(',');
    let pts = S.lines.get(sk);
    if (pts) return pts;
    pts = [];
    for (const id of ids || []) {
      const e = N.edges[bare(id)];
      if (!e || e.kind === 'sat') continue;
      const p = id.charAt(0) === '~' ? e.p.slice().reverse() : e.p;
      for (let i = pts.length ? 1 : 0; i < p.length; i++) pts.push(p[i]);
    }
    if (S.lines.size > 600) S.lines.clear();
    S.lines.set(sk, pts);
    return pts;
  }
  I.wayLine = wayLine;
  /* satellite hop: control point of a curve that peaks at the satellite (a geostationary one higher than one in low orbit) */
  function satTop(gw, to, orbit) {
    const dx = to[0] - gw[0], dy = to[1] - gw[1], d = Math.hypot(dx, dy);
    const lift = orbit === 'leo' ? Math.max(7, Math.min(22, 0.1 * d)) : Math.max(12, Math.min(40, 0.2 * d));
    return [(gw[0] + to[0]) / 2, Math.max(4, Math.min(gw[1], to[1]) - lift)];
  }
  const quad = (a, c, b, n) => { const out = []; for (let i = 0; i <= n; i++) { const u = i / n, v = 1 - u; out.push([v * v * a[0] + 2 * v * u * c[0] + u * u * b[0], v * v * a[1] + 2 * v * u * c[1] + u * u * b[1]]); } return out; };
  /* a radio link as a calm wave (continuous, never dashed) */
  function wave(p) {
    const a = p[0], b = p[p.length - 1], dx = b[0] - a[0], dy = b[1] - a[1], d = Math.hypot(dx, dy) || 1;
    const n = Math.max(3, Math.round(d / 1.4)), nx = -dy / d, ny = dx / d, amp = Math.min(0.5, d / 9);
    let s = 'M' + a[0].toFixed(2) + ' ' + a[1].toFixed(2);
    for (let i = 1; i <= n * 4; i++) { const u = i / (n * 4), w = Math.sin(u * n * Math.PI * 2) * amp; s += 'L' + (a[0] + dx * u + nx * w).toFixed(2) + ' ' + (a[1] + dy * u + ny * w).toFixed(2); }
    return s;
  }

  // ------------------------------------------------------------------ the shares of the countries
  /* targets [{cc, tx, rx}], routes = the stored routes (with `sat` where the destination network is a satellite network).
     opts: {out(cc) -> outside the EU?, sel (country selected), point(cc) -> [x, y]}. A country's bytes are shared equally over its
     measured ways, else the estimated way of the country carries them - the sum stays the sum. */
  I.shares = function (N, targets, routes, opts) {
    const o = opts || {}, out = [];
    for (const x of targets || []) {
      const bytes = x.tx + x.rx, zi = N.countries[x.cc];
      if (!(bytes > 0) || !zi) continue;
      const rs = (routes || []).filter(r => r.cc === x.cc && N.routes[r.key]);
      const base = { cc: x.cc, out: !!(o.out && o.out(x.cc)), quiet: !!(o.sel && o.sel !== x.cc), pick: !!(o.sel && o.sel === x.cc) };
      const list = rs.length ? rs : [null];
      for (const r of list) {
        const f = 1 / list.length, pt = o.point ? o.point(x.cc) : null;
        const s = Object.assign({}, base, { bytes: bytes * f, rx: x.rx * f, tx: x.tx * f });
        if (!r) { Object.assign(s, { ids: zi.path, origin: zi.origin, via: zi.via, cables: zi.cables, gateway_name: zi.gateway_name }); out.push(s); continue; }
        const n = N.routes[r.key];
        Object.assign(s, { ids: n.path, measured: n.measured_edges || 0, origin: 'route', via: n.via, cables: n.cables, key: r.key });
        if (n.end) { s.end = n.end; s.conflict = n.conflict || null; }
        // a way into a satellite-only country ends with the core's satellite edge: the earth station is where the way before it ends
        const viaSat = (n.path || []).some(id => N.edges[bare(id)] && N.edges[bare(id)].kind === 'sat');
        if (n.origin === 'sat') { s.origin = 'sat'; s.sat = { gw: n.gateway, to: n.target || pt, orbit: n.orbit || 'geo', name: n.gateway_name }; }
        else if (viaSat) { s.origin = 'sat'; s.orbit = r.sat || 'geo'; s.satTo = pt; }
        else if (!n.end && pt && r.sat) { s.origin = 'sat'; s.sat = { gw: null, to: pt, orbit: r.sat, name: null, fromEnd: true }; }     // never with an anycast end
        out.push(s);
      }
    }
    return out;
  };

  /* The country sheet: per bubble (a city, or the country's middle) the way up to the first hub of the country (else the first point
     inside), on over the inland net to the hub nearest the bubble and a short feeder to the bubble. d = the sheet's data (routes,
     cities, operators); bubbles [{x, y, p}]; opts {frame (rectangle of the country in map units), out}. */
  I.sheet = function (N, d, bubbles, opts) {
    const o = opts || {}, cc = d.cc, zi = N.countries[cc], out = [];
    if (!zi) return out;
    const il = (N.inland || {})[cc] || null, fr = o.frame || null;
    const inside = p => !!fr && p[0] >= fr.x0 - 0.5 && p[0] <= fr.x1 + 0.5 && p[1] >= fr.y0 - 0.5 && p[1] <= fr.y1 + 0.5;
    const hubKey = new Map(Object.keys(il ? il.hubs : {}).map(k => [key2(il.hubs[k]), k]));
    Object.keys(N.hubs || {}).forEach(k => { const hb = N.hubs[k]; if (hb.cc === cc && !il) hubKey.set(key2(hb.p), null); });
    const cut = ids => {
      let first = -1;
      for (let i = 0; i < ids.length; i++) {
        const e = N.edges[bare(ids[i])];
        if (!e) continue;
        if (e.kind === 'sat') return { ids: ids.slice(0, i), sat: true, hub: null };
        const q = ids[i].charAt(0) === '~' ? e.p[0] : e.p[e.p.length - 1], k = key2(q);
        if (hubKey.has(k)) return { ids: ids.slice(0, i + 1), hub: hubKey.get(k) };
        if (first < 0 && inside(q)) first = i;
      }
      return { ids: first >= 0 ? ids.slice(0, first + 1) : ids, hub: null };
    };
    const nearestHub = p => { let best = null; for (const k of Object.keys(il ? il.hubs : {})) { const q = il.hubs[k], dd = Math.hypot(q[0] - p[0], q[1] - p[1]); if (!best || dd < best[1]) best = [k, dd]; } return best ? best[0] : null; };
    const inland = (a, b) => {
      if (!il || !a || !b || a === b) return [];
      const w = il.paths[a + '|' + b];
      if (w) return w;
      const r = il.paths[b + '|' + a];
      return r ? r.slice().reverse().map(id => (id.charAt(0) === '~' ? id.slice(1) : '~' + id)) : [];
    };
    const routes = (d.routes || []).filter(r => N.routes[r.key] && !N.routes[r.key].end);
    const tot = (d.totals && (d.totals.tx + d.totals.rx)) || 0, rxShare = tot ? d.totals.rx / tot : 1;
    const ops = d.operators || [];
    for (const b of bubbles.slice().sort((a, c) => a.p.bytes - c.p.bytes)) {
      const to = [b.x, b.y];
      const city = b.p.name ? (d.cities || []).find(c => c.name === b.p.name) : null;
      const list = city ? (city.operators || []) : ops.filter(x => !x.other);
      const asns = new Set(list.map(x => x.asn));
      const allSat = list.length > 0 && list.every(x => x.sat);
      const satOp = list.find(x => x.sat);
      const r = (b.p.name && routes.find(x => x.city && x.city === b.p.name)) || routes.find(x => asns.has(x.asn));
      const nr = r ? N.routes[r.key] : null;
      const s = { cc, bytes: b.p.bytes, rx: b.p.bytes * rxShare, tx: b.p.bytes * (1 - rxShare), out: !!o.out, key: r ? r.key : null, pick: false, quiet: false };
      if (allSat) {                                       // a satellite network: from the earth station straight to the bubble
        if (nr) Object.assign(s, { ids: nr.path, measured: nr.measured_edges || 0, origin: 'sat', via: nr.via, cables: nr.cables, sat: { gw: null, to, orbit: satOp.sat, name: null } });
        else if (zi.sat) Object.assign(s, { ids: zi.sat.path, origin: 'sat', via: zi.via, cables: zi.cables, sat: { gw: zi.sat.gateway, to, orbit: satOp.sat, name: zi.sat.gateway_name } });
        else if (zi.origin === 'sat') Object.assign(s, { ids: zi.path, origin: 'sat', via: zi.via, gateway_name: zi.gateway_name, satTo: to, orbit: satOp.sat });
        else Object.assign(s, { ids: zi.path, origin: zi.origin, via: zi.via, cables: zi.cables });
        out.push(s);
        continue;
      }
      const c = cut(nr ? nr.path : zi.path);
      const ids = c.ids.concat(c.sat ? [] : inland(c.hub, nearestHub(to)));
      Object.assign(s, { ids, measured: nr ? Math.min(nr.measured_edges || 0, c.ids.length) : 0, origin: nr ? 'route' : zi.origin, via: nr ? nr.via : zi.via,
        cables: nr ? nr.cables : zi.cables });
      if (c.sat) { s.origin = 'sat'; s.sat = { gw: null, to, orbit: 'geo', name: zi.gateway_name }; } else s.tail = [to];
      out.push(s);
    }
    return out;
  };

  // ------------------------------------------------------------------ drawing
  /* Bundles the shares to strands and draws them. -> {g, streams, arts, edges, sats}. opts: {maxW (px, thickest strand), nodes (hubs on the
     carrying strands)}. A share: {ids, bytes, rx, tx, out, quiet, pick, measured, sat?, tail?, cc, origin, via, cables, end?, conflict?}. */
  I.draw = function (N, shares, opts) {
    const o = opts || {}, edges = new Map(), sats = new Map(), tails = [], arts = new Set(), streams = [], picks = [], satPts = [];
    for (const s of shares) {
      if (!(s.bytes > 0)) continue;
      const ids = s.ids || [];
      let sat = s.sat ? Object.assign({}, s.sat) : null;
      ids.forEach((id, i) => {
        const k = bare(id), e = N.edges[k];
        if (!e) return;
        if (e.kind === 'sat') {                           // a destination that only a satellite reaches: from the earth station up and down
          const p = id.charAt(0) === '~' ? e.p.slice().reverse() : e.p;
          sat = sat || { gw: p[0], to: s.satTo || p[p.length - 1], orbit: s.orbit || 'geo', name: s.gateway_name };
          return;
        }
        let x = edges.get(k);
        if (!x) { x = { b: 0, bo: 0, measured: false, ccs: new Set(), e, quiet: true, hows: new Set() }; edges.set(k, x); }
        x.hows.add(howText(s));
        x.b += s.bytes; if (s.out) x.bo += s.bytes;
        if (i < (s.measured || 0)) x.measured = true;
        if (!s.quiet) x.quiet = false;
        x.ccs.add(s.cc);
        arts.add(ART[e.kind] || 'land');
      });
      if (s.measured) arts.add('measured');
      let pts = wayLine(N, ids).slice();
      if (sat) {
        const gw = sat.gw || (pts.length ? pts[pts.length - 1] : null), to = sat.to;
        const atHome = N.home && gw && Math.hypot(gw[0] - N.home[0], gw[1] - N.home[1]) < 1.5;      // an earth station is never the home itself
        if (gw && to && !atHome) {
          const top = satTop(gw, to, sat.orbit), sk = key2(gw) + '|' + key2(to) + '|' + sat.orbit;
          const m = sats.get(sk) || { gw, to, top, b: 0, bo: 0, orbit: sat.orbit, name: sat.name, ccs: new Set(), quiet: true };
          m.b += s.bytes; if (s.out) m.bo += s.bytes; m.ccs.add(s.cc); if (!s.quiet) m.quiet = false;
          sats.set(sk, m);
          satPts.push(gw, top);
          arts.add('sat');
          if (!pts.length) pts = [gw];
          const c = [2 * top[0] - (gw[0] + to[0]) / 2, 2 * top[1] - (gw[1] + to[1]) / 2];
          pts = pts.concat(quad(gw, c, to, 14).slice(1));
        }
      }
      if (s.tail && s.tail.length) { tails.push({ from: pts.length ? pts[pts.length - 1] : s.tail[0], pts: s.tail, b: s.bytes, out: s.out, quiet: s.quiet, cc: s.cc }); pts = pts.concat(s.tail); arts.add('land'); }
      if (pts.length >= 2 && CB()) streams.push({ cc: s.cc, L: CB().line(pts), rx: s.rx, tx: s.tx, out: !!s.out, quiet: !!s.quiet, via: s.origin === 'route' ? 'route' : 'infra' });
      if (s.pick && pts.length >= 2) picks.push(pts);
    }
    const maxB = Math.max(1, ...[...edges.values()].map(x => x.b), ...[...sats.values()].map(x => x.b), ...tails.map(x => x.b));
    const maxW = o.maxW || 4.2, width = b => (0.7 + (maxW - 0.7) * Math.sqrt(b / maxB)).toFixed(2);
    const names = hubAt(N), g = svgEl('g', { class: 'in-layer' });
    const nodes = new Map();
    const count = x => (x.ccs.size === 1 ? cname([...x.ccs][0]) : t('infra.edge.targets', { n: x.ccs.size }));
    /* one shared sign for all lines (no <title> per line): the text sits in data-tip, a line is reachable by keyboard */
    const tipOf = (txt, parts) => [txt].concat(parts).filter(Boolean).join('\n');
    const tipAttrs = (txt, parts) => { const tx = tipOf(txt, parts); return { 'data-tip': tx, tabindex: '0', role: 'img', 'aria-label': tx.replace(/\n/g, ', ') }; };
    for (const x of [...edges.entries()].sort((a, b) => a[1].b - b[1].b)) {
      const [k, v] = x, e = v.e, p = e.p, a = names.get(key2(p[0])), z = names.get(key2(p[p.length - 1]));
      if (v.b >= maxB * 0.03) [a && [p[0], a], z && [p[p.length - 1], z]].forEach(q => { if (q) nodes.set(q[1], q[0]); });
      let what;
      if (e.kind === 'cable') what = e.name ? t('infra.edge.cable', { name: e.name }) : t('infra.edge.cable_unnamed');
      else if (e.kind === 'land' && a && z) what = t('infra.edge.land_between', { a, b: z });
      else what = t('infra.edge.' + e.kind);
      const hows = [...v.hows].filter(Boolean).slice(0, 3).join(' / ');
      const path = svgEl('path', Object.assign({ class: 'in-k in-k--' + e.kind + (v.measured ? ' is-measured' : '') + (v.quiet ? ' is-quiet' : '') + ' ' + (v.bo > v.b / 2 ? 'pw-out' : 'pw-eu'),
        d: e.kind === 'radio' ? wave(p) : pathD(p), style: '--in-w:' + width(v.b) + 'px', 'data-e': k },
        tipAttrs(what, [[count(v), LL.fmtBytes(v.b)].join(' · '), hows, v.measured ? t('infra.edge.measured') : ''])));
      g.appendChild(path);
    }
    for (const a of tails) {
      const path = svgEl('path', { class: 'in-k in-k--access' + (a.quiet ? ' is-quiet' : '') + ' ' + (a.out ? 'pw-out' : 'pw-eu'), d: pathD([a.from].concat(a.pts)), style: '--in-w:' + width(a.b) + 'px' });
      g.appendChild(path);
    }
    for (const m of sats.values()) {
      const orbit = t('infra.orbit.' + (m.orbit === 'leo' ? 'leo' : 'geo'));
      const tipA = tipAttrs(t('infra.sat.title', { orbit }), [m.name ? t('infra.sat.station', { name: m.name }) : null, count(m) + ' · ' + LL.fmtBytes(m.b)]);
      const c = [2 * m.top[0] - (m.gw[0] + m.to[0]) / 2, 2 * m.top[1] - (m.gw[1] + m.to[1]) / 2];
      const path = svgEl('path', Object.assign({ class: 'in-k in-sat' + (m.quiet ? ' is-quiet' : '') + ' ' + (m.bo > m.b / 2 ? 'pw-out' : 'pw-eu'), style: '--in-w:' + Math.min(1.6, Number(width(m.b))) + 'px',
        d: 'M' + m.gw[0].toFixed(2) + ' ' + m.gw[1].toFixed(2) + 'Q' + c[0].toFixed(2) + ' ' + c[1].toFixed(2) + ' ' + m.to[0].toFixed(2) + ' ' + m.to[1].toFixed(2) }, tipA));
      g.appendChild(path);
      const sym = svgEl('g', { class: 'in-satellite' + (m.quiet ? ' is-quiet' : ''), 'data-sat': '1', 'data-x': m.top[0].toFixed(2), 'data-y': m.top[1].toFixed(2),
        transform: 'translate(' + m.top[0].toFixed(2) + ' ' + m.top[1].toFixed(2) + ') scale(1.2)' });
      sym.innerHTML = '<rect class="in-satellite__body" x="-3" y="-3" width="6" height="6" rx="1.2"/><rect class="in-satellite__panel" x="-11" y="-2" width="7" height="4" rx=".6"/>'
        + '<rect class="in-satellite__panel" x="4" y="-2" width="7" height="4" rx=".6"/><path d="M0 3v3"/>';
      sym.setAttribute('data-tip', tipA['data-tip']);
      g.appendChild(sym);
    }
    if (o.nodes !== false) {
      for (const [name, p] of nodes) {
        const c = svgEl('circle', { class: 'in-node', cx: p[0].toFixed(2), cy: p[1].toFixed(2), r: 1.6, 'data-ir': '2.4' });
        c.setAttribute('data-tip', t('infra.hub', { name }));
        g.appendChild(c);
      }
    }
    for (const pts of picks) g.appendChild(svgEl('path', { class: 'in-pick', d: pathD(pts) }));
    S.arts = arts;
    return { g, streams, arts, edges: edges.size, sats: sats.size, extra: satPts };
  };

  /* keeps the satellites and hub points at their screen size at every zoom step (pxPerUnit = screen pixels of one map unit) */
  I.scale = function (svg, pxPerUnit) {
    if (!svg || !(pxPerUnit > 0)) return;
    const f = 1 / pxPerUnit, r = svg.getBoundingClientRect();
    const k = 1.2 * Math.max(0.7, Math.min(1, (r.width || 1100) / 1100));        // smaller maps (phone) get smaller symbols, never larger ones
    svg.querySelectorAll('.in-satellite[data-sat]').forEach(e => e.setAttribute('transform', 'translate(' + e.dataset.x + ' ' + e.dataset.y + ') scale(' + (f * k).toFixed(4) + ')'));
    svg.querySelectorAll('.in-node[data-ir]').forEach(c => c.setAttribute('r', (parseFloat(c.dataset.ir) * f).toFixed(3)));
  };

  /* the way of the chosen route in the routes list lights up on the maps */
  I.mark = function () {
    const N = S.data, R = LL.pathsRoutes, key = R && R.active ? R.active() : null;
    document.querySelectorAll('.in-layer').forEach(g => {
      g.querySelectorAll('.in-route').forEach(e => e.remove());
      const r = N && key && N.routes[key];
      if (!r) return;
      const pts = wayLine(N, r.path);
      if (pts.length >= 2) g.appendChild(svgEl('path', { class: 'in-route', d: pathD(pts) }));
    });
  };
  I.box = function (key) {
    const N = S.data, r = N && N.routes[key], pts = r ? wayLine(N, r.path) : [];
    if (!pts.length) return null;
    return { x0: Math.min(...pts.map(q => q[0])), x1: Math.max(...pts.map(q => q[0])), y0: Math.min(...pts.map(q => q[1])), y1: Math.max(...pts.map(q => q[1])) };
  };

  // ------------------------------------------------------------------ words: legend and the endpoints
  /* legend of the lines that are drawn right now (only these), neutral line patterns (the colour says EU or outside) */
  I.legendItems = function (cls, arts) {
    const a = arts || S.arts || new Set(['cable', 'land']), k = cls || 'pw-legend__item';
    return ORDER.filter(x => a.has(x)).map(x => h('span', { class: k }, h('i', { class: 'in-sw in-sw--' + x, 'aria-hidden': 'true' }), t('infra.legend.' + x)));
  };
  /* the origin of one share in words: measured by hops, sea cable likely, land path estimated, radio link likely, satellite likely */
  function howText(s) {
    return s.origin === 'sat' ? t('infra.origin.sat', { orbit: t('infra.orbit.' + ((s.sat && s.sat.orbit) === 'leo' ? 'leo' : 'geo')) }) : t('infra.origin.' + (s.origin || 'land'));
  }
  /* the one shared sign of the lines (hover, keyboard focus, tap); same look as the sign of the sea cables */
  let tip = null, away = 0;
  const hitOf = ev => (ev.target && ev.target.closest ? ev.target.closest('.in-layer [data-tip]') : null);
  function hideTip() { if (tip) { tip.remove(); tip = null; } }
  function showTip(el, x, y) {
    const stage = el.closest('.pw-stage, .pc-stage') || el.closest('svg').parentNode;
    if (!stage) return;
    if (!tip || tip.parentNode !== stage) { hideTip(); tip = h('div', { class: 'cb-tip in-tip', role: 'tooltip' }); stage.appendChild(tip); }
    const lines = (el.getAttribute('data-tip') || '').split('\n').filter(Boolean);
    tip.replaceChildren(...lines.map((l, i) => h(i === 0 ? 'b' : 'span', {}, l)));
    const r = stage.getBoundingClientRect();
    tip.style.left = Math.max(4, Math.min(x - r.left + 12, r.width - tip.offsetWidth - 4)) + 'px';
    tip.style.top = Math.max(4, Math.min(y - r.top + 14, r.height - tip.offsetHeight - 4)) + 'px';
  }
  I.tipText = el => (el && el.getAttribute ? el.getAttribute('data-tip') || '' : '');
  document.addEventListener('mouseover', ev => { const p = hitOf(ev); if (p) { clearTimeout(away); showTip(p, ev.clientX, ev.clientY); } });
  document.addEventListener('mousemove', ev => { const p = hitOf(ev); if (p && tip) showTip(p, ev.clientX, ev.clientY); });
  document.addEventListener('mouseout', ev => { if (hitOf(ev)) hideTip(); });
  document.addEventListener('focusin', ev => { const p = hitOf(ev); if (p) { const b = p.getBoundingClientRect(); showTip(p, b.left + b.width / 2, b.top + b.height / 2); } });
  document.addEventListener('focusout', ev => { if (hitOf(ev)) hideTip(); });
  document.addEventListener('click', ev => { const p = hitOf(ev); if (p) { clearTimeout(away); showTip(p, ev.clientX, ev.clientY); away = setTimeout(hideTip, 4000); } else if (tip && !(ev.target.closest && ev.target.closest('.cb-tip'))) hideTip(); }, true);
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && tip) hideTip(); });
  window.addEventListener('scroll', () => { if (tip) hideTip(); }, true);

  /* what a country's endpoint says: where the traffic came from per way, over which places and cables */
  I.endText = function (cc, shares) {
    const mine = (shares || []).filter(s => s.cc === cc), seen = new Set(), lines = [];
    for (const s of mine) {
      const how = howText(s);
      const via = (s.via || []).length ? t('infra.via', { places: s.via.slice(0, 4).join(' → ') }) : '';
      const cab = (s.cables || []).length ? t('infra.cables', { names: s.cables.slice(0, 2).join(', ') }) : '';
      const line = [how, via, cab].filter(Boolean).join(' · ');
      if (!seen.has(line)) { seen.add(line); lines.push(line); }
      if (s.conflict && s.conflict.place) { const m = t('infra.measured_at', { place: s.conflict.place }); if (!seen.has(m)) { seen.add(m); lines.push(m); } }
    }
    return lines.slice(0, 4).join('\n');
  };
})();
