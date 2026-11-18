/* Lanloom — firewall insight: the "firewall board" of a zone firewall read by an adapter. Read only, nothing here changes anything.
   - LL.plainText(msg)        a message {key, vars, notes} of the server as text (vars may contain messages)
   - LL.fwInsightCards(view, insight, ctx) -> cards for the firewall tab:
       Board    three tiles (blocked today, rules, checks) and the zone board (pads, lines with direction, hits of the last hour)
       Ask      "May A reach B?" answered from the read rules, with the deciding rule in plain words
       Checks   what the rules and the adapter found, each with what you can do
   The geometry and the small decisions are plain functions (exported for node, tests/js/fwinsight_test.js). */
(function (root) {
  'use strict';

  // ------------------------------------------------------------------ pure helpers (also run under Node)
  const r1 = x => Math.round(x * 10) / 10;
  const pairKey = (a, b) => a + '>' + b;

  // a server message as text; `tr` is the translator t(key, vars). Variables that are messages themselves are resolved first.
  function plainText(m, tr) {
    if (m == null) return '';
    if (typeof m === 'string' || typeof m === 'number') return String(m);
    if (Array.isArray(m)) return m.map(x => plainText(x, tr)).join(', ');
    if (typeof m !== 'object' || !m.key) return '';
    const vars = {};
    Object.keys(m.vars || {}).forEach(k => {
      const v = m.vars[k];
      vars[k] = v && typeof v === 'object' ? plainText(v, tr) : v;
    });
    return tr(m.key, vars);
  }
  function plainNotes(m, tr) {
    return m && Array.isArray(m.notes) ? m.notes.map(n => plainText(n, tr)).filter(Boolean) : [];
  }

  // size of the board and the centre of every pad. Up to 8 zones sit on an ellipse, up to 12 on two rings (outer and inner,
  // the inner pads between the outer ones), more go on a grid. `narrow` is the phone layout: smaller pads in two columns,
  // one row after the other (a ring does not fit 380 units).
  function boardLayout(n, narrow) {
    const pad = narrow ? { w: 120, h: 46 } : { w: 158, h: 68 };
    const W = narrow ? 380 : 960;
    let H = narrow ? 0 : (n <= 8 ? 500 : 560);
    const cx = W / 2;
    const pos = [];
    if (narrow) {
      const rows = Math.max(1, Math.ceil(n / 2));
      const step = pad.h + 44;
      H = rows * step - 12 + 20 - 6;
      for (let i = 0; i < n; i++) pos.push({ x: n === 1 ? cx : (i % 2 ? W - pad.w / 2 - 8 : pad.w / 2 + 8), y: r1(10 + step / 2 + Math.floor(i / 2) * step - 12) });
      return { w: W, h: H, pad, pos };
    }
    const ring = (count, rx, ry, cy, offset) => {
      for (let i = 0; i < count; i++) {
        const a = -Math.PI / 2 + (i + offset) * 2 * Math.PI / count;
        pos.push({ x: r1(cx + rx * Math.cos(a)), y: r1(cy + ry * Math.sin(a)) });
      }
    };
    if (n <= 0) return { w: W, h: H, pad, pos };
    if (n === 1) { pos.push({ x: cx, y: H / 2 }); return { w: W, h: H, pad, pos }; }
    const margin = 14;
    const rx = W / 2 - pad.w / 2 - margin, ry = H / 2 - pad.h / 2 - margin;
    if (n <= 8) ring(n, rx, ry, H / 2, 0.5);
    else if (n <= 12) {
      const outer = Math.ceil(n * 0.6), inner = n - outer;
      ring(outer, rx, ry, H / 2, 0.5);
      ring(inner, narrow ? rx * 0.42 : rx * 0.5, narrow ? ry * 0.46 : ry * 0.52, H / 2, 0.5);
    } else {
      const cols = Math.max(2, Math.min(Math.ceil(Math.sqrt(n * (W / H) * 1.1)), Math.floor((W - 2 * margin + 10) / (pad.w + 10))));
      const rows = Math.ceil(n / cols);
      const gx = (W - 2 * margin - pad.w) / Math.max(1, cols - 1), gy = (H - 2 * margin - pad.h) / Math.max(1, rows - 1);
      H = Math.max(H, rows * (pad.h + 36) + 2 * margin);
      for (let i = 0; i < n; i++) pos.push({ x: r1(margin + pad.w / 2 + (i % cols) * gx), y: r1(margin + pad.h / 2 + Math.floor(i / cols) * Math.max(gy, pad.h + 36)) });
    }
    return { w: W, h: H, pad, pos };
  }

  // where a ray from the centre of a pad towards `to` leaves the pad (rectangle of size `size`), `gap` px further out
  function rectEdgePoint(c, size, to, gap) {
    const dx = to.x - c.x, dy = to.y - c.y;
    if (!dx && !dy) return { x: c.x, y: c.y };
    const len = Math.hypot(dx, dy) || 1;
    const sx = Math.abs(dx) > 1e-9 ? (size.w / 2) / Math.abs(dx) : Infinity;
    const sy = Math.abs(dy) > 1e-9 ? (size.h / 2) / Math.abs(dy) : Infinity;
    const k = Math.min(sx, sy);
    const ux = dx / len, uy = dy / len;
    return { x: c.x + dx * k + ux * (gap || 0), y: c.y + dy * k + uy * (gap || 0) };
  }

  // curve from pad a to pad b, bent to the right of its direction, so a line there and the one back never lie on each other.
  // Returns the path, the arrowhead (triangle) at b, the middle of the curve (for a label) and the end points.
  function edgeGeometry(a, b, size, bend) {
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const amount = bend == null ? Math.min(34, Math.max(14, len * 0.12)) : bend;
    const ctl = { x: (a.x + b.x) / 2 - uy * amount, y: (a.y + b.y) / 2 + ux * amount };
    const s = rectEdgePoint(a, size, ctl, 2);
    const e = rectEdgePoint(b, size, ctl, 3);
    const tx = e.x - ctl.x, ty = e.y - ctl.y;
    const tl = Math.hypot(tx, ty) || 1;
    const dir = { x: tx / tl, y: ty / tl };
    const AH = 8, AW = 4.2;
    const base = { x: e.x - dir.x * AH, y: e.y - dir.y * AH };
    const arrow = [e, { x: base.x - dir.y * AW, y: base.y + dir.x * AW }, { x: base.x + dir.y * AW, y: base.y - dir.x * AW }].map(p => ({ x: r1(p.x), y: r1(p.y) }));
    const mid = { x: r1(0.25 * s.x + 0.5 * ctl.x + 0.25 * e.x), y: r1(0.25 * s.y + 0.5 * ctl.y + 0.25 * e.y) };
    return { d: 'M' + r1(s.x) + ' ' + r1(s.y) + ' Q' + r1(ctl.x) + ' ' + r1(ctl.y) + ' ' + r1(base.x) + ' ' + r1(base.y), arrow, mid, start: s, end: e, ctl };
  }

  // a point of the curve of edgeGeometry(): t = 0 is the start, 1 the end (before the arrowhead is added)
  function curvePoint(geo, t) {
    const u = 1 - t;
    return { x: r1(u * u * geo.start.x + 2 * u * t * geo.ctl.x + t * t * geo.end.x), y: r1(u * u * geo.start.y + 2 * u * t * geo.ctl.y + t * t * geo.end.y) };
  }
  const overlap = (a, b, m) => !(a.x + a.w / 2 + m <= b.x - b.w / 2 || b.x + b.w / 2 + m <= a.x - a.w / 2 || a.y + a.h / 2 + m <= b.y - b.h / 2 || b.y + b.h / 2 + m <= a.y - a.h / 2);
  // where the number of a line goes: the middle of the curve, or the nearest spot along it that touches no pad and no other number
  // (null when there is none).
  // `box` is {w, h} of the number, `taken` boxes {x, y, w, h} (centres) already placed, `pads` the same for the pads.
  function placeLabel(geo, box, taken, pads) {
    for (const t of [0.5, 0.42, 0.58, 0.34, 0.66, 0.27, 0.73, 0.2, 0.8]) {
      const c = curvePoint(geo, t);
      const b = { x: c.x, y: c.y, w: box.w, h: box.h };
      if (!taken.some(o => overlap(b, o, 2)) && !pads.some(o => overlap(b, o, 2))) return c;
    }
    return null;                                         // no free spot: the list below has the number
  }

  // line width from hits in the last hour: log scale between `lo` and `hi`, `max` hits (the busiest pair) is the widest
  function hitWidth(n, max, lo, hi) {
    lo = lo == null ? 1.4 : lo; hi = hi == null ? 4.4 : hi;
    if (!(n > 0) || !(max > 0)) return lo;
    return r1(lo + (hi - lo) * Math.min(1, Math.log10(1 + n) / Math.log10(1 + max)));
  }

  // how a zone pair is drawn: 'allow' (a solid line: the pair may talk, or an allow rule was hit) and/or 'block' (red dashed:
  // blocked traffic seen in the last hour); a pair that may not talk and was never hit only shows in the list
  function edgeKinds(p) {
    const out = [];
    if (p.allow || p.hits > 0) out.push('allow');
    if (p.blocked > 0) out.push('block');
    return out;
  }

  function summarize(findings) {
    const order = { alert: 0, warn: 1, info: 2 };
    const list = findings || [];
    let worst = null;
    list.forEach(f => { const l = order[f.level] == null ? 'info' : f.level; if (worst == null || order[l] < order[worst]) worst = l; });
    return { total: list.length, worst };
  }
  const sortFindings = list => (list || []).slice().sort((a, b) => ((({ alert: 0, warn: 1, info: 2 })[a.level] ?? 3) - (({ alert: 0, warn: 1, info: 2 })[b.level] ?? 3)));

  // a port as typed: '' means none, a whole number 1..65535 is valid, everything else is not
  function parsePort(text) {
    const s = String(text == null ? '' : text).trim();
    if (!s) return { ok: true, port: null };
    if (!/^\d{1,5}$/.test(s)) return { ok: false, port: null };
    const n = parseInt(s, 10);
    return n >= 1 && n <= 65535 ? { ok: true, port: n } : { ok: false, port: null };
  }
  function askQuery(source, sel) {
    const q = [];
    if (source) q.push('source=' + encodeURIComponent(source));
    q.push('from=' + encodeURIComponent(sel.from), 'to=' + encodeURIComponent(sel.to));
    if (sel.proto === 'tcp' || sel.proto === 'udp') q.push('proto=' + sel.proto);
    const p = parsePort(sel.port);
    if (p.port) q.push('port=' + p.port);
    return '/api/firewall/ask?' + q.join('&');
  }

  const PURE = { plainText, plainNotes, boardLayout, rectEdgePoint, edgeGeometry, hitWidth, curvePoint, placeLabel, edgeKinds, summarize, sortFindings, parsePort, askQuery, pairKey, version: '1.0.0' };
  if (typeof module !== 'undefined' && module.exports) module.exports = PURE;

  // ------------------------------------------------------------------ browser part
  if (typeof window === 'undefined' || !window.Lanloom || !window.Lanloom.h) return;
  const LL = window.Lanloom;
  const { h, t } = LL;
  LL.fwInsight = PURE;
  LL.plainText = m => plainText(m, t);

  const SVGNS = 'http://www.w3.org/2000/svg';
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(k => {
      const v = attrs[k];
      if (v == null || v === false) return;
      if (k === 'class') el.setAttribute('class', v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    });
    kids.flat(Infinity).forEach(kid => { if (kid != null && kid !== false) el.appendChild(typeof kid === 'string' || typeof kid === 'number' ? document.createTextNode(String(kid)) : kid); });
    return el;
  }
  const fmtCompact = n => { try { return new Intl.NumberFormat(LL.locale(), { notation: 'compact', maximumFractionDigits: 1 }).format(n); } catch (e) { return String(n); } };
  const clock = ts => { try { return new Date(ts * 1000).toLocaleTimeString(LL.locale(), { hour: '2-digit', minute: '2-digit', hour12: false }); } catch (e) { return ''; } };
  const LEVEL = { alert: 'danger', warn: 'warn', info: 'muted' };
  const plain = m => plainText(m, t);
  const cut = (txt, n) => (txt.length > n ? txt.slice(0, n - 1) + '…' : txt);

  // phone layout flips with the width; boards that left the page are forgotten when it happens
  const narrowQ = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 640px)') : null;
  const redraws = new Set();
  if (narrowQ && narrowQ.addEventListener) narrowQ.addEventListener('change', () => redraws.forEach(fn => { if (!fn()) redraws.delete(fn); }));
  const isNarrow = () => !!(narrowQ && narrowQ.matches);

  // pick (or drop) a zone pair; the tab is drawn anew, so the keyboard goes to the note that tells what was picked
  function choose(ctx, key) {
    ctx.pick(key);
    const el = document.querySelector('.fwi-picked button') || document.getElementById('fw-board-insight');
    if (el) el.focus();
  }

  function card(id, title, lead, cls) {
    return h('section', { class: 'card fw-extra fwi-card' + (cls ? ' ' + cls : ''), id },
      h('h2', {}, title), lead ? h('p', { class: 'muted fw-hint' }, lead) : null);
  }

  // ------------------------------------------------------------------ board card
  function tile(kind, label, value, sub, onclick, aria) {
    const body = [h('span', { class: 'fwi-tile__label' }, label), h('b', { class: 'fwi-tile__value' }, value), h('span', { class: 'fwi-tile__sub' }, sub)];
    return onclick
      ? h('button', { type: 'button', class: 'fwi-tile fwi-tile--' + kind + ' is-btn', onclick, 'aria-label': aria }, ...body)
      : h('div', { class: 'fwi-tile fwi-tile--' + kind }, ...body);
  }

  function tiles(view, insight) {
    const hits = insight.hits || {};
    const pols = view.state.policies || [];
    const own = pols.filter(p => !p.builtin).length;
    const sum = summarize(insight.findings);
    const blocked = hits.blocked_today;
    const toChecks = sum.total ? () => { const el = document.getElementById('fw-checks'); if (el) { el.scrollIntoView({ block: 'start', behavior: 'smooth' }); el.focus({ preventScroll: true }); } } : null;
    return h('div', { class: 'fwi-tiles' },
      tile('blocked', t('fwi.tile.blocked'), blocked == null ? '—' : LL.fmtNum(blocked, 0),
        blocked == null ? t('fwi.tile.no_counters') : hits.since ? t('fwi.tile.since', { time: clock(hits.since) }) : t('fwi.tile.today')),
      tile('rules', t('fwi.tile.rules'), LL.fmtNum(own, 0), t('fwi.tile.rules_sub', { total: pols.length })),
      tile('checks' + (sum.worst ? ' fwi-tile--' + sum.worst : ' fwi-tile--ok'), t('fwi.tile.checks'), LL.fmtNum(sum.total, 0),
        sum.total ? t('fwi.tile.checks_worst', { level: t('hv.level.' + sum.worst) }) : t('fwi.tile.checks_none'),
        toChecks, t('fwi.tile.checks_open', { n: sum.total })));
  }

  function topRow(insight) {
    const top = ((insight.hits || {}).top || []).filter(x => x.n > 0).slice(0, 3);
    if (!top.length) return null;
    return h('div', { class: 'fwi-top' }, h('span', { class: 'fwi-top__label muted' }, t('fwi.top.label')),
      h('ul', { class: 'fwi-top__list' }, top.map(x => h('li', { class: 'chip chip--muted', title: x.name }, h('span', { class: 'fwi-top__name' }, x.name), h('b', {}, fmtCompact(x.n))))));
  }

  // the drawing: pads (zones) on a ring, curved lines with arrowheads (zone pair, direction)
  function boardSvg(insight, ctx, local, narrow) {
    const zones = insight.board.zones, pairs = insight.board.pairs;
    const lay = boardLayout(zones.length, narrow);
    const pos = {};
    zones.forEach((z, i) => { pos[z.id] = lay.pos[i]; });
    const name = id => (zones.find(z => z.id === id) || {}).name || '?';
    const hourOk = !!(insight.hits || {}).hour_ok;
    const maxHits = Math.max(0, ...pairs.map(p => p.hits || 0), ...pairs.map(p => p.blocked || 0));
    const svg = s('svg', { class: 'fwi-svg', viewBox: '0 0 ' + lay.w + ' ' + lay.h, role: 'group', 'aria-label': t('fwi.board.aria', { zones: zones.length }), preserveAspectRatio: 'xMidYMid meet' });
    const gEdges = s('g', { class: 'fwi-edges', 'aria-hidden': 'true' });       // the list below is the text alternative of the lines
    const gLabels = s('g', { class: 'fwi-labels', 'aria-hidden': 'true' });
    const gPads = s('g', { class: 'fwi-pads' });
    svg.append(gEdges, gPads, gLabels);
    const drawn = [];
    pairs.filter(p => pos[p.from] && pos[p.to]).forEach(p => edgeKinds(p).forEach(kind => drawn.push({ p, k: pairKey(p.from, p.to), kind, both: kind === 'block' && edgeKinds(p).length > 1 })));
    // the busiest pairs get a number on their line (and the picked one); a number on every line would be noise
    const busy = new Set(drawn.filter(e => (e.p.hits || e.p.blocked) && hourOk).sort((a, b) => ((b.p.hits + b.p.blocked) - (a.p.hits + a.p.blocked))).slice(0, narrow ? 3 : 6).map(e => e.k));
    const padBoxes = zones.map((z, i) => ({ x: lay.pos[i].x, y: lay.pos[i].y, w: lay.pad.w, h: lay.pad.h }));
    const taken = [];
    const rank = e => (e.k === ctx.pair ? 4 : e.kind === 'block' ? 3 : e.p.hits > 0 || e.p.own > 0 ? 2 : 1);
    drawn.sort((a, b) => rank(a) - rank(b));
    const focus = local.zone;                     // a pad was chosen: only its lines stay strong
    drawn.forEach(e => {
      const p = e.p;
      const geo = edgeGeometry(pos[p.from], pos[p.to], lay.pad, e.both ? 48 : null);
      const picked = e.k === ctx.pair;
      const w = e.kind === 'block' ? hitWidth(p.blocked, maxHits, 1.6, 3.6) : r1(hitWidth(hourOk ? p.hits : 0, maxHits) + (p.own > 0 ? 1.1 : 0));
      const dim = (focus && p.from !== focus && p.to !== focus) || (ctx.pair && !picked);
      const quiet = e.kind === 'allow' && !(p.hits > 0) && !(p.own > 0);
      const cls = 'fwi-edge fwi-edge--' + e.kind + (quiet ? ' is-quiet' : '') + (p.own > 0 ? ' is-own' : '') + (picked ? ' is-picked' : '') + (dim ? ' is-dim' : '');
      const label = pairText(name(p.from), name(p.to), p, hourOk);
      const g = s('g', { class: cls, onclick: () => choose(ctx, e.k) },
        s('title', {}, label),
        s('path', { class: 'fwi-edge__hit', d: geo.d, fill: 'none' }),
        s('path', { class: 'fwi-edge__line', d: geo.d, fill: 'none', 'stroke-width': picked ? w + 1.2 : w }),
        s('polygon', { class: 'fwi-edge__arrow', points: geo.arrow.map(q => q.x + ',' + q.y).join(' ') }));
      gEdges.appendChild(g);
      if ((busy.has(e.k) || picked) && (p.hits || p.blocked) && hourOk) {
        const txt = fmtCompact(e.kind === 'block' ? p.blocked : p.hits);
        const tw = txt.length * 6.2 + 10;
        const at = placeLabel(geo, { w: tw, h: 16 }, taken, padBoxes);
        if (at) taken.push({ x: at.x, y: at.y, w: tw, h: 16 });
        if (at) gLabels.appendChild(s('g', { class: 'fwi-label' + (e.kind === 'block' ? ' is-block' : '') + (dim ? ' is-dim' : ''), transform: 'translate(' + at.x + ' ' + at.y + ')', 'aria-hidden': 'true' },
          s('rect', { x: r1(-tw / 2), y: -8, width: r1(tw), height: 16, rx: 8 }), s('text', { x: 0, y: 3.6, 'text-anchor': 'middle' }, txt)));
      }
    });
    zones.forEach((z, i) => {
      const c = lay.pos[i];
      const sel = focus === z.id;
      const x = r1(c.x - lay.pad.w / 2), y = r1(c.y - lay.pad.h / 2);
      const dimPad = focus && !sel && !drawn.some(e => (e.p.from === focus && e.p.to === z.id) || (e.p.to === focus && e.p.from === z.id));
      const nets = (z.networks || []).join(', ');
      const chip = z.internet ? t('fw.zone.internet') : t('fw.zone.no_internet');
      const pad = s('g', { 'data-zone': z.id, class: 'fwi-pad' + (sel ? ' is-sel' : '') + (dimPad ? ' is-dim' : '') + (z.internet ? ' is-net' : ''), tabindex: '0', role: 'button', 'aria-pressed': String(sel),
        'aria-label': t(focus && !sel ? 'fwi.pad.pick_to' : 'fwi.pad.pick', { zone: z.name, internet: chip, networks: nets || t('fwi.pad.no_networks') }),
        transform: 'translate(' + x + ' ' + y + ')', onclick: () => padClick(z.id),
        onkeydown: ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); padClick(z.id); } } },
        s('title', {}, z.name + (nets ? ' · ' + nets : '') + ' · ' + chip),
        s('rect', { class: 'fwi-pad__box', width: lay.pad.w, height: lay.pad.h, rx: 12 }),
        s('text', { class: 'fwi-pad__name', x: narrow ? 10 : 14, y: narrow ? 19 : 25 }, cut(z.name, narrow ? 15 : 20)),
        narrow ? s('text', { class: 'fwi-pad__sub', x: 10, y: 36 }, chip)
          : [s('text', { class: 'fwi-pad__sub', x: 14, y: 42 }, cut(nets || t('fwi.pad.no_networks'), 25)),
            s('rect', { class: 'fwi-pad__chip', x: 14, y: 49, width: chip.length * 5.4 + 14, height: 13, rx: 6.5 }),
            s('text', { class: 'fwi-pad__chiptext', x: 21, y: 58.6 }, chip)]);
      gPads.appendChild(pad);
    });
    function padClick(id) {
      if (!local.zone) { local.zone = id; redraw(id); return; }
      if (local.zone === id) { local.zone = null; redraw(id); return; }
      const from = local.zone;
      local.zone = null;
      choose(ctx, pairKey(from, id));
    }
    let wrap = null;
    function redraw(keep) {
      if (!wrap || !wrap.isConnected) return;
      const next = boardSvg(insight, ctx, local, isNarrow());
      next._bind(wrap);
      wrap.replaceChildren(next);
      const status = wrap.parentNode && wrap.parentNode.querySelector('.fwi-status');
      if (status) status.textContent = local.zone ? t('fwi.board.pick_second', { zone: name(local.zone) }) : '';
      const again = keep && next.querySelector('[data-zone="' + keep + '"]');
      if (again) again.focus();                       // the pads were drawn anew: the keyboard stays where it was
    }
    return Object.assign(svg, { _bind: w => { wrap = w; } });
  }

  function pairText(from, to, p, hourOk) {
    const bits = [from + ' → ' + to + ': ' + t(p.allow ? 'fw.pol.allow' : 'fw.pol.block')];
    if (hourOk && p.hits > 0) bits.push(t('fwi.rule.hour', { n: p.hits }));
    if (hourOk && p.blocked > 0) bits.push(t('fwi.pair.blocked_n', { n: p.blocked }));
    if (p.own > 0) bits.push(t('fwi.pair.own_n', { n: p.own }));
    return bits.join(' · ');
  }

  function pairList(insight, ctx) {
    const zones = insight.board.zones;
    const name = id => (zones.find(z => z.id === id) || {}).name || '?';
    const hourOk = !!(insight.hits || {}).hour_ok;
    const rows = insight.board.pairs.slice().sort((a, b) => ((b.hits + b.blocked) - (a.hits + a.blocked)) || (zones.findIndex(z => z.id === a.from) - zones.findIndex(z => z.id === b.from)));
    return h('details', { class: 'fwi-list' },
      h('summary', {}, t('fwi.list.show', { n: rows.length })),
      h('div', { class: 'fw-table-wrap' }, h('table', { class: 'fw-table fwi-table' },
        h('caption', { class: 'sr-only' }, t('fwi.list.caption')),
        h('thead', {}, h('tr', {}, [t('fwi.col.from'), t('fwi.col.to'), t('fwi.col.state'), t('fwi.col.hits'), t('fwi.col.blocked'), t('fwi.col.own'), ''].map(c => h('th', { scope: 'col' }, c)))),
        h('tbody', {}, rows.map(p => {
          const k = pairKey(p.from, p.to);
          return h('tr', { class: ctx.pair === k ? 'is-picked' : null },
            h('th', { scope: 'row' }, name(p.from)), h('td', {}, name(p.to)),
            h('td', {}, h('span', { class: 'chip ' + (p.allow ? 'chip--ok' : 'chip--muted') }, t(p.allow ? 'fw.pol.allow' : 'fw.pol.block'))),
            h('td', { class: 'fwi-num' }, hourOk ? LL.fmtNum(p.hits, 0) : '—'), h('td', { class: 'fwi-num' }, hourOk ? LL.fmtNum(p.blocked, 0) : '—'),
            h('td', { class: 'fwi-num' }, LL.fmtNum(p.own, 0)),
            h('td', {}, h('button', { type: 'button', class: 'btn btn--ghost btn--sm', 'aria-pressed': String(ctx.pair === k), 'aria-label': t('fwi.list.pick', { from: name(p.from), to: name(p.to) }), onclick: () => choose(ctx, k) },
              t(ctx.pair === k ? 'fwi.list.picked' : 'fwi.list.rules'))));
        })))));
  }

  function legend() {
    const sw = (cls, label) => h('li', { class: 'fwi-key' }, (() => { const x = s('svg', { class: 'fwi-key__sw', viewBox: '0 0 34 10', 'aria-hidden': 'true' }, s('line', { class: 'fwi-key__line ' + cls, x1: 1, y1: 5, x2: 33, y2: 5 })); return x; })(), h('span', {}, label));
    return h('ul', { class: 'fwi-legend', 'aria-label': t('fwi.legend.label') },
      sw('is-allow', t('fwi.legend.allow')), sw('is-own', t('fwi.legend.own')), sw('is-block', t('fwi.legend.block')));
  }

  function boardCard(view, insight, ctx) {
    const zones = (insight.board && insight.board.zones) || [];
    const out = card('fw-board-insight', t('fwi.board.title'), t('fwi.board.lead'));
    out.setAttribute('tabindex', '-1');
    out.append(...[tiles(view, insight), topRow(insight)].filter(Boolean));
    if (zones.length >= 2) {
      const local = { zone: null };
      const canvas = h('div', { class: 'fwi-canvas' });
      const stage = h('div', { class: 'fwi-stage' }, canvas, h('p', { class: 'fwi-status', role: 'status' }));
      const build = () => { const svg = boardSvg(insight, ctx, local, isNarrow()); canvas.replaceChildren(svg); svg._bind(canvas); };
      build();
      const redo = () => { if (!stage.isConnected) return false; build(); return true; };
      redraws.add(redo);
      const hits = insight.hits || {};
      const hint = hits.blocked_today == null ? t('fwi.board.no_counters') : !hits.hour_ok ? t('fwi.board.hour_wait') : null;
      const zoneName = id => (zones.find(z => z.id === id) || {}).name || '?';
      const picked = ctx.pair ? ctx.pair.split('>') : null;
      out.append(...[stage, legend(),
        h('p', { class: 'muted fw-hint fwi-hint' }, t('fwi.board.help')),
        hint ? h('p', { class: 'muted fw-hint fwi-hint' }, hint) : null,
        picked ? h('div', { class: 'fwi-picked callout' }, h('span', {}, t('fwi.board.picked', { from: zoneName(picked[0]), to: zoneName(picked[1]) })),
          h('span', { class: 'fwi-picked__tools' },
            h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => { const el = document.querySelector('.fw-pol'); if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' }); } }, t('fwi.board.jump')),
            h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => choose(ctx, ctx.pair) }, t('fw.pol.all_pairs')))) : null,
        pairList(insight, ctx)].filter(Boolean));
    }
    return out;
  }

  // ------------------------------------------------------------------ "May A reach B?"
  const askStore = {};
  function askFor(source) {
    if (!askStore[source]) {
      let saved = {};
      try { saved = JSON.parse(LL.prefs.get('ll-fw-ask', '{}')) || {}; } catch (e) { saved = {}; }
      askStore[source] = { from: saved.from || '', to: saved.to || '', proto: saved.proto || '', port: saved.port || '', res: null, err: null, at: 0, seq: 0 };
    }
    return askStore[source];
  }
  const saveAsk = a => LL.prefs.set('ll-fw-ask', JSON.stringify({ from: a.from, to: a.to, proto: a.proto, port: a.port }));

  function askOptions(state) {
    const zones = (state.zones || []).map(z => ({ v: 'zone:' + z.id, l: z.name }));
    const nets = (state.networks || []).filter(n => n.purpose !== 'vpn').map(n => ({ v: n.id, l: n.name + (n.cidr ? ' · ' + n.cidr : '') }));
    const devs = (state.devices || []).filter(d => d.ip).slice().sort((a, b) => String(a.name).localeCompare(String(b.name))).map(d => ({ v: d.id, l: d.name + ' · ' + d.ip }));
    return { zones, nets, devs };
  }
  function selectOf(opts, value, label, id) {
    const g = (title, list) => (list.length ? h('optgroup', { label: title }, list.map(o => h('option', { value: o.v, selected: o.v === value }, o.l))) : null);
    const sel = h('select', { class: 'select', id },
      h('option', { value: '' }, t('fwi.ask.choose')),
      h('option', { value: 'internet', selected: value === 'internet' }, t('fwi.ask.internet')),
      g(t('fwi.ask.g.zones'), opts.zones), g(t('fwi.ask.g.networks'), opts.nets), g(t('fwi.ask.g.devices'), opts.devs));
    sel.value = value;
    if (sel.value !== value) sel.value = '';
    return LL.field(label, sel);
  }

  function resultBlock(a) {
    const r = a.res;
    if (a.err) return h('p', { class: 'msg msg--error' }, a.err);
    if (!r) return h('p', { class: 'muted fwi-ask__idle' }, t('fwi.ask.idle'));
    const v = ['allow', 'block', 'reject', 'depends'].includes(r.verdict) ? r.verdict : 'depends';
    const route = [plain(r.src && r.src.label), plain(r.dst && r.dst.label)].join(' → ') + (r.proto ? ' · ' + r.proto.toUpperCase() : '') + (r.port ? ' ' + r.port : '');
    const maybe = r.maybe || [];
    return h('div', { class: 'fwi-ask__res' },
      h('div', { class: 'fwi-verdict fwi-verdict--' + v },
        h('span', { class: 'fwi-verdict__ico', 'aria-hidden': 'true' }, v === 'allow' ? '✓' : v === 'depends' ? '?' : '✕'),
        h('span', { class: 'fwi-verdict__text' }, plain(r.verdict_label) || t('fwi.verdict.' + v))),
      h('div', { class: 'fwi-ask__why' },
        h('div', { class: 'fwi-ask__route mono' }, route),
        h('p', { class: 'fwi-ask__reason' }, plain(r.reason)),
        r.rule ? h('div', { class: 'fwi-rulebox' },
          h('div', { class: 'fwi-rulebox__label muted' }, t(r.rule.builtin ? 'fwi.ask.rule_builtin' : 'fwi.ask.rule')),
          h('div', { class: 'fwi-rulebox__name' }, r.rule.name),
          h('div', { class: 'fwi-rulebox__plain' }, plain(r.rule.plain)),
          plainNotes(r.rule.plain, t).map(n => h('div', { class: 'fwi-note muted' }, n))) : null,
        !r.certain ? h('div', { class: 'fwi-maybe' },
          h('p', { class: 'muted fwi-maybe__hint' }, t('fwi.ask.uncertain')),
          maybe.length ? h('ul', { class: 'fwi-maybe__list' }, maybe.map(m => h('li', { class: 'fwi-maybe__row' },
            h('span', { class: 'fw-rule__act fw-rule__act--' + (['allow', 'block', 'reject'].includes(m.verdict) ? m.verdict : 'other') }, t('fw.pol.action.' + (['allow', 'block', 'reject'].includes(m.verdict) ? m.verdict : 'other'))),
            h('div', { class: 'fwi-maybe__main' }, h('strong', {}, m.name), h('div', {}, plain(m.plain)), h('div', { class: 'muted fwi-note' }, plain(m.why)))))) : null) : null));
  }

  function askCard(view, insight, ctx) {
    const state = view.state;
    const a = askFor(ctx.source);
    const opts = askOptions(state);
    const valid = new Set(['internet'].concat(opts.zones.map(o => o.v), opts.nets.map(o => o.v), opts.devs.map(o => o.v)));
    if (!valid.has(a.from)) a.from = '';
    if (!valid.has(a.to)) a.to = '';
    const out = card('fw-ask', t('fwi.ask.title'), t('fwi.ask.lead'));
    const uid = 'fwi-' + Math.random().toString(36).slice(2, 7);
    const from = selectOf(opts, a.from, t('fwi.ask.from'), uid + '-from');
    const to = selectOf(opts, a.to, t('fwi.ask.to'), uid + '-to');
    const proto = h('select', { class: 'select', id: uid + '-proto' }, [['', t('fwi.ask.proto_any')], ['tcp', 'TCP'], ['udp', 'UDP']].map(o => h('option', { value: o[0], selected: o[0] === a.proto }, o[1])));
    proto.value = a.proto;
    const port = h('input', { class: 'input mono', id: uid + '-port', type: 'text', inputmode: 'numeric', autocomplete: 'off', maxlength: '5', value: a.port, placeholder: t('fwi.ask.port_ph') });
    const portMsg = h('div', { class: 'field__hint fwi-portmsg', id: uid + '-pm', 'aria-live': 'polite' });
    port.setAttribute('aria-describedby', uid + '-pm');
    const result = h('div', { class: 'fwi-ask__out', role: 'status', 'aria-live': 'polite' }, resultBlock(a));
    let timer = null;
    const swap = h('button', { type: 'button', class: 'icon-btn fwi-ask__swap', 'aria-label': t('fwi.ask.swap'), title: t('fwi.ask.swap'), onclick: () => {
      const f = from.querySelector('select'), tt = to.querySelector('select');
      const x = f.value; f.value = tt.value; tt.value = x; changed(true);
    } }, '⇄');

    async function run() {
      const p = parsePort(a.port);
      portMsg.textContent = p.ok ? '' : t('fwi.ask.port_bad');
      if (!p.ok) { a.res = null; a.err = null; result.replaceChildren(resultBlock(a)); return; }
      if (!a.from || !a.to) { a.res = null; a.err = null; result.replaceChildren(resultBlock(a)); return; }
      const seq = ++a.seq;
      result.classList.add('is-busy');
      try {
        const r = await LL.api(askQuery(ctx.source, a));
        if (seq !== a.seq) return;
        a.res = r; a.err = null; a.at = Date.now();
      } catch (e) {
        if (seq !== a.seq) return;
        a.res = null; a.err = e && e.key && LL.strings[e.key] ? LL.errText(e) : t('fwi.ask.failed');
      }
      result.classList.remove('is-busy');
      result.replaceChildren(resultBlock(a));
    }
    function changed(now) {
      a.from = from.querySelector('select').value; a.to = to.querySelector('select').value; a.proto = proto.value; a.port = port.value.trim();
      saveAsk(a);
      clearTimeout(timer);
      timer = setTimeout(run, now ? 0 : 300);
    }
    [from.querySelector('select'), to.querySelector('select'), proto].forEach(el => el.addEventListener('change', () => changed(true)));
    port.addEventListener('input', () => changed(false));
    port.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); changed(true); } });
    out.append(h('div', { class: 'fwi-ask__form' }, from, swap, to, LL.field(t('fwi.ask.proto'), proto), LL.field(t('fwi.ask.port'), port), portMsg), result);
    if (a.from && a.to && Date.now() - a.at > 25000) setTimeout(run, 0);        // a redraw of the tab (every minute) refreshes the answer
    return out;
  }

  // ------------------------------------------------------------------ checks
  function checksCard(insight) {
    const list = sortFindings(insight.findings);
    if (!list.length) return null;
    const out = card('fw-checks', t('fw.checks.title'), t('fw.checks.lead'), 'fw-checks');
    out.setAttribute('tabindex', '-1');
    out.append(h('ul', { class: 'hv-findings' }, list.map(f => h('li', { class: 'hv-finding hv-finding--' + (f.level || 'info') },
      h('span', { class: 'chip chip--' + (LEVEL[f.level] || 'muted') }, t('hv.level.' + (f.level || 'info'))),
      h('div', { class: 'hv-finding__text' }, t('fwr.finding.' + f.kind, f.params || {}),
        f.suggestion ? h('div', { class: 'fwi-suggest' }, h('strong', {}, t('fwi.suggest.label')), ' ', t(f.suggestion)) : null)))));
    return out;
  }

  // ------------------------------------------------------------------ all cards, and where a command of the palette wants the page to scroll
  LL.fwInsightCards = function (view, insight, ctx) {
    if (!view || !view.state || !insight || !insight.source || !insight.board) return [];
    ctx = Object.assign({ pair: null, pick: () => {}, source: insight.source }, ctx || {});
    const cards = [boardCard(view, insight, ctx), askCard(view, insight, ctx), checksCard(insight)].filter(Boolean);
    let focus = null;
    try { focus = sessionStorage.getItem('ll-fw-focus'); if (focus === 'ask' || focus === 'checks') sessionStorage.removeItem('ll-fw-focus'); } catch (e) { /* private mode */ }
    if (focus === 'ask' || focus === 'checks') {            // the tab may draw twice while it loads: aim at whatever card is in the page
      const id = focus === 'ask' ? 'fw-ask' : 'fw-checks';
      const aim = () => { const el = document.getElementById(id); const r = el && el.getBoundingClientRect();
        if (el && (r.top < 0 || r.top > innerHeight * 0.5)) el.scrollIntoView({ block: 'start' }); };
      [150, 600, 1200].forEach(ms => setTimeout(aim, ms));
    }
    return cards;
  };

  // ------------------------------------------------------------------ command palette
  LL.registerCommand = LL.registerCommand || function (cmd) { LL.commands = (LL.commands || []).filter(c => c.id !== cmd.id).concat([cmd]); };
  LL.registerCommand({ id: 'firewall-ask', group: 'commands', order: 41, label: 'fwi.cmd.ask', keywords: 'fwi.cmd.ask_keywords',
    run: async () => {
      try {
        const x = await LL.api('/api/firewall/insight');       // the first live source that has rules
        if (x && x.source) localStorage.setItem('ll-fw-source', x.source);
        sessionStorage.setItem('ll-fw-focus', 'ask');
      } catch (e) { /* the tab opens anyway */ }
      LL.go('#/firewall');
    } });
})(typeof self !== 'undefined' ? self : this);
