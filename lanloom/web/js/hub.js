/* Lanloom — the hub bridge on the component card of a home-automation hub: what is read, what stays out for privacy,
   how the hub is spared, the busiest automations, script loops, paused lookups and (admin mode) the last runs of one
   automation. Read only; asking for runs costs the hub a little work, so it needs the operator role. Plain DOM. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;

  const GROUPS = ['presence', 'cameras', 'calendars', 'notifications'];

  function factsOf(rows) {
    return h('dl', { class: 'facts' }, rows.filter(r => r && r[1] != null && r[1] !== '').map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]).flat());
  }
  const fmtWhen = ts => {
    if (!ts) return '—';
    try { return new Date(ts * 1000).toLocaleString(LL.locale(), { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }); }
    catch (e) { return LL.fmtDate(ts); }
  };
  function sub(title, ...body) { return h('div', { class: 'hv-sub hub-sub' }, h('h4', {}, title), ...body); }

  function runsPanel(b, a, holder) {
    const msg = h('div', { class: 'msg' });
    const out = h('div', { class: 'hub-runs' });
    async function show() {
      try {
        const r = await api('/api/hub/' + b.id + '/traces/' + encodeURIComponent(a.entity_id));
        const tr = r.traces;
        if (!tr) return false;
        if (tr.error) { LL.msg(msg, t('hub.runs.failed'), 'error'); return true; }
        out.replaceChildren(...[
          tr.runs.length ? h('table', { class: 'mini-table hub-table' },
            h('thead', {}, h('tr', {}, h('th', {}, t('hub.runs.when')), h('th', {}, t('hub.runs.result')), h('th', {}, t('hub.runs.last_step')))),
            h('tbody', {}, tr.runs.slice(0, 8).map(run => h('tr', {},
              h('td', {}, fmtWhen(run.start)),
              h('td', {}, h('span', { class: 'chip ' + (run.error ? 'chip--danger' : run.execution === 'finished' ? 'chip--ok' : 'chip--muted') },
                t(run.error ? 'hub.runs.error' : run.execution === 'finished' ? 'hub.runs.finished' : 'hub.runs.stopped'))),
              h('td', { class: 'mono truncate' }, run.last_step || '—'))))) : h('p', { class: 'muted' }, t('hub.runs.none')),
          tr.steps.length ? h('p', { class: 'muted hub-steps' }, t('hub.runs.path'), ' ', h('span', { class: 'mono' }, tr.steps.map(s => s.path + (s.ok ? '' : ' ✕')).join(' → '))) : null].filter(Boolean));
        return true;
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); return true; }
    }
    const btn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
      btn.disabled = true;
      LL.msg(msg, t('hub.runs.asking'), 'info');
      try {
        await api('/api/hub/' + b.id + '/traces/' + encodeURIComponent(a.entity_id), { method: 'POST', body: {} });
        for (let i = 0; i < 20; i++) {             // the bridge answers within its trace budget
          if (await show()) { LL.msg(msg, '', 'info'); break; }
          await new Promise(r => setTimeout(r, 1500));
        }
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      btn.disabled = false;
    } }, t('hub.runs.ask'));
    holder.replaceChildren(h('div', { class: 'hub-runs__head' }, h('strong', { class: 'hv-cut' }, a.name), btn), msg, out);
    show();
  }

  LL.hubBridgeSection = function (b) {
    const kids = [];
    const c = b.counts || {};
    kids.push(h('p', { class: 'muted hub-lead' }, t(b.admin ? 'hub.lead.admin' : 'hub.lead.basic')));
    kids.push(factsOf([
      [t('hub.f.mode'), h('span', { class: 'chip ' + (b.admin ? 'chip--accent' : 'chip--muted') }, t(b.admin ? 'hub.mode.admin' : 'hub.mode.basic'))],
      [t('hub.f.inventory'), t('hub.inventory', { d: c.devices || 0, e: c.entities || 0, a: c.areas || 0 })],
      [t('hub.f.automations'), t('hub.items', { a: c.automations || 0, s: c.scripts || 0 })],
      [t('hub.f.macs'), c.with_mac ? t('hub.macs', { n: c.with_mac, m: c.trackers || 0 }) : null],
      [t('detail.age'), b.snapshot_age_s != null ? LL.fmtAge(b.snapshot_age_s) : null],
      [t('hub.f.limited'), b.limited ? t('hub.limited') : null]]));
    const p = b.privacy || {};
    kids.push(sub(t('hub.privacy.title'),
      h('ul', { class: 'plain-list hub-privacy' }, GROUPS.map(g => h('li', {},
        h('span', { class: 'chip ' + (p.groups && p.groups[g] ? 'chip--warn' : 'chip--ok') }, t(p.groups && p.groups[g] ? 'hub.privacy.shown' : 'hub.privacy.kept_out')),
        ' ', t('hub.privacy.group.' + g)))),
      h('p', { class: 'muted' }, t('hub.privacy.note', { n: p.dropped || 0 }))));
    const pr = b.protection || {};
    kids.push(sub(t('hub.protect.title'), h('ul', { class: 'plain-list' },
      h('li', {}, t('hub.protect.snapshot', { n: pr.snapshot_min || 10 })),
      h('li', {}, t('hub.protect.templates')),
      h('li', {}, t('hub.protect.states', { n: pr.state_rate || 20 })),
      b.admin ? h('li', {}, t('hub.protect.lookups', { c: pr.config_per_min, r: pr.related_per_min })) : null,
      b.admin ? h('li', {}, t('hub.protect.traces', { n: pr.trace_per_min })) : null,
      h('li', {}, t('hub.protect.refused', { n: pr.refused || 0 })),
      pr.unknown_types && pr.unknown_types.length ? h('li', { class: 'muted' }, t('hub.protect.unknown', { list: pr.unknown_types.join(', ') })) : null)));
    const holder = h('div', { class: 'hub-runs-holder' });
    const canRuns = b.admin && LL.can('operate');
    if ((b.busiest || []).length) {
      kids.push(sub(t('hub.busy.title'), h('table', { class: 'mini-table hub-table' },
        h('thead', {}, h('tr', {}, h('th', {}, t('hub.busy.name')), h('th', { class: 'num' }, t('hub.busy.hour')), h('th', { class: 'num' }, t('hub.busy.day')))),
        h('tbody', {}, b.busiest.map(a => h('tr', { class: (b.runaway || []).includes(a.entity_id) ? 'is-warn' : null },
          h('td', { class: 'hub-name' }, canRuns ? h('button', { class: 'link-btn', type: 'button', onclick: () => runsPanel(b, a, holder) }, a.name) : h('span', { class: 'hub-name__text' }, a.name),
            (b.runaway || []).includes(a.entity_id) ? h('span', { class: 'chip chip--warn hub-chip' }, t('hub.busy.runaway')) : null),
          h('td', { class: 'num' }, LL.fmtNum(a.runs_hour, 0)), h('td', { class: 'num' }, LL.fmtNum(a.runs_day, 0)))))),
        canRuns ? h('p', { class: 'muted' }, t('hub.runs.hint')) : null, holder));
    }
    if ((b.cycles || []).length) {
      kids.push(sub(t('hub.loops.title'), h('ul', { class: 'plain-list' }, b.cycles.map(cy => h('li', {}, h('span', { class: 'chip chip--warn' }, t('hub.loops.chip')), ' ',
        cy.map(x => x.name).join(' ⇄ ')))), h('p', { class: 'muted' }, t('hub.loops.note'))));
    }
    if ((b.blocked || []).length) {
      kids.push(sub(t('hub.blocked.title'), h('ul', { class: 'plain-list' }, b.blocked.map(x => h('li', {}, h('span', { class: 'hv-cut' }, x.name),
        h('span', { class: 'muted' }, ' · ', t('hub.blocked.until', { at: LL.fmtDate(x.until) })))))));
    }
    return h('section', { class: 'detail__sec hub-bridge' }, h('h3', {}, t('hub.title')), ...kids);
  };
  // ------------------------------------------------------------------ one component: hub side + network side (H2)
  const FRESH = { ok: 'ok', silent: 'danger', unknown: 'muted' };
  function freshFacts(f) {
    if (!f) return [];
    return [[t('radio.freshness'), h('span', { class: 'chip chip--' + (FRESH[f.fresh] || 'muted') }, t('radio.fresh.' + (f.fresh || 'unknown')))],
      [t('radio.last_seen'), f.last_seen_s != null ? t('onsite.seen', { age: LL.fmtAge(f.last_seen_s) }) : null],
      [t('onsite.battery'), f.battery != null ? LL.fmtNum(f.battery, 0) + ' %' : (f.battery_low ? t('onsite.low') : null)]];
  }
  function entityList(v) {
    if (!v.entities || !v.entities.length) return h('p', { class: 'muted' }, t('hub.side.no_entities'));
    return h('ul', { class: 'hub-ents' }, v.entities.slice(0, 12).map(e => h('li', {},
      h('a', { href: '#/sensors/' + v.integ + '/' + encodeURIComponent(e.id), class: 'hub-ents__name' }, e.name),
      h('span', { class: 'hub-ents__val' + (e.state === 'unavailable' ? ' is-down' : '') }, LL.hubValue ? LL.hubValue(e.state, e.unit) : String(e.state)))),
      v.entities_total > 12 ? h('li', { class: 'muted' }, h('a', { href: '#/sensors/' + v.integ }, t('hub.side.more', { n: v.entities_total - 12 }))) : null);
  }
  function chainBlock(v) {          // the effect chains behind this device's switches (js/automations.js); also the phone sheet
    if (!LL.autoChain || !v.integ || !v.device_id) return null;
    return sub(t('auto.chain.title'), LL.autoChain.section(v.integ, { device: v.device_id }, { bare: true, max: 3 }));
  }
  function deviceBlock(v, extra) {
    return [factsOf([
      [t('hub.side.name'), v.name],
      [t('hub.side.model'), [v.manufacturer, v.model].filter(Boolean).join(' ') || null],
      [t('hub.side.firmware'), v.sw],
      [t('hub.side.area'), v.area],
      [t('hub.side.radio'), v.radio ? (LL.radioKindText ? LL.radioKindText(v.radio) : v.radio) + (v.role && v.role !== 'device' ? ' · ' + t('radio.role.' + v.role) : '') : null],
      [t('hub.side.network'), v.network && v.network.coordinator ? t('radio.coordinator', { name: v.network.coordinator }) : null],
      [t('hub.side.integration'), (v.domains || []).length ? h('span', { class: 'mono' }, v.domains.join(', ')) : null],
      ...freshFacts(v.radio ? v.fresh : null),
      ...(extra || [])]), entityList(v), chainBlock(v)];
  }
  async function setMatch(compId, v, action) {
    try {
      await api('/api/hub/' + v.integ + '/match', { method: 'POST', body: { device: compId, hub_device: v.device_id, action } });
      LL.toast(t('hub.match.done.' + action), 'ok');
      setTimeout(() => LL.route(), 400);
    } catch (e) { LL.toast(LL.errText(e), 'error'); }
  }
  LL.hubSideSection = function (sides, suggest, compId) {
    const may = LL.can('operate');
    const kids = [];
    (sides || []).forEach(v => {
      const m = v.match || {};
      const why = h('p', { class: 'muted hub-why' }, t('hub.match.why.' + (m.how || 'manual'), { value: m.value || '', pct: Math.round((m.confidence || 0) * 100), hub: v.integ_name }));
      kids.push(sub(v.integ_name || t('hub.title'), why, ...deviceBlock(v),
        may ? h('div', { class: 'hub-actions' }, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => setMatch(compId, v, m.manual ? 'reset' : 'split') },
          t(m.manual ? 'hub.match.undo' : 'hub.match.split'))) : null));
    });
    (suggest || []).forEach(v => {
      const m = v.match || {};
      kids.push(sub(t('hub.match.suggest_title'), h('p', { class: 'muted hub-why' }, t('hub.match.why.' + m.how, { value: m.value || '', pct: Math.round((m.confidence || 0) * 100), hub: v.integ_name })),
        factsOf([[t('hub.side.name'), v.name], [t('hub.side.area'), v.area]]),
        may ? h('div', { class: 'hub-actions' },
          h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => setMatch(compId, v, 'join') }, t('hub.match.join')),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => setMatch(compId, v, 'split') }, t('hub.match.not_same'))) : null));
    });
    return h('section', { class: 'detail__sec hub-side' }, h('h3', {}, t('hub.side.title')), h('p', { class: 'muted hub-lead' }, t('hub.side.lead')), ...kids);
  };
  LL.hubDeviceSection = function (v) {
    return h('section', { class: 'detail__sec hub-side' }, h('h3', {}, t('hub.device.title')), sub(v.integ_name || t('hub.title'), ...deviceBlock(v)));
  };
  LL.radioNetSection = function (n) {
    const HEALTH = { ok: 'ok', warn: 'warn', bad: 'danger' };
    return h('section', { class: 'detail__sec hub-radio' }, h('h3', {}, LL.radioKindText ? LL.radioKindText(n.kind) : n.kind),
      factsOf([[t('radio.health'), h('span', { class: 'chip chip--' + HEALTH[n.health] }, t('radio.health.' + n.health))],
        [t('radio.coordinator_label'), n.coordinator_name || t('radio.no_coordinator.' + n.kind)],
        [t('radio.devices'), String(n.devices)], [t('radio.silent'), String(n.silent)], [t('radio.batteries'), String(n.battery)]]),
      h('ul', { class: 'hub-ents' }, (n.members || []).slice(0, 40).map(m => h('li', {},
        h('span', { class: 'hub-ents__name' }, m.name, m.area ? h('span', { class: 'muted' }, ' · ' + m.area) : null),
        h('span', { class: 'chip chip--' + (FRESH[m.fresh] || 'muted') }, m.battery != null ? LL.fmtNum(m.battery, 0) + ' %' : t('radio.fresh.' + (m.fresh || 'unknown')))))),
      h('a', { class: 'btn btn--ghost btn--sm', href: '#/sensors/' + n.integ }, t('radio.to_sensors')));
  };
})();
