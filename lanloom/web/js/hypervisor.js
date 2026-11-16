/* Lanloom — read-only views of a hypervisor integration: findings, firewall, guest network cards, bridges,
   and the device-card sections for a host and for a guest. Plain DOM, one small inline SVG, no animation. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t } = LL;
  const pct = x => Math.round((Number(x) || 0) * 100) + ' %';
  const LEVEL = { alert: 'danger', warn: 'warn', info: 'muted' };
  const byName = (a, b) => (a.name || '') < (b.name || '') ? -1 : 1;

  function table(cols, rows, cls) {
    return h('div', { class: 'fw-table-wrap' }, h('table', { class: 'fw-table' + (cls ? ' ' + cls : '') },
      h('thead', {}, h('tr', {}, cols.map(c => h('th', { scope: 'col' }, c)))),
      h('tbody', {}, rows.map(r => h('tr', { class: r.off ? 'is-off' : null }, r.cells.map(c => h('td', {}, c)))))));
  }
  function card(title, lead, ...body) {
    return h('section', { class: 'card fw-extra hv-card' }, h('h2', {}, title), lead ? h('p', { class: 'muted fw-hint' }, lead) : null, ...body);
  }
  const mono = v => v ? h('span', { class: 'mono' }, v) : '—';
  const flag = on => h('span', { class: 'chip ' + (on ? 'chip--ok' : 'chip--muted') }, t(on ? 'fw.state.on' : 'fw.state.off'));

  // ------------------------------------------------------------------ findings
  function findingText(f) {
    const k = 'hv.finding.' + f.kind;
    const p = Object.assign({}, f.params || {});
    const s = t(k, p);
    return s === k ? f.kind : s;
  }
  function findingList(list) {
    const order = { alert: 0, warn: 1, info: 2 };
    return h('ul', { class: 'hv-findings' }, list.slice().sort((a, b) => (order[a.level] ?? 3) - (order[b.level] ?? 3)).map(f =>
      h('li', { class: 'hv-finding hv-finding--' + (f.level || 'info') },
        h('span', { class: 'chip chip--' + (LEVEL[f.level] || 'muted') }, t('hv.level.' + (f.level || 'info'))),
        h('span', { class: 'hv-finding__text' }, findingText(f)))));
  }

  // ------------------------------------------------------------------ firewall tab
  function ruleRow(r) {
    const ports = [r.macro ? r.macro : null, r.sport ? r.sport + ' →' : null, r.dport].filter(Boolean).join(' ');
    const act = r.action === 'group' ? t('hv.rule.group', { name: r.group }) : t('fw.pol.action.' + (['allow', 'block', 'reject'].includes(r.action) ? r.action : 'other'));
    return { off: !r.enabled, cells: [
      t('hv.dir.' + (r.direction || 'in')),
      h('span', { class: 'fw-rule__act fw-rule__act--' + (['allow', 'block', 'reject'].includes(r.action) ? r.action : 'other') }, act),
      r.proto && r.proto !== 'any' ? r.proto.toUpperCase() : '—',
      mono(ports || null),
      h('span', { class: 'mono' }, (r.source || t('hv.any')) + ' → ' + (r.dest || t('hv.any'))),
      r.comment || '' ] };
  }
  function ruleGroups(s) {
    const hostName = id => (s.hosts.find(x => x.id === id) || {}).name || id;
    const guestName = id => (s.guests.find(x => x.id === id) || {}).name || id;
    const groups = [];
    const add = (key, title, rows) => { if (rows.length) groups.push({ key, title, rows }); };
    add('dc', t('hv.scope.dc'), (s.fw_rules || []).filter(r => r.scope === 'dc'));
    const names = [...new Set((s.fw_rules || []).filter(r => r.scope === 'group').map(r => r.owner))].sort();
    names.forEach(n => add('g:' + n, t('hv.scope.group', { name: n }), s.fw_rules.filter(r => r.scope === 'group' && r.owner === n)));
    s.hosts.slice().sort(byName).forEach(x => add('n:' + x.id, t('hv.scope.node', { name: x.name }), (s.fw_rules || []).filter(r => r.scope === 'node' && r.owner === x.id)));
    s.guests.slice().sort(byName).forEach(g => add('v:' + g.id, t('hv.scope.guest', { name: g.name }), (s.guest_rules || []).filter(r => r.owner === g.id)));
    return groups.map(g => h('div', { class: 'hv-group' }, h('h3', { class: 'fw-sub' }, g.title, h('span', { class: 'chip chip--muted' }, String(g.rows.length))),
      table([t('hv.col.direction'), t('hv.col.action'), t('fw.col.proto'), t('hv.col.ports'), t('hv.col.path'), t('hv.col.comment')], g.rows.map(ruleRow), 'hv-rules')));
  }
  function optionsLine(s) {
    const pol = v => v ? v.toLowerCase() : '—';
    const rows = (s.fw_options || []).slice().sort((a, b) => (a.scope === 'dc' ? 0 : 1) - (b.scope === 'dc' ? 0 : 1)).map(o => {
      const who = o.scope === 'dc' ? t('hv.scope.dc') : o.scope === 'guest' ? t('hv.scope.guest', { name: (s.guests.find(x => x.id === o.owner) || {}).name || '?' })
        : t('hv.scope.node', { name: (s.hosts.find(x => x.id === o.owner) || {}).name || '?' });
      return { cells: [who, flag(o.enabled), pol(o.policy_in), pol(o.policy_out)] };
    });
    return rows.length ? table([t('hv.col.level'), t('fw.col.state'), t('hv.col.policy_in'), t('hv.col.policy_out')], rows, 'hv-opts') : null;
  }
  function objects(s) {
    const raw = s.raw || [];
    const part = (area, title, withCount) => {
      const items = raw.filter(x => x.area === area);
      if (!items.length) return null;
      return h('div', { class: 'hv-group' }, h('h3', { class: 'fw-sub' }, title, h('span', { class: 'chip chip--muted' }, String(items.length))),
        table([t('fw.col.name'), t('hv.col.members'), t('hv.col.detail')], items.map(x => ({ cells: [h('strong', {}, x.title), x.count != null ? String(x.count) : '—', x.detail ? h('span', { class: 'mono hv-cut', title: x.detail }, x.detail) : '—'] })), 'hv-objs'));
    };
    return [part('fw_alias', t('hv.aliases')), part('fw_ipset', t('hv.ipsets')), part('fw_group', t('hv.groups'))].filter(Boolean);
  }
  function nicsCard(s) {
    const gname = id => s.guests.find(x => x.id === id) || {};
    const hname = id => (s.hosts.find(x => x.id === id) || {}).name || '—';
    const rows = (s.guest_nics || []).map(n => ({ g: gname(n.guest), n })).sort((a, b) => (a.g.name || '') < (b.g.name || '') ? -1 : (a.g.name || '') > (b.g.name || '') ? 1 : (a.n.name < b.n.name ? -1 : 1));
    if (!rows.length) return null;
    return card(t('hv.nics.title'), t('hv.nics.lead', { n: rows.length }), table(
      [t('hv.col.guest'), t('hv.col.node'), t('hv.col.card'), t('hv.col.bridge'), t('hv.col.vlan'), t('hv.col.firewall'), t('hv.col.rate'), t('hv.col.link')],
      rows.map(({ g, n }) => ({ cells: [
        h('span', { class: 'hv-guest' }, h('strong', {}, g.name || '?'), g.vmid != null ? h('span', { class: 'muted mono' }, ' ' + g.vmid) : null),
        hname(g.host), h('span', { class: 'mono' }, n.name), h('span', { class: 'mono' }, n.bridge || '—'),
        n.vlan != null ? String(n.vlan) : (n.trunks && n.trunks.length ? t('hv.trunk', { vlans: n.trunks.join(', ') }) : '—'),
        flag(!!n.firewall), n.rate_mbs ? n.rate_mbs + ' MB/s' : '—',
        n.link_down ? h('span', { class: 'chip chip--warn' }, t('hv.link_down')) : '—' ] }))));
  }
  function bridgeLine(b) {
    return { cells: [
      h('span', { class: 'hv-guest' }, h('strong', { class: 'mono' }, b.name), b.mgmt ? h('span', { class: 'chip chip--accent' }, t('hv.mgmt')) : null),
      t('hv.bridge.kind.' + b.kind) === 'hv.bridge.kind.' + b.kind ? b.kind : t('hv.bridge.kind.' + b.kind),
      b.vlan_aware ? t('hv.vlan_aware', { vids: b.vids || '—' }) : '—',
      (b.ports || []).join(', ') || '—', mono(b.cidr),
      b.active === false ? h('span', { class: 'chip chip--warn' }, t('hv.inactive')) : '' ] };
  }
  function networkCard(s) {
    const body = [];
    const bridges = s.bridges || [];
    s.hosts.slice().sort(byName).forEach(x => {
      const own = bridges.filter(b => b.host === x.id).sort(byName);
      if (own.length) body.push(h('div', { class: 'hv-group' }, h('h3', { class: 'fw-sub' }, t('hv.scope.node', { name: x.name })),
        table([t('fw.col.name'), t('hv.col.kind'), t('hv.col.vlan'), t('hv.col.ports_if'), t('hv.col.address'), ''], own.map(bridgeLine), 'hv-bridges')));
    });
    const zones = s.sdn_zones || [], vnets = s.sdn_vnets || [];
    if (zones.length || vnets.length) {
      body.push(h('div', { class: 'hv-group' }, h('h3', { class: 'fw-sub' }, t('hv.sdn.title')),
        zones.length ? table([t('hv.col.zone'), t('hv.col.kind'), t('hv.col.bridge')], zones.slice().sort(byName).map(z => ({ cells: [h('strong', {}, z.name), z.type, mono(z.bridge)] })), 'hv-zones') : null,
        vnets.length ? table([t('hv.col.vnet'), t('hv.col.zone'), t('hv.col.tag'), t('hv.col.subnets')], vnets.slice().sort(byName).map(v => ({ cells: [
          h('span', { class: 'hv-guest' }, h('strong', {}, v.name), v.alias ? h('span', { class: 'muted' }, ' ' + v.alias) : null), v.zone_name || '—', v.tag != null ? String(v.tag) : '—',
          h('span', { class: 'mono hv-cut' }, (v.subnets || []).map(x => x.cidr).join(', ') || '—') ] })), 'hv-vnets') : null));
    }
    return body.length ? card(t('hv.net.title'), t('hv.net.lead'), ...body) : null;
  }

  LL.hvFirewallCards = function (s) {
    const out = [];
    if (s.findings && s.findings.length) out.push(card(t('hv.findings.title'), t('hv.findings.lead'), findingList(s.findings)));
    const opts = optionsLine(s), groups = ruleGroups(s), objs = objects(s);
    if (opts || groups.length || objs.length) out.push(card(t('hv.fw.title'), t('hv.fw.lead'), opts, ...groups, ...objs));
    const nics = nicsCard(s), net = networkCard(s);
    if (nics) out.push(nics);
    if (net) out.push(net);
    return out;
  };
  LL.hvMeta = function (s) {
    const c = s.controller || {};
    return [c.cluster ? t('hv.meta.cluster', { name: c.cluster }) : null, c.nodes ? t('hv.meta.nodes', { n: c.nodes }) : null].filter(Boolean);
  };

  // ------------------------------------------------------------------ device card
  const sec = (title, body) => h('section', { class: 'detail__sec' }, h('h3', {}, title), body);
  const dot = st => h('span', { class: 'hv-dot hv-dot--' + (st === 'running' ? 'on' : st === 'paused' ? 'warn' : 'off'), title: t('hv.status.' + st) === 'hv.status.' + st ? st : t('hv.status.' + st) });
  // sizes from one terabyte on in TB (storage of a hypervisor is often larger than the app's usual byte formatter goes)
  const size = b => (Number(b) >= 1e12 ? LL.fmtNum(Number(b) / 1e12, Number(b) >= 1e13 ? 0 : 1) + '\u00a0TB' : LL.fmtBytes(b));
  const memText = (u, tot) => size(u) + ' / ' + size(tot);
  const upText = s => s == null ? null : LL.fmtAge(s);
  const factsOf = rows => h('dl', { class: 'facts' }, rows.filter(r => r && r[1] != null && r[1] !== '').map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]).flat());
  function backupText(b) {
    if (!b || b.last_t == null) return t('hv.backup.none');
    return t('hv.backup.ago', { age: LL.fmtAge(b.age_s) });
  }

  LL.hvHostSection = function (d) {
    const x = d.host || {};
    const kids = [factsOf([
      [t('hv.f.version'), d.version || x.version], [t('hv.f.cluster'), d.cluster], [t('hv.f.cpu'), x.cpu != null ? pct(x.cpu) + (x.cores ? ' · ' + t('hv.cores', { n: x.cores }) : '') : null],
      [t('hv.f.ram'), x.mem_total ? memText(x.mem_used, x.mem_total) : null], [t('hv.f.load'), x.load && x.load.length ? x.load.map(v => LL.fmtNum(Number(v), 2)).join(' · ') : null],
      [t('hv.f.uptime'), upText(x.uptime_s)], [t('hv.f.guests'), t('hv.guests_of', { r: x.guests_running != null ? x.guests_running : 0, n: x.guests_total != null ? x.guests_total : (d.guests || []).length })],
      [t('detail.age'), d.age_s != null ? LL.fmtAge(d.age_s) : null]])];
    if (d.findings && d.findings.length) kids.push(h('div', { class: 'hv-sub' }, findingList(d.findings)));
    const gs = (d.guests || []).slice().sort((a, b) => (a.vmid || 0) - (b.vmid || 0));
    if (gs.length) kids.push(h('div', { class: 'hv-sub' }, h('h4', {}, t('hv.guests_title', { n: gs.length })), h('ul', { class: 'hv-glist' }, gs.map(g => {
      const n0 = (g.nics || [])[0];
      const net = n0 ? [n0.bridge, n0.vlan != null ? 'VLAN ' + n0.vlan : null].filter(Boolean).join(' · ') : '';
      return h('li', { class: 'hv-grow' }, dot(g.status),
        h('div', { class: 'hv-grow__main' }, h('strong', { class: 'hv-cut' }, g.name), h('span', { class: 'muted hv-cut' }, [t('hv.kind.' + g.kind), g.vmid, net].filter(x => x !== '' && x != null).join(' · '))),
        h('span', { class: 'hv-grow__bk' + (g.backup && g.backup.last_t != null ? '' : ' is-none') }, backupText(g.backup)));
    }))));
    const st = d.storage || [];
    if (st.length) kids.push(h('div', { class: 'hv-sub' }, h('h4', {}, t('hv.storage')), h('ul', { class: 'hv-stor' }, st.map(s => {
      const f = Math.max(0, Math.min(1, s.fraction || 0));
      return h('li', { class: 'hv-stor__row' + (f >= 0.9 ? ' is-full' : '') },
        h('div', { class: 'hv-stor__top' }, h('strong', { class: 'hv-cut' }, s.name), h('span', { class: 'muted' }, s.total ? memText(s.used, s.total) : '—')),
        h('div', { class: 'hv-bar', role: 'img', 'aria-label': pct(f) }, h('i', { style: 'width:' + Math.round(f * 100) + '%' })));
    }))));
    const br = (d.bridges || []).filter(b => b.kind !== 'eth');      // physical ports are listed as ports of their bridge or bond
    if (br.length) kids.push(h('div', { class: 'hv-sub' }, h('h4', {}, t('hv.bridges')), h('ul', { class: 'plain-list' }, br.map(b =>
      h('li', {}, h('span', { class: 'mono' }, b.name), ' ', h('span', { class: 'muted' }, [b.ports && b.ports.length ? '→ ' + b.ports.join(', ') : null, b.cidr, b.vlan_aware ? t('hv.vlan_aware', { vids: b.vids || '—' }) : null, b.mgmt ? t('hv.mgmt') : null].filter(Boolean).join(' · ')))))));
    return sec(t('hv.card.host'), h('div', { class: 'hv-body' }, kids));
  };

  function spark(r) {
    const W = 300, H = 56, n = r.t.length;
    if (n < 2) return null;
    const ser = [r.in || [], r.out || []];
    const max = Math.max(1, ...ser.flat().map(v => Number(v) || 0));
    const path = a => a.map((v, i) => (i ? 'L' : 'M') + (i / (n - 1) * W).toFixed(1) + ' ' + (H - 2 - (Number(v) || 0) / max * (H - 6)).toFixed(1)).join('');
    const wrap = h('div', { class: 'hv-spark' });
    wrap.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img" aria-label="' + t('hv.traffic_hour').replace(/"/g, '') + '">'
      + '<path class="hv-spark__in" d="' + path(ser[0]) + '"/><path class="hv-spark__out" d="' + path(ser[1]) + '"/></svg>';
    return h('div', {}, wrap, h('div', { class: 'hv-spark__leg muted' }, h('span', { class: 'hv-spark__k hv-spark__k--in' }, '↓ ' + t('hv.in')), h('span', { class: 'hv-spark__k hv-spark__k--out' }, '↑ ' + t('hv.out')),
      h('span', {}, t('hv.peak', { v: LL.fmtRate(max) }))));
  }

  LL.hvGuestSection = function (d) {
    const g = d.guest || {};
    const kids = [factsOf([
      [t('hv.f.host'), d.host && d.host.name], [t('hv.f.status'), h('span', { class: 'hv-stat' }, dot(g.status), t('hv.status.' + g.status) === 'hv.status.' + g.status ? g.status : t('hv.status.' + g.status))],
      [t('hv.f.type'), t('hv.kind.' + g.kind) + (g.vmid != null ? ' · ' + g.vmid : '')],
      [t('hv.f.cpu'), g.status === 'running' && g.cpu != null ? pct(g.cpu) + (g.cores ? ' · ' + t('hv.cores', { n: g.cores }) : '') : (g.cores ? t('hv.cores', { n: g.cores }) : null)],
      [t('hv.f.ram'), g.mem_max ? (g.status === 'running' ? memText(g.mem, g.mem_max) : LL.fmtBytes(g.mem_max)) : null],
      [t('hv.f.disk'), g.disk_max ? LL.fmtBytes(g.disk_max) : null], [t('hv.f.uptime'), g.status === 'running' ? upText(g.uptime_s) : null],
      [t('hv.f.addresses'), (g.ips || []).length ? h('span', { class: 'mono' }, g.ips.join(', ')) : null],
      [t('hv.f.tags'), (g.tags || []).join(', ')]])];
    const nics = d.nics || [];
    if (nics.length) kids.push(h('div', { class: 'hv-sub' }, h('h4', {}, t('hv.nics.short')), h('ul', { class: 'plain-list' }, nics.map(n => h('li', {},
      h('span', { class: 'mono' }, n.name), ' ', h('span', { class: 'muted' }, [n.bridge, n.vlan != null ? 'VLAN ' + n.vlan : (n.trunks && n.trunks.length ? t('hv.trunk', { vlans: n.trunks.join(', ') }) : null),
        t('hv.fw_state', { s: t(n.firewall ? 'fw.state.on' : 'fw.state.off') }), n.link_down ? t('hv.link_down') : null].filter(Boolean).join(' · ')))))));
    const b = g.backup;
    kids.push(h('div', { class: 'hv-sub' }, h('h4', {}, t('hv.backup.title')), factsOf([
      [t('hv.backup.last'), backupText(b)],
      b && b.last_t != null ? [t('hv.backup.size'), b.size ? LL.fmtBytes(b.size) : null] : null,
      b && b.last_t != null ? [t('hv.backup.verified'), t(b.verified ? 'hv.yes' : 'hv.no')] : null,
      [t('hv.backup.job'), t(b && b.in_job ? 'hv.yes' : 'hv.no')],
      b && b.failed_t ? [t('hv.backup.failed'), t('hv.backup.ago', { age: LL.fmtAge(Math.max(0, Date.now() / 1000 - b.failed_t)) })] : null])));
    if (g.traffic || g.rrd) {
      kids.push(h('div', { class: 'hv-sub' }, h('h4', {}, t('hv.traffic')),
        g.traffic ? h('p', { class: 'hv-now' }, h('span', {}, '↓ ' + LL.fmtRate(g.traffic.in_bps)), h('span', {}, '↑ ' + LL.fmtRate(g.traffic.out_bps))) : null,
        g.rrd && g.rrd.t && g.rrd.t.length > 1 ? spark(g.rrd) : null));
    }
    const fw = d.firewall;
    if (fw || g.firewall != null) kids.push(h('div', { class: 'hv-sub' }, h('h4', {}, t('hv.fw.guest')), factsOf([
      [t('fw.col.state'), flag(fw ? !!fw.enabled : !!g.firewall)],
      fw ? [t('hv.col.policy_in'), fw.policy_in ? fw.policy_in.toLowerCase() : null] : null, fw ? [t('hv.col.policy_out'), fw.policy_out ? fw.policy_out.toLowerCase() : null] : null,
      [t('hv.fw.rules'), String((d.rules || []).length)]])));
    if (d.findings && d.findings.length) kids.push(h('div', { class: 'hv-sub' }, findingList(d.findings)));
    return sec(t('hv.card.guest'), h('div', { class: 'hv-body' }, kids));
  };
})();
