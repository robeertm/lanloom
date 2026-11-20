/* Lanloom — Vulnerability hints: published vulnerabilities that match a device's maker, model and firmware, from the offline
   feed in the image (lanloom/vulns.py). One list, most serious first, with what to do about each hint; a block in the device
   card; the devices that could not be checked and why; the feed's date and (for administrators) the import of a newer feed file.
   A hint is never phrased as an alarm: it always says how sure it is and what to do. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const enc = encodeURIComponent;
  const RECORD = 'https://nvd.nist.gov/vuln/detail/';

  function day(s) {
    if (!s) return '—';
    const d = new Date(s + 'T12:00:00');
    return isNaN(d.getTime()) ? s : d.toLocaleDateString(LL.locale(), { dateStyle: 'medium' });
  }
  const known = k => (LL.strings[k] ? t(k) : '');
  const devName = d => d.title || d.ip || t('kind.' + (d.kind || 'unknown'));
  const SEV_TONE = { critical: 'danger', high: 'warn', medium: 'muted', low: 'muted', unknown: 'muted' };

  // ------------------------------------------------------------------ small pieces
  const sevChip = f => h('span', { class: 'chip vl-sev vl-sev--' + f.severity + ' chip--' + (SEV_TONE[f.severity] || 'muted') },
    t('vulns.sev.' + f.severity) + (f.score != null ? ' · ' + LL.fmtNum(f.score, 1) : ''));
  const certChip = f => h('span', { class: 'chip vl-cert vl-cert--' + f.certainty, title: t('vulns.certainty.' + f.certainty + '.hint') }, t('vulns.certainty.' + f.certainty));
  const wildBadge = f => (f.exploited ? h('span', { class: 'vl-wild', title: f.exploited_since ? t('vulns.exploited.since', { date: day(f.exploited_since) }) : null },
    h('span', { class: 'vl-wild__dot', 'aria-hidden': 'true' }), t('vulns.exploited'), f.ransomware ? h('span', { class: 'vl-wild__ransom' }, ' · ' + t('vulns.ransomware')) : null) : null);
  const idLink = f => (/^CVE-/.test(f.id)
    ? h('a', { class: 'vl-id mono', href: RECORD + enc(f.id), target: '_blank', rel: 'noopener noreferrer', title: t('vulns.record') }, f.id)
    : h('span', { class: 'vl-id mono' }, f.id));
  function actionText(f) {
    if (f.action === 'replace' || (!f.fixed && (f.reasons || []).includes('all_versions'))) return t('vulns.do.replace');
    return f.fixed ? t('vulns.do.update_to', { fixed: f.fixed }) : t('vulns.do.update');
  }
  function whyText(f) {
    const parts = [];
    const r = f.reasons || [];
    parts.push(t(r.includes('maker_registry') ? 'vulns.why.maker_registry' : 'vulns.why.maker_model', { model: f.model || '—' }));
    if (r.includes('all_versions')) parts.push(t('vulns.why.all_versions'));
    else if (f.firmware) parts.push(t('vulns.why.firmware', { firmware: f.firmware, source: known('inventory.fwsrc.' + f.firmware_source) || '—' }));
    if (r.includes('firmware_announced')) parts.push(t('vulns.why.announced'));
    return parts.join(' ');
  }

  // ------------------------------------------------------------------ one hint (list and device card)
  function hintCard(f, opts) {
    const may = LL.can('operate');
    const dev = f.device;
    const msg = h('div', { class: 'msg', role: 'alert' });
    let aside = null;
    if (may && opts.onChange) {
      if (f.dismissed) {
        aside = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
          try { await api('/api/vulns/restore', { method: 'POST', body: { device: dev.id, id: f.id } }); LL.toast(t('vulns.toast.restored'), 'ok'); opts.onChange(); }
          catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        } }, t('vulns.act.restore'));
      } else {
        const box = h('div', { class: 'vl-aside', hidden: true });
        aside = [h('button', { class: 'btn btn--ghost btn--sm', type: 'button', 'aria-expanded': 'false', onclick: ev => {
          box.hidden = !box.hidden; ev.currentTarget.setAttribute('aria-expanded', box.hidden ? 'false' : 'true');
          if (!box.hidden && !box.firstChild) {
            const note = h('input', { type: 'text', class: 'input', maxlength: '200', autocomplete: 'off', placeholder: t('vulns.aside.placeholder') });
            const go = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: async () => {
              go.disabled = true;
              try { await api('/api/vulns/dismiss', { method: 'POST', body: { device: dev.id, id: f.id, note: note.value.trim() } }); LL.toast(t('vulns.toast.dismissed'), 'ok'); opts.onChange(); }
              catch (e) { LL.msg(msg, LL.errText(e), 'error'); go.disabled = false; }
            } }, t('vulns.act.dismiss'));
            box.append(h('p', { class: 'muted fineprint' }, t('vulns.aside.lead')), LL.field(t('vulns.aside.note'), note), h('div', { class: 'btn-row' }, go));
            note.focus();
          }
        } }, t('vulns.act.aside')), box];
      }
    }
    return h('article', { class: 'vl-hint card' + (f.exploited ? ' is-wild' : '') + (f.dismissed ? ' is-aside' : ''), dataset: { id: f.id } },
      h('div', { class: 'vl-hint__top' }, sevChip(f), wildBadge(f), certChip(f), h('span', { class: 'vl-hint__id' }, idLink(f))),
      h('p', { class: 'vl-hint__title' }, f.title || f.id),
      opts.device === false ? null : h('p', { class: 'vl-hint__dev' },
        h('a', { href: '#/devices/' + enc(dev.id) }, devName(dev)), dev.ip ? h('span', { class: 'mono muted' }, ' · ' + dev.ip) : null,
        h('span', { class: 'muted' }, ' · ' + [f.vendor, f.model].filter(Boolean).join(' '))),
      h('p', { class: 'vl-hint__why' }, whyText(f)),
      h('div', { class: 'vl-hint__do' }, h('span', { class: 'vl-hint__dolabel' }, t('vulns.do.label')), h('span', {}, actionText(f))),
      f.dismissed ? h('p', { class: 'vl-hint__aside muted' }, t('vulns.aside.by', { who: f.dismissed.by || '—', date: LL.fmtDate(f.dismissed.at) }),
        f.dismissed.note ? ' · ' + f.dismissed.note : null) : null,
      aside ? h('div', { class: 'vl-hint__acts' }, aside) : null, msg);
  }

  function statusLine(res) {
    if (res.status === 'unchecked') return t('vulns.unchecked.' + res.reason);
    if (res.status === 'not_affected') return t('vulns.status.not_affected');
    if (res.status === 'none') return t('vulns.status.none');
    return '';
  }

  // ------------------------------------------------------------------ block in the device card
  LL.vulnsSection = function (dev) {
    const box = h('div', { class: 'vl-mini' });
    const load = () => api('/api/devices/' + enc(dev.id) + '/vulns').then(res => {
      const open = res.findings.filter(f => !f.dismissed), aside = res.findings.length - open.length;
      box.replaceChildren(...[
        open.length ? h('div', { class: 'vl-mini__list stack-sm' }, open.map(f => hintCard(Object.assign({ device: res.device }, f), { device: false, onChange: load })))
          : h('p', { class: 'muted vl-mini__none' }, statusLine(res) || t('vulns.status.none')),
        aside ? h('p', { class: 'muted fineprint' }, t('vulns.mini.aside', { n: aside })) : null,
        h('p', { class: 'vl-mini__foot muted fineprint' }, t('vulns.mini.feed', { date: day(res.feed.as_of) }), ' · ',
          h('a', { href: '#/vulnerabilities' }, t('vulns.mini.all')))].filter(Boolean));
    }).catch(() => box.replaceChildren());
    load();
    return h('section', { class: 'detail__sec' }, h('h3', {}, t('vulns.title')), box);
  };

  // ------------------------------------------------------------------ the view
  const CHIPS = [['all', '', s => s.open], ['exploited', 'exploited', s => s.exploited], ['certain', 'certain', s => s.certain],
    ['possible', 'possible', s => s.possible], ['dismissed', 'dismissed', s => s.dismissed]];
  const SEVS = ['critical', 'high', 'medium', 'low'];

  async function vulnsView(app) {
    const fr = LL.frame(app, 'devices');
    const admin = LL.can('admin');
    const f = {};
    ['show', 'severity', 'q'].forEach(k => { const v = LL.query.get(k); if (v) f[k] = v; });
    let data = null, seq = 0, typing = null;
    const sync = () => { ['show', 'severity', 'q'].forEach(k => { if (f[k]) LL.query.set(k, f[k]); else LL.query.delete(k); }); LL.deepLink('#/vulnerabilities' + LL.queryString(), true); };

    const count = h('span', { class: 'muted inv-count', role: 'status' });
    const chipBar = h('div', { class: 'inv-chips', role: 'toolbar', 'aria-label': t('vulns.chips') });
    const q = h('input', { type: 'search', class: 'input inv-search', placeholder: t('vulns.search'), 'aria-label': t('vulns.search'), value: f.q || '', autocomplete: 'off', spellcheck: 'false' });
    const sevSel = h('select', { class: 'select inv-filter', 'aria-label': t('vulns.filter.severity'), onchange: ev => set('severity', ev.target.value) },
      h('option', { value: '' }, t('vulns.filter.all_sev')), SEVS.map(s => h('option', { value: s }, t('vulns.sev.' + s))));
    const listHost = h('div', { class: 'vl-list stack-sm' });
    const uncheckedHost = h('div', { class: 'vl-unchecked' });
    const feedHost = h('div', { class: 'vl-feedhost' });
    const switcher = LL.inventorySwitch ? LL.inventorySwitch('vulnerabilities') : null;
    fr.main.append(h('div', { class: 'page page--wide vl-page' },
      h('div', { class: 'page__head inv-head' }, h('h1', {}, t('vulns.title')), count, switcher),
      h('p', { class: 'muted inv-lead vl-lead' }, t('vulns.lead')), chipBar,
      h('div', { class: 'inv-filters' }, q, sevSel), listHost, uncheckedHost, feedHost));

    q.addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(() => set('q', q.value.trim()), 250); });
    function set(k, v) { if (v) f[k] = v; else delete f[k]; sync(); load(); }

    function drawChips() {
      const s = data.summary;
      chipBar.replaceChildren(...CHIPS.map(([id, show, num]) => {
        const on = (f.show || '') === show;
        return h('button', { class: 'mf-chip inv-chip vl-chip--' + id + (on ? ' is-on' : ''), type: 'button', 'aria-pressed': on ? 'true' : 'false', onclick: () => set('show', show) },
          t('vulns.chip.' + id), h('span', { class: 'inv-chip__n' }, LL.fmtNum(num(s), 0)));
      }));
      sevSel.value = f.severity || '';
      sevSel.classList.toggle('is-set', !!f.severity);
    }
    function drawList() {
      const s = data.summary;
      count.textContent = t('vulns.count', { n: s.open, devices: s.devices });
      if (!data.items.length) {
        const empty = f.show === 'dismissed' ? 'vulns.empty.dismissed' : (f.show || f.severity || f.q) ? 'vulns.empty.filtered' : 'vulns.empty.none';
        listHost.replaceChildren(h('div', { class: 'empty-note vl-empty' }, h('p', {}, t(empty, { checked: s.checked })),
          empty === 'vulns.empty.none' ? h('p', { class: 'muted fineprint' }, t('vulns.empty.limits')) : null));
        return;
      }
      listHost.replaceChildren(...data.items.map(it => hintCard(it, { onChange: load })));
    }
    function drawUnchecked() {
      const u = data.unchecked;
      if (!u.length) { uncheckedHost.replaceChildren(); return; }
      const rows = u.map(x => h('li', { class: 'vl-unchecked__row' },
        h('a', { href: '#/devices/' + enc(x.device.id) }, devName(x.device)), x.device.ip ? h('span', { class: 'mono muted' }, ' ' + x.device.ip) : null,
        h('span', { class: 'vl-unchecked__why muted' }, t('vulns.unchecked.' + x.reason)),
        x.reason === 'firmware_unknown' || x.reason === 'firmware_unreadable'
          ? h('a', { class: 'vl-unchecked__fix', href: '#/inventory/' + enc(x.device.id) }, t('vulns.unchecked.enter_fw')) : null));
      uncheckedHost.replaceChildren(h('details', { class: 'card vl-fold' },
        h('summary', {}, h('span', { class: 'vl-fold__title' }, t('vulns.unchecked.title', { n: u.length })), h('span', { class: 'muted fineprint' }, t('vulns.unchecked.lead'))),
        h('ul', { class: 'plain-list vl-unchecked__list' }, rows)));
    }
    function drawFeed() {
      const fd = data.feed;
      const msg = h('div', { class: 'msg', role: 'status' });
      const file = h('input', { type: 'file', accept: '.gz,.json,application/gzip,application/json', class: 'sr-only', id: 'vl-feedfile', onchange: async ev => {
        const fl = ev.target.files && ev.target.files[0];
        if (!fl) return;
        LL.msg(msg, t('vulns.feed.importing'));
        try {
          const r = await fetch(LL.url('/api/vulns/feed'), { method: 'POST', credentials: 'same-origin', headers: Object.assign({ 'X-Lanloom': '1', 'Content-Type': 'application/octet-stream' }, LL.siteHeaders()), body: fl });
          LL.siteChecked(r);
          const res = await r.json().catch(() => ({}));
          if (!r.ok) throw Object.assign(new Error(res.error || 'vulns.err.feed'), { key: res.error, status: r.status });
          LL.toast(t('vulns.feed.imported', { n: res.entries, date: day(res.as_of) }), 'ok');
          await load();
        } catch (e) { LL.msg(msg, e.key ? t(e.key) : LL.errText(e), 'error'); }
        ev.target.value = '';
      } });
      const acts = admin && !fd.demo ? h('div', { class: 'btn-row btn-row--wrap' },
        h('label', { class: 'btn btn--ghost btn--sm', for: 'vl-feedfile' }, t('vulns.feed.import')), file,
        fd.imported ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
          try { await api('/api/vulns/feed', { method: 'DELETE' }); LL.toast(t('vulns.feed.removed'), 'ok'); await load(); } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        } }, t('vulns.feed.remove')) : null) : null;
      feedHost.replaceChildren(h('section', { class: 'card vl-feed', 'aria-label': t('vulns.feed.title') },
        h('h2', { class: 'vl-feed__title' }, t('vulns.feed.title')),
        h('p', {}, t(fd.demo ? 'vulns.feed.demo' : 'vulns.feed.line', { date: day(fd.as_of), n: LL.fmtNum(fd.entries, 0), exploited: LL.fmtNum(fd.exploited, 0) })),
        fd.imported ? h('p', { class: 'muted' }, t('vulns.feed.imported_line', { date: day(fd.imported.as_of), n: LL.fmtNum(fd.imported.entries, 0) })) : null,
        h('p', { class: 'muted fineprint' }, t('vulns.feed.offline')),
        h('p', { class: 'muted fineprint' }, t('vulns.feed.limits')),
        acts, msg));
    }
    async function load() {
      const my = ++seq;
      const qs = new URLSearchParams(); ['show', 'severity', 'q'].forEach(k => { if (f[k]) qs.set(k, f[k]); });
      let res;
      try { res = await api('/api/vulns' + (qs.toString() ? '?' + qs : '')); } catch (e) { listHost.replaceChildren(h('div', { class: 'empty-note' }, LL.errText(e))); return; }
      if (my !== seq) return;
      data = res;
      drawChips(); drawList(); drawUnchecked(); drawFeed();
    }
    await load();
    const baseDestroy = fr.destroy;
    fr.destroy = () => { clearTimeout(typing); if (baseDestroy) baseDestroy.call(fr); };
    return fr;
  }
  LL.view('vulnerabilities', vulnsView);

  // ------------------------------------------------------------------ palette commands (every role may look)
  LL.registerCommand({ id: 'go.vulnerabilities', group: 'views', order: 3.95, label: () => t('cmdk.go', { name: t('vulns.title') }), keywords: 'vulns.cmd.kw.open', run: () => LL.go('#/vulnerabilities') });
  LL.registerCommand({ id: 'vulns-exploited', group: 'views', order: 3.96, typedOnly: true, label: 'vulns.cmd.exploited', keywords: 'vulns.cmd.kw.exploited', run: () => LL.go('#/vulnerabilities?show=exploited') });
  LL.registerCommand({ id: 'vulns-unchecked', group: 'views', order: 3.97, typedOnly: true, label: 'vulns.cmd.unchecked', keywords: 'vulns.cmd.kw.unchecked', run: () => LL.go('#/vulnerabilities') });
})();
