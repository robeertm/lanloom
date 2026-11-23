/* Lanloom — Running view: what runs on each host that has a host agent or a container source. One card per host with the
   system (load, memory, disks, a 24 h trend), containers, listening services, timers, failed units and overlay VPN peers.
   Read only; the data is metadata that the agents report (GET /api/running). Plain DOM + one canvas per host. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const enc = encodeURIComponent;
  const REFRESH_MS = 10000;
  const HEALTH_MS = 60000;
  const FIRST_ROWS = 14;                 // containers shown before "show all"
  const ui = { filter: 'all', search: '', more: new Set() };
  const cache = {};                      // host id -> { at, points } of the 24 h health series

  // ------------------------------------------------------------------ small helpers
  const pct = (v, digits) => LL.fmtNum(v, digits == null ? 0 : digits) + ' %';
  const ratio = (a, b) => (b > 0 ? Math.max(0, Math.min(100, a / b * 100)) : 0);
  const level = p => (p >= 90 ? 'bad' : p >= 80 ? 'warn' : 'ok');
  function size(n) {                      // like LL.fmtBytes, plus terabytes for big disks
    n = Number(n) || 0;
    if (n >= 1e12) { const v = n / 1e12; return LL.fmtNum(v, v >= 10 ? 0 : 1) + ' TB'; }
    return LL.fmtBytes(n);
  }
  const ago = (now, ts) => (ts ? t('time.ago', { age: LL.fmtAge(now - ts) }) : t('running.never'));
  const lc = s => String(s == null ? '' : s).toLowerCase();

  // ------------------------------------------------------------------ problems and filtering
  const badContainer = c => c.state === 'restarting' || c.state === 'dead' || c.health === 'unhealthy' || (c.state === 'exited' && Number(c.exit_code) > 0);
  const badTimer = x => x.verdict ? x.verdict === 'failed' : !!((x.unit_result && x.unit_result !== 'success') || x.unit_state === 'failed');
  const items = host => (host.containers && host.containers.items) || [];
  const failedUnits = host => (host.units && host.units.failed) || [];
  const timers = host => (host.units && host.units.timers) || [];
  const problemCount = host => (host.online ? 0 : 1) + items(host).filter(badContainer).length + failedUnits(host).length + timers(host).filter(badTimer).length;

  // what one host shows under the current filter and search; null hides the host
  function viewOf(host) {
    const q = lc(ui.search.trim());
    const onlyBad = ui.filter === 'problems';
    const nameHit = !q || lc(host.name).includes(q) || lc(host.host).includes(q);
    const pick = (list, test, hit) => list.filter(x => (!onlyBad || test(x)) && (nameHit || hit(x)));
    const v = {
      containers: pick(items(host), badContainer, c => [c.name, c.image, c.project, c.service].some(s => lc(s).includes(q))),
      listen: onlyBad ? [] : pick(host.listen || [], () => false, s => [s.proc, s.port, s.ip, s.proto].some(x => lc(x).includes(q))),
      timers: pick(timers(host), badTimer, x => lc(x.name).includes(q) || lc(x.unit).includes(q)),
      failed: pick(failedUnits(host), () => true, x => lc(x.name).includes(q)),
      peers: onlyBad ? [] : (((host.overlay && host.overlay.peers) || []).filter(p => nameHit || lc(p.name).includes(q))),
      system: !onlyBad && nameHit
    };
    const any = v.containers.length || v.listen.length || v.timers.length || v.failed.length || v.peers.length;
    if (onlyBad && !problemCount(host)) return null;
    if (q && !nameHit && !any) return null;
    return v;
  }

  // ------------------------------------------------------------------ pieces
  const dot = cls => h('i', { class: 'ru-dot ru-dot--' + cls, 'aria-hidden': 'true' });
  function bar(p, label, aria) {
    return h('div', { class: 'ru-meter' }, h('div', { class: 'ru-meter__top' }, h('span', {}, label), h('strong', { class: 'num' }, pct(p))),
      h('div', { class: 'ru-bar ru-bar--' + level(p), role: 'img', 'aria-label': aria }, h('i', { style: 'width:' + Math.round(p) + '%' })));
  }
  const section = (title, count, ...kids) => h('section', { class: 'ru-sec' },
    h('h3', { class: 'ru-sec__title' }, title, count != null ? h('span', { class: 'ru-sec__n' }, String(count)) : null), ...kids);

  function systemSection(host, cpus) {
    const s = host.system;
    if (!s) return null;
    const rows = [];
    if (s.load && s.load.length) {
      const l1 = s.load[0];
      rows.push(h('div', { class: 'ru-meter' }, h('div', { class: 'ru-meter__top' }, h('span', {}, t('running.load')),
        h('strong', { class: 'num' }, s.load.map(v => LL.fmtNum(v, 2)).join(' · '))),
      cpus ? h('div', { class: 'ru-bar ru-bar--' + level(ratio(l1, cpus)), role: 'img', 'aria-label': t('running.load_of', { cores: cpus }) }, h('i', { style: 'width:' + Math.round(ratio(l1, cpus)) + '%' })) : null,
      cpus ? h('div', { class: 'ru-meter__sub muted' }, t('running.load_of', { cores: cpus })) : null));
    }
    if (s.mem_total) {
      const used = s.mem_total - (s.mem_avail || 0);
      rows.push(bar(ratio(used, s.mem_total), t('running.memory'), t('running.mem_text', { used: size(used), total: size(s.mem_total) })),
        h('div', { class: 'ru-meter__sub muted ru-pull' }, t('running.mem_text', { used: size(used), total: size(s.mem_total) })));
    }
    if (s.swap_total) {
      const used = s.swap_total - (s.swap_free || 0);
      if (used > 0.05 * s.swap_total) rows.push(bar(ratio(used, s.swap_total), t('running.swap'), t('running.mem_text', { used: size(used), total: size(s.swap_total) })));
    }
    (s.disks || []).forEach(d => {
      const used = d.total - d.free;
      rows.push(bar(ratio(used, d.total), d.mount, t('running.disk_text', { free: size(d.free), total: size(d.total) })),
        h('div', { class: 'ru-meter__sub muted ru-pull' }, t('running.disk_text', { free: size(d.free), total: size(d.total) })));
    });
    return section(t('running.system'), null, h('div', { class: 'ru-meters' }, rows), sparkBox(host));
  }

  // ---- 24 h trend: load per core, memory, disk (percent), one canvas
  function sparkBox(host) {
    const canvas = h('canvas', { class: 'ru-spark__cv', role: 'img', 'aria-label': t('running.spark.label'), 'data-spark': host.id });
    const legend = h('div', { class: 'ru-legend muted' },
      [['load', 'running.load_short'], ['mem', 'running.memory'], ['disk', 'running.disk']].map(([k, key]) => h('span', {}, h('i', { class: 'ru-sw ru-sw--' + k, 'aria-hidden': 'true' }), t(key))),
      h('span', { class: 'ru-legend__range' }, t('running.spark.range')));
    return h('div', { class: 'ru-spark' }, canvas, legend);
  }
  const cssVar = (name, fallback) => (getComputedStyle(document.documentElement).getPropertyValue(name) || '').trim() || fallback;
  function drawSpark(canvas, points) {
    const w = canvas.clientWidth, hgt = canvas.clientHeight;
    if (!w || !hgt) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr);
    const g = canvas.getContext('2d');
    g.scale(dpr, dpr);
    g.clearRect(0, 0, w, hgt);
    const grid = cssVar('--ll-border', 'currentColor');
    g.strokeStyle = grid; g.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach(f => { const y = Math.round(hgt * f) + 0.5; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); });
    if (!points || points.length < 2) return;
    const t0 = points[0][0], t1 = points[points.length - 1][0] || (t0 + 1), span = Math.max(1, t1 - t0);
    const gap = 3 * 300;
    const series = [
      ['--ll-pcb-trace-flow', 'currentColor', p => (p[2] > 0 && p[1] != null ? Math.min(100, p[1] / p[2] * 100) : null)],
      ['--ll-pcb-trace-service', 'currentColor', p => p[3]],
      ['--ll-pcb-trace-uplink', 'currentColor', p => p[4]]];
    series.forEach(([name, fb, val]) => {
      g.strokeStyle = cssVar(name, fb); g.lineWidth = 1.6; g.lineJoin = 'round'; g.lineCap = 'round';
      g.beginPath();
      let prev = null;
      points.forEach(p => {
        const v = val(p);
        if (v == null) { prev = null; return; }
        const x = (p[0] - t0) / span * (w - 2) + 1, y = hgt - 2 - Math.max(0, Math.min(100, v)) / 100 * (hgt - 4);
        if (prev == null || p[0] - prev > gap) g.moveTo(x, y); else g.lineTo(x, y);
        prev = p[0];
      });
      g.stroke();
    });
  }
  function paintSparks(root) {
    root.querySelectorAll('canvas[data-spark]').forEach(cv => { const c = cache[cv.dataset.spark]; drawSpark(cv, c && c.points); });
  }
  async function loadHealth(root, hosts, alive) {
    const now = Date.now();
    const stale = hosts.filter(x => !cache[x.id] || now - cache[x.id].at > HEALTH_MS);
    for (const x of stale) {
      try {
        const r = await api('/api/agents/' + enc(x.id) + '/health?hours=24');
        cache[x.id] = { at: Date.now(), points: r.points || [] };
      } catch (e) { cache[x.id] = { at: Date.now(), points: [] }; }
      if (!alive()) return 0;
      root.querySelectorAll('canvas[data-spark="' + x.id + '"]').forEach(cv => drawSpark(cv, cache[x.id].points));
    }
    return stale.length;
  }
  const coresOf = id => { const p = cache[id] && cache[id].points; const last = p && p.length ? p[p.length - 1] : null; return last ? last[2] : null; };

  // ---- containers
  const STATE_DOT = { running: 'ok', restarting: 'bad', dead: 'bad', paused: 'warn', created: 'off', exited: 'off', removing: 'warn' };
  function stateChip(c) {
    if (c.state === 'restarting') return h('span', { class: 'chip chip--danger' }, c.restarts ? t('running.restarting_n', { n: c.restarts }) : t('running.state.restarting'));
    if (c.state === 'running' && c.health) return h('span', { class: 'chip chip--' + (c.health === 'healthy' ? 'ok' : c.health === 'unhealthy' ? 'danger' : 'warn') }, t('running.health.' + c.health));
    if (c.state === 'running') return h('span', { class: 'chip chip--ok' }, t('running.state.running'));
    return h('span', { class: 'chip' + (badContainer(c) ? ' chip--danger' : '') }, t('running.state.' + c.state));
  }
  function whenText(c, now) {
    if (c.state === 'running') return c.started ? t('running.up', { age: LL.fmtAge(now - c.started) }) : '';
    if (c.state === 'restarting') return c.exit_code ? t('running.exit_code', { code: c.exit_code }) : '';
    if (c.finished) return t('running.exited_ago', { age: LL.fmtAge(now - c.finished) }) + (c.exit_code ? ' · ' + t('running.exit_code', { code: c.exit_code }) : '');
    return '';
  }
  function portsText(c) {
    const seen = [];
    (c.ports || []).forEach(p => { if (p.port) { const s = p.port + '/' + (p.proto || 'tcp'); if (!seen.includes(s)) seen.push(s); } });
    return seen.length > 3 ? seen.slice(0, 3).join(', ') + ' ' + t('running.more_n', { n: seen.length - 3 }) : seen.join(', ');
  }
  function containerRow(c, now) {
    const rate = c.rate && (c.rate[0] || c.rate[1]) ? h('span', { class: 'ru-c__rate num', title: t('running.rate_title') }, '↑ ' + LL.fmtRate(c.rate[0]), h('br'), '↓ ' + LL.fmtRate(c.rate[1])) : h('span', { class: 'ru-c__rate' });
    return h('div', { class: 'ru-c' + (badContainer(c) ? ' is-bad' : '') + (c.state !== 'running' && !badContainer(c) ? ' is-idle' : ''), role: 'listitem' },
      dot(STATE_DOT[c.state] || 'off'),
      h('span', { class: 'ru-c__name' }, h('strong', { title: c.name }, c.name), c.project ? h('small', { class: 'muted', title: c.project }, c.project) : null),
      h('span', { class: 'ru-c__image mono', title: c.image }, c.image),
      h('span', { class: 'ru-c__state' }, stateChip(c)),
      h('span', { class: 'ru-c__when muted' }, whenText(c, now)),
      h('span', { class: 'ru-c__ports mono' }, portsText(c)),
      rate);
  }
  function containersSection(host, list, now, key) {
    const c = host.containers || {};
    if (!c.available) {
      const reason = String(c.reason || 'unavailable');
      const key = reason.indexOf('agent.err.') === 0 ? reason : (LL.strings['running.cont.reason.' + reason] ? 'running.cont.reason.' + reason : 'running.cont.reason.unavailable');
      return section(t('running.containers'), null, h('p', { class: 'muted ru-note' }, t(key)));
    }
    const sorted = list.slice().sort((a, b) => (badContainer(b) - badContainer(a)) || ((a.state === 'running' ? 0 : 1) - (b.state === 'running' ? 0 : 1)) || String(a.name).localeCompare(String(b.name)));
    const open = ui.more.has(key);
    const shown = open ? sorted : sorted.slice(0, FIRST_ROWS);
    const head = h('div', { class: 'ru-c ru-c--head muted', 'aria-hidden': 'true' }, h('span'), h('span', {}, t('running.col.name')), h('span', {}, t('running.col.image')),
      h('span', {}, t('running.col.state')), h('span', {}, t('running.col.since')), h('span', {}, t('running.col.ports')), h('span', {}, t('running.col.rate')));
    const more = sorted.length > FIRST_ROWS ? h('button', { class: 'btn btn--ghost btn--sm ru-more', type: 'button', onclick: () => { open ? ui.more.delete(key) : ui.more.add(key); LL.runningRedraw && LL.runningRedraw(); } },
      open ? t('running.show_less') : t('running.show_all', { n: sorted.length })) : null;
    return section(t('running.containers'), items(host).length, sorted.length ? h('div', { class: 'ru-clist', role: 'list' }, head, shown.map(x => containerRow(x, now))) : h('p', { class: 'muted ru-note' }, t('running.cont.none')),
      more, c.short_lived > 0 ? h('p', { class: 'muted fineprint ru-note' }, t('running.short_lived', { n: c.short_lived })) : null);
  }

  // ---- services, timers, units, peers
  function servicesSection(host, list) {
    if (!list.length && !host.listen_local) return null;
    const rows = list.slice().sort((a, b) => a.port - b.port).map(s => h('li', { class: 'ru-svc' }, h('span', { class: 'ru-svc__port mono' }, s.port + '/' + s.proto),
      h('span', { class: 'ru-svc__proc', title: s.proc || '' }, s.proc || t('running.unknown_proc')), h('span', { class: 'ru-svc__ip mono muted' }, s.ip)));
    return section(t('running.services'), list.length, rows.length ? h('ul', { class: 'ru-svcs' }, rows) : null,
      host.listen_local ? h('p', { class: 'muted fineprint ru-note' }, t('running.listen_local', { n: host.listen_local })) : null);
  }
  function timersSection(host, list, now) {
    if (!host.units || !host.units.available) return list.length ? null : (host.kind === 'agent' ? section(t('running.timers'), null, h('p', { class: 'muted ru-note' }, t('running.units.unavailable'))) : null);
    if (!list.length) return null;
    const rows = list.map(x => h('li', { class: 'ru-t' + (badTimer(x) ? ' is-bad' : '') },
      h('span', { class: 'ru-t__name', title: x.name }, x.name),
      h('span', { class: 'ru-t__when muted' }, ago(now, x.last)),
      h('span', { class: 'ru-t__when muted' }, x.next ? t('running.in', { age: LL.fmtAge(x.next - now) }) : '—'),
      x.verdict === 'once'
        ? h('span', { class: 'chip chip--warn', title: t('running.timer.once.title') }, t('running.timer.once', { when: x.next ? LL.fmtAge(x.next - now) : '—' }))
        : h('span', { class: 'chip ' + (badTimer(x) ? 'chip--danger' : 'chip--ok') }, t(badTimer(x) ? 'running.timer.failed' : 'running.timer.ok'))));
    return section(t('running.timers'), list.length, h('ul', { class: 'ru-ts' }, rows));
  }
  function failedSection(list, now) {
    if (!list.length) return null;
    return section(t('running.failed'), list.length, h('ul', { class: 'ru-fails' }, list.map(x => h('li', { class: 'ru-fail' },
      dot('bad'), h('span', { class: 'ru-fail__name', title: x.name }, x.name),
      h('span', { class: 'muted' }, (x.since ? t('running.since', { age: LL.fmtAge(now - x.since) }) : '') + (x.result ? ' · ' + x.result : ''))))));
  }
  function peersSection(list, ov) {
    if (!ov || !ov.available) return null;
    const self = ov.self;
    return section(t('running.overlay'), list.length, list.length ? h('ul', { class: 'ru-peers' }, list.map(p => h('li', { class: 'ru-peer' },
      dot(p.online ? 'ok' : 'off'), h('span', { class: 'ru-peer__name', title: p.name }, p.name), h('span', { class: 'mono muted ru-peer__ip' }, (p.ips || [])[0] || ''),
      h('span', { class: 'muted ru-peer__os' }, p.os || '')))) : h('p', { class: 'muted ru-note' }, t('running.peers.none')),
    self && self.name ? h('p', { class: 'muted fineprint ru-note' }, t('running.peers.self', { name: self.name })) : null);
  }

  // ------------------------------------------------------------------ one host card
  function hostCard(host, v, now) {
    const online = host.online;
    const status = online
      ? h('span', { class: 'chip chip--ok' }, dot('ok'), t('running.online'))
      : h('span', { class: 'chip chip--danger' }, dot('bad'), t('running.offline'));
    const seen = host.age_s != null ? t('time.ago', { age: LL.fmtAge(host.age_s) }) : ago(now, host.last_seen);
    const head = h('header', { class: 'ru-host__head' },
      h('div', { class: 'ru-host__title' }, h('h2', { title: host.name }, host.name), h('span', { class: 'chip ru-kind' }, t('running.kind.' + host.kind)), status),
      h('div', { class: 'ru-host__meta muted' },
        h('span', {}, t('running.last_report', { when: seen })),
        host.version ? h('span', {}, t('running.version', { v: host.version })) : null,
        host.os ? h('span', { title: host.os }, host.os) : null,
        (host.ips || []).length ? h('span', { class: 'mono' }, host.ips.slice(0, 2).join(', ')) : null,
        host.url ? h('span', { class: 'mono', title: host.url }, host.url) : null,
        host.device ? h('a', { href: '#/network/' + enc(host.device), class: 'ru-host__link' }, t('running.device_card')) : null));
    const upd = host.update ? h('div', { class: 'callout callout--warn ru-update' },
      h('p', {}, h('strong', {}, t('running.update.title', { v: host.update.version })), ' ', t('running.update.text')),
      h('div', { class: 'ru-update__row' }, h('code', { class: 'cmd' }, host.update.image),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.copyText(host.update.image) }, t('access.copy')))) : null;
    const err = host.error ? h('p', { class: 'msg msg--error ru-note' }, t(host.error)) : null;
    const key = host.id, cont = host.containers;
    const showCont = !!cont && (cont.available ? (v.containers.length > 0 || (ui.filter === 'all' && !ui.search.trim())) : ui.filter === 'all');
    const cells = [
      v.system ? systemSection(host, coresOf(host.id)) : null,
      h('div', { class: 'ru-col' }, failedSection(v.failed, now), timersSection(host, v.timers, now)),
      showCont ? h('div', { class: 'ru-wide' }, containersSection(host, v.containers, now, key)) : null,
      servicesSection(host, v.listen), peersSection(v.peers, host.overlay)];
    const kids = cells.filter(x => x && !(x.classList && x.classList.contains('ru-col') && !x.children.length));
    return h('article', { class: 'card ru-host' + (online ? '' : ' is-offline') + (problemCount(host) ? ' has-problems' : ''), 'data-host': host.id },
      head, upd, err, h('div', { class: 'ru-grid' }, kids));
  }

  // ------------------------------------------------------------------ the view
  async function runningView(app) {
    const fr = LL.frame(app, 'running');
    const page = h('div', { class: 'page ru-page' });
    fr.main.append(page);
    let alive = true, data = null, timer = null, fw = null;
    const lead = h('p', { class: 'muted ru-lead' });
    const filters = h('div', { class: 'seg ru-filters', role: 'group', 'aria-label': t('running.filter.label') });
    const search = h('input', { class: 'input ru-search', type: 'search', placeholder: t('running.search'), 'aria-label': t('running.search'), value: ui.search,
      oninput: () => { ui.search = search.value; draw(); } });
    const list = h('div', { class: 'ru-list' });
    page.append(h('div', { class: 'page__head ru-head' }, h('h1', {}, t('running.title')), lead), h('div', { class: 'ru-tools' }, filters, search), list);

    function counts() {
      const hosts = data.hosts;
      return { hosts: hosts.length, containers: hosts.reduce((a, x) => a + items(x).length, 0), bad: hosts.reduce((a, x) => a + problemCount(x), 0) };
    }
    function drawFilters(c) {
      filters.replaceChildren(...['all', 'problems'].map(k => h('button', { class: 'seg__btn' + (ui.filter === k ? ' is-active' : ''), type: 'button', 'aria-pressed': ui.filter === k ? 'true' : 'false',
        onclick: () => { ui.filter = k; draw(); } }, t('running.filter.' + k) + (k === 'problems' && c.bad ? ' (' + c.bad + ')' : ''))));
    }
    function empty() {
      return h('div', { class: 'card ru-empty' }, h('h2', {}, t('running.empty.title')), h('p', { class: 'muted' }, t('running.empty.text')),
        LL.can('admin') ? h('div', { class: 'btn-row btn-row--wrap' }, h('a', { class: 'btn btn--primary', href: '#/settings/agents' }, t('agents.pair.button'))) : h('p', { class: 'muted' }, t('running.empty.ask')));
    }
    function draw() {
      if (!alive || !data) return;
      const c = counts();
      lead.textContent = c.hosts ? t('running.lead', { hosts: c.hosts, containers: c.containers }) : '';
      drawFilters(c);
      const now = data.t || Date.now() / 1000;
      const cards = [];
      data.hosts.forEach(host => { const v = viewOf(host); if (v) cards.push(hostCard(host, v, now)); });
      if (!data.hosts.length) list.replaceChildren(empty());
      else if (!cards.length) list.replaceChildren(h('div', { class: 'card ru-empty' }, h('p', { class: 'muted' }, t(ui.filter === 'problems' && !ui.search ? 'running.none_problems' : 'running.none_match'))));
      else list.replaceChildren(...cards);
      paintSparks(list);
    }
    LL.runningRedraw = draw;
    async function load() {
      try { data = await api('/api/running'); } catch (e) { if (alive && !data) list.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!alive) return;
      if (fw) fw.ok();
      draw();
      loadHealth(list, data.hosts, () => alive).then(n => { if (n && alive) draw(); });
    }
    const onTheme = () => draw();
    document.addEventListener('ll-theme', onTheme);
    list.append(h('div', { class: 'skeleton' }));
    fw = LL.fresh.watch(list, REFRESH_MS, load);
    await load();
    timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
    const onResize = () => paintSparks(list);
    window.addEventListener('resize', onResize);
    return { destroy: () => { alive = false; clearInterval(timer); fw.destroy(); LL.runningRedraw = null; document.removeEventListener('ll-theme', onTheme); window.removeEventListener('resize', onResize); fr.destroy(); } };
  }
  LL.view('running', runningView);
})();
