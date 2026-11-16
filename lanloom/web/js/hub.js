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
        out.replaceChildren(
          tr.runs.length ? h('table', { class: 'mini-table hub-table' },
            h('thead', {}, h('tr', {}, h('th', {}, t('hub.runs.when')), h('th', {}, t('hub.runs.result')), h('th', {}, t('hub.runs.last_step')))),
            h('tbody', {}, tr.runs.slice(0, 8).map(run => h('tr', {},
              h('td', {}, LL.fmtDate(run.start)),
              h('td', {}, h('span', { class: 'chip ' + (run.error ? 'chip--danger' : run.execution === 'finished' ? 'chip--ok' : 'chip--muted') },
                t(run.error ? 'hub.runs.error' : run.execution === 'finished' ? 'hub.runs.finished' : 'hub.runs.stopped'))),
              h('td', { class: 'mono truncate' }, run.last_step || '—'))))) : h('p', { class: 'muted' }, t('hub.runs.none')),
          tr.steps.length ? h('p', { class: 'muted hub-steps' }, t('hub.runs.path'), ' ', h('span', { class: 'mono' }, tr.steps.map(s => s.path + (s.ok ? '' : ' ✕')).join(' → '))) : null);
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
          h('td', { class: 'truncate' }, canRuns ? h('button', { class: 'link-btn', type: 'button', onclick: () => runsPanel(b, a, holder) }, a.name) : a.name,
            (b.runaway || []).includes(a.entity_id) ? [' ', h('span', { class: 'chip chip--warn' }, t('hub.busy.runaway'))] : null),
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
})();
