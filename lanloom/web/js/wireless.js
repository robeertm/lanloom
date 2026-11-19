/* Lanloom — wireless networks (SSIDs) and blocked devices in the firewall editor (stage B). An SSID card per wireless
   network (name, network, bands, security, hidden, on/off; new ones; delete only the ones Lanloom created) and a card
   of blocked devices with "Block a device…". Controls appear only for what the executor can do (capability keys from
   the server) and never for an SSID that carries a protected device, the approver's device or Lanloom itself, nor for
   such a device (the server says which: v.wireless.ssids[id].locked, v.wireless.clients[id]). Radio settings of the access
   points are never offered. A password is never typed here: a new protected SSID gets a random one from the executor,
   read in the controller's own app. Every change becomes a draft entry; nothing is written before the preview, the
   approval and the executor's own checks.

   ctx: {change: bool, can(key), ops: draft entries {key: {kind, object, after}}, setOp(kind, object, after|null),
         dropOp(key), operate: bool, reload()} */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t } = LL;
  const WS_KINDS = ['ssid.create', 'ssid.update', 'ssid.delete', 'client.block'];
  const SECURITY = ['wpa2_wpa3_personal', 'wpa3_personal', 'wpa2_personal', 'open'];
  const BANDS = ['2.4ghz', '5ghz', '6ghz'];
  const opKey = (kind, obj) => kind + '|' + obj;
  const newId = () => 'new:' + Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, '0')).join('');
  const btn = (label, onclick, cls) => h('button', { class: 'btn btn--sm ' + (cls || 'btn--ghost'), type: 'button', onclick }, label);
  const DANGER = 'btn--ghost nc-danger';
  const chip = (text, cls) => h('span', { class: 'chip ' + (cls || 'chip--muted') }, text);
  let last = null;
  LL.wsRemember = s => { last = s || null; };
  LL.wsKinds = WS_KINDS;

  const byId = (list, id) => (list || []).find(x => x.id === id) || null;
  const netName = (s, id) => (byId((s || {}).networks, id) || {}).name;
  const bandText = b => String(b).replace('ghz', ' GHz');
  const secText = v => { const k = 'fw.sec.' + String(v || 'other').replace(/[^a-z0-9_]/g, '_'); return LL.strings[k] ? t(k) : String(v || '—'); };

  // ------------------------------------------------------------------ values in words (preview, draft bar, history)
  LL.wsValText = function (field, v) {
    if (field === 'bands' && Array.isArray(v)) return v.map(bandText).join(', ');
    if (field === 'security' && typeof v === 'string') return secText(v);
    if (field === 'hidden' && typeof v === 'boolean') return t(v ? 'ws.val.hidden' : 'ws.val.visible');
    if (field === 'blocked' && typeof v === 'boolean') return t(v ? 'ws.val.blocked' : 'ws.val.unblocked');
    if (field === 'network' && typeof v === 'string' && last && byId(last.networks, v)) return netName(last, v);
    return null;
  };
  LL.wsOpLabel = function (s, o) {
    if (!WS_KINDS.includes(o.kind)) return null;
    const a = o.after || {};
    if (o.kind === 'client.block') return t(a.blocked ? 'ws.chip.block' : 'ws.chip.unblock', { name: ((byId(s.devices, o.object) || {}).name) || '?' });
    const ss = byId(s.ssids, o.object) || {};
    if (o.kind === 'ssid.create') return t('ws.chip.new', { name: a.name || '?' });
    if (o.kind === 'ssid.delete') return t('ws.chip.gone', { name: ss.name || '?' });
    if (Object.keys(a).length === 1 && 'enabled' in a) return t(a.enabled ? 'ws.chip.on' : 'ws.chip.off', { name: ss.name || '?' });
    if (Object.keys(a).length === 1 && 'name' in a) return t('ws.chip.rename', { name: ss.name || '?', new: a.name });
    return t('ws.chip.update', { name: ss.name || '?' });
  };
  LL.wsSumText = function (x) {
    if (!WS_KINDS.includes(x.kind)) return null;
    if (x.kind === 'client.block') return x.name + ' · ' + t(x.on ? 'ws.sum.blocked' : 'ws.sum.unblocked');
    if (x.kind === 'ssid.create') return x.name + ' · ' + t('ws.sum.new');
    if (x.kind === 'ssid.delete') return x.name + ' · ' + t('ws.sum.gone');
    const parts = (x.fields || []).map(f => f === 'enabled' ? t(x.on ? 'ws.sum.on' : 'ws.sum.off') : t('fw.field.' + f));
    return x.name + (parts.length ? ' · ' + parts.join(', ') : '');
  };

  // ------------------------------------------------------------------ the SSID card
  const lockText = k => t({ 'ws.block.protected': 'ws.locked.protected', 'ws.block.self_path': 'ws.locked.self_path',
    'fw.block.builtin': 'ws.locked.builtin' }[k] || 'ws.locked.other');

  function ssidRow(v, ctx, w) {
    const s = v.state, info = ((v.wireless || {}).ssids || {})[w.id] || {};
    const upd = ctx.ops[opKey('ssid.update', w.id)], del = ctx.ops[opKey('ssid.delete', w.id)];
    const cur = Object.assign({}, w, upd ? upd.after : {});
    const tools = [];
    if (ctx.change && info.editable !== false && (v.wireless || {}).ssid_write) {
      if (upd) tools.push(btn(t('nc.undo'), () => ctx.dropOp(opKey('ssid.update', w.id))));
      if (del) tools.push(btn(t('nc.undo'), () => ctx.dropOp(opKey('ssid.delete', w.id))));
      if (!del && canAnyUpdate(ctx)) tools.push(btn(t('ws.edit'), () => ssidDialog(v, ctx, w)));
      if (!del && !upd && !info.locked && canF(ctx, 'enabled'))
        tools.push(btn(t(cur.enabled === false ? 'ws.switch_on' : 'ws.switch_off'), () => ctx.setOp('ssid.update', w.id, { enabled: cur.enabled === false })));
      if (!del && !upd && w.own && !info.locked && ctx.can('ssid.delete')) tools.push(btn(t('ws.delete'), () => ctx.setOp('ssid.delete', w.id, {}), DANGER));
    }
    const facts = [netName(s, cur.network) ? t('ws.fact.network', { net: netName(s, cur.network) }) : t('fw.ssid.native'),
      (cur.bands || []).map(bandText).join(' + ') || null, secText(cur.security),
      t('ws.fact.clients', { n: info.n || 0 })].filter(Boolean).join(' · ');
    return h('li', { class: 'hv-edit nc-row ws-row' + (del ? ' is-gone' : '') + (upd ? ' is-planned' : '') + (cur.enabled === false ? ' is-off' : '') },
      h('div', { class: 'hv-edit__head' },
        h('div', { class: 'hv-edit__who' }, h('span', { class: 'ws-ico', 'aria-hidden': 'true' }), h('strong', {}, del ? h('s', {}, w.name) : cur.name)),
        h('div', { class: 'hv-edit__chips' },
          cur.enabled === false ? chip(t('fw.state.off'), 'chip--warn') : null,
          cur.hidden ? chip(t('fw.ssid.hidden')) : null, w.guest ? chip(t('fw.ssid.guest')) : null,
          cur.security === 'open' ? chip(t('ws.open'), 'chip--warn') : null,
          w.own ? chip(t('nc.net.own'), 'chip--accent') : null,
          upd ? chip(t('nc.planned'), 'chip--accent') : null, del ? chip(t('nc.planned_gone'), 'chip--warn') : null),
        tools.length ? h('div', { class: 'hv-edit__tools' }, tools) : null),
      h('div', { class: 'nc-row__facts muted' }, facts),
      info.locked && ctx.change ? h('p', { class: 'muted fw-hint ws-lock' }, lockText(info.locked)) : null);
  }
  const canAnyUpdate = ctx => ['name', 'network', 'bands', 'security', 'hidden'].some(f => ctx.can('ssid.update.' + f)) || ctx.can('ssid.update');
  const canF = (ctx, f) => ctx.can('ssid.update.' + f) || ctx.can('ssid.update');

  function ssidCard(v, ctx) {
    const s = v.state, wf = v.wireless || {};
    const rows = (s.ssids || []).map(w => ssidRow(v, ctx, w));
    Object.values(ctx.ops).filter(o => o.kind === 'ssid.create').forEach(o => {
      const a = o.after || {};
      rows.push(h('li', { class: 'hv-edit nc-row ws-row is-planned is-new' },
        h('div', { class: 'hv-edit__head' },
          h('div', { class: 'hv-edit__who' }, h('span', { class: 'ws-ico', 'aria-hidden': 'true' }), h('strong', {}, a.name)),
          h('div', { class: 'hv-edit__chips' }, chip(t('nc.planned_new'), 'chip--accent')),
          ctx.change ? h('div', { class: 'hv-edit__tools' }, btn(t('ws.edit'), () => ssidDialog(v, ctx, null, o)), btn(t('nc.undo'), () => ctx.dropOp(opKey(o.kind, o.object)))) : null),
        h('div', { class: 'nc-row__facts muted' }, [t('ws.fact.network', { net: netName(s, a.network) || '?' }), (a.bands || []).map(bandText).join(' + '), secText(a.security)].join(' · '))));
    });
    const canNew = ctx.change && wf.ssid_write && ctx.can('ssid.create');
    const planned = Object.values(ctx.ops).filter(o => /^ssid\./.test(o.kind)).length;
    return h('section', { class: 'card fw-extra nc-card ws-card', id: 'fw-wireless' },
      h('header', { class: 'nc-card__head' }, h('h2', {}, t('ws.title', { n: (s.ssids || []).length })),
        planned ? chip(t('pc.planned_n', { n: planned }), 'chip--accent') : null,
        canNew ? btn(t('ws.new'), () => ssidDialog(v, ctx, null), 'btn--primary') : null),
      h('p', { class: 'muted fw-hint' }, t(ctx.change ? 'ws.lead_change' : 'ws.lead_view')),
      rows.length ? h('ul', { class: 'hv-edit__list' }, rows) : h('p', { class: 'muted' }, t('ws.none')));
  }

  // new SSID or a change of an existing one: name, network, bands, security, hidden
  function ssidDialog(v, ctx, w, staged) {
    const s = v.state, wf = v.wireless || {};
    const creating = !w;
    const base = creating ? Object.assign({ bands: ['2.4ghz', '5ghz'], security: 'wpa2_wpa3_personal', hidden: false }, staged ? staged.after : {})
      : Object.assign({}, w, (ctx.ops[opKey('ssid.update', w.id)] || {}).after || {});
    const info = creating ? {} : (wf.ssids || {})[w.id] || {};
    const locked = !!info.locked;
    const can = f => creating || (canF(ctx, f) && !(locked && ['name', 'network', 'bands', 'security'].includes(f)));
    const msg = h('div', { class: 'msg' });
    const nameIn = h('input', { class: 'input', type: 'text', maxlength: 32, value: base.name || '', autocomplete: 'off', disabled: !can('name') });
    const nets = wf.networks || [];
    const netSel = h('select', { class: 'input', disabled: !can('network') }, nets.map(n => h('option', { value: n.id, selected: base.network === n.id },
      [n.name, n.guest ? t('fw.ssid.guest') : null, n.management ? t('fw.net.management') : null].filter(Boolean).join(' · '))));
    if (!base.network) { const def = nets.find(n => !n.management && !n.guest) || nets[0]; if (def) netSel.value = def.id; }
    const bandBoxes = BANDS.map(b => { const cb = h('input', { type: 'checkbox', value: b, checked: (base.bands || []).includes(b), disabled: !can('bands') });
      return { cb, el: h('label', { class: 'check ws-band' }, cb, h('span', {}, bandText(b))) }; });
    const secKnown = SECURITY.includes(base.security);
    const secSel = h('select', { class: 'input', disabled: !can('security') || !secKnown },
      (secKnown ? SECURITY : [base.security].concat(SECURITY)).map(x => h('option', { value: x, selected: base.security === x }, secText(x))));
    const hidCb = h('input', { type: 'checkbox', checked: !!base.hidden, disabled: !can('hidden') });
    const note = h('p', { class: 'muted fw-hint' });
    const sync = () => {
      const open = secSel.value === 'open', six = bandBoxes[2].cb.checked;
      const net = nets.find(n => n.id === netSel.value) || {};
      note.textContent = open ? t(net.guest ? 'ws.dlg.open_guest' : 'ws.dlg.open_refused') : six && secSel.value !== 'wpa3_personal' ? t('ws.dlg.six_wpa3')
        : creating || (w && w.security === 'open') ? t('ws.dlg.password') : t('ws.dlg.password_kept');
      note.className = 'fw-hint ' + (open || (six && secSel.value !== 'wpa3_personal') ? 'callout callout--warn' : 'muted');
    };
    [secSel, netSel].concat(bandBoxes.map(b => b.cb)).forEach(el => el.addEventListener('change', sync));
    sync();
    const body = h('div', { class: 'fw-dev nc-dlg ws-dlg' },
      locked ? h('div', { class: 'callout' }, lockText(info.locked), ' ', t('ws.locked.hidden_only')) : null,
      LL.field(t('ws.dlg.name'), nameIn, t('ws.dlg.name_hint')),
      LL.field(t('ws.dlg.network'), netSel, t('ws.dlg.network_hint')),
      h('div', { class: 'field' }, h('span', { class: 'field__label' }, t('ws.dlg.bands')), h('div', { class: 'ws-bands' }, bandBoxes.map(b => b.el))),
      LL.field(t('ws.dlg.security'), secSel), note,
      h('label', { class: 'check' }, hidCb, h('span', {}, t('ws.dlg.hidden'))),
      h('p', { class: 'muted fw-hint' }, t('ws.dlg.radio')), msg);
    const save = h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: async () => {
      const after = { name: nameIn.value.trim(), network: netSel.value, bands: bandBoxes.filter(b => b.cb.checked).map(b => b.cb.value),
        security: secSel.value, hidden: hidCb.checked };
      if (!after.name) { LL.msg(msg, t('ws.err.name'), 'error'); return; }
      if (!after.bands.length) { LL.msg(msg, t('ws.err.bands'), 'error'); return; }
      if (!after.network) { LL.msg(msg, t('ws.err.network'), 'error'); return; }
      dlg.close();
      if (creating) { await ctx.setOp('ssid.create', staged ? staged.object : newId(), after); return; }
      const keep = {};
      Object.keys(after).forEach(k => { if (can(k) && JSON.stringify(after[k]) !== JSON.stringify(k === 'bands' ? (w.bands || []) : k === 'hidden' ? !!w.hidden : w[k])) keep[k] = after[k]; });
      if (!secKnown) delete keep.security;
      await ctx.setOp('ssid.update', w.id, Object.keys(keep).length ? keep : null);
    } }, t(creating ? 'ws.stage_new' : 'ws.stage'));
    const dlg = LL.dialog(creating ? t('ws.dlg.new_title') : t('ws.dlg.edit_title', { name: w.name }), body, save);
    dlg.el.classList.add('modal--sheet');
    setTimeout(() => nameIn.focus(), 30);
  }
  LL.wsNewSsid = (v, ctx) => ssidDialog(v, ctx, null);

  // ------------------------------------------------------------------ blocked devices
  function blockCard(v, ctx) {
    const s = v.state, wf = v.wireless || {};
    const devs = s.devices || [];
    const blocked = devs.filter(d => d.blocked);
    const planned = Object.values(ctx.ops).filter(o => o.kind === 'client.block');
    const rows = blocked.map(d => {
      const op = ctx.ops[opKey('client.block', d.id)];
      const tools = ctx.change && wf.block_write && ctx.can('client.block')
        ? [op ? btn(t('nc.undo'), () => ctx.dropOp(opKey('client.block', d.id))) : btn(t('ws.unblock'), () => ctx.setOp('client.block', d.id, { blocked: false }))] : [];
      return h('li', { class: 'hv-edit nc-row' + (op ? ' is-planned' : '') },
        h('div', { class: 'hv-edit__head' },
          h('div', { class: 'hv-edit__who' }, h('strong', {}, d.name || '?'), d.ip ? h('span', { class: 'muted mono' }, d.ip) : null),
          h('div', { class: 'hv-edit__chips' }, op ? chip(t('ws.planned_unblock'), 'chip--accent') : chip(t('ws.val.blocked'), 'chip--warn')),
          tools.length ? h('div', { class: 'hv-edit__tools' }, tools) : null),
        h('div', { class: 'nc-row__facts muted' }, [d.wired ? t('ws.fact.wired') : d.ssid ? t('ws.fact.ssid', { ssid: d.ssid }) : null,
          netName(s, d.network) ? t('ws.fact.network', { net: netName(s, d.network) }) : null].filter(Boolean).join(' · ') || '—'));
    });
    planned.filter(o => o.after.blocked).forEach(o => {
      const d = byId(devs, o.object) || {};
      rows.push(h('li', { class: 'hv-edit nc-row is-planned is-new' },
        h('div', { class: 'hv-edit__head' },
          h('div', { class: 'hv-edit__who' }, h('strong', {}, d.name || '?'), d.ip ? h('span', { class: 'muted mono' }, d.ip) : null),
          h('div', { class: 'hv-edit__chips' }, chip(t('ws.planned_block'), 'chip--accent')),
          ctx.change ? h('div', { class: 'hv-edit__tools' }, btn(t('nc.undo'), () => ctx.dropOp(opKey('client.block', o.object)))) : null)));
    });
    const canBlock = ctx.change && wf.block_write && ctx.can('client.block');
    return h('section', { class: 'card fw-extra nc-card ws-card', id: 'fw-blocked' },
      h('header', { class: 'nc-card__head' }, h('h2', {}, t('ws.block_title', { n: blocked.length })),
        canBlock ? btn(t('ws.block_pick'), () => blockDialog(v, ctx), 'btn--primary') : null),
      h('p', { class: 'muted fw-hint' }, t('ws.block_lead')),
      rows.length ? h('ul', { class: 'hv-edit__list' }, rows) : h('p', { class: 'muted' }, t('ws.block_none')));
  }

  // pick a device to block: a search over the client list; protected devices, the own device and Lanloom are not offered
  function blockDialog(v, ctx) {
    const s = v.state, wf = v.wireless || {};
    const q = h('input', { type: 'search', class: 'input', placeholder: t('fw.pick.search'), 'aria-label': t('fw.pick.search'), autocomplete: 'off' });
    const list = h('ul', { class: 'ws-pick' });
    const locked = d => !!(wf.clients || {})[d.id];
    const cands = (s.devices || []).filter(d => !d.blocked && !ctx.ops[opKey('client.block', d.id)])
      .sort((a, b) => locked(a) - locked(b) || (a.online === false) - (b.online === false) || String(a.name).localeCompare(String(b.name)));
    const draw = () => {
      const needle = q.value.trim().toLowerCase();
      const hits = cands.filter(d => !needle || [d.name, d.ip, d.ssid].some(x => String(x || '').toLowerCase().includes(needle))).slice(0, 60);
      list.replaceChildren(...hits.map(d => {
        const why = (wf.clients || {})[d.id];
        return h('li', { class: 'ws-pick__row' + (why ? ' is-locked' : '') },
          h('div', { class: 'ws-pick__who' }, h('strong', {}, d.name || '?'),
            h('span', { class: 'muted' }, [d.ip, d.wired ? t('ws.fact.wired') : d.ssid ? t('ws.fact.ssid', { ssid: d.ssid }) : null, d.online === false ? t('ws.fact.offline') : null].filter(Boolean).join(' · '))),
          why ? h('span', { class: 'muted ws-pick__why' }, t(why === 'ws.block.self_block' ? 'ws.locked.own' : why === 'ws.block.self_path' ? 'ws.locked.self' : 'ws.locked.protected_dev'))
            : btn(t('ws.block'), async () => { dlg.close(); await ctx.setOp('client.block', d.id, { blocked: true }); }, DANGER));
      }));
      if (!hits.length) list.append(h('li', { class: 'muted' }, t('fw.pick.none')));
    };
    q.addEventListener('input', draw);
    draw();
    const body = h('div', { class: 'fw-dev nc-dlg ws-dlg' }, h('p', { class: 'muted' }, t('ws.block_dlg_lead')), q, list);
    const dlg = LL.dialog(t('ws.block_dlg_title'), body, null);
    dlg.el.classList.add('modal--sheet');
    setTimeout(() => q.focus(), 30);
  }
  LL.wsBlockDialog = blockDialog;

  // the block switch in a device's sheet of the editor (js/firewall.js): one line with the reason when it is not offered
  LL.wsDeviceTools = function (v, ctx, d) {
    const wf = v.wireless || {};
    if (!wf.block_write || !ctx.can('client.block') || !ctx.change) return null;
    const op = ctx.ops[opKey('client.block', d.id)];
    const why = (wf.clients || {})[d.id];
    const want = op ? op.after.blocked : !!d.blocked;
    if (why && !d.blocked) return h('p', { class: 'muted fw-hint' }, t(why === 'ws.block.self_block' ? 'ws.locked.own' : why === 'ws.block.self_path' ? 'ws.locked.self' : 'ws.locked.protected_dev'));
    return h('div', { class: 'ws-devtools' },
      op ? btn(t('nc.undo'), () => ctx.dropOp(opKey('client.block', d.id)))
        : btn(t(want ? 'ws.unblock' : 'ws.block'), () => ctx.setOp('client.block', d.id, { blocked: !want }), want ? 'btn--ghost' : DANGER),
      h('span', { class: 'muted fw-hint' }, t(want ? 'ws.dev.blocked_hint' : 'ws.dev.block_hint')));
  };

  LL.wsCards = function (v, ctx) {
    const s = v.state || {};
    const wf = v.wireless;
    if (!wf) return [];
    LL.wsRemember(s);
    const out = [];
    if (wf.ssid_write) out.push(ssidCard(v, ctx));
    if (wf.block_write) out.push(blockCard(v, ctx));
    return out;
  };

  // ------------------------------------------------------------------ command palette
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'firewall-wireless', group: 'commands', order: 49, label: 'ws.cmd.wireless', keywords: 'ws.cmd.wireless_keywords',
      available: () => LL.can('operate'), run: () => LL.go('#/firewall/wireless') });
    LL.registerCommand({ id: 'firewall-block', group: 'commands', order: 50, label: 'ws.cmd.block', keywords: 'ws.cmd.block_keywords',
      available: () => LL.can('operate'), run: () => LL.go('#/firewall/block') });
  }
})();
