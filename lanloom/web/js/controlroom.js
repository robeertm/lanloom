/* Lanloom — control room (S9-S12): one page for what keeps the network and the house running.

   Data: GET /api/controlroom (the blocks, what needs a look, the freshness of every source, plus the internet, storage-box,
   battery-backup and backup views in one answer). Writes: PUT /api/wan/probe (administrators), PUT/DELETE /api/backups/chains/<id> and
   POST /api/backups/restore-check (operators). The server's texts are keys with variables (suffixes *_at, *_dur, *_bytes, *_key
   as in the nightly report: LL.fillText). A reading that went quiet is dimmed ("no fresh reading"), never green; every state
   carries a word and a sign, never a colour alone. #/wan, #/nas, #/backups show this page and jump to their part. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const SVGNS = 'http://www.w3.org/2000/svg';
  const REFRESH_MS = 15000;
  const HREF = { wan: '#/wan', nas: '#/nas', ups: '#/controlroom/ups', backups: '#/backups' };   // the blocks that have a part on this page
  const LEVEL_RANK = { danger: 3, warn: 2, unknown: 1, ok: 0 };
  const STATUS_LEVEL = { failed: 'danger', late: 'warn', none: 'warn', unknown: 'unknown', running: 'ok', ok: 'ok' };
  const STATUS_ORDER = ['failed', 'late', 'none', 'unknown', 'running', 'ok'];
  const SLOT = { u: 'up', r: 'reserve', f: 'flapping', d: 'down', '?': 'unknown' };
  const WAN_LEVEL = { up: 'ok', down: 'danger', flapping: 'warn', unknown: 'unknown', off: 'unknown' };
  const GRADE_LEVEL = { excellent: 'ok', good: 'ok', fair: 'ok', poor: 'warn', bad: 'danger', none: 'unknown' };
  // reading ranges of the three signal measures (dBm / dB), for the length of the bar only
  const RANGE = { rsrp: [-140, -44, 'dbm'], rsrq: [-20, -3, 'db'], sinr: [-5, 30, 'db'] };
  const RUN_CMD = 'curl -X POST -H "Authorization: Bearer $LANLOOM_TOKEN" -H "Content-Type: application/json" -d \'{"ok": true}\'';
  const RUN_PATH = 'api/v1/backups/<short-name>/run';
  const fx = v => String(Math.round(v * 100) / 100);                         // SVG and CSS geometry: always a decimal point
  const cls = lv => (lv === 'ok' || lv === 'warn' || lv === 'danger' ? lv : 'unknown');
  const num = (v, d) => LL.fmtNum(v, d);
  const word = (prefix, v) => (LL.strings[prefix + v] != null ? t(prefix + v) : String(v == null ? '' : v));
  const svgEl = (tag, attrs) => {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
    return el;
  };

  LL.NAV_CONTROLROOM_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M6.5 11h2.2l1.6-3.2 3 6.4 1.6-3.2h2.6M9 20h6M12 16v4"/></svg>';

  // ------------------------------------------------------------------ small parts
  // the server's text (key + variables); an empty variable is left out so a gap never shows as a placeholder
  function say(key, vars) {
    const v = {};
    Object.entries(vars || {}).forEach(([k, x]) => { if (x != null) v[k] = x; });
    return LL.fillText(key, v);
  }
  const mark = lv => h('i', { class: 'cr-mark cr-mark--' + cls(lv), 'aria-hidden': 'true' });
  const levelWord = lv => t('cr.ui.level.' + cls(lv));
  function chip(lv, text) {
    return h('span', { class: 'chip cr-chip chip--' + (cls(lv) === 'unknown' ? 'muted' : cls(lv)) }, mark(lv), text == null ? levelWord(lv) : text);
  }
  function freshness(age, fresh) {
    if (fresh === 'none' || age == null) return h('span', { class: 'cr-seen is-none' }, t('cr.ui.fresh.none'));
    return h('span', { class: 'cr-seen is-' + fresh }, t('cr.ui.fresh.' + fresh, { age: LL.fmtAge(age) }));
  }
  const pctBar = (pct, lv, label) => h('div', { class: 'cr-bar cr-bar--' + cls(lv), role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100',
    'aria-valuenow': String(Math.round(pct || 0)), 'aria-label': label },
  h('i', { style: 'width:' + fx(Math.max(0, Math.min(100, pct || 0))) + '%' }));
  const worstOf = lines => (lines || []).reduce((a, l) => (LEVEL_RANK[l.level] > LEVEL_RANK[a] ? l.level : a), 'ok');
  const unitText = (key, v, d) => t(key, { v: num(v, d == null ? (Math.abs(v) < 10 ? 1 : 0) : d) });
  const sectionTitle = (id, level) => h('div', { class: 'cr-sec__head' }, h('h2', { id: 'cr-h-' + id }, t('cr.ui.sec.' + id)), level ? chip(level) : null);
  function note(kind, text) { return h('p', { class: 'cr-note cr-note--' + kind }, mark(kind === 'info' ? 'unknown' : kind), h('span', {}, text)); }

  // ------------------------------------------------------------------ top: state of everything, the blocks, what needs a look
  function headline(d) {
    const n = d.points.length;
    if (!d.blocks.length) return { level: 'unknown', title: t('cr.ui.empty.title'), sub: t('cr.ui.empty.sub') };
    if (n) return { level: d.level === 'danger' ? 'danger' : 'warn', title: t('cr.ui.attention', { n }), sub: t('cr.ui.attention.sub') };
    if (d.level === 'unknown') return { level: 'unknown', title: t('cr.ui.unsure.title'), sub: t('cr.ui.unsure.sub') };
    return { level: 'ok', title: t('cr.ui.calm.title'), sub: t('cr.ui.calm.sub') };
  }
  function hero(d) {
    const x = headline(d);
    return h('section', { class: 'cr-hero cr-hero--' + cls(x.level), 'aria-label': t('cr.ui.hero') },
      mark(x.level), h('div', { class: 'cr-hero__text' }, h('strong', { class: 'cr-hero__title' }, x.title), h('span', { class: 'cr-hero__sub' }, x.sub)),
      h('span', { class: 'cr-hero__stamp muted' }, t('cr.ui.updated', { time: new Date(d.t * 1000).toLocaleTimeString(LL.locale(), { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) })));
  }
  function block(b) {
    const lines = b.lines.map((l, i) => ({ l, i })).sort((a, c) => LEVEL_RANK[c.l.level] - LEVEL_RANK[a.l.level] || a.i - c.i).map(x => x.l);
    const shown = lines.slice(0, 4), more = lines.length - shown.length;
    const name = t('cr.ui.block.' + b.id);
    return h('li', { class: 'cr-block cr-block--' + cls(b.level) + ' is-' + b.fresh, dataset: { block: b.id, level: b.level, fresh: b.fresh } },
      h('a', { class: 'cr-block__link', href: HREF[b.id] || b.link },
        h('div', { class: 'cr-block__head' }, h('h3', {}, name), chip(b.level)),
        h('ul', { class: 'cr-block__lines' }, shown.map(l => h('li', { class: 'cr-line cr-line--' + cls(l.level) }, mark(l.level), h('span', {}, say(l.key, l.vars)))),
          more > 0 ? h('li', { class: 'cr-line cr-line--more muted' }, t('cr.ui.more', { n: more })) : null),
        h('div', { class: 'cr-block__foot' }, freshness(b.age_s, b.fresh), h('span', { class: 'cr-block__go', 'aria-hidden': 'true' }, '›'))));
  }
  function blocksGrid(d) {
    if (!d.blocks.length) return h('section', { class: 'card cr-empty' }, h('p', {}, t('cr.ui.empty.text')), h('a', { class: 'btn btn--ghost btn--sm', href: '#/settings' }, t('cr.ui.empty.add')));
    return h('section', { class: 'cr-blocks', 'aria-label': t('cr.ui.blocks') }, h('ul', { class: 'cr-grid' }, d.blocks.map(block)));
  }
  function pointsList(d) {
    const body = d.points.length
      ? h('ul', { class: 'cr-points' }, d.points.map(p => h('li', { class: 'cr-point cr-point--' + cls(p.level), dataset: { point: p.id } },
        h('a', { href: HREF[p.block] || '#/controlroom' }, mark(p.level), h('span', { class: 'cr-point__text' }, say(p.key, p.vars)),
          h('span', { class: 'chip chip--muted cr-point__block' }, t('cr.ui.block.' + p.block)), h('span', { class: 'cr-point__go', 'aria-hidden': 'true' }, '›')))))
      : h('p', { class: 'cr-calm muted' }, t('cr.ui.points.none'));
    return h('section', { class: 'card cr-card cr-points-card', 'aria-labelledby': 'cr-h-points' }, h('div', { class: 'cr-sec__head' }, h('h2', { id: 'cr-h-points' }, t('cr.ui.points')),
      d.points.length ? h('span', { class: 'chip chip--muted' }, num(d.points.length, 0)) : null), body);
  }

  // ------------------------------------------------------------------ internet
  function axis(kind, a, probe) {
    const lv = WAN_LEVEL[a.state] || 'unknown';
    const facts = [];
    if (kind === 'gateway') {
      facts.push(a.from ? t('cr.ui.wan.from.' + a.from) : t('cr.ui.wan.from.none'));
      facts.push(freshness(a.age_s, a.age_s == null ? 'none' : (a.fresh || 'fresh')));
    } else if (a.state === 'off') {
      facts.push(t(probe.configured ? 'cr.ui.wan.probe.paused' : 'cr.ui.wan.probe.off'));
    } else {
      if (a.latency_ms != null) facts.push(say('cr.wan.latency', { ms: Math.round(a.latency_ms) }));
      if (a.fails) facts.push(t('cr.ui.wan.fails', { n: a.fails }));
      facts.push(freshness(a.age_s, a.age_s == null ? 'none' : (a.age_s <= 90 ? 'fresh' : 'late')));
    }
    return h('div', { class: 'cr-axis cr-axis--' + cls(lv), dataset: { axis: kind } },
      h('span', { class: 'cr-axis__label' }, t('cr.ui.wan.axis.' + kind)),
      h('strong', { class: 'cr-axis__state' }, mark(lv), t('cr.ui.state.' + a.state)),
      h('span', { class: 'cr-axis__facts muted' }, facts.map(f => (typeof f === 'string' ? h('span', {}, f) : f))));
  }
  function lineRow(ln) {
    const lv = ln.up === false ? 'danger' : ln.up === true ? 'ok' : 'unknown';
    const stateWord = ln.up === true ? t('cr.ui.state.up') : ln.up === false ? t('cr.ui.state.down') : t('cr.ui.state.unknown');
    const rates = ln.rx_bps != null || ln.tx_bps != null
      ? h('span', { class: 'cr-wline__rates mono' },
        h('span', { 'aria-label': t('cr.ui.wan.rx') }, '↓ ', LL.fmtRate((ln.rx_bps || 0) / 8)), h('span', { 'aria-label': t('cr.ui.wan.tx') }, '↑ ', LL.fmtRate((ln.tx_bps || 0) / 8))) : null;
    return h('li', { class: 'cr-wline' + (ln.active ? ' is-active' : '') + (ln.enabled === false ? ' is-off' : ''), dataset: { line: ln.id } },
      h('span', { class: 'cr-wline__name' }, h('strong', {}, ln.name),
        h('span', { class: 'cr-wline__sub muted' }, [word('cr.ui.kind.', ln.kind), ln.source].filter(Boolean).join(' · '))),
      h('span', { class: 'cr-wline__chips' },
        ln.role === 'primary' || ln.role === 'backup' ? h('span', { class: 'chip chip--muted' }, t('cr.ui.role.' + ln.role)) : null,
        ln.active ? h('span', { class: 'chip chip--accent' }, t('cr.ui.wan.active')) : null,
        ln.enabled === false ? h('span', { class: 'chip chip--muted' }, t('cr.ui.wan.disabled')) : null,
        chip(lv, stateWord)),
      rates, ln.latency_ms != null ? h('span', { class: 'cr-wline__lat muted mono' }, say('cr.wan.latency', { ms: Math.round(ln.latency_ms) })) : null);
  }
  function historyBar(hist) {
    const bar = hist.bar || '';
    const grid = h('div', { class: 'cr-hist__bar', role: 'img', 'aria-label': t('cr.ui.hist.aria', { n: bar.length, mins: Math.round((hist.slot_s || 900) / 60) }) },
      [...bar].map((c, i) => h('i', { class: 'cr-hist__slot cr-hist__slot--' + (SLOT[c] || 'unknown'),
        title: t('cr.ui.hist.slot', { at: LL.fmtDate((hist.start_t || 0) + i * (hist.slot_s || 900)), state: t('cr.ui.hist.state.' + (SLOT[c] || 'unknown')) }) })));
    grid.style.gridTemplateColumns = 'repeat(' + Math.max(1, bar.length) + ', minmax(0, 1fr))';
    const legend = h('ul', { class: 'cr-hist__legend' }, Object.values(SLOT).map(s => h('li', {}, h('i', { class: 'cr-hist__slot cr-hist__slot--' + s, 'aria-hidden': 'true' }), t('cr.ui.hist.state.' + s))));
    return h('div', { class: 'cr-hist' }, h('h3', {}, t('cr.ui.hist.title')), grid,
      h('div', { class: 'cr-hist__axis muted' }, h('span', {}, t('cr.ui.hist.start')), h('span', {}, t('cr.ui.hist.now'))), legend);
  }
  function eventRow(e) {
    const lv = { up: 'ok', failback: 'ok', down: 'danger', failover: 'warn' }[e.kind] || 'unknown';
    return h('li', { class: 'cr-ev' }, h('time', { class: 'cr-ev__time mono muted', datetime: new Date(e.t * 1000).toISOString() }, LL.fmtDate(e.t)),
      chip(lv, word('cr.ui.event.', e.kind)), h('span', { class: 'cr-ev__text' }, [e.line, e.basis ? t('cr.ui.basis.' + e.basis.replace('-event', '')) : ''].filter(Boolean).join(' · ')));
  }
  // levels of one signal measure: a bar as long as the reading is good, with the number and the word beside it
  function measure(key, value, grade) {
    const [lo, hi, unit] = RANGE[key], lv = GRADE_LEVEL[grade] || 'unknown';
    const share = value == null ? 0 : Math.max(0, Math.min(1, (value - lo) / (hi - lo))) * 100;
    return h('li', { class: 'cr-meas cr-meas--' + lv, dataset: { measure: key } },
      h('span', { class: 'cr-meas__name' }, t('cr.ui.sig.' + key)),
      h('span', { class: 'cr-meas__val mono' }, value == null ? '—' : t(unit === 'dbm' ? 'cr.ui.unit.dbm' : 'cr.ui.unit.db', { v: num(value, 0) })),
      pctBar(share, lv, t('cr.ui.sig.' + key) + ': ' + t('cr.grade.' + (grade || 'none'))),
      h('span', { class: 'cr-meas__grade' }, t('cr.grade.' + (grade || 'none'))));
  }
  function cellCard(c) {
    const q = c.quality || {}, sig = c.signal || {}, ms = q.measures || {};
    const kv = (k, v) => v ? h('div', {}, h('dt', {}, t(k)), h('dd', {}, v)) : null;
    const bands = (c.bands || []).map(b => h('li', { class: 'cr-band cr-band--' + (b.role || 'x') },
      h('strong', {}, b.band), h('span', { class: 'muted' }, [b.kind, b.mhz ? t('cr.ui.unit.mhz', { v: num(b.mhz, 0) }) : '', b.duplex].filter(Boolean).join(' · ')),
      b.role ? h('span', { class: 'chip chip--' + (b.role === 'coverage' ? 'accent' : 'muted') }, word('cr.ui.band.', b.role)) : null));
    const dt = c.data || {};
    const dataBlock = dt.pct != null ? h('div', { class: 'cr-data' },
      h('div', { class: 'cr-data__top' }, h('span', {}, t('cr.ui.cell.data')), h('strong', { class: 'mono' }, dt.limit ? t('cr.ui.cell.data_of', { used: LL.fmtBytes(dt.used), limit: LL.fmtBytes(dt.limit), pct: num(dt.pct, 0) }) : LL.fmtBytes(dt.used))),
      pctBar(dt.pct, dt.level, t('cr.ui.cell.data')), dt.period_end_t ? h('span', { class: 'muted cr-data__end' }, t('cr.ui.cell.data_end', { date: LL.fmtDate(dt.period_end_t) })) : null) : null;
    const admin = c.admin ? h('details', { class: 'cr-admin' }, h('summary', {}, t('cr.ui.cell.admin')),
      h('dl', { class: 'cr-facts' }, kv('cr.ui.cell.cell_id', c.admin.cell_id), kv('cr.ui.cell.tac', c.admin.tac), kv('cr.ui.cell.location', c.admin.location))) : null;
    return h('article', { class: 'cr-cell cr-cell--' + cls(c.level) + ' is-' + (c.fresh || 'none'), dataset: { cell: c.id } },
      h('header', { class: 'cr-cell__head' },
        h('div', {}, h('h3', {}, c.name || t('cr.ui.cell.title')), h('span', { class: 'muted' }, [c.carrier, c.tech].filter(Boolean).join(' · '))),
        h('div', { class: 'cr-cell__chips' }, c.active ? h('span', { class: 'chip chip--accent' }, t('cr.ui.wan.active')) : null,
          chip(GRADE_LEVEL[q.grade] || 'unknown', t('cr.ui.cell.signal', { grade: t('cr.grade.' + (q.grade || 'none')) })))),
      h('div', { class: 'cr-cell__cols' },
        h('div', { class: 'cr-cell__col' }, h('ul', { class: 'cr-meas-list' }, ['rsrp', 'rsrq', 'sinr'].map(k => measure(k, sig[k], ms[k]))), dataBlock),
        bands.length ? h('div', { class: 'cr-bands' }, h('h4', {}, t('cr.ui.cell.bands', { n: bands.length })), h('ul', {}, bands)) : null),
      admin, h('div', { class: 'cr-cell__foot' }, freshness(c.age_s, c.fresh || 'none')));
  }
  // setting up the outside check (administrators); the form is built once and keeps what is typed while the page refreshes
  function probeForm(onSaved) {
    const state = { dirty: false };
    const enabled = h('input', { type: 'checkbox' });
    const dns = h('input', { type: 'text', spellcheck: 'false', autocomplete: 'off', maxlength: '253', placeholder: 'example.org' });
    const tcp = h('textarea', { class: 'input cr-probe__tcp', rows: '3', spellcheck: 'false', autocomplete: 'off', placeholder: '192.0.2.1:53' });
    const preset = h('select', { class: 'select' });
    const msg = h('div', { class: 'msg', role: 'status' });
    const save = h('button', { class: 'btn btn--primary', type: 'submit' }, t('cr.ui.probe.save'));
    const form = h('form', { class: 'cr-probe__form', novalidate: true },
      LL.field(t('cr.ui.probe.preset'), preset, t('cr.ui.probe.preset.hint')),
      h('div', { class: 'grid-2' }, LL.field(t('cr.ui.probe.dns'), dns, t('cr.ui.probe.dns.hint')), LL.field(t('cr.ui.probe.tcp'), tcp, t('cr.ui.probe.tcp.hint'))),
      h('label', { class: 'check' }, enabled, h('span', {}, t('cr.ui.probe.enabled'))), msg, h('div', { class: 'btn-row' }, save));
    const mark_dirty = () => { state.dirty = true; };
    [enabled, dns, tcp].forEach(el => el.addEventListener('input', mark_dirty));
    let presets = [];
    preset.addEventListener('change', () => {
      const p = presets.find(x => x.id === preset.value);
      if (!p) return;
      dns.value = p.dns || ''; tcp.value = (p.tcp || []).join('\n'); enabled.checked = true; state.dirty = true;
    });
    form.addEventListener('submit', async ev => {
      ev.preventDefault();
      save.disabled = true;
      const body = { enabled: enabled.checked, dns: dns.value.trim(), tcp: tcp.value.split(/[\s,;]+/).filter(Boolean) };
      try { await api('/api/wan/probe', { method: 'PUT', body }); state.dirty = false; LL.msg(msg, t('cr.ui.probe.saved'), 'ok'); onSaved(); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      save.disabled = false;
    });
    form.fill = probe => {
      presets = probe.presets || [];
      const picked = preset.value;
      preset.replaceChildren(h('option', { value: '' }, t('cr.ui.probe.preset.none')), ...presets.map(p => h('option', { value: p.id },
        word('cr.ui.probe.preset.', p.id) + (p.tcp.length ? ' (' + p.tcp.join(', ') + ')' : p.dns ? ' (' + p.dns + ')' : ''))));
      preset.value = presets.some(x => x.id === picked) ? picked : '';
      if (state.dirty || form.contains(document.activeElement)) return;
      enabled.checked = !!probe.enabled; dns.value = probe.dns || ''; tcp.value = (probe.tcp || []).join('\n');
    };
    return form;
  }
  function wanSection(w, ctx) {
    const lv = WAN_LEVEL[w.state] || 'unknown';
    const probe = w.probe || {};
    const top = h('div', { class: 'cr-wan__top' },
      h('div', { class: 'cr-state cr-state--' + cls(lv) }, mark(lv), h('div', {}, h('strong', { class: 'cr-state__text' }, say('cr.wan.state.' + w.state, { since_at: w.since_t || 0 })),
        h('span', { class: 'cr-state__sub muted' }, t('cr.ui.wan.basis.' + (w.basis || 'none'))))),
      h('div', { class: 'cr-axes' }, axis('gateway', w.axes.gateway, probe), axis('probe', w.axes.probe, probe)));
    const kids = [sectionTitle('wan', lv), top];
    if (w.disagree && w.disagree !== 'none') kids.push(note('warn', say('cr.wan.disagree.' + w.disagree)));
    if (!probe.configured) kids.push(note('info', t('cr.ui.probe.none')));
    const fo = w.failover || {};
    if (fo.on_reserve) kids.push(note('warn', say('cr.wan.on_reserve', { since_at: fo.since_t || 0 })));
    if (w.lines && w.lines.length) {
      kids.push(h('div', { class: 'cr-sub' }, h('h3', {}, t('cr.ui.wan.lines')), h('ul', { class: 'cr-wlines' }, w.lines.map(lineRow)),
        fo.has_reserve && !fo.on_reserve ? h('p', { class: 'cr-sub__note muted' }, say('cr.wan.reserve_idle'), fo.switches_24h ? ' · ' + say('cr.wan.switches', { n: fo.switches_24h }) : '') : null));
    }
    const hist = w.history || {};
    if (hist.bar) {
      const stats = h('div', { class: 'cr-hist__stats' },
        h('div', { class: 'cr-stat' }, h('strong', { class: 'mono' }, hist.uptime_pct_7d == null ? '—' : t('cr.ui.unit.pct', { v: num(hist.uptime_pct_7d, 1) })), h('span', { class: 'muted' }, t('cr.ui.hist.uptime'))),
        h('div', { class: 'cr-stat' }, h('strong', { class: 'mono' }, num(hist.outages_7d || 0, 0)), h('span', { class: 'muted' }, t('cr.ui.hist.outages'))));
      kids.push(h('div', { class: 'cr-sub cr-histbox' }, historyBar(hist), stats));
    }
    if ((hist.events || []).length) kids.push(h('div', { class: 'cr-sub' }, h('h3', {}, t('cr.ui.hist.events')), h('ul', { class: 'cr-evs' }, hist.events.slice(0, 8).map(eventRow))));
    if ((w.cellular || []).length) kids.push(h('div', { class: 'cr-sub' }, h('h3', {}, t('cr.ui.cell.section')), h('div', { class: 'cr-cells' }, w.cellular.map(cellCard))));
    if (LL.can('admin')) {
      ctx.probe.fill(probe);
      kids.push(h('div', { class: 'cr-sub cr-probe' }, h('h3', {}, t('cr.ui.probe.title')), h('p', { class: 'muted' }, t('cr.ui.probe.lead')), ctx.probe));
    }
    return h('section', { class: 'card cr-card cr-sec', id: 'cr-wan', dataset: { sec: 'wan' }, tabindex: '-1', 'aria-labelledby': 'cr-h-wan' }, kids);
  }

  // ------------------------------------------------------------------ backups
  function upsLine(u) {
    const base = { name: u.name };
    if (u.state === 'battery' || u.state === 'low_battery') return say('cr.ups.' + u.state, Object.assign(base, { charge: u.charge == null ? null : Math.round(u.charge), runtime_dur: u.runtime_s == null ? null : Math.round(u.runtime_s) }));
    if (u.state === 'online') return say('cr.ups.online', Object.assign(base, { charge: u.charge == null ? null : Math.round(u.charge), runtime_dur: u.runtime_s == null ? null : Math.round(u.runtime_s) }));
    return say('cr.ups.' + (['replace', 'overload'].includes(u.state) ? u.state : 'unknown'), base);
  }
  function chainEditor(chain, reload) {
    const editing = !!chain;
    const name = h('input', { type: 'text', maxlength: '64', value: chain ? chain.name : '' });
    const id = h('input', { type: 'text', maxlength: '48', spellcheck: 'false', autocapitalize: 'off', value: chain ? chain.id.replace(/^manual:/, '') : '', disabled: editing });
    const source = h('input', { type: 'text', maxlength: '64', value: chain ? chain.source : '' });
    const target = h('input', { type: 'text', maxlength: '64', value: chain ? chain.target : '' });
    const hours = chain ? chain.expect_s / 3600 : 24;
    const inDays = hours >= 24 && Number.isInteger(hours / 24);
    const amount = h('input', { type: 'number', min: '1', step: '1', inputmode: 'numeric', value: String(inDays ? hours / 24 : Math.round(hours)) });
    const unit = h('select', { class: 'select', 'aria-label': t('cr.ui.bk.f.unit') }, ['hours', 'days'].map(u => h('option', { value: u }, t('cr.ui.unit.' + u))));
    unit.value = inDays ? 'days' : 'hours';
    const picked = new Set(chain && chain.devices ? chain.devices : []);
    const filter = h('input', { type: 'search', class: 'input-sm', placeholder: t('cr.ui.bk.f.devices.filter') });
    const devList = h('div', { class: 'cr-devpick choice-list choice-list--compact' });
    let devices = null;
    async function fillDevices() {
      if (!devices) { try { devices = (await api('/api/devices')).devices || []; } catch (e) { devices = []; } }
      const q = filter.value.trim().toLowerCase();
      const rows = devices.filter(d => !q || ((d.user_title || d.title || '') + ' ' + (d.ip || '') + ' ' + d.id).toLowerCase().includes(q));
      rows.sort((a, b) => (picked.has(b.id) ? 1 : 0) - (picked.has(a.id) ? 1 : 0));
      devList.replaceChildren(...rows.slice(0, 60).map(d => {
        const cb = h('input', { type: 'checkbox', checked: picked.has(d.id), onchange: () => { cb.checked ? picked.add(d.id) : picked.delete(d.id); } });
        return h('label', { class: 'choice' }, cb, h('span', { class: 'choice__main' }, h('span', { class: 'choice__title' }, d.user_title || d.title || d.ip || d.id), h('span', { class: 'muted mono' }, d.ip || '')));
      }));
    }
    filter.addEventListener('input', fillDevices);
    fillDevices();
    const msg = h('div', { class: 'msg', role: 'alert' });
    let touchedId = editing;
    id.addEventListener('input', () => { touchedId = true; });
    name.addEventListener('input', () => { if (!touchedId) id.value = name.value.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48); });
    const save = h('button', { class: 'btn btn--primary', type: 'button' }, t('cr.ui.bk.save'));
    const body = h('div', { class: 'stack cr-form' }, LL.field(t('cr.ui.bk.f.name'), name), LL.field(t('cr.ui.bk.f.id'), id, t('cr.ui.bk.f.id.hint')),
      h('div', { class: 'grid-2' }, LL.field(t('cr.ui.bk.f.source'), source), LL.field(t('cr.ui.bk.f.target'), target)),
      LL.field(t('cr.ui.bk.f.devices'), h('div', { class: 'stack-sm' }, filter, devList), t('cr.ui.bk.f.devices.hint')),
      h('div', { class: 'field' }, h('span', { class: 'field__label' }, t('cr.ui.bk.f.expect')), h('div', { class: 'cr-form__expect' }, amount, unit), h('div', { class: 'field__hint' }, t('cr.ui.bk.f.expect.hint'))), msg);
    const dlg = LL.dialog(t(editing ? 'cr.ui.bk.edit' : 'cr.ui.bk.add'), body, save);
    save.addEventListener('click', async () => {
      save.disabled = true;
      const key = editing ? chain.id.replace(/^manual:/, '') : id.value.trim();
      const expect = Number(amount.value) * (unit.value === 'days' ? 24 : 1);
      try {
        await api('/api/backups/chains/' + encodeURIComponent(key), { method: 'PUT', body: { name: name.value.trim(), source: source.value.trim(), target: target.value.trim(), expect_hours: expect, devices: [...picked] } });
        dlg.close(); LL.toast(t('cr.ui.bk.saved'), 'ok'); reload();
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); save.disabled = false; }
    });
    name.focus();
  }
  function restoreLine(c, reload, oldDays) {
    const text = c.restore_checked_t == null ? t('cr.ui.bk.restore.never') : t('cr.ui.bk.restore.at', { date: LL.fmtDate(c.restore_checked_t) });
    const lv = c.restore_hint === 'old' ? 'warn' : c.restore_hint === 'never' ? 'unknown' : 'ok';
    const hint = c.restore_hint === 'never' ? t('cr.ui.bk.restore.hint_never') : c.restore_hint === 'old' ? t('cr.ui.bk.restore.hint_old', { days: oldDays || 90 }) : '';
    const btn = LL.can('operate') ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async ev => {
      ev.currentTarget.disabled = true;
      try { await api('/api/backups/restore-check', { method: 'POST', body: { chain: c.id } }); LL.toast(t('cr.ui.bk.restore.noted'), 'ok'); reload(); }
      catch (e) { LL.toast(LL.errText(e), 'error'); ev.currentTarget.disabled = false; }
    } }, t('cr.ui.bk.restore.today')) : null;
    const undo = LL.can('operate') && c.restore_checked_t != null ? h('button', { class: 'btn btn--ghost btn--sm cr-chain__undo', type: 'button', onclick: async ev => {
      ev.currentTarget.disabled = true;
      try { await api('/api/backups/restore-check/undo', { method: 'POST', body: { chain: c.id } }); LL.toast(t('cr.ui.bk.restore.undone'), 'ok'); reload(); }
      catch (e) { LL.toast(LL.errText(e), 'error'); ev.currentTarget.disabled = false; }
    } }, t('cr.ui.bk.restore.undo')) : null;
    return h('div', { class: 'cr-chain__restore cr-chain__restore--' + lv }, mark(lv),
      h('span', { class: 'cr-chain__restore-text' }, h('span', {}, text), hint ? h('span', { class: 'muted' }, hint) : null),
      h('span', { class: 'cr-chain__restore-btns' }, btn, undo));
  }
  function chainRow(c, reload, opts) {
    const compact = !!(opts && opts.compact), oldDays = opts && opts.oldDays;
    const lv = STATUS_LEVEL[c.status] || 'unknown';
    const route = [c.source, c.target].filter(Boolean).join(' → ');
    const covers = (c.device_titles || []).length ? t('cr.ui.bk.covers', { names: c.device_titles.join(', ') }) : '';
    const meta = [route, covers, c.origin, c.kind ? word('cr.ui.bk.kind.', c.kind) : ''].filter(Boolean).join(' · ');
    const last = c.last_ok_t ? t('cr.ui.bk.last_ok', { age: LL.fmtAge(c.age_s != null ? c.age_s : Date.now() / 1000 - c.last_ok_t) }) : t('cr.ui.bk.no_ok');
    const lines = [last, t('cr.ui.bk.expect', { dur: LL.fmtAge(c.expect_s) })];
    if (c.last_fail_t && c.status === 'failed') lines.splice(1, 0, t('cr.ui.bk.last_fail', { age: LL.fmtAge(Date.now() / 1000 - c.last_fail_t) }));
    const p = c.status === 'running' && c.progress ? c.progress : null;
    const prog = p ? h('div', { class: 'cr-chain__progress' }, pctBar(p.pct, 'ok', t('cr.ui.bk.progress')),
      h('span', { class: 'cr-chain__pct mono' }, t('cr.ui.unit.pct', { v: num(p.pct, 0) })), p.basis ? h('span', { class: 'chip chip--muted' }, t('cr.ui.bk.basis.' + p.basis)) : null) : null;
    const acts = LL.can('operate') && c.editable && !compact ? h('span', { class: 'cr-chain__acts' },
      h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => chainEditor(c, reload) }, t('cr.ui.bk.edit')),
      h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', onclick: async ev => {
        if (!confirm(t('cr.ui.bk.delete_confirm', { name: c.name }))) return;
        ev.currentTarget.disabled = true;
        try { await api('/api/backups/chains/' + encodeURIComponent(c.id.replace(/^manual:/, '')), { method: 'DELETE' }); LL.toast(t('cr.ui.bk.deleted'), 'ok'); reload(); }
        catch (e) { LL.toast(LL.errText(e), 'error'); ev.currentTarget.disabled = false; }
      } }, t('cr.ui.bk.delete'))) : null;
    const setIgnored = async (ev, flag) => {
      ev.currentTarget.disabled = true;
      try { await api('/api/backups/ignore', { method: 'POST', body: { chain: c.id, ignored: flag } }); LL.toast(t(flag ? 'cr.ui.bk.ignored' : 'cr.ui.bk.unignored'), 'ok'); reload(); }
      catch (e) { LL.toast(LL.errText(e), 'error'); ev.currentTarget.disabled = false; }
    };
    if (opts && opts.ignored) {
      return h('li', { class: 'cr-chain cr-chain--ignored', dataset: { chain: c.id, status: 'ignored' } },
        h('div', { class: 'cr-chain__head' }, h('strong', { class: 'cr-chain__name' }, c.name),
          h('span', { class: 'cr-chain__side' }, LL.can('operate') ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: ev => setIgnored(ev, false) }, t('cr.ui.bk.unignore')) : null,
            chip('unknown', t('cr.ui.bk.ignore')))),
        meta ? h('div', { class: 'cr-chain__meta muted' }, meta) : null,
        c.ignored_t ? h('div', { class: 'cr-chain__when' }, h('span', {}, t('cr.ui.bk.ignored.since', { date: LL.fmtDate(c.ignored_t) }))) : null);
    }
    const skip = LL.can('operate') && !c.editable && !compact ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', title: t('cr.ui.bk.ignore.hint'), onclick: ev => setIgnored(ev, true) }, t('cr.ui.bk.ignore')) : null;
    return h('li', { class: 'cr-chain cr-chain--' + cls(lv) + (compact ? ' cr-chain--compact' : ''), dataset: { chain: c.id, status: c.status } },
      h('div', { class: 'cr-chain__head' }, h('strong', { class: 'cr-chain__name' }, c.name), h('span', { class: 'cr-chain__side' }, acts, skip, chip(lv, t('cr.ui.bk.status.' + c.status)))),
      meta ? h('div', { class: 'cr-chain__meta muted' }, meta) : null,
      h('div', { class: 'cr-chain__when' }, lines.map(l => h('span', {}, l))), prog,
      compact ? null : restoreLine(c, reload, oldDays));
  }
  function backupsSection(b, reload) {
    const sm = b.summary || {};
    const off = b.ignored || [];
    const chains = b.chains.slice().sort((a, c) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(c.status) || a.name.localeCompare(c.name));
    const counts = STATUS_ORDER.filter(s => sm[s]).map(s => h('span', { class: 'chip cr-chip chip--' + (cls(STATUS_LEVEL[s]) === 'unknown' ? 'muted' : cls(STATUS_LEVEL[s])) }, mark(STATUS_LEVEL[s]), t('cr.ui.bk.count', { n: sm[s], status: t('cr.ui.bk.status.' + s) })));
    const add = LL.can('operate') ? h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => chainEditor(null, reload) }, t('cr.ui.bk.add')) : null;
    const howto = LL.keepOpen.details('cr:howto', false, t('cr.ui.bk.howto.title'),
      h('div', { class: 'cr-howto' }, h('p', {}, t('cr.ui.bk.howto.text')), h('code', { class: 'cmd cr-howto__cmd' }, RUN_CMD + ' ' + location.origin + LL.base + RUN_PATH),
        h('p', { class: 'muted' }, t('cr.ui.bk.howto.token')), h('p', { class: 'muted' }, t('cr.ui.bk.howto.progress'))));
    return h('section', { class: 'card cr-card cr-sec', id: 'cr-backups', dataset: { sec: 'backups' }, tabindex: '-1', 'aria-labelledby': 'cr-h-backups' },
      h('div', { class: 'cr-sec__head' }, h('h2', { id: 'cr-h-backups' }, t('cr.ui.sec.backups')), add),
      h('p', { class: 'muted cr-lead' }, t('cr.ui.bk.lead')),
      counts.length ? h('div', { class: 'cr-counts' }, counts) : null,
      chains.length ? h('ul', { class: 'cr-chains cr-chains--grid' }, chains.map(c => chainRow(c, reload, { oldDays: b.restore_old_days }))) : h('p', { class: 'cr-calm muted' }, t(off.length ? 'cr.ui.bk.all_ignored' : 'cr.ui.bk.none')),
      off.length ? LL.keepOpen.details('cr:ignored', false, t('cr.ui.bk.ignored.title', { n: off.length }),
        h('div', { class: 'cr-ignored' }, h('p', { class: 'muted' }, t('cr.ui.bk.ignored.lead')), h('ul', { class: 'cr-chains cr-chains--grid' }, off.map(c => chainRow(c, reload, { ignored: true }))))) : null,
      howto);
  }

  // ------------------------------------------------------------------ storage boxes
  function ring(card) {
    const r = card.ring || {}, pct = r.pct;
    const C = 2 * Math.PI * 42;
    const pic = svgEl('svg', { class: 'cr-ring__pic', viewBox: '0 0 100 100', 'aria-hidden': 'true' });
    pic.append(svgEl('circle', { class: 'cr-ring__track', cx: 50, cy: 50, r: 42 }));
    if (pct != null) pic.append(svgEl('circle', { class: 'cr-ring__bar', cx: 50, cy: 50, r: 42, 'stroke-dasharray': fx(C * Math.max(0.02, Math.min(1, pct / 100))) + ' ' + fx(C), transform: 'rotate(-90 50 50)' }));
    return h('div', { class: 'cr-ring cr-ring--' + cls(r.level), role: 'img', 'aria-label': pct == null ? t('cr.ui.nas.ring_none') : t('cr.ui.nas.ring_aria', { pct: num(pct, 0), volume: r.volume || '' }) }, pic,
      h('div', { class: 'cr-ring__mid' }, h('b', {}, pct == null ? '—' : t('cr.ui.unit.pct', { v: num(pct, 0) })), h('span', {}, t('cr.ui.nas.full'))));
  }
  function volumeRow(v) {
    return h('li', { class: 'cr-vol cr-vol--' + cls(v.level) },
      h('div', { class: 'cr-vol__top' }, h('strong', { class: 'cr-vol__name' }, v.name), h('span', { class: 'cr-vol__size muted mono' }, t('cr.ui.nas.of', { used: LL.fmtBytes(v.used), total: LL.fmtBytes(v.total) })),
        h('span', { class: 'cr-vol__pct mono' }, t('cr.ui.unit.pct', { v: num(v.pct, 0) }))),
      pctBar(v.pct, v.level, v.name));
  }
  function nasCard(c, reload) {
    if (!c.reading) return h('article', { class: 'cr-nas cr-nas--none' }, h('header', { class: 'cr-nas__head' }, h('h3', {}, c.title), chip('unknown', t('cr.ui.nas.waiting'))), h('p', { class: 'muted' }, say('cr.nas.no_reading', { name: c.title })));
    const vit = Object.entries(c.vitals || {}).map(([k, v]) => h('div', { class: 'cr-vital cr-vital--' + cls(v.level), dataset: { vital: k } },
      h('span', { class: 'cr-vital__label muted' }, mark(v.level), t('cr.ui.vital.' + k)), h('strong', { class: 'mono' }, unitText(k === 'temp' ? 'hh.unit.celsius' : 'hh.unit.pct', v.value))));
    const ct = c.containers;
    const ctBlock = ct ? h('div', { class: 'cr-nas__sub' }, h('div', { class: 'cr-nas__subhead' }, h('h4', {}, t('cr.ui.nas.containers')),
      chip(ct.unhealthy ? 'warn' : 'ok', t('cr.ui.nas.running', { running: ct.running, total: ct.total }))),
    ct.items.length ? LL.keepOpen.details('cr:ct:' + c.device, false, t('cr.ui.nas.list', { n: ct.total }),
      h('ul', { class: 'cr-items' }, ct.items.map(i => h('li', {}, h('span', { class: 'cr-items__name' }, i.name),
        h('span', { class: 'chip chip--' + (i.state === 'running' ? 'ok' : i.state === 'restarting' ? 'warn' : 'muted') }, word('cr.ui.ct.state.', i.state)),
        i.health ? h('span', { class: 'chip chip--' + (i.health === 'healthy' ? 'ok' : i.health === 'unhealthy' ? 'warn' : 'muted') }, word('cr.ui.ct.health.', i.health)) : null)),
      ct.more ? h('li', { class: 'muted' }, t('cr.ui.more', { n: ct.more })) : null)) : null) : null;
    const g = c.guests;
    const gBlock = g ? h('div', { class: 'cr-nas__sub' }, h('div', { class: 'cr-nas__subhead' }, h('h4', {}, t('cr.ui.nas.guests')), chip(g.running === g.total ? 'ok' : 'unknown', t('cr.ui.nas.running', { running: g.running, total: g.total }))),
      LL.keepOpen.details('cr:vm:' + c.device, false, t('cr.ui.nas.list', { n: g.total }), h('ul', { class: 'cr-items' }, g.items.map(i => h('li', {}, h('span', { class: 'cr-items__name' }, i.name),
        h('span', { class: 'chip chip--muted' }, word('cr.ui.guest.', i.kind)), h('span', { class: 'chip chip--' + (i.status === 'running' ? 'ok' : 'muted') }, word('cr.ui.ct.state.', i.status))))))) : null;
    const bk = (c.backups || []).length ? h('div', { class: 'cr-nas__sub' }, h('h4', {}, t('cr.ui.nas.backups')), h('ul', { class: 'cr-chains' }, c.backups.map(x => chainRow(x, reload, { compact: true })))) : null;
    const ups = c.ups ? h('p', { class: 'cr-nas__ups cr-line cr-line--' + cls(c.ups.level) }, mark(c.ups.level), h('span', {}, upsLine(c.ups))) : null;
    const srcs = (c.sources || []).length ? h('ul', { class: 'cr-nas__sources' }, c.sources.map(s => h('li', { class: 'is-' + s.fresh }, h('span', { class: 'chip chip--muted' }, word('cr.ui.src.', s.source)), freshness(s.age_s, s.fresh)))) : null;
    return h('article', { class: 'cr-nas cr-nas--' + cls(c.level), dataset: { nas: c.device } },
      h('header', { class: 'cr-nas__head' }, h('h3', {}, c.title), chip(c.level)),
      h('div', { class: 'cr-nas__cols' },
        h('div', { class: 'cr-nas__col' }, h('div', { class: 'cr-nas__main' }, ring(c), h('ul', { class: 'cr-vols' }, (c.volumes || []).map(volumeRow))), vit.length ? h('div', { class: 'cr-vitals' }, vit) : null),
        ctBlock || gBlock || bk ? h('div', { class: 'cr-nas__col' }, ctBlock, gBlock, bk) : null),
      ups, srcs);
  }
  function nasSection(rows, reload) {
    const lv = rows.length ? worstOf(rows.map(r => ({ level: r.reading ? r.level : 'unknown' }))) : null;
    return h('section', { class: 'card cr-card cr-sec', id: 'cr-nas', dataset: { sec: 'nas' }, tabindex: '-1', 'aria-labelledby': 'cr-h-nas' }, sectionTitle('nas', lv),
      rows.length ? h('div', { class: 'cr-nas-grid' }, rows.map(c => nasCard(c, reload))) : h('p', { class: 'cr-calm muted' }, t('cr.ui.nas.none')));
  }

  // ------------------------------------------------------------------ battery backup
  function upsCard(u) {
    const onBattery = u.state === 'battery' || u.state === 'low_battery';
    const runtime = u.runtime_s == null ? null : LL.fillText(onBattery ? 'cr.ui.ups.runtime_left' : 'cr.ui.ups.runtime_if', { runtime_dur: Math.round(u.runtime_s) });
    const fact = (k, v) => (v == null ? null : h('div', {}, h('dt', {}, t('cr.ui.ups.f.' + k)), h('dd', { class: 'mono' }, v)));
    return h('article', { class: 'cr-ups cr-ups--' + cls(u.level) + ' is-' + (u.fresh || 'none'), dataset: { ups: u.id, state: u.state } },
      h('header', { class: 'cr-ups__head' }, h('div', {}, h('h3', {}, u.name), u.model ? h('span', { class: 'muted' }, u.model) : null), chip(u.level, t('cr.ui.ups.state.' + u.state))),
      h('div', { class: 'cr-ups__cols' }, h('div', { class: 'cr-ups__col' },
        u.charge != null ? h('div', { class: 'cr-ups__charge' }, h('div', { class: 'cr-ups__top' }, h('span', {}, t('cr.ui.ups.battery')), h('strong', { class: 'mono' }, t('cr.ui.unit.pct', { v: num(u.charge, 0) }))),
          pctBar(u.charge, u.charge < 20 ? 'danger' : u.charge < 50 ? 'warn' : 'ok', t('cr.ui.ups.battery'))) : null,
        runtime ? h('p', { class: 'cr-ups__runtime' }, runtime) : null,
        u.runtime_low ? note('warn', t('cr.ui.ups.runtime_low')) : null),
      h('dl', { class: 'cr-facts cr-ups__facts' },
        fact('load', u.load == null ? null : t('cr.ui.unit.pct', { v: num(u.load, 0) })), fact('input', u.input_v == null ? null : unitText('cr.ui.unit.v', u.input_v)),
        fact('output', u.output_v == null ? null : unitText('cr.ui.unit.v', u.output_v)), fact('battery_v', u.battery_v == null ? null : unitText('cr.ui.unit.v', u.battery_v)),
        fact('temp', u.temp == null ? null : unitText('hh.unit.celsius', u.temp)), fact('power', u.power_w == null ? null : unitText('cr.ui.unit.w', u.power_w)), fact('test', u.test || null))),
      h('div', { class: 'cr-ups__foot' }, freshness(u.age_s, u.fresh || 'none')));
  }
  function upsSection(units) {
    const lv = units.length ? worstOf(units) : null;
    return h('section', { class: 'card cr-card cr-sec', id: 'cr-ups', dataset: { sec: 'ups' }, tabindex: '-1', 'aria-labelledby': 'cr-h-ups' }, sectionTitle('ups', lv),
      units.length ? h('div', { class: 'cr-ups-grid' }, units.map(upsCard))
        : h('div', { class: 'cr-calm' }, h('p', { class: 'muted' }, t('cr.ui.ups.none')), h('a', { class: 'btn btn--ghost btn--sm', href: '#/settings' }, t('cr.ui.empty.add'))));
  }

  // ------------------------------------------------------------------ the freshness of every source
  function sourcesBar(list) {
    if (!list.length) return null;
    return h('section', { class: 'card cr-card cr-sources', 'aria-labelledby': 'cr-h-sources' }, h('h2', { id: 'cr-h-sources' }, t('cr.ui.sec.sources')), h('p', { class: 'muted cr-lead' }, t('cr.ui.sources.lead')),
      h('ul', { class: 'cr-srcs' }, list.map(s => h('li', { class: 'cr-src is-' + s.fresh, dataset: { source: s.id } },
        h('span', { class: 'grade-chip', dataset: { grade: s.grade } }, h('i'), t('board.grade.' + s.grade)),
        h('span', { class: 'cr-src__name' }, LL.strings[s.name] != null ? t(s.name) : s.name), freshness(s.age_s, s.fresh)))));
  }

  // ------------------------------------------------------------------ the page
  async function view(app, rest, name) {
    const jump = name === 'controlroom' ? (rest && rest[0]) : name;       // #/wan, #/nas, #/backups and #/controlroom/ups: the part to scroll to
    const fr = LL.frame(app, 'controlroom', h('div', { class: 'page page--wide cr-page' }));
    const page = fr.main.firstChild;
    const body = h('div', { class: 'cr-body' }, h('div', { class: 'skeleton' }));
    page.append(h('header', { class: 'page__head cr-head' }, h('div', {}, h('h1', {}, t('nav.controlroom')), h('p', { class: 'muted cr-lead' }, t('cr.ui.lead'))),
      LL.resilience ? h('a', { class: 'btn btn--sm cr-head__rs', href: '#/resilience' }, t('rs.cr.link')) : null), body);   // resilience and emergency scenarios (js/resilience.js)
    const ctx = { probe: LL.can('admin') ? probeForm(() => load()) : null };
    let seq = 0, first = true, timer = 0;
    async function load() {
      const my = ++seq;
      let d;
      try { d = await api('/api/controlroom'); }
      catch (e) { if (my === seq && first) body.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (my !== seq || !page.isConnected) return;
      const y = fr.main.scrollTop;
      const ae = document.activeElement;
      const typing = ae && body.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName);
      if (typing && !first) return;                                       // never redraw under a hand that is typing
      body.replaceChildren(hero(d), blocksGrid(d), pointsList(d), wanSection(d.wan, ctx), nasSection(d.nas, load), upsSection(d.ups), backupsSection(d.backups, load), sourcesBar(d.sources) || []);
      LL.controlroomPeek = { t: Date.now(), points: d.points, level: d.level };
      if (first) {
        first = false;
        const target = jump && body.querySelector('[data-sec="' + jump + '"]');
        if (target) { target.scrollIntoView({ block: 'start' }); target.focus({ preventScroll: true }); }
      } else fr.main.scrollTop = y;
    }
    await load();
    timer = setInterval(() => { if (!page.isConnected) { clearInterval(timer); return; } if (!document.hidden) load(); }, REFRESH_MS);
    const destroy = fr.destroy;
    fr.destroy = () => { clearInterval(timer); if (destroy) destroy.call(fr); };
    return fr;
  }
  LL.view('controlroom', (app, rest) => view(app, rest, 'controlroom'));
  LL.view('wan', (app, rest) => view(app, rest, 'wan'));
  LL.view('nas', (app, rest) => view(app, rest, 'nas'));
  LL.view('backups', (app, rest) => view(app, rest, 'backups'));

  // ------------------------------------------------------------------ the start page: only when something needs a look
  // The overview's own answer does not carry the control room, so the card asks for it (at most every 30 s) and the page redraws once.
  let peekAt = 0, peeking = false;
  function peek() {
    if (peeking || Date.now() - peekAt < 30000) return;
    peeking = true; peekAt = Date.now();
    api('/api/controlroom').then(d => {
      const had = (LL.controlroomPeek || { points: [] }).points.length;
      LL.controlroomPeek = { t: Date.now(), points: d.points, level: d.level };
      if (had !== d.points.length && LL.overviewRedraw) LL.overviewRedraw();
    }).catch(() => null).finally(() => { peeking = false; });
  }
  if (LL.registerOverviewCard) {
    LL.registerOverviewCard({ id: 'controlroom', order: 13, title: () => t('nav.controlroom'),
      available: () => { peek(); return !!LL.controlroomPeek && LL.controlroomPeek.points.length > 0; },
      count: () => LL.controlroomPeek.points.length,
      render: () => {
        const pts = LL.controlroomPeek.points;
        return h('div', { class: 'cr-ov' }, h('ul', { class: 'note-list' }, pts.slice(0, 5).map(p => h('li', { class: 'note-row note-row--' + (p.level === 'danger' ? 'danger' : 'warn') },
          h('a', { class: 'note-row__link', href: HREF[p.block] || '#/controlroom' }, h('i', { class: 'note-row__dot', 'aria-hidden': 'true' }),
            h('span', { class: 'note-row__main' }, h('span', { class: 'note-row__title' }, say(p.key, p.vars)), h('span', { class: 'note-row__sub' }, t('cr.ui.block.' + p.block))))))),
        pts.length > 5 ? h('p', { class: 'muted' }, t('cr.ui.more', { n: pts.length - 5 })) : null,
        h('div', { class: 'btn-row btn-row--wrap' }, h('a', { class: 'btn btn--ghost btn--sm', href: '#/controlroom' }, t('cr.ui.open'))));
      } });
  }

  // ------------------------------------------------------------------ palette
  if (LL.registerCommand) {
    const go = (id, order, name, hash) => LL.registerCommand({ id, group: 'views', order, label: () => t('cmdk.go', { name: t(name) }), keywords: () => t('cr.ui.cmd.kw.' + id.slice(3)), run: () => LL.go(hash) });
    go('go.controlroom', 3.6, 'nav.controlroom', '#/controlroom');
    go('go.internet', 3.61, 'cr.ui.sec.wan', '#/wan');
    go('go.storage', 3.62, 'cr.ui.sec.nas', '#/nas');
    go('go.backups', 3.63, 'cr.ui.sec.backups', '#/backups');
  }
})();
