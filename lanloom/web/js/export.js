/* Lanloom — Export and import: the network plan as PDF, SVG or draw.io, the inventory as CSV or JSON, the import of own fields, and the
   palette commands. The server builds every file (lanloom/export.py, export_plan.py); this file only asks for it and saves it. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;

  const KEY = 'll.export';
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { return {}; } };
  const keep = o => { try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) { /* private mode: the choice is not kept */ } };

  // one file from the server, saved through a temporary link
  async function download(what, opts) {
    const body = Object.assign({ what, lang: LL.lang }, opts || {});
    const r = await fetch(LL.url('/api/export'), { method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'X-Lanloom': '1', 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) {
      let data = {};
      try { data = await r.json(); } catch (e) { /* empty body */ }
      const err = new Error(data.error || ('HTTP ' + r.status));
      err.status = r.status; err.key = data.error; err.wait = data.wait;
      if (r.status === 401) LL.go('#/login');
      throw err;
    }
    const name = r.headers.get('X-Lanloom-File') || ('lanloom-' + what.replace('.', '-'));
    const url = URL.createObjectURL(await r.blob());
    const a = h('a', { href: url, download: name, class: 'sr-only' });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    return name;
  }
  LL.exportDownload = download;

  const mine = () => Object.assign({ view: 'network', look: 'light', size: 'A4', redact: false }, load());

  // ------------------------------------------------------------------ the page
  function exportView(app) {
    const fr = LL.frame(app, 'devices');
    const o = mine();
    const sel = (label, key, values, text) => {
      const s = h('select', { class: 'select', onchange: () => { o[key] = s.value; keep(o); } }, values.map(v => h('option', { value: v }, t(text + v))));
      s.value = o[key];
      return LL.field(label, s);
    };
    const redact = h('input', { type: 'checkbox', checked: !!o.redact, onchange: () => { o.redact = redact.checked; keep(o); } });
    const msg = h('div', { class: 'msg', role: 'status' });
    const btns = [];
    const go = (what, label, hint) => {
      const b = h('button', { class: 'btn ' + (what === 'plan.pdf' ? 'btn--primary' : 'btn--ghost'), type: 'button', onclick: async () => {
        btns.forEach(x => { x.disabled = true; });
        LL.msg(msg, t('export.working'), 'info');
        try { LL.msg(msg, t('export.done', { name: await download(what, o) }), 'ok'); }
        catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        btns.forEach(x => { x.disabled = false; });
      } }, label);
      btns.push(b);
      return h('div', { class: 'ex-act' }, b, h('span', { class: 'muted ex-act__hint' }, hint));
    };

    const plan = LL.settingsCard(t('export.plan.h'), [h('p', { class: 'muted' }, t('export.plan.lead')),
      h('div', { class: 'ex-opts' }, sel(t('export.opt.view'), 'view', ['network', 'overview'], 'export.view.'), sel(t('export.opt.look'), 'look', ['light', 'dark', 'print'], 'export.look.'),
        sel(t('export.opt.size'), 'size', ['A4', 'A3'], 'export.size.')),
      h('label', { class: 'check ex-redact' }, redact, h('span', {}, t('export.opt.redact'))), h('p', { class: 'muted fineprint' }, t('export.redact.hint')),
      h('div', { class: 'ex-acts' }, go('plan.pdf', t('export.btn.pdf'), t('export.pdf.hint')), go('plan.svg', t('export.btn.svg'), t('export.svg.hint')),
        go('plan.drawio', t('export.btn.drawio'), t('export.drawio.hint'))), msg]);
    plan.classList.add('ex-card');

    const inv = LL.settingsCard(t('export.inv.h'), [h('p', { class: 'muted' }, t('export.inv.lead')),
      h('div', { class: 'ex-acts' }, go('list.csv', t('export.btn.csv'), ''), go('list.json', t('export.btn.json'), ''))]);
    inv.classList.add('ex-card');

    const cards = [plan, inv];
    cards.push(LL.can('operate') ? importCard() : LL.settingsCard(t('export.import.h'), [h('p', { class: 'muted' }, t('export.import.only_operator'))]));
    cards[2].classList.add('ex-card');

    fr.main.append(h('div', { class: 'page ex-page' },
      h('div', { class: 'page__head' }, h('h1', {}, t('export.title')), LL.inventorySwitch ? h('a', { class: 'btn btn--ghost btn--sm ex-back', href: '#/inventory' }, t('inventory.title')) : null),
      h('p', { class: 'muted ex-lead' }, t('export.lead')), cards));
    return fr;
  }
  LL.view('export', exportView);

  // ------------------------------------------------------------------ import: check first, write only on request
  function importCard() {
    const file = h('input', { type: 'file', accept: '.csv,.json,text/csv,application/json', class: 'ex-file', 'aria-label': t('export.import.pick') });
    const out = h('div', { class: 'ex-report', role: 'status' });
    const check = h('button', { class: 'btn btn--ghost', type: 'button', disabled: true }, t('export.import.check'));
    const apply = h('button', { class: 'btn btn--primary', type: 'button', hidden: true }, t('export.import.apply'));
    let text = '', fmt = '';
    file.addEventListener('change', async () => {
      const f = file.files && file.files[0];
      apply.hidden = true; out.replaceChildren();
      if (!f) { check.disabled = true; return; }
      fmt = /\.json$/i.test(f.name) ? 'json' : 'csv';
      text = f.size > 2000000 ? '' : await f.text();
      check.disabled = false;
    });
    async function run(dry) {
      check.disabled = apply.disabled = true;
      try {
        const r = await api('/api/export/import', { method: 'POST', body: { format: fmt, text, dry_run: dry } });
        const lines = [];
        if (dry) lines.push(h('p', {}, t('export.import.summary', { rows: r.rows, matched: r.matched, changed: r.changed, unchanged: r.unchanged, unmatched: r.unmatched })));
        else lines.push(h('p', { class: 'ex-ok' }, t('export.import.done', { changed: r.changed })));
        if (r.errors && r.errors.length) {
          lines.push(h('h3', {}, t('export.import.errors')),
            h('ul', { class: 'ex-errors' }, r.errors.map(e => h('li', {}, t('export.import.err_row', { row: e.row, reason: LL.strings[e.error] ? t(e.error) : e.error })))));
        }
        if (dry && !r.changed) lines.push(h('p', { class: 'muted' }, t('export.import.empty')));
        out.replaceChildren(...lines);
        apply.hidden = !dry || !r.changed;
      } catch (e) { out.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); apply.hidden = true; }
      check.disabled = !file.files.length; apply.disabled = false;
    }
    check.addEventListener('click', () => run(true));
    apply.addEventListener('click', () => run(false));
    return LL.settingsCard(t('export.import.h'), [h('p', { class: 'muted' }, t('export.import.lead')),
      h('div', { class: 'ex-importrow' }, file, check, apply), out]);
  }

  // ------------------------------------------------------------------ palette commands (every role may export)
  const run = (what, extra) => async () => {
    LL.toast(t('cmdk.export.working'), 'info');
    try { LL.toast(t('export.done', { name: await download(what, Object.assign(mine(), extra || {})) }), 'ok'); }
    catch (e) { LL.toast(LL.errText(e), 'error'); }
  };
  const cmd = (id, order, label, what) => LL.registerCommand({ id, group: 'commands', order, label, keywords: () => t('cmdk.kw.export'), run: run(what) });
  cmd('export-pdf', 40, 'cmdk.export.pdf', 'plan.pdf');
  cmd('export-svg', 41, 'cmdk.export.svg', 'plan.svg');
  cmd('export-drawio', 42, 'cmdk.export.drawio', 'plan.drawio');
  cmd('export-csv', 43, 'cmdk.export.csv', 'list.csv');
  cmd('export-json', 44, 'cmdk.export.json', 'list.json');
  LL.registerCommand({ id: 'go.export', group: 'views', order: 3.55, label: () => t('cmdk.export.open'), keywords: () => t('cmdk.kw.export'), run: () => LL.go('#/export') });
})();
