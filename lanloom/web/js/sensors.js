/* Lanloom — Sensors tab of the hub bridge: every entity with search, "on site" (batteries and radio: now / soon / silent),
   the radio networks with their health, and the sensor page of one entity (1 h live ring, 24 h / 7 days / 30 days from
   the hub's recorder, thinned out at the server: min/max band + mean line, or a state bar). Read only. Plain DOM + canvas. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const RANGES = ['1h', '24h', '7d', '30d'];
  const MODES = ['all', 'onsite', 'radio'];
  const CAT_CHIP = { now: 'danger', silent: 'danger', soon: 'warn', ok: 'ok' };
  const FRESH_CHIP = { ok: 'ok', silent: 'danger', unknown: 'muted' };
  const HEALTH_CHIP = { ok: 'ok', warn: 'warn', bad: 'danger' };
  const PAGE = 300;
  const REST = ['off', 'closed', 'locked', 'idle', 'docked', 'standby', 'not_home', 'clear'];   // resting states: neutral in the state bar

  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { /* private mode */ } return null; };
  const isNum = v => v !== null && v !== '' && !isNaN(Number(v)) && isFinite(Number(v));

  const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)?$/;
  LL.hubValue = function (state, unit) {
    if (state == null) return '—';
    if (typeof state === 'string' && ISO.test(state)) {                 // a time stamp sensor: how long ago
      const ts = Date.parse(state) / 1000;
      if (isFinite(ts)) return ts <= Date.now() / 1000 ? t('onsite.seen', { age: LL.fmtAge(Date.now() / 1000 - ts) }) : LL.fmtDate(ts);
    }
    if (isNum(state)) return LL.fmtNum(Number(state)) + (unit ? ' ' + unit : '');
    const key = 'hub.state.' + state;
    return LL.strings[key] ? t(key) : String(state);
  };
  LL.radioKindText = k => t('radio.kind.' + (k || 'none'));
  const freshChip = f => h('span', { class: 'chip chip--' + (FRESH_CHIP[f] || 'muted') }, t('radio.fresh.' + (f || 'unknown')));

  // ------------------------------------------------------------------ list page
  async function sensorsView(app, rest) {
    const fr = LL.frame(app, 'sensors');
    const hubs = (LL.state && LL.state.hubs) || [];
    const page = h('div', { class: 'page sn-page' });
    fr.main.append(page);
    if (!hubs.length) {
      page.append(h('div', { class: 'card empty-state' }, h('h2', {}, t('sensors.empty.title')), h('p', { class: 'muted' }, t('sensors.empty.text')),
        LL.can('admin') ? h('a', { class: 'btn btn--primary', href: '#/settings' }, t('sensors.empty.action')) : null));
      return fr;
    }
    let integ = Number(rest && rest[0]);
    if (!hubs.some(x => x.id === integ)) integ = hubs[0].id;
    if (rest && rest.length > 1 && rest[1]) return entityPage(fr, page, integ, decodeURIComponent(rest.slice(1).join('/')));
    let mode = MODES.includes(store('ll-sensors-mode')) ? store('ll-sensors-mode') : 'all';
    let data = null, shown = PAGE, allOnsite = false;
    const q = h('input', { type: 'search', class: 'input', placeholder: t('sensors.search'), 'aria-label': t('sensors.search') });
    const areaSel = h('select', { class: 'select', 'aria-label': t('sensors.area') });
    const count = h('span', { class: 'muted' });
    const seg = h('div', { class: 'sn-seg', role: 'tablist', 'aria-label': t('sensors.title') });
    const body = h('div', { class: 'sn-body' });
    const hubSel = hubs.length > 1 ? h('select', { class: 'select select--sm', onchange: () => LL.go('#/sensors/' + hubSel.value) },
      hubs.map(x => h('option', { value: x.id, selected: x.id === integ }, x.name))) : null;
    const tools = h('div', { class: 'toolbar sn-tools' }, q, areaSel);
    page.append(h('div', { class: 'page__head sn-head' }, h('h1', {}, t('sensors.title')), count, hubSel ? h('span', { class: 'sn-head__hub' }, hubSel) : null), seg, tools, body);

    function drawSeg() {
      const n = data ? data.onsite.counts : {};
      const badge = { onsite: (n.now || 0) + (n.silent || 0), radio: data ? data.radio.filter(r => r.health !== 'ok').length : 0 };
      seg.replaceChildren(...MODES.map(m => h('button', { class: 'sn-seg__btn' + (m === mode ? ' is-active' : ''), type: 'button', role: 'tab', 'aria-selected': m === mode ? 'true' : 'false',
        onclick: () => { mode = m; store('ll-sensors-mode', m); shown = PAGE; render(); } }, t('sensors.mode.' + m),
        badge[m] ? h('span', { class: 'sn-seg__badge' }, String(badge[m])) : null)));
      tools.hidden = mode !== 'all';
    }
    function render() {
      if (!data) return;
      drawSeg();
      if (mode === 'onsite') body.replaceChildren(onsiteBlock(data, integ, allOnsite, v => { allOnsite = v; render(); }));
      else if (mode === 'radio') body.replaceChildren(radioBlock(data));
      else body.replaceChildren(listBlock());
    }
    function listBlock() {
      const s = q.value.trim().toLowerCase(), area = areaSel.value;
      const areas = Object.fromEntries((data.areas || []).map(a => [a.id, a.name]));
      const devs = Object.fromEntries((data.devices || []).map(d => [d.id, d]));
      const rows = data.entities.filter(e => (!area || (area === '-' ? !e.area : e.area === area)) &&
        (!s || [e.name, e.id, areas[e.area] || '', (devs[e.device] || {}).name || '', e.platform || ''].join(' ').toLowerCase().includes(s)));
      count.textContent = t('sensors.count', { n: rows.length, total: data.entities.length });
      if (!rows.length) return h('div', { class: 'empty-note' }, t('sensors.none'));
      const groups = new Map();
      rows.slice(0, shown).forEach(e => { const k = e.area || '-'; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(e); });
      const keys = [...groups.keys()].sort((a, b) => (a === '-') - (b === '-') || String(areas[a] || a).localeCompare(String(areas[b] || b), LL.locale()));
      const out = h('div', { class: 'sn-list card' });
      keys.forEach(k => {
        out.append(h('div', { class: 'sn-group' }, h('span', {}, k === '-' ? t('sensors.no_area') : (areas[k] || k)), h('span', { class: 'muted' }, String(groups.get(k).length))));
        groups.get(k).forEach(e => {
          const d = devs[e.device] || {};
          out.append(h('a', { class: 'sn-row', href: '#/sensors/' + integ + '/' + encodeURIComponent(e.id) },
            h('span', { class: 'sn-row__dot sn-dot--' + (e.state === 'unavailable' ? 'down' : d.fresh === 'silent' ? 'down' : 'up') }),
            h('span', { class: 'sn-row__main' }, h('span', { class: 'sn-row__name' }, e.name),
              h('span', { class: 'sn-row__sub mono' }, e.id + (d.name && d.name !== e.name ? ' · ' + d.name : ''))),   // one text: the ellipsis clips it
            h('span', { class: 'sn-row__val' + (e.state === 'unavailable' ? ' is-down' : '') }, LL.hubValue(e.state, e.unit)),
            h('span', { class: 'sn-row__age muted' }, e.changed_s != null ? LL.fmtAge(e.changed_s) : '')));
        });
      });
      if (rows.length > shown) out.append(h('button', { class: 'btn btn--ghost btn--block sn-more', type: 'button', onclick: () => { shown += PAGE * 2; render(); } },
        t('sensors.more', { n: rows.length - shown })));
      return out;
    }
    async function load() {
      try { data = await api('/api/hub/' + integ + '/sensors'); }
      catch (e) { if (!data) body.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (fw) fw.ok();
      const keep = areaSel.value;
      const used = new Set(data.entities.map(e => e.area || '-'));
      areaSel.replaceChildren(...[h('option', { value: '' }, t('sensors.area.all')),
        ...(data.areas || []).filter(a => used.has(a.id)).sort((a, b) => String(a.name).localeCompare(String(b.name), LL.locale())).map(a => h('option', { value: a.id }, a.name)),
        used.has('-') ? h('option', { value: '-' }, t('sensors.no_area')) : null].filter(Boolean));
      areaSel.value = keep;
      if (!data.connected) body.replaceChildren(h('div', { class: 'empty-note' }, t('sensors.waiting')));
      else render();
    }
    q.addEventListener('input', () => { shown = PAGE; render(); });
    areaSel.addEventListener('change', () => { shown = PAGE; render(); });
    body.append(h('div', { class: 'skeleton' }));
    const fw = LL.fresh.watch(body, 15000, load);
    await load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, 15000);
    return { destroy: () => { clearInterval(timer); fw.destroy(); fr.destroy(); } };
  }

  // ------------------------------------------------------------------ on site
  function onsiteBlock(data, integ, all, setAll) {
    const o = data.onsite, c = o.counts;
    const tiles = h('div', { class: 'sn-tiles' }, ['now', 'silent', 'soon', 'ok'].map(k => h('div', { class: 'sn-tile sn-tile--' + k },
      h('span', { class: 'sn-tile__n' }, String(c[k] || 0)), h('span', { class: 'sn-tile__label' }, t('onsite.cat.' + k)))));
    const items = o.items.filter(i => all || i.cat !== 'ok');
    const list = h('div', { class: 'sn-list card' });
    if (!items.length) list.append(h('div', { class: 'empty-note sn-allgood' }, t('onsite.all_good')));
    items.forEach(i => {
      const reason = i.reasons.length ? i.reasons.map(r => t('onsite.reason.' + r, { pct: i.battery })).join(' · ') : t('onsite.reason.ok');
      list.append(h('div', { class: 'sn-row sn-row--onsite' },
        h('span', { class: 'chip chip--' + CAT_CHIP[i.cat] + ' sn-cat' }, t('onsite.cat.' + i.cat)),
        h('span', { class: 'sn-row__main' }, h('span', { class: 'sn-row__name' }, i.name),
          h('span', { class: 'sn-row__sub' }, [i.area, i.radio ? LL.radioKindText(i.radio) : null, reason].filter(Boolean).join(' · '))),
        batteryBar(i),
        h('span', { class: 'sn-row__age muted' }, i.last_seen_s != null ? t('onsite.seen', { age: LL.fmtAge(i.last_seen_s) }) : '')));
    });
    const toggle = h('label', { class: 'check sn-all' }, h('input', { type: 'checkbox', checked: all, onchange: ev => setAll(ev.target.checked) }), h('span', {}, t('onsite.show_ok')));
    return h('div', { class: 'sn-stack' }, tiles,
      h('p', { class: 'muted sn-note' }, t('onsite.rule', { now: o.thresholds.now, soon: o.thresholds.soon, h: o.thresholds.silent_h })), toggle, list);
  }
  function batteryBar(i) {
    if (i.battery == null && !i.battery_low) return h('span', { class: 'sn-bat sn-bat--none' });
    const pct = i.battery == null ? 5 : Math.max(2, Math.min(100, i.battery));
    const lvl = i.battery_low || (i.battery != null && i.battery <= 10) ? 'now' : i.battery != null && i.battery <= 25 ? 'soon' : 'ok';
    return h('span', { class: 'sn-bat', title: t('onsite.battery') },
      h('span', { class: 'sn-bat__track' }, h('span', { class: 'sn-bat__fill sn-bat__fill--' + lvl, style: 'width:' + pct + '%' })),
      h('span', { class: 'sn-bat__pct' }, i.battery != null ? LL.fmtNum(i.battery, 0) + ' %' : t('onsite.low')));
  }

  // ------------------------------------------------------------------ radio networks
  function radioBlock(data) {
    const hl = data.health || {};
    const nets = (data.radio || []).map(n => h('div', { class: 'card sn-net' },
      h('div', { class: 'sn-net__head' }, h('span', { class: 'sn-net__icon', 'aria-hidden': 'true' }), h('div', { class: 'sn-net__titles' },
        h('h3', {}, LL.radioKindText(n.kind)), h('span', { class: 'muted' }, n.coordinator_name ? t('radio.coordinator', { name: n.coordinator_name }) : t('radio.no_coordinator.' + n.kind))),
        h('span', { class: 'chip chip--' + HEALTH_CHIP[n.health] }, t('radio.health.' + n.health))),
      h('dl', { class: 'facts sn-net__facts' },
        h('dt', {}, t('radio.devices')), h('dd', {}, String(n.devices)),
        h('dt', {}, t('radio.silent')), h('dd', { class: n.silent ? 'is-bad' : '' }, String(n.silent)),
        h('dt', {}, t('radio.batteries')), h('dd', { class: n.battery ? 'is-warn' : '' }, String(n.battery)),
        n.anchors ? [h('dt', {}, t('radio.anchors.' + n.kind)), h('dd', {}, t('radio.anchors_value', { n: n.anchors, silent: n.anchors_silent }))] : null),
      h('a', { class: 'btn btn--ghost btn--sm', href: '#/network' }, t('radio.on_map'))));
    const sub = (title, rows) => h('div', { class: 'card sn-health' }, h('h3', {}, title), rows.length ? h('ul', { class: 'sn-health__list' }, rows) : h('p', { class: 'muted' }, t('radio.none')));
    const recv = (hl.receivers || []).map(r => h('li', {}, h('span', {}, r.name), freshChip(r.fresh)));
    const brs = (hl.border_routers || []).map(r => h('li', {}, h('span', {}, r.name), freshChip(r.fresh)));
    const ev = data.events || [];
    const max = Math.max(1, ...ev.map(e => e.per_min));
    const events = h('div', { class: 'card sn-health' }, h('h3', {}, t('radio.events')), h('p', { class: 'muted sn-note' }, t('radio.events.note')),
      ev.length ? h('ul', { class: 'sn-bars' }, ev.map(e => h('li', {}, h('span', { class: 'sn-bars__name mono' }, e.platform === '-' ? t('radio.events.other') : e.platform),
        h('span', { class: 'sn-bars__track' }, h('span', { class: 'sn-bars__fill', style: 'width:' + Math.max(2, e.per_min / max * 100) + '%' })),
        h('span', { class: 'sn-bars__n' }, t('radio.per_min', { n: LL.fmtNum(e.per_min) }))))) : h('p', { class: 'muted' }, t('radio.events.none')));
    return h('div', { class: 'sn-stack' },
      nets.length ? h('div', { class: 'sn-nets' }, nets) : h('div', { class: 'empty-note' }, t('radio.empty')),
      h('div', { class: 'sn-healthgrid' },
        sub(t('radio.receivers', { n: hl.ble_devices || 0 }), recv),
        sub(t('radio.border_routers'), brs),
        h('div', { class: 'card sn-health' }, h('h3', {}, t('radio.kind.ipstd')),
          h('p', {}, t('radio.ipstd_value', { n: (hl.ipstd || {}).devices || 0, silent: (hl.ipstd || {}).silent || 0 })),
          h('p', { class: 'muted sn-note' }, t('radio.ipstd_note'))),
        events));
  }

  // ------------------------------------------------------------------ sensor page (one entity)
  async function entityPage(fr, page, integ, eid) {
    let range = RANGES.includes(store('ll-sensor-range')) ? store('ll-sensor-range') : '24h';
    let data = null, alive = true, fw = null;
    const head = h('div', { class: 'sn-ent__head' });
    const rangeSeg = h('div', { class: 'sn-seg sn-seg--range', role: 'tablist', 'aria-label': t('sensorview.range') });
    const chartWrap = h('div', { class: 'sn-chart' });
    const canvas = h('canvas', { class: 'sn-chart__canvas', role: 'img' });
    const tip = h('div', { class: 'sn-tip', hidden: true });
    const legend = h('div', { class: 'sn-legend' });
    const msg = h('div', { class: 'msg' });
    const factsEl = h('div', { class: 'sn-ent__facts' });
    chartWrap.append(canvas, tip);
    page.classList.add('sn-ent');
    page.append(h('a', { class: 'sn-back', href: '#/sensors/' + integ }, '← ' + t('sensors.back')), head,
      h('div', { class: 'card sn-ent__card' }, h('div', { class: 'sn-ent__bar' }, rangeSeg, legend), msg, chartWrap, factsEl));
    function drawSeg() {
      rangeSeg.replaceChildren(...RANGES.map(r => h('button', { class: 'sn-seg__btn' + (r === range ? ' is-active' : ''), type: 'button', role: 'tab', 'aria-selected': r === range ? 'true' : 'false',
        onclick: () => { range = r; store('ll-sensor-range', r); load(); } }, t('sensorview.range.' + r))));
    }
    function drawHead() {
      const e = data.entity;
      head.replaceChildren(h('div', { class: 'sn-ent__titles' }, h('h1', {}, e.name),
        h('div', { class: 'muted sn-ent__sub' }, [e.device, e.area, e.radio ? LL.radioKindText(e.radio) : null].filter(Boolean).join(' · '), h('span', { class: 'mono sn-ent__id' }, e.id))),
        h('div', { class: 'sn-ent__now' }, h('span', { class: 'sn-ent__value' + (e.state === 'unavailable' ? ' is-down' : '') }, LL.hubValue(e.state, e.unit)),
          h('span', { class: 'muted' }, e.changed_s != null ? t('sensorview.changed', { age: LL.fmtAge(e.changed_s) }) : t('sensorview.current')),
          e.fresh ? freshChip(e.fresh) : null));
    }
    function drawFacts() {
      const s = data.summary || {}, e = data.entity;
      const rows = data.kind === 'numeric'
        ? [[t('sensorview.min'), s.min != null ? LL.hubValue(s.min, e.unit) : '—'], [t('sensorview.mean'), s.mean != null ? LL.hubValue(Math.round(s.mean * 100) / 100, e.unit) : '—'],
          [t('sensorview.max'), s.max != null ? LL.hubValue(s.max, e.unit) : '—']]
        : [[t('sensorview.changes'), String(s.changes || 0)]];
      rows.push([t('sensorview.source'), t('sensorview.source.' + data.source)]);
      if (e.platform) rows.push([t('sensorview.integration'), e.platform]);
      factsEl.replaceChildren(LL.facts(rows));
    }
    async function load(quiet) {
      drawSeg();
      if (!quiet) LL.msg(msg, t('sensorview.loading'), 'info');
      try { data = await api('/api/hub/' + integ + '/entity/' + encodeURIComponent(eid) + '?range=' + range); LL.msg(msg, ''); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); return; }
      if (!alive) return;
      if (fw) fw.ok();
      drawHead(); drawFacts(); draw();
    }
    function colors() {
      const css = getComputedStyle(document.documentElement), v = n => css.getPropertyValue(n).trim();
      return { line: v('--ll-accent') || '#0C8574', text: v('--ll-text-3') || '#777', grid: v('--ll-border') || '#ddd', surface: v('--ll-surface') || '#fff',
        down: v('--ll-danger') || '#D64545', cats: [v('--ll-accent'), v('--ll-pcb-trace-flow'), v('--ll-pcb-trace-radio'), v('--ll-pcb-trace-integration'), v('--ll-pcb-trace-lan')].filter(Boolean),
        rest: v('--ll-border-strong') || '#ccc' };
    }
    const fmtX = ts => {
      const d = new Date(ts * 1000);
      const o = range === '1h' || range === '24h' ? { hour: '2-digit', minute: '2-digit', hour12: false } : { month: 'short', day: 'numeric' };
      try { return d.toLocaleString(LL.locale(), o); } catch (e) { return d.toISOString(); }
    };
    let geom = null;
    function draw() {
      if (!data) return;
      const c = colors();
      const w = Math.max(280, chartWrap.clientWidth || 600), hgt = data.kind === 'numeric' ? (window.innerWidth < 700 ? 220 : 300) : 120;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr); canvas.style.width = w + 'px'; canvas.style.height = hgt + 'px';
      const g = canvas.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, hgt);
      g.font = '11px ' + (getComputedStyle(document.body).fontFamily || 'sans-serif');
      const L = 52, R = 12, T = 12, B = 26, pw = w - L - R, ph = hgt - T - B;
      const x = ts => L + (ts - data.start) / Math.max(1, data.end - data.start) * pw;
      g.fillStyle = c.text; g.textBaseline = 'top';
      [data.start, (data.start + data.end) / 2, data.end].forEach((ts, i) => { const s = fmtX(ts); g.textAlign = ['left', 'center', 'right'][i]; g.fillText(s, i === 0 ? L : i === 1 ? L + pw / 2 : L + pw, T + ph + 8); });
      canvas.setAttribute('aria-label', t('sensorview.chart_label', { name: data.entity.name, range: t('sensorview.range.' + range) }));
      if (data.kind === 'numeric') {
        const pts = data.points || [];
        legend.replaceChildren(pts.length ? h('span', { class: 'sn-legend__item' }, h('i', { class: 'sn-key sn-key--band' }), t('sensorview.legend.band')) : '',
          pts.length ? h('span', { class: 'sn-legend__item' }, h('i', { class: 'sn-key sn-key--line' }), t('sensorview.legend.mean')) : '');
        if (!pts.length) { empty(g, w, hgt, c); geom = null; return; }
        let lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[3]));
        if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
        const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
        const y = v => T + ph - (v - lo) / (hi - lo) * ph;
        g.strokeStyle = c.grid; g.lineWidth = 1; g.textAlign = 'right'; g.textBaseline = 'middle';
        for (let i = 0; i <= 3; i++) {
          const v = lo + (hi - lo) * i / 3, yy = Math.round(y(v)) + 0.5;
          g.globalAlpha = i === 0 ? 1 : 0.6; g.beginPath(); g.moveTo(L, yy); g.lineTo(L + pw, yy); g.stroke(); g.globalAlpha = 1;
          g.fillStyle = c.text; g.fillText(LL.fmtNum(Math.round(v * 10) / 10), L - 8, yy);
        }
        g.save(); g.globalAlpha = 0.18; g.fillStyle = c.line; g.beginPath();
        pts.forEach((p, i) => { i ? g.lineTo(x(p[0]), y(p[3])) : g.moveTo(x(p[0]), y(p[3])); });
        for (let i = pts.length - 1; i >= 0; i--) g.lineTo(x(pts[i][0]), y(pts[i][1]));
        g.closePath(); g.fill(); g.restore();
        g.strokeStyle = c.line; g.lineWidth = 2; g.lineJoin = 'round'; g.lineCap = 'round'; g.beginPath();
        pts.forEach((p, i) => { i ? g.lineTo(x(p[0]), y(p[2])) : g.moveTo(x(p[0]), y(p[2])); });
        g.stroke();
        if (pts.length === 1) { g.fillStyle = c.line; g.beginPath(); g.arc(x(pts[0][0]), y(pts[0][2]), 4, 0, Math.PI * 2); g.fill(); }
        geom = { kind: 'numeric', pts, x, y, L, pw, T, ph };
      } else {
        const segs = data.segments || [];
        const states = [...new Set(segs.map(s => s[2]))];
        const share = (data.summary || {}).share || {};
        const order = Object.keys(share).concat(states.filter(s => !(s in share)));
        const col = {}; let k = 0;
        order.forEach(s => { col[s] = s === 'unavailable' ? c.down : (s === 'unknown' || REST.includes(s)) ? c.rest : c.cats[k++ % c.cats.length]; });
        legend.replaceChildren(...order.slice(0, 8).map(s => h('span', { class: 'sn-legend__item' }, h('i', { class: 'sn-key', style: 'background:' + col[s] }),
          LL.hubValue(s), share[s] != null ? h('span', { class: 'muted' }, ' ' + LL.fmtNum(Math.round(share[s] * 100), 0) + ' %') : null)));
        if (!segs.length) { empty(g, w, hgt, c); geom = null; return; }
        const bh = 40, by = T + ph - bh - 4;
        segs.forEach(s => {
          const x0 = x(s[0]), x1 = x(s[1]);
          g.fillStyle = col[s[2]]; g.globalAlpha = 0.9;
          g.fillRect(x0, by, Math.max(1, x1 - x0 - (x1 - x0 > 4 ? 1 : 0)), bh);
        });
        g.globalAlpha = 1;
        geom = { kind: 'state', segs, x, L, pw, by, bh };
      }
    }
    function empty(g, w, hgt, c) { g.fillStyle = c.text; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(t('sensorview.no_data'), w / 2, hgt / 2); }
    canvas.addEventListener('mousemove', ev => {
      if (!geom) return;
      const r = canvas.getBoundingClientRect(), mx = ev.clientX - r.left;
      if (mx < geom.L || mx > geom.L + geom.pw) { tip.hidden = true; return; }
      const ts = data.start + (mx - geom.L) / geom.pw * (data.end - data.start);
      let text;
      if (geom.kind === 'numeric') {
        const p = geom.pts.reduce((a, b) => (Math.abs(b[0] - ts) < Math.abs(a[0] - ts) ? b : a));
        text = [h('strong', {}, fmtX(p[0])), h('span', {}, t('sensorview.tip', { min: LL.hubValue(p[1], data.entity.unit), mean: LL.hubValue(Math.round(p[2] * 100) / 100, data.entity.unit), max: LL.hubValue(p[3], data.entity.unit) }))];
      } else {
        const s = geom.segs.find(sg => ts >= sg[0] && ts <= sg[1]);
        if (!s) { tip.hidden = true; return; }
        text = [h('strong', {}, LL.hubValue(s[2])), h('span', {}, fmtX(s[0]) + ' – ' + fmtX(s[1]))];
      }
      tip.replaceChildren(...text); tip.hidden = false;
      tip.style.left = Math.min(r.width - 170, Math.max(0, mx + 12)) + 'px';
    });
    canvas.addEventListener('mouseleave', () => { tip.hidden = true; });
    const onResize = () => draw();
    window.addEventListener('resize', onResize);
    document.addEventListener('ll-theme', onResize);
    fw = LL.fresh.watch(canvas, 10000, () => load(true), { band: false });
    await load();
    const timer = setInterval(() => { if (!document.hidden && range === '1h') load(true); }, 10000);
    return { destroy: () => { alive = false; clearInterval(timer); fw.destroy(); window.removeEventListener('resize', onResize); document.removeEventListener('ll-theme', onResize); fr.destroy(); } };
  }

  LL.view('sensors', sensorsView);

  // ------------------------------------------------------------------ search and commands (Ctrl K): the tab, its parts, a sensor's page
  if (LL.registerCommand) {
    const hasHub = () => !!(LL.state && (LL.state.hubs || []).length);
    const goMode = mode => () => { store('ll-sensors-mode', mode); LL.go('#/sensors'); if (location.hash === '#/sensors') LL.route(); };
    LL.registerCommand({ id: 'go.sensors', group: 'views', order: 4, available: hasHub, label: () => t('cmdk.go', { name: t('nav.sensors') }),
      keywords: () => t('cmdk.kw.sensors'), run: goMode('all') });
    LL.registerCommand({ id: 'go.sensors.onsite', group: 'views', order: 5, typedOnly: true, available: hasHub,
      label: () => t('cmdk.go', { name: t('nav.sensors') + ' › ' + t('sensors.mode.onsite') }), keywords: () => t('cmdk.kw.onsite'), run: goMode('onsite') });
    LL.registerCommand({ id: 'go.sensors.radio', group: 'views', order: 6, typedOnly: true, available: hasHub,
      label: () => t('cmdk.go', { name: t('nav.sensors') + ' › ' + t('sensors.mode.radio') }), keywords: () => t('cmdk.kw.radio'), run: goMode('radio') });
  }
  if (LL.registerAction) {
    LL.registerAction({ id: 'open.sensor', kinds: ['entity'], order: -1, label: 'cmdk.act.open_sensor', available: hit => !!(hit.entity_id && hit.integ),
      run: hit => LL.go('#/sensors/' + hit.integ + '/' + encodeURIComponent(hit.entity_id)) });
  }
})();
