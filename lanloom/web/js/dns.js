/* Lanloom — DNS filters: the card of a filter (totals of 24 hours, hours, days, top lists, upstreams, block lists, findings) and the
   "DNS" section of a device that uses a filter. Only totals are shown; single queries are never read or stored. Everything here only reads. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t } = LL;
  const enc = encodeURIComponent;
  const num = n => (n == null ? '—' : LL.fmtNum(n, 0));
  const pct = v => (v == null ? '—' : LL.fmtNum(v * 100, v > 0 && v < 0.1 ? 1 : 0) + ' %');
  const day = ts => new Date(ts * 1000).toLocaleDateString(LL.locale(), { day: 'numeric', month: 'short' });
  const hourText = hn => new Date(hn * 3600 * 1000).toLocaleTimeString(LL.locale(), { hour: '2-digit', minute: '2-digit', hour12: false });
  const HEALTH = { ok: 'ok', stale: 'warn', down: 'danger', waiting: 'muted' };

  // rows: [[label, queries, blocked], ...]; bars grow with the queries, the blocked part is drawn inside
  function bars(rows, label, cls) {
    const peak = Math.max(1, ...rows.map(r => r[1]));
    return h('div', { class: 'dns-bars ' + (cls || ''), role: 'img', 'aria-label': t('dns.bars.aria') }, rows.map(r => {
      const q = r[1], b = Math.min(r[2], q);
      return h('div', { class: 'dns-bar' + (q ? '' : ' is-empty'), title: label(r) + ' · ' + t('dns.bar.title', { q: num(q), b: num(b) }) },
        h('i', { class: 'dns-bar__all', style: 'height:' + (q ? Math.max(4, Math.round(q / peak * 100)) : 0) + '%' },
          h('i', { class: 'dns-bar__blk', style: 'height:' + (q ? Math.round(b / q * 100) : 0) + '%' })));
    }));
  }

  function topTable(head, rows) {
    if (!rows.length) return h('p', { class: 'muted dns-none' }, t('dns.none'));
    return h('table', { class: 'mini-table dns-table' }, h('thead', {}, h('tr', {}, head.map((c, i) => h('th', { class: i ? 'num' : '' }, c)))),
      h('tbody', {}, rows.map(r => h('tr', {}, r.map((c, i) => h('td', { class: i ? 'num' : 'truncate' }, c))))));
  }

  function filterBody(v) {
    const tot = v.totals;
    const kids = [];
    const chips = [h('span', { class: 'chip chip--' + (HEALTH[v.health] || 'muted') }, t('dns.health.' + v.health))];
    if (v.blocking === false) chips.push(h('span', { class: 'chip chip--warn' }, t('dns.blocking_off')));
    if (!tot) {
      kids.push(h('div', { class: 'dns-head' }, ...chips), h('p', { class: 'muted' }, t('dns.waiting')));
      return h('div', { class: 'dns' }, kids);
    }
    kids.push(h('div', { class: 'dns-head' }, h('strong', { class: 'dns-big' }, pct(tot.share)), h('span', { class: 'muted' }, t('dns.blocked_of', { q: num(tot.queries), b: num(tot.blocked) })), ...chips));
    for (const f of v.findings || []) kids.push(h('p', { class: 'dns-finding dns-finding--' + f.level }, t(f.key, f.params || {})));
    kids.push(bars(v.hours, r => hourText(r[0]), 'dns-bars--hours'),
      h('div', { class: 'dns-axis muted', 'aria-hidden': 'true' }, h('span', {}, t('dns.axis.24h')), h('span', {}, t('dns.axis.12h')), h('span', {}, t('dns.axis.now'))));
    if (LL.a11y) kids.push(LL.a11y.tableAlt(t('dns.table.hours_caption'), [t('dns.table.hour'), t('dns.queries'), t('dns.blocked')],
      v.hours.slice().reverse().map(r => [hourText(r[0]), num(r[1]), num(r[2])])));
    const ups = v.upstream_ok == null ? null : t(v.upstream_ok ? 'dns.upstream.ok' : 'dns.upstream.down');
    kids.push(LL.facts([[t('dns.queries'), num(tot.queries)], [t('dns.blocked'), num(tot.blocked)], [t('dns.clients'), num(tot.clients)],
      [t('dns.list_entries'), tot.list_entries ? num(tot.list_entries) : null], [t('dns.upstream'), ups], [t('dns.version'), v.version ? h('span', { class: 'mono' }, v.version) : null],
      [t('dns.last_reading'), v.age_s != null ? LL.fmtAge(v.age_s) : null]]));
    kids.push(h('h4', { class: 'dns-sub' }, t('dns.top_blocked')),
      topTable([t('dns.domain'), t('dns.count')], (v.top_blocked || []).map(r => [h('span', { class: 'mono' }, r.domain), num(r.n)])));
    kids.push(h('h4', { class: 'dns-sub' }, t('dns.top_clients')),
      topTable([t('dns.client'), t('dns.queries'), t('dns.blocked_share')], (v.top_clients || []).slice(0, 10).map(c => [
        c.device ? h('a', { href: '#/network/' + enc(c.device) }, c.title || c.ip) : (c.title || c.ip || '—'), num(c.q), c.b == null ? '—' : pct(c.q ? c.b / c.q : null)])));
    if ((v.upstreams || []).length) kids.push(h('h4', { class: 'dns-sub' }, t('dns.upstreams')),
      topTable([t('dns.resolver'), t('dns.answers'), t('dns.avg_ms')], v.upstreams.map(u => [h('span', { class: 'mono' }, u.name), num(u.n), u.ms == null ? '—' : LL.fmtNum(u.ms, 1)])));
    if ((v.lists || []).length) kids.push(h('h4', { class: 'dns-sub' }, t('dns.lists', { n: v.lists.length })),
      topTable([t('dns.list'), t('dns.entries'), t('dns.updated')], v.lists.map(l => [h('span', { class: l.enabled ? '' : 'muted' }, l.name), num(l.entries), l.updated ? day(l.updated) : '—'])));
    const days = (v.days || []).filter(r => r[1] > 0);
    if (days.length > 1) kids.push(h('h4', { class: 'dns-sub' }, t('dns.days', { n: v.days.length })),
      bars(v.days, r => day(r[0] * 86400), 'dns-bars--days'),
      LL.a11y ? LL.a11y.tableAlt(t('dns.table.days_caption'), [t('dns.table.day'), t('dns.queries'), t('dns.blocked')],
        v.days.slice().reverse().map(r => [day(r[0] * 86400), num(r[1]), num(r[2])])) : null);
    kids.push(h('p', { class: 'muted dns-note' }, t('dns.note')));
    return h('div', { class: 'dns' }, kids);
  }

  // the card of the filter (one section per filter on this component)
  LL.dnsSection = function (filters) {
    const list = (filters || []).filter(Boolean);
    if (!list.length) return null;
    return h('section', { class: 'detail__sec', 'data-sec': 'dns' }, h('h3', {}, list.length === 1 ? t('dns.title') : t('dns.title_n', { n: list.length })),
      list.map(v => h('div', { class: 'dns-filter' }, list.length > 1 ? h('h4', { class: 'dns-sub' }, v.name) : null, filterBody(v))));
  };

  // the "DNS" section of a device that is a client of a filter
  LL.dnsClientSection = function (c) {
    const rows = (c && c.filters) || [];
    if (!rows.length) return null;
    return h('section', { class: 'detail__sec', 'data-sec': 'dns-client' }, h('h3', {}, t('dns.client.title')),
      rows.map(r => h('div', { class: 'dns-client' }, LL.facts([[t('dns.client.filter'), r.name], [t('dns.queries'), num(r.q)],
        [t('dns.client.share_of_all'), r.share_of_all == null ? null : pct(r.share_of_all)], [t('dns.blocked_share'), r.blocked_share == null ? null : pct(r.blocked_share)]]),
        h('p', { class: 'muted dns-note' }, t('dns.client.note', { h: Math.round((r.window_s || 86400) / 3600) })))));
  };

  // the palette: jump to the card of the (first) filter
  LL.registerCommand({ id: 'dns-card', group: 'commands', order: 28, available: () => !!(LL.state && LL.state.dns > 0), label: 'cmdk.dns', keywords: () => t('cmdk.kw.dns'),
    run: async () => {
      try { const r = await LL.api('/api/dns'); const f = (r.filters || []).find(x => x.component); if (f) LL.go('#/network/' + enc(f.component)); } catch (e) { /* no filter: nothing to open */ }
    } });
})();
