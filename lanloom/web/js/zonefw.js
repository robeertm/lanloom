/* Lanloom — zone firewall in the firewall editor (stage B): zones of their own, which zone a network is in, rules
   between zones (new, change, switch off and on, order, delete own ones). "May one reach the other?" (js/fwinsight.js)
   answers in the editor too, now and after the draft. Controls appear only for what the executor can do (capability keys from the
   server); nothing is offered greyed out. Every change is a draft entry: nothing is written before the preview, the
   approval and the executor's own checks (a blocking rule never names management or the gateway; the approver, Lanloom
   and the executor keep their way to the gateway and the controller).

   ctx: {change: bool, can(key), ops: draft entries {key: {kind, object, after}}, setOp(kind, object, after|null),
         dropOp(key), reload()} - the same as the gateway cards (netconf.js). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const ZF_KINDS = ['zone.create', 'zone.update', 'zone.delete', 'policy.create', 'policy.update', 'policy.delete', 'policy.reorder'];
  const opKey = (kind, obj) => kind + '|' + obj;
  const newId = () => 'new:' + Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, '0')).join('');
  const btn = (label, onclick, cls, extra) => h('button', Object.assign({ class: 'btn btn--sm ' + (cls || 'btn--ghost'), type: 'button', onclick }, extra || {}), label);
  const chip = (text, cls) => h('span', { class: 'chip ' + (cls || 'chip--muted') }, text);
  const PORTS = /^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$/;
  let last = null;                     // the state the cards were drawn from (names in the preview and the draft bar)

  function zoneName(s, id) { const z = ((s || {}).zones || []).find(x => x.id === id); return z ? z.name : '?'; }
  function netName(s, id) { const n = ((s || {}).networks || []).find(x => x.id === id); return n ? n.name : '?'; }
  function sideText(s, side) {
    if (!side || typeof side !== 'object') return '—';
    return zoneName(s, side.zone) + (side.net ? ' · ' + netName(s, side.net) : '');
  }
  LL.zfSideText = side => sideText(last, side);
  LL.zfZoneName = id => zoneName(last, id);
  LL.zfKinds = ZF_KINDS;
  const protoText = p => !p || p === 'any' ? t('zf.proto.any') : p === 'tcp_udp' ? t('nc.proto.both') : String(p).toUpperCase();
  function ruleText(s, p) {
    return sideText(s, p.src) + ' → ' + sideText(s, p.dst) + (p.proto && p.proto !== 'any' ? ' · ' + protoText(p.proto) : '') + (p.ports ? ' · ' + t('fw.pol.ports', { ports: p.ports }) : '');
  }

  // ------------------------------------------------------------------ labels (draft bar, progress, history)
  LL.zfOpLabel = function (s, o) {
    if (!ZF_KINDS.includes(o.kind)) return null;
    const a = o.after || {};
    const z = (s.zones || []).find(x => x.id === o.object) || {};
    const p = (s.policies || []).find(x => x.id === o.object) || {};
    if (o.kind === 'zone.create') return t('zf.chip.zone_new', { name: a.name || '?' });
    if (o.kind === 'zone.update') return t('zf.chip.zone_rename', { name: z.name || '?', new: a.name || '?' });
    if (o.kind === 'zone.delete') return t('zf.chip.zone_gone', { name: z.name || '?' });
    if (o.kind === 'policy.create') return t('zf.chip.rule_new', { name: a.name || '?' });
    if (o.kind === 'policy.delete') return t('zf.chip.rule_gone', { name: p.name || '?' });
    if (o.kind === 'policy.reorder') return t('zf.chip.rule_order', { name: p.name || '?' });
    if (Object.keys(a).length === 1 && 'enabled' in a) return t(a.enabled ? 'zf.chip.rule_on' : 'zf.chip.rule_off', { name: p.name || '?' });
    return t('zf.chip.rule_update', { name: p.name || '?' });
  };
  LL.zfSumText = function (x) {
    if (!ZF_KINDS.includes(x.kind)) return null;
    return x.name + ' · ' + t('zf.sum.' + x.kind.replace('.', '_'));
  };

  // ------------------------------------------------------------------ zones
  function zonesCard(v, ctx) {
    const s = v.state, zf = v.zonefw;
    const can = k => ctx.change && ctx.can(k);
    const ops = ctx.ops || {};
    const planned = Object.values(ops).filter(o => o.kind === 'zone.create');
    const moveOp = n => ops[opKey('network.update', n.id)];
    const rows = (s.zones || []).map(z => {
      const nets = (s.networks || []).filter(n => n.zone === z.id && n.purpose !== 'wan' && n.purpose !== 'vpn');
      const refs = (zf.refs || {})[z.id] || { networks: 0, rules: 0 };
      const ren = ops[opKey('zone.update', z.id)];
      const gone = ops[opKey('zone.delete', z.id)];
      const tools = [];
      const fixed = z.builtin || z.system;
      if (can('zone.update.name') && !fixed) tools.push(btn(t('zf.zone.rename'), () => zoneDialog(v, ctx, z)));
      if (can('zone.delete') && z.own && !fixed) {
        if (refs.networks || refs.rules) tools.push(h('span', { class: 'muted zf-hint' }, t('zf.zone.empty_first', { n: refs.networks, r: refs.rules })));
        else tools.push(btn(gone ? t('zf.undo') : t('zf.zone.delete'), () => gone ? ctx.dropOp(opKey('zone.delete', z.id)) : ctx.setOp('zone.delete', z.id, {}), gone ? 'btn--ghost' : 'btn--danger-ghost'));
      }
      return h('li', { class: 'zf-zone' + (gone ? ' is-gone' : '') },
        h('div', { class: 'zf-zone__head' },
          h('strong', { class: 'zf-zone__name' }, ren ? [h('s', {}, z.name), ' → ', ren.after.name] : z.name),
          fixed ? chip(t('zf.zone.builtin')) : null, z.own ? chip(t('zf.zone.own'), 'chip--accent') : null,
          gone ? chip(t('zf.planned_gone'), 'chip--warn') : null,
          tools.length ? h('div', { class: 'zf-zone__tools' }, tools) : null),
        h('div', { class: 'muted zf-zone__count' }, t('zf.zone.counts', { n: refs.networks, r: refs.rules })),
        nets.length ? h('ul', { class: 'zf-nets' }, nets.map(n => {
          const mv = moveOp(n);
          const target = mv && mv.after && mv.after.zone ? mv.after.zone : null;
          const pick = can('network.update.zone') && !n.management && (n.purpose === 'lan' || n.purpose === 'iot' || !n.purpose)
            ? netZoneSelect(v, ctx, n, target) : null;
          return h('li', { class: 'zf-net' + (target ? ' is-staged' : '') },
            h('span', { class: 'zf-net__name' }, n.name), n.cidr ? h('span', { class: 'muted mono zf-net__cidr' }, n.cidr) : null,
            n.management ? chip(t('zf.net.management')) : null,
            target ? chip('→ ' + zoneName(s, target), 'chip--warn') : null, pick);
        })) : h('p', { class: 'muted zf-nets__none' }, t('zf.zone.no_networks')));
    });
    planned.forEach(o => rows.push(h('li', { class: 'zf-zone is-new' },
      h('div', { class: 'zf-zone__head' }, h('strong', { class: 'zf-zone__name' }, o.after.name), chip(t('zf.planned_new'), 'chip--warn'),
        h('div', { class: 'zf-zone__tools' }, btn(t('zf.undo'), () => ctx.dropOp(opKey('zone.create', o.object))))),
      h('p', { class: 'muted zf-nets__none' }, t('zf.zone.new_hint')))));
    const head = [h('h2', {}, t('zf.zones.title'))];
    if (can('zone.create')) head.push(btn(t('zf.zone.new'), () => zoneDialog(v, ctx, null), 'btn--primary'));
    return h('section', { class: 'card zf-card zf-zones', id: 'zf-zones' },
      h('header', { class: 'zf-card__head' }, ...head),
      h('p', { class: 'muted' }, t('zf.zones.lead')),
      h('ul', { class: 'zf-zones__list' }, rows));
  }

  function netZoneSelect(v, ctx, n, target) {
    const s = v.state, zf = v.zonefw;
    const ids = (zf.targets || []).filter(id => id !== n.zone || target);
    const sel = h('select', { class: 'input zf-net__pick', 'aria-label': t('zf.net.move_label', { name: n.name }) },
      h('option', { value: '' }, t('zf.net.move')),
      ids.map(id => h('option', { value: id, selected: id === target }, id === n.zone ? t('zf.net.stay', { zone: zoneName(s, id) }) : zoneName(s, id))));
    sel.addEventListener('change', () => {
      const to = sel.value;
      if (!to || to === n.zone) return ctx.dropOp(opKey('network.update', n.id));
      const cur = (ctx.ops || {})[opKey('network.update', n.id)];
      return ctx.setOp('network.update', n.id, Object.assign({}, cur ? cur.after : {}, { zone: to }));
    });
    return sel;
  }

  function zoneDialog(v, ctx, z) {
    const s = v.state;
    const staged = z ? (ctx.ops || {})[opKey('zone.update', z.id)] : null;
    const nameIn = h('input', { class: 'input', maxlength: '64', value: staged ? staged.after.name : z ? z.name : '', placeholder: t('zf.zone.name_ph'), autocomplete: 'off' });
    const msg = h('div', { class: 'msg' });
    const save = h('button', { class: 'btn btn--primary', type: 'button' }, t(z ? 'zf.dlg.stage' : 'zf.zone.create'));
    save.addEventListener('click', async () => {
      const name = nameIn.value.trim();
      if (!name) { LL.msg(msg, t('zf.err.name'), 'error'); return; }
      if ((s.zones || []).some(x => x.id !== (z && z.id) && String(x.name).trim().toLowerCase() === name.toLowerCase())) { LL.msg(msg, t('zf.block.zone_name_taken'), 'error'); return; }
      dlg.close();
      if (z) await (name === z.name ? ctx.dropOp(opKey('zone.update', z.id)) : ctx.setOp('zone.update', z.id, { name }));
      else await ctx.setOp('zone.create', newId(), { name });
    });
    const dlg = LL.dialog(z ? t('zf.zone.rename_title', { name: z.name }) : t('zf.zone.new_title'),
      h('div', { class: 'fw-dev zf-dlg' }, LL.field(t('zf.zone.name'), nameIn), h('p', { class: 'muted fw-hint' }, t(z ? 'zf.zone.rename_hint' : 'zf.zone.new_lead')), msg), save);
    setTimeout(() => nameIn.focus(), 30);
  }

  // ------------------------------------------------------------------ rules: tools on each row of the rule list
  function ownOfPair(s, p) {
    return (s.policies || []).filter(q => !q.builtin && q.src.zone === p.src.zone && q.dst.zone === p.dst.zone).sort((a, b) => (a.order - b.order) || (a.id < b.id ? -1 : 1));
  }
  LL.zfRuleTools = function (s, p, ctx) {
    if (!ctx || !ctx.change || p.builtin || p.editable === false) return null;
    const ops = ctx.ops || {};
    const out = [];
    const upd = ops[opKey('policy.update', p.id)];
    const cur = Object.assign({}, p, upd ? upd.after : {});
    if (ctx.can('policy.update') || ctx.can('policy.update.enabled')) {
      const sw = h('input', { type: 'checkbox', checked: !!cur.enabled, 'aria-label': t('zf.rule.active', { name: p.name }) });
      sw.addEventListener('change', () => {
        const after = Object.assign({}, upd ? upd.after : {}, { enabled: sw.checked });
        if (after.enabled === !!p.enabled) delete after.enabled;
        return Object.keys(after).length ? ctx.setOp('policy.update', p.id, after) : ctx.dropOp(opKey('policy.update', p.id));
      });
      out.push(h('label', { class: 'check zf-rule__on', title: t('zf.rule.active', { name: p.name }) }, sw, h('span', { 'aria-hidden': 'true' }, t('zf.rule.on'))));
    }
    if (ctx.can('policy.update')) out.push(btn(t('zf.rule.edit'), () => ruleDialog(s, ctx, p), 'btn--ghost', { 'aria-label': t('zf.rule.edit_named', { name: p.name }) }));
    if (ctx.can('policy.reorder')) {
      const pair = ownOfPair(s, p);
      const i = pair.findIndex(q => q.id === p.id);
      const staged = ops[opKey('policy.reorder', p.id)];
      const pairBusy = Object.values(ops).some(o => o.kind === 'policy.reorder' && o.object !== p.id && pair.some(q => q.id === o.object));
      if (pair.length > 1 && !pairBusy) {
        if (staged) out.push(btn(t('zf.undo'), () => ctx.dropOp(opKey('policy.reorder', p.id)), 'btn--ghost', { title: t('zf.rule.order_undo') }));
        else {
          if (i > 0) out.push(btn('↑', () => ctx.setOp('policy.reorder', p.id, { order: pair[i - 1].order }), 'btn--ghost zf-btn-icon', { 'aria-label': t('zf.rule.up', { name: p.name }), title: t('zf.rule.up', { name: p.name }) }));
          if (i < pair.length - 1) out.push(btn('↓', () => ctx.setOp('policy.reorder', p.id, { order: pair[i + 1].order }), 'btn--ghost zf-btn-icon', { 'aria-label': t('zf.rule.down', { name: p.name }), title: t('zf.rule.down', { name: p.name }) }));
        }
      }
    }
    if (ctx.can('policy.delete') && p.own) {
      const gone = ops[opKey('policy.delete', p.id)];
      out.push(btn(gone ? t('zf.undo') : t('zf.rule.delete'), () => gone ? ctx.dropOp(opKey('policy.delete', p.id)) : ctx.setOp('policy.delete', p.id, {}), gone ? 'btn--ghost' : 'btn--danger-ghost'));
    }
    return out.length ? h('div', { class: 'zf-rule__tools' }, out) : null;
  };
  // the mark of a staged change on a row of the rule list
  LL.zfRuleMark = function (p, ctx) {
    if (!ctx || !ctx.ops) return null;
    const o = ctx.ops;
    if (o[opKey('policy.delete', p.id)]) return chip(t('zf.planned_gone'), 'chip--warn');
    if (o[opKey('policy.reorder', p.id)]) return chip(t('zf.planned_order'), 'chip--warn');
    const u = o[opKey('policy.update', p.id)];
    if (u) return chip(Object.keys(u.after).length === 1 && 'enabled' in u.after ? t(u.after.enabled ? 'zf.planned_on' : 'zf.planned_off') : t('zf.planned_change'), 'chip--warn');
    return null;
  };
  // new rules of the draft (not yet on the controller) and the "New rule" button, above the rule list
  LL.zfRuleDraft = function (s, ctx) {
    if (!ctx || !ctx.change) return null;
    const planned = Object.values(ctx.ops || {}).filter(o => o.kind === 'policy.create');
    const add = ctx.can('policy.create') ? btn(t('zf.rule.new'), () => ruleDialog(s, ctx, null), 'btn--primary') : null;
    if (!planned.length && !add) return null;
    return h('div', { class: 'zf-ruledraft' }, add,
      planned.length ? h('ul', { class: 'zf-ruledraft__list' }, planned.map(o => h('li', { class: 'fw-rule is-new' },
        h('span', { class: 'fw-rule__act fw-rule__act--' + o.after.action }, t('fw.pol.action.' + o.after.action)),
        h('div', { class: 'fw-rule__main' }, h('div', { class: 'fw-rule__name' }, o.after.name, ' ', chip(t('zf.planned_new'), 'chip--warn')),
          h('div', { class: 'fw-rule__meta' }, ruleText(s, o.after))),
        h('div', { class: 'fw-rule__side' }, btn(t('zf.rule.edit'), () => ruleDialog(s, ctx, null, o)), btn(t('zf.undo'), () => ctx.dropOp(opKey('policy.create', o.object))))))) : null);
  };

  function sideFields(s, side, label) {
    const zones = (s.zones || []);
    const zSel = h('select', { class: 'input' }, zones.map(z => h('option', { value: z.id, selected: z.id === side.zone }, z.name)));
    const nSel = h('select', { class: 'input' });
    const fill = () => {
      const nets = (s.networks || []).filter(n => n.zone === zSel.value);
      nSel.replaceChildren(h('option', { value: '' }, t('zf.rule.whole_zone')), ...nets.map(n => h('option', { value: n.id, selected: n.id === side.net }, n.name)));
      nSel.disabled = !nets.length;
    };
    zSel.addEventListener('change', fill);
    fill();
    return { el: h('fieldset', { class: 'zf-side' }, h('legend', {}, label), h('div', { class: 'nc-dlg__pair' }, LL.field(t('zf.rule.zone'), zSel), LL.field(t('zf.rule.network'), nSel))),
      value: () => (nSel.value ? { zone: zSel.value, net: nSel.value } : { zone: zSel.value }) };
  }

  function ruleDialog(s, ctx, p, draftOp) {
    const upd = p ? (ctx.ops || {})[opKey('policy.update', p.id)] : null;
    const cur = Object.assign({ name: '', action: 'block', proto: 'any', ports: '', return_traffic: false, enabled: true,
      src: { zone: ((s.zones || []).find(z => !z.system) || (s.zones || [])[0] || {}).id }, dst: { zone: ((s.zones || []).find(z => z.system === 'internal') || (s.zones || [])[0] || {}).id } },
    p || {}, upd ? upd.after : {}, draftOp ? draftOp.after : {});
    const nameIn = h('input', { class: 'input', maxlength: '64', value: cur.name, autocomplete: 'off', placeholder: t('zf.rule.name_ph') });
    const actSel = h('select', { class: 'input' }, ['allow', 'block', 'reject'].map(a => h('option', { value: a, selected: a === cur.action }, t('fw.pol.action.' + a))));
    const src = sideFields(s, cur.src || {}, t('zf.rule.from'));
    const dst = sideFields(s, cur.dst || {}, t('zf.rule.to'));
    const protoSel = h('select', { class: 'input' }, ['any', 'tcp', 'udp', 'tcp_udp'].map(x => h('option', { value: x, selected: x === cur.proto }, protoText(x))));
    const portsIn = h('input', { class: 'input mono', value: cur.ports || '', placeholder: '443,8000-8100', inputmode: 'numeric', autocomplete: 'off' });
    const portsField = LL.field(t('zf.rule.ports'), portsIn, t('zf.rule.ports_hint'));
    const ret = h('input', { type: 'checkbox', checked: !!cur.return_traffic });
    const showPorts = () => { portsField.hidden = protoSel.value === 'any'; };
    protoSel.addEventListener('change', showPorts);
    showPorts();
    const msg = h('div', { class: 'msg' });
    const save = h('button', { class: 'btn btn--primary', type: 'button' }, t('zf.dlg.stage'));
    save.addEventListener('click', async () => {
      const after = { name: nameIn.value.trim(), action: actSel.value, src: src.value(), dst: dst.value(), proto: protoSel.value,
        ports: protoSel.value === 'any' ? '' : portsIn.value.replace(/\s+/g, ''), return_traffic: ret.checked };
      if (!after.name) { LL.msg(msg, t('zf.err.name'), 'error'); return; }
      if (after.ports && !PORTS.test(after.ports)) { LL.msg(msg, t('nc.err.ports'), 'error'); return; }
      dlg.close();
      if (!p) {
        await ctx.setOp('policy.create', draftOp ? draftOp.object : newId(), after);
        return;
      }
      const diff = {};
      Object.keys(after).forEach(k => {
        const was = k === 'ports' ? (p.ports || '') : k === 'return_traffic' ? !!p.return_traffic : p[k];
        if (JSON.stringify(after[k]) !== JSON.stringify(was)) diff[k] = after[k];
      });
      if (upd && 'enabled' in upd.after && upd.after.enabled !== !!p.enabled) diff.enabled = upd.after.enabled;
      await (Object.keys(diff).length ? ctx.setOp('policy.update', p.id, diff) : ctx.dropOp(opKey('policy.update', p.id)));
    });
    const body = h('div', { class: 'fw-dev zf-dlg' },
      LL.field(t('zf.rule.name'), nameIn), LL.field(t('zf.rule.action'), actSel), src.el, dst.el,
      h('div', { class: 'nc-dlg__pair' }, LL.field(t('zf.rule.proto'), protoSel), portsField),
      h('label', { class: 'check' }, ret, h('span', {}, t('zf.rule.return'))),
      h('p', { class: 'muted fw-hint' }, t('zf.rule.hint')), msg);
    const dlg = LL.dialog(p ? t('zf.rule.edit_title', { name: p.name }) : t('zf.rule.new_title'), body, save);
    setTimeout(() => nameIn.focus(), 30);
  }

  // ------------------------------------------------------------------ the cards of the editor
  LL.zfCards = function (v, ctx) {
    last = v.state;
    if (!v.zonefw || v.mode === 'live') return [];
    return [zonesCard(v, ctx)];
  };
  LL.zfRemember = function (s) { last = s; };
  LL.zfNewRule = (s, ctx) => ruleDialog(s, ctx, null);
})();
