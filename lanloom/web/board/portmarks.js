/* Lanloom — port marks: at the ends of a trace it says which port it is plugged into.
   Lanloom.Portmarks.create(host, options) → instance; load portmarks.css as well. No dependencies, no assumptions about the host app.
   Own layer above the board (like endmarks.js), screen units, fixed small font — traces, dots and tiles stay untouched:
   - Network view (cabling): at the device a small RJ45 socket with the port number of the switch or router the cable is plugged
     into ("Port 4"); for uplinks the port number at the parent device and, if known, the own port (e.g. "SFP+ 9"). Radio instead
     of cable: small radio symbol with band or SSID, if known.
   - Logical views: at the service end the target port or protocol (":8123", ":1883 MQTT"), if the host knows it. Traces that
     arrive at the same end (bundle trace) carry it only once at the trunk end; the feeders only if they have different ports.
   Unknown values are left out, never guessed. Detail levels: from a minimum scale calmly at every end where there is room (never
   over tile texts, other tiles, end marks, other traces or controls); below that only while hovering the trace or the device — then
   in full ("Switch living room · Port 4"). On touch devices only on tap (selected tile). No rebuild: content once per second,
   placement throttled while panning/zooming.

   ---------------------------------------------------------------------------------------------------- host interface, API = 1
   Same conventions as Lanloom.Endmarks (board units, view() maps to screen px):
     host.el, host.ready(), host.view(), host.size(), host.measure(), host.animUntil(), host.tiles(), host.labels(), host.kindFilter(),
     host.chain(), host.highlighted(), host.obstacles(rect), host.color(kind), host.t(key, params)   — as in endmarks.js
     host.network            bool: this board shows the cabling (network marks) instead of logical services
     host.traces()           → [trace] with {id, a, b, kind, pts, padA, padB, edgeEnd, bundles: [{key}] (bundle trunks this trace
                               belongs to; key starts with "<tile id>|")}
     host.traceColor(trace)  → (optional) actual stroke colour of a drawn trace; default host.color(trace.kind)
     host.selectedId()       → id of the selected tile, or null
     host.endmarkRects()     → [{x0, y0, x1, y1}] labels of the end marks (kept clear)
     host.textRects()        → {free: [rect], tile: Map(nid → [rect])} texts in board units (zone titles, tile texts)
     host.isTile(target)     → bool: the pointer target belongs to a tile (the tile wins over the trace below it)
     host.facts()            → [fact] — what is known about the ends; neutral fields only, unknown fields left out:
       {trace, end: 'a'|'b', link: 'uplink', parentName, deviceName, uplinkPort, uplinkMedium, ownPort, ownMedium, radio: bool, data}
       {trace, end, link: 'wired', parentName, port, portMedium, ownPort, data}          (device on a switch/router port)
       {trace, end, link: 'radio', parentName, radio: {band, ssid}, data}                 (band: '2.4ghz'|'5ghz'|'6ghz'|'11n'|'11ac'|'11ax'|'11be')
       {trace, end, link: 'service', targetNode, targetName, servicePorts: [port…], listedPorts: [port…], seen: bool,
        protocols: {port: name} (optional, overrides the built-in names), data}
       data is opaque and returned unchanged in state()/candidates().
   The host calls: content() after data changes, reposition(immediate) on pan/zoom, focus() on hover/selection/chain changes,
   themeChanged() after a theme switch, destroy() when the board goes away.
   options: {classAlias: {role: 'extra-class'}} */
(function (root) {
  'use strict';
  const LL = root.Lanloom = root.Lanloom || {};
  const API = 1;
  const TICK = 1000, THROTTLE = 90;
  const GAP = 2;               // gap to obstacles (px)
  const THRESHOLD = { network: 0.86, logical: 0.7 };   // calmly visible from this scale on (network: from the tiles' full font)
  const BAND_DEPTH = 10;       // this far (board units) a socket may reach into its own tile — there is never text there
  const SVG_NS = 'http://www.w3.org/2000/svg';

  /* strings:begin */
  const STRINGS = {
    "portmarks.port": "Port",
    "portmarks.parent": "parent device",
    "portmarks.uplink": "Uplink",
    "portmarks.radioUplink": "Radio uplink to {parent}",
    "portmarks.wlan": "Wireless",
    "portmarks.seen": "seen",
    "portmarks.listed": "listed {ports}",
    "portmarks.connection": "Connection",
    "portmarks.band.2.4ghz": "2.4 GHz",
    "portmarks.band.5ghz": "5 GHz",
    "portmarks.band.6ghz": "6 GHz",
    "portmarks.band.11n": "802.11n",
    "portmarks.band.11ac": "802.11ac",
    "portmarks.band.11ax": "802.11ax",
    "portmarks.band.11be": "802.11be"
  };
  /* strings:end */
  const fill = (s, v) => (v ? String(s).replace(/\{(\w+)\}/g, (m, k) => (v[k] != null ? v[k] : m)) : String(s));
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const tDefault = (k, v) => fill(STRINGS[k] || k, v);

  // ---------------------------------------------------------------- symbols (12 × 12, stroke in trace colour)
  const SYMBOL = {
    // RJ45 socket from the front: opening with latch at the bottom, three contacts at the top
    port: '<path d="M1.5 2h9v6.2H8.2V10H3.8V8.2H1.5z"/><path d="M4 2v1.6M6 2v1.6M8 2v1.6"/>',
    // SFP cage: flat cage with tab
    sfp: '<path d="M1 3.5h10v5H1z"/><path d="M3.5 6h5"/>',
    radio: '<path d="M6 9.6h.01"/><path d="M1.4 4.6a6.6 6.6 0 0 1 9.2 0"/><path d="M3.1 6.4a4.2 4.2 0 0 1 5.8 0"/><path d="M4.7 8a1.9 1.9 0 0 1 2.6 0"/>',
    // service port: plug
    service: '<path d="M4.2 1.5v2.6M7.8 1.5v2.6"/><path d="M2.6 4.1h6.8v1.7a3.4 3.4 0 0 1-6.8 0z"/><path d="M6 9.2v1.6"/>'
  };
  const symbolSvg = kind => `<svg class="ll-pm-ic" viewBox="0 0 12 12" width="11" height="11" aria-hidden="true">${SYMBOL[kind] || SYMBOL.port}</svg>`;

  // ---------------------------------------------------------------- measuring text (font of the layer)
  let ctxMeasure = null, fontKey = '';
  const measured = new Map();
  let measureHook = null;   // tests: (text, font) → width
  function width(t, fs, family) {
    const s = `600 ${fs}px ${family || 'system-ui'}`;
    if (s !== fontKey) { measured.clear(); fontKey = s; }
    let w = measured.get(t);
    if (w == null) {
      if (measureHook) w = Math.ceil(measureHook(t, s)) + 2;
      else {
        if (!ctxMeasure) ctxMeasure = document.createElement('canvas').getContext('2d');
        ctxMeasure.font = s; w = Math.ceil(ctxMeasure.measureText(t).width) + 2;
      }
      if (measured.size > 1500) measured.clear();
      measured.set(t, w);
    }
    return w;
  }
  function shorten(t, max, fs, family) {
    t = String(t || '');
    if (width(t, fs, family) <= max) return t;
    let n = t;
    while (n.length > 2 && width(n + '…', fs, family) > max) n = n.slice(0, -1);
    return n.trimEnd() + '…';
  }

  // ---------------------------------------------------------------- geometry
  const intersects = (a, b) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
  const rect = (x, y, w, h) => ({ x0: x, y0: y, x1: x + w, y1: y + h });
  const grow = (r, d) => ({ x0: r.x0 - d, y0: r.y0 - d, x1: r.x1 + d, y1: r.y1 + d });
  // segment ∩ rectangle (Liang–Barsky)
  function segmentHits(x1, y1, x2, y2, r) {
    let t0 = 0, t1 = 1;
    const dx = x2 - x1, dy = y2 - y1;
    const p = [-dx, dx, -dy, dy], q = [x1 - r.x0, r.x1 - x1, y1 - r.y0, r.y1 - y1];
    for (let i = 0; i < 4; i++) {
      if (Math.abs(p[i]) < 1e-9) { if (q[i] < 0) return false; continue; }
      const t = q[i] / p[i];
      if (p[i] < 0) { if (t > t1) return false; if (t > t0) t0 = t; } else { if (t < t0) return false; if (t < t1) t1 = t; }
    }
    return t0 <= t1;
  }
  // grid of the trace segments (screen px) for quick hit tests
  class SegmentGrid {
    constructor(Z) { this.Z = Z; this.m = new Map(); }
    add(x1, y1, x2, y2, bid) {
      const Z = this.Z, s = [x1, y1, x2, y2, bid];
      const cx0 = Math.floor(Math.min(x1, x2) / Z), cx1 = Math.floor(Math.max(x1, x2) / Z), cy0 = Math.floor(Math.min(y1, y2) / Z), cy1 = Math.floor(Math.max(y1, y2) / Z);
      if ((cx1 - cx0 + 1) * (cy1 - cy0 + 1) > 400) return;   // far outside (zoomed in a lot): only what is visible counts
      for (let cx = cx0; cx <= cx1; cx++) for (let cy = cy0; cy <= cy1; cy++) { const k = cx + ',' + cy; let l = this.m.get(k); if (!l) this.m.set(k, l = []); l.push(s); }
    }
    // does a foreign segment hit the rectangle? own(bid) → true = ignore
    hits(r, own) {
      const Z = this.Z, seen = new Set();
      for (let cx = Math.floor(r.x0 / Z); cx <= Math.floor(r.x1 / Z); cx++) for (let cy = Math.floor(r.y0 / Z); cy <= Math.floor(r.y1 / Z); cy++) {
        const l = this.m.get(cx + ',' + cy); if (!l) continue;
        for (const s of l) {
          if (seen.has(s)) continue; seen.add(s);
          if (own && own(s[4])) continue;
          if (segmentHits(s[0], s[1], s[2], s[3], r)) return true;
        }
      }
      return false;
    }
  }
  // point at distance d along P (P[0] = end) → {x, y, dx, dy} (direction of the segment, inwards)
  function along(P, d) {
    let rest = d;
    for (let i = 1; i < P.length; i++) {
      const [x1, y1] = P[i - 1], [x2, y2] = P[i], L = Math.hypot(x2 - x1, y2 - y1);
      if (L < 1e-6) continue;
      if (rest <= L) return { x: x1 + (x2 - x1) * rest / L, y: y1 + (y2 - y1) * rest / L, dx: (x2 - x1) / L, dy: (y2 - y1) / L, i };
      rest -= L;
    }
    return null;
  }
  const length = P => { let s = 0; for (let i = 1; i < P.length; i++) s += Math.hypot(P[i][0] - P[i - 1][0], P[i][1] - P[i - 1][1]); return s; };

  // ---------------------------------------------------------------- texts from neutral facts
  const BANDS = ['2.4ghz', '5ghz', '6ghz', '11n', '11ac', '11ax', '11be'];
  const bandText = (b, t) => (BANDS.includes(String(b || '')) ? (t || tDefault)('portmarks.band.' + b) : '');
  const MEDIUM = m => { const s = String(m || '').toUpperCase(); return /SFP\+|SFP28|SFP/.test(s) ? s.match(/SFP28|SFP\+|SFP/)[0] : ''; };
  // "Port 4" or "SFP+ 9" (medium only if it is not an ordinary copper port)
  const portText = (n, medium, t) => n ? `${MEDIUM(medium) || (t || tDefault)('portmarks.port')} ${n}` : '';
  const PROTOCOL = { 1883: 'MQTT', 8883: 'MQTTS', 22: 'SSH', 445: 'SMB', 139: 'SMB', 2049: 'NFS', 53: 'DNS', 3493: 'NUT', 5900: 'VNC', 873: 'rsync',
    2055: 'NetFlow', 5353: 'mDNS', 25: 'SMTP', 587: 'SMTP', 993: 'IMAPS', 143: 'IMAP', 32400: 'Plex', 8086: 'InfluxDB', 51820: 'WireGuard', 41641: 'Tailscale' };  // identifier: well-known protocol names
  const portLabel = (p, names) => { const n = (names && names[p]) || PROTOCOL[p]; return `:${p}${n ? ' ' + n : ''}`; };

  // fact → mark {b, ende, kind, short, long, title, pair, group, data} (null = nothing known)
  function markOf(f, t) {
    const b = f.trace, prefix = t('portmarks.port') + ' ';
    if (f.link === 'uplink') {
      const pn = f.parentName;
      if (f.radio) return { b, end: f.end, kind: 'radio', short: '', long: t('portmarks.radioUplink', { parent: pn || t('portmarks.parent') }), group: 'n:' + b.id + ':' + f.end };
      // mark at the child end (own vertical run per uplink — at the parent device the uplinks lie close together): like at the devices
      // it names the port the cable is plugged into above, plus the own port, if known ("3 ⇄ SFP+ 9")
      const pE = portText(f.uplinkPort, f.uplinkMedium, t), pK = portText(f.ownPort, f.ownMedium, t);
      if (!pE && !pK) return null;
      const nE = pE.startsWith(prefix) ? pE.slice(prefix.length) : pE, short = pE && pK ? `${nE} ⇄ ${pK}` : pE ? nE : pK;
      return { b, end: f.end, kind: MEDIUM(f.uplinkMedium || f.ownMedium) ? 'sfp' : 'port', short,
        long: `${pn || t('portmarks.parent')}${pE ? ' · ' + pE : ''} ⇄ ${f.deviceName || t('portmarks.uplink')}${pK ? ' · ' + pK : ''}`, pair: pE && pK ? `${pE} ⇄ ${pK}` : '',
        group: 'n:' + b.id + ':' + f.end, data: f.data };
    }
    if (f.link === 'wired') {
      if (!f.port) return null;                                  // port unknown: guess nothing
      const pG = f.ownPort ? String(f.ownPort) : '';              // port at the device (eth0, LAN …), only if known
      const pn = f.parentName;
      return { b, end: f.end, kind: MEDIUM(f.portMedium) ? 'sfp' : 'port', short: String(f.port), long: `${pn ? pn + ' · ' : ''}${portText(f.port, f.portMedium, t)}${pG ? ' ⇄ ' + pG : ''}`,
        pair: pG ? `${portText(f.port, f.portMedium, t)} ⇄ ${pG}` : '', group: 'n:' + b.id + ':' + f.end, data: f.data };
    }
    if (f.link === 'radio') {
      const r = f.radio || {}, band = bandText(r.band, t), ssid = String(r.ssid || '');   // band (2.4/5/6 GHz) before the standard (802.11ax)
      return { b, end: f.end, kind: 'radio', short: band || ssid, long: [t('portmarks.wlan'), band, ssid, f.parentName].filter(Boolean).join(' · '), group: 'n:' + b.id + ':' + f.end, data: f.data };
    }
    if (f.link === 'service') {
      const ports = f.servicePorts || [], listed = f.listedPorts || [];
      if (!ports.length) return null;
      const short = ports.map(p => portLabel(p, f.protocols)).join(' · ');
      const seen = f.seen ? ' · ' + t('portmarks.seen') : '';
      const more = listed.length > ports.length ? ' · ' + t('portmarks.listed', { ports: listed.slice(0, 6).map(p => ':' + p).join(' ') }) : '';
      return { b, end: f.end, kind: 'service', short, long: `${f.targetName} ${short}${seen}`, title: `${f.targetName} ${short}${seen}${more}`,
        group: 'd:' + f.targetNode + ':' + short, data: f.data };
    }
    return null;
  }
  // detail level: calm (always) or only on hover, font size and height at a scale
  function detail(s, network, touch) {
    const fs = Math.max(10, Math.min(11.5, Math.round(10 * Math.max(1, s) * 2) / 2));
    return { calm: !touch && s >= (network ? THRESHOLD.network : THRESHOLD.logical), fs, h: fs + 5 };
  }

  // ---------------------------------------------------------------- one layer per board
  class Portmarks {
    constructor(host, options) {
      this.host = host; this.o = Object.assign({}, options || {}); this.dead = false; this.specs = []; this.marks = new Map(); this.list = [];
      this.alias = this.o.classAlias || {};
      this.t = (k, v) => { const r = typeof host.t === 'function' ? host.t(k, v) : null; return r == null || r === k ? tDefault(k, v) : r; };
      this._lz = 0; this._lt = 0; this.hoverTrace = null;
      const el = this.el = document.createElement('div'); el.className = this.cls('layer'); el.setAttribute('aria-hidden', 'false');
      host.el.appendChild(el);
      this.network = !!host.network;
      // hovering a trace (only traces with a mark): nearest trace within 6 px
      this._move = ev => this.pointer(ev);
      this._out = () => { if (this.hoverTrace) { this.hoverTrace = null; this.reposition(); } };
      host.el.addEventListener('pointermove', this._move, { passive: true });
      host.el.addEventListener('pointerleave', this._out);
      this.colors = new Map();
      this._tick = setInterval(() => this.content(), TICK);
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { measured.clear(); if (!this.dead) this.place(); });
      this.content();
    }
    cls(role) { return 'll-pm-' + role + (this.alias[role] ? ' ' + this.alias[role] : ''); }
    toggle(el, role, on) { el.classList.toggle('ll-pm-' + role, on); if (this.alias[role]) el.classList.toggle(this.alias[role], on); }
    themeChanged() { this.colors = new Map(); this.content(); }

    // ------------------------------------------------------------ content (1 s): which ends carry which mark
    content() {
      if (this.dead || !this.host.ready() || document.hidden) return;
      this.colors = new Map();   // trace colour fresh every second (the colour may depend on the zone, trace ids can change after an update)
      const raw = (this.host.facts() || []).map(f => markOf(f, this.t)).filter(Boolean);
      // bundle traces: same group (target tile + label) and the same end (position) → only one mark at the trunk end; feeders with
      // different ports keep their own
      const endKey = x => { const p = x.end === 'a' ? x.b.pts[0] : x.b.pts[x.b.pts.length - 1]; return Math.round(p[0] / 6) + ',' + Math.round(p[1] / 6); };
      const byGroup = new Map(), list = [];
      for (const x of raw) {
        if (x.kind === 'service') {
          const zk = x.end === 'b' ? x.b.b : x.b.a;
          const trunk = (x.b.bundles || []).find(s => s.key && s.key.startsWith(zk + '|'));
          const k = x.group + '|' + (trunk ? 'S:' + trunk.key : endKey(x));
          const have = byGroup.get(k);
          if (have) { have.traces.push(x.b); continue; }
          x.traces = [x.b]; byGroup.set(k, x); list.push(x);
          continue;
        }
        x.traces = [x.b]; list.push(x);
      }
      // service ports: the same target tile with different ports → the feeders say it themselves (no bundling across ports);
      // the same port at several traces of the same tile and side → one mark
      const perTarget = new Map();
      list.filter(x => x.kind === 'service').forEach(x => { const zk = (x.end === 'b' ? x.b.b : x.b.a) + '|' + this.side(x); const l = perTarget.get(zk) || []; l.push(x); perTarget.set(zk, l); });
      const dupes = new Set();
      perTarget.forEach(l => {
        const perLabel = new Map();
        l.forEach(x => { const v = perLabel.get(x.short); if (v) { v.traces.push(...x.traces); dupes.add(x); } else perLabel.set(x.short, x); });
      });
      this.list = list.filter(x => !dupes.has(x));
      this.place();
    }
    side(x) { const p = x.end === 'a' ? x.b.padA : x.b.padB; return (p && p.s) || ''; }

    // ------------------------------------------------------------ hover
    focusNode() {
      const host = this.host, c = host.chain();
      if (c && c.node) return c.node;
      if (host.highlighted()) return host.highlighted();
      if (host.selectedId && host.selectedId()) return host.selectedId();
      return null;
    }
    focus() { if (!this.dead) this.reposition(true); }
    pointer(ev) {
      if (ev.pointerType && ev.pointerType !== 'mouse' && ev.pointerType !== 'pen') return;
      if (!this.list.length || this._zt) return;
      this._zt = requestAnimationFrame(() => {
        this._zt = 0; if (this.dead) return;
        const er = this.host.el.getBoundingClientRect(), x = ev.clientX - er.left, y = ev.clientY - er.top, L = this.host.view();
        let best = null, bd = 6;
        for (const m of this.list) for (const b of m.traces) {
          const P = b.pts;
          for (let i = 1; i < P.length; i++) {
            const x1 = P[i - 1][0] * L.s + L.x, y1 = P[i - 1][1] * L.s + L.y, x2 = P[i][0] * L.s + L.x, y2 = P[i][1] * L.s + L.y;
            const dx = x2 - x1, dy = y2 - y1, l2 = dx * dx + dy * dy;
            const t = l2 ? Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / l2)) : 0;
            const d = Math.hypot(x - (x1 + dx * t), y - (y1 + dy * t));
            if (d < bd) { bd = d; best = b.id; }
          }
        }
        // over a tile the tile counts (chain view), not the trace below it
        if (best && ev.target && this.host.isTile && this.host.isTile(ev.target)) best = null;
        if (best !== this.hoverTrace) { this.hoverTrace = best; this.reposition(true); }
      });
    }

    // ------------------------------------------------------------ placement (throttled)
    reposition(now) {
      if (this.dead) return;
      if (now) { clearTimeout(this._lt); this._lt = 0; this._lz = performance.now(); this.place(); return; }
      if (this._lt) return;
      const wait = Math.max(0, THROTTLE - (performance.now() - this._lz));
      this._lt = setTimeout(() => {
        this._lt = 0; this._lz = performance.now(); this.place();
        if (this.host.animUntil() && performance.now() < this.host.animUntil()) this.reposition();
      }, wait);
    }
    // fixed controls above the stage (as for the end marks)
    uiRects(er) { return this.host.obstacles ? this.host.obstacles(er) : []; }
    color(b) {
      let f = this.colors.get(b.id);
      if (f) return f;
      f = (this.host.traceColor && this.host.traceColor(b)) || this.host.color(b.kind);
      this.colors.set(b.id, f);
      return f;
    }
    // texts of the board (zone/group titles, tile texts) in board units — measured once per second
    measureTexts(L, er) {
      const now = performance.now();
      if (this._texts && now - this._textsT < 1000 && this._textsS === L.s) return this._texts;
      this._texts = this.host.textRects(L, er); this._textsT = now; this._textsS = L.s;
      return this._texts;
    }

    place() {
      if (this.dead || !this.host.ready()) return;
      const host = this.host;
      let S = host.size();
      if (!S.W || !S.H) { host.measure(); S = host.size(); }
      const W = S.W, H = S.H; if (!W || !H) return;
      const L = host.view(), er = host.el.getBoundingClientRect();
      const fk = this.focusNode(), hb = this.hoverTrace;
      const touch = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(pointer: coarse)').matches && !window.matchMedia('(any-pointer: fine)').matches;
      const D = detail(L.s, this.network, touch), calm = D.calm, fs = D.fs, H0 = D.h;
      const family = getComputedStyle(this.el).fontFamily || 'system-ui';
      this.el.style.setProperty('--ll-pm-fs', fs + 'px'); this.el.style.setProperty('--ll-pm-h', H0 + 'px');
      const tx = x => x * L.s + L.x, ty = y => y * L.s + L.y;
      const V = { x0: 2, y0: 2, x1: W - 2, y1: H - 2 };
      const inImage = r => r.x0 >= V.x0 && r.y0 >= V.y0 && r.x1 <= V.x1 && r.y1 <= V.y1;
      // which marks at all: calm → all; otherwise only those of the hovered trace or tile
      const on = x => !!((hb && x.traces.some(b => b.id === hb)) || (fk && x.traces.some(b => b.a === fk || b.b === fk)));
      const show = this.list.filter(x => calm || on(x));
      const specs = [];
      if (show.length) {
        const T = this.measureTexts(L, er);
        const hard = this.uiRects(er);
        const tiles = [];
        Object.entries(host.tiles() || {}).forEach(([id, k]) => tiles.push({ id, r: { x0: tx(k.x), y0: ty(k.y), x1: tx(k.x + k.w), y1: ty(k.y + k.h) } }));
        const texts = T.free.map(q => ({ x0: tx(q.x0), y0: ty(q.y0), x1: tx(q.x1), y1: ty(q.y1) }));
        (host.labels() || []).forEach(q => texts.push({ x0: tx(q[0]), y0: ty(q[1]), x1: tx(q[0] + q[2]), y1: ty(q[1] + q[3]) }));
        const tileTexts = new Map();
        T.tile.forEach((l, id) => tileTexts.set(id, l.map(q => ({ x0: tx(q.x0), y0: ty(q.y0), x1: tx(q.x1), y1: ty(q.y1) }))));
        const em = host.endmarkRects ? host.endmarkRects() : [];
        const grid = new SegmentGrid(48);
        const filter = host.kindFilter();
        for (const b of host.traces()) {
          if (!b.pts || (filter && !filter.has(b.kind))) continue;
          for (let i = 1; i < b.pts.length; i++) {
            const x1 = tx(b.pts[i - 1][0]), y1 = ty(b.pts[i - 1][1]), x2 = tx(b.pts[i][0]), y2 = ty(b.pts[i][1]);
            if (Math.max(x1, x2) < -50 || Math.min(x1, x2) > W + 50 || Math.max(y1, y2) < -50 || Math.min(y1, y2) > H + 50) continue;
            grid.add(x1, y1, x2, y2, b.id);
          }
        }
        const taken = [], placed = new Set();   // placed: tile|kind|label — the same fact stands only once per tile (not per trace and side)
        // order: highlighted first (they get the best spot), then uplinks before devices, then top to bottom
        const queue = show.map(x => ({ x, on: on(x) })).sort((p, q) => (q.on - p.on) || ((p.x.end === 'a') - (q.x.end === 'a')) * -1);
        for (const { x, on: bright } of queue) {
          const P = (x.end === 'a' ? x.b.pts : x.b.pts.slice().reverse()).map(q => [tx(q[0]), ty(q[1])]);
          const E = P[0];
          if (E[0] < -40 || E[0] > W + 40 || E[1] < -40 || E[1] > H + 40) continue;
          const node = x.end === 'a' ? x.b.a : x.b.b;
          const once = x.short ? node + '|' + x.kind + '|' + x.short : '';
          if (once && placed.has(once)) continue;
          const side = this.side(x) || (() => { const d = along(P, 0.5); if (!d) return 't'; return Math.abs(d.dx) > Math.abs(d.dy) ? (d.dx > 0 ? 'l' : 'r') : (d.dy > 0 ? 't' : 'b'); })();
          const bids = new Set(x.traces.map(b => b.id));
          const own = id => bids.has(id);
          const text = bright ? (x.long || x.short) : x.short;
          const variants = [...new Set([bright ? shorten(text, 260, fs, family) : text, ...(bright && x.short ? [x.short] : [])])].filter(v => v != null);
          const band = BAND_DEPTH * L.s;
          // check of a rectangle: never over foreign tiles, tile texts, texts, end marks, port marks, controls;
          // foreign traces and free labels (group titles) only at rest — while hovering they step back anyway and the mark lies
          // opaque above; the own tile only in the band at the edge (there is never text there)
          const check = (r, withTraces) => {
            if (!inImage(r)) return false;
            const rp = grow(r, GAP);
            for (const o of hard) if (intersects(rp, o)) return false;
            for (const o of taken) if (intersects(rp, o)) return false;
            for (const o of em) if (intersects(rp, o)) return false;
            for (const k of tiles) {
              if (!intersects(r, k.r)) continue;
              if (k.id !== node) return false;
              const q = k.r, deep = { t: r.y1 - q.y0, b: q.y1 - r.y0, l: r.x1 - q.x0, r: q.x1 - r.x0 }[side];
              if (deep == null || deep > band + 0.5) return false;
              for (const tr of tileTexts.get(k.id) || []) if (intersects(r, tr)) return false;   // text box already contains the ascender
            }
            if (withTraces) for (const tr of texts) if (intersects(r, tr)) return false;
            if (withTraces && grid.hits(grow(r, 1), own)) return false;
            return true;
          };
          let pl = null;
          for (const tv of variants) {
            const w = Math.min(290, width(tv, fs, family) + (tv ? (bright ? 28 : 23) : 19)), h = H0;   // symbol 11 + gap + padding + border
            const cand = [];
            // 1. socket at the tile edge: the end of the trace is plugged into the mark, part of it reaches into the own tile's band
            const ins = [Math.min(band, h - 4), Math.min(band, h * 0.6), Math.min(band, h * 0.4), 0].filter((v, i, l) => v >= 0 && l.indexOf(v) === i);
            for (const d of ins) {
              if (side === 't') cand.push([E[0] - w / 2, E[1] - h + d, 'edge']);
              else if (side === 'b') cand.push([E[0] - w / 2, E[1] - d, 'edge']);
              else if (side === 'l') cand.push([E[0] - w + d, E[1] - h / 2, 'edge']);
              else if (side === 'r') cand.push([E[0] - d, E[1] - h / 2, 'edge']);
            }
            // 2. on the trace, away from the end (the trace runs on under the mark)
            const Lg = Math.min(length(P), 56);   // never far from the end — otherwise it is unclear which end the mark belongs to
            for (let dd = 2; dd <= Lg; dd += 3) {
              const q = along(P, dd); if (!q) break;
              cand.push([q.x - w / 2, q.y - h / 2, 'trace']);
            }
            // 3. beside the trace close to the end
            for (let dd = 2; dd <= Math.min(Lg, 40); dd += 4) {
              const q = along(P, dd); if (!q) break;
              if (Math.abs(q.dy) > Math.abs(q.dx)) { cand.push([q.x + 3, q.y - h / 2, 'beside']); cand.push([q.x - 3 - w, q.y - h / 2, 'beside']); }
              else { cand.push([q.x - w / 2, q.y - h - 3, 'beside']); cand.push([q.x - w / 2, q.y + 3, 'beside']); }
            }
            for (const [kx, ky, kind] of cand) {
              const r = rect(Math.round(kx), Math.round(ky), w, h);
              if (check(r, !bright)) { pl = { r, text: tv, kind }; break; }
            }
            if (pl) break;
          }
          if (!pl) continue;
          taken.push(pl.r); if (once) placed.add(once);
          specs.push({ key: x.group + '|' + x.traces.map(b => b.id).join(','), x, r: pl.r, text: pl.text, on: bright, spot: pl.kind, color: this.color(x.b), anchor: E });
        }
      }
      this.specs = specs; this.calm = calm;
      this.draw();
    }

    draw() {
      const used = new Set();
      for (const sp of this.specs) {
        used.add(sp.key);
        let m = this.marks.get(sp.key);
        if (!m) {
          m = document.createElement('div'); m.className = this.cls('mark');
          m.innerHTML = `<span class="${this.cls('sym')}"></span><span class="${this.cls('txt')}"></span>`;
          this.el.appendChild(m); this.marks.set(sp.key, m);
        }
        const x = sp.x;
        if (m._kind !== x.kind) { m.firstChild.innerHTML = symbolSvg(x.kind); m._kind = x.kind; }
        if (m.lastChild.textContent !== sp.text) m.lastChild.textContent = sp.text;
        this.toggle(m, 'on', sp.on);
        this.toggle(m, 'empty', !sp.text);
        m.dataset.kind = x.kind; m.dataset.end = x.end; m.dataset.trace = x.b.id;
        m.style.transform = `translate(${sp.r.x0}px,${sp.r.y0}px)`;
        m.style.width = (sp.r.x1 - sp.r.x0) + 'px';
        if (m.style.color !== sp.color) m.style.color = sp.color;
        const title = x.title || x.long || x.short;
        if (m.getAttribute('aria-label') !== title) { m.setAttribute('aria-label', title); m.title = title; }
        m._spec = sp;
      }
      this.marks.forEach((m, k) => { if (!used.has(k)) { m.remove(); this.marks.delete(k); } });
      // chain view: foreign marks step back
      const fk = this.focusNode(), f = this.host.chain();
      this.marks.forEach(m => {
        const sp = m._spec; let rel = -1;
        if (f) rel = Math.max(...sp.x.traces.map(b => f.group ? f.group(b) : 2));
        else if (fk) rel = sp.x.traces.some(b => b.a === fk || b.b === fk) ? 2 : 0;
        this.toggle(m, 'off', rel === 0 && !sp.on);
      });
    }

    destroy() {
      if (this.dead) return;
      this.dead = true; clearInterval(this._tick); clearTimeout(this._lt); if (this._zt) cancelAnimationFrame(this._zt);
      this.host.el.removeEventListener('pointermove', this._move); this.host.el.removeEventListener('pointerleave', this._out);
      this.el.remove(); this.marks.clear();
    }
    // state for tests/measurements
    state() {
      return this.specs.map(sp => ({ key: sp.key, kind: sp.x.kind, end: sp.x.end, trace: sp.x.b.id, traces: sp.x.traces.map(b => b.id), a: sp.x.b.a, b: sp.x.b.b,
        short: sp.x.short, long: sp.x.long, text: sp.text, on: sp.on, spot: sp.spot, x0: sp.r.x0, y0: sp.r.y0, x1: sp.r.x1, y1: sp.r.y1, anchor: sp.anchor, data: sp.x.data || null }));
    }
    candidates() {   // all known marks (also without room) — tests
      return this.list.map(x => ({ kind: x.kind, end: x.end, trace: x.b.id, traces: x.traces.map(b => b.id), a: x.b.a, b: x.b.b, short: x.short, long: x.long, data: x.data || null }));
    }
  }

  /* Chip for a details panel: the same mark as a small chip ("Port 4", "SFP+ 10 ⇄ eth0", radio with band/SSID).
     fact as for host.facts() with link 'wired' or 'radio'; '' when nothing is known. */
  function chipHtml(f, t, alias) {
    t = t || tDefault;
    if (!f) return '';
    let kind, text;
    if (f.link === 'wired') {
      if (!f.port) return '';
      kind = MEDIUM(f.portMedium) ? 'sfp' : 'port';
      text = portText(f.port, f.portMedium, t) + (f.ownPort ? ' ⇄ ' + f.ownPort : '');
    } else {
      kind = 'radio';
      const r = f.radio || {};
      text = [bandText(r.band, t), r.ssid].filter(Boolean).join(' · ');
      if (!text) return '';
    }
    return `<span class="ll-pm-chip${alias ? ' ' + alias : ''}" data-kind="${kind}">${symbolSvg(kind)}<span>${esc(text)}</span></span>`;
  }

  const api = {
    API, STRINGS, THRESHOLD, PROTOCOL,
    create: (host, options) => new Portmarks(host, options),
    Portmarks, chipHtml, symbolSvg,
    _internals: { markOf: (f, t) => markOf(f, t || tDefault), detail, portLabel, portText, bandText, MEDIUM, setMeasure: f => { measureHook = f; measured.clear(); } }
  };
  LL.Portmarks = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof self !== 'undefined' ? self : this);
