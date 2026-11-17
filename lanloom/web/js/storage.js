/* Lanloom — Settings > Storage: space per area, how long each kind of data is kept, the nightly clean-up, memory. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const card = (title, kids) => h('section', { class: 'card settings-card' }, h('h2', {}, title), kids);

  // which retention belongs to which area (inventory and accounts are never cleaned up automatically)
  const AREA_KEEP = { paths: ['paths_hours', 'paths_days'], availability: ['availability_days'], reports: ['reports_days'], audit: ['audit_days'], scans: ['scans_keep'] };
  const keepLabel = (k, r) => k === 'paths_hours' ? t('storage.keep.hourly', { v: keepText(k, r[k]) }) : k === 'paths_days' ? t('storage.keep.daily', { v: keepText(k, r[k]) }) : keepText(k, r[k]);
  const KINDS = ['live_min', 'paths_hours', 'paths_days', 'availability_days', 'reports_days', 'audit_days', 'scans_keep'];

  function keepText(kind, v) {
    if (kind === 'live_min') return t('storage.keep.minutes', { n: v });
    if (kind === 'paths_hours') return t('storage.keep.days', { n: Math.round((v - 1) / 24) || 1 });
    if (kind === 'scans_keep') return t('storage.keep.scans', { n: v });
    if (v >= 365 && v % 365 === 0) return t('storage.keep.years', { n: v / 365 });
    return t('storage.keep.days', { n: v });
  }

  LL.storagePage = async function (pg) {
    const host = h('div', { class: 'st-page' });
    pg.append(host);
    let data = await api('/api/storage');

    function render(s) {
      const r = s.retention;
      // ---- space per area
      const total = s.areas.reduce((a, x) => a + (x.bytes || 0), 0) || 1;
      const bar = h('div', { class: 'st-bar', role: 'img', 'aria-label': t('storage.space.bar', { size: LL.fmtBytes(s.db_bytes + (s.wal_bytes || 0)) }) },
        s.areas.filter(a => a.bytes).map(a => h('i', { class: 'st-bar__seg st-c--' + a.id, style: 'flex-grow:' + Math.round(Math.max(0.5, a.bytes / total * 100) * 100) / 100, title: t('storage.area.' + a.id) + ' · ' + LL.fmtBytes(a.bytes) })));
      const rows = s.areas.map(a => h('tr', {},
        h('th', { scope: 'row' }, h('span', { class: 'st-sw st-c--' + a.id, 'aria-hidden': 'true' }), t('storage.area.' + a.id)),
        h('td', { class: 'num' }, a.bytes != null ? LL.fmtBytes(a.bytes) : '—'),
        h('td', { class: 'num' }, LL.fmtNum(a.rows, 0)),
        h('td', { class: 'st-keep' }, AREA_KEEP[a.id] ? AREA_KEEP[a.id].map(k => keepLabel(k, r)).join(' · ') : t('storage.keep.always'))));
      const disk = s.disk;
      const facts = [[t('storage.db'), LL.fmtBytes(s.db_bytes + (s.wal_bytes || 0)) + (s.wal_bytes > 1e5 ? ' (' + t('storage.wal', { size: LL.fmtBytes(s.wal_bytes) }) + ')' : '')],
        [t('storage.free_pages'), LL.fmtBytes(s.free_pages_bytes)],
        s.backups_bytes != null ? [t('storage.backups'), LL.fmtBytes(s.backups_bytes)] : null,
        s.other_bytes != null ? [t('storage.other'), LL.fmtBytes(s.other_bytes)] : null,
        disk ? [t('storage.disk_free'), t('storage.disk_of', { free: LL.fmtBytes(disk.free), total: LL.fmtBytes(disk.total) })] : null,
        s.growth && s.growth.per_day != null ? [t('storage.growth'), t('storage.growth_per_day', { size: LL.fmtBytes(s.growth.per_day) })] : null];
      const space = card(t('storage.space.title'), [h('p', { class: 'muted' }, t('storage.space.lead')), bar,
        h('div', { class: 'st-table-wrap' }, h('table', { class: 'mini-table st-table' },
          h('thead', {}, h('tr', {}, h('th', {}, t('storage.col.area')), h('th', { class: 'num' }, t('storage.col.size')), h('th', { class: 'num' }, t('storage.col.rows')), h('th', {}, t('storage.col.kept')))),
          h('tbody', {}, rows))),
        !s.dbstat ? h('p', { class: 'muted fineprint' }, t('storage.no_dbstat')) : null,
        LL.facts(facts.filter(Boolean))]);

      // ---- retention
      const msg = h('div', { class: 'msg', role: 'status' });
      const fields = KINDS.map(kind => {
        const sel = h('select', { class: 'select', onchange: async () => {
          sel.disabled = true;
          try { const res = await api('/api/storage/retention', { method: 'PUT', body: { [kind]: Number(sel.value) } }); data.retention = res.retention; LL.toast(t('settings.saved'), 'ok'); render(data); }
          catch (e) { LL.msg(msg, LL.errText(e), 'error'); sel.disabled = false; }
        } }, (s.choices[kind] || []).map(v => h('option', { value: String(v) }, keepText(kind, v))));
        sel.value = String(r[kind]);
        return LL.field(t('storage.kind.' + kind), sel, t('storage.kind.' + kind + '.hint'));
      });
      const keep = card(t('storage.keep.title'), [h('p', { class: 'muted' }, t('storage.keep.lead')), h('div', { class: 'grid-2 st-grid' }, fields), msg]);

      // ---- nightly clean-up
      const last = s.last_run;
      const runMsg = h('div', { class: 'msg', role: 'status' });
      const runBtn = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
        runBtn.disabled = true; LL.msg(runMsg, t('storage.run.working'), 'info');
        try { await api('/api/storage/cleanup', { method: 'POST', body: {} }); data = await api('/api/storage'); render(data); LL.toast(t('storage.run.done'), 'ok'); }
        catch (e) { LL.msg(runMsg, LL.errText(e), 'error'); runBtn.disabled = false; }
      } }, t('storage.run.now'));
      const removed = last && last.removed ? Object.values(last.removed).reduce((a, b) => a + (b || 0), 0) : 0;
      const clean = card(t('storage.run.title'), [h('p', { class: 'muted' }, t('storage.run.lead')),
        LL.facts([[t('storage.run.last'), last ? LL.fmtDate(last.t) : t('storage.run.never')],
          last ? [t('storage.run.result'), t('storage.run.summary', { n: removed, freed: LL.fmtBytes(last.freed || 0), dur: LL.fmtAge(last.dur_s || 0) })] : null,
          last ? [t('storage.run.vacuum'), last.vacuum ? t('storage.run.vacuum_yes') : (last.skipped ? t(last.skipped) : t('storage.run.vacuum_no'))] : null,
          [t('storage.run.next'), LL.fmtDate(s.next_run)]].filter(Boolean)),
        runMsg, h('div', { class: 'btn-row' }, runBtn)]);

      // ---- memory
      const mem = card(t('storage.mem.title'), [h('p', { class: 'muted' }, t('storage.mem.lead')),
        LL.facts([s.rss_bytes ? [t('storage.mem.rss'), LL.fmtBytes(s.rss_bytes)] : null,
          [t('storage.mem.live'), t('storage.mem.live_value', { n: LL.fmtNum(s.live.samples, 0), traces: s.live.keys, size: LL.fmtBytes(s.live.bytes) })]].filter(Boolean))]);
      host.replaceChildren(space, keep, clean, mem);
    }
    render(data);
    return {};
  };
})();
