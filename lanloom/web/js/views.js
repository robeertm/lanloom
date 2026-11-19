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
    // the page's own site rides in the address (a socket cannot carry headers): the server closes with 4409 when it is not the
    // one it may serve any more, and never serves the cookie's site to a page that shows another one (M2)
    const asked = LL.pageSite && LL.pageSite();
    const ws = live.ws = new WebSocket(proto + '//' + location.host + LL.url('/api/live') + (asked ? '?site=' + encodeURIComponent(asked) : ''));
    ws.onmessage = ev => {
      if (live.ws !== ws || (LL.switching && LL.switching())) return;     // a socket of the site that was left draws nothing
      let f; try { f = JSON.parse(ev.data); } catch (e) { return; }
      live.last = f; live.retry = 1000; live.lastAt = Date.now();
      if (f.t) live.offset = Date.now() / 1000 - f.t;       // clock offset browser - server (plus the transport time)
      liveState('live');
      live.listeners.forEach(fn => fn(f));
    };
    ws.onclose = ev => {
      if (live.ws === ws) live.ws = null;
      if (LL.switching && LL.switching()) return;           // leaving the site is no loss of the connection: no "Disconnected"
      if (ev && ev.code === 4409 && LL.siteResync) { LL.siteResync(); return; }   // not this page's site any more: follow the server once
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
    // the page leaves its site: the socket closes for good, quietly (no state change, no retry)
    shutdown() {
      clearTimeout(live.timer); live.paused = true;
      const old = live.ws; live.ws = null;
      if (old) {
        try {
          old.onclose = null; old.onmessage = null;
          if (old.readyState === 0) old.onopen = () => { try { old.close(); } catch (e) { /* closing */ } };    // not yet open: closing now would only make a console error
          else old.close();
        } catch (e) { /* closing */ }
      }
    },
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
    if (LL.state && LL.state.agents > 0) tabs.splice(tabs.findIndex(x => x[0] === 'reports'), 0, ['running', 'nav.running']);   // what runs on the hosts with an agent or a container source
    tabs.splice(tabs.findIndex(x => x[0] === 'network') + 1, 0, ['controlroom', 'nav.controlroom']);   // the control room: internet, storage boxes, battery backup, backups (js/controlroom.js)
    if (LL.atHome && !LL.atHome()) tabs.splice(tabs.findIndex(x => x[0] === 'firewall'), 1);   // the firewall editor works on the home site only (M1)
    const nav = h('nav', { class: 'tabs', 'aria-label': t('nav.label') }, tabs.map(([k, label]) =>
      h('a', { href: '#/' + k, class: 'tab tab--' + k + (k === active ? ' is-active' : '') + (LL.strings['nav.short.' + k] ? ' tab--short' : ''), 'aria-current': k === active ? 'page' : null,
        title: t(label), 'aria-label': t(label) },   // narrow screens show only the icon
        h('span', { class: 'tab__icon', 'aria-hidden': 'true' }), h('span', { class: 'tab__label' }, t(label)),
        LL.strings['nav.short.' + k] ? h('span', { class: 'tab__short', 'aria-hidden': 'true' }, t('nav.short.' + k)) : null)));
    const navIcon = k => NAV_ICONS[k] || ({ paths: LL.NAV_PATHS_ICON, reports: LL.NAV_REPORTS_ICON, controlroom: LL.NAV_CONTROLROOM_ICON }[k]) || LL.FW_ICON || '';
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
    const siteSwitch = LL.siteSwitcher ? LL.siteSwitcher() : null;          // several sites (M1): the switcher takes the room of one tab
    const header = h('header', { class: 'topbar' + (tabs.length + (siteSwitch ? 1 : 0) > 9 ? ' topbar--many' : '') },   // more than nine tabs (a hub or running hosts): narrower steps
      h('a', { class: 'topbar__brand', href: '#/map' }, LL.logo(26), h('span', { class: 'brand-name' }, 'Lanloom'),
        LL.state && LL.state.demo ? h('span', { class: 'chip chip--demo' }, t('app.demo')) : null),
      LL.versionChip ? LL.versionChip() : null,
      siteSwitch,
      nav, h('div', { class: 'topbar__right' }, LL.paletteButton ? LL.paletteButton() : null, LL.aiAskButton && (!LL.atHome || LL.atHome()) ? LL.aiAskButton() : null, updateBtn, reconnectBtn, gradePill,
        LL.railButtons ? LL.railButtons() : null, themeBtn));
    themeBtn.classList.add('theme-btn');
    const main = h('main', { class: 'main main--' + active, id: 'll-main', tabindex: '-1', 'aria-label': t('nav.' + active) }, content);
    const skip = h('a', { class: 'skip-link', href: '#ll-main', onclick: ev => { ev.preventDefault(); main.focus(); } }, t('a11y.skip'));
    const siteNow = LL.site ? LL.site() : null;
    document.title = t('nav.' + active) + (siteNow && siteNow.several ? ' · ' + LL.siteName(siteNow) : '') + ' · Lanloom';
    const notes = LL.noticeBar ? LL.noticeBar() : null;              // security notices (new device, lock-out ...)
    const demoBar = LL.state && LL.state.demo_view ? h('div', { class: 'demo-bar', role: 'status' },
      h('span', { class: 'demo-bar__text' }, h('strong', {}, t('setup.demo.bar_title')), ' ', t('setup.demo.bar_text')),
      h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => LL.demoView(false) }, t('setup.demo.leave'))) : null;
    const verFoot = active === 'map' || active === 'network' || !LL.versionFoot ? null : LL.versionFoot();   // phones: the version sits at the end of the page, not in the header
    app.replaceChildren(h('div', { class: 'frame' }, skip, header, demoBar, notes, LL.shellBody ? LL.shellBody(main, active) : main, verFoot));
    // a new page starts at its content: the focused tab of the old page is gone, the keyboard and screen reader land in the new view
    if (!frame.first) main.focus({ preventScroll: true });
    frame.first = false;
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
      if (LL.a11y && LL.a11y.watch) LL.a11y.watch(f);          // spoken status: a device stopped talking or is back (throttled)
    });
    let lastState = live.state;
    const unStatus = LL.live.onStatus(() => {
      if (live.state !== 'live') gradePill.dataset.grade = 'unknown';
      showState();
      if (live.state !== lastState && LL.a11y && (live.state === 'offline' || (lastState === 'offline' && live.state === 'live'))) LL.a11y.watchState(live.state);
      lastState = live.state;
    });
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
  frame.first = true;       // the first page after loading keeps the browser's own start of focus
  const NAV_ICONS = {
    overview: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 13a8 8 0 0 1 16 0"/><path d="M12 13l3.5-4"/><path d="M4 17h16"/><circle cx="12" cy="13" r="1.2"/></svg>',
    map: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M10 6.5h4.5v7.5M6.5 10v7.5H14"/></svg>',
    network: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="5" rx="1"/><rect x="2.5" y="16.5" width="6" height="5" rx="1"/><rect x="15.5" y="16.5" width="6" height="5" rx="1"/><path d="M12 7.5v4.5M5.5 16.5V12h13v4.5"/></svg>',
    devices: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1.2"/><circle cx="3.5" cy="12" r="1.2"/><circle cx="3.5" cy="18" r="1.2"/></svg>',
    sensors: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h3l2.5-6 4 12 2.5-6H21"/></svg>',
    automations: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="6" r="2"/><circle cx="5" cy="18" r="2"/><rect x="15" y="9" width="6" height="6" rx="1.5"/><path d="M7 6h3a2 2 0 0 1 2 2v2.5a1.5 1.5 0 0 0 1.5 1.5H15M7 18h3a2 2 0 0 0 2-2v-2.5"/></svg>',
    running: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="7" rx="1.8"/><rect x="3" y="13" width="18" height="7" rx="1.8"/><path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6"/></svg>',
    settings: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2.2"/><circle cx="10" cy="17" r="2.2"/></svg>'
  };

  // ------------------------------------------------------------------ translation of map views
  function localise(view) {
    const v = JSON.parse(JSON.stringify(view));
    v.zones.forEach(z => { if (z.title_key) z.title = t(z.title_key, z.vars || {}); });
    v.components.forEach(c => {
      // the "+N quiet devices" tile of a large network reads "Fold N quiet devices" while that network is unfolded
      if (c.quiet_more) c.expanded = LL.isUnfolded(c.fold_key);
      if (c.title_key && !c.title) c.title = t(c.quiet_more && LL.isUnfolded(c.fold_key) ? 'comp.quiet.less' : c.title_key, c.vars || {}) + (c.title_suffix ? ' ' + c.title_suffix : '');
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
      const fcSlot = h('div', { class: 'fc-slot' });           // the failure chain bar and the outage note (js/failure.js)
      fr.main.append(h('div', { class: 'map-col' }, filterBar, fcSlot, host), panel);
      // the board can fill the window (phones above all): its host moves into an overlay and back, the live feed keeps running
      const fullScreen = kind === 'network' && LL.hostFullScreen ? LL.hostFullScreen(host, { app, onClosed: () => { const b = filterBar.querySelector('.nm-fullbtn'); if (b) b.focus({ preventScroll: true }); } }) : null;
      const fullButton = () => LL.fullButton(() => fullScreen.open());
      let data = await api('/api/map?view=' + (kind === 'network' ? 'network' : 'overview'));
      if (!data.components.length) {
        filterBar.remove();
        host.replaceChildren(emptyState());
        return fr;
      }
      let selected = null;
      // the Network view has three faces: the circuit board, the tree of the chain (Internet → gateway → switches → devices) and the
      // same chain as a stack of boards in 3D; the choice is kept per browser and can be given in the address (#/network?v=tree, ?v=3d)
      const treeOk = kind === 'network' && !!(LL.Tree && LL.Tree.mount);
      const s3dOk = treeOk && !!(LL.Scene3D && LL.Scene3D.mount);
      const FACES = ['board', 'tree'].concat(s3dOk ? ['3d'] : []);
      const asked = LL.query.get('v'), kept = LL.prefs.get('ll-netview', 'board');
      let mode = treeOk ? (FACES.includes(asked) ? asked : (FACES.includes(kept) ? kept : 'board')) : 'board';
      let face = mode, s3dLost = false;          // what is really shown: '3d' falls back to the board without graphics acceleration
      // the address follows the open card (#/map/<id>), the back key closes it again
      const here = id => '#/' + route + (id ? '/' + encodeURIComponent(id) : '') + LL.queryString();
      const onSelectId = (id, fromAddress) => {
        // the "+N quiet devices" tile of a large network folds or unfolds that network's quiet devices; it opens no card
        const qm = String(id).startsWith('qm:') ? data.components.find(x => x.id === id && x.quiet_more) : null;
        if (qm) { LL.setUnfolded(qm.fold_key, !LL.isUnfolded(qm.fold_key)); return; }
        selected = id; board.highlight(id);
        if (!fromAddress) LL.deepLink(here(id));
        const opener = document.activeElement;
        openDetail(panel, id, () => {
          selected = null; board.highlight(null); LL.deepLink(here(null));
          // the keyboard goes back to where the card was opened from (the tile or tree row), not to the top of the page
          const back = opener && opener.isConnected ? opener : host.querySelector('[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
          if (back && back.focus) back.focus({ preventScroll: true });
        });
      };
      // area filter (#/map?zone=<id>) and "problems only" (#/map?only=problems); the internet edge always stays for orientation
      // containers sit folded in their host; a host that is unfolded (remembered in this browser) shows them
      const unfolded = view => {
        if (!view.components.some(c => c.fold && !LL.isUnfolded(c.fold))) return view;
        const hide = c => c.fold && !LL.isUnfolded(c.fold);
        const comps = view.components.filter(c => !hide(c)), ids = new Set(comps.map(c => c.id));
        return Object.assign({}, view, { components: comps, zones: view.zones.filter(z => !hide(z)), traces: view.traces.filter(tr => ids.has(tr.a) && ids.has(tr.b)) });
      };
      const filtered = full => {
        const view = unfolded(full);
        const zone = LL.query.get('zone'), only = LL.query.get('only');
        if (!zone && !only) return view;
        let comps = view.components;
        if (zone) comps = comps.filter(c => c.zone === zone || c.zone === 'edge');
        if (only === 'problems') comps = comps.filter(c => c.zone === 'edge' || c.status === 'down' || c.grade === 'interrupted');
        const ids = new Set(comps.map(c => c.id)), zs = new Set(comps.map(c => c.zone));
        return Object.assign({}, view, { components: comps, zones: view.zones.filter(z => zs.has(z.id)), traces: view.traces.filter(tr => ids.has(tr.a) && ids.has(tr.b)) });
      };
      const show = d => localise(filtered(d));
      // "Who talks to whom": dashed lines in the air between devices that talk to each other (gateway flows and host agents, last 24 hours)
      let talkOn = LL.query.get('talk') === '1', talkInfo = null, talkVer = null, talkBusy = false;
      async function loadTalk() {
        if (!board.setTalk) return;                      // the tree has no lines in the air
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
      // the tree filters on its own (it keeps the chain above a match) and has no fold tile: it lists every device
      const noQuiet = v => (v.components.some(c => c.quiet_more) ? Object.assign({}, v, { components: v.components.filter(c => !c.quiet_more) }) : v);
      const feed = d => (face === 'tree' || face === '3d' ? noQuiet(localise(d)) : show(d));
      const treeFilter = () => ({ zone: LL.query.get('zone') || '', only: LL.query.get('only') || '' });
      // containers fold under their host (E4): the board hides them until the host is opened (remembered per host)
      const foldIds = () => data.components.filter(c => c.folds).map(c => c.id);
      LL.foldHosts = foldIds();
      // the area filter (?zone=) and "problems only" (?only=): the address follows, the board is drawn again
      function setFilter(key, value) {
        if (value) LL.query.set(key, value); else LL.query.delete(key);
        LL.deepLink(here(selected));
        redraw();
      }
      // the legend of the board toggles the areas too: it gets the areas of the whole view (the filtered view only holds one)
      const zoneList = () => localise(data).zones.filter(z => !z.fold);
      const syncZones = () => { if (board && board.setZoneFilter) board.setZoneFilter(LL.query.get('zone') || null, zoneList()); };
      function redraw() {
        LL.foldHosts = foldIds();
        syncZones();
        board.update(feed(data)); if (LL.a11y) LL.a11y.setNames(localise(data).components); if (selected) board.highlight(selected);
        drawFilter();
        if (LL.failure) LL.failure.refresh();
        if (LL.resilience) LL.resilience.refresh();
      }
      function drawFilter() {
        const zones = localise(data).zones.filter(z => z.id !== 'edge' && !z.fold);
        const zone = LL.query.get('zone'), only = LL.query.get('only');
        const set = setFilter;
        const chip = (label, on, fn, extra) => h('button', { class: 'mf-chip' + (on ? ' is-on' : '') + (extra || ''), type: 'button', 'aria-pressed': on ? 'true' : 'false', onclick: fn }, label);
        // the view toggle comes first: with many areas the row scrolls and a chip at the end would be out of sight
        const nTalk = talkOn && talkInfo ? talkInfo.pairs.length : 0;
        const talkChip = chip(t('talk.chip') + (nTalk ? ' · ' + nTalk : ''), talkOn, toggleTalk, ' mf-chip--talk');
        if (nTalk) talkChip.title = t('talk.legend', { n: nTalk });
        // focus stays on the same control after a redraw: the child-index path from the bar to it, read from the DOM
        const fPath = []; for (let e = filterBar.contains(document.activeElement) ? document.activeElement : null; e && e !== filterBar; e = e.parentNode) fPath.unshift([...e.parentNode.children].indexOf(e));
        filterBar.replaceChildren(...[
          LL.netModes ? LL.netModes('live') : null, LL.netModes ? h('span', { class: 'mf-sep', 'aria-hidden': 'true' }) : null,
          treeOk ? viewSwitch() : null, face !== 'tree' && fullScreen ? fullButton() : null, treeOk || (face === 'board' && fullScreen) ? h('span', { class: 'mf-sep', 'aria-hidden': 'true' }) : null,
          face !== 'board' ? null : talkChip,        // the lines in the air belong to the board; the tree and the 3D view have the chain
          face !== 'board' || !LL.resilience ? null : chip(t('rs.lens.chip'), LL.resilience.lensOn(), () => LL.resilience.toggleLens(), ' mf-chip--lens'),   // the risk lens (js/resilience.js, key B)
          face === 'board' && LL.rewind ? LL.rewind.chip() : null,
          face === 'board' && talkOn && talkInfo && !talkInfo.pairs.length ? h('span', { class: 'mf-talk muted' }, t('talk.none')) : null,
          face === 'board' && LL.foldHosts.length ? chip(t('mapfilter.containers'), LL.allUnfolded(), () => LL.setAllUnfolded(!LL.allUnfolded()), ' mf-chip--containers') : null,
          h('span', { class: 'mf-sep', 'aria-hidden': 'true' }),
          zones.length > 1 ? chip(t('mapfilter.all'), !zone, () => set('zone', null)) : null,
          ...(zones.length > 1 ? zones.map(z => chip(z.title || '—', zone === z.id, () => set('zone', zone === z.id ? null : z.id))) : []),
          zones.length > 1 ? h('span', { class: 'mf-sep', 'aria-hidden': 'true' }) : null,
          chip(t('mapfilter.problems'), only === 'problems', () => set('only', only === 'problems' ? null : 'problems'), ' mf-chip--problems')].filter(Boolean));
        if (fPath.length) { let e = filterBar; for (const i of fPath) e = e && e.children[i]; if (e && e.focus) e.focus({ preventScroll: true }); }
        const n = filtered(data).components.filter(c => c.zone !== 'edge').length;
        if ((zone || only) && !n) filterBar.append(h('span', { class: 'mf-empty muted' }, t(only ? 'mapfilter.none_problems' : 'mapfilter.none')));
      }
      function mountViewer() {
        face = mode;
        let note = null;
        if (mode === '3d') {
          // 3D only with real graphics acceleration (or after "Try anyway" in this browser); otherwise a short note and the board
          const s3d = !s3dLost && LL.Scene3D.available() ? LL.Scene3D.mount(host, { view: noQuiet(localise(data)), t: LL.t, locale: LL.locale(), filter: treeFilter, onSelect: pick,
            onLost: () => { s3dLost = true; if (face === '3d') { board.destroy(); host.replaceChildren(); board = mountViewer(); drawFilter(); } } }) : null;
          if (s3d) { host.classList.add('board-host--3d'); host.classList.remove('board-host--tree'); return s3d; }
          face = 'board';
          note = h('div', { class: 's3d-fallback', role: 'note' }, h('span', { class: 's3d-fallback__text' }, t('s3d.fallback')),
            LL.Scene3D.canTry() ? h('button', { class: 'btn btn--sm', type: 'button', onclick: () => { LL.Scene3D.allowSoft(true); remount(); } }, t('s3d.try_anyway')) : null);
        }
        host.classList.toggle('board-host--tree', face === 'tree');
        host.classList.remove('board-host--3d');
        host.classList.toggle('board-host--note', !!note);
        if (note) host.appendChild(note);
        if (face === 'tree') return LL.Tree.mount(host, { view: feed(data), t: LL.t, locale: LL.locale(), filter: treeFilter, onSelect: id => onSelectId(id) });
        return LL.Board.mount(host, { view: feed(data), theme: LL.effectiveTheme(), t: LL.t, locale: LL.locale(), onSelect: pick,
          zones: zoneList(), zone: LL.query.get('zone') || null, onZone: id => setFilter('zone', LL.query.get('zone') === id ? null : id) });
      }
      // a tile picked in the full screen first leaves it (the card opens on the page); the fold tile stays in it
      const pick = id => {
        if (!fullScreen || !fullScreen.isOpen() || String(id).startsWith('qm:')) { onSelectId(id); return; }
        fullScreen.close(() => { const tile = host.querySelector('[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]'); if (tile) tile.focus({ preventScroll: true }); onSelectId(id); });
      };
      let board = mountViewer();
      if (LL.a11y) LL.a11y.setNames(localise(data).components);
      const fcDetach = LL.failure ? LL.failure.attach({ kind, slot: () => fcSlot, board: () => board, view: () => feed(data) }) : null;
      const rlDetach = LL.resilience ? LL.resilience.attach({ kind, slot: () => fcSlot, board: () => board, view: () => feed(data) }) : null;   // risk lens (js/resilience.js)
      document.addEventListener('ll-lens', drawFilter);
      // rewind (js/rewind.js): while it plays, the live frames rest and the board shows the moment under the cursor
      const rwDetach = LL.rewind ? LL.rewind.attach({ kind, slot: () => fcSlot, board: () => board, view: () => feed(data),
        onLeave: () => { refresh(); if (LL.failure) LL.failure.refresh(); drawFilter(); } }) : null;
      // Esc closes the open card (not while a dialog, the palette or a text field has the keyboard)
      const onEsc = ev => {
        if (ev.key === 'Escape' && panel.hidden && LL.failure && LL.failure.active() && !document.querySelector('.modal, .cmdk')) { LL.failure.close(); return; }
        if (ev.key !== 'Escape' || panel.hidden || document.querySelector('.modal, .cmdk')) return;
        const el = ev.target;
        if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
        const x = panel.querySelector('.detail__close'); if (x) x.click();
      };
      document.addEventListener('keydown', onEsc);
      // Board / Tree / 3D: one group of buttons, the pressed one is the current face; the open card stays open
      function viewSwitch() {
        const btn = (m, label, title) => {
          const b = h('button', { class: 'seg__btn' + (mode === m ? ' is-active' : ''), type: 'button', 'aria-pressed': mode === m ? 'true' : 'false', onclick: () => switchTo(m) }, t(label));
          if (title) b.title = t(title);
          return b;
        };
        return h('div', { class: 'seg nv-seg', role: 'group', 'aria-label': t('netview.label') }, btn('board', 'netview.board'), btn('tree', 'netview.tree'),
          s3dOk ? btn('3d', 'netview.3d', 'netview.3d_title') : null);
      }
      function remount() {
        board.destroy(); host.replaceChildren();
        board = mountViewer();
        if (selected) board.highlight(selected);
        if (LL.failure) LL.failure.refresh();
        if (LL.resilience) LL.resilience.refresh();
        if (face === 'board' && talkOn) loadTalk();
        drawFilter();
      }
      function switchTo(m) {
        if (m === mode) return;
        mode = m;
        LL.prefs.set('ll-netview', m);
        LL.query.delete('v'); LL.deepLink(here(selected), true);
        remount();
        const again = filterBar.querySelector('.nv-seg .is-active'); if (again) again.focus();
        LL.announce(t(face === 'tree' ? 'netview.now_tree' : face === '3d' ? 'netview.now_3d' : (mode === '3d' ? 's3d.fallback' : 'netview.now_board')), { now: true });
      }
      drawFilter();
      if (talkOn) loadTalk();
      const onTheme = ev => board.setTheme && board.setTheme(ev.detail);
      document.addEventListener('ll-theme', onTheme);
      document.addEventListener('ll-fold', redraw);
      // deep link #/map/<id> or #/network/<id> (command palette, answers of "Ask your network"): open that component's card;
      // a component that sits on the other board sends the page there
      const want = rest && rest[0] ? decodeURIComponent(rest[0]) : '';
      const wantComp = want && data.components.find(c => c.id === want);
      if (wantComp && wantComp.fold && !LL.isUnfolded(wantComp.fold)) { LL.setUnfolded(wantComp.fold, true, true); board.update(feed(data)); }
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
      // one fetch at a time, but one that never ends is given up after 30 s and the board is fetched again (O10)
      const rgate = LL.fresh.gate(60000);
      async function refresh() {
        const k = rgate.enter();
        if (!k) { stale = true; return; }
        try {
          const nd = await api('/api/map?view=' + (kind === 'network' ? 'network' : 'overview'));
          if (rgate.live(k)) { data = nd; structure = data.structure; fw.ok(); redraw(); }
        }
        catch (e) { /* keep the old board */ }
        rgate.leave(k);
        if (stale && rgate.live(k)) { stale = false; refresh(); }
      }
      // how old is the board: the live frames say nothing changed (or a change is owed), a fetch brings the change
      const fw = LL.fresh.watch(host, 60000, () => refresh());
      const unsub = liveSubscribe(f => {
        if (document.hidden || (LL.rewind && LL.rewind.active())) { if (f.structure !== structure || (mapver != null && f.mapver !== mapver)) stale = true; mapver = f.mapver; return; }
        board.live(f);
        if (LL.failure) LL.failure.onFrame(f);
        if (talkOn && f.pathsver != null && f.pathsver !== talkVer) loadTalk();
        const changed = f.structure !== structure || (mapver != null && f.mapver != null && f.mapver !== mapver);
        mapver = f.mapver;
        if (!changed && !stale) fw.ok();
        if (changed || stale) { stale = false; refresh(); }
      });
      const onVis = () => { if (!document.hidden && stale) { stale = false; refresh(); } };
      document.addEventListener('visibilitychange', onVis);
      return { destroy: () => { unsub(); fw.destroy(); if (fcDetach) fcDetach(); if (rlDetach) rlDetach(); if (rwDetach) rwDetach(); document.removeEventListener('ll-lens', drawFilter); document.removeEventListener('visibilitychange', onVis); document.removeEventListener('ll-theme', onTheme); document.removeEventListener('keydown', onEsc); document.removeEventListener('ll-fold', redraw); LL.foldHosts = []; if (fullScreen) fullScreen.destroy(); board.destroy(); fr.destroy(); } };
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
    // Esc closes the card like the close button (O8), also while it is still loading; a dialog, the palette or the paths full screen
    // on top takes Esc first. One listener per panel: the newest card's.
    let close = null, aborted = false;
    if (panel._llEsc) document.removeEventListener('keydown', panel._llEsc);
    const onEsc = panel._llEsc = ev => {
      if (!panel.classList.contains('is-open') || panel.hidden) { document.removeEventListener('keydown', onEsc); return; }
      if (ev.key !== 'Escape' || ev.defaultPrevented || document.querySelector('.modal, .cmdk, .pw-full')) return;
      if (close) { close.click(); return; }
      aborted = true; document.removeEventListener('keydown', onEsc); panel.hidden = true; panel.classList.remove('is-open'); if (onClose) onClose();
    };
    document.addEventListener('keydown', onEsc);
    let d;
    try { d = await api('/api/component/' + encodeURIComponent(id)); } catch (e) { panel.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
    if (aborted) return;
    const c = localise({ zones: [], components: [d.component], traces: [] }).components[0];
    close = h('button', { class: 'icon-btn detail__close', type: 'button', 'aria-label': t('detail.close'), onclick: () => { document.removeEventListener('keydown', onEsc); panel.hidden = true; panel.classList.remove('is-open'); unsub(); if (onClose) onClose(); } }, '✕');
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
    if (d.device && LL.hostHealthSection) sections.push(LL.hostHealthSection(d.device.id || id));   // host gauges and early warnings (W1)
    if (d.device && LL.availabilitySection) sections.push(LL.availabilitySection(id));
    if (d.dns && LL.dnsSection) sections.push(LL.dnsSection(d.dns));
    if (d.dns_client && LL.dnsClientSection) sections.push(LL.dnsClientSection(d.dns_client));
    if (d.container && LL.containerSection) sections.push(LL.containerSection(d.container));
    if (d.agent && LL.agentSection) sections.push(LL.agentSection(d.agent, d.device ? d.device.id : null));
    if (d.device && LL.servicesSection) sections.push(LL.servicesSection(id));
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
    // front view of a switch, router or access point that a controller adapter reads (js/portfront.js); the card's refresh redraws it
    let pfBody = null;
    if (d.portfront && LL.portFront) { pfBody = h('div', {}, LL.portFront(d.portfront, { head: false })); sections.push(section(t('pf.title'), pfBody)); }
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
    if (LL.whatHappenedSection && (d.device || id === 'internet')) sections.push(LL.whatHappenedSection(id));   // "What happened?" (js/incidents.js)
    if (LL.failureSection) sections.push(LL.failureSection(id));          // dependencies, failure chain, manual entries (js/failure.js)
    if (LL.resilienceSection) sections.push(LL.resilienceSection(id));   // risk, seven axes, power and spare (js/resilience.js)
    if (d.device && LL.maintenanceSection) sections.push(LL.maintenanceSection(id));
    if (LL.failure) {                                                       // "probably affected: X is down" (outage automatic)
      const hintBox = h('div', { class: 'fc-hint-box' });
      sections.unshift(hintBox);
      LL.failure.loadOutages().then(() => { const x = LL.failure.cardHint(id); if (x) hintBox.replaceChildren(x); }).catch(() => {});
    }
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
      try { const nd = await api('/api/component/' + encodeURIComponent(id)); d.series = nd.series; drawChart(); if (pfBody && nd.portfront && pfBody.isConnected) pfBody.replaceChildren(LL.portFront(nd.portfront, { head: false })); } catch (e) { /* ignore */ }
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
      LL.integrateSection ? LL.integrateSection(dev) : null, LL.protectSection ? LL.protectSection(dev, may) : null, LL.inventorySection ? LL.inventorySection(dev) : null, LL.vulnsSection ? LL.vulnsSection(dev) : null, ports ? section(t('device.ports'), ports) : null,
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
    // focus moves into the dialog, Tab stays inside, Esc and the close button give the focus back to where it came from
    const opener = document.activeElement;
    let release = null;
    const close = () => { if (!dlg.isConnected) return; dlg.remove(); document.removeEventListener('keydown', esc); if (release) release(); };
    const esc = ev => { if (ev.key === 'Escape' && document.querySelectorAll('.modal').length && [...document.querySelectorAll('.modal')].pop() === dlg) { ev.stopPropagation(); close(); } };
    const dlg = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title, onclick: ev => { if (ev.target === dlg) close(); } },
      h('div', { class: 'modal__card card' }, h('header', { class: 'modal__head' }, h('h2', {}, title), h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('detail.close'), onclick: close }, '✕')),
        h('div', { class: 'modal__body' }, body), foot ? h('footer', { class: 'modal__foot' }, foot) : null));
    document.addEventListener('keydown', esc);
    document.body.appendChild(dlg);
    if (LL.a11y && LL.a11y.trapFocus) release = LL.a11y.trapFocus(dlg, opener);
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
  let updateState = null, updatePill = null;
  const PULL_CMD = 'docker compose pull && docker compose up -d';
  const SIDECAR_CMD = 'docker compose pull && docker compose --profile updater up -d';
  const cmdCallout = (text, cmd) => h('div', { class: 'callout' }, h('p', {}, text), h('code', { class: 'cmd' }, cmd));
  function setUpdateState(u) { updateState = u; if (updatePill) updatePill.hidden = !u.available; }
  async function checkUpdate(btn) {
    updatePill = btn;
    try { setUpdateState(await api('/api/update')); } catch (e) { /* the notice stays hidden */ }
  }
  LL.updateInfo = () => updateState;
  LL.checkUpdateNow = () => checkUpdate(updatePill || { hidden: true });
  function openUpdate() {
    const u = updateState; if (!u) return;
    const msg = h('div', { class: 'msg' });
    const isPreview = !!(u.preview && u.preview.version === u.latest);
    const notes = h('pre', { class: 'release-notes' }, u.notes || '');
    const sig = u.signature || {};
    const apply = u.sidecar ? h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      apply.disabled = true;
      try { const req = await api('/api/update/apply', { method: 'POST', body: { version: u.latest } }); followUpdate(msg, u.latest, req.nonce); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); apply.disabled = false; }
    } }, t('update.apply', { v: u.latest })) : null;
    dialog(t('update.title', { v: u.latest }), h('div', { class: 'stack update-dialog' },
      h('p', {}, t('update.lead', { cur: u.current, v: u.latest }), isPreview ? [' ', h('span', { class: 'chip chip--warn' }, t('update.preview_badge'))] : null),
      isPreview ? h('div', { class: 'callout callout--warn update-preview' }, h('p', {}, t('update.preview_callout'))) : null,
      notes,
      u.sidecar ? h('p', { class: 'muted' }, t('update.sidecar_text')) : cmdCallout(t('update.manual'), PULL_CMD),
      u.sidecar && sig.supported && sig.enforced ? h('p', { class: 'muted update-sig' }, t('update.signature.enforced')) : null,
      u.sidecar && sig.supported === false ? cmdCallout(t('update.signature.unsupported'), SIDECAR_CMD) : null,
      u.sidecar_old && sig.supported !== false ? cmdCallout(t('update.sidecar_old'), SIDECAR_CMD) : null, msg),
      h('div', { class: 'btn-row' }, u.url ? h('a', { class: 'btn btn--ghost', href: u.url, target: '_blank', rel: 'noopener noreferrer' }, t('update.notes_link')) : null, apply));
  }
  function followUpdate(msg, version, nonce) {
    let trustShown = false;
    const timer = setInterval(async () => {
      let s; try { s = await api('/api/update', { allow401: true }); } catch (e) { LL.msg(msg, t('update.restarting'), 'info'); return; }
      const st = s.status || {};
      if (s.current === version) {
        clearInterval(timer);
        LL.msg(msg, t('update.done', { v: version }) + (st.signature && st.signature.verified ? ' ' + t('update.signature.verified') : ''), 'ok');
        setTimeout(() => location.reload(), 1500); return;
      }
      // until the sidecar picks up this request, status.json still describes an earlier run
      if (nonce && st.nonce !== nonce) { LL.msg(msg, t('update.state.idle'), 'info'); return; }
      const bad = st.state === 'failed' || st.state === 'rolled_back';
      if (st.state === 'idle' && st.message_key) { clearInterval(timer); LL.msg(msg, t(st.message_key), 'info'); return; }
      const label = st.state === 'pulling' && st.step === 'verify' ? t('update.state.verify') : t('update.state.' + (st.state || 'idle'));
      LL.msg(msg, label + (st.message_key ? ' — ' + t(st.message_key) : ''), bad ? 'error' : 'info');
      if (bad && st.message_key === 'update.err.signature_trust' && !trustShown) { trustShown = true; msg.after(cmdCallout(t('update.signature.manual'), PULL_CMD)); }
      if (bad) clearInterval(timer);
    }, 2000);
  }
  // a confirmation in the app's own dialog (no browser pop-up); resolves true on "yes", false on every other way out
  function askConfirm(title, body, yesLabel) {
    return new Promise(resolve => {
      let answered = false;
      const yes = h('button', { class: 'btn btn--primary', type: 'button', onclick: () => { answered = true; d.close(); resolve(true); } }, yesLabel);
      const no = h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => d.close() }, t('access.cancel'));
      const d = dialog(title, body, h('div', { class: 'btn-row' }, no, yes));
      const obs = new MutationObserver(() => { if (!d.el.isConnected) { obs.disconnect(); if (!answered) resolve(false); } });
      obs.observe(document.body, { childList: true });
      setTimeout(() => no.focus(), 30);
    });
  }

  // ------------------------------------------------------------------ what's new (changelog of the installed image)
  let wn = null, wnLoaded = false;
  const wnDots = new Set();
  const wnSync = () => { wnDots.forEach(d => { if (!d.isConnected && d.dataset.ready) { wnDots.delete(d); return; } d.dataset.ready = '1'; d.hidden = !(wn && wn.unseen); }); };
  const wnDot = () => { const d = h('i', { class: 'wn-dot', 'aria-hidden': 'true', hidden: true }); wnDots.add(d); if (wnLoaded) wnSync(); return d; };
  async function wnLoad(limit) {
    try { wn = await api('/api/whatsnew' + (limit ? '?limit=' + limit : '')); } catch (e) { return null; }
    wnLoaded = true; wnSync(); return wn;
  }
  // **bold**, *italic* and `code` become elements; everything else stays a text node (never markup from the server)
  function miniMd(text) {
    const out = [];
    String(text == null ? '' : text).split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/).forEach(part => {
      if (!part) return;
      if (part.startsWith('**') && part.endsWith('**') && part.length > 4) out.push(h('strong', {}, part.slice(2, -2)));
      else if (part.startsWith('`') && part.endsWith('`') && part.length > 2) out.push(h('code', {}, part.slice(1, -1)));
      else if (part.startsWith('*') && part.endsWith('*') && part.length > 2) out.push(h('em', {}, part.slice(1, -1)));
      else out.push(document.createTextNode(part));
    });
    return out;
  }
  LL.miniMd = miniMd;
  function wnEntry(e) {
    const heads = [h('h3', { class: 'wn-entry__ver' }, `v${e.version}`)];
    if (e.date) heads.push(h('span', { class: 'wn-entry__date muted' }, e.date));
    if (e.current) heads.push(h('span', { class: 'chip chip--accent' }, t('whatsnew.installed')));
    if (e.new) heads.push(h('span', { class: 'wn-new' }, h('i', { class: 'wn-dot', 'aria-hidden': 'true' }), t('whatsnew.new')));
    return h('article', { class: 'wn-entry' + (e.new ? ' is-new' : '') + (e.current ? ' is-current' : '') }, h('header', { class: 'wn-entry__head' }, heads),
      (e.sections || []).map(sec => h('section', { class: 'wn-sec' },
        h('h4', { class: 'wn-sec__title' }, LL.strings['whatsnew.section.' + String(sec.title).toLowerCase()] ? t('whatsnew.section.' + String(sec.title).toLowerCase()) : sec.title),
        h('ul', { class: 'wn-sec__list' }, (sec.items || []).map(it => h('li', {}, miniMd(it)))))));
  }
  async function openWhatsNew() {
    const list = h('div', { class: 'wn-list' }, h('p', { class: 'muted' }, t('whatsnew.loading')));
    const olderBtn = h('button', { class: 'btn btn--ghost btn--sm wn-older', type: 'button', hidden: true }, t('whatsnew.older'));
    const draw = () => {
      if (!wn) { list.replaceChildren(h('p', { class: 'muted' }, t('whatsnew.failed'))); olderBtn.hidden = true; return; }
      list.replaceChildren(...(wn.entries.length ? wn.entries.map(wnEntry) : [h('p', { class: 'muted' }, t('whatsnew.empty'))]));
      olderBtn.hidden = !wn.more;
    };
    olderBtn.onclick = async () => { olderBtn.disabled = true; await wnLoad(200); olderBtn.disabled = false; draw(); };
    dialog(t('whatsnew.title'), h('div', { class: 'wn' }, h('p', { class: 'muted wn-source' }, t('whatsnew.source')), list, h('div', { class: 'wn-more' }, olderBtn)));
    const first = await wnLoad();
    draw();
    if (first && first.unseen && first.current) {
      try { const r = await api('/api/whatsnew/seen', { method: 'POST', body: { version: first.current } }); wn.unseen = !!r.unseen; wn.seen = r.seen; wnSync(); } catch (e) { /* the dot stays until the next try */ }
    }
  }
  LL.openWhatsNew = openWhatsNew;
  let wnAsked = false;
  const wnOnce = () => { if (!wnAsked) { wnAsked = true; wnLoad(); } };   // once per page load, only to know whether the dot is due
  const verText = () => (LL.state && LL.state.version) || '';
  LL.versionChip = () => {
    if (!verText() || !LL.state.authenticated) return null;
    wnOnce();
    return h('button', { class: 'ver-chip', type: 'button', title: t('whatsnew.title'), 'aria-label': `v${verText()}, ` + t('whatsnew.title'), onclick: openWhatsNew }, `v${verText()}`, wnDot());
  };
  LL.versionFoot = () => {
    if (!verText() || !LL.state.authenticated) return null;
    wnOnce();
    return h('footer', { class: 'ver-foot muted' }, `Lanloom ${verText()}`, h('span', { class: 'dot-sep' }, '·'),
      h('button', { class: 'ver-foot__link', type: 'button', onclick: openWhatsNew }, t('whatsnew.btn'), wnDot()));
  };
  // the page follows the server: after an update the live connection returns, and the open page loads the new files once
  let healthBusy = false;
  async function checkInstalled() {
    if (healthBusy || !LL.state || !LL.state.version) return;
    healthBusy = true;
    try {
      const r = await fetch(LL.url('/api/health'), { cache: 'no-store', credentials: 'same-origin' });
      const hv = await r.json();
      if (hv && hv.version && hv.version !== LL.state.version) {
        let done = null; try { done = sessionStorage.getItem('ll.reloaded'); } catch (e) { /* no storage */ }
        if (done !== hv.version) {
          try { sessionStorage.setItem('ll.reloaded', hv.version); } catch (e) { /* no storage */ }
          LL.toast(t('whatsnew.reloading', { v: hv.version }), 'ok');
          setTimeout(() => location.reload(), 1500);
        }
      }
    } catch (e) { /* the server is still restarting */ }
    healthBusy = false;
  }
  let wasOffline = false;
  LL.live.onStatus(st => { if (st === 'offline') wasOffline = true; else if (st === 'live' && wasOffline) { wasOffline = false; checkInstalled(); } });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkInstalled(); });

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
    // network filter (?net=): "wired", "net:<name>", "vlan:<n>" or "wireless:<ssid>" (what the tags of the rows say); a value nothing matches counts as "all"
    let net = LL.cleanDeviceNet ? LL.cleanDeviceNet(data.devices, LL.query.get('net') || '') : '';
    const netSel = LL.deviceNetSelect ? LL.deviceNetSelect(data.devices, net, v => { net = v; if (v) LL.query.set('net', v); else LL.query.delete('net'); LL.deepLink(here(null), true); render(); }) : null;
    const keepNet = d => !net || !LL.NetModes || LL.NetModes.matchNet(d, net);
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
      const rows = data.devices.filter(d => keep(d) && keepNet(d) && (showHidden.checked || !d.hidden) && (!s || [d.title, d.ip, d.mac, t('kind.' + d.kind), (d.hostnames || []).join(' '), d.services.join(' ')].join(' ').toLowerCase().includes(s)));
      list.replaceChildren(...rows.map(d => h('button', { class: 'device-row' + (d.hidden ? ' is-hidden' : ''), type: 'button', onclick: () => openRow(d.id) },
        LL.kindIcon(d.kind), h('span', { class: 'device-row__main' }, h('span', { class: 'device-row__title' }, d.is_self ? t('kind.self') : (d.title || t('kind.' + d.kind))), h('span', { class: 'device-row__line' }, h('span', { class: 'device-row__sub mono' }, d.ip || d.mac || ''), LL.deviceTags ? LL.deviceTags(d) : null)),
        h('span', { class: 'device-row__kind' }, t('kind.' + d.kind)), h('span', { class: 'device-row__svc' }, d.services.slice(0, 3).map(s => h('span', { class: 'll-badge' }, s))),
        h('span', { class: 'status-dot status-dot--' + d.status, title: t('board.status.' + d.status) }))));
      if (!rows.length) list.replaceChildren(h('div', { class: 'empty-note' }, t('devices.none')));
      count.textContent = t('devices.count', { n: rows.length, total: data.devices.length });
    };
    const count = h('span', { class: 'muted' });
    // catalogue switch: the list of devices, the stability of each over 35 days (js/history.js), the web interfaces with their probe (js/services.js)
    const MODES = [['list', 'stab.mode.list']].concat(LL.stabilityPanel ? [['stability', 'stab.mode.stability']] : [], LL.linksPanel ? [['links', 'svc.mode']] : []);
    const wanted = LL.query.get('view');
    let mode = MODES.some(m => m[0] === wanted) ? wanted : 'list';
    let stab = null, links = null;
    const modeSwitch = MODES.length > 1 ? h('div', { class: 'seg dev-mode', role: 'group', 'aria-label': t('stab.switch') }) : null;
    function drawMode() {
      if (!modeSwitch) return;
      modeSwitch.replaceChildren(...MODES.map(([k, label]) => h('button', {
        type: 'button', class: 'seg__btn' + (k === mode ? ' is-active' : ''), 'aria-pressed': k === mode ? 'true' : 'false',
        onclick: () => { mode = k; LL.deepLink('#/devices' + (k === 'list' ? '' : '?view=' + k)); drawMode(); render(); } }, t(label))));
    }
    const baseRender = render;
    render = function () {
      showHidden.closest('label').hidden = mode !== 'list';
      if (netSel) netSel.hidden = mode !== 'list';
      if (mode === 'stability') {
        if (!stab) stab = LL.stabilityPanel({ filter: () => q.value.toLowerCase().trim(), onOpen: id => openRow(id) });
        else stab.redraw();
        list.replaceChildren(stab);
        count.textContent = '';
        return;
      }
      if (mode === 'links') {
        if (!links) links = LL.linksPanel({ filter: () => q.value.toLowerCase().trim(), onOpen: id => openRow(id) });
        else links.redraw();
        list.replaceChildren(links);
        count.textContent = '';
        return;
      }
      baseRender();
    };
    q.addEventListener('input', () => render()); showHidden.addEventListener('change', () => render());
    fr.main.append(h('div', { class: 'page page--wide' }, h('div', { class: 'page__head inv-head' }, h('h1', {}, t('nav.devices')), count, LL.inventorySwitch ? LL.inventorySwitch('devices') : null),
      h('div', { class: 'toolbar' }, q, netSel, modeSwitch, h('label', { class: 'check' }, showHidden, h('span', {}, t('devices.show_hidden'))),
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
    ['storage', 'storage', 'storage.tab', 'admin'], ['network', 'network', 'settings.tab.network', 'viewer'], ['agents', 'agents', 'agents.tab', 'viewer'], ['api', 'api', 'api.tab', 'viewer'], ['ai', 'ai', 'ai.tab', 'admin'],
    ['sites', 'sites', 'nav.sites', 'admin']];   // the sites have a page of their own (#/sites, M1)
  LL.settingsPages = SETTINGS_PAGES;      // the lowest role of each page also decides whether a remembered deep link may lead there (app.js)
  function settingsSubnav(active) {
    const homeOnly = p => p[0] !== 'ai' || !LL.atHome || LL.atHome();        // the AI assistant works on the home site
    const allowed = p => homeOnly(p) && (p[3] === 'viewer' || LL.can(p[3] === 'admin' ? 'admin' : 'operate'));
    return h('nav', { class: 'seg settings-subnav', 'aria-label': t('settings.sections') }, SETTINGS_PAGES.filter(allowed).map(([k, sub, label]) =>
      h('a', { href: k === 'sites' ? '#/sites' : '#/settings' + (sub ? '/' + sub : ''), class: 'seg__btn' + (k === active ? ' is-active' : ''), 'aria-current': k === active ? 'page' : null }, t(label))));
  }
  LL.settingsSubnav = settingsSubnav;
  async function settingsView(app, rest) {
    const fr = frame(app, 'settings');
    const sub = rest && rest[0];
    const subPage = { ai: LL.can('admin') && LL.aiSettingsPage, account: LL.accountPage, notifications: LL.notifyPage, users: LL.can('admin') && LL.usersPage, security: LL.can('admin') && LL.securityPage, backup: LL.can('admin') && LL.backupPage, storage: LL.can('admin') && LL.storagePage, network: LL.networkPage, agents: LL.agentsPage, api: LL.apiPage }[sub];
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
    const lastText = l => l ? t('settings.scan.last', { when: LL.fmtDate(l.started), n: l.hosts, mode: t('scan.mode.' + l.mode) }) : t('settings.scan.never');
    const lastLine = h('p', { class: 'muted' }, lastText(scan.last));
    // while a scan runs (started here, by the wizard or by the automatic rescan) a bar fills left of the button and the
    // button cancels it; afterwards it starts a scan again
    const scanBar = h('div', { class: 'progress__bar' });
    const scanMeter = h('div', { class: 'progress scan-run__bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0',
      'aria-label': t('settings.scan.title'), hidden: true }, scanBar);
    const scanBtn = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      if (scanWatch.running) {
        scanBtn.disabled = true;
        try { await api('/api/scan', { method: 'DELETE' }); } catch (e) { scanBtn.disabled = false; LL.msg(scanMsg, LL.errText(e), 'error'); }
        return;
      }
      LL.msg(scanMsg, '');
      try { const cur = await api('/api/scan', { method: 'POST', body: { cidrs: [...cidrs], deep: deep.checked, consent: consent.checked, snmp_public: snmpPub.checked } }); LL.msg(scanMsg, t('settings.scan.started'), 'ok'); scanWatch.start(cur); }
      catch (e) {
        LL.msg(scanMsg, LL.errText(e), 'error');
        api('/api/scan').then(r => { if (r.current && r.current.state === 'running' && !scanWatch.running) scanWatch.start(r.current); }).catch(() => {});   // started elsewhere meanwhile
      }
    } }, t('settings.scan.now'));
    const scanWatch = watchScan({ msg: scanMsg, bar: scanBar, meter: scanMeter, btn: scanBtn,
      onEnd: () => api('/api/scan').then(r => { lastLine.textContent = lastText(r.last); }).catch(() => {}) });
    const rescan = h('select', { class: 'select', onchange: () => api('/api/settings', { method: 'PUT', body: { rescan_hours: Number(rescan.value) } }).then(() => LL.toast(t('settings.saved'), 'ok')) },
      [0, 1, 3, 6, 12, 24].map(n => h('option', { value: String(n) }, n ? t('settings.rescan.every', { n }) : t('settings.rescan.never'))));
    rescan.value = String(s.rescan_hours);
    const svcProbe = h('input', { type: 'checkbox', checked: s.service_probe !== false, onchange: () => api('/api/settings', { method: 'PUT', body: { service_probe: svcProbe.checked } }).then(() => LL.toast(t('settings.saved'), 'ok')) });
    page.append(card(t('settings.scan.title'), [
      lastLine,
      netBox,
      h('label', { class: 'check' }, deep, h('span', {}, t('wizard.scan.deep'))), h('label', { class: 'check check--consent' }, consent, h('span', {}, t('wizard.scan.consent'))),
      h('label', { class: 'check' }, snmpPub, h('span', {}, t('scan.snmp_public'))), h('p', { class: 'muted fineprint' }, t('scan.snmp_public.hint')),
      scanMsg, h('div', { class: 'btn-row scan-run' }, scanMeter, scanBtn),
      h('hr'), LL.field(t('settings.rescan'), rescan, t('settings.rescan.hint')),
      h('label', { class: 'check' }, svcProbe, h('span', {}, t('settings.service_probe'))), h('p', { class: 'muted fineprint' }, t('settings.service_probe.hint'))]));
    if (scan.current && scan.current.state === 'running') scanWatch.start(scan.current);
    const frDestroy = fr.destroy;
    fr.destroy = () => { scanWatch.stop(); if (frDestroy) frDestroy.call(fr); };
    // integrations
    const integWrap = h('div', { class: 'stack' });
    async function renderInteg() {
      const list = (await LL.loadIntegrations()).integrations;
      integWrap.replaceChildren(...list.map(i => LL.integrationCard(i.kind, i, renderInteg)),
        h('div', { class: 'btn-row btn-row--wrap' }, LL.integKinds().map(k => h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => integWrap.insertBefore(LL.integrationCard(k, null, renderInteg), integWrap.lastChild) },
          LL.builtinKinds.includes(k) ? t('integ.add.' + k) : t('integ.add.family', { name: LL.integTitle(k) })))));
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
    // background noise (broadcast, multicast) out of the exact counters read at switch ports; off by default
    const noise = h('input', { type: 'checkbox', checked: !!s.port_noise, onchange: async () => {
      try { await api('/api/settings', { method: 'PUT', body: { port_noise: noise.checked } }); LL.toast(t('settings.saved'), 'ok'); }
      catch (e) { noise.checked = !noise.checked; LL.toast(LL.errText(e), 'error'); } } });
    page.append(card(t('settings.appearance'), [h('div', { class: 'grid-2' }, LL.field(t('settings.language'), lang), LL.field(t('settings.theme'), theme)),
      paletteField(), h('div', { class: 'grid-2' }, LL.field(t('settings.rate_unit'), unit, t('settings.rate_unit.hint')), motionField()),
      h('div', { class: 'stack-sm' }, h('label', { class: 'check' }, noise, h('span', {}, t('settings.port_noise'))), h('p', { class: 'field__hint' }, t('settings.port_noise.hint')))]));
    if (LL.pathsSettingsCard) page.append(LL.pathsSettingsCard(s));
    if (LL.pathsDetailSettingsCard) page.append(LL.pathsDetailSettingsCard(s));
    const routesCard = LL.pathsRoutesSettingsCard ? LL.pathsRoutesSettingsCard(s) : null;
    if (routesCard) page.append(routesCard);
    if (LL.netflowSettingsCard) page.append(LL.netflowSettingsCard(s));
    const mqttCard = LL.mqttOutCard ? LL.mqttOutCard() : null;
    if (mqttCard) page.append(mqttCard);
    // updates + about
    const updToggle = h('input', { type: 'checkbox', checked: !!s.update_check, onchange: () => api('/api/settings', { method: 'PUT', body: { update_check: updToggle.checked } }).then(() => LL.toast(t('settings.saved'), 'ok')) });
    const updMsg = h('div', { class: 'msg' });
    const reportCard = LL.reportSettingsCard ? await LL.reportSettingsCard() : null;
    if (reportCard) page.append(reportCard);
    setUpdateState(upd);
    const updCard = u => {
      const sig = u.signature || {};
      const flash = (text, kind) => LL.msg(updMsg, text, kind);
      const redraw = nu => { setUpdateState(nu); updSection.replaceWith(updCard(nu)); };
      const put = async body => { try { redraw(await api('/api/update/settings', { method: 'PUT', body })); LL.toast(t('settings.saved'), 'ok'); } catch (e) { flash(LL.errText(e), 'error'); } };
      const channel = h('select', { class: 'input', 'aria-label': t('update.channel') }, ['stable', 'preview'].map(c => h('option', { value: c }, t('update.channel.' + c))));
      channel.value = u.channel === 'preview' ? 'preview' : 'stable';
      channel.onchange = () => put({ channel: channel.value });
      const hold = h('input', { type: 'checkbox', checked: !!u.held });
      hold.onchange = () => put({ hold: hold.checked });
      const versionBtn = h('button', { class: 'link-btn', type: 'button', title: t('whatsnew.title'), onclick: openWhatsNew }, u.current);
      const prev = u.previous;
      const rollback = prev ? h('button', { class: 'btn btn--ghost', type: 'button', disabled: !u.rollback_ok, onclick: async () => {
        if (!await askConfirm(t('update.rollback.title', { v: prev.version }), h('div', { class: 'stack' }, h('p', {}, t('update.rollback.text'))), t('update.rollback.confirm'))) return;
        rollback.disabled = true;
        try { const req = await api('/api/update/rollback', { method: 'POST', body: { version: prev.version } }); flash(t('update.rollback.started', { v: prev.version }), 'info'); followUpdate(updMsg, prev.version, req.nonce); }
        catch (e) { flash(LL.errText(e), 'error'); rollback.disabled = !u.rollback_ok; }
      } }, t('update.rollback.btn', { v: prev.version })) : null;
      if (u.mode === 'addon') {
        // add-on of a home-automation hub: its add-on system shows and installs new versions; no feed, no sidecar here
        return updSection = h('section', { class: 'card settings-card update-card' }, h('h2', {}, t('settings.updates')),
          facts([[t('settings.version'), versionBtn], [t('update.addon.source'), t('update.addon.source.value')]]),
          h('p', { class: 'upd-addon' }, t('update.addon.text')),
          h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost', type: 'button', onclick: openWhatsNew }, t('whatsnew.btn'))));
      }
      return updSection = h('section', { class: 'card settings-card update-card' }, h('h2', {}, t('settings.updates')),
        facts([[t('settings.version'), versionBtn], [t('settings.latest'), u.latest || '—'], [t('settings.channel'), t('update.channel.' + (u.channel === 'preview' ? 'preview' : 'stable'))],
          [t('settings.checked'), u.checked ? LL.fmtDate(u.checked) : '—'], [t('settings.sidecar'), u.sidecar ? t('settings.sidecar.yes') : t('settings.sidecar.no')],
          u.sidecar ? [t('settings.signature'), sig.supported ? t(sig.enforced ? 'settings.signature.enforced' : 'settings.signature.yes') : t('settings.signature.no')] : null]),
        u.sidecar_old || (u.sidecar && sig.supported === false) ? cmdCallout(t(sig.supported === false ? 'update.signature.unsupported' : 'update.sidecar_old'), SIDECAR_CMD) : null,
        h('div', { class: 'upd-group' }, LL.field(t('update.channel'), channel, t('update.channel.hint')), u.preview_none ? h('p', { class: 'field__hint upd-none' }, t('update.channel.none')) : null),
        h('div', { class: 'upd-group' }, h('label', { class: 'check' }, hold, h('span', {}, t('update.hold'))), h('p', { class: 'field__hint' }, t('update.hold.hint')),
          u.held ? h('p', { class: 'upd-held' + (u.held_newer ? ' is-newer' : '') }, u.held_newer ? t('update.held_newer', { v: u.held, n: u.held_newer }) : t('update.held', { v: u.held })) : null),
        prev ? h('div', { class: 'upd-group' }, rollback, h('p', { class: 'field__hint' }, t(!u.sidecar ? 'update.rollback.no_sidecar' : u.rollback_unsigned ? 'update.rollback.unsigned' : 'update.rollback.hint'))) : null,
        h('label', { class: 'check' }, updToggle, h('span', {}, t('settings.update_check'))), h('p', { class: 'field__hint' }, t('settings.update_check.hint')), updMsg,
        h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
          try { const nu = await api('/api/update/check', { method: 'POST', body: {} }); setUpdateState(nu); flash(nu.available ? t('update.available', { v: nu.latest }) : (nu.error ? t('update.check_failed') : t('update.current')), nu.available ? 'ok' : 'info'); if (nu.available) openUpdate(); else updSection.replaceWith(updCard(nu)); }
          catch (e) { flash(LL.errText(e), 'error'); }
        } }, t('update.check_now')), h('button', { class: 'btn btn--ghost', type: 'button', onclick: openWhatsNew }, t('whatsnew.btn'))));
    };
    let updSection = null;
    page.append(updCard(upd));
    page.append(diagnosticsCard());
    page.append(card(t('settings.about'), [h('p', {}, t('settings.about.text')), h('p', { class: 'muted' }, t('settings.about.privacy'))]));
    fr.main.append(page);
    if (rest && rest[0] === 'reports' && reportCard) setTimeout(() => reportCard.scrollIntoView({ block: 'start' }), 30);
    if (rest && rest[0] === 'mqtt' && mqttCard) setTimeout(() => mqttCard.scrollIntoView({ block: 'start' }), 400);
    if (rest && rest[0] === 'routes' && routesCard) setTimeout(() => routesCard.scrollIntoView({ block: 'start' }), 400);
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
  // Settings > General > Network scan: polls GET /api/scan while a scan runs, fills the bar (never backwards, see
  // LL.scanMeter), turns the button into Cancel and back; stop() ends the polling when the page is left
  function watchScan({ msg, bar, meter, btn, onEnd }) {
    const w = { running: false, timer: null };
    let pct = null;
    function show(on) {
      w.running = on;
      meter.hidden = !on;
      btn.disabled = false;
      btn.className = 'btn ' + (on ? 'btn--ghost' : 'btn--primary');
      btn.textContent = t(on ? 'wizard.scan.cancel' : 'settings.scan.now');
    }
    function paint(s) {
      const p = pct(s);
      bar.style.width = Math.max(2, p) + '%';
      meter.setAttribute('aria-valuenow', String(p));
      return p;
    }
    async function poll() {
      let s; try { s = (await api('/api/scan')).current; } catch (e) { return; }
      if (!w.running || !s) return;
      if (s.state === 'running') {
        const p = paint(s);
        LL.msg(msg, s.total ? t('settings.scan.progress_n', { phase: t('scan.phase.' + s.phase), done: s.done || 0, total: s.total, found: s.found || 0, pct: p })
          : t('settings.scan.progress', { phase: t('scan.phase.' + s.phase), found: s.found || 0 }), 'info');
        return;
      }
      w.stop();
      if (s.state === 'done') paint(s);
      show(false);
      LL.msg(msg, s.state === 'done' ? t('settings.scan.done', { n: s.found }) : t(s.error || 'wizard.scan.stopped'), s.state === 'done' ? 'ok' : 'error');
      if (onEnd) onEnd(s);
    }
    w.start = cur => {
      w.stop();
      pct = LL.scanMeter();
      bar.style.width = '2%';
      show(true);
      if (cur && cur.state === 'running') paint(cur);
      w.timer = setInterval(poll, 1000);
    };
    w.stop = () => { if (w.timer) clearInterval(w.timer); w.timer = null; };
    return w;
  }

  LL.frame = frame;
  LL.liveSubscribe = liveSubscribe;
  LL.dialog = dialog;
  LL.view('map', mapView('overview'));
  LL.view('network', mapView('network'));
  LL.view('devices', devicesView);
  LL.view('settings', settingsView);
})();
