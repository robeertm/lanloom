/* Lanloom — history parts of the device card and the Stability list: availability strips (24 h in 5-minute slots, 35 days),
   traffic plot per node (1 h / 6 h / 24 h / 7 days, full screen, fine read-out), connections with live rates, "Simulate
   maintenance", folding sections that keep their state across live updates, the hover window of the board, palette entries.
   Everything here only reads. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const RANGES = ['1h', '6h', '24h', '7d'];
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();   // colours always come from the tokens
  const pct = v => (v == null ? '—' : LL.fmtNum(v * 100, v >= 0.99 ? 2 : 1) + ' %');
  const loc = () => LL.locale();
  const stamp = (ts, long) => new Date(ts * 1000).toLocaleString(loc(), long ? { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false } : { hour: '2-digit', minute: '2-digit', hour12: false });
  const dayStamp = ts => new Date(ts * 1000).toLocaleDateString(loc(), { day: 'numeric', month: 'short' });
  // an element that outlives its page part is not asked again
  const alive = el => el.isConnected;

  function fit(canvas, hgt) {
    const dpr = window.devicePixelRatio || 1, w = Math.max(60, canvas.clientWidth || 320);
    canvas.style.height = hgt + 'px';                              // the drawing size follows the pixel ratio, the box does not (no resize loop)
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr);
    const g = canvas.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, hgt);
    return { g, w, h: hgt };
  }
  function watchSize(canvas, redraw) {
    if (typeof ResizeObserver === 'undefined') return;
    let w = 0;
    new ResizeObserver(() => { if (canvas.isConnected && canvas.clientWidth !== w) { w = canvas.clientWidth; redraw(); } }).observe(canvas);
  }

  // ------------------------------------------------------------------ availability strip and bars
  const slotColour = b => {
    const u = b >> 4, d = b & 15;
    if (!u && !d) return css('--ll-surface-3');
    if (!d) return css('--ll-ok');
    return u ? css('--ll-warn') : css('--ll-danger');
  };
  const slotText = b => ((b >> 4) || (b & 15) ? ((b & 15) ? ((b >> 4) ? t('avail.slot.partial') : t('avail.slot.down')) : t('avail.slot.up')) : t('avail.slot.unknown'));
  function strip(slots, now, slotS) {
    const canvas = h('canvas', { class: 'avail-strip', height: '22', role: 'img', 'aria-label': t('avail.strip.aria') });
    const cur = Math.floor(now / slotS);
    const draw = () => {
      const { g, w, h: hh } = fit(canvas, 22), cw = w / slots.length;
      slots.forEach((b, i) => { g.fillStyle = slotColour(b); g.fillRect(Math.floor(i * cw), 0, Math.max(1, Math.ceil(cw) - (cw > 3 ? 1 : 0)), hh); });
    };
    canvas.addEventListener('pointermove', ev => {
      const r = canvas.getBoundingClientRect(), i = Math.min(slots.length - 1, Math.max(0, Math.floor((ev.clientX - r.left) / r.width * slots.length)));
      canvas.title = stamp((cur - (slots.length - 1 - i)) * slotS) + ' · ' + slotText(slots[i]);
    });
    requestAnimationFrame(draw); watchSize(canvas, draw);
    return canvas;
  }
  function bars(values, now, height) {
    const canvas = h('canvas', { class: 'avail-bars', height: String(height || 40), role: 'img', 'aria-label': t('avail.bars.aria', { n: values.length }) });
    const draw = () => {
      const { g, w, h: hh } = fit(canvas, height || 40), cw = w / values.length;
      values.forEach((v, i) => {
        const x = Math.floor(i * cw), bw = Math.max(1, Math.ceil(cw) - 2);
        if (v == null) { g.fillStyle = css('--ll-surface-3'); g.fillRect(x, hh - 3, bw, 3); return; }
        g.fillStyle = v >= 0.999 ? css('--ll-ok') : v >= 0.99 ? css('--ll-grade-near') : v >= 0.95 ? css('--ll-warn') : css('--ll-danger');
        const bh = Math.max(4, Math.round(hh * (0.35 + 0.65 * Math.max(0, (v - 0.9) / 0.1))));
        g.fillRect(x, hh - bh, bw, bh);
      });
    };
    canvas.addEventListener('pointermove', ev => {
      const r = canvas.getBoundingClientRect(), i = Math.min(values.length - 1, Math.max(0, Math.floor((ev.clientX - r.left) / r.width * values.length)));
      canvas.title = dayStamp(now - (values.length - 1 - i) * 86400) + ' · ' + (values[i] == null ? t('avail.slot.unknown') : pct(values[i]));
    });
    requestAnimationFrame(draw); watchSize(canvas, draw);
    return canvas;
  }
  LL.availCells = { strip, bars };

  // ------------------------------------------------------------------ the card section "Availability"
  LL.availabilitySection = function (deviceId) {
    const body = h('div', { class: 'avail' }, h('div', { class: 'skeleton' }));
    const sec = h('section', { class: 'detail__sec', 'data-sec': 'availability' }, h('h3', {}, t('avail.title')), body);
    async function load() {
      let d;
      try { d = await api('/api/availability/dev:' + encodeURIComponent(deviceId)); } catch (e) { sec.remove(); return; }
      fw.ok();
      if (!d.known) { sec.remove(); return; }
      const chips = [];
      if (d.open) chips.push(h('span', { class: 'chip chip--danger' }, t('avail.open')));
      if (d.wobbly) chips.push(h('span', { class: 'chip chip--warn' }, t('avail.wobbly')));
      const list = (d.list || []).slice(0, 5);
      body.replaceChildren(...[
        h('div', { class: 'avail__head' }, h('strong', { class: 'avail__big' }, pct(d.share24)), h('span', { class: 'muted' }, t('avail.over24')), ...chips),
        strip(d.slots24, d.t, d.slot_s),
        h('div', { class: 'avail__axis muted', 'aria-hidden': 'true' }, h('span', {}, t('avail.axis.24h')), h('span', {}, t('avail.axis.12h')), h('span', {}, t('avail.axis.now'))),
        LL.facts([[t('avail.share7'), pct(d.share7)], [t('avail.share35'), pct(d.share35)], [t('avail.incidents7'), String(d.incidents7)],
          [t('avail.mttr'), d.mttr_s ? LL.fmtAge(d.mttr_s) : '—'], [t('avail.longest'), d.longest_s ? LL.fmtAge(d.longest_s) : '—']]),
        h('div', { class: 'avail__sub muted' }, t('avail.days', { n: d.bars.length })),
        bars(d.bars, d.t, 40),
        LL.a11y ? LL.a11y.tableAlt(t('a11y.table.avail_caption', { n: d.bars.length }), [t('a11y.table.day'), t('a11y.table.share')],
          d.bars.map((v, i) => [dayStamp(d.t - (d.bars.length - 1 - i) * 86400), v == null ? t('avail.slot.unknown') : pct(v)]).reverse()) : null,
        list.length ? h('ul', { class: 'avail-list' }, list.map(i => h('li', {}, h('span', { class: 'mono' }, stamp(i.from, true)),
          h('span', { class: 'muted' }, ' · ' + (i.open ? t('avail.ongoing') : LL.fmtAge(i.dur)))))) : h('p', { class: 'muted avail__none' }, t('avail.none'))].filter(Boolean));
    }
    const fw = LL.fresh.watch(sec, 60000, load, { band: false });
    load();
    const timer = setInterval(() => { if (!alive(sec)) { clearInterval(timer); fw.destroy(); return; } if (!document.hidden) load(); }, 60000);
    return sec;
  };

  // ------------------------------------------------------------------ traffic plot
  const cache = new Map();
  async function plotData(id, range) {
    const k = id + '|' + range, hit = cache.get(k);
    if (hit && Date.now() - hit.at < 20000) return hit.data;
    const data = await api('/api/traffic/' + encodeURIComponent(id) + '?range=' + range);
    cache.set(k, { at: Date.now(), data });
    return data;
  }
  function drawPlot(canvas, data, height, cursor) {
    const { g, w, h: hh } = fit(canvas, height), axis = 16, top = 16, base = hh - axis, pts = data.points, n = pts.length;
    const peak = Math.max(1, data.peak[0], data.peak[1]) * 1.08;
    const text = css('--ll-text-3'), grid = css('--ll-border');
    g.font = '11px ' + (getComputedStyle(document.body).fontFamily || 'system-ui'); g.textBaseline = 'alphabetic';
    g.strokeStyle = grid; g.lineWidth = 1;
    for (let k = 0; k <= 3; k++) { const y = Math.round(top + (base - top) * k / 3) + 0.5; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
    const x = i => (n > 1 ? i / (n - 1) * (w - 1) : w);
    const y = v => base - (v / peak) * (base - top);
    const line = (idx, colour, fill) => {
      g.beginPath();
      pts.forEach((p, i) => (i ? g.lineTo(x(i), y(p[idx])) : g.moveTo(x(i), y(p[idx]))));
      g.strokeStyle = colour; g.lineWidth = 1.6; g.stroke();
      if (fill) { g.lineTo(x(n - 1), base); g.lineTo(0, base); g.closePath(); g.globalAlpha = 0.14; g.fillStyle = colour; g.fill(); g.globalAlpha = 1; }
    };
    line(1, css('--ll-pcb-trace-lan'), true);
    line(0, css('--ll-pcb-trace-flow'), false);
    g.fillStyle = text; g.textAlign = 'left'; g.fillText(LL.fmtRate(peak / 1.08), 2, top - 5);
    const when = i => data.t0 + i * data.step;
    const day = data.range === '7d';
    const axisLabel = ts => (day ? new Date(ts * 1000).toLocaleDateString(loc(), { weekday: 'short', day: 'numeric' }) : stamp(ts));
    g.textAlign = 'left'; g.fillText(axisLabel(when(0)), 2, hh - 3);
    g.textAlign = 'center'; g.fillText(axisLabel(when(Math.floor(n / 2))), w / 2, hh - 3);
    g.textAlign = 'right'; g.fillText(axisLabel(when(n - 1) + data.step), w - 2, hh - 3);
    if (cursor != null && cursor >= 0 && cursor < n) {
      const cx = Math.round(x(cursor)) + 0.5;
      g.strokeStyle = css('--ll-text-2'); g.lineWidth = 1; g.beginPath(); g.moveTo(cx, top); g.lineTo(cx, base); g.stroke();
      for (const [idx, c] of [[0, css('--ll-pcb-trace-flow')], [1, css('--ll-pcb-trace-lan')]]) {
        g.fillStyle = c; g.beginPath(); g.arc(cx, y(pts[cursor][idx]), 3, 0, 6.2832); g.fill();
      }
    }
  }
  // the numbers behind the picture for the table alternative: at most 12 periods, the average rate of each (newest last, like the plot)
  function plotRows(data) {
    const pts = data.points, n = pts.length, buckets = Math.min(12, n), day = data.range === '7d' || data.range === '24h';
    const rows = [];
    for (let b = 0; b < buckets; b++) {
      const from = Math.floor(b * n / buckets), to = Math.max(from + 1, Math.floor((b + 1) * n / buckets));
      let up = 0, down = 0;
      for (let i = from; i < to; i++) { up += pts[i][0]; down += pts[i][1]; }
      const k = to - from;
      rows.push([stamp(data.t0 + from * data.step, day) + ' – ' + stamp(data.t0 + to * data.step), LL.fmtRate(up / k), LL.fmtRate(down / k)]);
    }
    return rows;
  }
  // the plot with its ranges; ``liveEl`` (optional) is the one-second live chart that stays the first tab
  function plot(id, opts) {
    opts = opts || {};
    const height = opts.height || 112;
    let range = opts.range || (opts.live ? 'live' : '1h'), data = null;
    const canvas = h('canvas', { class: 'tplot', height: String(height), role: 'img', 'aria-label': t('plot.aria') });
    const readout = h('div', { class: 'tplot__read muted', 'aria-live': 'off' }, t('plot.hint'));
    const stats = h('div', { class: 'tplot__stats muted' });
    const tabs = h('div', { class: 'seg plot-tabs', role: 'tablist', 'aria-label': t('plot.range') });
    const names = { live: 'plot.live', '1h': 'plot.r1h', '6h': 'plot.r6h', '24h': 'plot.r24h', '7d': 'plot.r7d' };
    const full = opts.full ? null : h('button', { class: 'icon-btn plot-full-btn', type: 'button', title: t('plot.full'), 'aria-label': t('plot.full'), onclick: () => LL.plotFull(id, range === 'live' ? '1h' : range, opts.title) }, '⤢');
    const alt = LL.a11y ? LL.a11y.tableAlt(t('a11y.table.traffic_caption'), [t('a11y.table.time'), t('a11y.table.up'), t('a11y.table.down')], []) : null;
    const hist = h('div', { class: 'tplot__hist' }, canvas, readout, stats, alt);
    const wrap = h('div', { class: 'tplot-wrap' }, h('div', { class: 'tplot__bar' }, tabs, full), opts.live || null, hist);
    function drawTabs() {
      tabs.replaceChildren(...(opts.live ? ['live'] : []).concat(RANGES).map(r => h('button', { type: 'button', role: 'tab', class: 'seg__btn' + (r === range ? ' is-active' : ''),
        'aria-selected': r === range ? 'true' : 'false', onclick: () => { range = r; show(); } }, t(names[r]))));
    }
    async function load() {
      if (range === 'live') return;
      try { data = await plotData(id, range); } catch (e) { data = null; readout.textContent = LL.errText(e); return; }
      fw.ok();
      paint();
    }
    function paint(cursor) {
      if (!data || range === 'live') return;
      const nonzero = data.points.some(p => p[0] || p[1]);
      canvas.hidden = !nonzero; readout.hidden = !nonzero;          // an empty plot is a message, not a flat line
      if (alt) { alt.hidden = !nonzero; if (nonzero) alt.setRows(plotRows(data)); }
      if (nonzero) drawPlot(canvas, data, height, cursor);
      stats.replaceChildren(nonzero ? h('span', {}, t('plot.sent', { v: LL.fmtBytes(data.total[0]) }) + ' · ' + t('plot.received', { v: LL.fmtBytes(data.total[1]) }) + ' · ' + t('plot.peak', { v: LL.fmtRate(Math.max(data.peak[0], data.peak[1])) })) : h('span', {}, t('plot.none')));
    }
    canvas.addEventListener('pointermove', ev => {
      if (!data) return;
      const r = canvas.getBoundingClientRect(), n = data.points.length, i = Math.min(n - 1, Math.max(0, Math.round((ev.clientX - r.left) / r.width * (n - 1))));
      const p = data.points[i], ts = data.t0 + i * data.step;
      readout.textContent = stamp(ts, data.range === '7d' || data.range === '24h') + ' – ' + stamp(ts + data.step) + ' · ↑ ' + LL.fmtRate(p[0]) + ' · ↓ ' + LL.fmtRate(p[1]);
      paint(i);
    });
    canvas.addEventListener('pointerleave', () => { readout.textContent = t('plot.hint'); paint(); });
    function show() {
      drawTabs();
      if (opts.live) opts.live.hidden = range !== 'live';
      hist.hidden = range === 'live';
      if (range !== 'live') { requestAnimationFrame(load); }
      if (range === 'live') LL.plotRange = 'live'; else LL.plotRange = range;
    }
    watchSize(canvas, () => paint());
    const fw = LL.fresh.watch(wrap, 60000, () => { if (range !== 'live') { cache.delete(id + '|' + range); return load(); } }, { band: false });
    show();
    const timer = setInterval(() => { if (!alive(wrap)) { clearInterval(timer); fw.destroy(); return; } if (!document.hidden && range !== 'live') { cache.delete(id + '|' + range); load(); } }, 60000);
    return wrap;
  }
  LL.trafficPlot = (id, live, title) => plot(id, { live, title, range: LL.plotRange && LL.plotRange !== 'live' ? LL.plotRange : (live ? 'live' : '1h') });

  LL.plotFull = function (id, range, title) {
    const opener = document.activeElement;
    const close = () => { document.removeEventListener('keydown', onKey, true); layer.remove(); if (opener && opener.focus) opener.focus(); };
    const onKey = ev => { if (ev.key === 'Escape') { ev.stopPropagation(); close(); } };
    const closeBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('detail.close'), onclick: close }, '✕');
    const p = plot(id, { range, full: true, height: Math.max(220, Math.min(520, Math.round(window.innerHeight * 0.58))) });
    const layer = h('div', { class: 'plot-full', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('plot.full_title', { name: title || '' }),
      onclick: ev => { if (ev.target === layer) close(); } },
      h('div', { class: 'plot-full__card card' }, h('header', { class: 'plot-full__head' }, h('h2', {}, title || t('plot.traffic')), closeBtn), p));
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(layer);
    closeBtn.focus();
  };

  // ------------------------------------------------------------------ connections of a card, with live rates
  LL.connectionsSection = function (d, id) {
    const rows = (d.traces || []).map(tr => {
      const peerId = tr.a === id ? tr.b : tr.a, peer = (d.peers || {})[peerId] || {};
      const up = h('span', { class: 'conn__rate mono' }, '—'), down = h('span', { class: 'conn__rate mono' }, '—');
      const name = peer.title || t('kind.' + (peer.kind || 'unknown'));
      const label = peer.device ? h('a', { href: '#/devices/' + encodeURIComponent(peerId), class: 'conn__peer' }, name) : h('span', { class: 'conn__peer' }, name);
      return { tr, up, down, el: h('li', { class: 'conn' }, h('span', { class: 'trace-sw trace-sw--' + tr.kind }), label,
        h('span', { class: 'muted conn__kind' }, t('board.trace.' + tr.kind) + ' · ' + (tr.measured ? t('detail.measured') : t('detail.unmeasured'))),
        h('span', { class: 'conn__rates' }, h('span', {}, '↑ ', up), h('span', {}, '↓ ', down))) };
    });
    const sec = h('section', { class: 'detail__sec', 'data-sec': 'links' }, h('h3', {}, t('detail.links')),
      rows.length ? h('ul', { class: 'link-list conn-list' }, rows.map(r => r.el)) : h('p', { class: 'muted' }, t('detail.no_links')));
    sec.update = frame => {
      for (const r of rows) {
        const v = (frame.rates || {})[r.tr.id];
        const [a, b] = v || [0, 0];
        r.up.textContent = LL.fmtRate(r.tr.a === id ? a : b); r.down.textContent = LL.fmtRate(r.tr.a === id ? b : a);
      }
    };
    return sec;
  };

  // ------------------------------------------------------------------ "Simulate maintenance" (the failure chain "down", nothing is switched)
  // What stops with it (sure, red) and what is only impaired (likely, yellow), from lanloom/failure.py; "Show on the map" colours the board.
  LL.maintenanceSection = function (deviceId) {
    const out = h('div', { class: 'maint__out' });
    const btn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: run }, t('maint.run'));
    const row = it => h('li', { class: 'maint__item maint__item--' + it.cert }, LL.kindIcon(it.kind === 'device' || it.kind === 'node' ? (it.dev_kind || 'unknown') : it.kind === 'hub' ? 'hub' : it.kind === 'container' ? 'container' : 'service'),
      it.kind === 'device' ? h('a', { href: '#/devices/' + encodeURIComponent(it.id) }, LL.failure ? LL.failure.nodeName(it) : (it.title || it.ip)) : h('span', {}, LL.failure ? LL.failure.nodeName(it) : it.title),
      h('span', { class: 'muted' }, ' · ' + t(it.cert === 'sure' ? 'maint.hard' : 'maint.soft')));
    async function run() {
      btn.disabled = true;
      try {
        const r = await api('/api/failure/' + encodeURIComponent(deviceId) + '?dir=down');
        const kids = [];
        if (r.root.self) kids.push(h('p', { class: 'msg msg--info' }, t('maint.self')));
        if (r.root.gateway) kids.push(h('p', { class: 'msg msg--warn' }, t('maint.gateway')));
        const devs = r.items.filter(it => it.kind === 'device' || it.kind === 'node' || it.kind === 'container');
        const conns = r.items.filter(it => it.kind === 'connection' || it.kind === 'hub');
        const sure = devs.filter(it => it.cert === 'sure'), soft = devs.filter(it => it.cert !== 'sure');
        kids.push(h('p', {}, sure.length ? t('maint.affected', { n: sure.length }) : t('maint.none')));
        if (sure.length) kids.push(h('ul', { class: 'maint__list' }, sure.slice(0, MAX_AFFECTED).map(row)));
        if (soft.length) kids.push(h('p', {}, t('maint.impaired', { n: soft.length })), h('ul', { class: 'maint__list' }, soft.slice(0, MAX_AFFECTED).map(row)));
        const more = Math.max(0, sure.length - MAX_AFFECTED) + Math.max(0, soft.length - MAX_AFFECTED);
        if (more) kids.push(h('p', { class: 'muted' }, t('maint.more', { n: more })));
        if (r.counts.automations) kids.push(h('p', {}, t('maint.autos', { n: r.counts.automations })));
        if (conns.length) kids.push(h('p', {}, t('maint.connections', { names: conns.map(c => c.title).join(', ') })));
        if (LL.failure && r.counts.total) kids.push(h('button', { class: 'btn btn--sm maint__show', type: 'button', onclick: () => LL.failure.simulate(deviceId) }, t('maint.show')));
        kids.push(h('p', { class: 'muted maint__note' }, t('maint.note')));
        out.replaceChildren(...kids);
      } catch (e) { out.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); }
      btn.disabled = false;
    }
    return h('section', { class: 'detail__sec', 'data-sec': 'maintenance' }, h('h3', {}, t('maint.title')), h('p', { class: 'muted maint__lead' }, t('maint.lead')), btn, out);
  };
  const MAX_AFFECTED = 40;

  // ------------------------------------------------------------------ sections that fold, the state stays across live updates
  const foldKey = () => LL.siteKey('ll-card-folds');
  let folds = {};
  try { folds = JSON.parse(sessionStorage.getItem(foldKey()) || '{}') || {}; } catch (e) { folds = {}; }
  const saveFolds = () => { try { sessionStorage.setItem(foldKey(), JSON.stringify(folds)); } catch (e) { /* private mode */ } };
  LL.foldSections = function (root) {
    const apply = () => root.querySelectorAll('.detail__sec').forEach(sec => {
      const head = sec.firstElementChild;
      if (!head || head.tagName !== 'H3' || sec.dataset.fold) return;
      sec.dataset.fold = '1';
      const key = sec.dataset.sec || head.textContent;
      head.classList.add('fold-head'); head.setAttribute('role', 'button'); head.tabIndex = 0;
      const sync = () => { const f = !!folds[key]; sec.classList.toggle('is-folded', f); head.setAttribute('aria-expanded', f ? 'false' : 'true'); };
      const toggle = () => { if (folds[key]) delete folds[key]; else folds[key] = 1; saveFolds(); sync(); };
      head.addEventListener('click', toggle);
      head.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); } });
      sync();
    });
    apply();
    const mo = new MutationObserver(apply);
    mo.observe(root, { childList: true, subtree: true });
    let want = '';
    try { want = sessionStorage.getItem('ll-card-focus') || ''; sessionStorage.removeItem('ll-card-focus'); } catch (e) { want = ''; }
    if (want) {
      const target = root.querySelector('[data-sec="' + want + '"]');
      if (target) { delete folds[want]; saveFolds(); target.classList.remove('is-folded'); requestAnimationFrame(() => target.scrollIntoView({ block: 'start' })); }
    }
    return mo;
  };

  // ------------------------------------------------------------------ the Stability list
  LL.stabilityPanel = function (opts) {
    const el = h('div', { class: 'stab' }, h('div', { class: 'skeleton' }));
    let data = null;
    function draw() {
      if (!data) return;
      const s = data.summary, f = (opts.filter && opts.filter()) || '';
      const rows = data.rows.filter(r => !f || r.title.toLowerCase().includes(f));
      const tile = (k, v, cls) => h('div', { class: 'stab-tile' + (cls ? ' ' + cls : '') }, h('strong', {}, v), h('span', { class: 'muted' }, t(k)));
      const wob = data.rows.filter(r => r.wobbly);
      el.replaceChildren(...[
        h('div', { class: 'stab-sum' }, tile('stab.tile.tracked', String(s.subjects)), tile('stab.tile.stable', String(s.stable), 'is-ok'),
          tile('stab.tile.incidents', String(s.incidents7), s.incidents7 ? 'is-warn' : ''), tile('stab.tile.mttr', s.mttr_s ? LL.fmtAge(s.mttr_s) : '—'),
          tile('stab.tile.wobbly', String(s.wobbly), s.wobbly ? 'is-warn' : '')),
        wob.length ? h('div', { class: 'stab-wobbly' }, h('strong', {}, t('stab.wobbly.title')), ' ', h('span', { class: 'muted' }, t('stab.wobbly.lead')),
          h('div', { class: 'stab-wobbly__chips' }, wob.map(r => h('button', { class: 'mf-chip is-on', type: 'button', onclick: () => open(r.id) }, t('stab.wobbly.item', { name: r.title, n: r.incidents7 }))))) : null,
        h('div', { class: 'stab-head muted', 'aria-hidden': 'true' }, h('span', {}, t('stab.col.name')), h('span', {}, t('stab.col.days', { n: s.days })), h('span', { class: 'num' }, t('stab.col.h24')),
          h('span', { class: 'num' }, t('stab.col.d7')), h('span', { class: 'num' }, t('stab.col.inc')), h('span', { class: 'num' }, t('stab.col.mttr'))),
        h('div', { class: 'stab-list', role: 'list' }, rows.length ? rows.map(r => {
          const canOpen = r.id.startsWith('dev:');
          const kind = canOpen ? LL.kindIcon(r.kind || 'unknown') : h('span', { class: 'stab-src', 'aria-hidden': 'true' }, r.id.startsWith('svc:') ? '◈' : '⇄');
          const name = h('span', { class: 'stab-row__name' }, kind, h('span', { class: 'stab-row__title' }, r.title),
            r.open ? h('span', { class: 'chip chip--danger' }, t('avail.open')) : r.wobbly ? h('span', { class: 'chip chip--warn' }, t('avail.wobbly')) : null,
            r.id.startsWith('src:') ? h('span', { class: 'muted' }, t('stab.connection')) : r.id.startsWith('svc:') ? h('span', { class: 'muted' }, t('stab.service')) : null);
          const bar = bars(r.bars, data.generated, 22); bar.classList.add('stab-row__bars');
          return h(canOpen ? 'button' : 'div', Object.assign({ class: 'stab-row' + (r.open ? ' is-open' : r.wobbly ? ' is-wobbly' : ''), role: 'listitem' }, canOpen ? { type: 'button', onclick: () => open(r.id) } : {}),
            name, bar, h('span', { class: 'num', 'data-label': t('stab.col.h24') }, pct(r.share24)), h('span', { class: 'num', 'data-label': t('stab.col.d7') }, pct(r.share7)),
            h('span', { class: 'num', 'data-label': t('stab.col.inc') }, String(r.incidents7)), h('span', { class: 'num', 'data-label': t('stab.col.mttr') }, r.mttr_s ? LL.fmtAge(r.mttr_s) : '—'));
        }) : [h('div', { class: 'empty-note' }, data.rows.length ? t('devices.none') : t('stab.empty'))])].filter(Boolean));
    }
    const open = id => { if (id.startsWith('dev:') && opts.onOpen) opts.onOpen(id.slice(4)); };
    async function load() {
      try { data = await api('/api/stability'); fw.ok(); draw(); } catch (e) { if (!data) el.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); }
    }
    const fw = LL.fresh.watch(el, 60000, load);
    load();
    const timer = setInterval(() => { if (!alive(el)) { clearInterval(timer); fw.destroy(); return; } if (!document.hidden) load(); }, 60000);
    el.redraw = draw;
    return el;
  };

  // ------------------------------------------------------------------ hover window of the board: traffic and availability at a glance
  const hover = { timer: null, el: null, id: null };
  function hoverClose() { clearTimeout(hover.timer); if (hover.el) { hover.el.remove(); hover.el = null; } hover.id = null; }
  async function hoverOpen(tile) {
    const id = tile.dataset.id;
    if (!id || hover.id === id) return;
    hover.id = id;
    const name = (tile.querySelector('.ll-tile__title') || {}).textContent || '';
    const card = h('div', { class: 'tile-hover card', role: 'tooltip' }, h('div', { class: 'tile-hover__title' }, name), h('div', { class: 'tile-hover__body muted' }, t('plot.loading')));
    document.body.appendChild(card);
    hover.el = card;
    const r = tile.getBoundingClientRect(), cw = 270;
    card.style.left = Math.max(8, Math.min(window.innerWidth - cw - 8, r.left + r.width / 2 - cw / 2)) + 'px';
    const below = r.bottom + 8 + 150 < window.innerHeight;
    card.style.top = (below ? r.bottom + 8 : Math.max(8, r.top - 8 - 150)) + 'px';
    const body = card.querySelector('.tile-hover__body');
    const [tr, av] = await Promise.all([plotData(id, '1h').catch(() => null), api('/api/availability/dev:' + encodeURIComponent(id)).then(r => (r.known ? r : null)).catch(() => null)]);
    if (hover.el !== card) return;
    const kids = [];
    if (tr && tr.points.some(p => p[0] || p[1])) {
      const c = h('canvas', { class: 'tile-hover__plot', height: '54', 'aria-hidden': 'true' });
      kids.push(h('div', { class: 'tile-hover__cap muted' }, t('plot.r1h') + ' · ' + t('plot.peak', { v: LL.fmtRate(Math.max(tr.peak[0], tr.peak[1])) })), c);
      requestAnimationFrame(() => { if (card.isConnected) drawMini(c, tr); });
    } else kids.push(h('div', { class: 'tile-hover__cap muted' }, t('plot.none')));
    if (av) {
      kids.push(h('div', { class: 'tile-hover__cap muted' }, t('avail.over24') + ' · ' + pct(av.share24)), strip(av.slots24, av.t, av.slot_s));
    }
    body.className = 'tile-hover__body'; body.replaceChildren(...kids);
  }
  function drawMini(canvas, data) {
    const { g, w, h: hh } = fit(canvas, 54), pts = data.points, peak = Math.max(1, data.peak[0], data.peak[1]) * 1.08;
    for (const [idx, colour] of [[1, css('--ll-pcb-trace-lan')], [0, css('--ll-pcb-trace-flow')]]) {
      g.beginPath();
      pts.forEach((p, i) => { const x = i / (pts.length - 1) * (w - 1), y = hh - 2 - (p[idx] / peak) * (hh - 4); i ? g.lineTo(x, y) : g.moveTo(x, y); });
      g.strokeStyle = colour; g.lineWidth = 1.4; g.stroke();
    }
  }
  document.addEventListener('pointerover', ev => {
    if (ev.pointerType === 'touch') return;
    const tile = ev.target.closest && ev.target.closest('.ll-tile');
    if (!tile) return;
    clearTimeout(hover.timer);
    hover.timer = setTimeout(() => hoverOpen(tile), 450);
  });
  document.addEventListener('pointerout', ev => {
    const tile = ev.target.closest && ev.target.closest('.ll-tile');
    if (tile && !tile.contains(ev.relatedTarget)) hoverClose();
  });
  window.addEventListener('hashchange', hoverClose);
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape') hoverClose(); });
  document.addEventListener('scroll', hoverClose, true);

  // ------------------------------------------------------------------ palette
  LL.registerCommand({ id: 'go.stability', group: 'views', order: 6, label: () => t('cmdk.go', { name: t('stab.title') }), keywords: () => t('cmdk.kw.stability'), run: () => LL.go('#/devices?view=stability') });
  const openCard = (hit, focus) => {
    try { sessionStorage.setItem('ll-card-focus', focus); } catch (e) { /* ignore */ }
    LL.go('#/devices/' + encodeURIComponent(hit.id));
  };
  LL.registerAction({ id: 'device-availability', kinds: ['device'], order: 30, label: 'cmdk.act.availability', available: hit => !!hit.id, run: hit => openCard(hit, 'availability') });
  LL.registerAction({ id: 'device-traffic', kinds: ['device'], order: 31, label: 'cmdk.act.traffic', available: hit => !!hit.id, run: hit => openCard(hit, 'traffic') });
  LL.registerAction({ id: 'device-maintenance', kinds: ['device'], order: 32, label: 'cmdk.act.maintenance', available: hit => !!hit.id,
    run: hit => (LL.failure ? LL.failure.simulate(hit.id) : openCard(hit, 'maintenance')) });
})();
