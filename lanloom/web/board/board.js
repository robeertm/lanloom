/* Lanloom — circuit-board map engine.
   Lanloom.Board.mount(el, {view, theme, compact, onSelect, t, locale}) → {update, live, setTheme, highlight, destroy}
   Load order: router.js, layout.js, icons.js, dots.js, endmarks.js, portmarks.js, board.js (plus css/tokens.css, css/board.css,
   board/endmarks.css and board/portmarks.css). End and port marks are optional: without their scripts the board works as before.
   Zones and tiles are DOM (crisp text, focusable buttons), traces are SVG under the tiles, glow and dots are two
   viewport-sized canvases above. Traces are routed in a Web Worker (window fallback) and only when the structure
   (ids/positions) changes — never on live frames. */
(function (root) {
  'use strict';
  const LL = root.Lanloom = root.Lanloom || {};
  const Router = LL.Router, Layout = LL.Layout, Icons = LL.Icons, Dots = LL.Dots, Endmarks = LL.Endmarks, Portmarks = LL.Portmarks;
  const SCRIPT = (document.currentScript && document.currentScript.src) || '';
  const BASE = SCRIPT ? SCRIPT.replace(/[^/]*$/, '') : '';
  const NS = 'http://www.w3.org/2000/svg';
  const GRADES = ['realtime', 'near', 'periodic', 'sluggish', 'interrupted', 'unknown'];
  const TRACE_KINDS = ['uplink', 'lan', 'service', 'flow', 'integration'];
  const STATUSES = ['up', 'down', 'unknown'];
  const ELL = '…';
  const SAFETY = 1.5;   // px reserve between canvas measurement and DOM rendering
  const EDGE = '__edge', OUTSIDE = '__outside';   // node at the board edge (exits), peer "somewhere outside"

  // Built-in English strings (same as i18n/board.en.json) — used when no translator is passed
  /* strings:begin */
  const STRINGS = {
    "board.region": "Network map",
    "board.empty": "Nothing to show yet. Run a scan to fill the map.",
    "board.legend.label": "Map legend",
    "board.legend.dot": "1 dot ≈ {size}",
    "board.legend.dotLarge": "larger dots 8× and 64×",
    "board.legend.unmeasured": "Not measured",
    "board.legend.waiting": "Waiting for live data",
    "board.legend.live": "Live",
    "board.grade.realtime": "Real-time",
    "board.grade.near": "Near real-time",
    "board.grade.periodic": "Periodic",
    "board.grade.sluggish": "Sluggish",
    "board.grade.interrupted": "Interrupted",
    "board.grade.unknown": "Unknown",
    "board.status.up": "Online",
    "board.status.down": "Offline",
    "board.status.unknown": "Status unknown",
    "board.trace.uplink": "Uplink",
    "board.trace.lan": "LAN",
    "board.trace.service": "Service",
    "board.trace.flow": "Flow",
    "board.trace.integration": "Integration",
    "board.kind.internet": "Internet",
    "board.kind.router": "Router",
    "board.kind.switch": "Switch",
    "board.kind.ap": "Access point",
    "board.kind.nas": "Network storage",
    "board.kind.server": "Server",
    "board.kind.computer": "Computer",
    "board.kind.phone": "Phone",
    "board.kind.printer": "Printer",
    "board.kind.tv": "TV",
    "board.kind.media": "Media device",
    "board.kind.camera": "Camera",
    "board.kind.hub": "Automation hub",
    "board.kind.iot": "Smart device",
    "board.kind.service": "Service",
    "board.kind.broker": "Message broker",
    "board.kind.self": "This host",
    "board.kind.unknown": "Unknown device",
    "board.tile.aria": "{title}, {kind}, {status}, live data: {grade}",
    "board.zone.other": "Other",
    "board.zone.count": "{count} components"
  };
  /* strings:end */
  function defaultT(key, vars) { return fill(STRINGS[key] || key, vars); }
  function fill(s, vars) { return vars ? String(s).replace(/\{(\w+)\}/g, (m, k) => (vars[k] != null ? vars[k] : m)) : String(s); }

  // ------------------------------------------------------------------ binary byte units ("64 KiB")
  function formatBytes(n, locale) {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let v = Math.max(0, +n || 0), u = 0;
    while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
    let nf;
    try { nf = new Intl.NumberFormat(locale || undefined, { maximumFractionDigits: v < 10 && u ? 1 : 0 }); } catch (e) { nf = { format: x => String(Math.round(x * 10) / 10) }; }
    return nf.format(v) + ' ' + units[u];
  }

  // ------------------------------------------------------------------ text measuring (canvas, with the real loaded font)
  const measure = { ctx: null };
  function fontInfo(el) {
    const cs = getComputedStyle(el);
    return { font: `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`, ls: parseFloat(cs.letterSpacing) || 0, upper: cs.textTransform === 'uppercase' };
  }
  function textWidth(f, s) {
    if (!measure.ctx) measure.ctx = document.createElement('canvas').getContext('2d');
    const ctx = measure.ctx;
    if (ctx.font !== f.font) ctx.font = f.font;
    const t = f.upper ? s.toUpperCase() : s;
    return ctx.measureText(t).width + f.ls * t.length;
  }
  // Longest prefix of s (+ ellipsis) that fits into max px
  function fitText(f, s, max) {
    s = String(s == null ? '' : s);
    if (!s) return '';
    if (max <= 4) return '';
    if (textWidth(f, s) <= max) return s;
    let lo = 0, hi = s.length;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (textWidth(f, s.slice(0, m).trimEnd() + ELL) <= max) lo = m; else hi = m - 1; }
    if (!lo) return textWidth(f, ELL) <= max ? ELL : '';
    return s.slice(0, lo).replace(/[\s·,;:\-–—/]+$/, '') + ELL;
  }

  // ------------------------------------------------------------------ shared routing worker
  let worker = null, jobNo = 0;
  const waiting = new Map();
  function getWorker() {
    if (worker !== null) return worker;
    try {
      if (!BASE || typeof Worker === 'undefined' || /^file:/.test(BASE)) throw new Error('no worker');
      worker = new Worker(BASE + 'router.js');
      worker.onmessage = ev => { const m = ev.data || {}; const cb = waiting.get(m.job); if (cb) cb(m); };
      worker.onerror = ev => {
        // worker failed (e.g. blocked by policy): fall back to routing in the window for everything pending
        if (ev && ev.preventDefault) ev.preventDefault();
        worker = false;
        const cbs = [...waiting.values()]; waiting.clear();
        cbs.forEach(cb => cb({ type: 'error', text: 'worker failed', fatal: true }));
      };
    } catch (e) { worker = false; }
    return worker;
  }
  function job(msg, cb) {
    const id = ++jobNo; msg.job = id;
    waiting.set(id, m => { if (m.type === 'done' || m.type === 'trace' || m.type === 'error') waiting.delete(id); cb(m); });
    getWorker().postMessage(msg);
    return id;
  }

  // in-memory route cache (per page) + localStorage (per board slot)
  const memCache = new Map();
  function cacheGet(slot, key) {
    if (memCache.has(key)) return memCache.get(key);
    try { const x = JSON.parse(localStorage.getItem('lanloom.routes.' + slot) || 'null'); if (x && x.k === key && Array.isArray(x.r)) return x.r; } catch (e) { /* storage unavailable */ }
    return null;
  }
  function cachePut(slot, key, routes) {
    memCache.set(key, routes);
    if (memCache.size > 8) memCache.delete(memCache.keys().next().value);
    try { localStorage.setItem('lanloom.routes.' + slot, JSON.stringify({ k: key, r: routes })); } catch (e) { /* full or blocked — fine */ }
  }

  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = v => +(+v).toFixed(2);

  // ================================================================== Board
  class Board {
    constructor(el, o) {
      this.host = el; this.o = Object.assign({}, o || {});
      this.t = typeof this.o.t === 'function' ? (k, v) => { const r = this.o.t(k, v); return r == null || r === k ? defaultT(k, v) : r; } : defaultT;
      this.locale = this.o.locale || (document.documentElement && document.documentElement.lang) || undefined;
      this.tiles = new Map();      // id → {el, parts, data, widths}
      this.zones = new Map();      // id → {el, name, count}
      this.routes = new Map();     // trace id → route
      this.traceInfo = new Map();  // trace id → view trace
      this.key = null; this.layout = null; this.view = null; this.frameQ = null;
      this.highlighted = null; this.gradeOf = new Map();
      this.peers = {}; this.seenPorts = {}; this.exits = []; this.marks = null; this.routed = false;
      this.ready = new Promise(res => { this._resolveReady = res; });
      this.stats = { routeMs: null, routed: 0, failed: 0, fallback: 0, fromCache: false, layouts: 0, worker: null };
      this.build();
      this.setTheme(this.o.theme || 'light');
      this.update(this.o.view || { zones: [], components: [], traces: [] });
    }

    // ---------------------------------------------------------------- DOM skeleton
    build() {
      const r = this.root = document.createElement('div');
      r.className = 'll-board';
      r.setAttribute('role', 'region');
      r.setAttribute('aria-label', this.t('board.region'));
      r.innerHTML = `<div class="ll-scroll"><div class="ll-stage"><div class="ll-live" aria-hidden="true"><canvas class="ll-layer ll-glow"></canvas><canvas class="ll-layer ll-dots"></canvas></div><div class="ll-zones" aria-hidden="true"></div><svg class="ll-traces" xmlns="${NS}" aria-hidden="true"><g class="ll-tr"></g><g class="ll-pads"></g></svg><div class="ll-ztitles"></div><div class="ll-tiles"></div></div></div>
        <div class="ll-legend" role="note"></div><div class="ll-empty" hidden></div>`;
      this.host.appendChild(r);
      this.scroll = r.querySelector('.ll-scroll'); this.stage = r.querySelector('.ll-stage');
      this.svg = r.querySelector('.ll-traces'); this.gTr = r.querySelector('.ll-tr'); this.gPads = r.querySelector('.ll-pads');
      this.zonesEl = r.querySelector('.ll-zones'); this.ztitlesEl = r.querySelector('.ll-ztitles'); this.tilesEl = r.querySelector('.ll-tiles');
      this.liveEl = r.querySelector('.ll-live'); this.legendEl = r.querySelector('.ll-legend'); this.emptyEl = r.querySelector('.ll-empty');
      this.legendEl.setAttribute('aria-label', this.t('board.legend.label'));
      this.dots = new Dots(r.querySelector('.ll-glow'), r.querySelector('.ll-dots'), { compact: false });
      // events
      this._onClick = ev => {
        const b = ev.target.closest('.ll-tile'); if (!b || !this.tilesEl.contains(b)) return;
        if (typeof this.o.onSelect === 'function') this.o.onSelect(b.dataset.id);
      };
      this.tilesEl.addEventListener('click', this._onClick);
      this._onScroll = () => { this.dots.setScroll(this.scroll.scrollTop); this.marksReposition(); };
      this.scroll.addEventListener('scroll', this._onScroll, { passive: true });
      this._lastW = 0;
      this._ro = new ResizeObserver(() => {
        this.sizeLive();
        this.marksReposition();
        const w = this.scroll.clientWidth;
        if (Math.abs(w - this._lastW) < 1) return;
        clearTimeout(this._roT);
        this._roT = setTimeout(() => { if (!this.dead && Math.abs(this.scroll.clientWidth - this._lastW) >= 1) this.update(this.view); }, 160);
      });
      this._ro.observe(this.root);
      this.sizeLive();
      // fonts: text is fitted again once the web font is really there (measured, never estimated)
      this._onFonts = () => { if (!this.dead) this.refit(); };
      if (document.fonts) {
        if (document.fonts.ready) document.fonts.ready.then(this._onFonts);
        if (document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', this._onFonts);
      }
    }

    // The live layers stick to the top of the scroll area (between traces and tiles), sized to the visible part
    sizeLive() {
      const w = this.scroll.clientWidth, h = this.scroll.clientHeight;
      // the board fills at least the visible area (substrate everywhere, no seam below short boards)
      if (this.layout) this.stage.style.height = Math.max(this.layout.h, this.layout.compact ? 0 : h) + 'px';
      this.liveEl.style.height = Math.max(1, Math.min(h, this.layout ? Math.max(this.layout.h, this.layout.compact ? 0 : h) : h)) + 'px';
      this.dots.resize(w, Math.max(1, h));
    }

    // ---------------------------------------------------------------- theme
    setTheme(t) {
      this.theme = t === 'dark' || t === 'auto' ? t : 'light';
      this.root.dataset.theme = this.theme;
      this.readColors();
    }
    readColors() {
      const cs = getComputedStyle(this.root);
      const v = n => cs.getPropertyValue(n).trim();
      const kinds = {}; TRACE_KINDS.forEach(k => { kinds[k] = v('--ll-pcb-trace-' + k) || '#7f8c86'; });
      const dark = /dark/.test(cs.colorScheme || '') || this.theme === 'dark';
      this.dots.setColors({ kinds, core: v('--ll-pcb-dot-core') || '#fff', dark });
      this.colors = kinds;
      if (this.marks) { if (this.marks.em) this.marks.em.place(); if (this.marks.pm) this.marks.pm.themeChanged(); }
    }

    // ---------------------------------------------------------------- structure
    update(view) {
      if (this.dead) return;
      this.view = normaliseView(view);
      const width = this.scroll.clientWidth || this.root.clientWidth || 1200;
      this._lastW = width;
      const compact = this.o.compact == null ? width <= Layout.COMPACT_MAX : !!this.o.compact;
      const lay = this.layout = Layout.compute(this.view, { width, compact });
      this.computeExits();
      this.stats.layouts++;
      this.root.classList.toggle('is-compact', compact);
      if (this.dots.o.compact !== compact) { this.dots.o.compact = compact; this.dots.cap = compact ? 1200 : 5000; this.dots.perTrace = compact ? 16 : 40; this.dots.speed = compact ? 150 : 210; this.dots.sprites.clear(); }
      this.emptyEl.hidden = !!this.view.components.length;
      this.emptyEl.textContent = this.t('board.empty');
      this.stage.style.width = lay.w + 'px';
      this.svg.setAttribute('width', lay.w); this.svg.setAttribute('height', lay.h);
      this.svg.setAttribute('viewBox', `0 0 ${lay.w} ${lay.h}`);
      this.sizeLive();
      this.renderZones(lay);
      this.renderTiles(lay);
      this.legendEl.classList.toggle('is-inline', compact);
      if (compact && this.legendEl.parentNode !== this.scroll) this.scroll.appendChild(this.legendEl);
      if (!compact && this.legendEl.parentNode !== this.root) this.root.insertBefore(this.legendEl, this.emptyEl);
      this.renderLegend();
      this.fitAll();
      // traces
      this.traceInfo = new Map(this.view.traces.map(t => [String(t.id), t]));
      const titleWidth = {}; this.zones.forEach((z, id) => { titleWidth[id] = z.titleW || 160; });
      const spec = Layout.routerSpec(lay, this.view, { Router, titleWidth });
      if (this.o.router) { if (this.o.router.weight) spec.weight = this.o.router.weight; if (this.o.router.cost) spec.cost = this.o.router.cost; }   // tuning hook
      const key = Layout.structureKey(spec);
      if (key !== this.key) {
        this.key = key;
        this.route(spec, compact);
      } else this.renderTraces();   // same structure: keep routes, refresh classes (measured, kinds)
      if (this.highlighted) this.highlight(this.highlighted);
    }

    renderZones(lay) {
      const keep = new Set();
      lay.zones.forEach(z => {
        keep.add(z.id);
        let e = this.zones.get(z.id);
        if (!e) {
          const el = document.createElement('div');
          el.className = 'll-zone';
          this.zonesEl.appendChild(el);
          // the silkscreen title lies above the traces (knocked out of the frame line), the frame below them
          const title = document.createElement('div');
          title.className = 'll-zone__title';
          title.innerHTML = '<span class="ll-zone__name"></span><span class="ll-zone__count"></span>';
          this.ztitlesEl.appendChild(title);
          e = { el, title, name: title.querySelector('.ll-zone__name'), count: title.querySelector('.ll-zone__count') };
          this.zones.set(z.id, e);
        }
        e.el.style.cssText = `left:${z.x}px;top:${z.y}px;width:${z.w}px;height:${z.h}px`;
        e.title.style.cssText = `left:${z.x + 14}px;top:${z.y - 10}px;max-width:${Math.max(0, z.w - 28)}px`;
        e.full = z.synthetic ? this.t('board.zone.other') : (z.title || z.id);
        e.count.textContent = String(z.count);
        e.count.setAttribute('aria-label', this.t('board.zone.count', { count: z.count }));
        e.name.setAttribute('title', e.full);
        e.z = z;
      });
      this.zones.forEach((e, id) => { if (!keep.has(id)) { e.el.remove(); e.title.remove(); this.zones.delete(id); } });
    }

    renderTiles(lay) {
      const keep = new Set();
      const counts = {}; GRADES.forEach(g => { counts[g] = 0; });
      this.view.components.forEach(c => {
        const pos = lay.tiles[c.id]; if (!pos) return;
        keep.add(c.id);
        let e = this.tiles.get(c.id);
        if (!e) {
          const el = document.createElement('button');
          el.type = 'button';
          el.className = 'll-tile';
          el.innerHTML = '<span class="ll-tile__icon"></span><span class="ll-tile__body"><span class="ll-tile__title"></span><span class="ll-tile__sub"></span>' +
            '<span class="ll-tile__meta"><i class="ll-grade" aria-hidden="true"></i><span class="ll-tile__metric"></span><span class="ll-tile__badges"></span></span></span><i class="ll-led" aria-hidden="true"></i>';
          this.tilesEl.appendChild(el);
          e = { el, icon: el.querySelector('.ll-tile__icon'), title: el.querySelector('.ll-tile__title'), sub: el.querySelector('.ll-tile__sub'),
            metric: el.querySelector('.ll-tile__metric'), badgesEl: el.querySelector('.ll-tile__badges'), badges: [], kind: null };
          this.tiles.set(c.id, e);
        }
        const kind = Icons.PATHS[c.kind] ? c.kind : 'unknown';
        if (e.kind !== kind) { e.icon.innerHTML = Icons.svg(kind, 18); e.kind = kind; }
        e.el.dataset.id = c.id; e.el.dataset.kind = kind;
        e.el.dataset.status = STATUSES.includes(c.status) ? c.status : 'unknown';
        const grade = GRADES.includes(c.grade) ? c.grade : 'unknown';
        e.el.dataset.grade = grade; counts[grade]++;
        this.gradeOf.set(c.id, grade);
        e.el.style.cssText = `left:${pos.x}px;top:${pos.y}px;width:${pos.w}px;height:${pos.h}px`;
        e.data = c;
        // badges: create as needed (≤ 3)
        const badges = (c.badges || []).slice(0, 3).map(String);
        while (e.badges.length < badges.length) { const b = document.createElement('span'); b.className = 'll-badge'; e.badgesEl.appendChild(b); e.badges.push(b); }
        while (e.badges.length > badges.length) e.badges.pop().remove();
        e.badgeText = badges;
        e.title.setAttribute('title', c.title || '');
        this.ariaLabel(e);
      });
      this.tiles.forEach((e, id) => { if (!keep.has(id)) { e.el.remove(); this.tiles.delete(id); this.gradeOf.delete(id); } });
      this.gradeCounts = counts;
    }

    ariaLabel(e) {
      const c = e.data;
      const grade = this.gradeOf.get(c.id) || 'unknown';
      let s = this.t('board.tile.aria', {
        title: c.title || c.id,
        kind: this.t('board.kind.' + (e.kind || 'unknown')),
        status: this.t('board.status.' + (STATUSES.includes(c.status) ? c.status : 'unknown')),
        grade: this.t('board.grade.' + grade)
      });
      if (c.subtitle) s += ', ' + c.subtitle;
      if (e.metricFull) s += ', ' + e.metricFull;
      else if (c.metric) s += ', ' + c.metric;
      e.el.setAttribute('aria-label', s);
    }

    // ---------------------------------------------------------------- text fitting
    // All tiles share one size per mode, so the available widths are read once from a sample tile; texts are then
    // cut with canvas measurements of the real font, and a single DOM read afterwards verifies (and shaves) the rest.
    fonts() {
      const first = this.tiles.values().next().value;
      const zone = this.zones.values().next().value;
      const f = {};
      if (first) {
        f.title = fontInfo(first.title); f.sub = fontInfo(first.sub); f.metric = fontInfo(first.metric);
        const b = first.badges[0] || null;
        if (b) f.badge = fontInfo(b);
        else { const tmp = document.createElement('span'); tmp.className = 'll-badge'; first.badgesEl.appendChild(tmp); f.badge = fontInfo(tmp); tmp.remove(); }
        // widths (identical for all tiles of this layout)
        const meta = first.metric.parentNode;
        f.wTitle = first.title.clientWidth; f.wSub = first.sub.clientWidth; f.wMeta = meta.clientWidth;
        const gs = getComputedStyle(meta); f.gap = parseFloat(gs.columnGap || gs.gap) || 6;
        const bs = first.badges[0] ? getComputedStyle(first.badges[0]) : null;
        f.badgePad = bs ? (parseFloat(bs.paddingLeft) + parseFloat(bs.paddingRight) + parseFloat(bs.borderLeftWidth) + parseFloat(bs.borderRightWidth)) : 12;
        f.badgeGap = parseFloat(getComputedStyle(first.badgesEl).columnGap) || 4;
        f.grade = first.el.querySelector('.ll-grade').offsetWidth || 7;
      }
      if (zone) { f.zone = fontInfo(zone.name); }
      return f;
    }
    fitAll() {
      if (!this.tiles.size && !this.zones.size) return;
      const f = this._f = this.fonts();
      this.zones.forEach(z => { z.countW = z.count.offsetWidth || 24; });   // reads before the first write
      // 1) write
      this.tiles.forEach(e => this.fitTile(e, f));
      this.zones.forEach(z => this.fitZone(z, f));
      // 2) verify in the DOM (one batch read), shave what still overflows
      for (let pass = 0; pass < 3; pass++) {
        const bad = [];
        this.forEachText((el, full, f0) => { if (el.scrollWidth > el.clientWidth + 0.5) bad.push([el, full, f0, el.clientWidth - (el.scrollWidth - el.clientWidth) - 2]); });
        if (!bad.length) break;
        bad.forEach(([el, full, f0, max]) => { el.textContent = fitText(f0, full, Math.max(0, max)); });
      }
    }
    forEachText(fn) {
      const f = this._f;
      this.tiles.forEach(e => {
        fn(e.title, e.data.title || '', f.title);
        fn(e.sub, e.data.subtitle || '', f.sub);
        fn(e.metric, e.metricFull || '', f.metric);
        e.badges.forEach((b, i) => { if (!b.hidden) fn(b, e.badgeText[i] || '', f.badge); });
      });
      this.zones.forEach(z => fn(z.name, z.full || '', f.zone));
    }
    fitTile(e, f) {
      const c = e.data;
      e.title.textContent = fitText(f.title, c.title || '', f.wTitle - SAFETY);
      e.sub.textContent = fitText(f.sub, c.subtitle || '', f.wSub - SAFETY);
      e.metricFull = c.metric == null ? '' : String(c.metric);
      this.fitMeta(e, f);
    }
    fitMeta(e, f) {
      // badges first (each ≤ 64 px), then the metric gets the rest; badges are dropped from the end if the metric would get too narrow
      const BADGE_MAX = 64, MIN_METRIC = e.metricFull ? 52 : 0;
      const texts = e.badgeText.map(s => fitText(f.badge, s, BADGE_MAX - f.badgePad - SAFETY));
      const widths = texts.map(s => (s ? Math.ceil(textWidth(f.badge, s)) + f.badgePad + 1 : 0));
      let k = texts.length;
      const room = () => f.wMeta - f.grade - f.gap - (k ? widths.slice(0, k).reduce((a, w) => a + w, 0) + (k - 1) * f.badgeGap + f.gap : 0);
      while (k > 0 && (room() < MIN_METRIC || widths.slice(0, k).some(w => !w))) k--;
      e.badges.forEach((b, i) => { b.hidden = i >= k; b.textContent = i < k ? texts[i] : ''; if (i < k) b.title = e.badgeText[i]; });
      e.metricMax = Math.max(0, room() - SAFETY);
      e.metric.textContent = fitText(f.metric, e.metricFull, e.metricMax);
    }
    fitZone(z, f) {
      // title box = zone width - 28 (margins) - 16 (padding) - count chip - gap
      const countW = z.countW || 24;
      const max = z.z.w - 28 - 16 - countW - 8;
      z.name.textContent = fitText(f.zone, z.full, max - SAFETY);
      z.titleW = Math.min(max, textWidth(f.zone, z.name.textContent)) + countW + 24;
    }
    refit() { if (this.layout) this.fitAll(); }

    // ---------------------------------------------------------------- routing
    route(spec, compact) {
      const slot = (this.view.view || 'map') + (compact ? '.c' : '');
      const ck = Router.cacheKey(spec);
      const token = this._routeToken = {};
      const t0 = performance.now();
      const finish = (routes, failedIds, fromCache, ms) => {
        if (token !== this._routeToken || this.dead) return;
        // every visible trace gets a route: what the router could not place is drawn with a simple fallback geometry
        const have = new Set(routes.map(r => r.id));
        let fb = 0;
        spec.nets.forEach(n => {
          if (have.has(n.id)) return;
          routes.push(Router.fallbackRoute(n, this.layout.tiles[n.a], this.layout.tiles[n.b])); fb++;
        });
        this.applyRoutes(routes);
        this.routed = true;
        this.ensureMarks();
        this.stats.routeMs = ms == null ? Math.round(performance.now() - t0) : ms;
        this.stats.routed = routes.length; this.stats.failed = (failedIds || []).length; this.stats.fallback = fb; this.stats.fromCache = !!fromCache;
        if (!fromCache && !compact) cachePut(slot, ck, routes.filter(r => !r.fallback));
        this._resolveReady(this);
        this.root.dispatchEvent(new CustomEvent('ll-ready', { detail: Object.assign({}, this.stats) }));
      };
      if (compact) { finish(Router.railRoutes(spec), [], false); return; }   // phones: simple rail geometry, no search
      const cached = cacheGet(slot, ck);
      if (cached) { finish(cached.slice(), [], true, 0); return; }
      this.routes = new Map(); this.renderTraces();
      const w = getWorker();
      if (w) {
        this.stats.worker = true;
        const partial = [];
        job({ type: 'route', uid: slot, spec }, m => {
          if (token !== this._routeToken || this.dead) return;
          if (m.type === 'part') { m.traces.forEach(r => partial.push(r)); this.applyRoutes(partial.slice(), true); }
          else if (m.type === 'done') finish(m.all, (m.failed || []).map(x => x.id), false, m.ms);
          else if (m.type === 'error') { if (m.fatal) this.routeInWindow(spec, token, finish); else finish(partial.slice(), [], false); }
        });
      } else this.routeInWindow(spec, token, finish);
    }
    routeInWindow(spec, token, finish) {
      // fallback without worker: route in small chunks so the page stays responsive
      this.stats.worker = false;
      const t0 = performance.now();
      const grid = Router.buildGrid(spec);
      const out = [], failed = [];
      let i = 0;
      const step = () => {
        if (token !== this._routeToken || this.dead) return;
        const until = performance.now() + 12;
        while (i < spec.nets.length && performance.now() < until) {
          const n = spec.nets[i++];
          const r = grid.route(n);
          if (r) out.push(r); else failed.push(n.id);
        }
        if (i < spec.nets.length) { this.applyRoutes(out.slice(), true); setTimeout(step, 0); }
        else finish(out, failed, false, Math.round(performance.now() - t0));
      };
      step();
    }
    applyRoutes(list, partial) {
      this.routes = new Map(list.map(r => [r.id, r]));
      this.renderTraces();
      if (!partial || list.length) {
        this.dots.setGeometry(list.map(r => {
          const t = this.traceInfo.get(r.id) || {};
          return { id: r.id, pts: r.pts, kind: t.kind || 'lan', measured: !!t.measured };
        }));
      }
    }
    renderTraces() {
      const paths = [], pads = [], vias = new Set(), viaEls = [];
      const compact = this.layout && this.layout.compact;
      const pw = compact ? 5 : 6, ph = compact ? 3 : 3.4;
      this.routes.forEach(r => {
        const t = this.traceInfo.get(r.id); if (!t) return;
        const kind = TRACE_KINDS.includes(t.kind) ? t.kind : 'lan';
        const d = 'M' + r.pts.map(q => num(q[0]) + ',' + num(q[1])).join('L');
        paths.push(`<path class="ll-trace ll-k-${kind}${t.measured ? '' : ' is-unmeasured'}" data-t="${esc(r.id)}" data-a="${esc(r.a)}" data-b="${esc(r.b)}" d="${d}"/>`);
        [r.padA, r.padB].forEach(p => {
          if (!p) return;
          const across = p.s === 't' || p.s === 'b';
          const w = across ? pw : ph, h = across ? ph : pw;
          pads.push(`<rect class="ll-pad" data-t="${esc(r.id)}" x="${num(p.x - w / 2)}" y="${num(p.y - h / 2)}" width="${w}" height="${h}" rx="1"/>`);
        });
        (r.vias || []).forEach(v => {
          const k = Math.round(v[0] / 10) + "," + Math.round(v[1] / 10); if (vias.has(k)) return; vias.add(k);
          viaEls.push(`<circle class="ll-via" data-t="${esc(r.id)}" cx="${num(v[0])}" cy="${num(v[1])}" r="1.8"/>`);
        });
      });
      // exits: straight leads from a tile to the board edge (peers beyond the LAN); the end mark hangs at their outer end
      this.exits.forEach(x => {
        const d = 'M' + x.pts.map(q => num(q[0]) + ',' + num(q[1])).join('L');
        if (x.exit) paths.push(`<path class="ll-trace ll-k-${x.kind} ll-exit" data-t="${esc(x.id)}" data-a="${esc(x.a)}" d="${d}"/>`);
        const p = x.padA, across = p.s === 't' || p.s === 'b', w = across ? pw : ph, h = across ? ph : pw;
        pads.push(`<rect class="ll-pad" data-t="${esc(x.id)}" x="${num(p.x - w / 2)}" y="${num(p.y - h / 2)}" width="${w}" height="${h}" rx="1"/>`);
      });
      this.gTr.innerHTML = paths.join('');
      this.gPads.innerHTML = pads.join('') + viaEls.join('');
      if (this.highlighted) this.applyHighlight();
      if (this.marks) { if (this.marks.em) this.marks.em.content(); if (this.marks.pm) this.marks.pm.content(); }
    }

    // ---------------------------------------------------------------- live
    live(frame) {
      if (!frame || this.dead) return;
      if (frame.q && frame.q !== this.frameQ) { this.frameQ = frame.q; this.renderLegend(); }
      let gradeChanged = false;
      if (frame.grades) for (const id in frame.grades) {
        const e = this.tiles.get(id); if (!e) continue;
        const g = GRADES.includes(frame.grades[id]) ? frame.grades[id] : 'unknown';
        if (this.gradeOf.get(id) === g) continue;
        this.gradeCounts[this.gradeOf.get(id) || 'unknown']--; this.gradeCounts[g]++;
        this.gradeOf.set(id, g); e.el.dataset.grade = g; gradeChanged = true;
        this.ariaLabel(e);
      }
      if (frame.metrics && this._f) for (const id in frame.metrics) {
        const e = this.tiles.get(id); if (!e) continue;
        const m = frame.metrics[id] == null ? '' : String(frame.metrics[id]);
        if (m === e.metricFull) continue;
        e.metricFull = m;
        // no layout reads: the metric's room was measured when the tile was fitted
        e.metric.textContent = fitText(this._f.metric, m, e.metricMax);
        this.ariaLabel(e);
      }
      if (gradeChanged) this.updateGradeLegend();
      // peers beyond the LAN (end marks at their tile) and target ports seen in flows (port marks)
      if (frame.peers !== undefined || frame.ports !== undefined || Object.keys(this.peers).length || Object.keys(this.seenPorts).length) {
        const before = Object.keys(this.peers).filter(k => (this.peers[k] || []).length).sort().join(',');
        this.peers = frame.peers && typeof frame.peers === 'object' ? frame.peers : {};
        this.seenPorts = frame.ports && typeof frame.ports === 'object' ? frame.ports : {};
        if (Object.keys(this.peers).filter(k => (this.peers[k] || []).length).sort().join(',') !== before) { this.computeExits(); this.renderTraces(); }
      }
      if (frame.overall && frame.overall.grade !== this.overall) { this.overall = frame.overall.grade; this.updateGradeLegend(); }
      this.dots.frame(frame);
    }

    // ---------------------------------------------------------------- legend
    renderLegend() {
      const t = this.t, q = this.frameQ;
      const kinds = new Set(this.view ? this.view.traces.filter(x => this.layout && this.layout.tiles[x.a] && this.layout.tiles[x.b]).map(x => x.kind) : []);
      const dotSvg = '<svg width="40" height="12" viewBox="0 0 40 12" aria-hidden="true"><path d="M1 6H39" class="ll-lg-trace"/><circle cx="9" cy="6" r="2.4" class="ll-lg-dot"/><circle cx="9" cy="6" r="1" class="ll-lg-core"/><circle cx="21" cy="6" r="2.4" class="ll-lg-dot"/><circle cx="21" cy="6" r="1" class="ll-lg-core"/><circle cx="33" cy="6" r="3.4" class="ll-lg-dot"/><circle cx="33" cy="6" r="1.4" class="ll-lg-core"/></svg>';
      const dashSvg = '<svg width="28" height="12" viewBox="0 0 28 12" aria-hidden="true"><path d="M1 6H27" class="ll-lg-dash"/></svg>';
      const kindSw = k => `<svg width="18" height="12" viewBox="0 0 18 12" aria-hidden="true"><path d="M1 6H17" style="stroke:var(--ll-pcb-trace-${k});stroke-width:${k === 'uplink' ? 2.4 : 1.8}"/></svg>`;
      const parts = [];
      parts.push(`<span class="ll-legend__item ll-legend__q">${dotSvg}<span>${q ? esc(t('board.legend.dot', { size: formatBytes(q, this.locale) })) : esc(t('board.legend.waiting'))}</span></span>`);
      parts.push(`<span class="ll-legend__item">${dashSvg}<span>${esc(t('board.legend.unmeasured'))}</span></span>`);
      const kl = TRACE_KINDS.filter(k => kinds.has(k));
      if (kl.length) parts.push('<span class="ll-legend__sep" aria-hidden="true"></span>' + kl.map(k => `<span class="ll-legend__item">${kindSw(k)}<span>${esc(t('board.trace.' + k))}</span></span>`).join(''));
      parts.push('<span class="ll-legend__sep" aria-hidden="true"></span><span class="ll-legend__grades"></span>');
      this.legendEl.innerHTML = parts.join('');
      this.gradesEl = this.legendEl.querySelector('.ll-legend__grades');
      this.updateGradeLegend();
    }
    updateGradeLegend() {
      if (!this.gradesEl || !this.gradeCounts) return;
      const t = this.t;
      this.gradesEl.innerHTML = GRADES.map(g => {
        const n = this.gradeCounts[g] || 0;
        return `<span class="ll-legend__grade${n ? '' : ' is-zero'}" data-grade="${g}"><i aria-hidden="true"></i>${esc(t('board.grade.' + g))} <em>${n}</em></span>`;
      }).join('');
      this.marksReposition();   // the legend is an obstacle for the labels: its width changes with the counts
    }

    // ---------------------------------------------------------------- highlight
    highlight(id) {
      this.highlighted = id && this.tiles.has(id) ? id : null;
      this.applyHighlight();
      if (this.marks) { if (this.marks.em) this.marks.em.focus(); if (this.marks.pm) this.marks.pm.focus(); }
    }
    applyHighlight() {
      const id = this.highlighted;
      this.root.classList.toggle('is-focus', !!id);
      this.root.querySelectorAll('.is-on, .is-selected').forEach(e => e.classList.remove('is-on', 'is-selected'));
      if (!id) { this.dots.setFocus(null); return; }
      const on = new Set([id]), traces = new Set();
      this.routes.forEach(r => { if (r.a === id || r.b === id) { traces.add(r.id); on.add(r.a); on.add(r.b); } });
      this.exits.forEach(x => { if (x.a === id) traces.add(x.id); });
      on.forEach(x => { const e = this.tiles.get(x); if (e) e.el.classList.add('is-on'); });
      const sel = this.tiles.get(id); if (sel) sel.el.classList.add('is-selected');
      this.svg.querySelectorAll('[data-t]').forEach(e => { if (traces.has(e.dataset.t)) e.classList.add('is-on'); });
      this.dots.setFocus(traces);
    }

    // ---------------------------------------------------------------- end and port marks (endmarks.js / portmarks.js, host API 1)
    /* A tile with peers beyond the LAN (the internet tile) gets an exit: a straight lead to the board edge on a side where it crosses
       no other tile and no zone title (the longest such lead wins); the end mark hangs at its outer end. Without a free side the
       label sits at a pad on the tile's top edge instead (no lead). */
    computeExits() {
      const lay = this.layout, out = [], M = 6;
      const titles = []; this.zones.forEach(z => { if (z.z) titles.push({ x: z.z.x + 14, y: z.z.y - 10, w: z.titleW || 160, h: 20 }); });
      const free = (at, x0, y0, x1, y1) => {
        const lo = { x: Math.min(x0, x1) - 4, y: Math.min(y0, y1) - 4 }, hi = { x: Math.max(x0, x1) + 4, y: Math.max(y0, y1) + 4 };
        const hit = q => q.x < hi.x && q.x + q.w > lo.x && q.y < hi.y && q.y + q.h > lo.y;
        return !Object.values(lay.tiles).some(q => q.id !== at && hit(q)) && !titles.some(hit);
      };
      if (lay) Object.keys(this.peers).sort().forEach(at => {
        const t = lay.tiles[at]; if (!t || !(this.peers[at] || []).length) return;
        const cy = t.y + t.h / 2, cx = t.x + t.w / 2;
        const sides = [
          { room: t.x - M, pad: { x: t.x, y: cy, s: 'l' }, end: { x: M, y: cy, s: 'l' } },
          { room: lay.w - M - t.x - t.w, pad: { x: t.x + t.w, y: cy, s: 'r' }, end: { x: lay.w - M, y: cy, s: 'r' } },
          { room: t.y - M, pad: { x: cx, y: t.y, s: 't' }, end: { x: cx, y: M, s: 't' } }
        ].filter(c => c.room >= 40 && free(at, c.pad.x, c.pad.y, c.end.x, c.end.y)).sort((p, q) => q.room - p.room);
        const c = sides[0];
        if (c) out.push({ id: 'x:' + at, a: at, b: EDGE, kind: 'uplink', pts: [[c.pad.x, c.pad.y], [c.end.x, c.end.y]], padA: c.pad, padB: c.end, edgeEnd: 'b',
          exit: { s: c.end.s, end: [c.end.x, c.end.y] }, bundles: [] });
        else { const pad = { x: cx, y: t.y, s: 't' }; out.push({ id: 'x:' + at, a: at, b: EDGE, kind: 'uplink', pts: [[cx, t.y], [cx, t.y]], padA: pad, padB: pad, edgeEnd: 'b', exit: null, bundles: [] }); }
      });
      this.exits = out;
    }
    // routed traces plus exits in the neutral form of the marks (the same object per trace while its route stays)
    traceViews() {
      const out = [], cache = this._views || (this._views = new Map()), keep = new Set();
      this.routes.forEach(r => {
        const t = this.traceInfo.get(r.id); if (!t) return;
        let v = cache.get(r.id);
        if (!v || v.route !== r) { v = { route: r, id: r.id, a: r.a, b: r.b, pts: r.pts, padA: r.padA, padB: r.padB, edgeEnd: null, exit: null, bundles: [] }; cache.set(r.id, v); }
        v.kind = TRACE_KINDS.includes(t.kind) ? t.kind : 'lan';
        keep.add(r.id); out.push(v);
      });
      cache.forEach((v, k) => { if (!keep.has(k)) cache.delete(k); });
      return out.concat(this.exits);
    }
    // port marks: target port of a flow — seen in live connections, else the first open port from the scan; unknown → nothing
    portFacts() {
      const out = [];
      this.traceViews().forEach(v => {
        if (v.kind !== 'flow') return;
        const t = this.traceInfo.get(v.id) || {}, seen = Array.isArray(this.seenPorts[v.id]) ? this.seenPorts[v.id].filter(Number.isInteger) : [];
        const listed = Array.isArray(t.listed) ? t.listed.filter(Number.isInteger) : [];
        const ports = seen.length ? seen.slice(0, 2) : listed.slice(0, 1);
        if (!ports.length) return;
        const e = this.tiles.get(v.b);
        out.push({ trace: v, end: 'b', link: 'service', targetNode: v.b, targetName: (e && e.data.title) || v.b, servicePorts: ports, listedPorts: listed,
          seen: !!seen.length, data: { target: v.b, ports, seen: !!seen.length } });
      });
      return out;
    }
    markHost() {
      const rel = (r, er) => ({ x0: r.left - er.left, y0: r.top - er.top, x1: r.right - er.left, y1: r.bottom - er.top });
      return {
        el: this.root,
        t: (k, v) => this.t(k, v),
        ready: () => this.routed && !this.dead,
        traces: () => this.traceViews(),
        node: nid => (nid === EDGE || this.tiles.has(nid) ? { host: false, systems: [] } : null),
        peers: v => (v.b === EDGE ? (this.peers[v.a] || []).map(p => ({ id: String(p.id), out: +p.out || 0, in: +p.in || 0, weight: +p.weight || 0 })) : []),
        glow: id => { const x = this.exits.find(e => e.id === id); return x && (this.peers[x.a] || []).some(p => +p.weight > 0) ? 1 : 0; },
        edgePeer: OUTSIDE,
        peerName: () => null,
        nodeName: nid => { const e = this.tiles.get(nid); return e ? (e.data.title || this.t('board.kind.' + (e.kind || 'unknown'))) : null; },
        canOpen: () => false,
        color: kind => (this.colors && this.colors[kind]) || '#7f8c86',
        tiles: () => {
          const o = {};
          if (this.layout) this.tiles.forEach((e, id) => { const p = this.layout.tiles[id]; if (p) o[id] = { x: p.x, y: p.y, w: p.w, h: p.h, el: e.el }; });
          return o;
        },
        labels: () => { const o = []; this.zones.forEach(z => { if (z.z) o.push([z.z.x + 14, z.z.y - 10, z.titleW || 160, 20]); }); return o; },
        kindFilter: () => null,
        chain: () => null,
        highlighted: () => this.highlighted,
        selected: () => this.highlighted,
        selectedId: () => this.highlighted,
        view: () => ({ s: 1, x: -this.scroll.scrollLeft, y: -this.scroll.scrollTop }),
        size: () => ({ W: this.scroll.clientWidth, H: this.scroll.clientHeight }),
        measure: () => {},
        animUntil: () => 0,
        goTo: nid => { const p = this.layout && this.layout.tiles[nid]; if (p) this.scroll.scrollTo({ top: Math.max(0, p.y - 80), behavior: 'smooth' }); },
        obstacles: er => {
          if (this.legendEl.parentNode !== this.root || this.legendEl.hidden) return [];
          const r = this.legendEl.getBoundingClientRect();
          return r.width && r.height ? [{ x0: r.left - er.left - 3, y0: r.top - er.top - 3, x1: r.right - er.left + 3, y1: r.bottom - er.top + 3 }] : [];
        },
        // port marks
        network: false,
        facts: () => this.portFacts(),
        endmarkRects: () => (this.marks && this.marks.em ? this.marks.em.specs.map(sp => sp.r) : []),
        isTile: el => !!(el.closest && el.closest('.ll-tile')),
        textRects: (L, er) => {
          const unit = r => { const q = rel(r, er); return { x0: (q.x0 - L.x) / L.s, y0: (q.y0 - L.y) / L.s, x1: (q.x1 - L.x) / L.s, y1: (q.y1 - L.y) / L.s }; };
          const free = [], tile = new Map();
          this.zones.forEach(z => { const r = z.title.getBoundingClientRect(); if (r.width >= 1 && r.height >= 1) free.push(unit(r)); });
          this.tiles.forEach((e, id) => {
            const l = [];
            [e.title, e.sub, e.metric].forEach(x => { const r = x.getBoundingClientRect(); if (r.width >= 1 && r.height >= 1) l.push(unit(r)); });
            tile.set(id, l);
          });
          return { free, tile };
        }
      };
    }
    ensureMarks() {
      if (this.marks || this.dead || (!Endmarks && !Portmarks)) return;
      const host = this.markHost();
      this.marks = { em: null, pm: null };
      if (Endmarks) this.marks.em = Endmarks.create(host, { edgeSides: 'tb' });   // the board only scrolls vertically
      if (Portmarks) this.marks.pm = Portmarks.create(host);
    }
    marksReposition() { if (this.marks) { if (this.marks.em) this.marks.em.reposition(); if (this.marks.pm) this.marks.pm.reposition(); } }

    // ---------------------------------------------------------------- teardown
    destroy() {
      if (this.dead) return;
      this.dead = true; this._routeToken = null;
      clearTimeout(this._roT);
      this._ro.disconnect();
      this.tilesEl.removeEventListener('click', this._onClick);
      this.scroll.removeEventListener('scroll', this._onScroll);
      if (document.fonts && document.fonts.removeEventListener) document.fonts.removeEventListener('loadingdone', this._onFonts);
      if (this.marks) { if (this.marks.em) this.marks.em.destroy(); if (this.marks.pm) this.marks.pm.destroy(); this.marks = null; }
      this.dots.destroy();
      this.root.remove();
    }
  }

  function normaliseView(v) {
    v = v || {};
    return {
      view: v.view || 'map',
      zones: Array.isArray(v.zones) ? v.zones : [],
      components: (Array.isArray(v.components) ? v.components : []).filter(c => c && c.id != null).map(c => Object.assign({}, c, { id: String(c.id), zone: c.zone == null ? '' : String(c.zone) })),
      traces: (Array.isArray(v.traces) ? v.traces : []).filter(t => t && t.id != null).map(t => Object.assign({}, t, { id: String(t.id), a: String(t.a), b: String(t.b) }))
    };
  }

  function mount(el, o) {
    if (!el) throw new Error('Lanloom.Board.mount: element required');
    const b = new Board(el, o);
    return {
      update: v => b.update(v),
      live: f => b.live(f),
      setTheme: t => b.setTheme(t),
      highlight: id => b.highlight(id),
      destroy: () => b.destroy(),
      ready: b.ready,
      stats: () => Object.assign({}, b.stats, { dots: Object.assign({}, b.dots.stats) }),
      marks: () => ({ end: b.marks && b.marks.em ? b.marks.em.state() : [], port: b.marks && b.marks.pm ? b.marks.pm.state() : [] }),
      _board: b
    };
  }

  LL.Board = { mount, formatBytes, fitText, STRINGS, version: '1.0.0' };
})(typeof self !== 'undefined' ? self : this);
