/* Lanloom — gateway changes in the firewall editor (stage B): networks and VLANs, fixed addresses and port forwards,
   plus the segmentation plan (target network per device, "→ IoT" marks, progress, draft the possible moves) and what
   uses a device by its address. Controls appear only for what the executor can do (capability keys from the server);
   nothing is offered greyed out. Every change becomes a draft entry; nothing is written before the preview, the
   approval and the executor's own checks.

   ctx: {change: bool, can(key), ops: draft entries {key: {kind, object, after}}, setOp(kind, object, after|null),
         dropOp(key), canMove: bool, stageMoves({device: network}), operate: bool, reload()} */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const NC_KINDS = ['network.create', 'network.update', 'network.delete', 'dhcp.reserve', 'dhcp.release', 'forward.create', 'forward.update', 'forward.delete'];
  const opKey = (kind, obj) => kind + '|' + obj;
  const newId = () => 'new:' + Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, '0')).join('');
  const btn = (label, onclick, cls) => h('button', { class: 'btn btn--sm ' + (cls || 'btn--ghost'), type: 'button', onclick }, label);
  const chip = (text, cls) => h('span', { class: 'chip ' + (cls || 'chip--muted') }, text);
  const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
  const isIp = v => IPV4.test(v) && v.split('.').every(x => Number(x) <= 255);
  const PORTS = /^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$/;

  function protoText(p) { return p === 'tcp_udp' || !p ? t('nc.proto.both') : String(p).toUpperCase(); }
  function dhcpText(d) {
    if (!d || !d.on) return t('nc.dhcp.off');
    return d.range && d.range.length === 2 ? t('nc.dhcp.range', { from: d.range[0], to: d.range[1] }) : t('nc.dhcp.on');
  }
  // values in the preview: the address service as a range, a forward's protocol in words, the rest like guest values
  LL.ncValText = function (field, v) {
    const pc = LL.pcValText ? LL.pcValText(field, v) : null;      // switch port values (js/portconf.js)
    if (pc != null) return pc;
    const ws = LL.wsValText ? LL.wsValText(field, v) : null;      // wireless network and block values (js/wireless.js)
    if (ws != null) return ws;
    if (field === 'dhcp' && v && typeof v === 'object') return dhcpText(v);
    if ((field === 'src' || field === 'dst') && v && typeof v === 'object' && LL.zfSideText) return LL.zfSideText(v);
    if (field === 'zone' && typeof v === 'string' && v && LL.zfZoneName) return LL.zfZoneName(v);
    if (field === 'order' && typeof v === 'number') return '#' + v;
    if (field === 'proto' && v === 'any') return t('zf.proto.any');
    if (field === 'proto' && typeof v === 'string' && v) return protoText(v);
    return LL.hvValText(field, v);
  };
  LL.ncKinds = NC_KINDS;

  // ------------------------------------------------------------------ labels (draft bar, progress, history)
  LL.ncOpLabel = function (s, o) {
    if (!NC_KINDS.includes(o.kind)) return null;
    const a = o.after || {};
    const net = (s.networks || []).find(n => n.id === o.object) || {};
    const fwd = (s.port_forwards || []).find(f => f.id === o.object) || {};
    const res = (s.dhcp_reservations || []).find(r => r.id === o.object) || {};
    const devName = id => ((s.devices || []).find(d => d.id === id) || {}).name || res.name || '?';
    if (o.kind === 'network.create') return t('nc.chip.net_new', { name: a.name || '?' });
    if (o.kind === 'network.update' && Object.keys(a).length === 1 && a.zone && LL.zfZoneName) return t('zf.chip.net_zone', { name: net.name || '?', zone: LL.zfZoneName(a.zone) });
    if (o.kind === 'network.update') return t('nc.chip.net_update', { name: net.name || '?' });
    if (o.kind === 'network.delete') return t('nc.chip.net_gone', { name: net.name || '?' });
    if (o.kind === 'dhcp.reserve') return t('nc.chip.reserve', { name: devName(a.client), ip: a.ip || '?' });
    if (o.kind === 'dhcp.release') return t('nc.chip.release', { name: devName(res.client) });
    if (o.kind === 'forward.create') return t('nc.chip.fwd_new', { name: a.name || '?' });
    if (o.kind === 'forward.delete') return t('nc.chip.fwd_gone', { name: fwd.name || '?' });
    if (Object.keys(a).length === 1 && 'enabled' in a) return t(a.enabled ? 'nc.chip.fwd_on' : 'nc.chip.fwd_off', { name: fwd.name || '?' });
    return t('nc.chip.fwd_update', { name: fwd.name || '?' });
  };
  LL.ncSumText = function (x) {
    if (!NC_KINDS.includes(x.kind)) return null;
    return x.name + ' · ' + t('nc.sum.' + x.kind.replace('.', '_'), { ip: x.ip || '' });
  };

  // ------------------------------------------------------------------ "→ IoT" on a device tile, the sheet's sections
  LL.ncTileMark = function (v, d) {
    const p = v.segplan && v.segplan.devices && v.segplan.devices[d.id];
    if (!p || p.moved) return null;
    return h('span', { class: 'chip fw-tile__plan' + (p.planned ? ' is-planned' : ''), title: t(p.planned ? 'seg.tile.planned' : 'seg.tile.target', { net: p.label }) }, '→ ' + p.label);
  };
  LL.ncUses = function (v, d) {
    const list = (v.uses || {})[d.id] || [];
    if (!list.length) return null;
    return h('div', { class: 'nc-uses' }, h('div', { class: 'fw-dev__label' }, t('nc.uses.title')),
      h('ul', { class: 'nc-uses__list' }, list.map(u => h('li', {}, h('span', { class: 'chip chip--muted' }, t('nc.uses.via.' + (['integration', 'tracker', 'automation', 'lanloom'].includes(u.via) ? u.via : 'other'))), ' ', u.by))),
      h('p', { class: 'muted fw-hint' }, t('nc.uses.hint')));
  };

  // ------------------------------------------------------------------ networks
  function networksCard(v, ctx) {
    const s = v.state;
    const nc = v.netconf || {};
    const ops = ctx.ops;
    const nets = (s.networks || []).filter(n => n.purpose !== 'wan' && n.purpose !== 'vpn');
    const zone = id => ((s.zones || []).find(z => z.id === id) || {}).name;
    const refsText = r => {
      if (!r) return '';
      const bits = [];
      if (r.devices) bits.push(t('nc.refs.devices', { n: r.devices }));
      if (r.reservations) bits.push(t('nc.refs.reservations', { n: r.reservations }));
      if (r.forwards) bits.push(t('nc.refs.forwards', { n: r.forwards }));
      if (r.ssids) bits.push(t('nc.refs.ssids', { n: r.ssids }));
      if (r.ports) bits.push(t('nc.refs.ports', { n: r.ports }));
      return bits.length ? bits.join(' · ') : t('nc.refs.none');
    };
    const rows = nets.map(n => {
      const upd = ops[opKey('network.update', n.id)], del = ops[opKey('network.delete', n.id)];
      const cur = Object.assign({}, n, upd ? upd.after : {});
      const refs = (nc.refs || {})[n.id];
      const used = refs && Object.values(refs).some(x => x > 0);
      const local = !n.purpose || n.purpose === 'lan' || n.purpose === 'iot';
      const tools = [];
      if (ctx.change && local && !n.management) {
        if (upd) tools.push(btn(t('nc.undo'), () => ctx.dropOp(opKey('network.update', n.id))));
        if (del) tools.push(btn(t('nc.undo'), () => ctx.dropOp(opKey('network.delete', n.id))));
        if (!del && (ctx.can('network.update.name') || ctx.can('network.update.dhcp'))) tools.push(btn(t('nc.net.edit'), () => netDialog(v, ctx, n)));
        if (!del && !upd && n.own && !used && ctx.can('network.delete')) tools.push(btn(t('nc.net.delete'), () => ctx.setOp('network.delete', n.id, {}), 'btn--ghost nc-danger'));
      }
      return h('li', { class: 'hv-edit nc-row' + (del ? ' is-gone' : '') + (upd ? ' is-planned' : '') },
        h('div', { class: 'hv-edit__head' },
          h('div', { class: 'hv-edit__who' }, h('strong', {}, del ? h('s', {}, n.name) : cur.name),
            h('span', { class: 'muted mono' }, n.cidr || '—'), n.vlan != null ? chip(t('nc.vlan', { n: n.vlan })) : null),
          h('div', { class: 'hv-edit__chips' },
            n.management ? chip(t('fw.net.management')) : null,
            n.own ? chip(t('nc.net.own'), 'chip--accent') : null,
            upd ? chip(t('nc.planned'), 'chip--accent') : null, del ? chip(t('nc.planned_gone'), 'chip--warn') : null),
          tools.length ? h('div', { class: 'hv-edit__tools' }, tools) : null),
        h('div', { class: 'nc-row__facts muted' }, [t('nc.net.dhcp', { what: dhcpText(cur.dhcp) }), zone(n.zone) ? t('nc.net.zone', { zone: zone(n.zone) }) : null, refsText(refs)].filter(Boolean).join(' · ')));
    });
    Object.values(ops).filter(o => o.kind === 'network.create').forEach(o => {
      const a = o.after || {};
      rows.push(h('li', { class: 'hv-edit nc-row is-planned is-new' },
        h('div', { class: 'hv-edit__head' },
          h('div', { class: 'hv-edit__who' }, h('strong', {}, a.name), h('span', { class: 'muted mono' }, a.cidr || '—'), a.vlan != null ? chip(t('nc.vlan', { n: a.vlan })) : null),
          h('div', { class: 'hv-edit__chips' }, chip(t('nc.planned_new'), 'chip--accent')),
          ctx.change ? h('div', { class: 'hv-edit__tools' }, btn(t('nc.net.edit'), () => netDialog(v, ctx, null, o)), btn(t('nc.undo'), () => ctx.dropOp(opKey(o.kind, o.object)))) : null),
        h('div', { class: 'nc-row__facts muted' }, [t('nc.net.dhcp', { what: dhcpText(a.dhcp) }), zone(nc.default_zone) ? t('nc.net.zone', { zone: zone(nc.default_zone) }) : null].filter(Boolean).join(' · '))));
    });
    const canNew = ctx.change && ctx.can('network.create');
    return h('section', { class: 'card fw-extra nc-card', id: 'fw-networks' },
      h('header', { class: 'nc-card__head' }, h('h2', {}, t('nc.net.title', { n: nets.length })),
        canNew ? btn(t('nc.net.new'), () => netDialog(v, ctx, null), 'btn--primary') : null),
      h('p', { class: 'muted fw-hint' }, t(ctx.change ? 'nc.net.lead_change' : 'nc.net.lead_view')),
      h('ul', { class: 'hv-edit__list' }, rows));
  }

  // new network (from a template) or a change of an existing one (name, address service)
  function netDialog(v, ctx, net, staged, preset) {
    const nc = v.netconf || {};
    const creating = !net;
    const tpls = nc.templates || [];
    const base = staged ? staged.after : net ? Object.assign({}, net, (ctx.ops[opKey('network.update', net.id)] || {}).after || {}) : null;
    let tpl = tpls.find(x => preset && x.template === preset.template) || tpls[0] || {};
    const nameIn = h('input', { type: 'text', class: 'input', maxlength: '64', autocomplete: 'off' });
    const cidrIn = h('input', { type: 'text', class: 'input mono', autocomplete: 'off', spellcheck: 'false', inputmode: 'decimal' });
    const vlanIn = h('input', { type: 'number', class: 'input mono', min: '2', max: '4094', inputmode: 'numeric' });
    const dhcpCb = h('input', { type: 'checkbox' });
    const fromIn = h('input', { type: 'text', class: 'input mono', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', 'aria-label': t('nc.dhcp.from') });
    const toIn = h('input', { type: 'text', class: 'input mono', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', 'aria-label': t('nc.dhcp.to') });
    const msg = h('div', { class: 'msg' });
    const fill = x => {
      nameIn.value = x.name != null ? x.name : '';
      cidrIn.value = x.cidr || '';
      vlanIn.value = x.vlan != null ? String(x.vlan) : '';
      const d = x.dhcp || { on: true, range: null };
      dhcpCb.checked = !!d.on;
      fromIn.value = d.range ? d.range[0] : ''; toIn.value = d.range ? d.range[1] : '';
      fromIn.disabled = toIn.disabled = !d.on;
    };
    dhcpCb.addEventListener('change', () => { fromIn.disabled = toIn.disabled = !dhcpCb.checked; });
    const fromTpl = x => Object.assign({}, x, { name: preset && preset.name ? preset.name : t(x.name_key) });
    fill(base || fromTpl(tpl));
    const tplRow = creating && !staged && tpls.length ? h('div', { class: 'seg nc-tpl', role: 'group', 'aria-label': t('nc.tpl.label') }, tpls.map(x =>
      h('button', { type: 'button', class: 'seg__btn' + (x === tpl ? ' is-active' : ''), 'aria-pressed': String(x === tpl), onclick: ev => {
        tpl = x; fill(fromTpl(x));
        ev.currentTarget.parentNode.querySelectorAll('.seg__btn').forEach(b => { b.classList.toggle('is-active', b === ev.currentTarget); b.setAttribute('aria-pressed', String(b === ev.currentTarget)); });
      } }, t('nc.tpl.' + x.template)))) : null;
    const canName = creating || ctx.can('network.update.name');
    const canDhcp = creating || ctx.can('network.update.dhcp');
    nameIn.disabled = !canName;
    const zone = ((v.state.zones || []).find(z => z.id === nc.default_zone) || {}).name;
    const body = h('div', { class: 'fw-dev nc-dlg' },
      tplRow,
      LL.field(t('nc.net.name'), nameIn),
      creating ? h('div', { class: 'nc-dlg__pair' }, LL.field(t('nc.net.cidr'), cidrIn, t('nc.net.cidr_hint')), LL.field(t('nc.net.vlan'), vlanIn, t('nc.net.vlan_hint'))) : null,
      canDhcp ? h('div', { class: 'nc-dlg__dhcp' }, h('label', { class: 'check' }, dhcpCb, h('span', {}, t('nc.dhcp.label'))),
        h('div', { class: 'nc-dlg__pair' }, LL.field(t('nc.dhcp.from'), fromIn), LL.field(t('nc.dhcp.to'), toIn))) : null,
      creating && zone ? h('p', { class: 'muted fw-hint' }, t('nc.net.lands_in', { zone })) : null,
      !creating && net && (v.netconf.refs || {})[net.id] && Object.values(v.netconf.refs[net.id]).some(x => x > 0) ? h('p', { class: 'muted fw-hint' }, t('nc.net.in_use_hint')) : null,
      msg);
    const save = h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: async () => {
      const after = {};
      const name = nameIn.value.trim();
      if (canName && !name) { LL.msg(msg, t('nc.err.name'), 'error'); return; }
      if (canName) after.name = name;
      if (creating) {
        const cidr = cidrIn.value.trim();
        if (!/^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/.test(cidr)) { LL.msg(msg, t('nc.err.cidr'), 'error'); return; }
        const vlan = Number(vlanIn.value);
        if (!Number.isInteger(vlan) || vlan < 2 || vlan > 4094) { LL.msg(msg, t('nc.err.vlan'), 'error'); return; }
        after.cidr = cidr; after.vlan = vlan; after.purpose = (tpl && tpl.purpose) || 'lan';
      }
      if (canDhcp) {
        const on = dhcpCb.checked, a = fromIn.value.trim(), b = toIn.value.trim();
        if (on && (a || b) && !(isIp(a) && isIp(b))) { LL.msg(msg, t('nc.err.range'), 'error'); return; }
        after.dhcp = { on, range: on && a && b ? [a, b] : null };
      }
      dlg.close();
      if (creating) await ctx.setOp('network.create', staged ? staged.object : newId(), after);
      else {
        const keep = {};
        Object.keys(after).forEach(k => { if (JSON.stringify(after[k]) !== JSON.stringify(k === 'dhcp' ? { on: !!(net.dhcp || {}).on, range: (net.dhcp || {}).range || null } : net[k])) keep[k] = after[k]; });
        await ctx.setOp('network.update', net.id, Object.keys(keep).length ? keep : null);
      }
    } }, t(creating ? 'nc.net.stage_new' : 'nc.net.stage'));
    const dlg = LL.dialog(creating ? t('nc.net.new_title') : t('nc.net.edit_title', { name: net.name }), body, save);
    dlg.el.classList.add('modal--sheet');
    setTimeout(() => nameIn.focus(), 30);
  }
  LL.ncNewNetwork = (v, ctx, preset) => netDialog(v, ctx, null, null, preset);

  // ------------------------------------------------------------------ fixed addresses
  const RES_FOLD = 8;
  let resOpen = false;
  function reservationsCard(v, ctx) {
    const s = v.state;
    const ops = ctx.ops;
    const devs = s.devices || [];
    const locked = r => !!((devs.find(x => x.id === r.client) || {}).protected);
    const list = (s.dhcp_reservations || []).slice().sort((a, b) => (locked(a) - locked(b)) || (a.name || '').localeCompare(b.name || ''));
    const netName = id => ((s.networks || []).find(n => n.id === id) || {}).name || '';
    const rows = list.map(r => {
      const d = devs.find(x => x.id === r.client) || {};
      const rel = ops[opKey('dhcp.release', r.id)];
      const tools = [];
      if (ctx.change && ctx.can('dhcp.release') && !d.protected) tools.push(rel ? btn(t('nc.undo'), () => ctx.dropOp(opKey('dhcp.release', r.id))) : btn(t('nc.res.release'), () => ctx.setOp('dhcp.release', r.id, {})));
      return h('li', { class: 'hv-edit__nic' + (rel ? ' is-planned nc-gone' : '') },
        h('strong', { class: 'hv-edit__what' }, rel ? h('s', {}, d.name || r.name || '?') : d.name || r.name || '?'),
        h('span', { class: 'mono hv-edit__val' }, r.ip), h('span', { class: 'muted' }, netName(r.network || d.network)),
        d.protected ? h('span', { class: 'chip chip--muted', title: t('fw.tile.stays_why', { reason: t(d.protected) }) }, t('fw.tile.stays')) : null,
        rel ? chip(t('nc.planned_gone'), 'chip--warn') : null,
        tools.length ? h('span', { class: 'hv-edit__tools' }, tools) : null);
    });
    Object.values(ops).filter(o => o.kind === 'dhcp.reserve').forEach(o => {
      const d = devs.find(x => x.id === (o.after || {}).client) || {};
      rows.push(h('li', { class: 'hv-edit__nic is-planned' }, h('strong', { class: 'hv-edit__what' }, d.name || '?'), h('span', { class: 'mono hv-edit__val' }, o.after.ip),
        h('span', { class: 'muted' }, netName(d.network)), chip(t('nc.planned_new'), 'chip--accent'),
        ctx.change ? h('span', { class: 'hv-edit__tools' }, btn(t('nc.undo'), () => ctx.dropOp(opKey(o.kind, o.object)))) : null));
    });
    const canNew = ctx.change && ctx.can('dhcp.reserve');
    // planned changes first; a long list folds after the first rows
    rows.sort((a, b) => (b.classList.contains('is-planned') ? 1 : 0) - (a.classList.contains('is-planned') ? 1 : 0));
    const ul = h('ul', { class: 'hv-edit__nics nc-list' + (resOpen ? '' : ' is-folded') }, rows);
    const more = rows.length > RES_FOLD ? btn(t(resOpen ? 'nc.res.fewer' : 'nc.res.all', { n: rows.length }), ev => {
      resOpen = !resOpen; ul.classList.toggle('is-folded', !resOpen); ev.currentTarget.textContent = t(resOpen ? 'nc.res.fewer' : 'nc.res.all', { n: rows.length });
    }, 'btn--ghost nc-more') : null;
    return h('section', { class: 'card fw-extra nc-card', id: 'fw-reservations' },
      h('header', { class: 'nc-card__head' }, h('h2', {}, t('nc.res.title', { n: list.length })), canNew ? btn(t('nc.res.new'), () => reserveDialog(v, ctx), 'btn--primary') : null),
      h('p', { class: 'muted fw-hint' }, t('nc.res.lead')),
      rows.length ? ul : h('p', { class: 'muted' }, t('nc.res.none')), more);
  }

  function reserveDialog(v, ctx) {
    const s = v.state;
    const taken = new Set((s.dhcp_reservations || []).map(r => r.client).concat(Object.values(ctx.ops).filter(o => o.kind === 'dhcp.reserve').map(o => o.after.client)));
    const nets = Object.fromEntries((s.networks || []).map(n => [n.id, n]));
    const devs = (s.devices || []).filter(d => !d.protected && !taken.has(d.id) && nets[d.network] && !['wan', 'vpn', 'guest'].includes(nets[d.network].purpose))
      .sort((a, b) => a.name.localeCompare(b.name));
    const sel = h('select', { class: 'input' }, [h('option', { value: '' }, t('nc.res.pick'))].concat(devs.map(d => h('option', { value: d.id }, d.name + (d.ip ? ' · ' + d.ip : '')))));
    const ipIn = h('input', { type: 'text', class: 'input mono', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false' });
    const hint = h('div', { class: 'field__hint' });
    const msg = h('div', { class: 'msg' });
    sel.addEventListener('change', () => { const d = devs.find(x => x.id === sel.value); ipIn.value = d && d.ip ? d.ip : ''; hint.textContent = d ? t('fw.dev.fixed_hint', { cidr: (nets[d.network] || {}).cidr || '' }) : ''; });
    const save = h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: async () => {
      const ip = ipIn.value.trim();
      if (!sel.value) { LL.msg(msg, t('nc.err.device'), 'error'); return; }
      if (!isIp(ip)) { LL.msg(msg, t('fw.dev.ip_invalid'), 'error'); return; }
      dlg.close();
      await ctx.setOp('dhcp.reserve', newId(), { client: sel.value, ip });
    } }, t('nc.res.stage'));
    const dlg = LL.dialog(t('nc.res.new_title'), h('div', { class: 'fw-dev nc-dlg' }, LL.field(t('nc.res.device'), sel), LL.field(t('fw.dev.fixed_ip'), ipIn), hint, msg), save);
    dlg.el.classList.add('modal--sheet');
    setTimeout(() => sel.focus(), 30);
  }

  // ------------------------------------------------------------------ port forwards
  function forwardsCard(v, ctx) {
    const s = v.state;
    const ops = ctx.ops;
    const fw = (s.port_forwards || []).slice().sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    const devByIp = ip => (s.devices || []).find(d => ip && (d.ip === ip || d.fixed_ip === ip));
    const line = (p, cur) => h('span', { class: 'nc-fwd__route' }, h('span', { class: 'mono' }, t('nc.fwd.outside', { port: cur.wan_port || '—' })), ' → ',
      h('span', { class: 'mono' }, (cur.dst_ip || '?') + (cur.dst_port && cur.dst_port !== cur.wan_port ? ':' + cur.dst_port : '')),
      devByIp(cur.dst_ip) ? h('span', { class: 'muted' }, ' · ' + devByIp(cur.dst_ip).name) : null);
    const rows = fw.map(p => {
      const upd = ops[opKey('forward.update', p.id)], del = ops[opKey('forward.delete', p.id)];
      const cur = Object.assign({}, p, upd ? upd.after : {});
      const tools = [];
      if (ctx.change) {
        if (upd || del) tools.push(btn(t('nc.undo'), () => ctx.dropOp(opKey(upd ? 'forward.update' : 'forward.delete', p.id))));
        if (!del && !upd && ctx.can('forward.update.enabled')) tools.push(btn(t(p.enabled ? 'nc.fwd.off' : 'nc.fwd.on'), () => ctx.setOp('forward.update', p.id, { enabled: !p.enabled })));
        if (!del && ['name', 'wan_port', 'dst_ip', 'dst_port', 'proto'].some(f => ctx.can('forward.update.' + f))) tools.push(btn(t('nc.fwd.edit'), () => fwdDialog(v, ctx, p)));
        if (!del && !upd && p.own && ctx.can('forward.delete')) tools.push(btn(t('nc.fwd.delete'), () => ctx.setOp('forward.delete', p.id, {}), 'btn--ghost nc-danger'));
      }
      return h('li', { class: 'hv-edit nc-row' + (cur.enabled ? '' : ' is-off') + (del ? ' is-gone' : '') + (upd ? ' is-planned' : '') },
        h('div', { class: 'hv-edit__head' },
          h('div', { class: 'hv-edit__who' }, h('strong', {}, del ? h('s', {}, p.name) : cur.name), chip(protoText(cur.proto))),
          h('div', { class: 'hv-edit__chips' }, chip(t(cur.enabled ? 'fw.state.on' : 'fw.state.off'), cur.enabled ? 'chip--warn' : 'chip--muted'),
            p.own ? chip(t('nc.fwd.own'), 'chip--accent') : null, upd ? chip(t('nc.planned'), 'chip--accent') : null, del ? chip(t('nc.planned_gone'), 'chip--warn') : null),
          tools.length ? h('div', { class: 'hv-edit__tools' }, tools) : null),
        h('div', { class: 'nc-row__facts' }, line(p, cur)));
    });
    Object.values(ops).filter(o => o.kind === 'forward.create').forEach(o => {
      const a = o.after || {};
      rows.push(h('li', { class: 'hv-edit nc-row is-planned is-new' },
        h('div', { class: 'hv-edit__head' }, h('div', { class: 'hv-edit__who' }, h('strong', {}, a.name), chip(protoText(a.proto))),
          h('div', { class: 'hv-edit__chips' }, chip(t('nc.planned_new'), 'chip--accent')),
          ctx.change ? h('div', { class: 'hv-edit__tools' }, btn(t('nc.fwd.edit'), () => fwdDialog(v, ctx, null, o)), btn(t('nc.undo'), () => ctx.dropOp(opKey(o.kind, o.object)))) : null),
        h('div', { class: 'nc-row__facts' }, line(a, a))));
    });
    const canNew = ctx.change && ctx.can('forward.create');
    return h('section', { class: 'card fw-extra nc-card', id: 'fw-forwards' },
      h('header', { class: 'nc-card__head' }, h('h2', {}, t('nc.fwd.title', { n: fw.length })), canNew ? btn(t('nc.fwd.new'), () => fwdDialog(v, ctx, null), 'btn--primary') : null),
      h('p', { class: 'muted fw-hint' }, t('nc.fwd.lead')),
      rows.length ? h('ul', { class: 'hv-edit__list' }, rows) : h('p', { class: 'muted' }, t('fw.fwd.none')));
  }

  function fwdDialog(v, ctx, fwd, staged) {
    const s = v.state;
    const creating = !fwd;
    const base = staged ? staged.after : fwd ? Object.assign({}, fwd, (ctx.ops[opKey('forward.update', fwd.id)] || {}).after || {}) : { name: '', proto: 'tcp', wan_port: '', dst_ip: '', dst_port: '' };
    const can = f => creating || ctx.can('forward.update.' + f);
    const nameIn = h('input', { type: 'text', class: 'input', maxlength: '64', autocomplete: 'off', value: base.name || '', disabled: !can('name') });
    const protoSel = h('select', { class: 'input', disabled: !can('proto') }, ['tcp', 'udp', 'tcp_udp'].map(p => h('option', { value: p, selected: (base.proto || 'tcp_udp') === p }, protoText(p))));
    const wanIn = h('input', { type: 'text', class: 'input mono', inputmode: 'numeric', autocomplete: 'off', value: base.wan_port || '', disabled: !can('wan_port') });
    const nets = Object.fromEntries((s.networks || []).map(n => [n.id, n]));
    const targets = (s.devices || []).filter(d => d.ip && !d.protected && nets[d.network] && !nets[d.network].management).sort((a, b) => a.name.localeCompare(b.name));
    const devSel = h('select', { class: 'input', disabled: !can('dst_ip'), 'aria-label': t('nc.fwd.device') }, [h('option', { value: '' }, t('nc.fwd.device_pick'))].concat(targets.map(d => h('option', { value: d.ip, selected: d.ip === base.dst_ip }, d.name + ' · ' + d.ip))));
    const ipIn = h('input', { type: 'text', class: 'input mono', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', value: base.dst_ip || '', disabled: !can('dst_ip') });
    const dstIn = h('input', { type: 'text', class: 'input mono', inputmode: 'numeric', autocomplete: 'off', value: base.dst_port || '', disabled: !can('dst_port'), placeholder: t('nc.fwd.same_port') });
    devSel.addEventListener('change', () => { if (devSel.value) ipIn.value = devSel.value; });
    const msg = h('div', { class: 'msg' });
    const body = h('div', { class: 'fw-dev nc-dlg' },
      LL.field(t('nc.fwd.name'), nameIn),
      h('div', { class: 'nc-dlg__pair' }, LL.field(t('nc.fwd.proto'), protoSel), LL.field(t('nc.fwd.wan_port'), wanIn, t('nc.fwd.wan_hint'))),
      LL.field(t('nc.fwd.device'), devSel),
      h('div', { class: 'nc-dlg__pair' }, LL.field(t('nc.fwd.dst_ip'), ipIn, t('nc.fwd.dst_hint')), LL.field(t('nc.fwd.dst_port'), dstIn)),
      h('p', { class: 'callout callout--warn nc-dlg__warn' }, t('nc.fwd.warn')), msg);
    const save = h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: async () => {
      const after = { name: nameIn.value.trim(), proto: protoSel.value, wan_port: wanIn.value.replace(/\s+/g, ''), dst_ip: ipIn.value.trim(), dst_port: dstIn.value.replace(/\s+/g, '') };
      if (!after.name) { LL.msg(msg, t('nc.err.name'), 'error'); return; }
      if (!PORTS.test(after.wan_port) || (after.dst_port && !PORTS.test(after.dst_port))) { LL.msg(msg, t('nc.err.ports'), 'error'); return; }
      if (!isIp(after.dst_ip)) { LL.msg(msg, t('fw.dev.ip_invalid'), 'error'); return; }
      dlg.close();
      if (creating) await ctx.setOp('forward.create', staged ? staged.object : newId(), after);
      else {
        const keep = {};
        Object.keys(after).forEach(k => { if (can(k) && String(after[k] || '') !== String(fwd[k] || '')) keep[k] = after[k]; });
        await ctx.setOp('forward.update', fwd.id, Object.keys(keep).length ? keep : null);
      }
    } }, t(creating ? 'nc.fwd.stage_new' : 'nc.fwd.stage'));
    const dlg = LL.dialog(creating ? t('nc.fwd.new_title') : t('nc.fwd.edit_title', { name: fwd.name }), body, save);
    dlg.el.classList.add('modal--sheet');
    setTimeout(() => nameIn.focus(), 30);
  }

  LL.ncCards = function (v, ctx) {
    const s = v.state || {};
    const out = [];
    const w = (v.capabilities && v.capabilities.write) || {};
    const any = prefix => Object.keys(w).some(k => k.startsWith(prefix));
    if (any('network.') && (s.networks || []).length) out.push(networksCard(v, ctx));
    if (any('dhcp.')) out.push(reservationsCard(v, ctx));
    if (any('forward.')) out.push(forwardsCard(v, ctx));
    return out;
  };

  // ------------------------------------------------------------------ segmentation plan
  LL.segCard = function (v, ctx) {
    const seg = v.segplan;
    const s = v.state || {};
    if (!seg || !(s.networks || []).length) return null;
    const can = ctx.operate;
    const pct = seg.total ? Math.round(seg.moved / seg.total * 100) : 0;
    const devs = Object.fromEntries((s.devices || []).map(d => [d.id, d]));
    const groups = (seg.targets || []).map(g => {
      const ids = Object.keys(seg.devices).filter(id => (g.net ? seg.devices[id].net === g.net : !seg.devices[id].net && seg.devices[id].label.toLowerCase() === g.label.toLowerCase()));
      const moves = {};
      ids.forEach(id => { if (seg.moves[id]) moves[id] = seg.moves[id]; });
      const n = Object.keys(moves).length;
      const tools = [];
      if (!g.net && ctx.change && ctx.can('network.create')) tools.push(btn(t('seg.create_net'), () => LL.ncNewNetwork(v, ctx, { name: g.label, template: /iot|smart/i.test(g.label) ? 'iot' : /cam/i.test(g.label) ? 'cameras' : 'lan' })));
      if (n && ctx.change && ctx.canMove) tools.push(btn(t('seg.draft_moves', { n }), () => ctx.stageMoves(moves), 'btn--primary'));
      const gp = g.total ? Math.round(g.moved / g.total * 100) : 0;
      return h('li', { class: 'hv-edit seg-group' + (g.moved === g.total ? ' is-done' : '') },
        h('div', { class: 'hv-edit__head' },
          h('div', { class: 'hv-edit__who' }, h('strong', {}, '→ ' + g.label), !g.net ? chip(t('seg.not_yet'), 'chip--warn') : null),
          h('span', { class: 'seg-group__count' }, t('seg.moved_of', { moved: g.moved, total: g.total })),
          tools.length ? h('div', { class: 'hv-edit__tools' }, tools) : null),
        h('div', { class: 'seg-bar', role: 'img', 'aria-label': t('seg.moved_of', { moved: g.moved, total: g.total }) }, h('i', { style: 'width:' + gp + '%' })),
        h('ul', { class: 'seg-devs' }, ids.map(id => {
          const p = seg.devices[id];
          return h('li', { class: 'seg-dev' + (p.moved ? ' is-moved' : '') }, p.moved ? h('span', { class: 'seg-dev__ok', 'aria-hidden': 'true' }, '✓') : null, (devs[id] || {}).name || '?');
        })));
    });
    return h('section', { class: 'card fw-extra seg-card', id: 'fw-plan' },
      h('header', { class: 'nc-card__head' }, h('h2', {}, t('seg.title')),
        can ? btn(t(seg.total ? 'seg.edit' : 'seg.start'), () => planDialog(v, ctx), seg.total ? 'btn--ghost' : 'btn--primary') : null),
      h('p', { class: 'muted fw-hint' }, t('seg.lead')),
      seg.total ? h('div', { class: 'seg-total' },
        h('div', { class: 'seg-total__text' }, h('strong', {}, t('seg.moved_of', { moved: seg.moved, total: seg.total })), h('span', { class: 'muted' }, ' · ' + pct + ' %'),
          seg.elsewhere ? h('span', { class: 'muted' }, ' · ' + t('seg.elsewhere', { n: seg.elsewhere })) : null),
        h('div', { class: 'seg-bar seg-bar--big', role: 'img', 'aria-label': pct + ' %' }, h('i', { style: 'width:' + pct + '%' }))) : h('p', { class: 'muted' }, t('seg.empty')),
      groups.length ? h('ul', { class: 'hv-edit__list' }, groups) : null);
  };

  // choose a target (an existing network or the name of one still to come) and tick the devices that should go there
  function planDialog(v, ctx) {
    const s = v.state;
    const seg = v.segplan || { devices: {} };
    const nets = (s.networks || []).filter(n => !n.management && n.purpose !== 'wan' && n.purpose !== 'vpn');
    const labels = [...new Set(Object.values(seg.devices).filter(p => !p.net).map(p => p.label))];
    const sel = h('select', { class: 'input' }, nets.map(n => h('option', { value: 'n:' + n.id }, n.name + ' · ' + n.cidr))
      .concat(labels.map(l => h('option', { value: 'l:' + l }, t('seg.dlg.planned', { name: l }))))
      .concat([h('option', { value: 'new' }, t('seg.dlg.new_label'))]));
    const labelIn = h('input', { type: 'text', class: 'input', maxlength: '32', autocomplete: 'off', placeholder: t('seg.dlg.label_ph'), hidden: true });
    const q = h('input', { type: 'search', class: 'input', placeholder: t('fw.pick.search'), 'aria-label': t('fw.pick.search'), autocomplete: 'off' });
    const list = h('div', { class: 'seg-pick' });
    const picked = {};
    const target = () => sel.value === 'new' ? (labelIn.value.trim() ? { label: labelIn.value.trim() } : null) : sel.value.startsWith('n:') ? { net: sel.value.slice(2) } : { label: sel.value.slice(2) };
    const same = (p, tg) => !!p && !!tg && (tg.net ? p.net === tg.net && !p.planned : !p.net && p.label.toLowerCase() === tg.label.toLowerCase());
    const netName = id => (nets.find(n => n.id === id) || (s.networks || []).find(n => n.id === id) || {}).name || '';
    const all = (s.devices || []).filter(d => !d.protected).sort((a, b) => a.name.localeCompare(b.name));
    const draw = () => {
      const tg = target();
      const needle = q.value.trim().toLowerCase();
      list.replaceChildren(...all.filter(d => !needle || d.name.toLowerCase().includes(needle) || String(d.ip || '').includes(needle)).slice(0, 200).map(d => {
        const p = seg.devices[d.id];
        const on = d.id in picked ? picked[d.id] : same(p, tg);
        const cb = h('input', { type: 'checkbox', checked: on, onchange: () => { picked[d.id] = cb.checked; } });
        return h('label', { class: 'check seg-pick__row' }, cb, h('span', { class: 'seg-pick__name' }, d.name),
          h('span', { class: 'muted seg-pick__meta' }, [netName(d.network), p && !same(p, tg) ? '→ ' + p.label : null].filter(Boolean).join(' · ')));
      }));
    };
    sel.addEventListener('change', () => { labelIn.hidden = sel.value !== 'new'; Object.keys(picked).forEach(k => delete picked[k]); draw(); if (!labelIn.hidden) labelIn.focus(); });
    labelIn.addEventListener('input', draw);
    q.addEventListener('input', draw);
    draw();
    const msg = h('div', { class: 'msg' });
    const save = h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: async () => {
      const tg = target();
      if (!tg) { LL.msg(msg, t('seg.err.label'), 'error'); return; }
      const targets = {};
      Object.entries(picked).forEach(([id, on]) => {
        const p = seg.devices[id];
        if (on) targets[id] = tg;
        else if (same(p, tg)) targets[id] = null;
      });
      if (!Object.keys(targets).length) { dlg.close(); return; }
      try { await api('/api/firewall/segplan', { method: 'PUT', body: { targets } }); dlg.close(); LL.toast(t('seg.saved'), 'ok'); ctx.reload(); }
      catch (e) { LL.msg(msg, e && e.key && LL.strings[e.key] ? t(e.key) : LL.errText(e), 'error'); }
    } }, t('seg.dlg.save'));
    const dlg = LL.dialog(t('seg.dlg.title'), h('div', { class: 'fw-dev seg-dlg' }, h('p', { class: 'muted' }, t('seg.dlg.lead')),
      LL.field(t('seg.dlg.target'), sel), labelIn, q, list, msg), save);
    dlg.el.classList.add('modal--sheet');
  }
  LL.segPlanDialog = planDialog;
})();
