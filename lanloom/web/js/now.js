/* Lanloom — "Now" (#/now) and "Focus" (#/focus/<id>): the phone's start into the network, also usable on a wide screen.

   Now: a health ring (devices online / offline / other, the overall word), the sites (M1, only with several), what changed in the
   last hours, the look back over 24 hours as a strip (a tap opens the rewind of the map at that moment), cards that other parts
   register (LL.registerNowCard({id, order, available(), render()}) - the energy card does), and the way into Focus.
   Data: GET /api/overview (health, history, incidents), GET /api/overview/now (changes), GET /api/sites/overview (several sites).
   Details open in sheets (js/sheet.js): what needs a look, all changes, the sites, the phone's "More" list; a link from one full
   sheet to another follows with the back key. Pull down at the top to refresh.

   Focus: one component in the middle, its partners in a ring around it (the traces of the network map: cable, radio, uplink,
   virtual ...), live state from the shared live feed. A tap on a partner moves the focus there; the list below says the same in
   words. Everything here only reads. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const SVGNS = 'http://www.w3.org/2000/svg';
  const REFRESH_MS = 20000;
  const RING_MAX = 12;               // partners drawn in the ring; more are listed below
  const fx = v => String(Math.round(v * 100) / 100);
  const svg = (tag, attrs, ...kids) => {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, String(v));
    kids.flat().forEach(k => { if (k != null) el.appendChild(typeof k === 'string' ? document.createTextNode(k) : k); });
    return el;
  };
  const LEVEL = ['ok', 'warn', 'danger'];
  const lvl = x => (x === 'info' ? 'ok' : LEVEL.includes(x) ? x : 'unknown');       // a note of the kind "info" is no unrest
  const ago = ts => t('time.ago', { age: LL.fmtAge(Math.max(0, Date.now() / 1000 - ts)) });

  LL.NAV_NOW_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>';
  LL.NAV_MORE_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="5.5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="18.5" cy="12" r="1.3"/></svg>';
  LL.NAV_FOCUS_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="3.5" r="1.4"/><circle cx="20" cy="15" r="1.4"/><circle cx="4" cy="15" r="1.4"/><path d="M12 5v4M18.7 14.2l-4 -1.3M5.3 14.2l4-1.3"/></svg>';

  // ------------------------------------------------------------------ cards other parts add (energy ...)
  LL.nowCards = LL.nowCards || [];
  LL.registerNowCard = function (card) { LL.nowCards = LL.nowCards.filter(c => c.id !== card.id).concat([card]).sort((a, b) => a.order - b.order); };

  // ------------------------------------------------------------------ the health ring
  function ring(d) {
    const dv = d.devices || {};
    const total = Math.max(0, dv.total || 0), on = dv.online || 0, off = dv.offline || 0;
    const other = Math.max(0, total - on - off);
    const level = lvl((d.health && d.health.level) || 'ok');
    const R = 52, C = 2 * Math.PI * R;
    const g = svg('svg', { class: 'now-ring__svg', viewBox: '0 0 128 128', 'aria-hidden': 'true' });
    g.appendChild(svg('circle', { class: 'now-ring__track', cx: 64, cy: 64, r: R }));
    let at = 0;
    [['ok', on], ['danger', off], ['muted', other]].forEach(([cls, n]) => {
      if (!total || !n) return;
      const len = C * n / total;
      const gap = total > 1 && len > 6 ? 2 : 0;
      g.appendChild(svg('circle', { class: 'now-ring__seg now-ring__seg--' + cls, cx: 64, cy: 64, r: R, 'stroke-dasharray': fx(Math.max(0.5, len - gap)) + ' ' + fx(C), 'stroke-dashoffset': fx(-at), transform: 'rotate(-90 64 64)' }));
      at += len;
    });
    const pct = total ? Math.round(on / total * 100) : 0;
    const label = t('now.ring.label', { on, total, level: t('now.level.' + level) });
    return h('section', { class: 'card now-card now-ring now-ring--' + level, 'aria-label': t('now.ring.title'), dataset: { level } },
      h('div', { class: 'now-ring__fig', role: 'img', 'aria-label': label }, g,
        h('div', { class: 'now-ring__mid', 'aria-hidden': 'true' }, h('b', { class: 'now-ring__pct' }, t('now.ring.pct', { pct: LL.fmtNum(pct, 0) })), h('span', { class: 'now-ring__sub' }, t('now.ring.online')))),
      h('div', { class: 'now-ring__text' },
        h('h2', { class: 'now-ring__word' }, h('i', { class: 'now-dot now-dot--' + level, 'aria-hidden': 'true' }), t('now.level.' + level)),
        h('ul', { class: 'now-facts' },
          h('li', {}, h('b', {}, LL.fmtNum(on, 0)), ' ', t('now.fact.online', { total: LL.fmtNum(total, 0) })),
          off ? h('li', { class: 'is-danger' }, h('b', {}, LL.fmtNum(off, 0)), ' ', t('now.fact.offline')) : null,
          h('li', {}, h('b', {}, LL.fmtNum((d.sources || {}).fresh || 0, 0)), ' ', t('now.fact.sources', { total: LL.fmtNum((d.sources || {}).total || 0, 0) })),
          d.incidents && d.incidents.open ? h('li', { class: 'is-warn' }, h('a', { href: '#/incidents' }, t('now.fact.incidents', { n: d.incidents.open }))) : null),
        (d.notes || []).length ? h('button', { class: 'btn btn--primary btn--sm now-ring__look', type: 'button', onclick: () => LL.sheets.open('notes') }, t('now.look', { n: d.notes.length }))
          : h('p', { class: 'muted now-ring__calm' }, t('now.calm'))));
  }

  // ------------------------------------------------------------------ sites (M1)
  function sitesCard(list) {
    if (!list || list.length < 2) return null;
    const cur = LL.site && LL.site() ? LL.site().id : null;
    return h('section', { class: 'card now-card now-sites', 'aria-labelledby': 'now-h-sites' },
      h('header', { class: 'now-card__head' }, h('h2', { id: 'now-h-sites' }, t('now.sites.title')), h('span', { class: 'now-card__n' }, LL.fmtNum(list.length, 0))),
      h('ul', { class: 'now-sites__list' }, list.slice(0, 4).map(s => siteRow(s, cur))),
      list.length > 4 ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.sheets.open('sites') }, t('now.sites.all', { n: list.length })) : null);
  }
  function siteRow(s, cur) {
    const dv = s.devices || {};
    const level = lvl(s.level);
    const here = s.id === cur;
    return h('li', { class: 'now-site' + (here ? ' is-here' : ''), dataset: { site: s.id } },
      h('button', { class: 'now-site__btn', type: 'button', disabled: here, onclick: () => { if (!here && LL.switchSite) LL.switchSite(s.id, '#/now'); } },
        h('i', { class: 'now-dot now-dot--' + level, 'aria-hidden': 'true' }),
        h('span', { class: 'now-site__name' }, LL.siteName(s)),
        h('span', { class: 'now-site__sub muted' }, here ? t('now.sites.here') : t('now.sites.devices', { on: LL.fmtNum(dv.online || 0, 0), total: LL.fmtNum(dv.total || 0, 0) })),
        h('span', { class: 'now-site__state' }, t('now.level.' + level))));
  }

  // ------------------------------------------------------------------ changes
  function changeRow(c) {
    const link = LL.changeLink ? LL.changeLink(c) : null;
    const lv = LL.changeLevel ? LL.changeLevel(c.kind) : 'ok';
    const inner = [h('i', { class: 'change__dot change__dot--' + lv, 'aria-hidden': 'true' }), h('span', { class: 'change__text' }, LL.changeText(c)), h('span', { class: 'change__when muted' }, ago(c.t))];
    return h('li', { class: 'change' }, link ? h('a', { class: 'change__link', href: link }, inner) : h('span', { class: 'change__link' }, inner));
  }
  function changesCard(changes) {
    return h('section', { class: 'card now-card now-changes', 'aria-labelledby': 'now-h-changes' },
      h('header', { class: 'now-card__head' }, h('h2', { id: 'now-h-changes' }, t('now.changes.title')), h('span', { class: 'now-card__n' }, LL.fmtNum(changes.length, 0))),
      changes.length ? h('ul', { class: 'change-list' }, changes.slice(0, 4).map(changeRow)) : h('p', { class: 'muted' }, t('now.changes.none')),
      changes.length > 4 ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.sheets.open('changes') }, t('now.changes.all', { n: changes.length })) : null);
  }

  // ------------------------------------------------------------------ the look back: 24 h of health samples, incidents marked
  function lookBack(d) {
    const hist = (d.health && d.health.history) || [];
    const step = (d.health && d.health.step_s) || 300;
    const now = Date.now() / 1000, t0 = now - 86400;
    const W = 288, H = 34;
    const g = svg('svg', { class: 'now-tl__svg', viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
    hist.forEach(([ts, rank, on, total]) => {
      if (ts < t0) return;
      const x = (ts - t0) / 86400 * W;
      const share = total ? on / total : 0;
      const hh = 6 + share * (H - 10);
      g.appendChild(svg('rect', { class: 'now-tl__bar now-tl__bar--' + (LEVEL[rank] || 'ok'), x: fx(x), y: fx(H - hh), width: fx(Math.max(0.6, step / 86400 * W - 0.25)), height: fx(hh) }));
    });
    const inc = d.incidents && d.incidents.latest;
    const marks = [];
    if (inc && inc.start && inc.start > t0) marks.push({ t: inc.start, open: inc.open });
    marks.forEach(m => g.appendChild(svg('path', { class: 'now-tl__mark' + (m.open ? ' is-open' : ''), d: 'M' + fx((m.t - t0) / 86400 * W) + ' 0v' + H })));
    const fig = h('button', { class: 'now-tl__fig', type: 'button', 'aria-label': t('now.tl.open') }, g);
    fig.addEventListener('click', ev => {
      const r = fig.getBoundingClientRect();
      const at = Math.round(t0 + Math.max(0, Math.min(1, (ev.clientX - r.left) / Math.max(1, r.width))) * 86400);
      LL.go('#/map?rw=' + at);
    });
    const hours = [0, 6, 12, 18, 24].map(k => {
      const d2 = new Date((t0 + k * 3600) * 1000);
      return h('span', {}, k === 24 ? t('now.tl.now') : d2.toLocaleTimeString(LL.locale(), { hour: '2-digit', minute: '2-digit', hour12: false }));
    });
    const worst = hist.filter(x => x[0] >= t0).reduce((a, x) => Math.max(a, x[1]), 0);
    return h('section', { class: 'card now-card now-tl', 'aria-labelledby': 'now-h-tl' },
      h('header', { class: 'now-card__head' }, h('h2', { id: 'now-h-tl' }, t('now.tl.title')),
        h('span', { class: 'chip chip--' + (worst === 2 ? 'danger' : worst === 1 ? 'warn' : 'ok') }, t('now.tl.worst.' + (LEVEL[worst] || 'ok')))),
      hist.length ? [fig, h('div', { class: 'now-tl__axis', 'aria-hidden': 'true' }, hours),
        h('p', { class: 'muted now-tl__hint' }, t('now.tl.hint'))] : h('p', { class: 'muted' }, t('now.tl.none')),
      h('div', { class: 'btn-row btn-row--wrap' },
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/map?rw=' + Math.round(t0) }, t('now.tl.replay')),
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/incidents' }, t('now.tl.incidents'))));
  }

  function focusCard() {
    return h('section', { class: 'card now-card now-focus-entry' },
      h('div', { class: 'now-focus-entry__icon', 'aria-hidden': 'true' }),
      h('div', { class: 'now-focus-entry__text' }, h('h2', {}, t('now.focus.title')), h('p', { class: 'muted' }, t('now.focus.text'))),
      h('a', { class: 'btn btn--sm', href: '#/focus' }, t('now.focus.open')));
  }

  // ------------------------------------------------------------------ the page
  let last = null;            // the newest answers, the sheets read them
  async function nowView(app) {
    const fr = LL.frame(app, 'now', h('div', { class: 'page now-page' }));
    const page = fr.main.firstChild;
    const site = LL.site && LL.site();
    const updated = h('span', { class: 'muted now-head__time' });
    const body = h('div', { class: 'now-body' }, h('div', { class: 'skeleton' }));
    page.append(h('header', { class: 'page__head now-head' },
      h('div', {}, h('h1', {}, t('nav.now')), site && site.several ? h('p', { class: 'muted now-head__site' }, LL.siteName(site)) : null), updated), body);
    let seq = 0, timer = 0;
    async function load() {
      const my = ++seq;
      let ov, rn, sites = null;
      try {
        [ov, rn] = await Promise.all([api('/api/overview'), api('/api/overview/now')]);
        if (site && site.several) { try { sites = (await api('/api/sites/overview')).sites; } catch (e) { sites = null; } }
      } catch (e) {
        if (my === seq && !last) body.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e)));
        return;
      }
      if (my !== seq || !page.isConnected) return;
      last = { ov, rn, sites };
      const y = fr.main.scrollTop;
      const extra = LL.nowCards.filter(c => { try { return c.available(); } catch (e) { return false; } }).map(c => {
        try { return c.render(); } catch (e) { return null; }
      });
      body.replaceChildren(...[ring(ov), sitesCard(sites), changesCard(rn.changes || []), lookBack(ov), ...extra, focusCard()].filter(Boolean));
      updated.textContent = t('now.updated', { time: new Date(ov.t * 1000).toLocaleTimeString(LL.locale(), { hour12: false }) });
      fr.main.scrollTop = y;
      if (LL.sheets.top() && LL.sheets.top().id !== 'more' && LL.sheets.top().id !== 'lane') redrawSheet();
    }
    LL.pullToRefresh(fr.main, load);
    LL.nowRedraw = () => { if (page.isConnected) load(); };
    await load();
    LL.sheets.fromAddress();
    timer = setInterval(() => { if (!page.isConnected) { clearInterval(timer); return; } if (!document.hidden) load(); }, REFRESH_MS);
    const destroy = fr.destroy;
    fr.destroy = () => { clearInterval(timer); LL.sheets.reset(); LL.nowRedraw = null; if (destroy) destroy.call(fr); };
    return fr;
  }
  LL.view('now', app => nowView(app));

  // ------------------------------------------------------------------ the sheets of this page
  let sheetBody = null, sheetId = null;
  function redrawSheet() {
    if (!sheetBody || !sheetBody.isConnected || !last) return;
    const kind = { notes: drawNotes, changes: drawChanges, sites: drawSites }[sheetId];
    if (kind) kind(sheetBody);
  }
  async function need() { if (!last) { const [ov, rn] = await Promise.all([api('/api/overview'), api('/api/overview/now')]); last = { ov, rn, sites: null }; } }
  function drawNotes(body) {
    const notes = last.ov.notes || [];
    body.replaceChildren(notes.length ? h('ul', { class: 'note-list' }, notes.map(LL.noteRow)) : h('p', { class: 'muted' }, t('now.calm')),
      h('p', { class: 'sheet__anchor' }, h('a', { href: '#', class: 'sheet-link', onclick: ev => { ev.preventDefault(); LL.sheets.open('changes'); } }, t('now.sheet.to_changes'))));
  }
  function drawChanges(body) {
    const ch = last.rn.changes || [];
    body.replaceChildren(ch.length ? h('ul', { class: 'change-list' }, ch.map(changeRow)) : h('p', { class: 'muted' }, t('now.changes.none')),
      h('p', { class: 'sheet__anchor' }, h('a', { href: '#', class: 'sheet-link', onclick: ev => { ev.preventDefault(); LL.sheets.open('notes'); } }, t('now.sheet.to_notes'))));
  }
  function drawSites(body) {
    const cur = LL.site && LL.site() ? LL.site().id : null;
    body.replaceChildren(h('ul', { class: 'now-sites__list' }, (last.sites || []).map(s => siteRow(s, cur))),
      LL.can('admin') ? h('a', { class: 'btn btn--ghost btn--sm', href: '#/sites' }, t('now.sites.manage')) : null);
  }
  const sheetKind = (id, title, draw) => LL.sheets.register(id, { title, render: async body => { sheetBody = body; sheetId = id; await need(); draw(body); } });
  sheetKind('notes', 'now.sheet.notes', drawNotes);
  sheetKind('changes', 'now.sheet.changes', drawChanges);
  sheetKind('sites', 'now.sheet.sites', drawSites);

  // the phone's "More": every view of the navigation that is not in the bar, as a list
  LL.sheets.register('more', {
    title: 'now.more.title',
    render(body) {
      const tabs = [...document.querySelectorAll('.tabs a.tab')];
      body.replaceChildren(h('ul', { class: 'more-list' }, tabs.map(a => {
        const icon = a.querySelector('.tab__icon');
        return h('li', {}, h('a', { class: 'more-list__item' + (a.classList.contains('is-active') ? ' is-active' : ''), href: a.getAttribute('href'),
          onclick: () => { LL.sheets.reset(); } },
        h('span', { class: 'more-list__icon', 'aria-hidden': 'true', html: null }), h('span', { class: 'more-list__label' }, a.getAttribute('title') || a.textContent)));
      }).map((li, i) => { li.querySelector('.more-list__icon').innerHTML = (tabs[i].querySelector('.tab__icon') || {}).innerHTML || ''; return li; })),
      h('div', { class: 'btn-row btn-row--wrap more-list__foot' }, h('a', { class: 'btn btn--ghost btn--sm', href: '#/focus', onclick: () => LL.sheets.reset() }, t('nav.focus')),
        LL.energyPeek && LL.energyPeek.available ? h('a', { class: 'btn btn--ghost btn--sm', href: '#/energy', onclick: () => LL.sheets.reset() }, t('nav.energy')) : null));
    }
  });
  // the button at the end of the phone's bar
  LL.moreButton = function () {
    const b = h('button', { class: 'tab tab--more', type: 'button', 'aria-label': t('now.more.title'), title: t('now.more.title'), 'aria-haspopup': 'dialog',
      onclick: () => LL.sheets.open('more') }, h('span', { class: 'tab__icon', 'aria-hidden': 'true' }), h('span', { class: 'tab__label' }, t('nav.more')));
    b.querySelector('.tab__icon').innerHTML = LL.NAV_MORE_ICON;
    return b;
  };
  // the views the phone's bar always shows (the open one comes in addition when it is another)
  LL.PHONE_TABS = ['now', 'map', 'controlroom', 'devices'];

  // ------------------------------------------------------------------ Focus
  const nameOf = c => (c.title || (c.title_key ? t(c.title_key) : '') || t('kind.' + (c.kind || 'unknown')));
  async function focusView(app, rest) {
    const want = rest && rest[0] ? decodeURIComponent(rest[0]) : null;
    const fr = LL.frame(app, 'focus', h('div', { class: 'page now-page focus-page' }));
    const page = fr.main.firstChild;
    const body = h('div', { class: 'focus-body' }, h('div', { class: 'skeleton' }));
    page.append(h('header', { class: 'page__head now-head' }, h('div', {}, h('h1', {}, t('nav.focus')), h('p', { class: 'muted' }, t('focus.lead')))), body);
    let data;
    try { data = await api('/api/map?view=network'); } catch (e) { body.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); return fr; }
    if (!page.isConnected) return fr;
    const comps = new Map(data.components.filter(c => !c.quiet_more).map(c => [c.id, c]));
    const pick = want && comps.has(want) ? want : (data.components.find(c => c.kind === 'router') || data.components.find(c => c.kind !== 'internet') || {}).id;
    if (!pick) { body.replaceChildren(h('p', { class: 'muted' }, t('focus.empty'))); return fr; }
    if (pick !== want) history.replaceState(null, '', LL.base + '#/focus/' + encodeURIComponent(pick));
    const centre = comps.get(pick);
    const partners = [];
    data.traces.forEach(tr => {
      const other = tr.a === pick ? tr.b : tr.b === pick ? tr.a : null;
      if (other && comps.has(other) && !partners.some(p => p.c.id === other)) partners.push({ c: comps.get(other), kind: tr.kind, up: tr.b === pick, measured: tr.measured });
    });
    const order = { uplink: 0, cable: 1, lan: 2, radio: 3, virtual: 4 };
    partners.sort((a, b) => (b.up - a.up) || ((order[a.kind] ?? 9) - (order[b.kind] ?? 9)) || nameOf(a.c).localeCompare(nameOf(b.c)));
    // the picker: every component by name
    const sel = h('select', { class: 'focus-pick', 'aria-label': t('focus.pick') },
      [...comps.values()].sort((a, b) => nameOf(a).localeCompare(nameOf(b))).map(c => h('option', { value: c.id, selected: c.id === pick }, nameOf(c))));
    sel.addEventListener('change', () => LL.go('#/focus/' + encodeURIComponent(sel.value)));
    const fig = ringFigure(centre, partners.slice(0, RING_MAX));
    const list = h('ul', { class: 'focus-list' }, partners.map(p => h('li', { class: 'focus-row', dataset: { id: p.c.id, kind: p.kind } },
      h('a', { class: 'focus-row__link', href: '#/focus/' + encodeURIComponent(p.c.id) },
        h('i', { class: 'focus-row__trace focus-trace--' + p.kind, 'aria-hidden': 'true' }),
        h('span', { class: 'focus-row__main' }, h('span', { class: 'focus-row__name' }, nameOf(p.c)),
          h('span', { class: 'focus-row__sub muted' }, t('focus.link.' + (order[p.kind] != null ? p.kind : 'other')) + (p.up ? ' · ' + t('focus.upstream') : ''))),
        h('span', { class: 'focus-row__state', dataset: { tile: p.c.id } }, t('board.grade.' + (p.c.grade || 'unknown')))))));
    const acts = h('div', { class: 'btn-row btn-row--wrap focus-acts' },
      h('a', { class: 'btn btn--sm', href: '#/map/' + encodeURIComponent(pick) }, t('focus.on_map')),
      /^d-/.test(pick) ? h('a', { class: 'btn btn--ghost btn--sm', href: '#/devices/' + encodeURIComponent(pick) }, t('focus.card')) : null);
    body.replaceChildren(h('div', { class: 'focus-top' }, sel, acts),
      h('section', { class: 'card focus-card' }, fig,
        h('p', { class: 'focus-sum muted' }, partners.length ? t('focus.sum', { name: nameOf(centre), n: partners.length }) : t('focus.alone', { name: nameOf(centre) })),
        partners.length > RING_MAX ? h('p', { class: 'muted focus-more' }, t('focus.more', { n: partners.length - RING_MAX })) : null),
      partners.length ? h('section', { class: 'card focus-partners', 'aria-labelledby': 'focus-h-list' }, h('h2', { id: 'focus-h-list' }, t('focus.partners')), list) : null);
    // live: grades and metrics of the centre and the partners
    const unsub = LL.live.subscribe(f => {
      if (!fig.isConnected) return;
      Object.entries(f.grades || {}).forEach(([id, g]) => {
        fig.querySelectorAll('[data-tile="' + CSS.escape(id) + '"]').forEach(el => { el.dataset.grade = g; });
        list.querySelectorAll('.focus-row__state[data-tile="' + CSS.escape(id) + '"]').forEach(el => { el.textContent = t('board.grade.' + g); });
      });
      const m = f.metrics && f.metrics[pick];
      const mel = fig.querySelector('.focus-centre__metric');
      if (mel && m != null) mel.textContent = String(m);
    });
    const destroy = fr.destroy;
    fr.destroy = () => { unsub(); if (destroy) destroy.call(fr); };
    return fr;
  }
  // the centre and the ring of partners as an SVG; every partner is a link
  function ringFigure(c, partners) {
    const W = 400, H = 390, cx = W / 2, cy = H / 2, RX = 140, RY = 132, NODE = 16;
    const many = partners.length > 8;
    const g = svg('svg', { class: 'focus-fig', viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': t('focus.fig', { name: nameOf(c), n: partners.length }) });
    const lines = svg('g', { class: 'focus-lines' }), nodes = svg('g', { class: 'focus-nodes' });
    g.append(lines, nodes);
    const short = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
    partners.forEach((p, i) => {
      const a = -Math.PI / 2 + i * 2 * Math.PI / Math.max(1, partners.length);
      const x = cx + RX * Math.cos(a), y = cy + RY * Math.sin(a);
      // the line ends at the edge of the partner's circle; the name sits on the far side, so no line crosses it
      const len = Math.hypot(x - cx, y - cy) || 1;
      const ex = x - (x - cx) / len * NODE, ey = y - (y - cy) / len * NODE;
      lines.appendChild(svg('path', { class: 'focus-line focus-trace--' + p.kind + (p.measured ? '' : ' is-guess'), d: 'M' + fx(cx) + ' ' + fx(cy) + 'L' + fx(ex) + ' ' + fx(ey) }));
      const name = nameOf(p.c);
      const below = Math.sin(a) > -0.15;            // names under the circle, only the upper ones above it
      const tx = x;
      const ty = below ? y + NODE + 14 + (many && i % 2 && Math.sin(a) > 0.5 ? 12 : 0) : y - NODE - 8;
      const link = svg('a', { href: '#/focus/' + encodeURIComponent(p.c.id), class: 'focus-node', 'data-tile': p.c.id, 'data-grade': p.c.grade || 'unknown' },
        svg('title', {}, name),
        svg('circle', { class: 'focus-node__dot', cx: fx(x), cy: fx(y), r: NODE }),
        svg('circle', { class: 'focus-node__led', cx: fx(x + 11), cy: fx(y - 11), r: 3.5 }),
        svg('text', { class: 'focus-node__name', x: fx(tx), y: fx(ty), 'text-anchor': 'middle' }, short(name, many ? 13 : 17)));
      if (LL.Icons && LL.Icons.svg) {
        const ic = svg('svg', { x: fx(x - 9), y: fx(y - 9), width: 18, height: 18, class: 'focus-node__icon', 'aria-hidden': 'true' });
        ic.innerHTML = LL.Icons.svg(p.c.kind);
        link.insertBefore(ic, link.querySelector('.focus-node__led'));
      }
      nodes.appendChild(link);
    });
    const centre = svg('g', { class: 'focus-centre', 'data-tile': c.id, 'data-grade': c.grade || 'unknown' },
      svg('rect', { class: 'focus-centre__tile', x: fx(cx - 62), y: fx(cy - 28), width: 124, height: 56, rx: 12 }),
      svg('circle', { class: 'focus-node__led', cx: fx(cx + 50), cy: fx(cy - 16), r: 4 }),
      svg('text', { class: 'focus-centre__name', x: fx(cx), y: fx(cy - 3), 'text-anchor': 'middle' }, short(nameOf(c), 16)),
      svg('text', { class: 'focus-centre__metric', x: fx(cx), y: fx(cy + 16), 'text-anchor': 'middle' }, c.metric || t('kind.' + (c.kind || 'unknown'))));
    nodes.appendChild(centre);
    return g;
  }
  LL.view('focus', (app, rest) => focusView(app, rest));

  // ------------------------------------------------------------------ palette
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'go.now', group: 'views', order: -0.9, label: () => t('cmdk.go', { name: t('nav.now') }), keywords: () => t('now.cmd.kw'), run: () => LL.go('#/now') });
    LL.registerCommand({ id: 'go.focus', group: 'views', order: -0.8, label: () => t('cmdk.go', { name: t('nav.focus') }), keywords: () => t('focus.cmd.kw'), run: () => LL.go('#/focus') });
  }
})();
