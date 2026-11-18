/* Lanloom — Settings > Network: zones (networks and VLANs), the protection list, and what can be connected.
   Also the "Scan protection" and "Can be connected" sections of a device card. Reads only; the one thing that goes
   beyond reading is the user's explicit agreement to scan across a router (administrators). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const card = (title, kids, cls) => h('section', { class: 'card settings-card' + (cls ? ' ' + cls : '') }, h('h2', {}, title), kids);

  const REACH_CHIP = { scan: 'chip--ok', consent: 'chip--accent', adapter: 'chip--muted', blocked: 'chip--warn' };
  const CLASS_CHIP = { passive: 'chip--warn', industrial: 'chip--warn', printer: 'chip--accent' };
  const MARKS = ['printer', 'industrial', 'passive', 'none'];
  const STATUS_CHIP = { available: 'chip--ok', planned: 'chip--muted' };
  const ICONS = { hub: 'hub', mqtt: 'broker', snmp: 'router', controller: 'switch', hypervisor: 'server', nas: 'nas', printer: 'printer', camera: 'camera', dns_filter: 'service' };

  const zoneTitle = z => (z.name && z.vlan ? t('zone.net_named_vlan', { name: z.name, vlan: z.vlan, cidr: z.cidr })
    : z.name ? t('zone.net_named', { name: z.name, cidr: z.cidr }) : z.vlan ? t('zone.net_vlan', { vlan: z.vlan, cidr: z.cidr }) : z.cidr);

  // ------------------------------------------------------------------ zones
  function zoneRow(z, may, reload) {
    const counts = [t('zone.devices', { n: z.devices })];
    if (z.adapter_devices) counts.push(t('zone.from_adapter', { n: z.adapter_devices }));
    const facts = [];
    if (z.sources.includes('local')) facts.push(t('zone.src.local', { iface: z.iface || '' }));
    if (z.sources.includes('adapter')) facts.push(t('zone.src.adapter', { names: (z.adapters || []).filter(Boolean).join(', ') || '—' }));
    if (z.sources.includes('route') && !z.sources.includes('local')) facts.push(t('zone.src.route'));
    if (z.dhcp != null) facts.push(z.dhcp ? t('zone.dhcp.on') : t('zone.dhcp.off'));
    if (z.isolated) facts.push(t('zone.isolated'));
    let action = null;
    if (may && (z.reach === 'adapter' || z.reach === 'consent')) {
      const on = z.reach === 'adapter';
      action = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
        if (on && !confirm(t('zone.consent.confirm', { cidr: z.cidr }))) return;
        try { await api('/api/zones/consent', { method: 'POST', body: { cidr: z.cidr, on } }); LL.toast(t('settings.saved'), 'ok'); reload(); }
        catch (e) { LL.toast(LL.errText(e), 'error'); }
      } }, on ? t('zone.consent.allow') : t('zone.consent.withdraw'));
    }
    return h('li', { class: 'dz-zone' },
      h('div', { class: 'dz-zone__main' },
        h('div', { class: 'dz-zone__title' }, h('span', {}, zoneTitle(z)), h('span', { class: 'chip ' + (REACH_CHIP[z.reach] || 'chip--muted') }, t('zone.reach.' + z.reach))),
        h('div', { class: 'muted dz-zone__meta' }, counts.join(' · ')),
        facts.length ? h('div', { class: 'muted dz-zone__meta' }, facts.join(' · ')) : null,
        z.reason ? h('div', { class: 'muted dz-zone__meta' }, t(z.reason)) : null),
      action ? h('div', { class: 'dz-zone__act' }, action) : null);
  }

  async function zonesCard(admin) {
    const wrap = h('div', { class: 'stack-sm' });
    const body = h('div', {});
    async function render() {
      const data = await api('/api/zones');
      const list = data.zones.length ? h('ul', { class: 'plain-list dz-list' }, data.zones.map(z => zoneRow(z, admin, render)))
        : h('div', { class: 'empty-note' }, t('zone.none'));
      const adapterToggle = h('input', { type: 'checkbox', checked: !!data.adapter_inventory, disabled: !admin, onchange: async () => {
        try { await api('/api/settings', { method: 'PUT', body: { adapter_inventory: adapterToggle.checked } }); LL.toast(t('settings.saved'), 'ok'); render(); }
        catch (e) { LL.toast(LL.errText(e), 'error'); adapterToggle.checked = !adapterToggle.checked; }
      } });
      body.replaceChildren(list,
        data.no_address ? h('p', { class: 'muted fineprint' }, t('zone.no_address', { n: data.no_address })) : null,
        data.unzoned ? h('p', { class: 'muted fineprint' }, t('zone.unzoned', { n: data.unzoned })) : null,
        h('hr'), h('label', { class: 'check' }, adapterToggle, h('span', {}, t('zone.adapter_inventory'))),
        h('p', { class: 'muted fineprint' }, t('zone.adapter_inventory.hint')));
    }
    wrap.append(h('p', { class: 'muted' }, t('zone.lead')), body);
    await render();
    return card(t('zone.title'), wrap);
  }

  // ------------------------------------------------------------------ protection list
  function markSelect(dev, onDone) {
    const cur = dev.mark || (dev.source === 'mark' ? dev.class : '') || '';
    const sel = h('select', { class: 'select', 'aria-label': t('protect.change'), onchange: async () => {
      try { await api('/api/devices/' + encodeURIComponent(dev.id) + '/protect', { method: 'POST', body: { mark: sel.value || null } }); LL.toast(t('device.saved'), 'ok'); if (onDone) onDone(); }
      catch (e) { LL.toast(LL.errText(e), 'error'); }
    } }, [h('option', { value: '' }, t('protect.mark.auto'))].concat(MARKS.map(m => h('option', { value: m }, t('protect.mark.' + m)))));
    sel.value = cur;
    return sel;
  }

  async function protectCard(may) {
    const body = h('div', {});
    async function render() {
      const data = await api('/api/protect');
      const rows = (data.devices || []).map(d => h('li', { class: 'dz-prot' },
        h('div', { class: 'dz-prot__main' }, h('span', { class: 'dz-prot__name' }, d.title || d.ip || d.mac),
          h('span', { class: 'muted mono dz-prot__addr' }, [d.ip, d.mac].filter(Boolean).join(' · ')),
          h('span', { class: 'chip ' + (CLASS_CHIP[d.class] || 'chip--muted') }, t('protect.class.' + d.class)),
          h('span', { class: 'muted dz-prot__why' }, t(d.why || 'protect.why.mark'))),
        may ? markSelect({ id: d.id, class: d.class, source: d.source }, render) : null));
      body.replaceChildren(rows.length ? h('ul', { class: 'plain-list dz-list' }, rows) : h('div', { class: 'empty-note' }, t('protect.none')));
    }
    await render();
    return card(t('protect.title'), [h('p', { class: 'muted' }, t('protect.lead')),
      h('ul', { class: 'plain-list dz-rules' }, ['printer', 'industrial', 'passive'].map(c => h('li', {}, h('span', { class: 'chip ' + CLASS_CHIP[c] }, t('protect.class.' + c)), h('span', {}, t('protect.rule.' + c))))),
      body, h('p', { class: 'muted fineprint' }, t('protect.hint'))]);
  }

  // ------------------------------------------------------------------ what can be connected
  function guideBlock(it) {
    const own = it.status === 'planned';        // a planned class has its own steps, the others use the guide of their kind
    const items = [];
    for (let i = 1; i <= (it.steps || 4); i++) items.push(h('li', {}, own ? t(`catalog.${it.entry}.guide.${i}`) : t('setup.guide.' + it.guide + '.' + i)));
    return h('div', { class: 'setup-guide' }, h('h4', {}, own ? t('catalog.' + it.entry + '.guide.title') : t('setup.guide.' + it.guide + '.title')), h('ol', { class: 'setup-guide__steps' }, items),
      h('p', { class: 'muted fineprint' }, t('setup.guide.readonly')));
  }

  function itemCard(it, admin, onChange, compact) {
    const connectable = it.status === 'available' && it.kind && admin;
    const body = h('div', { class: 'setup-suggest__body', hidden: true });
    let btn = null;
    if (connectable) {
      const label = it.configured ? t('setup.suggest.again') : t('integ.setup');
      btn = h('button', { class: 'btn btn--' + (it.configured ? 'ghost' : 'primary') + ' btn--sm', type: 'button', 'aria-expanded': 'false', onclick: () => {
        if (!body.childNodes.length) body.append(guideBlock(it), LL.integrationCard(it.kind, null, onChange, { prefill: it.prefill, name: it.title, bare: true }));
        body.hidden = !body.hidden; btn.setAttribute('aria-expanded', String(!body.hidden)); btn.textContent = body.hidden ? label : t('integ.close');
      } }, label);
    } else if (it.status === 'planned') {
      btn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', 'aria-expanded': 'false', onclick: () => {
        if (!body.childNodes.length) body.append(guideBlock(it));
        body.hidden = !body.hidden; btn.setAttribute('aria-expanded', String(!body.hidden)); btn.textContent = body.hidden ? t('integrate.guide') : t('integ.close');
      } }, t('integrate.guide'));
    }
    const status = it.configured ? h('span', { class: 'chip chip--ok' }, t('setup.suggest.connected'))
      : h('span', { class: 'chip ' + (STATUS_CHIP[it.status] || 'chip--muted') }, it.status === 'planned' ? t('integrate.planned', { topic: t('catalog.topic.' + it.topic) }) : t('integrate.available'));
    const reasons = h('div', { class: 'chip-row' }, (it.reasons || []).map(r => h('span', { class: 'chip chip--muted' }, t(r.key, r.params || {}))));
    return h('article', { class: 'setup-suggest card card--flat' + (it.configured ? ' is-configured' : '') + (compact ? ' dz-int--compact' : '') },
      h('header', { class: 'setup-suggest__head' }, compact ? null : LL.kindIcon(ICONS[it.entry] || it.device_kind || 'unknown'),
        h('div', { class: 'setup-suggest__titles' }, h('h3', {}, t('catalog.' + it.entry + '.title')),
          compact ? null : h('p', { class: 'muted' }, h('span', {}, it.title), ' · ', h('span', { class: 'mono' }, it.ip || '—')),
          h('p', { class: 'muted dz-int__mode' }, t('catalog.' + it.entry + '.text'), ' ', t('catalog.mode.' + it.mode)), compact ? null : reasons),
        h('div', { class: 'dz-int__side' }, status, btn)),
      body);
  }

  async function integrateCard(admin) {
    const body = h('div', { class: 'stack' });
    async function render() {
      try { await LL.loadIntegrations(); } catch (e) { /* the list only decides the "connected" marks */ }
      const data = await api('/api/integrate');
      const groups = [['available', it => it.status === 'available' && !it.configured], ['connected', it => it.configured], ['planned', it => it.status === 'planned' && !it.configured]];
      const out = [];
      groups.forEach(([k, f]) => {
        const list = data.items.filter(f);
        if (!list.length) return;
        out.push(h('h3', { class: 'dz-int__group' }, t('integrate.group.' + k, { n: list.length })), ...list.map(it => itemCard(it, admin, render)));
      });
      body.replaceChildren(...(out.length ? out : [h('div', { class: 'empty-note' }, t('integrate.none'))]));
    }
    await render();
    return card(t('integrate.title'), [h('p', { class: 'muted' }, t('integrate.lead')), body]);
  }

  LL.networkPage = async function (pg) {
    const host = h('div', { class: 'dz-page' });
    pg.append(host);
    const admin = LL.can('admin'), may = LL.can('operate');
    const cards = await Promise.all([zonesCard(admin), protectCard(may), integrateCard(admin)]);
    host.append(...cards);
  };

  // ------------------------------------------------------------------ sections of a device card
  LL.protectSection = function (dev, may) {
    const p = dev.protect;
    if (!p && !may) return null;
    const chip = p && p.class ? h('span', { class: 'chip ' + (CLASS_CHIP[p.class] || 'chip--muted') }, t('protect.class.' + p.class)) : null;
    const rows = [];
    if (chip) rows.push(h('div', { class: 'dz-prot__main' }, chip, h('span', { class: 'muted' }, t(p.why || 'protect.why.mark')), p.source === 'auto' ? h('span', { class: 'muted' }, '· ' + t('protect.auto')) : null));
    if (chip) rows.push(h('p', { class: 'muted fineprint' }, t('protect.rule.' + p.class)));
    if (may) rows.push(h('div', { class: 'dz-prot__set' }, h('span', { class: 'muted' }, t('protect.change')), markSelect({ id: dev.id, mark: p && p.mark, class: p && p.class, source: p && p.source }, () => LL.route())));
    const sec = h('section', { class: 'detail__sec' }, h('h3', {}, t('protect.section')), h('div', { class: 'stack-sm' }, rows));
    return sec;
  };

  LL.integrateSection = function (dev) {
    const sec = h('section', { class: 'detail__sec', hidden: true }, h('h3', {}, t('integrate.section')));
    const admin = LL.can('admin');
    api('/api/devices/' + encodeURIComponent(dev.id) + '/integrate').then(async data => {
      const items = data.items || [];
      if (!items.length) return;
      try { await LL.loadIntegrations(); } catch (e) { /* ignore */ }
      sec.append(h('div', { class: 'stack-sm' }, items.map(it => itemCard(it, admin, () => LL.route(), true))));
      sec.hidden = false;
    }).catch(() => { /* a failed hint never breaks the card */ });
    return sec;
  };
})();
