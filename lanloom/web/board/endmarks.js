/* Lanloom — end marks: every trace that leaves the view says where it goes.
   Lanloom.Endmarks.create(host, options) → instance; load endmarks.css as well. No dependencies, no assumptions about the host app.

   Two kinds, both calm labels in trace colour above the board (screen layer, fixed font size like map labels):
   - End mark at the pad: traces to the board edge (peers without a tile in this view) and to the frame of a host zone (the host
     itself) name their peer with a direction — "→ example.org", "← 5 sources: sensor …". The peers come from the host.
   - Edge mark at the view edge: traces cut off by zooming/panning get a label with their target at the edge (like the arrows in
     map apps); nearby traces are merged into one bundle "3 targets: …".
   Hovering/tapping a label shows the full list, an entry leads to the target (the camera moves there, or the host opens it).
   Labels never overlap and avoid tiles, labels and controls; with little room only the count. No rebuild: content runs once per
   second, placement throttled while panning/zooming; dots and traces stay untouched.

   ---------------------------------------------------------------------------------------------------- host interface, API = 1
   All coordinates of tiles, traces and texts are board units; view() maps them to screen px (x * s + x0). Functions are read live.
     host.el                 element the layers are appended to (positioned; covers the visible board, size W × H)
     host.ready()            → bool, traces are routed
     host.traces()           → [trace]; trace = {id, a, b, kind, pts: [[x, y]…], padA, padB ({x, y, s: 't'|'b'|'l'|'r'}),
                               edgeEnd: 'a'|'b'|null (end at the board edge / zone frame), exit: {s, end: [x, y]}|null (straight
                               lead from that end to the edge)}. Objects should keep their identity between calls.
     host.node(nid)          → {host: bool (the node stands for the zone's own host), systems: [peer id…]}|null
     host.peers(trace)       → [{id, out, in, weight}] peers behind the edge end, heaviest first (bytes out/in, weight)
     host.glow(traceId, now) → 0…1 current glow of the trace (dims idle labels)
     host.edgePeer           peer id that stands for "somewhere outside" (used while peers are not known yet)
     host.peerName(id)       → display name of a peer, or null (then "outside" for edgePeer, else the id)
     host.nodeName(nid)      → display name of a tile/node, or null
     host.canOpen(id)        → bool, the peer can be opened (list entry becomes a button); host.open(id) opens it
     host.color(kind)        → trace colour (CSS colour) of a trace kind
     host.tiles()            → {nid: {x, y, w, h, el}} tiles of this view
     host.labels()           → [[x, y, w, h]] free texts on the board (zone titles …), kept clear
     host.kindFilter()       → Set of visible trace kinds, or null (all)
     host.chain()            → null, or {node: nid|null, group(trace) → 0|1|2} while a chain view (hover focus) is active
     host.highlighted()      → nid|null, highlighted tile
     host.selected()         → truthy while a tile is selected
     host.view()             → {s, x, y} scale and offset (also during camera moves)
     host.size()             → {W, H} of host.el; host.measure() re-reads it
     host.animUntil()        → performance.now() timestamp until which the camera moves (0 = still)
     host.goTo(nid)          moves the camera to a tile; host.hover(nid) (optional) shows the tile's chain
     host.mayFit() / host.fit(animated)   (optional) re-fit the board after the labels' room grew
     host.obstacles(rect)    → [{x0, y0, x1, y1}] fixed controls above the board in px relative to rect (bounding rect of host.el)
     host.setUnlabeled(trace, bool)       (optional) the exit lead of this trace has no label (host may draw a plug instead)
     host.t(key, params)     → text (optional; English built in, see STRINGS)
   The host calls: place() after structure/theme changes, reposition() on every pan/zoom, focus() when hover/selection changes,
   destroy() when the board goes away. state() returns the placed labels (tests, measurements).
   options: {classAlias: {role: 'extra-class'} (extra class names per element role), locale (name ordering, default 'en'),
             edgeSides: view edges that can cut traces, default 'lrtb' (a board that only scrolls vertically passes 'tb')} */
(function (root) {
  'use strict';
  const LL = root.Lanloom = root.Lanloom || {};
  const API = 1;
  const TICK = 1000;          // content (peers, glow) once per second
  const THROTTLE = 80;        // placement while panning/zooming at most every 80 ms
  let HEIGHT = 18;            // label height in px (font + 7)
  const PAD = 16;             // padding left+right+border (edge mark: 3 px edge)
  const BUNDLE_SETTLED = 10000;        // a bundle counts as settled after standing this long (hysteresis only applies then)
  const BUNDLE_MEMORY = 60000;         // bundled ends stay bundled this long, even if their targets waver briefly
  const NAME_LEAD = 1.5;               // another target takes over the bundle's name only with 1.5 times the weight …
  const NAME_TIME = 8000;              // … lasting this long without interruption (short peaks do not change the name)
  const NAME_SIMILAR = 0.75;           // on first show: among targets from 75 % of the largest weight the fixed order wins (name A–Z)
  const MAXW = 230;           // widest label width
  const TIP = 13;             // room of an edge mark's triangle (triangle up to 8 px + gap 4 px + rounding)
  const GAP = 2;              // gap between labels
  const LIST_MAX = 14;        // rows in the drop-down

  /* strings:begin */
  const STRINGS = {
    "endmarks.outside": "outside",
    "endmarks.targets": "{n} targets",
    "endmarks.sources": "{n} sources",
    "endmarks.peers": "{n} peers",
    "endmarks.self": "{name} itself",
    "endmarks.hostItself": "{name} (the host itself)",
    "endmarks.bundleTitle": "{n} tiles → {name}",
    "endmarks.edgeTraces": "{n} traces at the edge",
    "endmarks.ariaEnd": "Trace leads outside: {target}",
    "endmarks.ariaEdge": "Trace leads out of view: {target}",
    "endmarks.trace": "Trace",
    "endmarks.leadsOut": "leads outside",
    "endmarks.leadsOutPeers": "leads outside · {n} peers",
    "endmarks.traceAtEdge": "Trace at the view edge",
    "endmarks.targetOutside": "Target outside the view",
    "endmarks.targetsOutside": "{n} targets outside the view",
    "endmarks.goTo": "Go there",
    "endmarks.toTarget": "To the target",
    "endmarks.more": "… and {n} more",
    "endmarks.fromTiles": "From {n} tiles"
  };
  /* strings:end */
  const fill = (s, v) => (v ? String(s).replace(/\{(\w+)\}/g, (m, k) => (v[k] != null ? v[k] : m)) : String(s));
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* Keep the bundled label's name stable. The shape keeps a hysteresis; the name used to follow the largest weight tick by tick
     (two opposing lines swapped it all the time). Rule: the label keeps its current main target as long as it still occurs. Another
     target replaces it only when it is NAME_LEAD times heavier and stays so for NAME_TIME without interruption. On first show, among
     similarly heavy targets (≥ NAME_SIMILAR of the largest) the fixed order by display name wins. The store remembers the members per
     side; when their number changes, the bundle with the most shared members carries on.
     → targets in display order (main target first, the rest by weight, then display name) */
  function bundleLead(store, side, members, list, now, nameOf, locale) {
    const loc = locale || 'en';
    const order = (p, q) => q.weight - p.weight || (nameOf(p.id).localeCompare(nameOf(q.id), loc) || (p.id < q.id ? -1 : 1));
    for (const [k, v] of store) if (now - v.t > BUNDLE_MEMORY) store.delete(k);
    let best = null, bestN = 0;
    for (const v of store.values()) {
      if (v.side !== side) continue;
      const n = members.filter(m => v.members.has(m)).length;
      if (n > bestN) { best = v; bestN = n; }
    }
    const raw = list.slice().sort(order), top = raw[0];
    let f = best && raw.find(q => q.id === best.lead) ? best : null;
    if (!f) {
      const limit = top.weight * NAME_SIMILAR;
      const lead = raw.filter(q => q.weight >= limit).sort((p, q) => nameOf(p.id).localeCompare(nameOf(q.id), loc) || (p.id < q.id ? -1 : 1))[0];
      f = { side, lead: lead.id, challenger: null, since: 0 };
    } else {
      const cur = raw.find(q => q.id === f.lead);
      if (top.id !== f.lead && top.weight >= cur.weight * NAME_LEAD) {
        if (f.challenger !== top.id) { f.challenger = top.id; f.since = now; }
        else if (now - f.since >= NAME_TIME) { f.lead = top.id; f.challenger = null; }
      } else f.challenger = null;
    }
    f.members = new Set(members); f.t = now;
    store.set(side + '|' + [...members].sort().join(','), f);
    for (const [k, v] of store) if (v === f && k !== side + '|' + [...members].sort().join(',')) store.delete(k);
    return [raw.find(q => q.id === f.lead)].concat(raw.filter(q => q.id !== f.lead));
  }

  // ---------------------------------------------------------------- measuring text (page font; again after the web font loaded)
  let ctxMeasure = null, fontKey = '';
  const measured = new Map();
  let fs = 11;        // label font size: 11 px, up to 13 px when zoomed in (stays smaller than the tile subtitles)
  let family = '';    // label font family (read from the layer — the stage may use a different one than body)
  let measureHook = null;   // tests: (text, font) → width
  function font() { return `600 ${fs}px ` + (family || (typeof document !== 'undefined' && getComputedStyle(document.body).fontFamily) || 'system-ui'); }
  function width(t) {
    const s = font();
    if (s !== fontKey) { measured.clear(); fontKey = s; }
    let w = measured.get(t);
    if (w == null) {
      if (measureHook) w = Math.ceil(measureHook(t, s)) + 2;
      else {
        if (!ctxMeasure) ctxMeasure = document.createElement('canvas').getContext('2d');
        ctxMeasure.font = s; w = Math.ceil(ctxMeasure.measureText(t).width) + 2;   // +2: rounding/kerning in the real text
      }
      if (measured.size > 2000) measured.clear();
      measured.set(t, w);
    }
    return w;
  }
  // Shorten the text so the label is at most MAXW wide (never overflow inside the label)
  function fitting(front, name, back, max) {
    const room = (max || MAXW) - PAD;
    if (width(front + name + back) <= room) return front + name + back;
    let n = name;
    while (n.length > 3 && width(front + n + '…') > room) n = n.slice(0, -1);
    return front + n.trimEnd() + '…';      // shortened: the "…" already shows that it goes on
  }
  // Length steps of a label: full, shorter, even shorter (depending on room)
  function steps(front, name, back, extra) { return [...new Set([MAXW, 170, 120].map(m => fitting(front, name, back, m - (extra || 0))))]; }
  // Detail level: font size of the labels at a scale
  function fontFor(s) { return Math.max(11, Math.min(13, Math.round(9.5 * s))); }

  // Direction as seen from the trace: → goes out to the peer, ← comes from it, ↔ both
  function arrowFor(out, inn) {
    if (out > 0 && inn > 0 && Math.min(out, inn) >= 0.15 * Math.max(out, inn)) return '↔';
    return out >= inn ? '→' : '←';
  }

  // ---------------------------------------------------------------- geometry
  const intersects = (a, b) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
  function free(r, lists) { for (const l of lists) for (const o of l) if (intersects(r, o)) return false; return true; }
  function rect(x, y, w, h) { return { x0: x, y0: y, x1: x + w, y1: y + h }; }
  // First exit from V on the way from pts[0] to pts[n-1] (pts in screen px) → {x, y, s} or null
  function exitPoint(pts, V) {
    for (let i = 1; i < pts.length; i++) {
      const [x1, y1] = pts[i - 1], [x2, y2] = pts[i];
      const out = x2 < V.x0 || x2 > V.x1 || y2 < V.y0 || y2 > V.y1;
      if (!out) continue;
      let best = null;
      const check = (t, s) => { if (t >= 0 && t <= 1 && (!best || t < best.t)) { const x = x1 + (x2 - x1) * t, y = y1 + (y2 - y1) * t; if (x >= V.x0 - 0.5 && x <= V.x1 + 0.5 && y >= V.y0 - 0.5 && y <= V.y1 + 0.5) best = { t, x, y, s }; } };
      if (x2 !== x1) { check((V.x0 - x1) / (x2 - x1), 'l'); check((V.x1 - x1) / (x2 - x1), 'r'); }
      if (y2 !== y1) { check((V.y0 - y1) / (y2 - y1), 't'); check((V.y1 - y1) / (y2 - y1), 'b'); }
      return best;
    }
    return null;
  }
  // Variants × candidates: first free spot; full before medium before short
  function makePlacer(imageOk) {
    return (variants, candidates, lists, extra) => {
      for (const text of variants) {
        const w = Math.min(MAXW, width(text) + PAD + (extra || 0));
        for (const k of candidates) {
          const [x, y] = k(w, HEIGHT), r = rect(Math.round(x), Math.round(y), w, HEIGHT);
          const rp = { x0: r.x0 - GAP, y0: r.y0 - GAP, x1: r.x1 + GAP, y1: r.y1 + GAP };
          if (imageOk(r) && free(rp, lists)) return { r, text, w, short: text !== variants[0], k };
        }
      }
      return null;
    };
  }

  // ---------------------------------------------------------------- one layer per board
  class Endmarks {
    constructor(host, options) {
      this.host = host; this.o = Object.assign({}, options || {}); this.dead = false; this.ends = []; this.specs = [];
      this.alias = this.o.classAlias || {}; this.locale = this.o.locale || 'en'; this.edgeSides = this.o.edgeSides || 'lrtb';
      this.t = (k, v) => { const r = typeof host.t === 'function' ? host.t(k, v) : null; return r == null || r === k ? fill(STRINGS[k] || k, v) : r; };
      this.marks = new Map(); this._lz = 0; this._lt = 0; this._base = null;
      const layer = this.el = document.createElement('div'); layer.className = this.cls('layer');
      layer.innerHTML = `<div class="${this.cls('ends')}"></div><div class="${this.cls('edges')}"></div>`;
      this.elEnds = layer.firstChild; this.elEdges = layer.lastChild;
      const pop = this.pop = document.createElement('div'); pop.className = this.cls('list'); pop.hidden = true; pop.setAttribute('role', 'dialog');
      host.el.appendChild(layer); host.el.appendChild(pop);
      // Labels and list do not belong to the stage: no dragging, no click into the void (which would close the details), no hover
      const stop = ev => ev.stopPropagation();
      const MARK = '.ll-em-mark';
      [layer, pop].forEach(e => ['pointerdown', 'pointerup', 'click', 'dblclick'].forEach(t => e.addEventListener(t, stop)));
      layer.addEventListener('pointermove', ev => { if (ev.target.closest(MARK)) ev.stopPropagation(); });
      pop.addEventListener('pointermove', stop);
      pop.addEventListener('wheel', ev => ev.stopPropagation(), { passive: true });
      layer.addEventListener('pointerover', ev => { const m = ev.target.closest(MARK); if (m) this.over(m); });
      layer.addEventListener('pointerout', ev => { const m = ev.target.closest(MARK); if (m && !m.contains(ev.relatedTarget)) this.leave(m); });
      layer.addEventListener('click', ev => { const m = ev.target.closest(MARK); if (m) this.toggleList(m, true); });
      layer.addEventListener('focusin', ev => { const m = ev.target.closest(MARK); if (m) this.showList(m, false); });
      layer.addEventListener('keydown', ev => { if (ev.key === 'Escape') this.closeList(); });
      pop.addEventListener('pointerenter', () => clearTimeout(this._popT));
      pop.addEventListener('pointerleave', () => { if (!this._pinned) this._popT = setTimeout(() => this.closeList(), 260); });
      pop.addEventListener('click', ev => {
        const q = ev.target.closest('[data-q]');
        if (q) { const sp = this._popMark && this._popMark._spec, e = sp && sp.sources && sp.sources[+q.dataset.q]; this.closeList(); const k = e && this.host.tiles() && this.host.tiles()[e.nid]; if (k) { this.host.goTo(e.nid); Endmarks.lastTarget = { kind: 'tile', nid: e.nid }; } return; }
        const z = ev.target.closest('[data-i]'); if (z) this.go(+z.dataset.i);
      });
      pop.addEventListener('keydown', ev => { if (ev.key === 'Escape') { const m = this._popMark; this.closeList(); if (m && m.isConnected) m.focus(); } });
      this._outside = ev => { if (!this.pop.hidden && !this.pop.contains(ev.target) && !(ev.target.closest && ev.target.closest(MARK))) this.closeList(); };
      document.addEventListener('pointerdown', this._outside, true);
      this._tick = setInterval(() => this.content(), TICK);
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { measured.clear(); if (!this.dead) this.place(); });
      this._fontsT = () => { measured.clear(); if (!this.dead) this.reposition(); };
      if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', this._fontsT);
      this.content();
    }
    // class names: own (ll-em-<role>) plus an optional alias of the host
    cls(role) { return 'll-em-' + role + (this.alias[role] ? ' ' + this.alias[role] : ''); }
    peerName(id) {
      const n = this.host.peerName(id);
      if (n != null) return n;
      if (id === this.host.edgePeer) return this.t('endmarks.outside');
      return String(id);
    }
    nodeName(nid) { return this.host.nodeName(nid); }

    // ------------------------------------------------------------ content: edge traces and their peers (1 s)
    content() {
      if (this.dead) return;
      if (!this.host.ready() || document.hidden) return;
      const host = this.host, now = performance.now();
      const ends = [];
      for (const b of host.traces()) {
        if (!b.edgeEnd) continue;
        const rid = b.edgeEnd === 'a' ? b.a : b.b, inner = b.edgeEnd === 'a' ? b.b : b.a;
        const k = host.node(rid), pad = b.edgeEnd === 'a' ? b.padA : b.padB;
        if (!k || !pad) continue;
        let peers = host.peers(b);
        if (k.host) {   // the host itself: the peer is the host of the zone
          const own = peers.filter(z => k.systems.includes(z.id));
          peers = own.length ? own : [{ id: k.systems[0], out: 1, in: 0, weight: 1 }];
        }
        // Exit whose peers are not booked yet (traffic came before the trace): label "↔ outside" instead of an open end
        if (!peers.length && b.exit && !k.host) peers = [{ id: host.edgePeer, out: 1, in: 1, weight: 0 }];
        if (!peers.length) continue;
        const g = host.glow(b.id, now);
        ends.push({ b, inner, rid, pad, peers, host: !!k.host, g, weight: peers.reduce((a, z) => a + z.weight, 0) });
      }
      // one exit per tile and side — traces with the same end (same tile, same side) are merged into one strand with one label
      // (peers added up), never several stubs on top of each other
      const strand = new Map(), all = [];
      for (const e of ends) {
        if (!e.b.exit) { all.push(e); continue; }
        const k = e.inner + '|' + e.b.exit.s + '|' + Math.round(e.b.exit.end[0]) + ',' + Math.round(e.b.exit.end[1]);
        const x = strand.get(k);
        if (!x) { const n = Object.assign({}, e, { tracesAll: [e.b], peers: e.peers.map(z => Object.assign({}, z)) }); strand.set(k, n); all.push(n); continue; }
        x.tracesAll.push(e.b); x.g = Math.max(x.g, e.g);
        for (const z of e.peers) { const y = x.peers.find(q => q.id === z.id); if (y) { y.out += z.out; y.in += z.in; y.weight += z.weight; } else x.peers.push(Object.assign({}, z)); }
        x.peers.sort((p, q) => q.weight - p.weight || (p.id < q.id ? -1 : 1)); x.weight = x.peers.reduce((a, z) => a + z.weight, 0);
      }
      this.ends = all.sort((p, q) => q.weight - p.weight || (p.inner < q.inner ? -1 : 1));
      this.measureNeed();
      this.place();
      if (!this.pop.hidden && this._popMark) this.refreshList();
    }

    /* Room the labels need per side (screen px, font 13 px) — full form "↔ 13 peers: router …" and medium "↔ 13 peers". The host
       leaves that much margin when fitting (the board keeps ≥ 70 % of the scale without this margin). The need only grows (no zooming
       back and forth); when it grows noticeably and nobody has zoomed, the board is fitted again. */
    measureNeed() {
      const prev = fs; fs = 13;
      const t = this.t;
      const next = { l: null, r: null, t: 0, b: 0 }, LEAD = 30;   // LEAD: lead beyond the board + room to the view edge
      for (const e of this.ends) {
        const a = e.b.exit; if (!a) continue;
        if (a.s === 't' || a.s === 'b') { next[a.s] = HEIGHT + 14; continue; }
        const z = e.peers, out = z.reduce((x, q) => x + q.out, 0), inn = z.reduce((x, q) => x + q.in, 0), p = arrowFor(out, inn);
        const word = p === '→' ? 'endmarks.targets' : p === '←' ? 'endmarks.sources' : 'endmarks.peers', first = this.peerName(z[0].id);
        const full = z.length === 1 ? fitting(p + ' ', first, '', MAXW) : fitting(`${p} ${t(word, { n: z.length })}: `, first, ' …', MAXW);
        const mid = z.length === 1 ? fitting(p + ' ', first, '', 120) : `${p} ${t(word, { n: z.length })}`;
        const w = { full: Math.min(MAXW, width(full) + PAD) + LEAD, mid: Math.min(MAXW, width(mid) + PAD) + LEAD };
        const x = next[a.s] || (next[a.s] = { full: 0, mid: 0 });
        x.full = Math.max(x.full, w.full); x.mid = Math.max(x.mid, w.mid);
      }
      fs = prev;
      const b = this.need || { l: null, r: null, t: 0, b: 0 };
      let more = false;
      for (const k of ['l', 'r']) {
        if (!next[k]) continue;
        if (!b[k]) { b[k] = next[k]; more = true; continue; }
        if (next[k].full > b[k].full + 16 || next[k].mid > b[k].mid + 16) { b[k] = { full: Math.max(b[k].full, next[k].full), mid: Math.max(b[k].mid, next[k].mid) }; more = true; }
      }
      for (const k of ['t', 'b']) if (next[k] > b[k]) { b[k] = next[k]; more = true; }
      this.need = b;
      const host = this.host;
      if (more && host.fit && host.mayFit && host.mayFit()) host.fit(!this._firstFit ? false : true);
      if (more) this._firstFit = true;
    }

    /* Several exits on the same edge with the same main target (e.g. four tiles → "Other internet services" at the top) used to stack
       as equal labels. Now: ends of the same side and the same main target that lie close together along the edge (≤ GAP_MAX px
       between neighbours) are merged into ONE label — "↔ 4× Other internet services". A bus in trace colour runs along the edge from
       end to end and to the label (like a bundle trace); hover and list show all sources. The main target of one among the targets of
       the other also counts as the same target (direction does not matter). → Set of the bundled ends */
    placeBundles(c) {
      const GAP_MAX = 260, done = new Set(), { tx, ty, V, inside, hard, soft, tiles, taken, specs, placeAt } = c;
      const t = this.t, host = this.host;
      const cand = [];
      for (const e of this.ends) {
        const a = e.b.exit; if (!a || e.host) continue;
        const px = tx(a.end[0]), py = ty(a.end[1]); if (!inside(px, py, V)) continue;
        /* The target sets waver from tick to tick (a target shows up only now and then) — a bundle flickered between one label and
           single labels. So `_bundlePairs` remembers which ends were bundled (BUNDLE_MEMORY long): such a pair stays related even if no
           shared target is visible in the current tick. */
        cand.push({ e, s: a.s, px, py, sl: a.s + '|' + e.inner, target: e.peers[0].id, set: new Set(e.peers.map(z => z.id)) });
      }
      /* "Same target" does not only mean the same main target. Ends of one side belong together when the main target of one is among
         the targets of the other (direction does not matter) — connected through neighbours along the edge (≤ GAP_MAX). The label then
         shows both directions ("↔"). */
      const groups = new Map();
      cand.forEach(k => { if (!groups.has(k.s)) groups.set(k.s, []); groups.get(k.s).push(k); });
      const pairs = this._bundlePairs || (this._bundlePairs = new Map()), nowP = Date.now();
      for (const [k, v] of pairs) if (nowP - v.t > BUNDLE_MEMORY) pairs.delete(k);
      const pairKey = (p, q) => p.sl < q.sl ? p.sl + '~' + q.sl : q.sl + '~' + p.sl;
      // only a bundle that stood BUNDLE_SETTLED long is remembered — short random bundles while building up leave no trace
      const settled = (p, q) => { const v = pairs.get(pairKey(p, q)); return !!v && v.t - v.since >= BUNDLE_SETTLED; };
      const related = (p, q) => p.set.has(q.target) || q.set.has(p.target) || settled(p, q);
      const heaps = [];
      for (const list of groups.values()) {
        if (list.length < 2) continue;
        const across = list[0].s === 'l' || list[0].s === 'r', along = k => across ? k.py : k.px;
        list.sort((p, q) => along(p) - along(q));
        // union: pairs with a shared target that lie close along the edge (not further than GAP_MAX from the next member)
        const head = list.map((_, i) => i), find = i => { while (head[i] !== i) i = head[i] = head[head[i]]; return i; };
        /* the gap grows with the picture (2560 px wide: 358 px instead of 260 — otherwise an end 321 px beside the bundle stayed single)
           and by half for already bundled pairs (hysteresis: no back and forth at the limit) */
        const gapMax = Math.max(GAP_MAX, 0.14 * (across ? V.y1 : V.x1));
        for (let i = 0; i < list.length; i++) for (let j = i + 1; j < list.length && along(list[j]) - along(list[i]) <= gapMax * 1.5; j++) {
          const d = along(list[j]) - along(list[i]);
          if ((d <= gapMax && related(list[i], list[j])) || (settled(list[i], list[j]) && d <= gapMax * 1.5)) head[find(j)] = find(i);
        }
        const byRoot = new Map();
        list.forEach((k, i) => { const r = find(i); if (!byRoot.has(r)) byRoot.set(r, []); byRoot.get(r).push(k); });
        for (const h of byRoot.values()) if (h.length >= 2) heaps.push({ h, across, along });
      }
      {
        for (const { h, across, along } of heaps) {
          const s = h[0].s, outward = { t: -1, l: -1, b: 1, r: 1 }[s];
          // bus on the outermost position of the ends (parallel to the edge), label centred in front of it
          const across2 = across ? (outward < 0 ? Math.min(...h.map(k => k.px)) : Math.max(...h.map(k => k.px))) : (outward < 0 ? Math.min(...h.map(k => k.py)) : Math.max(...h.map(k => k.py)));
          const lo = along(h[0]), hi = along(h[h.length - 1]), mid = (lo + hi) / 2;
          // add up targets
          // per target remember the directions of the single ends: if it runs out at one and in at the other, the label or list entry
          // shows both directions ("↔"), even if one side is small in bytes
          const peers = new Map(), dirs = new Set();
          h.forEach(k => {
            let r1 = 0, r2 = 0;
            k.e.peers.forEach(z => {
              r1 += z.out; r2 += z.in;
              const y = peers.get(z.id), d = arrowFor(z.out, z.in);
              if (y) { y.out += z.out; y.in += z.in; y.weight += z.weight; y.dirs.add(d); } else peers.set(z.id, Object.assign({}, z, { dirs: new Set([d]) }));
            });
            dirs.add(arrowFor(r1, r2));
          });
          const both = m => m.has('↔') || (m.has('→') && m.has('←'));
          const zl = bundleLead(this._bundleNames || (this._bundleNames = new Map()), s, h.map(k => k.e.inner), [...peers.values()], nowP, id => this.peerName(id), this.locale);
          zl.forEach(q => { q.arrow = both(q.dirs) ? '↔' : arrowFor(q.out, q.in); });
          const out = zl.reduce((x, q) => x + q.out, 0), inn = zl.reduce((x, q) => x + q.in, 0), p = both(dirs) ? '↔' : arrowFor(out, inn);
          const name = this.peerName(zl[0].id), n = h.length, more = zl.length > 1 ? ' …' : '';
          const variants = [...new Set([...steps(`${p} ${n}× `, name, more), `${p} ${n}×`])];
          const h2 = HEIGHT / 2;
          // position: in front of the bus outwards, centred; otherwise shifted along the bus (still touching it)
          const offset = [0, -0.25, 0.25, -0.5, 0.5].map(f => f * Math.max(40, hi - lo));
          // label 3/7/12 px in front of the bus (a short stub connects) — right at the edge it would hit the tiles of the first row
          const spots = [], inner = [], further = [];
          for (const DIST of [3, 7, 12]) {
            // outside (in front of the bus) and inside (on the tile side of the bus)
            const xA = w => s === 'l' ? across2 - DIST - w : across2 + DIST, yA = s === 't' ? across2 - DIST - HEIGHT : across2 + DIST;
            const xI = w => s === 'l' ? across2 + DIST : across2 - DIST - w, yI = s === 't' ? across2 + DIST : across2 - DIST - HEIGHT;
            offset.forEach(d => { const m = mid + d; spots.push(across ? (w => [xA(w), m - h2]) : (w => [m - w / 2, yA])); inner.push(across ? (w => [xI(w), m - h2]) : (w => [m - w / 2, yI])); });
            // … or next to the bundle (bus extended to the label), where there is more room along the edge
            if (across) { spots.push(w => [xA(w), lo - 10 - HEIGHT], w => [xA(w), hi + 10]); inner.push(w => [xI(w), lo - 10 - HEIGHT], w => [xI(w), hi + 10]); }
            else { spots.push(w => [lo - 12 - w, yA], w => [hi + 12, yA]); inner.push(w => [lo - 12 - w, yI], w => [hi + 12, yI]); }
            // next to a control at the edge (fault notice, bars): past it left/right or above/below
            for (const o of hard) {
              if (across ? !(o.y1 > lo - 200 && o.y0 < hi + 200) : !(o.x1 > lo - 300 && o.x0 < hi + 300)) continue;
              if (across) [xA, xI].forEach(fx => further.push(w => [fx(w), o.y0 - 6 - HEIGHT], w => [fx(w), o.y1 + 6]));
              else [yA, yI].forEach(y => further.push(w => [o.x0 - 6 - w, y], w => [o.x1 + 6, y]));
            }
            // further along the edge (the bus runs to the label)
            for (let k = 1; k <= 12; k++) for (const sg of [-1, 1]) {
              const m = mid + sg * k * 34;
              [across ? xA : null, across ? xI : null].forEach((fx, j) => {
                if (across) further.push(w => [fx(w), m - h2]);
                else further.push(w => [m - w / 2, j ? yI : yA]);
              });
            }
          }
          // The bundle never falls back into single labels just because the preferred spot is taken (a fault notice often sits at the
          // bottom): first full texts outside → inside → next to obstacles → further along, then over labels, last "→ 2×"
          let pl = null;
          for (const tx2 of variants) { for (const L of [spots, inner, further]) { pl = placeAt([tx2], L, [hard, soft, taken]); if (pl) break; } if (pl) break; }
          if (!pl) for (const tx2 of variants) { pl = placeAt([tx2], [...spots, ...inner, ...further], [hard, tiles, taken]); if (pl) break; }
          // last fallbacks: only avoid hard obstacles, then anywhere in the picture — a bundle never turns into single labels
          if (!pl) for (const tx2 of variants) { pl = placeAt([tx2], [...spots, ...inner, ...further], [hard]); if (pl) break; }
          if (!pl) for (const tx2 of variants.slice().reverse()) { pl = placeAt([tx2], [...spots, ...inner, ...further], []); if (pl) break; }
          if (!pl) continue;
          taken.push(pl.r);
          h.forEach((k, i) => h.slice(i + 1).forEach(q => { const pk = pairKey(k, q), v = pairs.get(pk); pairs.set(pk, { since: v ? v.since : nowP, t: nowP }); }));
          // connection: middle of the label edge facing the bus (the plug sits there) — outside or inside, depending on the label
          const beyond = across ? pl.r.x0 >= across2 : pl.r.y0 >= across2;
          const anc = across ? [beyond ? pl.r.x0 : pl.r.x1, (pl.r.y0 + pl.r.y1) / 2] : [(pl.r.x0 + pl.r.x1) / 2, beyond ? pl.r.y0 : pl.r.y1], ancAlong = across ? anc[1] : anc[0];
          const sideS = across ? (beyond ? 'r' : 'l') : (beyond ? 'b' : 't');
          // bus: from end to end along the edge (including the label's connection), plus short feeders of the ends onto the bus
          const sLo = Math.min(lo, ancAlong), sHi = Math.max(hi, ancAlong), bus = [];
          bus.push(across ? [across2, sLo, across2, sHi] : [sLo, across2, sHi, across2]);
          bus.push(across ? [across2, ancAlong, anc[0], ancAlong] : [ancAlong, across2, ancAlong, anc[1]]);   // stub to the label
          h.forEach(k => { const d = across ? Math.abs(k.px - across2) : Math.abs(k.py - across2); if (d > 0.5) bus.push(across ? [k.px, k.py, across2, k.py] : [k.px, k.py, k.px, across2]); });
          const traces = h.flatMap(k => k.e.tracesAll || [k.e.b]);
          const sources = h.map(k => ({ nid: k.e.inner, name: this.nodeName(k.e.inner) || k.e.inner }));
          specs.push({ key: 'eb:' + s + ':' + h.map(k => k.e.inner).sort().join(','), kind: 'end', exit: true, side: sideS, dir: s, r: pl.r, text: pl.text, short: pl.short,
            color: host.color(h[0].e.b.kind), g: Math.max(...h.map(k => k.e.g)), inner: h[0].e.inner, traces, anchor: anc, along: false, bus, sources,
            title: t('endmarks.bundleTitle', { n, name }), bundle: n,
            entries: zl.map(q => ({ kind: 'peer', id: q.id, name: this.peerName(q.id), arrow: q.arrow, weight: q.weight })) });
          h.forEach(k => done.add(k.e));
        }
      }
      return done;
    }

    // ------------------------------------------------------------ placement (throttled); panning without zoom: end marks follow at once
    reposition() {
      if (this.dead) return;
      const L = this.host.view(), B = this._base;
      if (B && Math.abs(L.s - B.s) < 1e-6) {
        const dx = L.x - B.x, dy = L.y - B.y;
        this.elEnds.style.transform = `translate(${dx.toFixed(1)}px,${dy.toFixed(1)}px)`;
        this.carried(dx, dy);
      }
      if (this._lt) return;
      const wait = Math.max(0, THROTTLE - (performance.now() - this._lz));
      this._lt = setTimeout(() => {
        this._lt = 0; this._lz = performance.now();
        this.place();
        if (this.host.animUntil() && performance.now() < this.host.animUntil()) this.reposition();   // camera move: follow to the end
      }, wait);
    }

    /* While dragging the end marks move along by transform, the edge marks stay at the view edge — until the next placement (≤ 80 ms)
       they could overlap or slip under a control or out of the picture. Such end marks step back meanwhile. */
    carried(dx, dy) {
      const edge = this.specs.filter(sp => sp.kind === 'edge').map(sp => sp.r), hard = this._hard || [], S = this.host.size(), W = S.W, H = S.H;
      this.marks.forEach(m => {
        const sp = m._spec; if (!sp || sp.kind !== 'end') return;
        const r = { x0: sp.r.x0 + dx, y0: sp.r.y0 + dy, x1: sp.r.x1 + dx, y1: sp.r.y1 + dy };
        const gone = r.x0 < 2 || r.y0 < 2 || r.x1 > W - 2 || r.y1 > H - 2 || edge.some(q => intersects(r, q)) || hard.some(q => intersects(r, q));
        this.toggle(m, 'hidden', gone);
      });
    }
    // fixed controls above the stage (from the host)
    uiRects(er) { return this.host.obstacles ? this.host.obstacles(er) : []; }
    toggle(el, role, on) { el.classList.toggle('ll-em-' + role, on); if (this.alias[role]) el.classList.toggle(this.alias[role], on); }

    // ------------------------------------------------------------ layout
    place() {
      if (this.dead || !this.host.ready()) return;
      const host = this.host, t = this.t;
      let S = host.size();
      if (!S.W || !S.H) { host.measure(); S = host.size(); }
      const L = host.view(), W = S.W, H = S.H;
      if (!W || !H) return;
      const er = host.el.getBoundingClientRect();
      fs = fontFor(L.s); HEIGHT = fs + 7;
      family = getComputedStyle(this.el).fontFamily || family;
      this.el.style.setProperty('--ll-em-fs', fs + 'px'); this.el.style.setProperty('--ll-em-h', HEIGHT + 'px');
      const tx = x => x * L.s + L.x, ty = y => y * L.s + L.y;
      const V = { x0: 4, y0: 4, x1: W - 4, y1: H - 4 };
      const VR = { x0: 6, y0: 6, x1: W - 6, y1: H - 6 };
      const hard = this.uiRects(er), soft = [], taken = [], tiles = [];
      this._hard = hard;
      const underUi = q => hard.some(o => q[0] >= o.x0 && q[0] <= o.x1 && q[1] >= o.y0 && q[1] <= o.y1);
      const visible = r => r.x1 > V.x0 && r.x0 < V.x1 && r.y1 > V.y0 && r.y0 < V.y1;
      const T = host.tiles() || {};
      Object.values(T).forEach(k => { const r = { x0: tx(k.x) - 2, y0: ty(k.y) - 2, x1: tx(k.x + k.w) + 2, y1: ty(k.y + k.h) + 2 }; if (visible(r)) { soft.push(r); tiles.push(r); } });
      (host.labels() || []).forEach(q => { const r = { x0: tx(q[0]), y0: ty(q[1]), x1: tx(q[0] + q[2]), y1: ty(q[1] + q[3]) }; if (visible(r)) soft.push(r); });
      const inside = (x, y, R) => x >= R.x0 && x <= R.x1 && y >= R.y0 && y <= R.y1;
      const inImage = r => r.x0 >= V.x0 && r.y0 >= V.y0 && r.x1 <= V.x1 && r.y1 <= V.y1;
      const specs = [];
      const placeAt = makePlacer(inImage);

      // 1) end marks at visible pads
      const edgeLeft = [];   // pad outside the picture → edge mark at the tile's side
      // same targets on the same edge → ONE label (bundle), the ends hang on a bus along the edge
      const inBundle = this.placeBundles ? this.placeBundles({ tx, ty, V, inside, hard, soft, tiles, taken, specs, placeAt }) : new Set();
      const unlabeled = (b, v) => { if (b.exit && host.setUnlabeled) host.setUnlabeled(b, v); };
      for (const e of this.ends) {
        if (inBundle.has(e)) { (e.tracesAll || [e.b]).forEach(b => unlabeled(b, false)); continue; }
        const exit = e.b.exit, tracesE = e.tracesAll || [e.b];
        const px = tx(exit ? exit.end[0] : e.pad.x), py = ty(exit ? exit.end[1] : e.pad.y);
        if (exit) tracesE.forEach(b => unlabeled(b, false));
        if (!inside(px, py, V)) { edgeLeft.push(e); continue; }
        const z = e.peers, out = z.reduce((a, q) => a + q.out, 0), inn = z.reduce((a, q) => a + q.in, 0);
        const p = arrowFor(out, inn), first = e.host ? t('endmarks.self', { name: this.peerName(z[0].id) }) : this.peerName(z[0].id);
        const word = p === '→' ? 'endmarks.targets' : p === '←' ? 'endmarks.sources' : 'endmarks.peers';
        const variants = z.length === 1 ? [...new Set([...steps(p + ' ', first, ''), fitting(p + ' ', first, '', 84), ...(e.b.exit ? [p + ' 1'] : [])])]
          : [...steps(`${p} ${t(word, { n: z.length })}: `, first, ' …'), `${p} ${t(word, { n: z.length })}`, `${p} ${z.length}`];
        // exit: the smallest form is always direction + count ("↔ 4", "→ 1") — never "…" or a bare number; tappable, the list names all
        const s = exit ? exit.s : e.pad.s, a = 6, h2 = HEIGHT / 2;
        if (exit) {
          /* Exit: the label hangs at the end of the straight lead — it touches the end (plug at the label edge), facing outwards. No
             free shifting: with little room the text gets shorter (down to "↔ 13"), never over a tile. Only under a control (fault
             notice …) it moves to that control's edge as before (the lead is hidden there). */
          // position of the label relative to the end (= edge where the lead arrives): first outwards in running direction; if there
          // is no room (view edge, control), it hangs like a flag beside the end — the lead still touches it
          const posA = hs => ({
            r: [(w) => [px, py - h2]], l: [(w) => [px - w, py - h2]],
            t: [(w) => [px - w / 2, py - HEIGHT], (w) => [px - 12, py - HEIGHT], (w) => [px - w + 12, py - HEIGHT]],
            b: [(w) => [px - w / 2, py], (w) => [px - 12, py], (w) => [px - w + 12, py]]
          }[hs]);
          /* Positions in this order, each with full to short texts (down to "↔ 13" or "↔ …"), never over a tile:
             1. outwards at the end of the lead;
             2. on the lead, sliding from the end towards the tile (tight, e.g. side bar open): the label starts where the lead
                arrives (plug at this edge), the lead runs on under the label;
             3. as a flag beside the end;
             4. like 1./2., may cover labels (not tiles) then.
             Only if that fails too the lead ends in a plug stroke (drawn by the host). */
          const P = (e.b.edgeEnd === 'a' ? e.b.pts.slice().reverse() : e.b.pts).map(q => [tx(q[0]), ty(q[1])]);   // tile → end
          const onLead = [];
          for (let i = P.length - 1; i > 0 && onLead.length < 120; i--) {
            const [x2, y2] = P[i], [x1, y1] = P[i - 1], dx = x2 - x1, dy = y2 - y1, L2 = Math.hypot(dx, dy); if (L2 < 0.5) continue;
            const acr = Math.abs(dx) >= Math.abs(dy), sd = acr ? (dx > 0 ? 'r' : 'l') : (dy > 0 ? 'b' : 't');   // label side seen from the arrival point
            for (let d = (i === P.length - 1 ? 4 : 0); d <= L2; d += 4) {
              const x = x2 - dx * d / L2, y = y2 - dy * d / L2;
              if (i === 1 && L2 - d < 5) break;          // not into the tile (obstacle +2, gap 2, 1 px lead visible)
              const f = { r: (w) => [x, y - h2], l: (w) => [x - w, y - h2], b: (w) => [x - w / 2, y], t: (w) => [x - w / 2, y - HEIGHT] }[sd];
              f.anc = [x, y]; f.side = sd; onLead.push(f);
            }
          }
          const outer = posA(s).map(f => { f.anc = [px, py]; f.side = s; return f; });
          const flags = seq => seq.flatMap(hs => posA(hs).map(f => { f.anc = [px, py]; f.side = hs; return f; }));
          let pl = placeAt(variants, outer, [hard, soft, taken])
            || placeAt(variants, onLead, [hard, soft, taken])
            || placeAt(variants, flags(({ t: ['r', 'l'], b: ['r', 'l'], l: ['t', 'b'], r: ['t', 'b'] }[s] || [])), [hard, soft, taken])
            || placeAt(variants, [...outer, ...onLead, ...flags(({ t: ['r', 'l'], b: ['r', 'l'], l: ['t', 'b'], r: ['t', 'b'] }[s] || []))], [hard, tiles, taken]);
          let anc = pl ? pl.k.anc : [px, py], hangs = pl ? pl.k.side : s;
          let loose = false;
          if (!pl && hard.some(o => px >= o.x0 - 2 && px <= o.x1 + 2 && py >= o.y0 - HEIGHT && py <= o.y1 + HEIGHT)) {
            const cover = hard.find(o => px >= o.x0 - 2 && px <= o.x1 + 2 && py >= o.y0 - HEIGHT && py <= o.y1 + HEIGHT);
            const vd = { b: (w) => [px - w / 2, cover.y0 - HEIGHT - 3], t: (w) => [px - w / 2, cover.y1 + 3], l: (w) => [cover.x1 + 3, py - h2], r: (w) => [cover.x0 - w - 3, py - h2] }[s];
            const away = { b: [0, 1], t: [0, -1], l: [-1, 0], r: [1, 0] }[s] || [1, 0];
            if (vd) { pl = placeAt(variants, [vd, ...[1, -1, 2, -2].map(j => (w) => { const q = vd(w); return away[0] ? [q[0], q[1] + j * (HEIGHT + 3)] : [q[0] + j * (w / 2 + 6), q[1]]; })], [hard, tiles, taken]); loose = !!pl; }   // never over a tile here either
          }
          /* Emergency (very tight: end under the area bar or the side-bar button, at the top close to the header): the label searches
             in growing circles around the last visible point of the lead for the nearest free spot (never over tiles, controls or other
             labels) and hangs on a thin leash in trace colour — so no lead ends without a label. */
          let leash = null;
          if (!pl) {
            const under = (x, y) => hard.some(o => x >= o.x0 && x <= o.x1 && y >= o.y0 && y <= o.y1) || !inside(x, y, V);
            let a0 = [px, py];
            if (under(px, py)) { const f = onLead.find(f => !under(f.anc[0], f.anc[1])); if (f) a0 = f.anc; else if (P.length) a0 = P[0]; }
            const crosses = (x1, y1, x2, y2, withTiles) => { for (let tt = 0.15; tt < 1; tt += 0.1) { const x = x1 + (x2 - x1) * tt, y = y1 + (y2 - y1) * tt; if ((withTiles && tiles.some(o => x > o.x0 && x < o.x1 && y > o.y0 && y < o.y1)) || hard.some(o => x > o.x0 && x < o.x1 && y > o.y0 && y < o.y1)) return true; } return false; };
            const circle = [];
            for (const rad of [22, 34, 48, 64, 84, 108, 136, 170, 210]) for (let k = 0; k < 32; k++) {
              const wi = k / 32 * 2 * Math.PI, cx = a0[0] + Math.cos(wi) * rad, cy = a0[1] + Math.sin(wi) * rad;
              circle.push((w) => [cx - w / 2, cy - h2]);
            }
            // first a leash that crosses no tile; otherwise it may run (thin) over a tile — the label itself never
            for (const [text, withTiles] of [...variants.slice(-3).map(x => [x, true]), ...variants.slice(-2).map(x => [x, false])]) {
              const w = Math.min(MAXW, width(text) + PAD);
              for (const k of circle) {
                const [x, y] = k(w), r = rect(Math.round(x), Math.round(y), w, HEIGHT), rp = { x0: r.x0 - GAP, y0: r.y0 - GAP, x1: r.x1 + GAP, y1: r.y1 + GAP };
                if (!inImage(r) || !free(rp, [hard, tiles, taken])) continue;
                const nx = Math.max(r.x0, Math.min(r.x1, a0[0])), ny = Math.max(r.y0, Math.min(r.y1, a0[1]));   // nearest point of the label
                if (crosses(a0[0], a0[1], nx, ny, withTiles)) continue;
                pl = { r, text, w, short: text !== variants[0] }; leash = [a0[0], a0[1], nx, ny]; anc = [nx, ny];
                hangs = Math.abs(nx - r.x0) < 0.5 ? 'r' : Math.abs(nx - r.x1) < 0.5 ? 'l' : Math.abs(ny - r.y0) < 0.5 ? 'b' : 't';
                break;
              }
              if (pl) break;
            }
          }
          if (!pl) { tracesE.forEach(b => unlabeled(b, true)); continue; }
          taken.push(pl.r);
          specs.push({ key: 'e:' + e.inner + '|' + e.rid + '|' + s, kind: 'end', exit: !loose, side: loose ? s : hangs, dir: s, r: pl.r, text: pl.text, short: pl.short, color: host.color(e.b.kind),
            g: e.g, inner: e.inner, traces: tracesE, anchor: anc, along: !leash && (anc[0] !== px || anc[1] !== py), leash, title: this.nodeName(e.inner),
            entries: z.map(q => ({ kind: 'peer', id: q.id, name: this.peerName(q.id), arrow: arrowFor(q.out, q.in), weight: q.weight })) });
          continue;
        }
        const cand = {   // first outwards (beyond the edge), then inside next to the trace above/below the pad
          b: [(w) => [px - w / 2, py + a], (w) => [px + a, py - HEIGHT - 3], (w) => [px - a - w, py - HEIGHT - 3], (w) => [px + a, py - 2 * HEIGHT - 6], (w) => [px - a - w, py - 2 * HEIGHT - 6]],
          t: [(w) => [px - w / 2, py - HEIGHT - a], (w) => [px + a, py + 3], (w) => [px - a - w, py + 3], (w) => [px + a, py + HEIGHT + 6], (w) => [px - a - w, py + HEIGHT + 6]],
          l: [(w) => [px - w - a, py - h2], (w) => [px + 3, py - HEIGHT - 4], (w) => [px + 3, py + 4], (w) => [px + 3, py - 2 * HEIGHT - 7], (w) => [px + 3, py + HEIGHT + 7]],
          r: [(w) => [px + a, py - h2], (w) => [px - w - 3, py - HEIGHT - 4], (w) => [px - w - 3, py + 4], (w) => [px - w - 3, py - 2 * HEIGHT - 7], (w) => [px - w - 3, py + HEIGHT + 7]]
        }[s] || [(w) => [px + a, py - h2], (w) => [px - w - a, py - h2]];
        // outwards also a bit further away (the pad lies in the ±6 px band, tiles sometimes reach up to the edge)
        const away = { b: [0, 1], t: [0, -1], l: [-1, 0], r: [1, 0] }[s] || [1, 0], k0 = cand[0];
        cand.splice(1, 0, ...[6, 12, 20].map(d => (w) => { const q = k0(w); return [q[0] + away[0] * d, q[1] + away[1] * d]; }),
          // … and outside shifted along the edge (a neighbour label takes the spot), before it moves inside next to the trace
          ...[1, -1, 2, -2].map(j => (w) => { const q = k0(w); return away[0] ? [q[0], q[1] + j * (HEIGHT + 3)] : [q[0] + j * (w / 2 + 6), q[1]]; }));
        // pad under a control (fault notice at the bottom centre, bars …): otherwise the label found no room and would vanish — it
        // moves to the control's edge, above the trace (or beside it), if need be briefly over the tile
        const cover = hard.find(o => px >= o.x0 - 2 && px <= o.x1 + 2 && py >= o.y0 - HEIGHT && py <= o.y1 + HEIGHT);
        const vd = cover && { b: (w) => [px - w / 2, cover.y0 - HEIGHT - 3], t: (w) => [px - w / 2, cover.y1 + 3], l: (w) => [cover.x1 + 3, py - h2], r: (w) => [cover.x0 - w - 3, py - h2] }[s];
        if (vd) cand.push(vd, ...[1, -1].map(j => (w) => { const q = vd(w); return away[0] ? [q[0], q[1] + j * (HEIGHT + 3)] : [q[0] + j * (w / 2 + 6), q[1]]; }));
        let pl = placeAt(variants, cand, [hard, soft, taken]);
        // tight: only the count or the shortened name, if need be over a tile — only at the board edge (the trace leaves the view).
        // Edge of a host zone: the zone itself names the target; if its label finds no free spot, it is dropped (no crowding)
        if (!pl && !e.host) pl = placeAt(variants.slice(-1), cand, [hard, taken]);
        if (!pl) continue;
        taken.push(pl.r);
        specs.push({ key: 'e:' + e.inner + '|' + e.rid, kind: 'end', side: s, r: pl.r, text: pl.text, short: pl.short, color: host.color(e.b.kind),
          g: e.g, inner: e.inner, traces: [e.b], anchor: [px, py], title: this.nodeName(e.inner),
          entries: z.map(q => ({ kind: 'peer', id: q.id, name: e.host ? t('endmarks.hostItself', { name: this.peerName(q.id) }) : this.peerName(q.id), arrow: arrowFor(q.out, q.in), weight: q.weight })) });
      }

      // 2) edge marks: traces with one end in the picture that are cut off at the view edge
      const found = [];
      // only the edges that can cut a trace count (sides that never scroll lie infinitely far away)
      const VC = { x0: this.edgeSides.includes('l') ? VR.x0 : -Infinity, y0: this.edgeSides.includes('t') ? VR.y0 : -Infinity,
        x1: this.edgeSides.includes('r') ? VR.x1 : Infinity, y1: this.edgeSides.includes('b') ? VR.y1 : Infinity };
      const edgeOf = new Map(this.ends.map(e => [e.b.id, e]));
      const filter = host.kindFilter();
      for (const b of host.traces()) {
        if (!b.pts || b.pts.length < 2) continue;
        if (filter && !filter.has(b.kind)) continue;
        const pts = b.pts.map(q => [tx(q[0]), ty(q[1])]);
        const ia = inside(pts[0][0], pts[0][1], VC) && !underUi(pts[0]), ib = inside(pts[pts.length - 1][0], pts[pts.length - 1][1], VC) && !underUi(pts[pts.length - 1]);
        if (ia === ib) continue;
        const inner = ia ? b.a : b.b, far = ia ? b.b : b.a;
        const E = exitPoint(ia ? pts : pts.slice().reverse(), VC); if (!E) continue;
        let entries;
        const re = edgeOf.get(b.id);
        if (re && far === re.rid) entries = re.peers.map(q => ({ kind: 'peer', id: q.id, name: re.host ? t('endmarks.hostItself', { name: this.peerName(q.id) }) : this.peerName(q.id), arrow: arrowFor(q.out, q.in), weight: q.weight || 1 }));
        else if (b.edgeEnd) continue;                         // edge trace without a known peer: no empty label
        else { const n = this.nodeName(far); if (!n) continue; entries = [{ kind: 'tile', nid: far, name: n, weight: 1 }]; }
        found.push({ b, inner, E, entries });
      }
      // bundle nearby exits of the same edge
      const bySide = { l: [], r: [], t: [], b: [] };
      found.forEach(f => bySide[f.E.s].push(f));
      const bundles = [];
      Object.entries(bySide).forEach(([s, list]) => {
        const across = s === 'l' || s === 'r';
        list.sort((p, q) => (across ? p.E.y - q.E.y : p.E.x - q.E.x));
        let cur = null;
        for (const f of list) {
          const pos = across ? f.E.y : f.E.x;
          if (cur && pos - cur.end <= (across ? HEIGHT + 2 : 40)) { cur.f.push(f); cur.end = pos; }
          else { cur = { s, f: [f], start: pos, end: pos }; bundles.push(cur); }
        }
      });
      bundles.forEach(bd => {
        const m = new Map();
        bd.f.forEach(f => f.entries.forEach(e => { const k = e.kind === 'tile' ? 'k:' + e.nid : 's:' + e.id; const x = m.get(k); if (x) x.weight += e.weight; else m.set(k, Object.assign({}, e)); }));
        bd.entries = [...m.values()].sort((p, q) => q.weight - p.weight || (p.name < q.name ? -1 : 1));
        bd.pos = (bd.start + bd.end) / 2;
      });
      bundles.sort((p, q) => q.entries.length - p.entries.length);
      for (const bd of bundles.slice(0, 48)) {
        const s = bd.s, across = s === 'l' || s === 'r', n = bd.entries.length, first = bd.entries[0].name;
        // tip ◂ ▸ ▴ ▾ shows where the trace continues (like in map apps) — not to be confused with the traffic arrows
        // (CSS draws the tip as a triangle, its room is in TIP)
        const variants = n === 1 ? [...steps('', first, '', TIP), '1'] : [...steps(`${t('endmarks.targets', { n })}: `, first, ' …', TIP), t('endmarks.targets', { n }), String(n)];
        // at the edge; if a control sits there (area picker, controls …), the label moves inside next to it
        const atEdge = (x, y, w, h) => {
          for (let k = 0; k < 4; k++) {
            const r = rect(x, y, w, h), o = hard.find(q => intersects(r, q)); if (!o) break;
            if (s === 'l') x = o.x1 + 2; else if (s === 'r') x = o.x0 - 2 - w;
            else x = (x + w / 2 > (o.x0 + o.x1) / 2) ? o.x1 + 2 : o.x0 - 2 - w;      // top/bottom: past the control sideways, stay at the edge
          }
          return [x, y];
        };
        const cand = [];
        for (let i = 0; i < 9; i++) {
          const d = (i % 2 ? 1 : -1) * Math.ceil(i / 2);
          if (across) cand.push((w, h) => atEdge(s === 'l' ? VR.x0 : VR.x1 - w, bd.pos - h / 2 + d * (h + GAP + 1), w, h));
          else cand.push((w, h) => atEdge(bd.pos - w / 2 + d * (w / 2 + 8), s === 't' ? VR.y0 : VR.y1 - h, w, h));
        }
        // first close to the exit without covering tiles/labels, otherwise at the edge over the tile (the label is opaque)
        const pl = placeAt(variants.slice(0, -1), cand.slice(0, 5), [hard, taken, soft], TIP) || placeAt(variants, cand, [hard, taken], TIP);
        if (!pl) continue;
        taken.push(pl.r);
        const b0 = bd.f[0].b;
        specs.push({ key: 'r:' + s + ':' + bd.f.map(f => f.b.id).sort().join(','), kind: 'edge', side: s, r: pl.r, text: pl.text, short: pl.short,
          color: host.color(b0.kind), g: 1, inner: bd.f[0].inner, traces: bd.f.map(f => f.b), anchor: null,
          title: n === 1 ? null : t('endmarks.edgeTraces', { n: bd.f.length }), entries: bd.entries });
      }
      // pad outside, tile in the picture: the trace is already covered as an edge mark above (if it crosses the edge)
      this.specs = specs; this.edgeLeft = edgeLeft.length;
      this.draw(L);
    }

    draw(L) {
      const used = new Set(), t = this.t;
      for (const sp of this.specs) {
        used.add(sp.key);
        let m = this.marks.get(sp.key);
        if (!m) {
          m = document.createElement('button'); m.type = 'button';
          m.innerHTML = `<span class="${this.cls('txt')}"></span>`;
          (sp.kind === 'end' ? this.elEnds : this.elEdges).appendChild(m);
          this.marks.set(sp.key, m);
        }
        const cls = `${this.cls('mark')} ${this.cls(sp.kind)} ll-em-s-${sp.side}${sp.short ? ' ' + this.cls('short') : ''}${sp.exit ? ' ' + this.cls('exit') : ''}`;
        if (sp.exit) {   // the plug sits where the lead touches the label
          m.style.setProperty('--ll-em-ax', Math.round(sp.anchor[0] - sp.r.x0) + 'px');
          m.style.setProperty('--ll-em-ay', Math.round(sp.anchor[1] - sp.r.y0) + 'px');
        }
        const ON = this.cls('on').split(' '), OFF = this.cls('off').split(' ');
        const strip = c => c.split(/\s+/).filter(x => x && !ON.includes(x) && !OFF.includes(x)).join(' ');
        if (strip(m.className) !== cls) { const on = m.classList.contains('ll-em-on'), off = m.classList.contains('ll-em-off'); m.className = cls; if (on) this.toggle(m, 'on', true); if (off) this.toggle(m, 'off', true); }
        const tt = m.firstChild; if (tt.textContent !== sp.text) tt.textContent = sp.text;
        m.style.transform = `translate(${sp.r.x0}px,${sp.r.y0}px)`;
        m.style.width = (sp.r.x1 - sp.r.x0) + 'px';
        if (m.style.color !== sp.color) m.style.color = sp.color;
        m.style.setProperty('--ll-em-g', sp.kind === 'end' ? (0.5 + 0.5 * Math.min(1, sp.g * 1.6)).toFixed(2) : '1');
        const target = sp.entries.length === 1 ? sp.entries[0].name : t('endmarks.targets', { n: sp.entries.length });
        m.setAttribute('aria-label', t(sp.kind === 'end' ? 'endmarks.ariaEnd' : 'endmarks.ariaEdge', { target }));
        m._spec = sp;
      }
      this.marks.forEach(m => this.toggle(m, 'hidden', false));
      const stroke = (sp, q, cls) => `<line${cls ? ` class="${cls}"` : ''} x1="${q[0].toFixed(1)}" y1="${q[1].toFixed(1)}" x2="${q[2].toFixed(1)}" y2="${q[3].toFixed(1)}" style="color:${sp.color};opacity:${sp.kind === 'end' ? (0.5 + 0.5 * Math.min(1, sp.g * 1.6)).toFixed(2) : 1}"/>`;
      const leashes = this.specs.filter(sp => sp.leash).map(sp => stroke(sp, sp.leash)).join('')
        + this.specs.filter(sp => sp.bus).map(sp => sp.bus.map(q => stroke(sp, q, this.cls('bus'))).join('')).join('');   // buses of the bundles
      if (!this.elLeashes) { this.elLeashes = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); this.elLeashes.setAttribute('class', this.cls('leashes')); this.elEnds.prepend(this.elLeashes); }
      if (this.elLeashes._h !== leashes) { this.elLeashes.innerHTML = leashes; this.elLeashes._h = leashes; }
      this.marks.forEach((m, k) => { if (!used.has(k)) { if (this._popMark === m) this.closeList(); m.remove(); this.marks.delete(k); } });
      this.elEnds.style.transform = ''; this._base = { s: L.s, x: L.x, y: L.y };
      this.focus();
    }

    // ------------------------------------------------------------ hover: labels of the own traces stand out, others step back
    focus() {
      const host = this.host, f = host.chain(), hid = host.highlighted();
      this.marks.forEach(m => {
        const sp = m._spec; if (!sp) return;
        let rel = -1;
        if (f) rel = Math.max(...sp.traces.map(b => f.group(b)));
        else if (hid) rel = sp.traces.some(b => b.a === hid || b.b === hid) ? 2 : 0;
        this.toggle(m, 'on', rel >= 1);
        this.toggle(m, 'off', rel === 0);
      });
    }

    // ------------------------------------------------------------ list (drop-down)
    over(m) {
      clearTimeout(this._popT);
      const sp = m._spec, T = this.host.tiles();
      // hovering a label shows the chain of its tile (like hovering the tile itself)
      if (sp && !this.host.selected() && T && T[sp.inner] && this.host.hover) this.host.hover(sp.inner);
      if (this._pinned && this._popMark !== m) return;
      this._popT = setTimeout(() => this.showList(m, false), 120);
    }
    leave(m) {
      clearTimeout(this._popT);
      if (!this._pinned) this._popT = setTimeout(() => { if (!this.pop.matches(':hover')) this.closeList(); }, 260);
    }
    toggleList(m, pinned) { if (!this.pop.hidden && this._popMark === m && this._pinned) this.closeList(); else this.showList(m, pinned); }
    showList(m, pinned) {
      this._popMark = m; this._pinned = !!pinned;
      this.refreshList();
      this.pop.hidden = false;
      this.listPosition();
    }
    refreshList() {
      const m = this._popMark, sp = m && m._spec; if (!sp) return;
      const t = this.t, sum = sp.entries.reduce((a, e) => a + (e.weight || 0), 0) || 1;
      const head = sp.kind === 'end'
        ? `<b>${esc(sp.title || t('endmarks.trace'))}</b><span>${sp.entries.length > 1 ? esc(t('endmarks.leadsOutPeers', { n: sp.entries.length })) : esc(t('endmarks.leadsOut'))}</span>`
        : `<b>${esc(sp.title || t('endmarks.traceAtEdge'))}</b><span>${sp.entries.length > 1 ? esc(t('endmarks.targetsOutside', { n: sp.entries.length })) : esc(t('endmarks.targetOutside'))}</span>`;
      const rows = sp.entries.slice(0, LIST_MAX).map((e, i) => {
        const goes = e.kind === 'tile' || !!this.host.canOpen(e.id);
        const share = sp.entries.length > 1 && sp.kind === 'end' ? `<small>${Math.max(1, Math.round(100 * (e.weight || 0) / sum))} %</small>` : '';
        const inner = `${e.arrow ? `<i>${e.arrow}</i>` : ''}<span class="${this.cls('name')}">${esc(e.name)}</span>${share}`;
        return goes ? `<button type="button" class="${this.cls('row')}" data-i="${i}" title="${esc(t(e.kind === 'tile' ? 'endmarks.goTo' : 'endmarks.toTarget'))}">${inner}</button>` : `<div class="${this.cls('row')} ${this.cls('row-static')}">${inner}</div>`;
      }).join('');
      const more = sp.entries.length > LIST_MAX ? `<div class="${this.cls('more')}">${esc(t('endmarks.more', { n: sp.entries.length - LIST_MAX }))}</div>` : '';
      // bundle: all sources — an entry moves to the tile
      const sources = sp.sources ? `<div class="${this.cls('more')}">${esc(t('endmarks.fromTiles', { n: sp.sources.length }))}</div><div class="${this.cls('rows')}">${sp.sources.map((q, i) => `<button type="button" class="${this.cls('row')}" data-q="${i}" title="${esc(t('endmarks.goTo'))}"><i>·</i><span class="${this.cls('name')}">${esc(q.name)}</span></button>`).join('')}</div>` : '';
      const html = `<div class="${this.cls('head')}">${head}</div><div class="${this.cls('rows')}">${rows}</div>${more}${sources}`;
      if (this.pop._html !== html) { this.pop.innerHTML = html; this.pop._html = html; }
      this.pop.style.setProperty('--ll-em-color', sp.color);
    }
    listPosition() {
      const m = this._popMark, sp = m && m._spec; if (!sp) return;
      const S = this.host.size(), W = S.W, H = S.H, pw = this.pop.offsetWidth, ph = this.pop.offsetHeight, r = sp.r;
      let x = r.x0, y = r.y1 + 6;
      if (y + ph > H - 8) y = r.y0 - ph - 6;
      if (y < 8) y = Math.max(8, Math.min(H - ph - 8, r.y0));
      if (sp.side === 'r' && sp.kind === 'edge') x = r.x1 - pw;
      x = Math.max(8, Math.min(W - pw - 8, x));
      this.pop.style.transform = `translate(${Math.round(x)}px,${Math.round(y)}px)`;
    }
    closeList() { clearTimeout(this._popT); this.pop.hidden = true; this._popMark = null; this._pinned = false; }
    // entry chosen: tile of this view → camera moves there; peer → the host opens it
    go(i) {
      const sp = this._popMark && this._popMark._spec, e = sp && sp.entries[i]; if (!e) return;
      this.closeList();
      if (e.kind === 'tile') {
        const T = this.host.tiles(), k = T && T[e.nid]; if (!k) return;
        this.host.goTo(e.nid);
        if (this.host.hover && !this.host.selected()) this.host.hover(e.nid);
        Endmarks.lastTarget = { kind: 'tile', nid: e.nid };
        return;
      }
      Endmarks.lastTarget = { kind: 'peer', id: e.id };
      if (this.host.open) this.host.open(e.id);
    }

    destroy() {
      this.dead = true; clearInterval(this._tick); clearTimeout(this._lt); clearTimeout(this._popT);
      document.removeEventListener('pointerdown', this._outside, true);
      if (document.fonts && document.fonts.removeEventListener) document.fonts.removeEventListener('loadingdone', this._fontsT);
      this.el.remove(); this.pop.remove(); this.marks.clear();
    }
    // state for tests/measurements
    state() {
      return this.specs.map(sp => ({ key: sp.key, kind: sp.kind, side: sp.side, text: sp.text, short: sp.short, inner: sp.inner, x0: sp.r.x0, y0: sp.r.y0, x1: sp.r.x1, y1: sp.r.y1,
        anchor: sp.anchor, exit: !!sp.exit, along: !!sp.along, leash: sp.leash || null, bundle: sp.bundle || 0, bus: sp.bus || null, sources: sp.sources ? sp.sources.map(q => q.nid) : null, dir: sp.dir || sp.side, entries: sp.entries.map(e => ({ kind: e.kind, id: e.id || null, nid: e.nid || null, name: e.name, arrow: e.arrow || null })),
        traces: sp.traces.map(b => b.id) }));
    }
  }

  const api = {
    API, STRINGS,
    create: (host, options) => new Endmarks(host, options),
    Endmarks,
    lastTarget: null,
    clearMeasure: () => measured.clear(),
    // pure helpers (tests)
    _internals: { bundleLead, exitPoint, arrowFor, fontFor, makePlacer, steps, fitting, setMeasure: f => { measureHook = f; measured.clear(); }, height: () => HEIGHT, setFont: s => { fs = s; HEIGHT = s + 7; } }
  };
  // lastTarget lives on the class (set from instances) and is mirrored on the namespace
  Object.defineProperty(Endmarks, 'lastTarget', { get: () => api.lastTarget, set: v => { api.lastTarget = v; } });
  LL.Endmarks = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof self !== 'undefined' ? self : this);
