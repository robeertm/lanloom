/* Lanloom — containers on the map: fold state per host (remembered in this browser), the card sections of a container and of a
   host that runs a host agent or is read through a container source. Plain DOM, no build step.
   Fold state: localStorage "ll.fold.<hostId>" = "1" means unfolded; the default is folded. The open map listens to "ll-fold". */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t } = LL;
  const enc = encodeURIComponent;
  const KEY = id => 'll.fold.' + id;

  // ------------------------------------------------------------------ fold state
  LL.foldHosts = LL.foldHosts || [];                 // ids of the hosts whose containers the open map can fold
  const read = id => { try { return localStorage.getItem(KEY(id)) === '1'; } catch (e) { return false; } };
  const write = (id, on) => { try { if (on) localStorage.setItem(KEY(id), '1'); else localStorage.removeItem(KEY(id)); } catch (e) { /* private mode: not remembered */ } };
  LL.isUnfolded = read;
  LL.setUnfolded = function (id, on, quiet) { write(id, on); if (!quiet) document.dispatchEvent(new CustomEvent('ll-fold')); };
  LL.setAllUnfolded = function (on) { (LL.foldHosts || []).forEach(id => write(id, on)); document.dispatchEvent(new CustomEvent('ll-fold')); };
  LL.allUnfolded = () => (LL.foldHosts || []).length > 0 && LL.foldHosts.every(read);

  // ------------------------------------------------------------------ small pieces
  const dot = cls => h('i', { class: 'ru-dot ru-dot--' + cls, 'aria-hidden': 'true' });
  const STATE_DOT = { running: 'ok', restarting: 'bad', dead: 'bad', paused: 'warn', created: 'off', exited: 'off', removing: 'warn' };
  const bad = c => c.state === 'restarting' || c.state === 'dead' || c.health === 'unhealthy' || (c.state === 'exited' && Number(c.exit_code) > 0);
  const section = (title, body, key) => h('section', { class: 'detail__sec', dataset: { sec: key } }, h('h3', {}, title), body);
  const facts = rows => h('dl', { class: 'facts' }, rows.filter(r => r && r[1] != null && r[1] !== '').map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]).flat());
  const ago = s => t('time.ago', { age: LL.fmtAge(Math.max(0, s)) });
  const size = n => LL.fmtBytes(Number(n) || 0);
  const ratio = (a, b) => (b > 0 ? Math.max(0, Math.min(100, a / b * 100)) : 0);

  function chips(c) {
    const out = [];
    if (c.state === 'restarting') out.push(h('span', { class: 'chip chip--danger' }, c.restarts ? t('running.restarting_n', { n: c.restarts }) : t('running.state.restarting')));
    else out.push(h('span', { class: 'chip' + (c.state === 'running' ? ' chip--ok' : bad(c) ? ' chip--danger' : '') }, t('running.state.' + c.state)));
    if (c.health && c.state !== 'restarting') out.push(h('span', { class: 'chip chip--' + (c.health === 'healthy' ? 'ok' : c.health === 'unhealthy' ? 'danger' : 'warn') }, t('running.health.' + c.health)));
    return h('div', { class: 'ct-chips' }, out);
  }
  function whenText(c, now) {
    if (c.state === 'running') return c.started ? t('running.up', { age: LL.fmtAge(Math.max(0, now - c.started)) }) : '';
    if (c.finished) return t('running.exited_ago', { age: LL.fmtAge(Math.max(0, now - c.finished)) });
    return '';
  }
  const portsText = c => {
    const seen = [];
    (c.ports || []).forEach(p => { if (p.port) { const s = p.port + '/' + (p.proto || 'tcp'); if (!seen.includes(s)) seen.push(s); } });
    return seen.join(', ');
  };
  const link = (href, text) => h('a', { class: 'ct-link', href }, text);
  const runningBtn = () => h('a', { class: 'btn btn--ghost btn--sm', href: '#/running' }, t('detail.agent.open_running'));

  // ------------------------------------------------------------------ card of one container
  LL.containerSection = function (d) {
    const c = d.container || {};
    const now = c.t || Date.now() / 1000;
    const rate = c.rate && (c.rate[0] || c.rate[1]) ? h('span', { class: 'num' }, '↑ ' + LL.fmtRate(c.rate[0]) + '  ↓ ' + LL.fmtRate(c.rate[1])) : null;
    const nets = (c.networks || []).map(n => h('li', {}, h('span', { class: 'mono' }, n.name), n.ip ? h('span', { class: 'muted' }, ' · ' + n.ip) : null));
    const rows = [
      [t('detail.container.image'), h('span', { class: 'mono' }, c.image || '')],
      [t('detail.container.project'), c.project ? h('span', { class: 'mono' }, c.project) : null],
      [t('detail.container.service'), c.service ? h('span', { class: 'mono' }, c.service) : null],
      [t('detail.container.runtime'), d.runtime || null],
      [t('detail.container.since'), whenText(c, now) || null],
      [t('detail.container.exit'), c.state !== 'running' && c.exit_code != null ? String(c.exit_code) : null],
      [t('detail.container.restarts'), c.restarts ? LL.fmtNum(c.restarts, 0) : null],
      [t('detail.container.created'), c.created ? ago(now - c.created) : null],
      [t('detail.container.ports'), portsText(c) ? h('span', { class: 'mono' }, portsText(c)) : null],
      [t('detail.container.netmode'), c.netmode ? h('span', { class: 'mono' }, c.netmode) : null],
      [t('detail.container.rate'), rate],
      [t('detail.container.total'), c.tx || c.rx ? h('span', { class: 'num' }, '↑ ' + size(c.tx) + '  ↓ ' + size(c.rx)) : null],
      [t('detail.container.host'), d.device ? link('#/network/' + enc(d.device), d.host || d.device) : (d.host || null)]];
    const kids = [chips(c), facts(rows)];
    if (nets.length) kids.push(h('div', { class: 'ct-sub' }, h('h4', {}, t('detail.container.networks')), h('ul', { class: 'plain-list' }, nets)));
    kids.push(h('div', { class: 'ct-foot' }, runningBtn()));
    return section(t('detail.container.title'), h('div', { class: 'ct-body' }, kids), 'container');
  };

  // ------------------------------------------------------------------ card of a host with an agent or container source
  function meter(p, label, text) {
    const lvl = p >= 90 ? 'bad' : p >= 80 ? 'warn' : 'ok';
    return h('div', { class: 'ru-meter' }, h('div', { class: 'ru-meter__top' }, h('span', {}, label), h('strong', { class: 'num' }, LL.fmtNum(p, 0) + ' %')),
      h('div', { class: 'ru-bar ru-bar--' + lvl, role: 'img', 'aria-label': text }, h('i', { style: 'width:' + Math.round(p) + '%' })),
      h('div', { class: 'ru-meter__sub muted' }, text));
  }
  function hostEntry(x, devId, onToggle) {
    const kids = [];
    const online = x.online;
    const head = h('div', { class: 'ct-head' }, dot(online ? 'ok' : 'bad'), h('strong', {}, x.name || x.host || '—'),
      h('span', { class: 'chip' + (online ? ' chip--ok' : ' chip--danger') }, t(online ? 'running.online' : 'running.offline')),
      h('span', { class: 'chip' }, t('running.kind.' + (x.kind === 'proxy' ? 'proxy' : 'agent'))));
    kids.push(head);
    const items = (x.containers && x.containers.items) || [];
    const failed = (x.units && x.units.failed) || [];
    const timersFailed = ((x.units && x.units.timers) || []).filter(m => (m.unit_result && m.unit_result !== 'success') || m.unit_state === 'failed').length;
    const unhealthy = items.filter(bad).length;
    const run = items.filter(c => c.state === 'running').length;
    const s = x.system || {};
    const sys = [];
    const l1 = s.load && s.load.length ? s.load[0] : null;
    if (s.mem_total) { const used = s.mem_total - (s.mem_avail || 0); sys.push(meter(ratio(used, s.mem_total), t('running.memory'), t('running.mem_text', { used: size(used), total: size(s.mem_total) }))); }
    const worst = (s.disks || []).slice().sort((a, b) => ratio(b.total - b.free, b.total) - ratio(a.total - a.free, a.total))[0];
    if (worst) sys.push(meter(ratio(worst.total - worst.free, worst.total), worst.mount, t('running.disk_text', { free: size(worst.free), total: size(worst.total) })));
    kids.push(facts([
      [t('detail.agent.last_report'), x.age_s != null ? ago(x.age_s) : t('running.never')],
      [t('detail.agent.version'), x.version ? h('span', { class: 'mono' }, x.version) : null],
      [t('detail.agent.os'), x.os || null],
      [t('running.load'), l1 != null ? h('span', { class: 'num' }, s.load.map(v => LL.fmtNum(v, 2)).join(' · ')) : null],
      [t('detail.agent.counts'), x.containers && x.containers.available ? t('detail.agent.counts_text', { running: run, unhealthy, failed: failed.length + timersFailed }) : null],
      [t('detail.agent.error'), x.error ? t(x.error) : null]]));
    if (x.update) kids.push(h('p', { class: 'ct-update' }, h('strong', {}, t('running.update.title', { v: x.update.version })), ' ', h('span', { class: 'muted' }, t('running.update.text')), h('code', { class: 'ct-code mono' }, x.update.image)));
    if (sys.length) kids.push(h('div', { class: 'ru-meters ct-meters' }, sys));
    if (items.length) {
      const sorted = items.slice().sort((a, b) => (bad(b) - bad(a)) || ((a.state === 'running' ? 0 : 1) - (b.state === 'running' ? 0 : 1)) || String(a.name).localeCompare(String(b.name)));
      const folds = devId && (LL.foldHosts || []).includes(devId);
      const toggle = folds ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: ev => { LL.setUnfolded(devId, !LL.isUnfolded(devId)); ev.currentTarget.textContent = t(LL.isUnfolded(devId) ? 'detail.agent.hide_on_map' : 'detail.agent.show_on_map'); } },
        t(LL.isUnfolded(devId) ? 'detail.agent.hide_on_map' : 'detail.agent.show_on_map')) : null;
      kids.push(h('div', { class: 'ct-sub' }, h('div', { class: 'ct-sub__head' }, h('h4', {}, t('detail.agent.containers', { n: items.length })), toggle),
        h('ul', { class: 'ct-list' }, sorted.map(c => h('li', {}, h('a', { class: 'ct-row' + (bad(c) ? ' is-bad' : ''), href: '#/network/' + enc('ct:' + x.id + ':' + c.name), onclick: () => { if (devId) LL.setUnfolded(devId, true, true); } },
          dot(STATE_DOT[c.state] || 'off'), h('span', { class: 'ct-row__name' }, c.name), h('span', { class: 'ct-row__state muted' }, t('running.state.' + c.state))))))));
    }
    return h('div', { class: 'ct-entry' }, kids);
  }
  LL.agentSection = function (d, devId) {
    const hosts = d.hosts || [];
    if (!hosts.length) return null;
    const title = hosts.every(x => x.kind === 'proxy') ? t('detail.agent.title_source') : t('detail.agent.title');
    return section(title, h('div', { class: 'ct-body' }, hosts.map(x => hostEntry(x, devId)), h('div', { class: 'ct-foot' }, runningBtn())), 'agent');
  };
})();
