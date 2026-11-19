/* Lanloom — Automations tab of the hub bridge (read only): the list with search and filters, one automation in plain words
   with its flow (When → Only if → Then), findings and last runs, the dependency board (sources left, consumers right, a dot
   travels when an automation runs), who switches what, the timeline, the day plan with a now line, devices per room and
   the hub's health. Nothing here changes the hub; the tab explains, it never heals. Plain DOM + one SVG layer. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const MODES = ['list', 'who', 'timeline', 'plan', 'rooms', 'health'];
  const LEVEL_CHIP = { warn: 'warn', info: 'muted', danger: 'danger', ok: 'ok' };
  const TL_KINDS = ['all', 'automation', 'user', 'device'];
  const PAGE = 200;
  const ON_STATES = ['on', 'open', 'playing', 'unlocked', 'heat', 'cool', 'home'];
  const SVG = 'http://www.w3.org/2000/svg';
  const reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { /* private mode */ } return null; };
  const hubs = () => (LL.state && LL.state.hubs) || [];
  const enc = encodeURIComponent;

  // ------------------------------------------------------------------ plain words: sentence specs from the server
  const list = (items, kind, max) => {
    items = (items || []).filter(x => x != null && x !== '');
    max = max || 3;
    if (items.length > max) items = items.slice(0, max).concat([t('auto.more', { n: items.length - max })]);
    try { return new Intl.ListFormat(LL.locale(), { style: 'long', type: kind || 'conjunction' }).format(items.map(String)); }
    catch (e) { return items.join(', '); }
  };
  function fmtDur(s) {
    s = Math.abs(Math.round(Number(s) || 0));
    if (s < 90) return t('auto.dur.sec', { n: s });
    const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.round((s % 3600) / 60);
    const parts = [];
    if (d) parts.push(t('auto.dur.day', { n: d }));
    if (hr) parts.push(t('auto.dur.hour', { n: hr }));
    if (m && !d) parts.push(t('auto.dur.min', { n: m }));
    return list(parts, 'unit', 3);
  }
  LL.fmtDurShort = fmtDur;
  const WD = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
  function dayName(code, style) {
    const i = WD.indexOf(code);
    if (i < 0) return code;
    try { return new Date(Date.UTC(2024, 0, 1 + i, 12)).toLocaleDateString(LL.locale(), { weekday: style || 'long', timeZone: 'UTC' }); }
    catch (e) { return code; }
  }
  function days(wd, style) {
    if (!wd || !wd.length) return '';
    const key = wd.join(',');
    if (key === 'mon,tue,wed,thu,fri') return t('auto.wd.workdays');
    if (key === 'sat,sun') return t('auto.wd.weekend');
    return list(wd.map(d => dayName(d, style)), 'conjunction', 7);
  }
  const numText = v => (v == null ? '' : typeof v === 'object' ? (v.name != null ? v.name : (v.at != null ? v.at : LL.fmtNum(v.num) + (v.unit ? ' ' + v.unit : ''))) : String(v));
  function vars(spec) {
    const v = spec.v || {}, out = {};
    const either = /\.t\./.test(spec.k) ? 'disjunction' : 'conjunction';
    for (const [k, x] of Object.entries(v)) {
      if (k === 'who') out[k] = list(x, either);
      else if (k === 'what' || k === 'areas' || k === 'names') out[k] = list(x, 'conjunction');
      else if (k === 'at') out[k] = list(x, 'disjunction', 4);
      else if (k === 'states') out[k] = list(x.map(s => LL.hubValue(s)), 'disjunction');
      else if (k === 'state') out[k] = LL.hubValue(x);
      else if (k === 'dur') out[k] = fmtDur(x);
      else if (k === 'wd') out[k] = days(x);
      else if (k === 'val' || k === 'lo' || k === 'hi' || k === 'a' || k === 'b') out[k] = numText(x);
      else if (k === 'n' || k === 'minute') out[k] = x;
      else out[k] = String(x);
    }
    return out;
  }
  function say(spec) {
    if (!spec) return '';
    let s = t(spec.k, vars(spec));
    (spec.tail || []).forEach(tl => { s += ' ' + t(tl.k, vars(tl)); });
    return s;
  }
  LL.autoSay = say;
  // one sentence with its nested parts (branches, repeats, waits) as an indented list
  function sentence(spec, depth) {
    depth = depth || 0;
    const li = h('li', { class: 'au-say__item' }, h('span', { class: 'au-say__text' }, say(spec)));
    const subs = spec.sub || [], then = spec.then || [];
    if (subs.length && spec.k.endsWith('a.if')) {
      li.append(h('ul', { class: 'au-say__sub' }, subs.map(s => sentence(s, depth + 1))));
      if (then.length) li.append(h('div', { class: 'au-say__then' }, t('auto.ex.then_word')), h('ul', { class: 'au-say__sub' }, then.map(s => sentence(s, depth + 1))));
      return li;
    }
    if (subs.length) li.append(h('ul', { class: 'au-say__sub' }, subs.map(s => sentence(s, depth + 1))));
    if (then.length) li.append(h('ul', { class: 'au-say__sub' + (subs.length ? ' au-say__sub--then' : '') }, then.map(s => sentence(s, depth + 1))));
    return li;
  }
  function plainWords(ex, row) {
    if (!ex || !ex.exact) {
      return h('div', { class: 'card au-plain' }, h('h2', { class: 'au-card__title' }, t('auto.plain.title')),
        h('p', { class: 'muted' }, t(row && row.kind === 'script' ? 'auto.plain.unknown_script' : 'auto.plain.unknown')));
    }
    const part = (key, icon, items, joiner) => items.length ? h('div', { class: 'au-plain__part au-plain__part--' + icon },
      h('div', { class: 'au-plain__label' }, h('span', { class: 'au-plain__icon', 'aria-hidden': 'true' }), t(key)),
      h('div', { class: 'au-plain__body' }, joiner ? h('p', { class: 'au-plain__join muted' }, t(joiner)) : null,
        h('ul', { class: 'au-say' }, items.map(s => sentence(s))))) : null;
    return h('div', { class: 'card au-plain' },
      h('h2', { class: 'au-card__title' }, t('auto.plain.title')),
      h('p', { class: 'au-plain__lead' }, lead(ex)),
      part('auto.plain.when', 'when', ex.when, ex.when.length > 1 ? 'auto.plain.when_any' : null),
      part('auto.plain.only', 'only', ex.only, ex.only.length > 1 ? 'auto.plain.only_all' : null),
      part('auto.plain.then', 'then', ex.then, null),
      ex.where && ex.where.length ? h('p', { class: 'au-plain__where muted' }, t('auto.plain.where', { areas: list(ex.where, 'conjunction', 4) })) : null,
      ex.cut ? h('p', { class: 'muted au-plain__cut' }, t('auto.plain.cut')) : null,
      h('p', { class: 'au-plain__rule fineprint' }, t('auto.plain.rule')));
  }
  // the whole automation in one or two sentences ("When ..., and only if ..., then ...")
  function lead(ex) {
    const w = ex.when.map(say), o = ex.only.map(say);
    const flat = ex.then.filter(s => !(s.sub || s.then));          // nested steps (choices, repeats) are told below, not in one sentence
    const d = flat.map(say).concat(flat.length < ex.then.length ? [t('auto.plain.lead_more')] : []);
    if (!d.length) return t('auto.plain.nothing');
    const then = list(d, 'conjunction', 4);
    if (!w.length) return t('auto.plain.lead_run', { then });
    const when = list(w, 'disjunction', 3);
    return o.length ? t('auto.plain.lead_only', { when, only: list(o, 'conjunction', 3), then }) : t('auto.plain.lead', { when, then });
  }

  // ------------------------------------------------------------------ small pieces
  const verbText = v => (LL.strings['auto.verb.' + v] ? t('auto.verb.' + v) : v);
  const chip = (cls, text, title) => h('span', { class: 'chip chip--' + cls, title: title || null }, text);
  function findingText(f) {
    const v = { n: f.n, names: list(f.names || [], 'conjunction', 3), other: f.other_name, days: f.days };
    if (f.last) v.last = LL.fmtDate(f.last);
    return t('auto.f.' + f.code + '.text', v);
  }
  const findingChip = f => chip(LEVEL_CHIP[f.level] || 'muted', t('auto.f.' + f.code), findingText(f));
  const stateChip = (kind, st) => kind === 'script' ? chip(st === 'on' ? 'ok' : 'muted', st === 'on' ? t('auto.state.running') : t('auto.kind.script'))
    : chip(st === 'on' ? 'ok' : st === 'unavailable' ? 'danger' : 'muted', st === 'on' ? t('auto.state.on') : st === 'off' ? t('auto.state.off') : LL.hubValue(st));
  const ago = ts => (ts ? t('time.ago', { age: LL.fmtAge(Date.now() / 1000 - ts) }) : t('auto.never_ran'));
  const clock = ts => new Date(ts * 1000).toLocaleTimeString(LL.locale(), { hour: '2-digit', minute: '2-digit', hour12: false });
  const entityHref = (integ, e) => (e.item ? '#/automations/' + integ + '/item/' + enc(e.id) : '#/automations/' + integ + '/board/' + enc(e.id));
  function entChip(integ, e) {
    const bad = e.missing || e.unavailable;
    return h('a', { class: 'au-ent' + (bad ? ' is-bad' : ''), href: entityHref(integ, e), title: e.id },
      h('span', { class: 'au-ent__dot au-dot--' + (bad ? 'down' : ON_STATES.includes(e.state) ? 'on' : 'idle') }),
      h('span', { class: 'au-ent__name' }, e.missing ? t('auto.ent.missing', { name: e.name }) : e.name),
      e.missing ? null : h('span', { class: 'au-ent__val' }, LL.hubValue(e.state, e.unit)));
  }
  function adminNote(data) {
    if (data.admin && data.exact !== 0) return null;
    return h('div', { class: 'msg msg--info au-admin' }, h('strong', {}, t(data.admin ? 'auto.admin.reading' : 'auto.admin.off_title')), ' ',
      t(data.admin ? 'auto.admin.reading_text' : 'auto.admin.off_text'),
      !data.admin && LL.can('admin') ? h('a', { class: 'au-admin__link', href: '#/settings' }, ' ' + t('auto.admin.open_settings')) : null);
  }

  // ------------------------------------------------------------------ the tab
  async function autoView(app, rest) {
    const fr = LL.frame(app, 'automations');
    const page = h('div', { class: 'page au-page' });
    fr.main.append(page);
    const hs = hubs();
    if (!hs.length) {
      page.append(h('div', { class: 'card empty-state' }, h('h2', {}, t('auto.empty.title')), h('p', { class: 'muted' }, t('auto.empty.text')),
        LL.can('admin') ? h('a', { class: 'btn btn--primary', href: '#/settings' }, t('sensors.empty.action')) : null));
      return fr;
    }
    let integ = Number(rest && rest[0]);
    if (!hs.some(x => x.id === integ)) integ = hs[0].id;
    if (rest && rest[1] === 'item' && rest[2]) return itemPage(fr, page, integ, decodeURIComponent(rest.slice(2).join('/')));
    if (rest && rest[1] === 'board' && rest[2]) return boardPage(fr, page, integ, decodeURIComponent(rest.slice(2).join('/')));
    const qMode = LL.query && LL.query.get('view');
    let mode = MODES.includes(qMode) ? qMode : (MODES.includes(store('ll-auto-mode')) ? store('ll-auto-mode') : 'list');
    let data = null, part = null, timer = null, shown = PAGE, tlKind = 'all', alive = true;
    const filt = { q: (LL.query && LL.query.get('q')) || '', area: '', state: '', finding: (LL.query && LL.query.get('finding')) || '' };
    const count = h('span', { class: 'muted au-count' });
    const hubSel = hs.length > 1 ? h('select', { class: 'select select--sm', 'aria-label': t('auto.hub'), onchange: () => LL.go('#/automations/' + hubSel.value) },
      hs.map(x => h('option', { value: x.id, selected: x.id === integ }, x.name))) : null;
    const seg = h('div', { class: 'sn-seg au-seg', role: 'tablist', 'aria-label': t('auto.title') });
    const body = h('div', { class: 'au-body' });
    page.append(h('div', { class: 'page__head au-head' }, h('h1', {}, t('auto.title')), count, hubSel ? h('span', { class: 'au-head__hub' }, hubSel) : null), seg, body);

    function badge(m) {
      if (!data) return 0;
      if (m === 'list') return data.totals.with_findings;
      if (m === 'who') return data.totals.conflicts;
      if (m === 'health') return data.totals.health;
      return 0;
    }
    function drawSeg() {
      seg.replaceChildren(...MODES.map(m => h('button', { class: 'sn-seg__btn' + (m === mode ? ' is-active' : ''), type: 'button', role: 'tab', 'aria-selected': m === mode ? 'true' : 'false',
        dataset: { mode: m }, onclick: () => setMode(m) }, t('auto.mode.' + m), badge(m) ? h('span', { class: 'sn-seg__badge' + (m === 'list' ? ' au-badge--warn' : '') }, String(badge(m))) : null)));
    }
    function setMode(m) {
      mode = m; store('ll-auto-mode', m); shown = PAGE; part = null;
      LL.deepLink('#/automations/' + integ + (m === 'list' ? '' : '?view=' + m), true);
      drawSeg(); body.replaceChildren(h('div', { class: 'skeleton' })); loadPart();
    }

    // ---------------------------------------------------------------- list
    const qIn = h('input', { type: 'search', class: 'input', placeholder: t('auto.search'), 'aria-label': t('auto.search'), value: filt.q });
    const areaSel = h('select', { class: 'select', 'aria-label': t('auto.filter.area') });
    const stateSel = h('select', { class: 'select', 'aria-label': t('auto.filter.state') },
      ['', 'on', 'off', 'script'].map(v => h('option', { value: v }, t('auto.filter.state.' + (v || 'all')))));
    const findSel = h('select', { class: 'select', 'aria-label': t('auto.filter.finding') });
    const tools = h('div', { class: 'toolbar au-tools' }, qIn, areaSel, stateSel, findSel);
    qIn.addEventListener('input', () => { filt.q = qIn.value; shown = PAGE; renderList(); });
    areaSel.addEventListener('change', () => { filt.area = areaSel.value; shown = PAGE; renderList(); });
    stateSel.addEventListener('change', () => { filt.state = stateSel.value; shown = PAGE; renderList(); });
    findSel.addEventListener('change', () => { filt.finding = findSel.value; shown = PAGE; renderList(); });
    const listBox = h('div', { class: 'au-listbox' });
    function fillFilters() {
      const keepA = filt.area;
      areaSel.replaceChildren(h('option', { value: '' }, t('auto.filter.area.all')), ...data.areas.map(a => h('option', { value: a }, a)));
      areaSel.value = data.areas.includes(keepA) ? keepA : '';
      filt.area = areaSel.value;
      findSel.replaceChildren(h('option', { value: '' }, t('auto.filter.finding.all')), h('option', { value: 'any' }, t('auto.filter.finding.any')),
        ...Object.keys(data.counts).filter(c => data.counts[c]).map(c => h('option', { value: c }, t('auto.f.' + c) + ' (' + LL.fmtNum(data.counts[c]) + ')')));
      findSel.value = filt.finding && (filt.finding === 'any' || data.counts[filt.finding]) ? filt.finding : '';
      filt.finding = findSel.value;
      stateSel.value = filt.state;
    }
    function tiles() {
      const tt = data.totals;
      const tile = (cls, n, label, m, fnd) => h('button', { class: 'au-tile au-tile--' + cls, type: 'button', onclick: () => {
        if (m) setMode(m); else { filt.finding = fnd || ''; findSel.value = filt.finding; renderList(); } } },
        h('span', { class: 'au-tile__n' }, LL.fmtNum(n)), h('span', { class: 'au-tile__label' }, label));
      return h('div', { class: 'au-tiles' },
        tile('all', tt.automations, t('auto.tile.automations', { on: tt.on, off: tt.off }), null, ''),
        tile(tt.with_findings ? 'warn' : 'ok', tt.with_findings, t('auto.tile.findings'), null, 'any'),
        tile(tt.conflicts ? 'bad' : 'ok', tt.conflicts, t('auto.tile.conflicts'), 'who'),
        tile(tt.health ? 'bad' : 'ok', tt.health, t('auto.tile.health'), 'health'),
        tile('all', tt.scripts, t('auto.tile.scripts'), null, ''));
    }
    function matches(r) {
      const s = filt.q.trim().toLocaleLowerCase(LL.locale());
      if (s && ![r.name, r.id, r.search, (r.areas || []).join(' ')].join(' ').toLocaleLowerCase(LL.locale()).includes(s)) return false;
      if (filt.area && !(r.areas || []).includes(filt.area)) return false;
      if (filt.state === 'script' && r.kind !== 'script') return false;
      if ((filt.state === 'on' || filt.state === 'off') && (r.kind !== 'automation' || r.state !== filt.state)) return false;
      if (filt.finding === 'any' && !r.findings.length) return false;
      if (filt.finding && filt.finding !== 'any' && !r.findings.some(f => f.code === filt.finding)) return false;
      return true;
    }
    function renderList() {
      if (!data) return;
      const rows = data.rows.filter(matches);
      count.textContent = t('auto.count', { n: rows.length, total: data.rows.length });
      const card = h('div', { class: 'card au-list' });
      if (!rows.length) card.append(h('div', { class: 'empty-note' }, t('auto.none')));
      rows.slice(0, shown).forEach(r => {
        const trig = (r.triggers || []).map(k => t('auto.k.trigger.' + k));
        const sub = [r.areas && r.areas.length ? list(r.areas, 'conjunction', 2) : null, trig.length ? list(trig, 'conjunction', 2) : null, ago(r.last)].filter(Boolean).join(' · ');
        const warn = r.findings.filter(f => f.level === 'warn');
        const outage = LL.failure ? LL.failure.outageText('he:' + integ + ':' + r.id) : '';      // an outage probably reaches it (failure chain)
        card.append(h('a', { class: 'au-row' + (r.state === 'off' ? ' is-off' : '') + (outage ? ' is-outage' : ''), href: '#/automations/' + integ + '/item/' + enc(r.id), dataset: { id: r.id } },
          h('span', { class: 'au-row__icon au-kind--' + r.kind + (r.state === 'on' ? ' is-on' : ''), 'aria-hidden': 'true' }),
          h('span', { class: 'au-row__main' }, h('span', { class: 'au-row__name' }, r.name), h('span', { class: 'au-row__sub' }, sub)),
          h('span', { class: 'au-row__chips' }, outage ? chip('warn', t('fail.auto.chip'), outage) : null, r.findings.slice(0, 2).map(findingChip), r.findings.length > 2 ? chip('muted', '+' + LL.fmtNum(r.findings.length - 2)) : null),
          h('span', { class: 'au-row__runs', title: t('auto.runs_day_title') }, h('strong', {}, LL.fmtNum(r.runs_day)), h('span', { class: 'muted' }, t('auto.runs_day_short'))),
          h('span', { class: 'au-row__state' }, warn.length ? h('span', { class: 'au-row__warn', title: warn.map(findingText).join('\n') }) : null, stateChip(r.kind, r.state))));
      });
      if (rows.length > shown) card.append(h('button', { class: 'btn btn--ghost btn--block au-more', type: 'button', onclick: () => { shown += PAGE * 2; renderList(); } },
        t('auto.more_rows', { n: rows.length - shown })));
      listBox.replaceChildren(card);
    }
    function listBlock() {
      fillFilters();
      const wrap = h('div', { class: 'au-stack' }, adminNote(data), tiles(), tools, listBox);
      renderList();
      return wrap;
    }

    // ---------------------------------------------------------------- the parts
    let fw = null;
    async function loadPart() {
      const seq = Symbol('load');
      loadPart.seq = seq;
      try {
        if (!data || mode === 'list') { data = await api('/api/hub/' + integ + '/auto'); drawSeg(); }
        if (mode !== 'list') part = await api('/api/hub/' + integ + '/auto/' + mode);
      } catch (e) { if (alive) body.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!alive || loadPart.seq !== seq) return;
      if (fw) fw.ok();
      if (!data.connected) { body.replaceChildren(h('div', { class: 'empty-note' }, t('auto.waiting'))); count.textContent = ''; return; }
      if (mode === 'list') { body.replaceChildren(listBlock()); return; }
      count.textContent = '';
      const view = { who: whoBlock, timeline: timelineBlock, plan: planBlock, rooms: roomsBlock, health: healthBlock }[mode];
      const keepScroll = fr.main.scrollTop;
      body.replaceChildren(view(part));
      fr.main.scrollTop = keepScroll;
    }

    // who switches what
    function whoBlock(d) {
      const conf = d.conflicts || [];
      const confCard = h('div', { class: 'card au-conf' + (conf.length ? ' is-bad' : '') }, h('h2', { class: 'au-card__title' }, t('auto.who.conflicts')),
        conf.length ? h('ul', { class: 'au-conf__list' }, conf.map(c => h('li', {},
          h('span', { class: 'chip chip--danger' }, t('auto.f.conflict')),
          h('span', { class: 'au-conf__text' }, t('auto.who.conflict_text', { entity: c.name, a: c.a_name, b: c.b_name, s: d.window_s, n: c.n, last: clock(c.last) })),
          h('a', { class: 'btn btn--ghost btn--sm', href: '#/automations/' + integ + '/board/' + enc(c.entity) }, t('auto.board.open'))))) :
          h('p', { class: 'muted' }, t('auto.who.no_conflicts', { s: d.window_s })));
      const s = h('input', { type: 'search', class: 'input', placeholder: t('auto.who.search'), 'aria-label': t('auto.who.search') });
      const rowsBox = h('div', { class: 'card au-who' });
      function draw() {
        const q = s.value.trim().toLocaleLowerCase(LL.locale());
        const rows = d.entities.filter(r => !q || [r.name, r.id, r.area || ''].concat(r.causes.map(c => c.name || ''), r.planned.map(p => p.name)).join(' ').toLocaleLowerCase(LL.locale()).includes(q));
        rowsBox.replaceChildren();
        if (!rows.length) rowsBox.append(h('div', { class: 'empty-note' }, t('auto.who.none')));
        rows.forEach(r => {
          const causes = r.causes.map(c => h('span', { class: 'au-cause au-cause--' + c.kind, title: c.last ? t('auto.who.last', { time: clock(c.last) }) : null },
            c.by && c.kind !== 'user' ? h('a', { href: '#/automations/' + integ + '/item/' + enc(c.by) }, c.name) : t('auto.cause.' + c.kind),
            h('span', { class: 'au-cause__n' }, '×' + LL.fmtNum(c.n)), c.states.length ? h('span', { class: 'au-cause__st' }, '→ ' + list(c.states.map(x => LL.hubValue(x)), 'disjunction')) : null));
          const planned = r.planned.filter(p => !r.causes.some(c => c.by === p.by)).map(p => h('a', { class: 'au-cause au-cause--planned', href: '#/automations/' + integ + '/item/' + enc(p.by),
            title: t('auto.who.planned_title') }, p.name, h('span', { class: 'au-cause__st' }, list(p.verbs.map(verbText), 'disjunction'))));
          rowsBox.append(h('div', { class: 'au-whorow' + (r.conflicts ? ' is-bad' : '') },
            h('a', { class: 'au-whorow__ent', href: '#/automations/' + integ + '/board/' + enc(r.id) },
              h('span', { class: 'au-ent__dot au-dot--' + (r.unavailable ? 'down' : ON_STATES.includes(r.state) ? 'on' : 'idle') }),
              h('span', { class: 'au-whorow__name' }, h('strong', {}, r.name), h('span', { class: 'muted' }, [r.area, LL.hubValue(r.state, r.unit)].filter(Boolean).join(' · ')))),
            h('div', { class: 'au-whorow__causes' }, causes, planned, !causes.length && !planned.length ? h('span', { class: 'muted' }, t('auto.who.nobody')) : null),
            r.conflicts ? chip('danger', t('auto.f.conflict')) : (r.switchers > 1 ? chip('warn', t('auto.who.shared', { n: r.switchers })) : h('span', {}))));
        });
      }
      s.addEventListener('input', draw);
      draw();
      return h('div', { class: 'au-stack' }, adminNote(data), confCard, h('p', { class: 'muted au-note' }, t('auto.who.note')), h('div', { class: 'toolbar au-tools' }, s), rowsBox);
    }

    // timeline
    function timelineBlock(d) {
      const s = h('input', { type: 'search', class: 'input', placeholder: t('auto.tl.search'), 'aria-label': t('auto.tl.search') });
      const kinds = h('div', { class: 'sn-seg au-seg au-seg--sm', role: 'tablist', 'aria-label': t('auto.tl.kind') });
      const box = h('div', { class: 'card au-tl' });
      function draw() {
        kinds.replaceChildren(...TL_KINDS.map(k => h('button', { class: 'sn-seg__btn' + (k === tlKind ? ' is-active' : ''), type: 'button', role: 'tab',
          'aria-selected': k === tlKind ? 'true' : 'false', onclick: () => { tlKind = k; draw(); } }, t('auto.tl.kind.' + k))));
        const q = s.value.trim().toLocaleLowerCase(LL.locale());
        const evs = d.events.filter(e => (tlKind === 'all' || (tlKind === 'automation' ? (e.kind === 'automation' || e.kind === 'script') : e.kind === tlKind)) &&
          (!q || [e.name, e.entity, e.by_name || '', e.area || ''].join(' ').toLocaleLowerCase(LL.locale()).includes(q)));
        box.replaceChildren();
        if (!evs.length) { box.append(h('div', { class: 'empty-note' }, t('auto.tl.none'))); return; }
        let hour = null;
        evs.forEach(e => {
          const hr = new Date(e.t * 1000).toLocaleString(LL.locale(), { weekday: 'short', hour: '2-digit', hour12: false });
          if (hr !== hour) { hour = hr; box.append(h('div', { class: 'au-tl__hour' }, t('auto.tl.hour', { hour: new Date(e.t * 1000).toLocaleString(LL.locale(), { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) }))); }
          const text = e.type === 'run' ? t('auto.tl.run', { name: e.name, n: e.n })
            : t('auto.tl.change.' + e.kind, { name: e.name, state: LL.hubValue(e.state), by: e.by_name || '' });
          box.append(h('div', { class: 'au-tl__row au-tl--' + e.kind + (e.type === 'run' ? ' is-run' : '') },
            h('span', { class: 'au-tl__time mono' }, clock(e.t)),
            h('span', { class: 'au-tl__mark', 'aria-hidden': 'true' }),
            h('a', { class: 'au-tl__text', href: e.type === 'run' || e.kind === 'automation' || e.kind === 'script'
              ? '#/automations/' + integ + '/item/' + enc(e.type === 'run' ? e.entity : e.by) : '#/automations/' + integ + '/board/' + enc(e.entity) }, text),
            e.area ? h('span', { class: 'au-tl__area muted' }, e.area) : h('span', {})));
        });
      }
      s.addEventListener('input', draw);
      draw();
      return h('div', { class: 'au-stack' }, h('p', { class: 'muted au-note' }, t('auto.tl.note')), h('div', { class: 'toolbar au-tools au-tools--tl' }, kinds, s), box);
    }

    // day plan
    function minuteOf(m) {
      if (m.at) { const [hh, mm] = m.at.split(':').map(Number); return hh * 60 + mm; }
      if (m.ts) { const d = new Date(m.ts * 1000); return d.getHours() * 60 + d.getMinutes(); }
      return null;
    }
    const hhmm = mins => { const d = new Date(2024, 0, 1, Math.floor(mins / 60), mins % 60); return d.toLocaleTimeString(LL.locale(), { hour: '2-digit', minute: '2-digit', hour12: false }); };
    const nowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60; };
    const SUN_REL = { 'sunrise.before': 'auto.plan.rel.sunrise_before', 'sunrise.after': 'auto.plan.rel.sunrise_after',
      'sunset.before': 'auto.plan.rel.sunset_before', 'sunset.after': 'auto.plan.rel.sunset_after' };
    function markLabel(m) {
      if (m.kind === 'sun') return m.off ? t(SUN_REL[m.ev + (m.off < 0 ? '.before' : '.after')], { dur: fmtDur(m.off), time: hhmm(minuteOf({ ts: m.ts - m.off })) })
        : t('auto.plan.sun.' + m.ev, { time: hhmm(minuteOf(m)) });
      return hhmm(minuteOf(m)) + (m.helper ? ' · ' + m.helper : '') + (m.wd ? ' · ' + days(m.wd, 'short') : '');
    }
    function planBlock(d) {
      const wrap = h('div', { class: 'au-stack' }, adminNote(data));
      if (!d.items.length && !d.rhythm.length && !d.unplaced.length) {
        wrap.append(h('div', { class: 'card empty-note' }, d.known ? t('auto.plan.none') : t('auto.plan.unknown')));
        return wrap;
      }
      const pct = m => (m / 1440 * 100).toFixed(3) + '%';
      const rise = d.sun.rise ? minuteOf({ ts: d.sun.rise }) : null, set = d.sun.set ? minuteOf({ ts: d.sun.set }) : null;
      const axis = h('div', { class: 'au-plan__axis' }, [0, 3, 6, 9, 12, 15, 18, 21].map(hr => h('span', { class: 'au-plan__tick', style: 'left:' + pct(hr * 60) }, hhmm(hr * 60))));
      const night = (rise != null && set != null) ? [h('span', { class: 'au-plan__night', style: 'left:0;width:' + pct(rise) }), h('span', { class: 'au-plan__night', style: 'left:' + pct(set) + ';right:0' })] : [];
      const nowLine = h('div', { class: 'au-plan__now', style: 'left:' + pct(nowMin()) }, h('span', { class: 'au-plan__nowlabel' }, t('auto.plan.now', { time: hhmm(Math.floor(nowMin())) })));
      const rows = d.items.map(it => {
        const track = h('div', { class: 'au-plan__track' }, night.map(n => n.cloneNode()));
        it.spans.forEach(w => {
          const a = w.kind === 'sun' ? (w.from_ts ? minuteOf({ ts: w.from_ts }) : null) : (w.from ? minuteOf({ at: w.from }) : null);
          const b = w.kind === 'sun' ? (w.to_ts ? minuteOf({ ts: w.to_ts }) : null) : (w.to ? minuteOf({ at: w.to }) : null);
          const s0 = a == null ? 0 : a, s1 = b == null ? 1440 : b;
          const label = t('auto.plan.window', { from: a == null ? '' : hhmm(a), to: b == null ? '' : hhmm(b) });
          const seg = (x0, x1) => track.append(h('span', { class: 'au-plan__win', style: 'left:' + pct(x0) + ';width:' + pct(Math.max(4, x1 - x0)), title: label }));
          if (s1 >= s0) seg(s0, s1); else { seg(s0, 1440); seg(0, s1); }
        });
        (it.ran || []).forEach(ts => { if (Date.now() / 1000 - ts < 86400) track.append(h('span', { class: 'au-plan__ran', style: 'left:' + pct(minuteOf({ ts })) })); });
        it.marks.forEach(m => {
          const mm = minuteOf(m);
          if (mm == null) return;
          track.append(h('span', { class: 'au-plan__mark au-plan__mark--' + m.kind + (it.state === 'off' ? ' is-off' : '') + (mm > 1290 ? ' is-end' : ''), style: 'left:' + pct(mm), title: markLabel(m) },
            h('span', { class: 'au-plan__marklabel' }, m.kind === 'sun' ? hhmm(mm) : m.at)));
        });
        return h('div', { class: 'au-plan__row' + (it.state === 'off' ? ' is-off' : '') },
          h('a', { class: 'au-plan__name', href: '#/automations/' + integ + '/item/' + enc(it.id), title: it.name }, it.name, it.state === 'off' ? h('span', { class: 'muted' }, ' · ' + t('auto.state.off')) : null),
          track);
      });
      const sunRow = h('div', { class: 'au-plan__row au-plan__row--sun' }, h('span', { class: 'au-plan__name muted' }, t('auto.plan.sun')),
        h('div', { class: 'au-plan__track' }, night,
          rise != null ? h('span', { class: 'au-plan__sun', style: 'left:' + pct(rise) }, t('auto.plan.sun.sunrise', { time: hhmm(rise) })) : null,
          set != null ? h('span', { class: 'au-plan__sun', style: 'left:' + pct(set) }, t('auto.plan.sun.sunset', { time: hhmm(set) })) : null));
      const grid = h('div', { class: 'au-plan__grid' }, h('div', { class: 'au-plan__row au-plan__row--axis' }, h('span', {}), axis), sunRow, rows,
        h('div', { class: 'au-plan__nowwrap' }, nowLine));
      const legend = h('div', { class: 'au-plan__legend muted' },
        h('span', { class: 'au-leg au-leg--time' }, t('auto.plan.leg.time')), h('span', { class: 'au-leg au-leg--sun' }, t('auto.plan.leg.sun')),
        h('span', { class: 'au-leg au-leg--win' }, t('auto.plan.leg.window')), h('span', { class: 'au-leg au-leg--ran' }, t('auto.plan.leg.ran')),
        h('span', { class: 'au-leg au-leg--night' }, t('auto.plan.leg.night')));
      // phone: the same plan as an agenda, sorted by time, with the now divider
      const agenda = [];
      d.items.forEach(it => it.marks.forEach(m => { const mm = minuteOf(m); if (mm != null) agenda.push({ mm, it, m }); }));
      agenda.sort((a, b) => a.mm - b.mm);
      const nm = nowMin();
      const ag = h('ol', { class: 'au-agenda' });
      let nowShown = false;
      agenda.forEach(a => {
        if (!nowShown && a.mm > nm) { nowShown = true; ag.append(h('li', { class: 'au-agenda__now' }, t('auto.plan.now', { time: hhmm(Math.floor(nm)) }))); }
        ag.append(h('li', { class: 'au-agenda__item' + (a.it.state === 'off' ? ' is-off' : '') }, h('span', { class: 'au-agenda__time mono' }, hhmm(a.mm)),
          h('span', { class: 'au-plan__mark au-plan__mark--' + a.m.kind + ' au-agenda__mark', 'aria-hidden': 'true' }),
          h('a', { href: '#/automations/' + integ + '/item/' + enc(a.it.id) }, a.it.name), h('span', { class: 'muted au-agenda__sub' }, markLabel(a.m))));
      });
      if (!nowShown) ag.append(h('li', { class: 'au-agenda__now' }, t('auto.plan.now', { time: hhmm(Math.floor(nm)) })));
      wrap.append(h('div', { class: 'card au-plan' }, h('div', { class: 'au-plan__head' }, h('h2', { class: 'au-card__title' }, t('auto.plan.title')), legend), grid, ag),
        h('p', { class: 'fineprint au-note' }, t('auto.plan.note')));
      const extra = [];
      if (d.rhythm.length) extra.push(h('div', { class: 'card au-side' }, h('h3', {}, t('auto.plan.rhythm')), h('ul', { class: 'au-plain-list' },
        d.rhythm.map(r => h('li', {}, h('a', { href: '#/automations/' + integ + '/item/' + enc(r.id) }, r.name), h('span', { class: 'muted' }, say(r.spec)))))));
      if (d.unplaced.length) extra.push(h('div', { class: 'card au-side' }, h('h3', {}, t('auto.plan.unplaced')), h('ul', { class: 'au-plain-list' },
        d.unplaced.map(r => h('li', {}, h('a', { href: '#/automations/' + integ + '/item/' + enc(r.id) }, r.name),
          h('span', { class: 'muted' }, r.kind === 'sun' ? t('auto.plan.sun_unknown.' + (r.ev || 'sunset')) : t('auto.plan.helper_unknown', { name: r.helper || '' })))))));
      if (extra.length) wrap.append(h('div', { class: 'au-sides' }, extra));
      // the now line moves on
      const tick = setInterval(() => { if (!document.body.contains(nowLine)) { clearInterval(tick); return; } nowLine.style.left = pct(nowMin()); }, 30000);
      return wrap;
    }

    // devices per room
    function roomsBlock(d) {
      if (!d.rooms.length) return h('div', { class: 'card empty-note' }, t('auto.rooms.none'));
      const grid = h('div', { class: 'au-rooms' });
      d.rooms.forEach(r => {
        const devs = r.devices.map(dv => h('div', { class: 'au-room__dev' },
          h('div', { class: 'au-room__devhead' }, h('span', { class: 'au-room__devname' }, dv.name),
            dv.fresh === 'silent' ? chip('danger', t('radio.fresh.silent')) : null,
            dv.network_device ? h('a', { class: 'au-room__net', href: '#/devices/' + enc(dv.network_device), title: t('auto.rooms.network_card') }, t('auto.rooms.network')) : null),
          h('ul', { class: 'au-room__ents' }, dv.entities.map(e => h('li', {},
            h('a', { href: '#/sensors/' + integ + '/' + enc(e.id), class: 'au-room__ent' + (e.state === 'unavailable' ? ' is-bad' : ON_STATES.includes(e.state) ? ' is-on' : '') },
              h('span', { class: 'au-ent__dot au-dot--' + (e.state === 'unavailable' ? 'down' : ON_STATES.includes(e.state) ? 'on' : 'idle') }),
              h('span', { class: 'au-room__entname' }, e.name === dv.name ? t('auto.rooms.main') : e.name),
              h('span', { class: 'au-room__val' }, LL.hubValue(e.state, e.unit))))),
            dv.more ? h('li', { class: 'muted au-room__more' }, t('auto.rooms.more', { n: dv.more })) : null)));
        grid.append(h('div', { class: 'card au-room' + (r.unavailable ? ' has-bad' : '') },
          h('div', { class: 'au-room__head' }, h('h3', {}, r.name || t('sensors.no_area')),
            h('span', { class: 'au-room__counts' }, r.on ? chip('ok', t('auto.rooms.on', { n: r.on })) : null, r.unavailable ? chip('danger', t('auto.rooms.unavailable', { n: r.unavailable })) : null)),
          r.automations.length ? h('p', { class: 'au-room__autos muted' }, t('auto.rooms.automations', { n: r.automations.length }), ' ',
            r.automations.slice(0, 3).map((a, i) => [i ? ', ' : '', h('a', { href: '#/automations/' + integ + '/item/' + enc(a.id) }, a.name)]),
            r.automations.length > 3 ? ' ' + t('auto.more', { n: r.automations.length - 3 }) : '') : null,
          devs.length ? devs : h('p', { class: 'muted' }, t('auto.rooms.no_devices'))));
      });
      return h('div', { class: 'au-stack' }, h('p', { class: 'muted au-note' }, t('auto.rooms.note')), grid);
    }

    // health of the hub
    function healthBlock(d) {
      const found = d.findings.map(f => h('li', { class: 'au-hf au-hf--' + f.level },
        chip(LEVEL_CHIP[f.level] || 'muted', t('auto.health.level.' + f.level)),
        h('span', {}, t('auto.health.f.' + f.code, { domain: f.domain || '', state: f.state ? t('report.auto.entry.' + f.state) : '', n: f.n || 0 }))));
      const top = h('div', { class: 'card au-health__top' + (d.findings.some(f => f.level === 'warn') ? ' is-bad' : ' is-ok') },
        h('h2', { class: 'au-card__title' }, t('auto.health.title')),
        found.length ? h('ul', { class: 'au-hf__list' }, found) : h('p', {}, t('auto.health.all_good')),
        h('p', { class: 'fineprint' }, t('auto.health.never_heals')));
      const ints = d.integrations;
      const intCard = h('div', { class: 'card au-side' }, h('h3', {}, t('auto.health.integrations')),
        !d.entries_known ? h('p', { class: 'muted' }, t('auto.health.entries_unknown')) :
          [h('p', { class: 'muted' }, t('auto.health.loaded', { n: ints.loaded, total: ints.total })),
            ints.problems.length || ints.not_loaded.length ? h('ul', { class: 'au-plain-list' }, ints.problems.concat(ints.not_loaded).map(p => h('li', {},
              h('span', { class: 'mono' }, p.domain), chip(p.state === 'not_loaded' ? 'muted' : 'danger', t('report.auto.entry.' + p.state)),
              p.n > 1 ? h('span', { class: 'muted' }, '×' + LL.fmtNum(p.n)) : null))) : h('p', {}, t('auto.health.integrations_ok'))]);
      const unCard = h('div', { class: 'card au-side au-side--wide' }, h('h3', {}, t('auto.health.unavailable', { n: d.unavailable_total })),
        d.unavailable.length ? d.unavailable.map(g => LL.keepOpen.details('au-unav-' + g.domain, false,
          h('span', { class: 'au-unav__sum' }, h('span', { class: 'mono' }, g.domain), chip(g.n >= 3 ? 'warn' : 'muted', LL.fmtNum(g.n))),
          h('ul', { class: 'au-plain-list' }, g.items.map(i => h('li', {}, h('a', { href: '#/sensors/' + integ + '/' + enc(i.id) }, i.name),
            h('span', { class: 'muted' }, [i.device !== i.name ? i.device : null, i.area].filter(Boolean).join(' · ')))),
            g.n > g.items.length ? h('li', { class: 'muted' }, t('auto.more', { n: g.n - g.items.length })) : null))) : h('p', {}, t('auto.health.unavailable_none')),
        d.unknown ? h('p', { class: 'muted' }, t('auto.health.unknown', { n: d.unknown })) : null);
      const max = Math.max(1, ...d.platforms.map(p => p.n));
      const platCard = h('div', { class: 'card au-side' }, h('h3', {}, t('auto.health.platforms')),
        d.platforms.length ? h('ul', { class: 'sn-bars' }, d.platforms.map(p => h('li', {}, h('span', { class: 'sn-bars__name mono' }, p.platform === '-' ? t('radio.events.other') : p.platform),
          h('span', { class: 'sn-bars__track' }, h('span', { class: 'sn-bars__fill au-bars--bad', style: 'width:' + Math.max(4, p.n / max * 100) + '%' })),
          h('span', { class: 'sn-bars__n' }, LL.fmtNum(p.n))))) : h('p', { class: 'muted' }, t('auto.health.unavailable_none')),
        d.radio_silent ? h('a', { class: 'btn btn--ghost btn--sm', href: '#/sensors/' + integ, onclick: () => store('ll-sensors-mode', 'onsite') }, t('auto.health.radio_silent', { n: d.radio_silent })) : null);
      return h('div', { class: 'au-stack' }, top, h('div', { class: 'au-sides' }, intCard, platCard, unCard));
    }

    body.append(h('div', { class: 'skeleton' }));
    drawSeg();
    fw = LL.fresh.watch(body, 15000, loadPart);
    await loadPart();
    const every = () => ({ list: 15000, who: 15000, timeline: 10000, plan: 60000, rooms: 10000, health: 30000 }[mode] || 15000);
    const loop = () => { timer = setTimeout(async () => { if (!document.hidden && !(mode === 'list' && document.activeElement && body.contains(document.activeElement) && document.activeElement.tagName === 'SELECT')) await loadPart(); if (alive) loop(); }, every()); };
    loop();
    const onOutage = () => { if (alive && mode === 'list') renderList(); };
    document.addEventListener('ll-outage', onOutage);
    if (LL.failure) LL.failure.loadOutages();
    return { destroy: () => { alive = false; clearTimeout(timer); fw.destroy(); document.removeEventListener('ll-outage', onOutage); fr.destroy(); } };
  }

  // ------------------------------------------------------------------ one automation
  async function itemPage(fr, page, integ, eid) {
    page.classList.add('au-item');
    let alive = true, d = null;
    const back = h('a', { class: 'sn-back', href: '#/automations/' + integ }, '← ' + t('auto.back'));
    const head = h('div', { class: 'au-item__head' });
    const grid = h('div', { class: 'au-item__grid' });
    const runsBox = h('div', { class: 'card au-runs' });
    const boardBox = h('div', { class: 'card au-boardcard' });
    page.append(back, head, grid);
    let traces = null, traceWait = null, board = null, fw = null;
    async function load() {
      try { d = await api('/api/hub/' + integ + '/auto/item/' + enc(eid)); }
      catch (e) { if (alive && !d) grid.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!alive) return;
      if (fw) fw.ok();
      draw();
    }
    function draw() {
      const r = d.row;
      head.replaceChildren(h('div', { class: 'au-item__title' }, h('span', { class: 'au-row__icon au-kind--' + r.kind + (r.state === 'on' ? ' is-on' : ''), 'aria-hidden': 'true' }),
        h('h1', {}, r.name), stateChip(r.kind, r.state)),
        h('div', { class: 'au-item__facts' },
          fact(t('auto.fact.kind'), t('auto.kind.' + r.kind)), r.mode ? fact(t('auto.fact.mode'), t('auto.mode_value.' + r.mode)) : null,
          fact(t('auto.fact.last'), ago(r.last)), fact(t('auto.fact.runs'), t('auto.fact.runs_value', { hour: r.runs_hour, day: r.runs_day })),
          fact(t('auto.fact.id'), h('span', { class: 'mono' }, r.id))));
      const fnd = h('div', { class: 'card au-findings' + (r.findings.some(f => f.level === 'warn') ? ' is-bad' : '') }, h('h2', { class: 'au-card__title' }, t('auto.findings')),
        r.findings.length ? h('ul', { class: 'au-findings__list' }, r.findings.map(f => h('li', {}, findingChip(f), h('span', {}, findingText(f)),
          f.ids && f.ids.length && f.code !== 'dead' ? h('span', { class: 'au-findings__links' }, f.ids.slice(0, 3).map(x => h('a', { href: '#/automations/' + integ + '/board/' + enc(x) }, t('auto.board.short')))) : null))) :
          h('p', { class: 'muted' }, d.explain.exact ? t('auto.findings.none') : t('auto.findings.limited')));
      grid.replaceChildren(h('div', { class: 'au-item__main' }, adminNote({ admin: d.admin, exact: d.explain.exact ? 1 : 0 }), plainWords(d.explain, r), flowCard(), boardBox),
        h('div', { class: 'au-item__side' }, fnd, runsBox, related()));
      drawRuns();
      drawBoard();
    }
    const fact = (label, value) => h('div', { class: 'au-fact' }, h('span', { class: 'au-fact__label' }, label), h('span', { class: 'au-fact__value' }, value));
    function flowCard() {
      if (!d.explain.exact) return null;
      const col = (part, title) => h('div', { class: 'au-flow__col au-flow__col--' + part }, h('div', { class: 'au-flow__head' }, h('span', { class: 'au-flow__icon', 'aria-hidden': 'true' }), t(title)),
        (d.flow[part] || []).length ? (d.flow[part] || []).map(n => h('div', { class: 'au-step' },
          h('div', { class: 'au-step__kind' }, t('auto.k.' + part + '.' + n.k), n.svc ? h('span', { class: 'mono au-step__svc' }, n.svc) : null),
          h('ul', { class: 'au-say au-say--step' }, sentence(n.spec)),
          n.ents.length ? h('div', { class: 'au-step__ents' }, n.ents.map(e => entChip(integ, e))) : null)) : h('p', { class: 'muted au-step__none' }, t('auto.flow.none.' + part)));
      return h('div', { class: 'card au-flow' }, h('h2', { class: 'au-card__title' }, t('auto.flow.title')),
        h('div', { class: 'au-flow__cols' }, col('trigger', 'auto.plain.when'), h('span', { class: 'au-flow__arrow', 'aria-hidden': 'true' }),
          col('condition', 'auto.plain.only'), h('span', { class: 'au-flow__arrow', 'aria-hidden': 'true' }), col('action', 'auto.plain.then')));
    }
    function related() {
      if (!d.related || !d.related.length) return null;
      return h('div', { class: 'card au-related' }, h('h2', { class: 'au-card__title' }, t('auto.related')), h('p', { class: 'muted au-note' }, t('auto.related.note')),
        h('div', { class: 'au-step__ents' }, d.related.map(e => entChip(integ, e))));
    }
    function drawRuns() {
      const r = d.row;
      const hours = d.hours || [];
      const now = Date.now() / 1000;
      const bars = [];
      const max = Math.max(1, ...hours.map(x => x[1]));
      for (let i = 23; i >= 0; i--) {
        const start = Math.floor(now / 3600 - i) * 3600;
        const got = hours.find(x => x[0] === start);
        const n = got ? got[1] : 0;
        bars.push(h('span', { class: 'au-hbar', title: t('auto.runs.hour_tip', { time: clock(start), n }) }, h('span', { class: 'au-hbar__fill' + (n >= 60 ? ' is-bad' : ''), style: 'height:' + (n ? Math.max(6, n / max * 100) : 0) + '%' })));
      }
      const can = LL.can('operate') && d.admin && r.traceable;
      const btn = can ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: askTraces }, traceWait ? t('auto.runs.asking') : t('auto.runs.load')) : null;
      if (btn && traceWait) btn.disabled = true;
      runsBox.replaceChildren(h('div', { class: 'au-runs__head' }, h('h2', { class: 'au-card__title' }, t('auto.runs.title')), btn),
        h('div', { class: 'au-hbars', role: 'img', 'aria-label': t('auto.runs.hours_label', { n: r.runs_day }) }, bars),
        h('div', { class: 'au-hbars__axis muted' }, h('span', {}, t('auto.runs.ago24')), h('span', {}, t('auto.runs.now'))),
        traces && traces.runs && traces.runs.length ? traceList(traces) : h('p', { class: 'muted au-note' }, !d.admin ? t('auto.runs.admin') : !r.traceable ? t('auto.runs.no_id') : can ? t('auto.runs.hint') : t('auto.runs.operator')));
    }
    function traceList(tr) {
      const res = run => run.error ? 'danger' : run.execution === 'finished' ? 'ok' : run.execution === 'failed_conditions' ? 'muted' : 'warn';
      return h('div', { class: 'au-traces' }, h('ul', { class: 'au-traces__list' }, tr.runs.slice(0, 8).map(run => h('li', {},
        h('span', { class: 'mono' }, run.start ? clock(run.start) : '—'), chip(res(run), t('auto.runs.exec.' + (['finished', 'failed_conditions', 'cancelled', 'aborted', 'error', 'failed_single'].includes(run.execution) ? run.execution : 'other'))),
        h('span', { class: 'muted' }, run.start && run.finish ? fmtDur(Math.max(0, run.finish - run.start)) : ''),
        run.last_step ? h('span', { class: 'mono au-traces__step' }, run.last_step) : null))),
        tr.steps && tr.steps.length ? h('div', { class: 'au-path' }, h('span', { class: 'muted' }, t('auto.runs.path')),
          tr.steps.map(s => h('span', { class: 'au-path__step' + (s.ok ? '' : ' is-bad') }, s.path))) : null);
    }
    async function askTraces() {
      try { await api('/api/hub/' + integ + '/traces/' + enc(eid), { method: 'POST', body: {} }); } catch (e) { LL.toast(LL.errText(e), 'error'); return; }
      const since = Date.now() / 1000;
      traceWait = setInterval(async () => {
        try {
          const got = await api('/api/hub/' + integ + '/traces/' + enc(eid));
          if (got.traces && (got.traces.t || 0) >= since - 2) { traces = got.traces; clearInterval(traceWait); traceWait = null; if (alive) drawRuns(); }
        } catch (e) { clearInterval(traceWait); traceWait = null; }
        if (Date.now() / 1000 - since > 45) { clearInterval(traceWait); traceWait = null; if (alive) { drawRuns(); LL.toast(t('auto.runs.timeout'), 'info'); } }
      }, 1500);
      drawRuns();
    }
    async function drawBoard() {
      try { const b = await api('/api/hub/' + integ + '/auto/board/' + enc(eid)); if (!alive) return; board = boardEl(integ, b, true); }
      catch (e) { board = null; }
      boardBox.replaceChildren(h('div', { class: 'au-runs__head' }, h('h2', { class: 'au-card__title' }, t('auto.board.title')),
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/automations/' + integ + '/board/' + enc(eid) }, t('auto.board.open'))), board ? board.el : h('p', { class: 'muted' }, t('auto.board.none')));
      if (board) board.layout();
    }
    try { const got = await api('/api/hub/' + integ + '/traces/' + enc(eid)); traces = got.traces; } catch (e) { /* none yet */ }
    grid.append(h('div', { class: 'skeleton' }));
    fw = LL.fresh.watch(grid, 15000, load);
    await load();
    const unsub = LL.live.subscribe(f => { if (board && f.autoruns) board.pulse(f.autoruns.filter(x => x[0] === integ)); });
    const timer = setInterval(() => { if (!document.hidden && !traceWait) load(); }, 15000);
    const onResize = () => { if (board) board.layout(); };
    window.addEventListener('resize', onResize);
    return { destroy: () => { alive = false; unsub(); clearInterval(timer); fw.destroy(); if (traceWait) clearInterval(traceWait); window.removeEventListener('resize', onResize); fr.destroy(); } };
  }

  // ------------------------------------------------------------------ the dependency board
  // sources | centre | consumers, joined by traces drawn in one SVG layer; a dot travels a trace when an automation runs
  function boardEl(integ, b, compact) {
    const padEl = (p, side) => {
      const href = p.id ? entityHref(integ, p) : null;
      const bad = p.missing || p.unavailable;
      const verbs = (p.verbs || []).map(verbText);
      const inner = [h('span', { class: 'au-pad__role' }, t('auto.role.' + p.role) + (verbs.length ? ' · ' + list(verbs, 'disjunction') : '')),
        h('span', { class: 'au-pad__name' }, p.id ? (p.missing ? t('auto.ent.missing', { name: p.name }) : p.name) : t('auto.cause.' + p.role)),
        p.id && !p.missing ? h('span', { class: 'au-pad__val' }, p.item ? t('auto.kind.' + p.domain) : LL.hubValue(p.state, p.unit)) : null];
      return h(href ? 'a' : 'div', { class: 'au-pad au-pad--' + side + ' au-pad--' + (p.item ? 'item' : p.id ? 'entity' : 'cause') + (bad ? ' is-bad' : ''), href, dataset: { id: p.id || '', role: p.role } },
        h('span', { class: 'au-pad__pin', 'aria-hidden': 'true' }), h('span', { class: 'au-pad__body' }, inner));
    };
    const c = b.center;
    const left = h('div', { class: 'au-board__col au-board__col--left' }, h('div', { class: 'au-board__label' }, t(c.item ? 'auto.board.left_item' : 'auto.board.left')),
      b.left.length ? b.left.map(p => padEl(p, 'left')) : h('p', { class: 'muted au-board__empty' }, t('auto.board.left_none')));
    const right = h('div', { class: 'au-board__col au-board__col--right' }, h('div', { class: 'au-board__label' }, t(c.item ? 'auto.board.right_item' : 'auto.board.right')),
      b.right.length ? b.right.map(p => padEl(p, 'right')) : h('p', { class: 'muted au-board__empty' }, t('auto.board.right_none')));
    const centre = h('div', { class: 'au-board__chip' + (c.item ? ' is-item' : '') + (c.unavailable ? ' is-bad' : ''), dataset: { id: c.id } },
      h('span', { class: 'au-board__chipkind' }, c.item ? t('auto.kind.' + c.kind) : (c.area || t('auto.board.entity'))),
      h('strong', { class: 'au-board__chipname' }, c.name),
      h('span', { class: 'au-board__chipval' }, c.item ? stateChip(c.kind, c.state) : LL.hubValue(c.state, c.unit)),
      compact ? null : h('span', { class: 'au-board__links' },
        c.item ? h('a', { href: '#/automations/' + integ + '/item/' + enc(c.id) }, t('auto.board.to_item')) : h('a', { href: '#/sensors/' + integ + '/' + enc(c.id) }, t('auto.board.to_sensor')),
        c.device ? h('span', { class: 'muted' }, ' · ' + c.device) : null));
    const mid = h('div', { class: 'au-board__col au-board__col--mid' }, centre);
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('class', 'au-board__traces');
    svg.setAttribute('aria-hidden', 'true');
    const el = h('div', { class: 'au-board' + (compact ? ' au-board--compact' : '') }, svg, left, mid, right);
    const paths = [];                                   // {path, from, to, side}
    function layout() {
      if (!el.isConnected) return;
      const box = el.getBoundingClientRect();
      if (!box.width) return;
      svg.setAttribute('viewBox', '0 0 ' + box.width + ' ' + box.height);
      svg.setAttribute('width', box.width); svg.setAttribute('height', box.height);
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      paths.length = 0;
      const cr = centre.getBoundingClientRect();
      const stacked = getComputedStyle(el).getPropertyValue('--au-stacked').trim() === '1';
      el.querySelectorAll('.au-pad').forEach(pad => {
        const r = pad.getBoundingClientRect();
        const side = pad.classList.contains('au-pad--left') ? 'left' : 'right';
        let x1, y1, x2, y2, d;
        if (stacked) {                                    // phone: columns stacked, traces run down the left edge
          x1 = 10; y1 = r.top + r.height / 2 - box.top; x2 = 10; y2 = cr.top + cr.height / 2 - box.top;
          d = 'M' + (r.left - box.left) + ',' + y1 + ' H' + x1 + ' V' + y2 + ' H' + (cr.left - box.left);
        } else if (side === 'left') {
          x1 = r.right - box.left; y1 = r.top + r.height / 2 - box.top; x2 = cr.left - box.left; y2 = cr.top + cr.height / 2 - box.top;
          const mx = (x1 + x2) / 2;
          d = 'M' + x1 + ',' + y1 + ' C' + mx + ',' + y1 + ' ' + mx + ',' + y2 + ' ' + x2 + ',' + y2;
        } else {
          x1 = cr.right - box.left; y1 = cr.top + cr.height / 2 - box.top; x2 = r.left - box.left; y2 = r.top + r.height / 2 - box.top;
          const mx = (x1 + x2) / 2;
          d = 'M' + x1 + ',' + y1 + ' C' + mx + ',' + y1 + ' ' + mx + ',' + y2 + ' ' + x2 + ',' + y2;
        }
        const path = document.createElementNS(SVG, 'path');
        path.setAttribute('d', d);
        path.setAttribute('class', 'au-trace au-trace--' + (pad.dataset.role || 'x') + (pad.classList.contains('is-bad') ? ' is-bad' : ''));
        svg.appendChild(path);
        paths.push({ path, id: pad.dataset.id, side, reverse: stacked && side === 'right' });
      });
    }
    function dot(p) {
      if (reduced()) { p.path.classList.add('is-flash'); setTimeout(() => p.path.classList.remove('is-flash'), 900); return; }
      const len = p.path.getTotalLength();
      const circle = document.createElementNS(SVG, 'circle');
      circle.setAttribute('r', '4'); circle.setAttribute('class', 'au-dot');
      svg.appendChild(circle);
      const t0 = performance.now(), dur = 900;
      (function step(now) {
        const k = Math.min(1, (now - t0) / dur);
        const pt = p.path.getPointAtLength((p.reverse ? 1 - k : k) * len);
        circle.setAttribute('cx', pt.x); circle.setAttribute('cy', pt.y);
        if (k < 1 && circle.isConnected) requestAnimationFrame(step); else circle.remove();
      })(t0);
    }
    function pulse(runs) {
      runs.forEach(([, id]) => {
        if (id === c.id) {                                // the centre ran: everything it reads flows in, everything it switches out
          paths.forEach((p, i) => setTimeout(() => dot(p), (p.side === 'left' ? 0 : 450) + i * 30));
          centre.classList.add('is-firing'); setTimeout(() => centre.classList.remove('is-firing'), 1200);
        } else paths.filter(p => p.id === id).forEach(dot);
      });
    }
    requestAnimationFrame(layout);
    return { el, layout, pulse };
  }

  async function boardPage(fr, page, integ, eid) {
    page.classList.add('au-boardpage');
    let alive = true, board = null, fw = null;
    const back = h('a', { class: 'sn-back', href: '#/automations/' + integ + (store('ll-auto-mode') && store('ll-auto-mode') !== 'list' ? '?view=' + store('ll-auto-mode') : '') }, '← ' + t('auto.back'));
    const head = h('div', { class: 'page__head' }, h('h1', {}, t('auto.board.title')));
    const box = h('div', { class: 'card au-boardcard au-boardcard--page' });
    page.append(back, head, h('p', { class: 'muted au-note' }, t('auto.board.note')), box);
    async function load() {
      let b;
      try { b = await api('/api/hub/' + integ + '/auto/board/' + enc(eid)); } catch (e) { if (alive && !board) box.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!alive) return;
      if (fw) fw.ok();
      head.replaceChildren(h('h1', {}, t('auto.board.title_of', { name: b.center.name })));
      board = boardEl(integ, b, false);
      box.replaceChildren(adminNote({ admin: b.admin, exact: b.known ? 1 : 0 }) || '', board.el);
      board.layout();
    }
    box.append(h('div', { class: 'skeleton' }));
    fw = LL.fresh.watch(box, 15000, load);
    await load();
    const unsub = LL.live.subscribe(f => { if (board && f.autoruns) board.pulse(f.autoruns.filter(x => x[0] === integ)); });
    const timer = setInterval(() => { if (!document.hidden) load(); }, 15000);
    const onResize = () => { if (board) board.layout(); };
    window.addEventListener('resize', onResize);
    return { destroy: () => { alive = false; unsub(); clearInterval(timer); fw.destroy(); window.removeEventListener('resize', onResize); fr.destroy(); } };
  }

  LL.view('automations', autoView);

  // ------------------------------------------------------------------ search and commands (Ctrl K)
  if (LL.registerCommand) {
    const hasHub = () => hubs().length > 0;
    const MODE_KW = { who: 'cmdk.kw.auto_who', timeline: 'cmdk.kw.auto_timeline', plan: 'cmdk.kw.auto_plan', rooms: 'cmdk.kw.auto_rooms', health: 'cmdk.kw.auto_health' };
    const goMode = mode => () => { store('ll-auto-mode', mode); LL.go('#/automations' + (mode === 'list' ? '' : '?view=' + mode)); };
    LL.registerCommand({ id: 'go.automations', group: 'views', order: 4.5, available: hasHub, label: () => t('cmdk.go', { name: t('nav.automations') }),
      keywords: () => t('cmdk.kw.automations'), run: goMode('list') });
    MODES.filter(m => m !== 'list').forEach((m, i) => LL.registerCommand({ id: 'go.automations.' + m, group: 'views', order: 4.6 + i / 100, typedOnly: true, available: hasHub,
      label: () => t('cmdk.go', { name: t('nav.automations') + ' › ' + t('auto.mode.' + m) }), keywords: () => t(MODE_KW[m]), run: goMode(m) }));
    LL.registerCommand({ id: 'auto.findings', group: 'views', order: 4.7, typedOnly: true, available: hasHub, label: () => t('cmdk.auto_findings'),
      keywords: () => t('cmdk.kw.auto_findings'), run: () => { store('ll-auto-mode', 'list'); LL.go('#/automations?finding=any'); } });
  }
  if (LL.registerAction) {
    LL.registerAction({ id: 'open.automation', kinds: ['automation'], order: -1, label: 'cmdk.act.open_automation', available: hit => !!(hit.entity_id && hit.integ),
      run: hit => LL.go('#/automations/' + hit.integ + '/item/' + enc(hit.entity_id)) });
    LL.registerAction({ id: 'auto-board', kinds: ['entity', 'automation'], order: 5, label: 'cmdk.act.board', available: hit => !!(hit.entity_id && hit.integ),
      run: hit => LL.go('#/automations/' + hit.integ + '/board/' + enc(hit.entity_id)) });
  }
})();
