/* Lanloom — Overview (start page): how the network is doing, in three seconds.

   Health with a short look back, key figures (each a deep link), and cards from a register: noteworthy issues (one sub
   line each), the nightly report, new and unsure devices (first seen, origin class) and the freshness of every source
   with its last sign of life. The page draws itself again every few seconds; open/closed parts keep their state
   (LL.keepOpen), so the live rhythm never folds what the reader opened.

   Later features add their own figure or card: LL.registerOverviewTile({id, order, available(d), render(d)}) and
   LL.registerOverviewCard({id, order, available(d), title(d), render(d)}). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const EVERY_MS = 10000;
  const LIST_SHORT = 5;

  // ------------------------------------------------------------------ registers
  LL.overviewTiles = LL.overviewTiles || [];
  LL.registerOverviewTile = function (tile) { LL.overviewTiles = LL.overviewTiles.filter(x => x.id !== tile.id).concat([tile]).sort((a, b) => a.order - b.order); };
  LL.overviewCards = LL.overviewCards || [];
  LL.registerOverviewCard = function (card) { LL.overviewCards = LL.overviewCards.filter(x => x.id !== card.id).concat([card]).sort((a, b) => a.order - b.order); };

  const ago = ts => t('time.ago', { age: LL.fmtAge(Date.now() / 1000 - ts) });
  const devName = d => d.title || (d.kind && d.kind !== 'unknown' ? t('kind.' + d.kind) : d.ip);

  // ------------------------------------------------------------------ key figures
  function tile(id, label, value, sub, href, level, onclick) {
    const attrs = { class: 'ov-kpi ov-kpi--' + id + (level ? ' ov-kpi--' + level : ''), dataset: { kpi: id } };
    const kids = [h('span', { class: 'ov-kpi__label' }, label), h('b', { class: 'ov-kpi__value' }, value), h('span', { class: 'ov-kpi__sub' }, sub || ' ')];
    if (onclick) return h('button', Object.assign(attrs, { type: 'button', onclick }), kids);
    return h('a', Object.assign(attrs, { href }), kids);
  }
  LL.registerOverviewTile({ id: 'devices', order: 10, render: d => tile('devices', t('ov.kpi.devices'), LL.fmtNum(d.devices.total, 0),
    d.devices.hidden ? t('ov.kpi.devices.hidden', { n: d.devices.hidden }) : t('ov.kpi.devices.sub'), '#/devices') });
  LL.registerOverviewTile({ id: 'online', order: 20, render: d => tile('online', t('ov.kpi.online'), LL.fmtNum(d.devices.online, 0),
    d.devices.offline ? t('ov.kpi.online.offline', { n: d.devices.offline }) : t('ov.kpi.online.all'), d.devices.offline ? '#/devices?show=offline' : '#/devices',
    d.devices.offline ? 'warn' : '') });
  LL.registerOverviewTile({ id: 'new', order: 30, render: d => tile('new', t('ov.kpi.new'), LL.fmtNum(d.devices.new, 0),
    d.devices.new ? t('ov.kpi.new.sub') : t('ov.kpi.new.none'), '#/devices?show=new', d.devices.new ? 'info' : '') });
  LL.registerOverviewTile({ id: 'sources', order: 40, render: d => tile('sources', t('ov.kpi.sources'),
    d.sources.total ? d.sources.fresh + ' / ' + d.sources.total : '—',
    d.sources.stale ? t('ov.kpi.sources.stale', { n: d.sources.stale }) : d.sources.waiting ? t('ov.kpi.sources.waiting', { n: d.sources.waiting }) : (d.sources.total ? t('ov.kpi.sources.fresh') : t('ov.kpi.sources.none')),
    null, d.sources.stale ? 'warn' : '', () => LL.openLiveGrade && LL.openLiveGrade()) });
  LL.registerOverviewTile({ id: 'wan', order: 50, render: d => {
    const w = d.wan;
    const sub = w.state === 'down' ? t('ov.kpi.wan.down_sub')
      : w.measured ? '↓ ' + LL.fmtRate(w.down) + '  ↑ ' + LL.fmtRate(w.up)
      : w.last_outage ? t('ov.kpi.wan.last_outage', { age: ago(w.last_outage) })
      : w.state === 'up' ? t('ov.kpi.wan.unmeasured') : t('ov.kpi.wan.unknown_sub');
    return tile('wan', t('ov.kpi.wan'), t('ov.wan.' + w.state), sub, '#/map/internet', w.state === 'down' ? 'danger' : '');
  } });
  LL.registerOverviewTile({ id: 'paths', order: 60, render: d => {
    const p = d.paths;
    if (!p.available) return tile('paths', t('ov.kpi.paths'), '—', t('ov.kpi.paths.none'), '#/paths');
    const top = (p.top || []).map(c => c.cc).join(' · ');
    return tile('paths', t('ov.kpi.paths'), LL.fmtBytes(p.tx + p.rx), top ? t('ov.kpi.paths.top', { top, n: p.countries }) : t('ov.kpi.paths.sub'), '#/paths', p.unusual ? 'info' : '');
  } });

  // ------------------------------------------------------------------ cards
  function moreList(id, items, render) {
    const all = LL.keepOpen.get('ov:more:' + id, false);
    const list = h('ul', { class: 'ov-list' }, (all ? items : items.slice(0, LIST_SHORT)).map(render));
    const more = items.length > LIST_SHORT ? h('button', { class: 'btn btn--ghost btn--sm ov-more', type: 'button', 'aria-expanded': all ? 'true' : 'false',
      onclick: () => { LL.keepOpen.set('ov:more:' + id, !all); draw(); } }, all ? t('ov.less') : t('ov.more', { n: items.length - LIST_SHORT })) : null;
    return [list, more];
  }
  const devRow = (d, extra) => h('li', { class: 'ov-dev' }, h('a', { class: 'ov-dev__link', href: '#/devices/' + encodeURIComponent(d.id) },
    LL.kindIcon ? LL.kindIcon(d.kind) : null,
    h('span', { class: 'ov-dev__main' }, h('span', { class: 'ov-dev__title' }, devName(d)),
      h('span', { class: 'ov-dev__sub' }, h('span', { class: 'mono' }, d.ip), ' · ', extra(d))),
    h('span', { class: 'chip chip--muted ov-origin ov-origin--' + (d.origin || 'no_mac') }, t('ident.origin_short.' + (d.origin || 'no_mac')))));
  LL.registerOverviewCard({ id: 'notes', order: 10, title: () => t('ov.card.notes'), count: d => d.notes.length,
    render: d => (d.notes.length ? h('ul', { class: 'note-list' }, d.notes.map(LL.noteRow)) : h('p', { class: 'ov-calm' }, t('ov.notes.none'))) });
  LL.registerOverviewCard({ id: 'report', order: 20, available: d => d.report.available, title: () => t('ov.card.report'), count: d => d.report.unread || null,
    render: d => {
      const r = d.report, l = r.latest;
      if (!l) return h('div', {}, h('p', { class: 'ov-calm' }, t('ov.report.none')), r.next_at ? h('p', { class: 'muted' }, t('ov.report.next', { when: LL.fmtDate(r.next_at) })) : null);
      const state = !l.points ? t('ov.report.quiet') : l.open ? t('ov.report.open', { n: l.open, total: l.points }) : t('ov.report.done', { n: l.points });
      return h('div', { class: 'ov-report' },
        h('a', { class: 'ov-report__link ov-report__link--' + (l.open ? l.level : 'ok'), href: '#/reports/' + l.day },
          h('span', { class: 'ov-report__day' }, LL.fmtDate(l.created)), h('span', { class: 'ov-report__state' }, state),
          !l.read ? h('span', { class: 'chip chip--accent' }, t('ov.report.unread')) : null),
        r.unread > 1 ? h('p', { class: 'muted' }, t('ov.report.unread_more', { n: r.unread })) : null,
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/reports' }, t('ov.report.all')));
    } });
  LL.registerOverviewCard({ id: 'new', order: 30, title: () => t('ov.card.new'), count: d => d.devices.new,
    render: d => (d.new.length ? h('div', {}, moreList('new', d.new, x => devRow(x, y => t('ov.first_seen', { when: ago(y.first_seen) }))),
      h('a', { class: 'btn btn--ghost btn--sm', href: '#/devices?show=new' }, t('ov.new.all'))) : h('p', { class: 'ov-calm' }, t('ov.new.none'))) });
  LL.registerOverviewCard({ id: 'unsure', order: 40, title: () => t('ov.card.unsure'), count: d => d.devices.unsure,
    render: d => (d.unsure.length ? h('div', {}, h('p', { class: 'muted ov-lead' }, t('ov.unsure.lead')),
      moreList('unsure', d.unsure, x => devRow(x, y => (y.vendor || t('kind.' + y.kind)))),
      h('a', { class: 'btn btn--ghost btn--sm', href: '#/devices?show=unsure' }, t('ov.unsure.all'))) : h('p', { class: 'ov-calm' }, t('ov.unsure.none'))) });
  LL.registerOverviewCard({ id: 'sources', order: 50, title: () => t('ov.card.sources'), count: d => d.source_list.length,
    render: d => {
      if (!d.source_list.length) return h('p', { class: 'ov-calm' }, t('ov.sources.none'));
      const rows = d.source_list.map(s => {
        const name = s.kind ? s.name : t(s.name);
        const life = s.age_s != null ? t('ov.src.life', { age: LL.fmtAge(s.age_s) }) : t('srcstate.' + s.state + '.hint');
        const reason = s.state === 'failed' && s.key ? t(s.key) : (s.cadence_s ? t('ov.src.cadence', { c: LL.fmtAge(s.cadence_s) }) : '');
        return h('li', { class: 'ov-src ov-src--' + s.state },
          h('span', { class: 'grade-chip ov-src__grade', dataset: { grade: s.state === 'fresh' ? (s.grade === 'unknown' ? 'periodic' : s.grade) : (s.state === 'stale' || s.state === 'failed' ? 'interrupted' : 'unknown') } }, h('i')),
          h('span', { class: 'ov-src__main' }, h('span', { class: 'ov-src__name' }, name), h('span', { class: 'ov-src__sub' }, [life, reason].filter(Boolean).join(' · '))),
          h('span', { class: 'chip ov-src__state chip--' + ({ fresh: 'ok', stale: 'warn', failed: 'danger' }[s.state] || 'muted') }, t('srcstate.' + s.state)));
      });
      return h('div', {}, h('ul', { class: 'ov-list' }, rows),
        LL.can('admin') ? h('a', { class: 'btn btn--ghost btn--sm', href: '#/settings' }, t('ov.sources.manage')) : null);
    } });

  // ------------------------------------------------------------------ health with a short look back
  const LEVEL = ['ok', 'info', 'warn', 'danger'];
  function strip(hist, step) {
    const W = 288, H = 28, NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H); svg.setAttribute('preserveAspectRatio', 'none'); svg.setAttribute('class', 'ov-strip__svg');
    svg.setAttribute('role', 'img');
    const bad = hist.filter(p => p[1] >= 2).length;
    svg.setAttribute('aria-label', t('ov.health.strip_label', { n: bad }));
    if (!hist.length) return svg;
    const end = hist[hist.length - 1][0], start = end - step * (W - 1);
    let run = null;
    const flush = () => { if (!run) return; const r = document.createElementNS(NS, 'rect');
      r.setAttribute('x', run.x0); r.setAttribute('y', 0); r.setAttribute('width', Math.max(1, run.x1 - run.x0 + 1)); r.setAttribute('height', H);
      r.setAttribute('class', 'ov-strip__run ov-strip__run--' + LEVEL[run.l]);
      const tt = document.createElementNS(NS, 'title'); tt.textContent = LL.fmtDate(run.t0) + ' – ' + t('ov.health.' + LEVEL[run.l]); r.appendChild(tt);
      svg.appendChild(r); run = null; };
    for (const p of hist) {
      const x = Math.round((p[0] - start) / step);
      if (x < 0 || x >= W) continue;
      if (run && run.l === p[1] && x <= run.x1 + 2) run.x1 = x; else { flush(); run = { x0: x, x1: x, l: p[1], t0: p[0] }; }
    }
    flush();
    return svg;
  }
  function healthCard(d) {
    const lv = d.health.level;
    const parts = [t('ov.health.devices', { online: d.devices.online, total: d.devices.total })];
    if (d.sources.total) parts.push(d.sources.stale ? t('ov.health.sources_stale', { n: d.sources.stale }) : t('ov.health.sources_fresh', { n: d.sources.fresh }));
    parts.push(t('ov.health.wan.' + d.wan.state));
    const icon = h('span', { class: 'ov-health__icon', 'aria-hidden': 'true' });
    icon.innerHTML = lv === 'ok' ? '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.2 4.2L19 7"/></svg>'
      : '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 7.5v6M12 16.8v.1"/></svg>';
    const hist = d.health.history || [];
    const bad = hist.filter(p => p[1] >= 2).length;
    return h('section', { class: 'card ov-health ov-health--' + lv, 'aria-live': 'polite' },
      h('div', { class: 'ov-health__state' }, icon,
        h('div', { class: 'ov-health__text' }, h('h2', { class: 'ov-health__title' }, t('ov.health.' + lv + '.title', { n: d.health.notes })),
          h('p', { class: 'ov-health__line' }, parts.join(' · ')))),
      h('div', { class: 'ov-strip' }, h('div', { class: 'ov-strip__head' }, h('span', {}, t('ov.health.lookback')),
        h('span', { class: 'muted' }, hist.length ? (bad ? t('ov.health.bad_time', { age: LL.fmtAge(bad * d.health.step_s) }) : t('ov.health.calm')) : t('ov.health.collecting'))),
        strip(hist, d.health.step_s),
        h('div', { class: 'ov-strip__axis muted', 'aria-hidden': 'true' }, h('span', {}, t('ov.health.axis_start')), h('span', {}, t('ov.health.axis_end')))));
  }

  // ------------------------------------------------------------------ page
  let S = null;
  function draw() {
    if (!S || !S.page.isConnected || !S.d) return;
    const d = S.d;
    if (d.empty) {
      S.body.replaceChildren(h('div', { class: 'card empty-state ov-empty' }, h('div', { class: 'empty-state__art' }, LL.logo(56)), h('h2', {}, t('ov.empty.title')),
        h('p', { class: 'muted' }, t(LL.can('admin') ? 'map.empty.text' : 'map.empty.text_viewer')),
        LL.can('admin') ? h('a', { class: 'btn btn--primary', href: '#/settings' }, t('map.empty.action')) : null));
      return;
    }
    const kpis = h('div', { class: 'ov-kpis' }, LL.overviewTiles.filter(x => !x.available || x.available(d)).map(x => { try { return x.render(d); } catch (e) { return null; } }));
    const cards = h('div', { class: 'ov-cards' }, LL.overviewCards.filter(c => !c.available || c.available(d)).map(c => {
      let body; try { body = c.render(d); } catch (e) { body = h('p', { class: 'ov-calm' }, t('error.generic')); }
      const n = c.count ? c.count(d) : null;
      const det = LL.keepOpen.details('ov:card:' + c.id, true, h('span', { class: 'ov-card__title' }, c.title(d), n ? h('span', { class: 'ov-card__n' }, LL.fmtNum(n, 0)) : null), h('div', { class: 'ov-card__body' }, body));
      return h('section', { class: 'card ov-card ov-card--' + c.id, dataset: { card: c.id } }, det);
    }));
    S.stamp.textContent = t('ov.stamp', { time: new Date(d.t * 1000).toLocaleTimeString(LL.locale(), { hour12: false, hour: '2-digit', minute: '2-digit' }) });
    const y = S.scroller ? S.scroller.scrollTop : 0;
    // keyboard focus survives the redraw: remember the focused control by its class list and its index among equals (read from the DOM)
    const ae = document.activeElement; let fKey = null, fIdx = 0;
    if (ae && ae !== document.body && S.body.contains(ae)) {
      fKey = (typeof ae.className === 'string' ? ae.className.trim().split(/\s+/).filter(Boolean) : []).map(c => '.' + CSS.escape(c)).join('') || ae.tagName.toLowerCase();
      fIdx = [...S.body.querySelectorAll(fKey)].indexOf(ae);
    }
    S.body.replaceChildren(healthCard(d), kpis, cards);
    if (S.scroller) S.scroller.scrollTop = y;
    if (fKey && fIdx >= 0) { const again = S.body.querySelectorAll(fKey)[fIdx]; if (again && again.focus) again.focus({ preventScroll: true }); }
  }
  async function load() {
    if (!S || document.hidden) return;
    try { S.d = await api('/api/overview'); S.err = false; } catch (e) { if (!S.d) S.body.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
    draw();
  }
  LL.overviewRedraw = draw;              // tests: one live redraw on demand

  async function overviewView(app) {
    const fr = LL.frame(app, 'overview');
    const stamp = h('span', { class: 'muted ov-stamp' });
    const body = h('div', { class: 'ov-body' }, h('div', { class: 'skeleton ov-skel' }));
    const page = h('div', { class: 'page page--wide ov-page' }, h('div', { class: 'page__head ov-head' }, h('h1', {}, t('nav.overview')), stamp), body);
    fr.main.append(page);
    S = { page, body, stamp, d: null, scroller: fr.main };
    await load();
    const timer = setInterval(load, EVERY_MS);
    const onVis = () => { if (!document.hidden) load(); };
    document.addEventListener('visibilitychange', onVis);
    return { destroy: () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVis); S = null; fr.destroy(); } };
  }
  LL.view('overview', overviewView);
})();
