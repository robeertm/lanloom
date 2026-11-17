/* Lanloom — main views: map (overview / network), devices, settings, detail panel, live feed, update notice. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const GRADES = ['realtime', 'near', 'periodic', 'sluggish', 'interrupted', 'unknown'];

  // ------------------------------------------------------------------ live feed (one socket for all views)
  // state: connecting | live | offline | paused (tab in the background: the socket rests, nothing is drawn)
  const live = { ws: null, listeners: new Set(), statusListeners: new Set(), last: null, lastAt: 0, offset: null, retry: 1000, timer: null,
    state: 'connecting', paused: false };
  function liveState(st) {
    if (live.state === st) return;
    live.state = st;
    live.statusListeners.forEach(fn => { try { fn(st); } catch (e) { /* a listener must not stop the feed */ } });
  }
  function liveConnect() {
    if (live.paused) { liveState('paused'); return; }
    if (live.ws && live.ws.readyState <= 1) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    if (live.state !== 'live') liveState('connecting');
    const ws = live.ws = new WebSocket(proto + '//' + location.host + LL.url('/api/live'));
    ws.onmessage = ev => {
      let f; try { f = JSON.parse(ev.data); } catch (e) { return; }
      live.last = f; live.retry = 1000; live.lastAt = Date.now();
      if (f.t) live.offset = Date.now() / 1000 - f.t;       // clock offset browser - server (plus the transport time)
      liveState('live');
      live.listeners.forEach(fn => fn(f));
    };
    ws.onclose = () => {
      if (live.ws === ws) live.ws = null;
      if (live.paused) { liveState('paused'); return; }
      liveState('offline');
      if (live.listeners.size) { clearTimeout(live.timer); live.timer = setTimeout(liveConnect, live.retry); live.retry = Math.min(15000, live.retry * 2); }
    };
  }
  function liveSubscribe(fn) { live.listeners.add(fn); liveConnect(); if (live.last) fn(live.last); return () => live.listeners.delete(fn); }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      live.paused = true; clearTimeout(live.timer);
      if (live.ws) { try { live.ws.close(); } catch (e) { /* closing */ } }
      liveState('paused');
    } else if (live.paused) {
      live.paused = false; live.retry = 1000;
      if (live.listeners.size) liveConnect(); else liveState('connecting');
    }
  });
  LL.live = {
    subscribe: liveSubscribe,
    onStatus(fn) { live.statusListeners.add(fn); return () => live.statusListeners.delete(fn); },
    state: () => live.state, lastAt: () => live.lastAt, last: () => live.last, offset: () => live.offset,
    reconnect() {
      live.paused = false; live.retry = 1000; clearTimeout(live.timer);
      const old = live.ws; live.ws = null;
      if (old) { try { old.onclose = null; old.close(); } catch (e) { /* closing */ } }
      liveState('connecting'); liveConnect();
    }
  };

  // ------------------------------------------------------------------ app frame (header + nav)
  function frame(app, active, content) {
    const tabs = [['overview', 'nav.overview'], ['map', 'nav.map'], ['network', 'nav.network'], ['paths', 'nav.paths'], ['devices', 'nav.devices'], ['firewall', 'nav.firewall'], ['reports', 'nav.reports'], ['settings', 'nav.settings']];
    if (LL.state && (LL.state.hubs || []).length) tabs.splice(5, 0, ['sensors', 'nav.sensors'], ['automations', 'nav.automations']);   // the hub's sensors and automations, only with a hub connection
    const nav = h('nav', { class: 'tabs', 'aria-label': t('nav.label') }, tabs.map(([k, label]) =>
      h('a', { href: '#/' + k, class: 'tab tab--' + k + (k === active ? ' is-active' : '') + (LL.strings['nav.short.' + k] ? ' tab--short' : ''), 'aria-current': k === active ? 'page' : null,
        title: t(label), 'aria-label': t(label) },   // narrow screens show only the icon
        h('span', { class: 'tab__icon', 'aria-hidden': 'true' }), h('span', { class: 'tab__label' }, t(label)),
        LL.strings['nav.short.' + k] ? h('span', { class: 'tab__short', 'aria-hidden': 'true' }, t('nav.short.' + k)) : null)));
    const navIcon = k => NAV_ICONS[k] || ({ paths: LL.NAV_PATHS_ICON, reports: LL.NAV_REPORTS_ICON }[k]) || LL.FW_ICON || '';
    nav.querySelectorAll('.tab__icon').forEach((el, i) => { el.innerHTML = navIcon(tabs[i][0]); });
    // live state in the header: "Live · 14:32:05", "Updated 12 s ago", "Disconnected", "Paused"; the dot is the overall live grade
    const gradePill = h('button', { class: 'grade-pill live-pill', type: 'button', title: t('live.overall'), onclick: () => (live.state === 'offline' ? LL.live.reconnect() : openSources()) },
      h('i', { class: 'grade-pill__dot' }), h('span', { class: 'grade-pill__text' }, t('livestate.connecting')));
    const reconnectBtn = h('button', { class: 'btn btn--ghost btn--sm live-reconnect', type: 'button', hidden: true, onclick: () => LL.live.reconnect() }, t('livestate.reconnect'));
    const updateBtn = h('button', { class: 'update-pill', type: 'button', hidden: true, onclick: () => openUpdate() }, t('update.available_short'));
    const themeBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('theme.toggle'), title: t('theme.toggle'), onclick: () => {
      const order = ['auto', 'light', 'dark']; const next = order[(order.indexOf(LL.theme()) + 1) % 3]; LL.setTheme(next); LL.toast(t('theme.' + next));
    } });
    themeBtn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/></svg>';
    const header = h('header', { class: 'topbar' + (tabs.length > 8 ? ' topbar--many' : '') },   // ten tabs with a hub: narrower steps
      h('a', { class: 'topbar__brand', href: '#/map' }, LL.logo(26), h('span', { class: 'brand-name' }, 'Lanloom'),
        LL.state && LL.state.demo ? h('span', { class: 'chip chip--demo' }, t('app.demo')) : null),
      nav, h('div', { class: 'topbar__right' }, LL.paletteButton ? LL.paletteButton() : null, LL.aiAskButton ? LL.aiAskButton() : null, updateBtn, reconnectBtn, gradePill,
        LL.railButtons ? LL.railButtons() : null, themeBtn));
    themeBtn.classList.add('theme-btn');
    const main = h('main', { class: 'main main--' + active }, content);
    const notes = LL.noticeBar ? LL.noticeBar() : null;              // security notices (new device, lock-out ...)
    const demoBar = LL.state && LL.state.demo_view ? h('div', { class: 'demo-bar', role: 'status' },
      h('span', { class: 'demo-bar__text' }, h('strong', {}, t('setup.demo.bar_title')), ' ', t('setup.demo.bar_text')),
      h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => LL.demoView(false) }, t('setup.demo.leave'))) : null;
    app.replaceChildren(h('div', { class: 'frame' }, header, demoBar, notes, LL.shellBody ? LL.shellBody(main, active) : main));
    const pillText = gradePill.querySelector('.grade-pill__text');
    const narrow = window.matchMedia('(max-width: 1400px)');          // the short form: only the time
    function showState() {
      const st = live.state, age = live.lastAt ? (Date.now() - live.lastAt) / 1000 : null;
      let text, key = st;
      if (st === 'live' && age != null && age > 5) { key = 'stale'; text = t(narrow.matches ? 'time.ago' : 'livestate.stale', { age: LL.fmtAge(age) }); }
      else if (st === 'live') text = t(narrow.matches ? 'livestate.live_short' : 'livestate.live', { time: new Date(live.lastAt).toLocaleTimeString(LL.locale(), { hour12: false }) });
      else text = t('livestate.' + st);
      gradePill.dataset.live = key;
      if (pillText.textContent !== text) pillText.textContent = text;
      reconnectBtn.hidden = st !== 'offline';
      const g = (live.last && live.last.overall && live.last.overall.grade) || 'unknown';
      gradePill.title = t('livestate.title', { grade: t('board.grade.' + g) });
    }
    const unsub = liveSubscribe(f => {
      gradePill.dataset.grade = live.state === 'live' ? ((f.overall && f.overall.grade) || 'unknown') : 'unknown';
      showState();
    });
    const unStatus = LL.live.onStatus(() => { if (live.state !== 'live') gradePill.dataset.grade = 'unknown'; showState(); });
    const clock = setInterval(showState, 1000);
    showState();
    if (LL.can('admin')) checkUpdate(updateBtn);
    if (LL.setReportsUnread) LL.setReportsUnread((LL.state && LL.state.reports_unread) || 0);
    if (LL.state && LL.state.authenticated && LL.can('operate') && !LL.state.report_tz_auto && !frame.tzSent) {
      frame.tzSent = true;                 // the browser's time zone is the automatic report zone (when the container has none)
      try { api('/api/reports/tz', { method: 'POST', body: { tz: Intl.DateTimeFormat().resolvedOptions().timeZone || '' } }).catch(() => {}); } catch (e) { /* ignore */ }
    }
    return { main, destroy: () => { unsub(); unStatus(); clearInterval(clock); } };
  }
  const NAV_ICONS = {
    overview: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 13a8 8 0 0 1 16 0"/><path d="M12 13l3.5-4"/><path d="M4 17h16"/><circle cx="12" cy="13" r="1.2"/></svg>',
    map: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M10 6.5h4.5v7.5M6.5 10v7.5H14"/></svg>',
    network: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="5" rx="1"/><rect x="2.5" y="16.5" width="6" height="5" rx="1"/><rect x="15.5" y="16.5" width="6" height="5" rx="1"/><path d="M12 7.5v4.5M5.5 16.5V12h13v4.5"/></svg>',
    devices: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1.2"/><circle cx="3.5" cy="12" r="1.2"/><circle cx="3.5" cy="18" r="1.2"/></svg>',
    sensors: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h3l2.5-6 4 12 2.5-6H21"/></svg>',
    automations: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="6" r="2"/><circle cx="5" cy="18" r="2"/><rect x="15" y="9" width="6" height="6" rx="1.5"/><path d="M7 6h3a2 2 0 0 1 2 2v2.5a1.5 1.5 0 0 0 1.5 1.5H15M7 18h3a2 2 0 0 0 2-2v-2.5"/></svg>',
    settings: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2.2"/><circle cx="10" cy="17" r="2.2"/></svg>'
  };

  // ------------------------------------------------------------------ translation of map views
  function localise(view) {
    const v = JSON.parse(JSON.stringify(view));
    v.zones.forEach(z => { if (z.title_key) z.title = t(z.title_key, z.vars || {}); });
    v.components.forEach(c => {
      if (c.title_key && !c.title) c.title = t(c.title_key) + (c.title_suffix ? ' ' + c.title_suffix : '');
      if (c.subtitle_key && !c.subtitle) c.subtitle = t(c.subtitle_key, c.vars || {});
    });
    return v;
  }

  // ------------------------------------------------------------------ map views
  function mapView(kind) {
    return async function (app, rest) {
      const route = kind === 'network' ? 'network' : 'map';
      const fr = frame(app, route);
      const host = h('div', { class: 'board-host' });
      const filterBar = h('div', { class: 'map-filter', role: 'toolbar', 'aria-label': t('mapfilter.label') });
      const panel = h('aside', { class: 'detail', hidden: true, 'aria-label': t('detail.label') });
      fr.main.append(h('div', { class: 'map-col' }, filterBar, host), panel);
      let data = await api('/api/map?view=' + (kind === 'network' ? 'network' : 'overview'));
      if (!data.components.length) {
        filterBar.remove();
        host.replaceChildren(emptyState());
        return fr;
      }
      let selected = null;
      // the address follows the open card (#/map/<id>), the back key closes it again
      const here = id => '#/' + route + (id ? '/' + encodeURIComponent(id) : '') + LL.queryString();
      const onSelectId = (id, fromAddress) => {
        selected = id; board.highlight(id);
        if (!fromAddress) LL.deepLink(here(id));
        openDetail(panel, id, () => { selected = null; board.highlight(null); LL.deepLink(here(null)); });
      };
      // area filter (#/map?zone=<id>) and "problems only" (#/map?only=problems); the internet edge always stays for orientation
      const filtered = view => {
        const zone = LL.query.get('zone'), only = LL.query.get('only');
        if (!zone && !only) return view;
        let comps = view.components;
        if (zone) comps = comps.filter(c => c.zone === zone || c.zone === 'edge');
        if (only === 'problems') comps = comps.filter(c => c.zone === 'edge' || c.status === 'down' || c.grade === 'interrupted');
        const ids = new Set(comps.map(c => c.id)), zs = new Set(comps.map(c => c.zone));
        return Object.assign({}, view, { components: comps, zones: view.zones.filter(z => zs.has(z.id)), traces: view.traces.filter(tr => ids.has(tr.a) && ids.has(tr.b)) });
      };
      const show = d => localise(filtered(d));
      // "Who talks to whom": dashed lines in the air between devices that talk to each other (gateway flows, last 24 hours)
      let talkOn = LL.query.get('talk') === '1', talkInfo = null, talkVer = null, talkBusy = false;
      async function loadTalk() {
        if (!talkOn) { talkInfo = null; board.setTalk([]); return; }
        if (talkBusy) return;
        talkBusy = true;
        try { talkInfo = await api('/api/paths/lan?window=24h'); talkVer = talkInfo.version; board.setTalk(talkInfo.pairs); if (selected) board.highlight(selected); drawFilter(); }
        catch (e) { /* keep the lines that are there */ }
        talkBusy = false;
      }
      function toggleTalk() {
        talkOn = !talkOn;
        if (talkOn) LL.query.set('talk', '1'); else LL.query.delete('talk');
        LL.deepLink(here(selected));
        loadTalk(); drawFilter();
      }
      function drawFilter() {
        const zones = localise(data).zones.filter(z => z.id !== 'edge');
        const zone = LL.query.get('zone'), only = LL.query.get('only');
        const set = (key, value) => {
          if (value) LL.query.set(key, value); else LL.query.delete(key);
          LL.deepLink(here(selected));
          board.update(show(data)); if (selected) board.highlight(selected);
          drawFilter();
        };
        const chip = (label, on, fn, extra) => h('button', { class: 'mf-chip' + (on ? ' is-on' : '') + (extra || ''), type: 'button', 'aria-pressed': on ? 'true' : 'false', onclick: fn }, label);
        filterBar.replaceChildren(...[
          zones.length > 1 ? chip(t('mapfilter.all'), !zone, () => set('zone', null)) : null,
          ...(zones.length > 1 ? zones.map(z => chip(z.title || '—', zone === z.id, () => set('zone', zone === z.id ? null : z.id))) : []),
          h('span', { class: 'mf-sep', 'aria-hidden': 'true' }),
          chip(t('mapfilter.problems'), only === 'problems', () => set('only', only === 'problems' ? null : 'problems'), ' mf-chip--problems'),
          chip(t('talk.chip'), talkOn, toggleTalk, ' mf-chip--talk')].filter(Boolean));
        if (talkOn && talkInfo) filterBar.append(h('span', { class: 'mf-talk muted' }, talkInfo.pairs.length
          ? t('talk.legend', { n: talkInfo.pairs.length }) : t('talk.none')));
        const n = filtered(data).components.filter(c => c.zone !== 'edge').length;
        if ((zone || only) && !n) filterBar.append(h('span', { class: 'mf-empty muted' }, t(only ? 'mapfilter.none_problems' : 'mapfilter.none')));
      }
      const board = LL.Board.mount(host, {
        view: show(data), theme: LL.effectiveTheme(), t: LL.t, locale: LL.locale(), onSelect: id => onSelectId(id)
      });
      drawFilter();
      if (talkOn) loadTalk();
      const onTheme = ev => board.setTheme(ev.detail);
      document.addEventListener('ll-theme', onTheme);
      // deep link #/map/<id> or #/network/<id> (command palette, answers of "Ask your network"): open that component's card;
      // a component that sits on the other board sends the page there
      const want = rest && rest[0] ? decodeURIComponent(rest[0]) : '';
      if (want) {
        if (data.components.some(c => c.id === want)) onSelectId(want, true);
        else {
          const other = kind === 'network' ? 'map' : 'network';
          api('/api/map?view=' + (kind === 'network' ? 'overview' : 'network')).then(d => { if (d.components.some(c => c.id === want)) LL.go('#/' + other + '/' + encodeURIComponent(want)); }).catch(() => {});
        }
      }
      // change channel instead of polling: the live frame carries the structure version and a map version that rises
      // when statuses, flow traces or source states change; the map is fetched only then (and once after a hidden tab)
      let structure = data.structure, mapver = null, stale = false;
      let refreshing = false;
      async function refresh() {
        if (refreshing) { stale = true; return; }
        refreshing = true;
        try { data = await api('/api/map?view=' + (kind === 'network' ? 'network' : 'overview')); structure = data.structure; board.update(show(data)); if (selected) board.highlight(selected); drawFilter(); }
        catch (e) { /* keep the old board */ }
        refreshing = false;
        if (stale) { stale = false; refresh(); }
      }
      const unsub = liveSubscribe(f => {
        if (document.hidden) { if (f.structure !== structure || (mapver != null && f.mapver !== mapver)) stale = true; mapver = f.mapver; return; }
        board.live(f);
        if (talkOn && f.pathsver != null && f.pathsver !== talkVer) loadTalk();
        const changed = f.structure !== structure || (mapver != null && f.mapver != null && f.mapver !== mapver);
        mapver = f.mapver;
        if (changed || stale) { stale = false; refresh(); }
      });
      const onVis = () => { if (!document.hidden && stale) { stale = false; refresh(); } };
      document.addEventListener('visibilitychange', onVis);
      return { destroy: () => { unsub(); document.removeEventListener('visibilitychange', onVis); document.removeEventListener('ll-theme', onTheme); board.destroy(); fr.destroy(); } };
    };
  }

  function emptyState() {
    return h('div', { class: 'empty-state' }, h('div', { class: 'empty-state__art' }, LL.logo(56)), h('h2', {}, t('map.empty.title')),
      h('p', { class: 'muted' }, t(LL.can('admin') ? 'map.empty.text' : 'map.empty.text_viewer')), LL.can('admin') ? h('a', { class: 'btn btn--primary', href: '#/settings' }, t('map.empty.action')) : null);
  }

  // ------------------------------------------------------------------ detail panel
  async function openDetail(panel, id, onClose) {
    panel.hidden = false;
    panel.classList.add('is-open');
    panel.replaceChildren(h('div', { class: 'detail__loading' }, h('div', { class: 'skeleton' })));
    let d;
    try { d = await api('/api/component/' + encodeURIComponent(id)); } catch (e) { panel.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
    const c = localise({ zones: [], components: [d.component], traces: [] }).components[0];
    const close = h('button', { class: 'icon-btn detail__close', type: 'button', 'aria-label': t('detail.close'), onclick: () => { panel.hidden = true; panel.classList.remove('is-open'); unsub(); if (onClose) onClose(); } }, '✕');
    const gradeEl = h('span', { class: 'grade-chip', dataset: { grade: c.grade } }, h('i'), t('board.grade.' + (c.grade || 'unknown')));
    const statusEl = h('span', { class: 'chip chip--' + ({ up: 'ok', down: 'danger' }[c.status] || 'muted') }, t('board.status.' + (c.status || 'unknown')));
    const sections = [];
    // live chart
    const chart = h('canvas', { class: 'spark', width: '360', height: '96', 'aria-label': t('detail.traffic') });
    const chartLegend = h('div', { class: 'spark-legend muted' });
    if ((d.series && d.series.length) || (LL.trafficPlot && (d.traces || []).length)) {
      const liveBox = h('div', { class: 'spark-wrap' }, chart, chartLegend);
      const trafficSec = section(t('detail.traffic'), LL.trafficPlot && (d.traces || []).length ? LL.trafficPlot(id, d.series && d.series.length ? liveBox : null, c.title) : liveBox);
      trafficSec.dataset.sec = 'traffic';
      sections.push(trafficSec);
    }
    if (d.device && LL.availabilitySection) sections.push(LL.availabilitySection(id));
    if (d.hypervisor) sections.push(LL.hvHostSection(d.hypervisor));
    if (d.guest) sections.push(LL.hvGuestSection(d.guest));
    // the hub side of a merged device comes first: one component, network side and hub side
    if (d.hub_side || d.hub_suggest) sections.push(LL.hubSideSection(d.hub_side, d.hub_suggest, id));
    const devSec = d.device ? deviceSection(d.device, d) : null;
    const foot = devSec && devSec.querySelector(':scope > .detail__foot');   // "Hide from the map" and "Forget device" stay the last buttons of the card
    if (foot) foot.remove();
    if (d.physical && (d.physical.link || d.physical.children || d.physical.choices || d.physical.unknown)) {
      const phys = h('div', {}, physicalSection(d.physical, id));
      const edit = h('div', {}, d.physical.choices && LL.can('operate') ? manualEditor(d.physical, id, refreshPhysical) : null);
      sections.push(phys);
      if (devSec) sections.push(devSec);
      sections.push(edit);
      async function refreshPhysical() {
        try {
          const nd = await api('/api/component/' + encodeURIComponent(id)), np = nd.physical || {};
          phys.replaceChildren(physicalSection(np, id));
          edit.replaceChildren(np.choices && LL.can('operate') ? manualEditor(np, id, refreshPhysical) : '');
        } catch (e) { /* keep */ }
      }
    } else if (devSec) sections.push(devSec);
    if (d.snmp) sections.push(snmpSection(d.snmp));
    if (d.broker) sections.push(brokerSection(d.broker));
    if (d.topic) sections.push(section(t('detail.topic'), facts([[t('detail.rate'), d.topic.msgs_s + ' ' + t('unit.msg_s')], [t(LL.rateUnit === 'bits' ? 'detail.bits' : 'detail.bytes'), LL.fmtRate(d.topic.bytes_s)],
      [t('detail.cadence'), d.topic.cadence_s ? LL.fmtAge(d.topic.cadence_s) : '—'], [t('detail.last'), LL.fmtAge(d.topic.last_s)]])));
    if (d.hub_device) sections.push(LL.hubDeviceSection(d.hub_device));
    if (d.radio_net) sections.push(LL.radioNetSection(d.radio_net));
    if (d.hub_bridge) sections.push(LL.hubBridgeSection(d.hub_bridge));
    if (d.entities) sections.push(entitiesSection(d.entities));
    if (d.ifaces && Object.keys(d.ifaces).length) sections.push(section(t('detail.ifaces'), facts(Object.entries(d.ifaces).map(([n, v]) => [n, '↑ ' + LL.fmtRate(v[0]) + '  ↓ ' + LL.fmtRate(v[1])]))));
    if ((d.device || id === 'internet') && LL.talkSection) sections.push(LL.talkSection(id));
    const links = LL.connectionsSection ? LL.connectionsSection(d, id) : section(t('detail.links'), h('ul', { class: 'link-list' }, d.traces.length ? d.traces.map(tr => h('li', {},
      h('span', { class: 'trace-sw trace-sw--' + tr.kind }), t('board.trace.' + tr.kind), h('span', { class: 'muted' }, ' · ' + (tr.measured ? t('detail.measured') : t('detail.unmeasured'))))) : [h('li', { class: 'muted' }, t('detail.no_links'))]));
    sections.push(links);
    if (d.device && LL.maintenanceSection) sections.push(LL.maintenanceSection(id));
    if (foot) sections.push(foot);
    panel.replaceChildren(h('div', { class: 'detail__head' }, LL.kindIcon(c.kind), h('div', { class: 'detail__titles' }, h('h2', {}, c.title || '—'), h('div', { class: 'muted mono' }, c.subtitle || t('kind.' + c.kind))), close),
      h('div', { class: 'detail__chips' }, h('span', { class: 'chip' }, t('kind.' + c.kind)), statusEl, gradeEl,
        d.device && LL.aiExplainButton ? LL.aiExplainButton('device', { id: d.device.id }, { title: c.title }) : null),
      h('div', { class: 'detail__body' }, sections));
    if (LL.foldSections) LL.foldSections(panel.querySelector('.detail__body'));     // sections fold; the choice survives live updates and other cards
    function drawChart() {
      if (!d.series || !d.series.length) return;
      const pts = {};
      for (const s of d.series) for (const p of s.points) { const k = Math.round(p[0]); const e = pts[k] || (pts[k] = [0, 0]); e[0] += p[1]; e[1] += p[2]; }
      const keys = Object.keys(pts).map(Number).sort((a, b) => a - b).slice(-300);
      const dpr = window.devicePixelRatio || 1; const w = chart.clientWidth || 360, hgt = 96;
      chart.width = w * dpr; chart.height = hgt * dpr;
      const g = chart.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, w, hgt);
      const max = Math.max(1, ...keys.map(k => Math.max(pts[k][0], pts[k][1])));
      const css = getComputedStyle(document.documentElement);
      const lines = [[0, css.getPropertyValue('--ll-pcb-trace-flow').trim() || '#7446D4'], [1, css.getPropertyValue('--ll-pcb-trace-lan').trim() || '#0C8574']];
      g.strokeStyle = css.getPropertyValue('--ll-border').trim(); g.lineWidth = 1; g.beginPath(); g.moveTo(0, hgt - 0.5); g.lineTo(w, hgt - 0.5); g.stroke();
      for (const [idx, color] of lines) {
        g.strokeStyle = color; g.lineWidth = 1.6; g.beginPath();
        keys.forEach((k, i) => { const x = keys.length > 1 ? i / (keys.length - 1) * w : w; const y = hgt - 4 - (pts[k][idx] / max) * (hgt - 10); i ? g.lineTo(x, y) : g.moveTo(x, y); });
        g.stroke();
      }
      const lastK = keys[keys.length - 1];
      chartLegend.replaceChildren(h('span', { class: 'lg lg--out' }, '↑ ' + LL.fmtRate(pts[lastK][0])), h('span', { class: 'lg lg--in' }, '↓ ' + LL.fmtRate(pts[lastK][1])),
        h('span', {}, t('detail.peak', { v: LL.fmtRate(max) })), h('span', {}, t('detail.window', { n: Math.round(keys.length / 60) || 1 })));
    }
    requestAnimationFrame(drawChart);
    const unsub = liveSubscribe(f => {
      const g = (f.grades || {})[id];
      if (g) { gradeEl.dataset.grade = g; gradeEl.lastChild.textContent = t('board.grade.' + g); }
      if (links.update) links.update(f);
    });
    const refresher = setInterval(async () => {
      if (panel.hidden || !panel.isConnected) { clearInterval(refresher); unsub(); return; }   // closed, or the view was drawn anew
      if (document.hidden) return;                 // no requests for a tab nobody looks at
      try { const nd = await api('/api/component/' + encodeURIComponent(id)); d.series = nd.series; drawChart(); } catch (e) { /* ignore */ }
    }, 5000);
  }

  function section(title, body) { return h('section', { class: 'detail__sec' }, h('h3', {}, title), body); }
  function facts(rows) { return h('dl', { class: 'facts' }, rows.filter(r => r && r[1] != null && r[1] !== '').map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]).flat()); }

  function deviceSection(dev, d) {
    const may = LL.can('operate');
    const name = !may ? h('span', {}, dev.title || t('kind.' + dev.kind)) : h('input', { class: 'input-plain', value: dev.title, placeholder: t('kind.' + dev.kind), maxlength: '48', 'aria-label': t('device.name'),
      onchange: async () => { await api('/api/devices/' + encodeURIComponent(dev.id), { method: 'PATCH', body: { title: name.value } }); LL.toast(t('device.saved'), 'ok'); } });
    const kindSel = dev.is_self ? h('span', {}, t('kind.self')) : !may ? h('span', {}, t('kind.' + dev.kind)) : LL.kindSelect(dev.kind, async v => { await api('/api/devices/' + encodeURIComponent(dev.id), { method: 'PATCH', body: { kind: v } }); LL.toast(t('device.saved'), 'ok'); });
    const reasonText = r => {
      const kind = r.split(':')[0]; let v = r.split(':').slice(1).join(':');
      if (kind === 'word') v = t('kind.' + v);
      if (kind === 'role') v = t('role.' + v);
      if (kind === 'vendor') v = t('kind.' + v);
      if (kind === 'snmp' || kind === 'netbios') return t('reason.' + kind + '.' + v);
      return t('reason.' + kind, { v });
    };
    const reasons = (dev.reasons || []).map(reasonText).join(' · ');
    const srcChip = s => s ? h('span', { class: 'chip chip--muted ident-src' }, t('ident.src.' + s)) : null;
    const withSrc = (v, s) => v ? h('span', { class: 'ident-val' }, h('span', {}, v), srcChip(s)) : null;
    const vendorCell = dev.vendor ? withSrc(dev.vendor, dev.vendor_source)
      : dev.origin === 'random' ? h('span', { class: 'muted' }, t('ident.origin.random'))
      : dev.origin === 'unregistered' ? h('span', { class: 'muted' }, t('ident.origin.unregistered')) : null;
    const rows = [[t('device.name'), name], [t('device.kind'), kindSel], [t('device.certainty'), LL.certaintyChip(dev.certainty)], [t('device.why'), reasons || '—'],
      [t('ident.vendor'), vendorCell], [t('ident.model'), withSrc(dev.model, dev.model_source)],
      [t('ident.firmware'), withSrc(dev.firmware, dev.firmware_source)],
      [t('device.ip'), dev.ip ? h('span', { class: 'mono' }, dev.ip) : h('span', { class: 'muted' }, t('device.no_ip'))], [t('device.mac'), dev.mac ? h('span', { class: 'mono' }, dev.mac) : null],
      [t('ident.ipv6'), dev.ipv6 && dev.ipv6.length ? h('span', { class: 'mono ident-v6' }, dev.ipv6.join('\n')) : null],
      [t('device.hostnames'), dev.hostnames && dev.hostnames.length ? dev.hostnames.join(', ') : null],
      [t('ident.netbios'), dev.netbios && dev.netbios.computer ? dev.netbios.computer + (dev.netbios.workgroup ? ' · ' + dev.netbios.workgroup : '') : null],
      [t('ident.origin'), t('ident.origin_short.' + (dev.origin || 'no_mac'))],
      [t('device.found_by'), (dev.sources || []).map(s => t('source.kind.' + s)).join(', ')],
      [t('device.first_seen'), LL.fmtDate(dev.first_seen)], [t('device.last_seen'), LL.fmtDate(dev.last_seen)],
      [t('device.presence'), d.presence_age_s != null ? t('device.presence_ago', { age: LL.fmtAge(d.presence_age_s) }) : '—']];
    const ports = dev.ports && dev.ports.length ? h('table', { class: 'mini-table' }, h('thead', {}, h('tr', {}, h('th', {}, t('device.port')), h('th', {}, t('device.service')), h('th', {}, t('device.banner')))),
      h('tbody', {}, dev.ports.map(p => h('tr', {}, h('td', { class: 'mono' }, String(p.port)), h('td', {}, p.service || ''), h('td', { class: 'truncate' }, (p.banner && (p.banner.title || p.banner.server)) || ''))))) : null;
    const ann = [];
    (dev.mdns || []).forEach(m => ann.push(h('li', {}, h('span', { class: 'mono' }, m.type), ' — ', m.name || '')));
    (dev.ssdp || []).forEach(s => ann.push(h('li', {}, h('span', { class: 'mono' }, (s.device_type || s.st || '').replace(/^urn:/, '')), ' — ', [s.friendly_name, s.model].filter(Boolean).join(' · '))));
    const hide = !may ? null : h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => { await api('/api/devices/' + encodeURIComponent(dev.id), { method: 'PATCH', body: { hidden: !dev.hidden } }); LL.toast(t('device.saved'), 'ok'); LL.route(); } }, dev.hidden ? t('device.unhide') : t('device.hide'));
    // a leftover (hidden, or offline) can be removed for good; never this host or the gateway. A device that still
    // answers comes back with the next scan, as a new entry.
    const forget = may && (dev.hidden || dev.status === 'down') && !dev.is_self && !dev.is_gateway ? h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', onclick: async ev => {
      if (!await askForget(dev.title || dev.ip || dev.mac || '')) return;
      try {
        await api('/api/devices/' + encodeURIComponent(dev.id), { method: 'DELETE' });
        const card = ev.target.closest('.detail');
        if (card) { card.hidden = true; card.classList.remove('is-open'); }               // its refresher stops with it
        LL.toast(t('device.forgotten'), 'ok');
        LL.deepLink(location.hash.replace('/' + encodeURIComponent(dev.id), ''), true);   // no link to a device that is gone
        LL.route();
      } catch (e) { LL.toast(LL.errText(e), 'error'); }
    } }, t('device.forget')) : null;
    return h('div', {}, section(t('detail.device'), facts(rows)), LL.aiHintSection ? LL.aiHintSection(dev) : null, identitySection(dev, may),
      LL.integrateSection ? LL.integrateSection(dev) : null, LL.protectSection ? LL.protectSection(dev, may) : null, LL.inventorySection ? LL.inventorySection(dev) : null, ports ? section(t('device.ports'), ports) : null,
      ann.length ? section(t('device.announcements'), h('ul', { class: 'plain-list' }, ann)) : null, hide || forget ? h('div', { class: 'detail__foot' }, forget, hide) : null);
  }

  // "Forget device" asks in the app's own dialog (no browser pop-up)
  function askForget(name) {
    return new Promise(resolve => {
      let answered = false;
      const yes = h('button', { class: 'btn btn--danger', type: 'button', onclick: () => { answered = true; d.close(); resolve(true); } }, t('device.forget'));
      const no = h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => d.close() }, t('access.cancel'));
      const d = dialog(t('device.forget_title', { name }), h('p', {}, t('device.forget_confirm')), h('div', { class: 'btn-row' }, no, yes));
      const obs = new MutationObserver(() => { if (!d.el.isConnected) { obs.disconnect(); if (!answered) resolve(false); } });
      obs.observe(document.body, { childList: true });
      setTimeout(() => no.focus(), 30);
    });
  }

  // ------------------------------------------------------------------ one device, several interfaces
  function identitySection(dev, may) {
    const why = rs => (rs || []).map(r => t('ident.why.' + r)).join(' · ');
    const pct = c => Math.round((c || 0) * 100) + ' %';
    const act = async (action, other) => {
      try {
        await api('/api/devices/' + encodeURIComponent(dev.id) + '/identity', { method: 'POST', body: { action, other } });
        LL.toast(t('device.saved'), 'ok'); LL.route();
      } catch (e) { LL.toast(t(e.message || 'device.identity.invalid'), 'error'); }
    };
    const btn = (label, action, other) => may ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => act(action, other) }, label) : null;
    const items = [];
    if (dev.merged_into) {
      const m = dev.merged_into;
      items.push(h('li', { class: 'ident-row' }, h('div', {}, h('strong', {}, t('ident.part_of', { name: m.title || m.ip })),
        h('div', { class: 'muted' }, m.manual ? t('ident.manual') : t('ident.because', { why: why(m.reasons), conf: pct(m.confidence) }))),
        btn(t('ident.split'), 'split', m.id)));
    }
    (dev.aliases || []).forEach(a => items.push(h('li', { class: 'ident-row' },
      h('div', {}, h('span', { class: 'mono' }, [a.ip, a.mac].filter(Boolean).join(' · ')),
        h('div', { class: 'muted' }, a.manual ? t('ident.manual') : t('ident.because', { why: why(a.reasons), conf: pct(a.confidence) }))),
      btn(t('ident.split'), 'split', a.id))));
    const sugg = (dev.suggestions || []).map(sg => h('li', { class: 'ident-row' },
      h('div', {}, h('span', {}, t('ident.maybe', { name: sg.title || sg.ip })), h('div', { class: 'muted' }, t('ident.because', { why: why(sg.reasons), conf: pct(sg.confidence) }))),
      h('span', { class: 'ident-acts' }, btn(t('ident.join'), 'join', sg.id), btn(t('ident.keep_apart'), 'split', sg.id))));
    if (!items.length && !sugg.length) return null;
    return section(t('ident.title'), h('div', { class: 'stack-sm' },
      items.length ? h('ul', { class: 'plain-list ident-list' }, items) : null,
      sugg.length ? h('div', {}, h('p', { class: 'muted ident-lead' }, t('ident.suggest_lead')), h('ul', { class: 'plain-list ident-list' }, sugg)) : null));
  }

  // ------------------------------------------------------------------ physical connection (manual > controller > LLDP > address table)
  const refName = r => (r ? (r.title || t('kind.' + (r.kind || 'unknown')) + (r.ip ? ' ' + r.ip : '')) : '—');
  function portChip(p) {
    const P = LL.Portmarks;
    let f = null;
    if (p.medium === 'radio') f = { link: 'radio', radio: { band: p.band || '', ssid: p.ssid || '' } };
    else if (p.medium !== 'virtual' && p.port) f = { link: 'wired', port: p.port, ownPort: p.own_port || '' };
    const html = f && P && P.chipHtml ? P.chipHtml(f, LL.t) : '';
    if (!html) return null;
    const el = h('span', {}); el.innerHTML = html; return el.firstChild;
  }
  const mediumText = m => t('topo.medium.' + (m || 'unknown'));
  const sourceText = s => t('topo.source.' + s);
  function physicalSection(p) {
    const out = [];
    const l = p.link;
    if (l) {
      const via = h('span', { class: 'phys-via' }, h('span', {}, refName(l.parent_ref)), portChip(l));
      const src = sourceText(l.source) + (l.age_s != null ? ' · ' + t('device.presence_ago', { age: LL.fmtAge(l.age_s) }) : '');
      const rows = [[t('topo.via'), via], [t('topo.medium'), mediumText(l.medium)], [t('topo.source'), src]];
      if (l.own_port) rows.push([t('topo.own_port'), h('span', { class: 'mono' }, l.own_port)]);
      if (l.reason) rows.push([t('topo.why'), t(l.reason, { via: refName(l.vars.via_ref), port: l.vars.port || '', n: l.vars.n || 0 })]);
      if (l.also && l.also.length) rows.push([t('topo.also'), l.also.map(sourceText).join(', ')]);
      const box = [facts(rows)];
      const disagree = (l.conflicts || []).concat(l.loop || []);
      if (disagree.length) {
        box.push(h('div', { class: 'callout callout--warn phys-conflict' },
          disagree.map(c => h('p', {}, t((l.loop || []).includes(c) ? 'topo.loop' : 'topo.conflict', {
            source: sourceText(c.source), where: refName(c.parent_ref) + (c.port ? ' · ' + t('portmarks.port') + ' ' + c.port : ''), winner: sourceText(l.source) }))),
          h('p', { class: 'muted' }, t('topo.rule'))));
      }
      out.push(section(t('topo.title'), h('div', { class: 'stack-sm' }, box)));
    }
    if (p.children && p.children.length) {
      const shown = p.children.slice(0, 80);
      const rows = shown.map(c => h('tr', { title: sourceText(c.source) }, h('td', { class: 'phys-port' }, portChip(c) || h('span', { class: 'muted' }, mediumText(c.medium))),
        h('td', { class: 'truncate' }, refName(c))));
      const extra = (p.unknown || []).map(u => h('p', { class: 'muted' }, t('topo.unknown', { n: u.count, port: u.port })));
      out.push(section(t('topo.children', { n: p.children.length }), h('div', {}, h('table', { class: 'mini-table phys-table' },
        h('thead', {}, h('tr', {}, h('th', {}, t('device.port')), h('th', {}, t('device.name')))), h('tbody', {}, rows)),
        p.children.length > shown.length ? h('p', { class: 'muted' }, t('topo.more', { n: p.children.length - shown.length })) : null, extra)));
    } else if (p.unknown && p.unknown.length) {
      out.push(section(t('topo.children', { n: 0 }), h('div', {}, p.unknown.map(u => h('p', { class: 'muted' }, t('topo.unknown', { n: u.count, port: u.port }))))));
    }
    return h('div', {}, out);
  }
  function manualEditor(p, id, refresh) {
    const m = p.manual || null;
    const parent = h('select', { class: 'select select--sm' }, [h('option', { value: '' }, t('topo.manual.none'))].concat(
      p.choices.map(c => h('option', { value: c.id }, refName(c) + (c.title && c.ip ? ' · ' + c.ip : '')))));
    parent.value = m ? m.parent : '';
    const port = h('input', { class: 'input-sm', value: m && m.port ? m.port : '', maxlength: '24', placeholder: t('topo.manual.port_ph'), spellcheck: 'false', autocomplete: 'off' });
    const medium = h('select', { class: 'select select--sm' }, ['wired', 'radio', 'virtual'].map(k => h('option', { value: k }, mediumText(k))));
    medium.value = m ? m.medium : 'wired';
    const msg = h('div', { class: 'msg' });
    const sync = () => { port.disabled = medium.value === 'virtual' || !parent.value; medium.disabled = !parent.value; };
    parent.addEventListener('change', sync); medium.addEventListener('change', sync); sync();
    const send = async link => {
      try { await api('/api/devices/' + encodeURIComponent(id), { method: 'PATCH', body: { link } }); LL.toast(t('device.saved'), 'ok'); refresh(); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    };
    const save = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => send(parent.value ? { parent: parent.value, port: port.value.trim(), medium: medium.value } : null) }, t('topo.manual.save'));
    const clear = m ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => send(null) }, t('topo.manual.clear')) : null;
    return section(t('topo.manual.title'), h('div', { class: 'stack-sm phys-edit' },
      h('p', { class: 'muted' }, t('topo.manual.lead')),
      LL.field(t('topo.manual.parent'), parent), h('div', { class: 'phys-edit__row' }, LL.field(t('device.port'), port), LL.field(t('topo.medium'), medium)),
      msg, h('div', { class: 'btn-row phys-edit__actions' }, clear, save)));
  }

  function snmpSection(s) {
    const rows = (s.ifaces || []).map(i => h('tr', {}, h('td', { class: 'mono' }, i.name || String(i.index)), h('td', {}, i.oper === 'up' || i.oper === 1 ? t('board.status.up') : t('board.status.down')),
      h('td', { class: 'num' }, i.in != null ? LL.fmtBytes(i.in) : '—'), h('td', { class: 'num' }, i.out != null ? LL.fmtBytes(i.out) : '—')));
    return section(t('detail.snmp'), h('div', {}, facts([[t('detail.sysname'), s.sys && s.sys.name], [t('detail.sysdescr'), s.sys && s.sys.descr], [t('detail.age'), LL.fmtAge(s.age_s)]]),
      h('table', { class: 'mini-table' }, h('thead', {}, h('tr', {}, h('th', {}, t('detail.iface')), h('th', {}, t('detail.state')), h('th', { class: 'num' }, t('detail.in_10s')), h('th', { class: 'num' }, t('detail.out_10s')))), h('tbody', {}, rows))));
  }

  function brokerSection(b) {
    return section(t('detail.topics'), h('table', { class: 'mini-table' }, h('thead', {}, h('tr', {}, h('th', {}, t('detail.topic')), h('th', { class: 'num' }, t('unit.msg_s')), h('th', { class: 'num' }, t('detail.bytes')))),
      h('tbody', {}, (b.topics || []).map(r => h('tr', {}, h('td', { class: 'mono truncate' }, r[0]), h('td', { class: 'num' }, String(r[1])), h('td', { class: 'num' }, LL.fmtRate(r[2])))))));
  }

  function entitiesSection(ents) {
    const filter = h('input', { type: 'search', class: 'input-sm', placeholder: t('detail.filter') });
    const body = h('tbody');
    function render() {
      const q = filter.value.toLowerCase();
      body.replaceChildren(...ents.filter(e => !q || (e.name + ' ' + e.id).toLowerCase().includes(q)).slice(0, 200).map(e => h('tr', {},
        h('td', { class: 'truncate' }, e.name), h('td', { class: 'num' }, e.state != null ? String(e.state) + (e.unit ? ' ' + e.unit : '') : '—'), h('td', { class: 'num muted' }, e.changed_s != null ? LL.fmtAge(e.changed_s) : ''))));
    }
    filter.addEventListener('input', render); render();
    return section(t('detail.entities', { n: ents.length }), h('div', {}, filter, h('table', { class: 'mini-table' }, h('thead', {}, h('tr', {}, h('th', {}, t('detail.entity')), h('th', { class: 'num' }, t('detail.state')), h('th', { class: 'num' }, t('detail.changed')))), body)));
  }

  // ------------------------------------------------------------------ sources dialog (live grade)
  function dialog(title, body, foot) {
    const close = () => { dlg.remove(); document.removeEventListener('keydown', esc); };
    const esc = ev => { if (ev.key === 'Escape') close(); };
    const dlg = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title, onclick: ev => { if (ev.target === dlg) close(); } },
      h('div', { class: 'modal__card card' }, h('header', { class: 'modal__head' }, h('h2', {}, title), h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('detail.close'), onclick: close }, '✕')),
        h('div', { class: 'modal__body' }, body), foot ? h('footer', { class: 'modal__foot' }, foot) : null));
    document.addEventListener('keydown', esc);
    document.body.appendChild(dlg);
    return { el: dlg, close };
  }

  function counterSection() {
    const box = h('div', { class: 'cc-box' });
    api('/api/selfcheck/counters').then(c => {
      const pairs = Object.values(c.latest || {});
      if (!pairs.length) { box.replaceChildren(h('h3', {}, t('counters.title')), h('p', { class: 'muted' }, t(c.netflow ? 'counters.none_yet' : 'counters.none'))); return; }
      const row = r => h('tr', {}, h('td', {}, t('counters.pair.' + r.pair)),
        h('td', { class: 'num' }, r.factor != null ? LL.fmtNum(r.factor * 100, 0) + '\u00a0%' : '—'),
        h('td', {}, h('span', { class: 'chip chip--' + (r.state === 'ok' ? 'ok' : 'warn') }, t('counters.state.' + r.state))),
        h('td', { class: 'num' }, LL.fmtDate((r.hour + 1) * 3600)));
      const bad = (c.findings || []).slice(-6).reverse();
      box.replaceChildren(h('h3', {}, t('counters.title')), h('p', { class: 'muted' }, t('counters.explain', { low: Math.round(c.low * 100), high: Math.round(c.high * 100) })),
        h('table', { class: 'mini-table cc-table' }, h('thead', {}, h('tr', {}, h('th', {}, t('counters.col.pair')), h('th', { class: 'num' }, t('counters.col.factor')), h('th', {}, t('counters.col.state')), h('th', { class: 'num' }, t('counters.col.hour')))),
          h('tbody', {}, pairs.map(row))),
        bad.length ? h('div', { class: 'callout callout--warn' }, h('strong', {}, t('counters.findings', { n: bad.length })),
          h('ul', { class: 'cc-list' }, bad.map(r => h('li', {}, t('counters.finding.' + r.state, { pair: t('counters.pair.' + r.pair), pct: Math.round((r.factor || 0) * 100), at: LL.fmtDate((r.hour + 1) * 3600) }))))) : null);
    }).catch(() => box.replaceChildren());
    return box;
  }
  function openSources() {
    const f = live.last || { overall: { sources: [] } };
    // live state, the rhythm of the feed and the clock offset between this browser and the server
    const off = live.offset;
    const offText = off == null ? t('livegrade.offset.unknown')
      : Math.abs(off) < 0.5 ? t('livegrade.offset.same')
      : t(off > 0 ? 'livegrade.offset.ahead' : 'livegrade.offset.behind', { s: LL.fmtNum(Math.abs(off), 1) });
    const stateRow = h('div', { class: 'lg-state' },
      h('span', { class: 'lg-state__pill', dataset: { live: live.state } }, h('i'), t('livestate.' + live.state, { time: live.lastAt ? new Date(live.lastAt).toLocaleTimeString(LL.locale(), { hour12: false }) : '—' })),
      live.state === 'live' ? null : h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => { LL.live.reconnect(); dlgRef.close(); } }, t('livestate.reconnect')));
    const clockRow = h('p', { class: 'lg-clock' + (off != null && Math.abs(off) >= 2 ? ' lg-clock--warn' : '') }, h('strong', {}, t('livegrade.offset') + ': '), offText,
      off != null && Math.abs(off) >= 2 ? h('span', { class: 'muted' }, ' ' + t('livegrade.offset.hint')) : null);
    const dlgRef = {};
    const rows = (f.overall.sources || []).map(s => h('tr', {}, h('td', {}, s.kind ? s.name : t(s.name)), h('td', {}, h('span', { class: 'grade-chip', dataset: { grade: s.grade } }, h('i'), t('board.grade.' + s.grade))),
      h('td', { class: 'num' }, s.cadence_s != null ? LL.fmtAge(s.cadence_s) : '—'), h('td', { class: 'num' }, s.age_s != null ? LL.fmtAge(s.age_s) : '—')));
    Object.assign(dlgRef, dialog(t('live.title'), h('div', {}, stateRow, h('p', { class: 'muted' }, t('live.explain')), clockRow,
      h('table', { class: 'mini-table' }, h('thead', {}, h('tr', {}, h('th', {}, t('live.source')), h('th', {}, t('live.grade')), h('th', { class: 'num' }, t('live.cadence')), h('th', { class: 'num' }, t('live.age')))),
        h('tbody', {}, rows.length ? rows : [h('tr', {}, h('td', { colspan: '4', class: 'muted' }, t('live.none')))])),
      h('ul', { class: 'grade-legend' }, GRADES.map(g => h('li', {}, h('span', { class: 'grade-chip', dataset: { grade: g } }, h('i'), t('board.grade.' + g)), ' ', t('live.def.' + g)))),
      counterSection())));
  }
  LL.openLiveGrade = openSources;

  // ------------------------------------------------------------------ update notice
  let updateState = null;
  async function checkUpdate(btn) {
    try { updateState = await api('/api/update'); } catch (e) { return; }
    btn.hidden = !updateState.available;
  }
  function openUpdate() {
    const u = updateState; if (!u) return;
    const msg = h('div', { class: 'msg' });
    const notes = h('pre', { class: 'release-notes' }, u.notes || '');
    const manual = h('div', { class: 'callout' }, h('p', {}, t('update.manual')), h('code', { class: 'cmd' }, 'docker compose pull && docker compose up -d'));
    const apply = u.sidecar ? h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      apply.disabled = true;
      try { const req = await api('/api/update/apply', { method: 'POST', body: { version: u.latest } }); followUpdate(msg, u.latest, req.nonce); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); apply.disabled = false; }
    } }, t('update.apply', { v: u.latest })) : null;
    dialog(t('update.title', { v: u.latest }), h('div', { class: 'stack' }, h('p', {}, t('update.lead', { cur: u.current, v: u.latest })), notes, u.sidecar ? h('p', { class: 'muted' }, t('update.sidecar_text')) : manual,
      u.sidecar_old ? h('div', { class: 'callout' }, h('p', {}, t('update.sidecar_old')), h('code', { class: 'cmd' }, 'docker compose pull && docker compose --profile updater up -d')) : null, msg),
      h('div', { class: 'btn-row' }, u.url ? h('a', { class: 'btn btn--ghost', href: u.url, target: '_blank', rel: 'noopener noreferrer' }, t('update.notes_link')) : null, apply));
  }
  function followUpdate(msg, version, nonce) {
    const timer = setInterval(async () => {
      let s; try { s = await api('/api/update', { allow401: true }); } catch (e) { LL.msg(msg, t('update.restarting'), 'info'); return; }
      const st = s.status || {};
      if (s.current === version) { clearInterval(timer); LL.msg(msg, t('update.done', { v: version }), 'ok'); setTimeout(() => location.reload(), 1500); return; }
      // until the sidecar picks up this request, status.json still describes an earlier run
      if (nonce && st.nonce !== nonce) { LL.msg(msg, t('update.state.idle'), 'info'); return; }
      const bad = st.state === 'failed' || st.state === 'rolled_back';
      if (st.state === 'idle' && st.message_key) { clearInterval(timer); LL.msg(msg, t(st.message_key), 'info'); return; }
      LL.msg(msg, t('update.state.' + (st.state || 'idle')) + (st.message_key ? ' — ' + t(st.message_key) : ''), bad ? 'error' : 'info');
      if (bad) clearInterval(timer);
    }, 2000);
  }

  // ------------------------------------------------------------------ devices list
  async function devicesView(app, rest) {
    const fr = frame(app, 'devices');
    const data = await api('/api/devices');
    const q = h('input', { type: 'search', class: 'input', placeholder: t('devices.search'), 'aria-label': t('devices.search') });
    const showHidden = h('input', { type: 'checkbox' });
    const list = h('div', { class: 'device-list' });
    const panel = h('aside', { class: 'detail', hidden: true, 'aria-label': t('detail.label') });
    // list filter from the start page (#/devices?show=new|unsure|offline): a chip says what is shown and removes the filter
    const show = ['new', 'unsure', 'offline'].includes(LL.query.get('show')) ? LL.query.get('show') : '';
    let ov = null;
    if (show === 'new') { try { ov = await api('/api/overview'); } catch (e) { ov = null; } }
    const newIds = new Set(ov ? ov.new.map(d => d.id) : []);
    const keep = d => !show || (show === 'new' ? newIds.has(d.id) : show === 'offline' ? d.status === 'down' : (!d.is_self && d.certainty !== 'sure'));
    const here = id => '#/devices' + (id ? '/' + encodeURIComponent(id) : '') + LL.queryString();
    // a second interface has no card of its own on the map: its row opens the card of the device it belongs to
    const cardOf = id => { const d = data.devices.find(x => x.id === id); return d && d.merged_into ? d.merged_into.id : id; };
    // the list scrolls inside the main area: the card opens where the list is scrolled to, so it is in view however
    // far down the row was (set once when it opens; following every scroll would move it under a pointer)
    const placeCard = () => { const y = fr.main.scrollTop; panel.style.transform = y ? 'translateY(' + y + 'px)' : ''; };
    const openRow = id => { placeCard(); LL.deepLink(here(id)); openDetail(panel, cardOf(id), () => LL.deepLink(here(null))); };
    let render = function () {
      const s = q.value.toLowerCase();
      const rows = data.devices.filter(d => keep(d) && (showHidden.checked || !d.hidden) && (!s || [d.title, d.ip, d.mac, t('kind.' + d.kind), (d.hostnames || []).join(' '), d.services.join(' ')].join(' ').toLowerCase().includes(s)));
      list.replaceChildren(...rows.map(d => h('button', { class: 'device-row' + (d.hidden ? ' is-hidden' : ''), type: 'button', onclick: () => openRow(d.id) },
        LL.kindIcon(d.kind), h('span', { class: 'device-row__main' }, h('span', { class: 'device-row__title' }, d.is_self ? t('kind.self') : (d.title || t('kind.' + d.kind))), h('span', { class: 'device-row__sub mono' }, d.ip || d.mac || '')),
        h('span', { class: 'device-row__kind' }, t('kind.' + d.kind)), h('span', { class: 'device-row__svc' }, d.services.slice(0, 3).map(s => h('span', { class: 'll-badge' }, s))),
        h('span', { class: 'status-dot status-dot--' + d.status, title: t('board.status.' + d.status) }))));
      if (!rows.length) list.replaceChildren(h('div', { class: 'empty-note' }, t('devices.none')));
      count.textContent = t('devices.count', { n: rows.length, total: data.devices.length });
    };
    const count = h('span', { class: 'muted' });
    // catalogue switch: the list of devices or the stability of each over 35 days (js/history.js)
    let mode = LL.stabilityPanel && LL.query.get('view') === 'stability' ? 'stability' : 'list';
    let stab = null;
    const modeSwitch = LL.stabilityPanel ? h('div', { class: 'seg dev-mode', role: 'group', 'aria-label': t('stab.switch') }) : null;
    function drawMode() {
      if (!modeSwitch) return;
      modeSwitch.replaceChildren(...[['list', 'stab.mode.list'], ['stability', 'stab.mode.stability']].map(([k, label]) => h('button', {
        type: 'button', class: 'seg__btn' + (k === mode ? ' is-active' : ''), 'aria-pressed': k === mode ? 'true' : 'false',
        onclick: () => { mode = k; LL.deepLink('#/devices' + (k === 'stability' ? '?view=stability' : '')); drawMode(); render(); } }, t(label))));
    }
    const baseRender = render;
    render = function () {
      showHidden.closest('label').hidden = mode === 'stability';
      if (mode === 'stability') {
        if (!stab) stab = LL.stabilityPanel({ filter: () => q.value.toLowerCase().trim(), onOpen: id => openRow(id) });
        else stab.redraw();
        list.replaceChildren(stab);
        count.textContent = '';
        return;
      }
      baseRender();
    };
    q.addEventListener('input', () => render()); showHidden.addEventListener('change', () => render());
    fr.main.append(h('div', { class: 'page page--wide' }, h('div', { class: 'page__head inv-head' }, h('h1', {}, t('nav.devices')), count, LL.inventorySwitch ? LL.inventorySwitch('devices') : null),
      h('div', { class: 'toolbar' }, q, modeSwitch, h('label', { class: 'check' }, showHidden, h('span', {}, t('devices.show_hidden'))),
        show ? h('a', { class: 'mf-chip is-on dev-filter', href: '#/devices', title: t('devices.filter.clear') }, t('devices.filter.' + show), h('span', { 'aria-hidden': 'true' }, ' ✕')) : null), list), panel);
    drawMode();
    render();
    // deep link #/devices/<id> (answers of "Ask your network", the start page and the live rail link here): open that device's card
    const want = rest && rest[0] ? decodeURIComponent(rest[0]) : '';
    if (want && data.devices.some(d => d.id === want)) openDetail(panel, cardOf(want), () => LL.deepLink(here(null)));
    return fr;
  }

  // ------------------------------------------------------------------ settings
  // sub-pages of the settings; each names the lowest role that sees it (the server enforces the same per route)
  const SETTINGS_PAGES = [['general', '', 'settings.general', 'viewer'], ['account', 'account', 'settings.tab.account', 'viewer'],
    ['notifications', 'notifications', 'notify.tab', 'viewer'],
    ['users', 'users', 'settings.tab.users', 'admin'], ['security', 'security', 'settings.tab.security', 'admin'], ['backup', 'backup', 'backup.tab', 'admin'],
    ['storage', 'storage', 'storage.tab', 'admin'], ['network', 'network', 'settings.tab.network', 'viewer'], ['ai', 'ai', 'ai.tab', 'admin']];
  function settingsSubnav(active) {
    const allowed = p => p[3] === 'viewer' || LL.can(p[3] === 'admin' ? 'admin' : 'operate');
    return h('nav', { class: 'seg settings-subnav', 'aria-label': t('settings.sections') }, SETTINGS_PAGES.filter(allowed).map(([k, sub, label]) =>
      h('a', { href: '#/settings' + (sub ? '/' + sub : ''), class: 'seg__btn' + (k === active ? ' is-active' : ''), 'aria-current': k === active ? 'page' : null }, t(label))));
  }
  LL.settingsSubnav = settingsSubnav;
  async function settingsView(app, rest) {
    const fr = frame(app, 'settings');
    const sub = rest && rest[0];
    const subPage = { ai: LL.can('admin') && LL.aiSettingsPage, account: LL.accountPage, notifications: LL.notifyPage, users: LL.can('admin') && LL.usersPage, security: LL.can('admin') && LL.securityPage, backup: LL.can('admin') && LL.backupPage, storage: LL.can('admin') && LL.storagePage, network: LL.networkPage }[sub];
    if (subPage) {
      const pg = h('div', { class: 'page page--narrow' }, h('div', { class: 'page__head settings-head' }, h('h1', {}, t('nav.settings')), settingsSubnav(sub)));
      fr.main.append(pg);
      const inner = await subPage(pg);
      const destroy = fr.destroy;
      fr.destroy = () => { if (inner && inner.destroy) inner.destroy(); if (destroy) destroy.call(fr); };
      return fr;
    }
    if (!LL.can('admin')) return generalForEveryone(fr);
    const [s, scan, nets, upd] = await Promise.all([api('/api/settings'), api('/api/scan'), api('/api/networks'), api('/api/update')]);
    const page = h('div', { class: 'page page--narrow' }, h('div', { class: 'page__head settings-head' }, h('h1', {}, t('nav.settings')), settingsSubnav('general')));
    // scanning
    const cidrs = new Set((s.rescan_cidrs && s.rescan_cidrs.length ? s.rescan_cidrs : nets.networks.filter(n => n.allowed && n.gateway).map(n => n.cidr)));
    const netBox = h('div', { class: 'choice-list choice-list--compact' }, nets.networks.concat([...cidrs].filter(c => !nets.networks.some(n => n.cidr === c)).map(c => ({ cidr: c, allowed: true }))).map(n => {
      const cb = h('input', { type: 'checkbox', checked: cidrs.has(n.cidr), disabled: !n.allowed, onchange: () => { cb.checked ? cidrs.add(n.cidr) : cidrs.delete(n.cidr); } });
      return h('label', { class: 'choice' + (n.allowed ? '' : ' is-disabled') }, cb, h('span', { class: 'choice__main' }, h('span', { class: 'choice__title mono' }, n.cidr)));
    }));
    const deep = h('input', { type: 'checkbox' });
    const consent = h('input', { type: 'checkbox', disabled: true });
    deep.addEventListener('change', () => { consent.disabled = !deep.checked; if (!deep.checked) consent.checked = false; });
    const snmpPub = h('input', { type: 'checkbox', checked: !!s.scan_snmp_public });
    const scanMsg = h('div', { class: 'msg' });
    const last = scan.last;
    const scanBtn = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      LL.msg(scanMsg, '');
      try { await api('/api/scan', { method: 'POST', body: { cidrs: [...cidrs], deep: deep.checked, consent: consent.checked, snmp_public: snmpPub.checked } }); LL.msg(scanMsg, t('settings.scan.started'), 'ok'); watchScan(scanMsg); }
      catch (e) { LL.msg(scanMsg, LL.errText(e), 'error'); }
    } }, t('settings.scan.now'));
    const rescan = h('select', { class: 'select', onchange: () => api('/api/settings', { method: 'PUT', body: { rescan_hours: Number(rescan.value) } }).then(() => LL.toast(t('settings.saved'), 'ok')) },
      [0, 1, 3, 6, 12, 24].map(n => h('option', { value: String(n) }, n ? t('settings.rescan.every', { n }) : t('settings.rescan.never'))));
    rescan.value = String(s.rescan_hours);
    page.append(card(t('settings.scan.title'), [
      h('p', { class: 'muted' }, last ? t('settings.scan.last', { when: LL.fmtDate(last.started), n: last.hosts, mode: t('scan.mode.' + last.mode) }) : t('settings.scan.never')),
      netBox,
      h('label', { class: 'check' }, deep, h('span', {}, t('wizard.scan.deep'))), h('label', { class: 'check check--consent' }, consent, h('span', {}, t('wizard.scan.consent'))),
      h('label', { class: 'check' }, snmpPub, h('span', {}, t('scan.snmp_public'))), h('p', { class: 'muted fineprint' }, t('scan.snmp_public.hint')),
      scanMsg, h('div', { class: 'btn-row' }, scanBtn),
      h('hr'), LL.field(t('settings.rescan'), rescan, t('settings.rescan.hint'))]));
    if (scan.current && scan.current.state === 'running') watchScan(scanMsg);
    // integrations
    const integWrap = h('div', { class: 'stack' });
    async function renderInteg() {
      const list = (await LL.loadIntegrations()).integrations;
      integWrap.replaceChildren(...list.map(i => LL.integrationCard(i.kind, i, renderInteg)),
        h('div', { class: 'btn-row btn-row--wrap' }, LL.integKinds().map(k => h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => integWrap.insertBefore(LL.integrationCard(k, null, renderInteg), integWrap.lastChild) },
          ['hub', 'mqtt', 'snmp', 'controller'].includes(k) ? t('integ.add.' + k) : t('integ.add.family', { name: LL.integTitle(k) })))));
    }
    page.append(card(t('settings.integrations'), [h('p', { class: 'muted' }, t('wizard.connect.safety')), integWrap]));
    page.append(card(t('setup.again.title'), [h('p', { class: 'muted' }, t('setup.again.text')),
      h('div', { class: 'btn-row btn-row--wrap' }, h('button', { class: 'btn btn--primary', type: 'button', onclick: () => LL.setupAgain() }, t('setup.again.start')),
        LL.state && LL.state.demo ? null : h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => LL.demoView(true) }, t('setup.demo.start')))]));
    renderInteg();
    // appearance
    const [lang, theme] = langThemeSelects();
    const unit = h('select', { class: 'select', onchange: async () => {
      await api('/api/settings', { method: 'PUT', body: { rate_unit: unit.value } }); LL.rateUnit = unit.value; LL.toast(t('settings.saved'), 'ok'); } },
      ['bytes', 'bits'].map(v => h('option', { value: v }, t('settings.rate_unit.' + v))));
    unit.value = s.rate_unit === 'bits' ? 'bits' : 'bytes';
    page.append(card(t('settings.appearance'), [h('div', { class: 'grid-2' }, LL.field(t('settings.language'), lang), LL.field(t('settings.theme'), theme)),
      paletteField(), h('div', { class: 'grid-2' }, LL.field(t('settings.rate_unit'), unit, t('settings.rate_unit.hint')), motionField())]));
    if (LL.pathsSettingsCard) page.append(LL.pathsSettingsCard(s));
    if (LL.netflowSettingsCard) page.append(LL.netflowSettingsCard(s));
    // updates + about
    const updToggle = h('input', { type: 'checkbox', checked: !!s.update_check, onchange: () => api('/api/settings', { method: 'PUT', body: { update_check: updToggle.checked } }).then(() => LL.toast(t('settings.saved'), 'ok')) });
    const updMsg = h('div', { class: 'msg' });
    const reportCard = LL.reportSettingsCard ? await LL.reportSettingsCard() : null;
    if (reportCard) page.append(reportCard);
    page.append(card(t('settings.updates'), [
      facts([[t('settings.version'), upd.current], [t('settings.latest'), upd.latest || '—'], [t('settings.checked'), upd.checked ? LL.fmtDate(upd.checked) : '—'],
        [t('settings.sidecar'), upd.sidecar ? t('settings.sidecar.yes') : t('settings.sidecar.no')]]),
      upd.sidecar_old ? h('div', { class: 'callout' }, h('p', {}, t('update.sidecar_old')), h('code', { class: 'cmd' }, 'docker compose pull && docker compose --profile updater up -d')) : null,
      h('label', { class: 'check' }, updToggle, h('span', {}, t('settings.update_check'))), h('p', { class: 'field__hint' }, t('settings.update_check.hint')), updMsg,
      h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
        try { updateState = await api('/api/update/check', { method: 'POST', body: {} }); LL.msg(updMsg, updateState.available ? t('update.available', { v: updateState.latest }) : (updateState.error ? t('update.check_failed') : t('update.current')), updateState.available ? 'ok' : 'info'); if (updateState.available) openUpdate(); }
        catch (e) { LL.msg(updMsg, LL.errText(e), 'error'); }
      } }, t('update.check_now')))]));
    page.append(diagnosticsCard());
    page.append(card(t('settings.about'), [h('p', {}, t('settings.about.text')), h('p', { class: 'muted' }, t('settings.about.privacy'))]));
    fr.main.append(page);
    if (rest && rest[0] === 'reports' && reportCard) setTimeout(() => reportCard.scrollIntoView({ block: 'start' }), 30);
    return fr;
  }
  // Settings > General > Diagnostic export: one redacted JSON file for an issue (lanloom/diagnostics.py); the download is audited
  function diagnosticsCard() {
    const msg = h('div', { class: 'msg' });
    const btn = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
      LL.msg(msg, '');
      btn.disabled = true;
      try {
        const doc = await api('/api/diagnostics', { method: 'POST', body: {} });
        const url = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2) + '\n'], { type: 'application/json' }));
        const a = h('a', { href: url, download: 'lanloom-diagnostics.json', class: 'sr-only' });
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 4000);
        LL.msg(msg, t('diag.done', { n: (doc.redaction && doc.redaction.replaced) || 0 }), 'ok');
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      btn.disabled = false;
    } }, t('diag.button'));
    return card(t('diag.title'), [h('p', { class: 'muted' }, t('diag.text')), h('p', { class: 'muted fineprint' }, t('diag.never')), msg, h('div', { class: 'btn-row' }, btn)]);
  }
  function card(title, kids) { return h('section', { class: 'card settings-card' }, h('h2', {}, title), kids); }
  LL.settingsCard = card;
  LL.facts = facts;
  function motionField() {
    const sel = h('select', { class: 'select', onchange: () => { LL.setMotion(sel.value); LL.toast(t('settings.saved'), 'ok'); } },
      ['auto', 'full', 'saver', 'off'].map(v => h('option', { value: v }, t('settings.motion.' + v))));
    sel.value = LL.motion ? LL.motion() : 'auto';
    return LL.field(t('settings.motion'), sel, t('settings.motion.hint'));
  }
  function langThemeSelects() {
    // the language is a personal choice (stored with the account); the first account's choice is the default for everyone
    const lang = h('select', { class: 'select', onchange: async () => { await api('/api/account/prefs', { method: 'PUT', body: { language: lang.value } }); await LL.loadLang(lang.value); LL.route(); } },
      LL.langOptions());
    lang.value = LL.lang;
    const theme = h('select', { class: 'select', onchange: () => LL.setTheme(theme.value) }, ['auto', 'light', 'dark'].map(v => h('option', { value: v }, t('theme.' + v))));
    theme.value = LL.theme();
    return [lang, theme];
  }
  // what viewers and operators see under Settings > General: appearance and about (everything else is for admins)
  function generalForEveryone(fr) {
    const page = h('div', { class: 'page page--narrow' }, h('div', { class: 'page__head settings-head' }, h('h1', {}, t('nav.settings')), settingsSubnav('general')));
    const [lang, theme] = langThemeSelects();
    page.append(card(t('settings.appearance'), [h('div', { class: 'grid-2' }, LL.field(t('settings.language'), lang), LL.field(t('settings.theme'), theme)), paletteField(), motionField()]),
      card(t('settings.role.title'), [h('p', {}, t('settings.role.' + ((LL.state.can && LL.state.can.role) || 'viewer'))), h('p', { class: 'muted' }, t('settings.role.ask'))]),
      card(t('settings.about'), [h('p', {}, t('settings.about.text')), h('p', { class: 'muted' }, t('settings.about.privacy'))]));
    fr.main.append(page);
    return fr;
  }
  // Colour theme: one preview per palette, drawn with that palette's own tokens in the mode that is showing now.
  function paletteField() {
    const mode = LL.effectiveTheme();
    const group = h('div', { class: 'palette-pick', role: 'radiogroup', 'aria-labelledby': 'palette-label' });
    const refresh = () => group.querySelectorAll('.pp').forEach(el => el.classList.toggle('is-on', el.querySelector('input').checked));
    LL.PALETTES.forEach(id => {
      const input = h('input', { type: 'radio', name: 'palette', value: id, class: 'pp__input', 'aria-describedby': 'pp-desc-' + id, onchange: () => {
        if (!input.checked) return;
        LL.setPalette(id); refresh(); LL.toast(t('palette.changed', { name: t('palette.' + id) }), 'ok');
      } });
      input.checked = LL.palette() === id;
      const preview = h('span', { class: 'pp__preview', 'aria-hidden': 'true', dataset: { palette: id, theme: mode } },
        h('i', { class: 'pp__bar' }),
        h('span', { class: 'pp__tile' }, h('i', { class: 'pp__dot' }), h('i', { class: 'pp__line' })),
        h('span', { class: 'pp__traces' }, ['lan', 'radio', 'service', 'flow', 'integration', 'uplink'].map(k => h('i', { class: 'pp__trace pp__trace--' + k }))));
      group.append(h('label', { class: 'pp' + (input.checked ? ' is-on' : '') }, input, preview,
        h('span', { class: 'pp__name' }, t('palette.' + id)), h('span', { class: 'pp__desc', id: 'pp-desc-' + id }, t('palette.' + id + '.desc'))));
    });
    const follow = () => {                                  // the mode select above changes the mode the previews are drawn in
      if (!group.isConnected) { document.removeEventListener('ll-theme', follow); return; }
      group.querySelectorAll('.pp__preview').forEach(el => { el.dataset.theme = LL.effectiveTheme(); });
    };
    document.addEventListener('ll-theme', follow);
    return h('div', { class: 'field' }, h('span', { class: 'field__label', id: 'palette-label' }, t('settings.palette')), group,
      h('div', { class: 'field__hint' }, t('settings.palette.hint')));
  }
  function watchScan(msg) {
    const timer = setInterval(async () => {
      let s; try { s = (await api('/api/scan')).current; } catch (e) { return; }
      if (s.state === 'running') LL.msg(msg, t('settings.scan.progress', { phase: t('scan.phase.' + s.phase), found: s.found || 0 }), 'info');
      else { clearInterval(timer); LL.msg(msg, s.state === 'done' ? t('settings.scan.done', { n: s.found }) : t(s.error || 'wizard.scan.stopped'), s.state === 'done' ? 'ok' : 'error'); }
    }, 1200);
  }

  LL.frame = frame;
  LL.liveSubscribe = liveSubscribe;
  LL.dialog = dialog;
  LL.view('map', mapView('overview'));
  LL.view('network', mapView('network'));
  LL.view('devices', devicesView);
  LL.view('settings', settingsView);
})();
