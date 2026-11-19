/* Lanloom — switch ports in the firewall editor (stage B): a port's name, its profile, the network it carries untagged,
   the networks it carries tagged, power over Ethernet on or off and a power cycle. Each switch is drawn as its faceplate
   (js/portfront.js); a port opens its panel with "Change port…". Controls appear only for what the executor can do
   (capability keys from the server) and never for infrastructure ports or ports that carry a protected device, the
   approver's device or Lanloom itself (the server says which: v.portconf.locked). Every change becomes a draft entry;
   nothing is written before the preview, the approval and the executor's own checks.

   ctx: {change: bool, can(key), ops: draft entries {key: {kind, object, after}}, setOp(kind, object, after|null),
         dropOp(key), operate: bool, reload()} */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t } = LL;
  const PC_KINDS = ['port.set', 'port.cycle'];
  const opKey = (kind, obj) => kind + '|' + obj;
  const btn = (label, onclick, cls) => h('button', { class: 'btn btn--sm ' + (cls || 'btn--ghost'), type: 'button', onclick }, label);
  const chip = (text, cls) => h('span', { class: 'chip ' + (cls || 'chip--muted') }, text);
  const LOCK = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
  let last = null;                                   // the state of the last view: names for the preview and the draft bar
  LL.pcRemember = s => { last = s || null; };
  LL.pcKinds = PC_KINDS;

  const byId = (list, id) => (list || []).find(x => x.id === id) || null;
  const netName = (s, id) => (byId((s || {}).networks, id) || {}).name;
  const profName = (s, id) => (byId((s || {}).port_profiles, id) || {}).name;
  const portOf = (s, id) => byId((s || {}).ports, id) || {};
  const swOf = (s, p) => byId((s || {}).equipment, p.device) || {};
  const portLabel = (s, p) => (swOf(s, p).name || '?') + ' · ' + (p.name || t('pf.port', { n: p.idx }));
  const poeOn = p => (p.poe && typeof p.poe === 'object') ? !!p.poe.on : !!p.poe;
  const poeCapable = p => (p.poe && typeof p.poe === 'object') ? p.poe.capable !== false : typeof p.poe === 'boolean';

  // ------------------------------------------------------------------ values in words (preview, draft bar, history)
  LL.pcValText = function (field, v) {
    const s = last;
    if (field === 'profile') return v ? (profName(s, v) || v) : t('pc.profile.none');
    if (field === 'native_network') return v ? (netName(s, v) || v) : t('pc.net.none');
    if (field === 'tagged_mode' && typeof v === 'string') return t('pc.tagged.' + v);
    if (field === 'tagged' && Array.isArray(v)) return v.length ? v.map(id => netName(s, id) || id).join(', ') : t('pc.tagged.empty');
    if (field === 'poe' && typeof v === 'boolean') return t(v ? 'pc.poe.on' : 'pc.poe.off');
    if (field === 'cycle') return t('pc.cycle.now');
    return null;
  };
  LL.pcOpLabel = function (s, o) {
    if (!PC_KINDS.includes(o.kind)) return null;
    const p = portOf(s, o.object), a = o.after || {};
    const where = portLabel(s, p);
    if (o.kind === 'port.cycle') return t('pc.chip.cycle', { name: where });
    if (Object.keys(a).length === 1 && 'poe' in a) return t(a.poe ? 'pc.chip.poe_on' : 'pc.chip.poe_off', { name: where });
    if (a.profile) return t('pc.chip.profile', { name: where, profile: profName(s, a.profile) || '?' });
    if (a.native_network) return t('pc.chip.native', { name: where, net: netName(s, a.native_network) || '?' });
    if (Object.keys(a).length === 1 && 'name' in a) return t('pc.chip.rename', { name: where, new: a.name || String(p.idx || '') });
    return t('pc.chip.update', { name: where });
  };
  LL.pcSumText = function (x) {
    if (!PC_KINDS.includes(x.kind)) return null;
    if (x.kind === 'port.cycle') return x.name + ' · ' + t('pc.sum.cycle');
    const parts = (x.fields || []).filter(f => f !== 'tagged' || !(x.fields || []).includes('tagged_mode'))
      .map(f => f === 'poe' ? t(x.on ? 'pc.sum.poe_on' : 'pc.sum.poe_off') : t('fw.field.' + f));
    return x.name + (parts.length ? ' · ' + parts.join(', ') : '');
  };

  // ------------------------------------------------------------------ the faceplates of the editor
  function frontOf(v, sw, ctx) {
    const s = v.state, pc = v.portconf || {};
    const devs = pc.devices || {};
    const ports = (s.ports || []).filter(p => p.device === sw.id).map(p => {
      const op = ctx.ops[opKey('port.set', p.id)], cyc = ctx.ops[opKey('port.cycle', p.id)];
      const cur = Object.assign({}, p, op ? op.after : {});
      if (op && 'poe' in op.after) cur.poe = Object.assign({}, p.poe || {}, { on: op.after.poe });
      const native = byId(s.networks, cur.native_network);
      const list = devs[p.id] || [];
      return { id: p.id, idx: p.idx, name: cur.name || String(p.idx), link: !!p.link, speed: p.speed, duplex: p.duplex,
        sfp: /SFP/i.test(String(p.media || '') + ' ' + String(p.name || '')),
        poe: p.poe && typeof p.poe === 'object' ? { capable: p.poe.capable !== false, on: poeOn(cur), watts: p.poe.watts } : null,
        vlan: native ? native.vlan : cur.native_vlan, network: native ? native.name : null,
        profile: cur.profile ? profName(s, cur.profile) : null, tagged: cur.tagged_mode === 'all' || (cur.tagged || []).length > 0,
        tagged_mode: cur.tagged_mode, tagged_names: (cur.tagged || []).map(id => netName(s, id)).filter(Boolean),
        poe_mode: p.poe && p.poe.mode, uplink: !!p.is_uplink, neighbor: null,
        devices: list.slice(0, 6), n_devices: list.length, flaps: 0, errors: 0, events: 0, level: 'ok',
        planned: !!(op || cyc) };
    });
    return { id: 'pc-' + sw.id, name: sw.name, kind: sw.kind, model: sw.model, online: sw.online !== false, ports, radios: [] };
  }

  function panelTools(v, ctx, p) {
    const pc = v.portconf || {};
    const lock = (pc.locked || {})[p.id];
    const op = ctx.ops[opKey('port.set', p.id)], cyc = ctx.ops[opKey('port.cycle', p.id)];
    const out = [];
    if (lock) { const ico = h('span', { class: 'pc-lock__ico', 'aria-hidden': 'true' }); ico.innerHTML = LOCK; out.push(h('p', { class: 'muted pc-lock' }, ico, h('span', {}, t('pc.locked.' + lockKey(lock))))); }
    if (!ctx.change) {
      if (canAny(ctx) && !lock) out.push(h('p', { class: 'muted fw-hint' }, t('pc.hint.change_mode')));
      return out;
    }
    const tools = [];
    if (canAny(ctx)) tools.push(btn(t('pc.change'), () => portDialog(v, ctx, portOf(v.state, p.id)), 'btn--primary'));
    if (op) tools.push(btn(t('nc.undo'), () => ctx.dropOp(opKey('port.set', p.id))));
    if (cyc) tools.push(btn(t('pc.cycle.undo'), () => ctx.dropOp(opKey('port.cycle', p.id))));
    if (tools.length) out.push(h('div', { class: 'pc-tools' }, tools));
    return out;
  }
  // the server's refusal keys, in the words of the panel
  function lockKey(k) {
    return { 'fw.block.uplink': 'uplink', 'pc.block.wan': 'wan', 'pc.block.port_mode': 'port_mode', 'pc.block.infra': 'infra',
      'pc.block.lldp': 'lldp', 'pc.block.protected': 'protected', 'pc.block.self_path': 'self_path', 'pc.block.no_poe': 'other' }[k] || 'other';
  }
  const canAny = ctx => ['port.set.name', 'port.set.profile', 'port.set.native_network', 'port.set.tagged_mode', 'port.set.poe', 'port.cycle', 'port.set'].some(k => ctx.can(k));
  const canF = (ctx, f) => ctx.can('port.set.' + f) || ctx.can('port.set');

  LL.pcCards = function (v, ctx) {
    const s = v.state || {};
    if (!v.portconf || !(s.ports || []).length) return [];
    LL.pcRemember(s);
    const order = { switch: 0, router: 1, ap: 2 };
    const sws = (s.equipment || []).filter(e => (s.ports || []).some(p => p.device === e.id))
      .sort((a, b) => (order[a.kind] ?? 3) - (order[b.kind] ?? 3) || (a.name < b.name ? -1 : 1));
    const planned = Object.values(ctx.ops).filter(o => PC_KINDS.includes(o.kind));
    const body = sws.map(sw => h('article', { class: 'pc-switch' },
      LL.portFront ? LL.portFront(frontOf(v, sw, ctx), { extra: p => panelTools(v, ctx, p) }) : null));
    return [h('section', { class: 'card fw-extra pc-card', id: 'fw-ports' },
      h('header', { class: 'nc-card__head' }, h('h2', {}, t('pc.title')), planned.length ? chip(t('pc.planned_n', { n: planned.length }), 'chip--accent') : null),
      h('p', { class: 'muted fw-hint' }, t(ctx.change ? 'pc.lead_change' : 'pc.lead_view')), body)];
  };

  // ------------------------------------------------------------------ the dialog of one port
  function portDialog(v, ctx, port) {
    const s = v.state, pc = v.portconf || {};
    const op = ctx.ops[opKey('port.set', port.id)];
    const cur = Object.assign({}, port, op ? op.after : {});
    const lock = (pc.locked || {})[port.id];
    const msg = h('div', { class: 'msg' });
    const nameIn = h('input', { class: 'input', type: 'text', maxlength: 32, value: cur.name || '', placeholder: String(port.idx) });
    const nets = (s.networks || []).filter(n => !['wan', 'vpn'].includes(n.purpose));
    const profs = pc.profiles || [];
    const canProfile = !lock && canF(ctx, 'profile') && profs.length > 0;
    const canNative = !lock && canF(ctx, 'native_network') && port.native_network != null;
    const canTagged = !lock && canF(ctx, 'tagged_mode') && port.tagged_mode != null;
    const profSel = h('select', { class: 'input' }, h('option', { value: '' }, t('pc.profile.none')),
      profs.map(x => h('option', { value: x.id, selected: cur.profile === x.id }, x.name)));
    const nativeSel = h('select', { class: 'input' }, nets.map(n => h('option', { value: n.id, selected: cur.native_network === n.id },
      [n.name, n.vlan != null ? t('nc.vlan', { n: n.vlan }) : null, n.management ? t('fw.net.management') : null].filter(Boolean).join(' · '))));
    const modeSel = h('select', { class: 'input' }, ['none', 'all', 'custom'].map(m => h('option', { value: m, selected: cur.tagged_mode === m }, t('pc.tagged.' + m))));
    const boxes = nets.filter(n => n.vlan != null).map(n => {
      const cb = h('input', { type: 'checkbox', value: n.id, checked: (cur.tagged || []).includes(n.id) });
      return { cb, el: h('label', { class: 'check pc-tag' }, cb, h('span', {}, [n.name, t('nc.vlan', { n: n.vlan })].join(' · '))) };
    });
    const tagList = h('div', { class: 'pc-tags' }, boxes.map(b => b.el));
    const prof = id => (s.port_profiles || []).find(x => x.id === id) || {};
    const poeDecided = () => typeof prof(profSel.value).poe === 'boolean';
    const canPoe = !lock && canF(ctx, 'poe') && poeCapable(port) && (!port.poe || ['auto', 'off', undefined, null].includes(port.poe.mode));
    const poeCb = h('input', { type: 'checkbox', checked: poeOn(cur) });
    const netBox = h('div', { class: 'pc-nets' });
    const profNote = h('p', { class: 'muted fw-hint' });
    const poeRow = canPoe ? h('label', { class: 'check pc-poe' }, poeCb, h('span', {}, t('pc.poe.label'))) : null;
    function sync() {
      const byProfile = canProfile && profSel.value;
      netBox.hidden = !!byProfile;
      if (byProfile) {
        const x = prof(profSel.value);
        profNote.textContent = t('pc.profile.decides', { net: netName(s, x.native_network) || '—', tagged: t('pc.tagged.' + (x.tagged_mode || 'none')) });
      } else profNote.textContent = '';
      profNote.hidden = !byProfile;
      tagList.hidden = modeSel.value !== 'custom';
      boxes.forEach(b => { const off = b.cb.value === nativeSel.value; b.el.hidden = off; if (off) b.cb.checked = false; });
      if (poeRow) { poeRow.hidden = poeDecided(); }
    }
    profSel.addEventListener('change', sync); modeSel.addEventListener('change', sync); nativeSel.addEventListener('change', sync);
    if (canNative) netBox.append(LL.field(t('pc.native.label'), nativeSel, t('pc.native.hint')));
    if (canTagged) netBox.append(LL.field(t('pc.tagged.label'), modeSel), tagList);
    const devs = (pc.devices || {})[port.id] || [];
    const canCycle = !lock && ctx.can('port.cycle') && poeCapable(port) && poeOn(port);
    const cycled = !!ctx.ops[opKey('port.cycle', port.id)];
    const body = h('div', { class: 'fw-dev nc-dlg pc-dlg' },
      h('p', { class: 'muted' }, devs.length ? t('pc.dlg.devices', { names: devs.map(d => d.name).join(', ') }) : t('pc.dlg.no_devices')),
      lock ? h('div', { class: 'callout' }, t('pc.locked.' + lockKey(lock)), ' ', t('pc.locked.name_only')) : null,
      canF(ctx, 'name') ? LL.field(t('pc.name.label'), nameIn, t('pc.name.hint')) : null,
      canProfile ? LL.field(t('pc.profile.label'), profSel) : null, profNote, netBox,
      poeRow,
      canCycle ? h('div', { class: 'pc-cycle' }, h('p', { class: 'muted fw-hint' }, t('pc.cycle.hint')),
        btn(t(cycled ? 'pc.cycle.staged' : 'pc.cycle.stage'), async () => { dlg.close(); await ctx.setOp('port.cycle', port.id, cycled ? null : { cycle: true }); }, 'btn--ghost')) : null,
      msg);
    sync();
    const save = h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: async () => {
      const after = {};
      if (canF(ctx, 'name')) after.name = nameIn.value.trim();
      if (canProfile) after.profile = profSel.value;
      if (!(canProfile && profSel.value)) {
        if (canNative) after.native_network = nativeSel.value;
        if (canTagged) {
          after.tagged_mode = modeSel.value;
          if (modeSel.value === 'custom') {
            after.tagged = boxes.filter(b => b.cb.checked && !b.el.hidden).map(b => b.cb.value).sort();
            if (!after.tagged.length) { LL.msg(msg, t('pc.err.tagged'), 'error'); return; }
          }
        }
      }
      if (canPoe && !poeDecided()) after.poe = poeCb.checked;
      const keep = {};
      Object.keys(after).forEach(k => {
        const now = k === 'poe' ? poeOn(port) : k === 'profile' ? (port.profile || '') : k === 'tagged' ? (port.tagged || []).slice().sort() : port[k];
        if (k === 'name' && (after.name === (port.name || '') || (!after.name && port.name === String(port.idx)))) return;
        if (JSON.stringify(after[k]) !== JSON.stringify(now)) keep[k] = after[k];
      });
      if (keep.tagged_mode && keep.tagged_mode !== 'custom') delete keep.tagged;
      dlg.close();
      await ctx.setOp('port.set', port.id, Object.keys(keep).length ? keep : null);
    } }, t('pc.stage'));
    const dlg = LL.dialog(t('pc.dlg.title', { name: portLabel(s, port) }), body, save);
    dlg.el.classList.add('modal--sheet');
    setTimeout(() => nameIn.focus(), 30);
  }
  LL.pcPortDialog = portDialog;

  // ------------------------------------------------------------------ command palette: "Change a switch port"
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'firewall-ports', group: 'commands', order: 48, label: 'pc.cmd.ports', keywords: 'pc.cmd.ports_keywords',
      available: () => LL.can('operate'), run: () => LL.go('#/firewall/ports') });
  }
})();
