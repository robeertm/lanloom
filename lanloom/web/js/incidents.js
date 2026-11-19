/* Lanloom — incidents (S8): incident files, "What happened?", the 90-day calendar, the hour heatmap, the event chronicle and the
   timeline & data state.

   Data: GET /api/incidents?days=&kind=, /api/incidents/<id>, PUT /api/incidents/<id> {kind: maintenance|null, note} (operators),
   /api/incidents/calendar, /api/incidents/heatmap, /api/incidents/events?hours=, /api/incidents/night, /api/incidents/chronicle,
   /api/incidents/what/<part> (lanloom/incidents.py, lanloom/chronicle.py). Everything is rule based and only reads.

   Page #/incidents: the calendar of the last 90 days and the heatmap of the last 7 days side by side, then three tabs -
   Incidents (filter by range and kind), Events (the journal of the last 24 hours / 7 days) and Timeline & data state (firmware
   jumps, own updates, new devices, long outages). #/incidents/<id> is one file: the probable root with its certainty and
   reason, what changed before, the time line, the measurements (availability around it), the affected parts, the healing;
   buttons "Play in rewind" (js/rewind.js) and "Failure chain" (js/failure.js), and the operator's mark "maintenance".
   The device card gets "What happened?" (LL.whatHappenedSection), the start page a key figure and the card "Incidents of
   the night" (also the phone card), the palette its commands. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const KINDS = ['outage', 'flapping', 'blip', 'maintenance'];
  const DAYS = [1, 7, 30, 90];
  const CHIP = { outage: 'danger', flapping: 'warn', blip: 'warn', maintenance: 'muted' };
  const enc = encodeURIComponent;
  const loc = () => LL.locale();
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const when = (ts, o) => new Date(ts * 1000).toLocaleString(loc(), o || { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
  const clock = ts => new Date(ts * 1000).toLocaleTimeString(loc(), { hour: '2-digit', minute: '2-digit', hour12: false });
  const dur = s => LL.fmtAge(Math.max(1, s));
  const ago = ts => t('time.ago', { age: LL.fmtAge(Math.max(1, Date.now() / 1000 - ts)) });

  // ------------------------------------------------------------------ names and texts
  function rootName(r) {
    if (!r) return '';
    if (r.id === 'lanloom') return 'Lanloom';
    if (r.id === 'internet' || r.kind === 'internet') return t('kind.internet');
    return r.title || r.ip || t('kind.' + (r.kind || 'unknown'));
  }
  LL.incidentName = rootName;
  function title(i) {
    if (i.kind === 'maintenance' && i.why_kind === 'update') return t('inc.title.update', { to: ((i.root.why || {}).vars || {}).to || '' });
    return t(i.root.observed === false ? 'inc.title.guess.' + i.kind : 'inc.title.' + i.kind, { name: rootName(i.root) });
  }
  function certChip(cert, guess) {
    return h('span', { class: 'chip chip--' + (cert === 'sure' ? 'accent' : 'warn') + ' inc-cert' }, t(guess ? 'inc.cert.guess' : 'inc.cert.' + cert));
  }
  function whyText(why) {
    if (!why) return '';
    const v = why.vars || {};
    if (why.key === 'shared') return t('inc.why.shared', { n: v.n || 2 });
    if (why.key === 'update') return t('inc.why.update', { from: v.from || '', to: v.to || '' });
    return t(why.key === 'port' && v.port ? 'fail.how.port_n' : 'fail.how.' + why.key, { port: v.port || '' });
  }
  function fill(key, vars) {           // variables ending in _key are text keys, _dur durations, _at times
    const out = {};
    Object.keys(vars || {}).forEach(k => {
      const v = vars[k];
      if (/_key$/.test(k)) out[k.slice(0, -4)] = v ? t(v) : '';
      else if (/_dur$/.test(k)) out[k.slice(0, -4)] = dur(v);
      else if (/_at$/.test(k)) out[k.slice(0, -3)] = clock(v);
      else out[k] = v;
    });
    return t(key, out);
  }
  // the certainty matters where parts were bundled or the root is only probable; one device on its own is just itself
  const showCert = i => i.kind !== 'maintenance' && (i.root.observed === false || (i.counts && i.counts.members > 0));
  function kindChip(k) { return h('span', { class: 'chip chip--' + (CHIP[k] || 'muted') + ' inc-kind' }, t('inc.kind.' + k)); }
  function lineOf(i) {
    const bits = [when(i.start), i.open ? t('inc.open_for', { dur: dur(i.dur) }) : t('inc.lasted', { dur: dur(i.dur) })];
    if (i.counts && i.counts.members) bits.push(t('inc.members_n', { n: i.counts.members }));
    return bits.join(' · ');
  }
  function rewindAt(i) { return Math.max(i.start - 15 * 60, Date.now() / 1000 - 7 * 86400 + 60); }
  function rewindRange(i) { return Date.now() / 1000 - i.start > 23 * 3600 ? '7d' : '24h'; }
  function playButton(i, small) {
    if (Date.now() / 1000 - i.start > 7 * 86400 - 600) return null;              // the rewind reaches back 7 days
    return h('button', { class: 'btn btn--sm ' + (small ? 'btn--ghost ' : '') + 'inc-play', type: 'button', onclick: () => LL.rewind && LL.rewind.open(rewindAt(i), rewindRange(i)) }, t('inc.btn.play'));
  }
  function chainButton(i) {
    if (!LL.failure || i.root.id === 'lanloom' || (i.chain_ok === false)) return null;
    return h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.failure.show(i.root.id, 'down') }, t('inc.btn.chain'));
  }

  // ------------------------------------------------------------------ the list
  function row(i) {
    return h('li', { class: 'inc-row inc-row--' + i.kind + (i.open ? ' is-open' : '') },
      h('a', { class: 'inc-row__link', href: '#/incidents/' + enc(i.id) },
        h('span', { class: 'inc-row__mark', 'aria-hidden': 'true' }),
        h('span', { class: 'inc-row__main' },
          h('span', { class: 'inc-row__title' }, title(i)),
          h('span', { class: 'inc-row__sub muted' }, lineOf(i))),
        h('span', { class: 'inc-row__chips' }, i.open ? h('span', { class: 'chip chip--danger' }, t('inc.open')) : null, kindChip(i.kind),
          showCert(i) ? certChip(i.root.cert, i.root.observed === false) : null)));
  }

  // ------------------------------------------------------------------ calendar (90 days) and heatmap (7 days x 24 hours)
  function level(n, max) { if (!n) return 0; if (max <= 1) return 3; const r = n / max; return r > 0.66 ? 4 : r > 0.33 ? 3 : r > 0.15 ? 2 : 1; }
  function calendar(cal, onPick) {
    const days = cal.days;
    const first = new Date(days[0].day + 'T12:00:00');
    const lead = (first.getDay() + 6) % 7;                         // weeks start on Monday
    const cells = [];
    for (let k = 0; k < lead; k++) cells.push(h('span', { class: 'inc-cal__cell is-pad', 'aria-hidden': 'true' }));
    days.forEach(d => {
      const lv = level(d.faults, cal.max);
      const label = new Date(d.day + 'T12:00:00').toLocaleDateString(loc(), { weekday: 'short', day: 'numeric', month: 'short' }) + ': ' +
        (d.faults ? t('inc.cal.faults', { n: d.faults }) : t('inc.cal.none')) + (d.kinds.maintenance ? ' · ' + t('inc.cal.maint', { n: d.kinds.maintenance }) : '') +
        (d.down_s ? ' · ' + t('inc.cal.down', { dur: dur(d.down_s) }) : '');
      cells.push(h('button', { class: 'inc-cal__cell lv' + lv + (d.kinds.maintenance && !d.faults ? ' is-maint' : ''), type: 'button', title: label, 'aria-label': label,
        onclick: () => onPick(d.day) }));
    });
    const weeks = Math.ceil(cells.length / 7);
    const months = [];
    let last = '';
    days.forEach((d, idx) => {
      const m = new Date(d.day + 'T12:00:00').toLocaleDateString(loc(), { month: 'short' });
      if (m !== last) { months.push(h('span', { class: 'inc-cal__month', style: 'grid-column:' + (Math.floor((idx + lead) / 7) + 1) }, m)); last = m; }
    });
    return h('div', { class: 'inc-cal' },
      h('div', { class: 'inc-cal__months', style: 'grid-template-columns:repeat(' + weeks + ',1fr)', 'aria-hidden': 'true' }, months),
      h('div', { class: 'inc-cal__grid', style: 'grid-template-columns:repeat(' + weeks + ',1fr)', role: 'group', 'aria-label': t('inc.cal.title') }, cells),
      h('div', { class: 'inc-scale muted', 'aria-hidden': 'true' }, t('inc.scale.less'), [0, 1, 2, 3, 4].map(l => h('span', { class: 'inc-cal__cell lv' + l })), t('inc.scale.more'),
        h('span', { class: 'inc-cal__cell is-maint' }), t('inc.kind.maintenance')));
  }
  function heatmap(hm) {
    const head = h('div', { class: 'inc-heat__row inc-heat__row--head', 'aria-hidden': 'true' }, h('span', { class: 'inc-heat__day' }),
      Array.from({ length: 24 }, (_, i) => h('span', { class: 'inc-heat__hour' }, i % 3 === 0 ? String(i).padStart(2, '0') : '')));
    const rows = hm.days.map((day, r) => h('div', { class: 'inc-heat__row' },
      h('span', { class: 'inc-heat__day' }, new Date(day + 'T12:00:00').toLocaleDateString(loc(), { weekday: 'short' })),
      hm.down[r].map((n, hr) => {
        const ev = hm.events[r][hr];
        const label = new Date(day + 'T12:00:00').toLocaleDateString(loc(), { weekday: 'short', day: 'numeric' }) + ' ' + String(hr).padStart(2, '0') + ':00 · ' +
          t('inc.heat.down', { n }) + ' · ' + t('inc.heat.events', { n: ev });
        return h('span', { class: 'inc-heat__cell lv' + level(n, hm.max) + (!n && ev ? ' has-ev' : ''), title: label, role: 'img', 'aria-label': label });
      })));
    return h('div', { class: 'inc-heat' }, head, rows,
      h('div', { class: 'inc-scale muted', 'aria-hidden': 'true' }, t('inc.heat.legend_down'), [1, 2, 3, 4].map(l => h('span', { class: 'inc-heat__cell lv' + l })),
        h('span', { class: 'inc-heat__cell has-ev' }), t('inc.heat.legend_events')));
  }

  // ------------------------------------------------------------------ events and timeline & data state
  const EV_KEY = { dev_new: 'inc.ev.dev_new', dev_offline: 'inc.ev.dev_offline', dev_back: 'inc.ev.dev_back', dev_gone: 'inc.ev.dev_gone', src_fail: 'inc.ev.src_fail',
    src_ok: 'inc.ev.src_ok', wan_down: 'inc.ev.wan_down', wan_up: 'inc.ev.wan_up', wan_switch: 'inc.ev.wan_switch', app_start: 'inc.ev.app_start', fw_change: 'inc.ev.fw_change',
    inv_expired: 'inc.ev.inv_expired' };
  const EV_LEVEL = { dev_offline: 'warn', dev_gone: 'warn', src_fail: 'warn', wan_down: 'danger', dev_back: 'ok', src_ok: 'ok', wan_up: 'ok' };
  function eventList(items) {
    if (!items.length) return h('p', { class: 'muted inc-empty' }, t('inc.ev.none'));
    return h('ul', { class: 'inc-evs' }, items.map(e => h('li', { class: 'inc-ev inc-ev--' + (EV_LEVEL[e.kind] || 'info') },
      h('span', { class: 'inc-ev__time' }, when(e.t, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })),
      h('span', { class: 'inc-ev__dot', 'aria-hidden': 'true' }),
      h('span', { class: 'inc-ev__text' }, e.kind === 'app_start' ? t(e.vars && e.vars.prev && e.vars.prev !== e.vars.version ? 'inc.ev.app_update' : 'inc.ev.app_start', { version: (e.vars || {}).version || '', prev: (e.vars || {}).prev || '' })
        : (EV_KEY[e.kind] ? t(EV_KEY[e.kind], { name: e.name || '—' }) : (e.name || e.kind)),
      e.id && /^d-/.test(e.id) ? [' ', h('a', { class: 'inc-ev__link', href: '#/devices/' + enc(e.id) }, t('inc.ev.device'))] : null,
      e.incident ? [' ', h('a', { class: 'inc-ev__link', href: '#/incidents/' + enc(e.incident) }, t('inc.ev.file'))] : null))));
  }
  const CH_ICON = { firmware: 'chip--accent', version: 'chip--muted', new_device: 'chip--ok', long_outage: 'chip--danger' };
  function chronicleList(items) {
    if (!items.length) return h('p', { class: 'muted inc-empty' }, t('inc.ch.none'));
    return h('ul', { class: 'inc-chs' }, items.map(r => {
      const d = r.data || {};
      let text;
      if (r.kind === 'firmware') text = t('inc.ch.firmware', { name: d.title || '—', from: d.from || '?', to: d.to || '?' });
      else if (r.kind === 'version') text = t('inc.ch.version', { from: d.from || '?', to: d.to || '?' });
      else if (r.kind === 'new_device') text = t('inc.ch.new_device', { name: d.title || d.ip || '—' });
      else text = t('inc.ch.long_outage', { name: d.title || '—', dur: dur(d.dur || 0) });
      const link = r.kind === 'long_outage' && d.inc ? h('a', { class: 'inc-ev__link', href: '#/incidents/' + enc(d.inc) }, t('inc.ev.file'))
        : /^d-/.test(r.subject) ? h('a', { class: 'inc-ev__link', href: '#/devices/' + enc(r.subject) }, t('inc.ev.device')) : null;
      return h('li', { class: 'inc-ch' }, h('span', { class: 'inc-ev__time' }, when(r.t, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false })),
        h('span', { class: 'chip ' + (CH_ICON[r.kind] || 'chip--muted') + ' inc-ch__kind' }, t('inc.ch.kind.' + r.kind)), h('span', { class: 'inc-ev__text' }, text, link ? [' ', link] : null));
    }));
  }

  // ------------------------------------------------------------------ the page
  async function listView(app) {
    const fr = LL.frame(app, 'incidents', h('div', { class: 'page page--wide inc-page' }));
    const page = fr.main.firstChild;
    const q = () => ({ days: Number(LL.query.get('days')) || 7, kind: LL.query.get('kind') || '', tab: LL.query.get('tab') || 'list', hours: Number(LL.query.get('hours')) || 24 });
    const top = h('div', { class: 'inc-top' });
    const tabs = h('div', { class: 'inc-tabs' });
    const body = h('div', { class: 'inc-body' });
    page.append(
      h('header', { class: 'page__head inc-head' },
        h('div', {}, h('h1', {}, t('nav.incidents')), h('p', { class: 'muted inc-lead' }, t('inc.page.lead'))),
        h('div', { class: 'btn-row' }, LL.rewind ? h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => LL.rewind.open() }, t('inc.btn.rewind24')) : null,
          LL.rewind ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.rewind.open(null, '7d') }, t('inc.btn.rewind7')) : null)),
      top, tabs, body);
    const set = (k, v) => { if (v == null || v === '') LL.query.delete(k); else LL.query.set(k, String(v)); LL.deepLink('#/incidents' + LL.queryString(), true); draw(); };
    let seq = 0;
    async function drawTop() {
      let cal, hm;
      try { [cal, hm] = await Promise.all([api('/api/incidents/calendar'), api('/api/incidents/heatmap')]); }
      catch (e) { top.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); return; }
      const pickDay = day => { LL.query.set('tab', 'list'); LL.query.set('days', '90'); LL.query.set('day', day); LL.deepLink('#/incidents' + LL.queryString(), true); draw(); };
      top.replaceChildren(
        h('section', { class: 'card inc-card inc-card--cal', 'aria-label': t('inc.cal.title') }, h('h2', { class: 'inc-card__title' }, t('inc.cal.title')),
          h('p', { class: 'muted inc-card__sub' }, t('inc.cal.sub', { n: cal.days.reduce((a, d) => a + d.faults, 0) })), calendar(cal, pickDay)),
        h('section', { class: 'card inc-card inc-card--heat', 'aria-label': t('inc.heat.title') }, h('h2', { class: 'inc-card__title' }, t('inc.heat.title')),
          h('p', { class: 'muted inc-card__sub' }, t('inc.heat.sub')), heatmap(hm)));
    }
    async function draw() {
      const my = ++seq, f = q();
      tabs.replaceChildren(h('div', { class: 'seg inc-tabseg', role: 'tablist', 'aria-label': t('inc.tabs') }, ['list', 'events', 'chronicle'].map(k => h('button', {
        class: 'seg__btn' + (f.tab === k ? ' is-active' : ''), type: 'button', role: 'tab', 'aria-selected': f.tab === k ? 'true' : 'false',
        onclick: () => { LL.query.delete('day'); set('tab', k === 'list' ? '' : k); } }, t('inc.tab.' + k)))));
      body.replaceChildren(h('p', { class: 'muted' }, t('inc.loading')));
      try {
        if (f.tab === 'events') {
          const d = await api('/api/incidents/events?hours=' + f.hours);
          if (my !== seq) return;
          body.replaceChildren(h('div', { class: 'inc-filter' }, h('div', { class: 'seg', role: 'group', 'aria-label': t('inc.range') }, [24, 168].map(n => h('button', {
            class: 'seg__btn' + (f.hours === n ? ' is-active' : ''), type: 'button', 'aria-pressed': f.hours === n ? 'true' : 'false', onclick: () => set('hours', n === 24 ? '' : n) }, t(n === 24 ? 'inc.range.24h' : 'inc.range.7d'))))),
          h('section', { class: 'card inc-card' }, eventList(d.items)));
          return;
        }
        if (f.tab === 'chronicle') {
          const d = await api('/api/incidents/chronicle?days=90');
          if (my !== seq) return;
          const kinds = ['firmware', 'version', 'new_device', 'long_outage'];
          const pick = LL.query.get('ch') || '';
          const items = pick ? d.items.filter(r => r.kind === pick) : d.items;
          body.replaceChildren(h('div', { class: 'inc-filter' }, h('div', { class: 'inc-chips', role: 'group', 'aria-label': t('inc.ch.filter') },
            h('button', { class: 'mf-chip' + (!pick ? ' is-on' : ''), type: 'button', 'aria-pressed': !pick ? 'true' : 'false', onclick: () => set('ch', '') }, t('inc.all')),
            kinds.map(k => h('button', { class: 'mf-chip' + (pick === k ? ' is-on' : ''), type: 'button', 'aria-pressed': pick === k ? 'true' : 'false', onclick: () => set('ch', pick === k ? '' : k) },
              t('inc.ch.kind.' + k) + ' · ' + (d.counts[k] || 0))))),
          h('section', { class: 'card inc-card' }, h('p', { class: 'muted inc-card__sub' }, t('inc.ch.sub', { when: d.collected ? ago(d.collected) : '—' })), chronicleList(items)));
          return;
        }
        const d = await api('/api/incidents?days=' + f.days + (f.kind ? '&kind=' + enc(f.kind) : ''));
        if (my !== seq) return;
        const day = LL.query.get('day');
        let items = d.items;
        if (day) items = items.filter(i => { const x = new Date(i.start * 1000); return x.getFullYear() + '-' + String(x.getMonth() + 1).padStart(2, '0') + '-' + String(x.getDate()).padStart(2, '0') === day; });
        const filter = h('div', { class: 'inc-filter' },
          h('div', { class: 'seg', role: 'group', 'aria-label': t('inc.range') }, DAYS.map(n => h('button', { class: 'seg__btn' + (f.days === n && !day ? ' is-active' : ''), type: 'button',
            'aria-pressed': f.days === n && !day ? 'true' : 'false', onclick: () => { LL.query.delete('day'); set('days', n === 7 ? '' : n); } }, t('inc.days.' + n)))),
          h('div', { class: 'inc-chips', role: 'group', 'aria-label': t('inc.kind.filter') },
            h('button', { class: 'mf-chip' + (!f.kind ? ' is-on' : ''), type: 'button', 'aria-pressed': !f.kind ? 'true' : 'false', onclick: () => set('kind', '') }, t('inc.all')),
            KINDS.map(k => h('button', { class: 'mf-chip inc-chip--' + k + (f.kind === k ? ' is-on' : ''), type: 'button', 'aria-pressed': f.kind === k ? 'true' : 'false',
              onclick: () => set('kind', f.kind === k ? '' : k) }, t('inc.kinds.' + k) + ' · ' + (d.counts[k] || 0)))),
          day ? h('span', { class: 'chip chip--accent inc-daychip' }, new Date(day + 'T12:00:00').toLocaleDateString(loc(), { weekday: 'short', day: 'numeric', month: 'short' }), ' ',
            h('button', { class: 'inc-daychip__x', type: 'button', 'aria-label': t('inc.day.clear'), onclick: () => { LL.query.delete('day'); set('days', ''); } }, '✕')) : null);
        body.replaceChildren(filter, items.length ? h('ul', { class: 'inc-list' }, items.map(row))
          : h('div', { class: 'empty-note inc-empty' }, t(f.kind || day ? 'inc.none_filtered' : 'inc.none')),
        h('p', { class: 'fineprint muted inc-how' }, t('inc.how')));
      } catch (e) { if (my === seq) body.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); }
    }
    drawTop(); await draw();
    return fr;
  }

  // ------------------------------------------------------------------ one file
  function strip(s) {
    if (!s || !s.slots || !s.slots.length) return null;
    const canvas = h('canvas', { class: 'inc-strip', height: '24', role: 'img', 'aria-label': t('inc.file.strip_aria') });
    const draw = () => {
      if (!canvas.isConnected) return;
      const dpr = window.devicePixelRatio || 1, w = Math.max(120, canvas.clientWidth), hh = 24;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(hh * dpr);
      const g = canvas.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0);
      const cw = w / s.slots.length;
      s.slots.forEach((b, i) => {
        const u = b >> 4, d = b & 15;
        g.fillStyle = !u && !d ? css('--ll-surface-3') : !d ? css('--ll-ok') : u ? css('--ll-warn') : css('--ll-danger');
        g.fillRect(Math.floor(i * cw), 0, Math.max(1, Math.ceil(cw) - (cw > 3 ? 1 : 0)), hh);
      });
    };
    requestAnimationFrame(draw);
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(draw).observe(canvas);
    const end = s.t0 + s.slots.length * s.step;
    return h('div', { class: 'inc-stripbox' }, canvas, h('div', { class: 'inc-strip__axis muted' }, h('span', {}, clock(s.t0)), h('span', {}, clock(end))));
  }
  function section(key, ...kids) { return h('section', { class: 'card inc-sec', 'aria-label': t(key) }, h('h2', { class: 'inc-sec__title' }, t(key)), ...kids); }
  function beforeList(items) {
    if (!items.length) return h('p', { class: 'muted' }, t('inc.before.none'));
    return h('ul', { class: 'inc-evs' }, items.map(b => h('li', { class: 'inc-ev' + (b.near ? ' is-near' : '') },
      h('span', { class: 'inc-ev__time' }, when(b.t, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })), h('span', { class: 'inc-ev__dot', 'aria-hidden': 'true' }),
      h('span', { class: 'inc-ev__text' }, fill(b.key, b.vars)))));
  }
  async function fileView(app, id) {
    const fr = LL.frame(app, 'incidents', h('div', { class: 'page inc-file' }));
    const page = fr.main.firstChild;
    let i;
    try { i = await api('/api/incidents/' + enc(id)); }
    catch (e) {
      page.append(h('p', {}, h('a', { href: '#/incidents' }, '← ' + t('inc.page.title'))), h('div', { class: 'empty-note' }, e.status === 404 ? t('inc.err.unknown') : LL.errText(e)));
      return fr;
    }
    const guess = i.root.observed === false;
    const markBox = h('div', { class: 'inc-mark' });
    function drawMark() {
      const m = i.mark;
      const kids = [];
      if (m && (m.kind || m.note)) kids.push(h('p', { class: 'inc-mark__now' }, m.kind === 'maintenance' ? h('span', { class: 'chip chip--muted' }, t('inc.mark.is_maint')) : null, m.note ? ' ' + m.note : ''));
      if (LL.can('operate') && i.why_kind !== 'update' && i.why_kind !== 'window') {
        const note = h('input', { class: 'input-sm inc-mark__note', type: 'text', maxlength: '160', value: (m && m.note) || '', placeholder: t('inc.mark.note'), 'aria-label': t('inc.mark.note') });
        const save = async kind => {
          try { const r = await api('/api/incidents/' + enc(i.id), { method: 'PUT', body: { kind, note: note.value } }); i.mark = r.mark; LL.toast(t('inc.mark.saved'), 'ok'); LL.go('#/incidents/' + enc(i.id)); }
          catch (e) { LL.toast(LL.errText(e), 'error'); }
        };
        kids.push(h('div', { class: 'inc-mark__row' }, note,
          m && m.kind === 'maintenance' ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => save(null) }, t('inc.mark.unmaint'))
            : h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => save('maintenance') }, t('inc.mark.maint')),
          h('button', { class: 'btn btn--sm', type: 'button', onclick: () => save(m && m.kind ? m.kind : null) }, t('inc.mark.save_note'))));
        kids.push(h('p', { class: 'fineprint muted' }, t('inc.mark.help')));
      }
      markBox.replaceChildren(...kids);
    }
    drawMark();
    const heal = i.heal ? fill(i.heal.key, i.heal.vars) : '';
    const rootLink = /^d-/.test(i.root.id) ? '#/devices/' + enc(i.root.id) : i.root.id === 'internet' ? '#/map/internet' : null;
    page.append(
      h('p', { class: 'inc-back' }, h('a', { href: '#/incidents' }, '← ' + t('inc.page.title'))),
      h('header', { class: 'inc-file__head inc-file__head--' + i.kind },
        h('div', { class: 'inc-file__titles' },
          h('div', { class: 'inc-file__chips' }, i.open ? h('span', { class: 'chip chip--danger' }, t('inc.open')) : null, kindChip(i.kind), showCert(i) ? certChip(i.root.cert, guess) : null),
          h('h1', {}, title(i)),
          h('p', { class: 'muted' }, lineOf(i) + (heal ? ' · ' + heal : ''))),
        h('div', { class: 'btn-row inc-file__btns' }, playButton(i), chainButton(i))),
      h('div', { class: 'inc-file__grid' },
        section('inc.file.root',
          h('p', { class: 'inc-root' }, rootLink ? h('a', { href: rootLink, class: 'inc-root__name' }, rootName(i.root)) : h('b', { class: 'inc-root__name' }, rootName(i.root)), ' ',
            showCert(i) ? certChip(i.root.cert, guess) : null),
          h('p', { class: 'muted' }, guess ? t('inc.file.root_guess', { n: i.counts.members, why: whyText(i.root.why) })
            : i.why_kind === 'update' ? whyText(i.root.why)
            : i.why_kind === 'window' ? t('inc.file.root_window', { name: (i.window || {}).name || '' })
            : i.why_kind === 'marked' ? t('inc.file.root_marked')
            : i.counts.members ? t('inc.file.root_sure', { n: i.counts.members }) : t('inc.file.root_alone')),
          i.kind === 'flapping' ? h('p', { class: 'muted' }, t('inc.file.flapping', { n: i.runs })) : null,
          i.kind === 'blip' ? h('p', { class: 'muted' }, t('inc.file.blip')) : null),
        section('inc.file.before', h('p', { class: 'muted inc-sec__lead' }, t('inc.file.before_lead')), beforeList(i.before || [])),
        section('inc.file.timeline', h('ul', { class: 'inc-evs inc-tl' }, (i.timeline || []).map(e => h('li', { class: 'inc-ev inc-ev--' + (e.level || 'info') },
          h('span', { class: 'inc-ev__time' }, when(e.t, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })), h('span', { class: 'inc-ev__dot', 'aria-hidden': 'true' }),
          h('span', { class: 'inc-ev__text' }, fill(e.key, e.vars), e.cert === 'likely' ? h('span', { class: 'muted' }, ' · ' + t('inc.cert.likely')) : e.cert === 'mixed' ? h('span', { class: 'muted' }, ' · ' + t('inc.cert.mixed')) : null))))),
        i.strip && i.strip.slots ? section('inc.file.measure', h('p', { class: 'muted inc-sec__lead' }, t('inc.file.measure_lead', { share: i.strip.share == null ? '—' : LL.fmtNum(i.strip.share * 100, 1) + ' %' })), strip(i.strip),
          h('div', { class: 'inc-legend muted' }, ['up', 'partial', 'down', 'unknown'].map(k => h('span', { class: 'inc-legend__key inc-legend__key--' + k }, t('avail.slot.' + k))))) : null,
        i.members && i.members.length ? section('inc.file.members', h('p', { class: 'muted inc-sec__lead' }, t('inc.file.members_lead', { n: i.counts.members, sure: i.counts.sure, likely: i.counts.likely })),
          h('ul', { class: 'fc-rows inc-members' }, i.members.map(m => h('li', { class: 'fc-row fc-row--' + m.cert }, LL.kindIcon(m.kind || 'unknown'),
            h('span', { class: 'fc-row__main' }, /^d-/.test(m.id) ? h('a', { class: 'fc-row__name', href: '#/devices/' + enc(m.id) }, m.title || t('kind.' + m.kind)) : h('span', { class: 'fc-row__name' }, m.title || m.id),
              h('span', { class: 'fc-row__why' }, [whyText(m.why), clock(m.from) + (m.to ? '–' + clock(m.to) : ' – ' + t('inc.still'))].filter(Boolean).join(' · '))),
            h('span', { class: 'chip chip--' + (m.cert === 'sure' ? 'accent' : 'warn') + ' fc-row__cert' }, t('inc.cert.' + m.cert))))),
          i.counts.members > i.members.length ? h('p', { class: 'muted' }, t('fail.more', { n: i.counts.members - i.members.length })) : null) : null,
        section('inc.file.heal', h('p', {}, heal || '—'), markBox)));
    return fr;
  }
  LL.view('incidents', async function (app, rest) {
    if (rest && rest[0]) return fileView(app, decodeURIComponent(rest[0]));
    return listView(app);
  });

  // ------------------------------------------------------------------ the device card: "What happened?"
  LL.whatHappenedSection = function (id) {
    const box = h('div', { class: 'inc-what' });
    const sec = h('section', { class: 'detail__sec inc-what-sec', 'data-sec': 'what-happened', hidden: true }, h('h3', {}, t('inc.what.title')), box);
    api('/api/incidents/what/' + enc(id)).then(d => {
      if (!d || d.calm || !d.incident) { sec.remove(); return; }
      const i = d.incident;
      const kids = [];
      if (d.is_root) kids.push(h('p', {}, h('strong', {}, t(i.open ? 'inc.what.root_open' : 'inc.what.root_was', { since: clock(i.start), ago: ago(i.start) }))));
      else kids.push(h('p', {}, h('strong', {}, t(i.root.observed === false ? 'inc.what.member_guess' : 'inc.what.member', { name: rootName(i.root), since: clock(d.member ? d.member.from : i.start) })),
        ' ', certChip(d.member ? d.member.cert : i.root.cert, i.root.observed === false)));
      if (i.counts.members && d.is_root) kids.push(h('p', { class: 'muted' }, t('inc.what.took', { n: i.counts.members })));
      kids.push(h('p', { class: 'muted inc-what__before' }, (d.before || []).length ? t('inc.what.before', { n: d.before.length, first: fill(d.before[0].key, d.before[0].vars) }) : t('inc.what.before_none')));
      kids.push(h('div', { class: 'btn-row btn-row--wrap' }, h('a', { class: 'btn btn--ghost btn--sm', href: '#/incidents/' + enc(i.id) }, t('inc.btn.file')), playButton(i, true), chainButton(i)));
      box.replaceChildren(...kids);
      sec.hidden = false;
    }).catch(() => sec.remove());
    return sec;
  };

  // ------------------------------------------------------------------ start page: key figure and "Incidents of the night" (the phone card)
  if (LL.registerOverviewTile) {
    LL.registerOverviewTile({ id: 'incidents', order: 55, available: d => !!d.incidents, render: d => {
      const x = d.incidents;
      const attrs = { class: 'ov-kpi ov-kpi--incidents' + (x.open ? ' ov-kpi--danger' : ''), dataset: { kpi: 'incidents' }, href: x.open && x.open_items[0] ? '#/incidents/' + enc(x.open_items[0].id) : '#/incidents' };
      return h('a', attrs, h('span', { class: 'ov-kpi__label' }, t('inc.kpi')), h('b', { class: 'ov-kpi__value' }, x.open ? LL.fmtNum(x.open, 0) : LL.fmtNum(x.week, 0)),
        h('span', { class: 'ov-kpi__sub' }, x.open ? t('inc.kpi.open', { n: x.open }) : x.week ? t('inc.kpi.week', { n: x.week }) : t('inc.kpi.none')));
    } });
  }
  let night = null, nightAt = 0;
  function loadNight() {
    if (Date.now() - nightAt < 60000 && night) return Promise.resolve(night);
    nightAt = Date.now();
    return api('/api/incidents/night').then(d => { night = d; return d; }).catch(() => night);
  }
  if (LL.registerOverviewCard) {
    LL.registerOverviewCard({ id: 'incidents-night', order: 16, available: d => !!d.incidents, title: () => t('inc.night.title'),
      count: () => (night ? night.faults || null : null),
      render: () => {
        const box = h('div', { class: 'inc-night' }, h('p', { class: 'muted' }, t('inc.loading')));
        loadNight().then(n => {
          if (!n) { box.replaceChildren(h('p', { class: 'muted' }, t('inc.none'))); return; }
          const items = n.items.slice(0, 5);
          box.replaceChildren(
            items.length ? h('ul', { class: 'inc-list inc-list--compact' }, items.map(row)) : h('p', { class: 'muted inc-calm' }, t('inc.night.calm')),
            h('div', { class: 'btn-row btn-row--wrap inc-night__foot' }, h('a', { class: 'btn btn--ghost btn--sm', href: '#/incidents' }, t('inc.night.all')),
              LL.rewind ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.rewind.open(n.since, Date.now() / 1000 - n.since > 23 * 3600 ? '7d' : '24h') }, t('inc.night.play')) : null));
        });
        return box;
      } });
  }

  // ------------------------------------------------------------------ palette
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'go.incidents', group: 'views', order: 3.5, label: () => t('cmdk.go', { name: t('inc.page.title') }), keywords: () => t('inc.cmd.kw'), run: () => LL.go('#/incidents') });
    LL.registerCommand({ id: 'incidents-calendar', group: 'views', order: 3.51, typedOnly: true, label: 'inc.cmd.chronicle', keywords: () => t('inc.cmd.kw_chronicle'), run: () => LL.go('#/incidents?tab=chronicle') });
    LL.registerCommand({ id: 'incidents-events', group: 'views', order: 3.52, typedOnly: true, label: 'inc.cmd.events', keywords: () => t('inc.cmd.kw_events'), run: () => LL.go('#/incidents?tab=events') });
  }
  if (LL.registerAction) {
    LL.registerAction({ id: 'incidents-what', kinds: ['device'], order: 35, label: 'inc.act.what', available: hit => !!hit.id, run: hit => LL.go('#/devices/' + enc(hit.id)) });
  }
})();
