/* Lanloom — main views: map (overview / network), devices, settings, detail panel, live feed, update notice. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const GRADES = ['realtime', 'near', 'periodic', 'sluggish', 'interrupted', 'unknown'];

  // ------------------------------------------------------------------ live feed (one socket for all views)
  const live = { ws: null, listeners: new Set(), last: null, retry: 1000, timer: null };
  function liveConnect() {
    if (live.ws && live.ws.readyState <= 1) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = live.ws = new WebSocket(proto + '//' + location.host + LL.url('/api/live'));
    ws.onmessage = ev => { let f; try { f = JSON.parse(ev.data); } catch (e) { return; } live.last = f; live.retry = 1000; live.listeners.forEach(fn => fn(f)); };
    ws.onclose = () => { live.ws = null; if (live.listeners.size) { clearTimeout(live.timer); live.timer = setTimeout(liveConnect, live.retry); live.retry = Math.min(15000, live.retry * 2); } };
  }
  function liveSubscribe(fn) { live.listeners.add(fn); liveConnect(); if (live.last) fn(live.last); return () => live.listeners.delete(fn); }

  // ------------------------------------------------------------------ app frame (header + nav)
  function frame(app, active, content) {
    const tabs = [['map', 'nav.map'], ['network', 'nav.network'], ['paths', 'nav.paths'], ['devices', 'nav.devices'], ['firewall', 'nav.firewall'], ['reports', 'nav.reports'], ['settings', 'nav.settings']];
    const nav = h('nav', { class: 'tabs', 'aria-label': t('nav.label') }, tabs.map(([k, label]) =>
      h('a', { href: '#/' + k, class: 'tab tab--' + k + (k === active ? ' is-active' : '') + (LL.strings['nav.short.' + k] ? ' tab--short' : ''), 'aria-current': k === active ? 'page' : null },
        h('span', { class: 'tab__icon', 'aria-hidden': 'true' }), h('span', { class: 'tab__label' }, t(label)),
        LL.strings['nav.short.' + k] ? h('span', { class: 'tab__short', 'aria-hidden': 'true' }, t('nav.short.' + k)) : null)));
    const navIcon = k => NAV_ICONS[k] || ({ paths: LL.NAV_PATHS_ICON, reports: LL.NAV_REPORTS_ICON }[k]) || LL.FW_ICON || '';
    nav.querySelectorAll('.tab__icon').forEach((el, i) => { el.innerHTML = navIcon(tabs[i][0]); });
    const gradePill = h('button', { class: 'grade-pill', type: 'button', title: t('live.overall'), onclick: () => openSources() },
      h('i', { class: 'grade-pill__dot' }), h('span', { class: 'grade-pill__text' }, t('board.grade.unknown')));
    const updateBtn = h('button', { class: 'update-pill', type: 'button', hidden: true, onclick: () => openUpdate() }, t('update.available_short'));
    const themeBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('theme.toggle'), title: t('theme.toggle'), onclick: () => {
      const order = ['auto', 'light', 'dark']; const next = order[(order.indexOf(LL.theme()) + 1) % 3]; LL.setTheme(next); LL.toast(t('theme.' + next));
    } });
    themeBtn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/></svg>';
    const header = h('header', { class: 'topbar' },
      h('a', { class: 'topbar__brand', href: '#/map' }, LL.logo(26), h('span', { class: 'brand-name' }, 'Lanloom'),
        LL.state && LL.state.demo ? h('span', { class: 'chip chip--demo' }, t('app.demo')) : null),
      nav, h('div', { class: 'topbar__right' }, updateBtn, gradePill, themeBtn));
    const main = h('main', { class: 'main main--' + active }, content);
    const notes = LL.noticeBar ? LL.noticeBar() : null;              // security notices (new device, lock-out ...)
    app.replaceChildren(h('div', { class: 'frame' }, header, notes, main));
    const unsub = liveSubscribe(f => {
      const g = (f.overall && f.overall.grade) || 'unknown';
      gradePill.dataset.grade = g;
      gradePill.lastChild.textContent = t('board.grade.' + g);
    });
    if (LL.can('admin')) checkUpdate(updateBtn);
    if (LL.setReportsUnread) LL.setReportsUnread((LL.state && LL.state.reports_unread) || 0);
    if (LL.state && LL.state.authenticated && LL.can('operate') && !LL.state.report_tz_auto && !frame.tzSent) {
      frame.tzSent = true;                 // the browser's time zone is the automatic report zone (when the container has none)
      try { api('/api/reports/tz', { method: 'POST', body: { tz: Intl.DateTimeFormat().resolvedOptions().timeZone || '' } }).catch(() => {}); } catch (e) { /* ignore */ }
    }
    return { main, destroy: unsub };
  }
  const NAV_ICONS = {
    map: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M10 6.5h4.5v7.5M6.5 10v7.5H14"/></svg>',
    network: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="5" rx="1"/><rect x="2.5" y="16.5" width="6" height="5" rx="1"/><rect x="15.5" y="16.5" width="6" height="5" rx="1"/><path d="M12 7.5v4.5M5.5 16.5V12h13v4.5"/></svg>',
    devices: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1.2"/><circle cx="3.5" cy="12" r="1.2"/><circle cx="3.5" cy="18" r="1.2"/></svg>',
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
    return async function (app) {
      const fr = frame(app, kind === 'network' ? 'network' : 'map');
      const host = h('div', { class: 'board-host' });
      const panel = h('aside', { class: 'detail', hidden: true, 'aria-label': t('detail.label') });
      fr.main.append(host, panel);
      let data = await api('/api/map?view=' + (kind === 'network' ? 'network' : 'overview'));
      if (!data.components.length) {
        host.replaceChildren(emptyState());
        return fr;
      }
      let selected = null;
      const board = LL.Board.mount(host, {
        view: localise(data), theme: LL.effectiveTheme(), t: LL.t, locale: LL.locale(),
        onSelect: id => { selected = id; board.highlight(id); openDetail(panel, id, () => { selected = null; board.highlight(null); }); }
      });
      const onTheme = ev => board.setTheme(ev.detail);
      document.addEventListener('ll-theme', onTheme);
      let structure = data.structure;
      let refreshing = false;
      const unsub = liveSubscribe(async f => {
        board.live(f);
        if (f.structure !== structure && !refreshing) {
          refreshing = true;
          try { data = await api('/api/map?view=' + (kind === 'network' ? 'network' : 'overview')); structure = data.structure; board.update(localise(data)); if (selected) board.highlight(selected); }
          catch (e) { /* keep the old board */ }
          refreshing = false;
        }
      });
      const poll = setInterval(async () => {   // statuses and new flow traces without a structural change
        try { const d = await api('/api/map?view=' + (kind === 'network' ? 'network' : 'overview')); data = d; structure = d.structure; board.update(localise(d)); if (selected) board.highlight(selected); } catch (e) { /* ignore */ }
      }, 60000);
      return { destroy: () => { unsub(); clearInterval(poll); document.removeEventListener('ll-theme', onTheme); board.destroy(); fr.destroy(); } };
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
    if (d.series && d.series.length) {
      sections.push(section(t('detail.traffic'), h('div', { class: 'spark-wrap' }, chart, chartLegend)));
    }
    if (d.hypervisor) sections.push(LL.hvHostSection(d.hypervisor));
    if (d.guest) sections.push(LL.hvGuestSection(d.guest));
    const devSec = d.device ? deviceSection(d.device, d) : null;
    const foot = devSec && devSec.querySelector(':scope > .detail__foot');   // "Hide from the map" stays the last button of the card
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
    if (d.entities) sections.push(entitiesSection(d.entities));
    if (d.ifaces && Object.keys(d.ifaces).length) sections.push(section(t('detail.ifaces'), facts(Object.entries(d.ifaces).map(([n, v]) => [n, '↑ ' + LL.fmtRate(v[0]) + '  ↓ ' + LL.fmtRate(v[1])]))));
    sections.push(section(t('detail.links'), h('ul', { class: 'link-list' }, d.traces.length ? d.traces.map(tr => h('li', {},
      h('span', { class: 'trace-sw trace-sw--' + tr.kind }), t('board.trace.' + tr.kind), h('span', { class: 'muted' }, ' · ' + (tr.measured ? t('detail.measured') : t('detail.unmeasured'))))) : [h('li', { class: 'muted' }, t('detail.no_links'))])));
    if (foot) sections.push(foot);
    panel.replaceChildren(h('div', { class: 'detail__head' }, LL.kindIcon(c.kind), h('div', { class: 'detail__titles' }, h('h2', {}, c.title || '—'), h('div', { class: 'muted mono' }, c.subtitle || t('kind.' + c.kind))), close),
      h('div', { class: 'detail__chips' }, h('span', { class: 'chip' }, t('kind.' + c.kind)), statusEl, gradeEl,
        d.device && LL.aiExplainButton ? LL.aiExplainButton('device', { id: d.device.id }, { title: c.title }) : null),
      h('div', { class: 'detail__body' }, sections));
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
    });
    const refresher = setInterval(async () => {
      if (panel.hidden) { clearInterval(refresher); return; }
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
      return t('reason.' + kind, { v });
    };
    const reasons = (dev.reasons || []).map(reasonText).join(' · ');
    const rows = [[t('device.name'), name], [t('device.kind'), kindSel], [t('device.certainty'), LL.certaintyChip(dev.certainty)], [t('device.why'), reasons || '—'],
      [t('device.ip'), h('span', { class: 'mono' }, dev.ip)], [t('device.mac'), dev.mac ? h('span', { class: 'mono' }, dev.mac) : null],
      [t('device.hostnames'), dev.hostnames && dev.hostnames.length ? dev.hostnames.join(', ') : null],
      [t('device.found_by'), (dev.sources || []).map(s => t('source.kind.' + s)).join(', ')],
      [t('device.first_seen'), LL.fmtDate(dev.first_seen)], [t('device.last_seen'), LL.fmtDate(dev.last_seen)],
      [t('device.presence'), d.presence_age_s != null ? t('device.presence_ago', { age: LL.fmtAge(d.presence_age_s) }) : '—']];
    const ports = dev.ports && dev.ports.length ? h('table', { class: 'mini-table' }, h('thead', {}, h('tr', {}, h('th', {}, t('device.port')), h('th', {}, t('device.service')), h('th', {}, t('device.banner')))),
      h('tbody', {}, dev.ports.map(p => h('tr', {}, h('td', { class: 'mono' }, String(p.port)), h('td', {}, p.service || ''), h('td', { class: 'truncate' }, (p.banner && (p.banner.title || p.banner.server)) || ''))))) : null;
    const ann = [];
    (dev.mdns || []).forEach(m => ann.push(h('li', {}, h('span', { class: 'mono' }, m.type), ' — ', m.name || '')));
    (dev.ssdp || []).forEach(s => ann.push(h('li', {}, h('span', { class: 'mono' }, (s.device_type || s.st || '').replace(/^urn:/, '')), ' — ', [s.friendly_name, s.model].filter(Boolean).join(' · '))));
    const hide = !may ? null : h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => { await api('/api/devices/' + encodeURIComponent(dev.id), { method: 'PATCH', body: { hidden: !dev.hidden } }); LL.toast(t('device.saved'), 'ok'); LL.route(); } }, dev.hidden ? t('device.unhide') : t('device.hide'));
    return h('div', {}, section(t('detail.device'), facts(rows)), ports ? section(t('device.ports'), ports) : null,
      ann.length ? section(t('device.announcements'), h('ul', { class: 'plain-list' }, ann)) : null, hide ? h('div', { class: 'detail__foot' }, hide) : null);
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

  function openSources() {
    const f = live.last || { overall: { sources: [] } };
    const rows = (f.overall.sources || []).map(s => h('tr', {}, h('td', {}, s.kind ? s.name : t(s.name)), h('td', {}, h('span', { class: 'grade-chip', dataset: { grade: s.grade } }, h('i'), t('board.grade.' + s.grade))),
      h('td', { class: 'num' }, s.cadence_s != null ? LL.fmtAge(s.cadence_s) : '—'), h('td', { class: 'num' }, s.age_s != null ? LL.fmtAge(s.age_s) : '—')));
    dialog(t('live.title'), h('div', {}, h('p', { class: 'muted' }, t('live.explain')),
      h('table', { class: 'mini-table' }, h('thead', {}, h('tr', {}, h('th', {}, t('live.source')), h('th', {}, t('live.grade')), h('th', { class: 'num' }, t('live.cadence')), h('th', { class: 'num' }, t('live.age')))),
        h('tbody', {}, rows.length ? rows : [h('tr', {}, h('td', { colspan: '4', class: 'muted' }, t('live.none')))])),
      h('ul', { class: 'grade-legend' }, GRADES.map(g => h('li', {}, h('span', { class: 'grade-chip', dataset: { grade: g } }, h('i'), t('board.grade.' + g)), ' ', t('live.def.' + g))))));
  }

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
    dialog(t('update.title', { v: u.latest }), h('div', { class: 'stack' }, h('p', {}, t('update.lead', { cur: u.current, v: u.latest })), notes, u.sidecar ? h('p', { class: 'muted' }, t('update.sidecar_text')) : manual, msg),
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
  async function devicesView(app) {
    const fr = frame(app, 'devices');
    const data = await api('/api/devices');
    const q = h('input', { type: 'search', class: 'input', placeholder: t('devices.search'), 'aria-label': t('devices.search') });
    const showHidden = h('input', { type: 'checkbox' });
    const list = h('div', { class: 'device-list' });
    const panel = h('aside', { class: 'detail', hidden: true, 'aria-label': t('detail.label') });
    function render() {
      const s = q.value.toLowerCase();
      const rows = data.devices.filter(d => (showHidden.checked || !d.hidden) && (!s || [d.title, d.ip, d.mac, t('kind.' + d.kind), (d.hostnames || []).join(' '), d.services.join(' ')].join(' ').toLowerCase().includes(s)));
      list.replaceChildren(...rows.map(d => h('button', { class: 'device-row' + (d.hidden ? ' is-hidden' : ''), type: 'button', onclick: () => openDetail(panel, d.id) },
        LL.kindIcon(d.kind), h('span', { class: 'device-row__main' }, h('span', { class: 'device-row__title' }, d.is_self ? t('kind.self') : (d.title || t('kind.' + d.kind))), h('span', { class: 'device-row__sub mono' }, d.ip)),
        h('span', { class: 'device-row__kind' }, t('kind.' + d.kind)), h('span', { class: 'device-row__svc' }, d.services.slice(0, 3).map(s => h('span', { class: 'll-badge' }, s))),
        h('span', { class: 'status-dot status-dot--' + d.status, title: t('board.status.' + d.status) }))));
      if (!rows.length) list.replaceChildren(h('div', { class: 'empty-note' }, t('devices.none')));
      count.textContent = t('devices.count', { n: rows.length, total: data.devices.length });
    }
    const count = h('span', { class: 'muted' });
    q.addEventListener('input', render); showHidden.addEventListener('change', render);
    fr.main.append(h('div', { class: 'page' }, h('div', { class: 'page__head' }, h('h1', {}, t('nav.devices')), count),
      h('div', { class: 'toolbar' }, q, h('label', { class: 'check' }, showHidden, h('span', {}, t('devices.show_hidden')))), list), panel);
    render();
    return fr;
  }

  // ------------------------------------------------------------------ settings
  // sub-pages of the settings; each names the lowest role that sees it (the server enforces the same per route)
  const SETTINGS_PAGES = [['general', '', 'settings.general', 'viewer'], ['account', 'account', 'settings.tab.account', 'viewer'],
    ['users', 'users', 'settings.tab.users', 'admin'], ['security', 'security', 'settings.tab.security', 'admin'], ['ai', 'ai', 'ai.tab', 'admin']];
  function settingsSubnav(active) {
    const allowed = p => p[3] === 'viewer' || LL.can(p[3] === 'admin' ? 'admin' : 'operate');
    return h('nav', { class: 'seg settings-subnav', 'aria-label': t('settings.sections') }, SETTINGS_PAGES.filter(allowed).map(([k, sub, label]) =>
      h('a', { href: '#/settings' + (sub ? '/' + sub : ''), class: 'seg__btn' + (k === active ? ' is-active' : ''), 'aria-current': k === active ? 'page' : null }, t(label))));
  }
  LL.settingsSubnav = settingsSubnav;
  async function settingsView(app, rest) {
    const fr = frame(app, 'settings');
    const sub = rest && rest[0];
    const subPage = { ai: LL.can('admin') && LL.aiSettingsPage, account: LL.accountPage, users: LL.can('admin') && LL.usersPage, security: LL.can('admin') && LL.securityPage }[sub];
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
    const scanMsg = h('div', { class: 'msg' });
    const last = scan.last;
    const scanBtn = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      LL.msg(scanMsg, '');
      try { await api('/api/scan', { method: 'POST', body: { cidrs: [...cidrs], deep: deep.checked, consent: consent.checked } }); LL.msg(scanMsg, t('settings.scan.started'), 'ok'); watchScan(scanMsg); }
      catch (e) { LL.msg(scanMsg, LL.errText(e), 'error'); }
    } }, t('settings.scan.now'));
    const rescan = h('select', { class: 'select', onchange: () => api('/api/settings', { method: 'PUT', body: { rescan_hours: Number(rescan.value) } }).then(() => LL.toast(t('settings.saved'), 'ok')) },
      [0, 1, 3, 6, 12, 24].map(n => h('option', { value: String(n) }, n ? t('settings.rescan.every', { n }) : t('settings.rescan.never'))));
    rescan.value = String(s.rescan_hours);
    page.append(card(t('settings.scan.title'), [
      h('p', { class: 'muted' }, last ? t('settings.scan.last', { when: LL.fmtDate(last.started), n: last.hosts, mode: t('scan.mode.' + last.mode) }) : t('settings.scan.never')),
      netBox,
      h('label', { class: 'check' }, deep, h('span', {}, t('wizard.scan.deep'))), h('label', { class: 'check check--consent' }, consent, h('span', {}, t('wizard.scan.consent'))),
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
    renderInteg();
    // appearance
    const [lang, theme] = langThemeSelects();
    const unit = h('select', { class: 'select', onchange: async () => {
      await api('/api/settings', { method: 'PUT', body: { rate_unit: unit.value } }); LL.rateUnit = unit.value; LL.toast(t('settings.saved'), 'ok'); } },
      ['bytes', 'bits'].map(v => h('option', { value: v }, t('settings.rate_unit.' + v))));
    unit.value = s.rate_unit === 'bits' ? 'bits' : 'bytes';
    page.append(card(t('settings.appearance'), [h('div', { class: 'grid-2' }, LL.field(t('settings.language'), lang), LL.field(t('settings.theme'), theme)),
      paletteField(), LL.field(t('settings.rate_unit'), unit, t('settings.rate_unit.hint'))]));
    if (LL.pathsSettingsCard) page.append(LL.pathsSettingsCard(s));
    // updates + about
    const updToggle = h('input', { type: 'checkbox', checked: !!s.update_check, onchange: () => api('/api/settings', { method: 'PUT', body: { update_check: updToggle.checked } }).then(() => LL.toast(t('settings.saved'), 'ok')) });
    const updMsg = h('div', { class: 'msg' });
    const reportCard = LL.reportSettingsCard ? await LL.reportSettingsCard() : null;
    if (reportCard) page.append(reportCard);
    page.append(card(t('settings.updates'), [
      facts([[t('settings.version'), upd.current], [t('settings.latest'), upd.latest || '—'], [t('settings.checked'), upd.checked ? LL.fmtDate(upd.checked) : '—'],
        [t('settings.sidecar'), upd.sidecar ? t('settings.sidecar.yes') : t('settings.sidecar.no')]]),
      h('label', { class: 'check' }, updToggle, h('span', {}, t('settings.update_check'))), h('p', { class: 'field__hint' }, t('settings.update_check.hint')), updMsg,
      h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
        try { updateState = await api('/api/update/check', { method: 'POST', body: {} }); LL.msg(updMsg, updateState.available ? t('update.available', { v: updateState.latest }) : (updateState.error ? t('update.check_failed') : t('update.current')), updateState.available ? 'ok' : 'info'); if (updateState.available) openUpdate(); }
        catch (e) { LL.msg(updMsg, LL.errText(e), 'error'); }
      } }, t('update.check_now')))]));
    page.append(card(t('settings.about'), [h('p', {}, t('settings.about.text')), h('p', { class: 'muted' }, t('settings.about.privacy'))]));
    fr.main.append(page);
    if (rest && rest[0] === 'reports' && reportCard) setTimeout(() => reportCard.scrollIntoView({ block: 'start' }), 30);
    return fr;
  }
  function card(title, kids) { return h('section', { class: 'card settings-card' }, h('h2', {}, title), kids); }
  LL.settingsCard = card;
  LL.facts = facts;
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
    page.append(card(t('settings.appearance'), [h('div', { class: 'grid-2' }, LL.field(t('settings.language'), lang), LL.field(t('settings.theme'), theme)), paletteField()]),
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
  LL.dialog = dialog;
  LL.view('map', mapView('overview'));
  LL.view('network', mapView('network'));
  LL.view('devices', devicesView);
  LL.view('settings', settingsView);
})();
