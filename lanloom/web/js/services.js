/* Lanloom — services and web interfaces: the "Links" view of the device catalogue (every web interface with the state of its
   probe, the certificate clock, the services without a web page), the "Services" section of the device card, palette entry.
   Everything here only reads; the one change is "forget" for a service marked "Removed?" (operators). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const alive = el => el.isConnected;
  const STATE_CHIP = { ok: 'ok', auth: 'ok', error: 'danger', doubt: 'warn', dead: 'danger', removed: 'muted', hostdown: 'muted', unprobed: 'muted', unknown: 'muted' };
  const TIER_CHIP = { ok: 'ok', hint: 'accent', warn: 'warn', danger: 'danger', expired: 'danger', unknown: 'muted' };

  function stateChip(r) {
    const text = r.state === 'error' ? t('svc.state.error', { code: r.status || '5xx' })
      : r.state === 'auth' ? t('svc.state.auth', { code: r.status || 401 })
      : r.state === 'ok' && r.web && r.status ? t('svc.state.ok_code', { code: r.status }) : t('svc.state.' + r.state);
    const tip = r.probed_at ? t('svc.probed', { age: LL.fmtAge(Date.now() / 1000 - r.probed_at) }) + (r.ms != null ? ' · ' + r.ms + ' ms' : '') : '';
    return h('span', { class: 'chip chip--' + (STATE_CHIP[r.state] || 'muted') + ' svc-state svc-state--' + r.state, title: tip }, text);
  }

  function certChip(c) {
    if (!c) return null;
    const d = c.days;
    const text = d == null ? t('svc.cert.unknown') : d < 0 ? t('svc.cert.expired', { n: -d }) : d === 0 ? t('svc.cert.today') : t(c.tier === 'ok' ? 'svc.cert.valid' : 'svc.cert.soon', { n: d });
    const issuer = (c.self_signed ? t('svc.cert.selfsigned') : t('svc.cert.issuer', { name: c.issuer || '—' })) + (c.not_after ? ' · ' + t('svc.cert.until', { date: LL.fmtDate(c.not_after) }) : '');
    return h('span', { class: 'chip chip--' + (TIER_CHIP[c.tier] || 'muted') + ' svc-cert svc-cert--' + c.tier, title: issuer }, h('span', { class: 'svc-cert__icon', 'aria-hidden': 'true' }, '🔒︎'), text);
  }

  function product(r) {
    const bits = [];
    if (r.product && r.version) bits.push(r.product + ' ' + r.version);
    else if (r.version) bits.push(t('svc.version', { v: r.version }));
    return bits.join(' · ');
  }
  const portText = r => (r.web ? (r.tls ? 'https' : 'http') : r.name) + ' :' + r.port;

  function openLink(r, compact) {
    if (!r.web) return null;
    if (r.clickable && r.url) return h('a', { class: 'btn btn--ghost btn--sm svc-open', href: r.url, target: '_blank', rel: 'noopener noreferrer', onclick: ev => ev.stopPropagation(), 'aria-label': t('svc.open_aria', { name: r.title, port: r.port }) }, compact ? '↗' : t('svc.open') + ' ↗');
    return h('span', { class: 'btn btn--ghost btn--sm svc-open is-off', role: 'link', 'aria-disabled': 'true', title: t('svc.off_hint') }, compact ? '↗' : t('svc.open') + ' ↗');
  }

  function forgetButton(r, reload) {
    if (r.state !== 'removed' || !LL.can('operate')) return null;
    return h('button', { class: 'btn btn--ghost btn--sm svc-forget', type: 'button', onclick: async ev => {
      ev.stopPropagation(); ev.currentTarget.disabled = true;
      try { await api('/api/services/forget', { method: 'POST', body: { key: r.key } }); reload(); LL.toast(t('svc.forgotten'), 'ok'); } catch (e) { LL.toast(LL.errText(e), 'error'); ev.currentTarget.disabled = false; }
    } }, t('svc.forget'));
  }

  // ------------------------------------------------------------------ the Links view
  LL.linksPanel = function (opts) {
    const el = h('div', { class: 'svc' }, h('div', { class: 'skeleton' }));
    let data = null, filter = 'all';
    const FILTERS = [['all', 'svc.f.all'], ['problems', 'svc.f.problems'], ['certs', 'svc.f.certs']];
    function matches(r, f) {
      return !f || [r.title, r.ip, r.name, String(r.port), r.label || '', r.product || ''].join(' ').toLowerCase().includes(f);
    }
    function draw() {
      if (!data) return;
      const f = (opts.filter && opts.filter()) || '';
      const all = data.rows.filter(r => matches(r, f));
      const web = all.filter(r => r.web), other = all.filter(r => !r.web);
      const s = data.summary;
      const tile = (k, v, cls) => h('div', { class: 'stab-tile' + (cls ? ' ' + cls : '') }, h('strong', {}, v), h('span', { class: 'muted' }, t(k)));
      const problem = r => r.state === 'dead' || r.state === 'error' || r.state === 'doubt';
      const certRows = all.filter(r => r.cert).sort((a, b) => (a.cert.days == null ? 1e9 : a.cert.days) - (b.cert.days == null ? 1e9 : b.cert.days));
      let shown = web;
      if (filter === 'problems') shown = web.filter(problem);
      if (filter === 'certs') shown = [];
      const head = h('div', { class: 'svc-head muted', 'aria-hidden': 'true' }, h('span', {}, t('svc.col.name')), h('span', {}, t('svc.col.service')), h('span', {}, t('svc.col.state')), h('span', {}, t('svc.col.cert')), h('span', {}, ''));
      const row = r => h('div', { class: 'svc-row svc-row--' + r.state + (r.clickable ? '' : ' is-off'), role: 'listitem' },
        h('button', { class: 'svc-row__name', type: 'button', onclick: () => opts.onOpen && opts.onOpen(r.device), title: t('svc.open_card') },
          LL.kindIcon(r.kind || 'unknown'), h('span', { class: 'svc-row__text' }, h('span', { class: 'svc-row__title' }, r.title),
            h('span', { class: 'svc-row__sub muted' }, [r.label && r.label !== r.title ? r.label : '', product(r)].filter(Boolean).join(' · ') || r.ip))),
        h('span', { class: 'svc-row__port mono', 'data-label': t('svc.col.service') }, portText(r)),
        h('span', { class: 'svc-row__state', 'data-label': t('svc.col.state') }, stateChip(r)),
        h('span', { class: 'svc-row__cert', 'data-label': t('svc.col.cert') }, certChip(r.cert)),
        h('span', { class: 'svc-row__act' }, forgetButton(r, load), openLink(r, false)));
      const certRow = r => h('div', { class: 'svc-crow' + (r.cert.tier === 'danger' || r.cert.tier === 'expired' ? ' is-red' : r.cert.tier === 'warn' ? ' is-yellow' : ''), role: 'listitem' },
        h('button', { class: 'svc-row__name', type: 'button', onclick: () => opts.onOpen && opts.onOpen(r.device) }, LL.kindIcon(r.kind || 'unknown'),
          h('span', { class: 'svc-row__text' }, h('span', { class: 'svc-row__title' }, r.title), h('span', { class: 'svc-row__sub muted mono' }, portText(r) + ' · ' + r.ip))),
        certChip(r.cert));
      const certBlock = certRows.length ? h('section', { class: 'svc-block' }, h('h3', {}, t('svc.certs.title')), h('p', { class: 'muted svc-lead' }, t('svc.certs.lead')),
        h('div', { class: 'svc-list', role: 'list' }, certRows.slice(0, filter === 'certs' ? 200 : 8).map(certRow)),
        filter !== 'certs' && certRows.length > 8 ? h('p', { class: 'muted svc-more' }, t('svc.more', { n: certRows.length - 8 })) : null) : null;
      el.replaceChildren(...[
        h('div', { class: 'stab-sum svc-sum' }, tile('svc.tile.web', String(s.web)), tile('svc.tile.dead', String(s.dead), s.dead ? 'is-warn' : ''),
          tile('svc.tile.error', String(s.error), s.error ? 'is-warn' : ''), tile('svc.tile.certs', String(s.certs_soon), s.certs_soon ? 'is-warn' : 'is-ok'),
          tile('svc.tile.removed', String(s.removed))),
        data.enabled ? null : h('p', { class: 'msg msg--info' }, t('svc.disabled')),
        h('div', { class: 'seg svc-filter', role: 'group', 'aria-label': t('svc.filter') }, FILTERS.map(([k, label]) => h('button', {
          type: 'button', class: 'seg__btn' + (k === filter ? ' is-active' : ''), 'aria-pressed': k === filter ? 'true' : 'false', onclick: () => { filter = k; draw(); } }, t(label)))),
        filter === 'certs' ? null : h('section', { class: 'svc-block' }, h('h3', {}, t('svc.web.title')), h('p', { class: 'muted svc-lead' }, t('svc.web.lead', { min: Math.round((data.interval_s || 180) / 60) })),
          shown.length ? [head, h('div', { class: 'svc-list', role: 'list' }, shown.map(row))] : h('div', { class: 'empty-note' }, data.rows.length ? (filter === 'problems' ? t('svc.none_problems') : t('devices.none')) : t('svc.empty'))),
        certBlock,
        filter === 'all' && other.length ? h('details', { class: 'svc-block svc-other' }, h('summary', {}, t('svc.other.title', { n: other.length })), h('p', { class: 'muted svc-lead' }, t('svc.other.lead')),
          h('div', { class: 'svc-list', role: 'list' }, other.map(row))) : null].filter(Boolean));
    }
    async function load() {
      try { data = await api('/api/services'); fw.ok(); draw(); } catch (e) { if (!data) el.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); }
    }
    const fw = LL.fresh.watch(el, 60000, load);
    load();
    const timer = setInterval(() => { if (!alive(el)) { clearInterval(timer); fw.destroy(); return; } if (!document.hidden) load(); }, 60000);
    el.redraw = draw;
    return el;
  };

  // ------------------------------------------------------------------ the section of the device card
  LL.servicesSection = function (deviceId) {
    const body = h('div', { class: 'svc-card' }, h('div', { class: 'skeleton' }));
    const sec = h('section', { class: 'detail__sec', 'data-sec': 'services' }, h('h3', {}, t('svc.card.title')), body);
    async function load() {
      let d;
      try { d = await api('/api/devices/' + encodeURIComponent(deviceId) + '/services'); } catch (e) { sec.remove(); return; }
      fw.ok();
      if (!d.rows.length) { sec.remove(); return; }
      body.replaceChildren(h('ul', { class: 'svc-clist' }, d.rows.map(r => h('li', { class: 'svc-citem svc-citem--' + r.state },
        h('div', { class: 'svc-citem__main' }, h('strong', { class: 'mono' }, portText(r)), h('span', { class: 'muted svc-citem__sub' }, [r.label, product(r)].filter(Boolean).join(' · ') || '')),
        h('div', { class: 'svc-citem__chips' }, stateChip(r), certChip(r.cert)),
        h('div', { class: 'svc-citem__act' }, forgetButton(r, load), openLink(r, false))))),
        h('p', { class: 'muted svc-note' }, t(d.enabled ? 'svc.card.note' : 'svc.disabled')));
    }
    const fw = LL.fresh.watch(sec, 60000, load, { band: false });
    load();
    const timer = setInterval(() => { if (!alive(sec)) { clearInterval(timer); fw.destroy(); return; } if (!document.hidden) load(); }, 60000);
    return sec;
  };

  // ------------------------------------------------------------------ palette
  LL.registerCommand({ id: 'go.links', group: 'views', order: 7, label: () => t('cmdk.go', { name: t('svc.mode') }), keywords: () => t('cmdk.kw.links'), run: () => LL.go('#/devices?view=links') });
})();
