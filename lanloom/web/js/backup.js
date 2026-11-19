/* Lanloom — backup and restore: Settings > Backup (export, automatic backups, stored files, restore, moving to another host)
   and the restore page of the first start.
   API: GET /api/backup, PUT /api/backup/settings, POST /api/backup/{export,run,upload,check,restore}, GET/DELETE /api/backup/files/<name>;
   first start: POST /api/setup/restore/{upload,check} and /api/setup/restore (with the one-time setup code).
   The passphrase only travels to this server, is never stored and cannot be recovered. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false));
  const card = (title, kids, cls) => { const c = LL.settingsCard(title, kids); c.classList.add('bk-card'); if (cls) c.classList.add(cls); return c; };
  const roleChip = role => h('span', { class: 'chip role-chip role-chip--' + role }, LL.strings['role.name.' + role] ? t('role.name.' + role) : String(role || ''));
  const when = ts => (ts ? LL.fmtDate(ts) : '—');
  const fail = (key, vars) => Object.assign(new Error(key), { key, vars });
  const EVERY_HOURS = [6, 12, 24, 72, 168];

  // ------------------------------------------------------------------ requests with a body that is not JSON
  // Files go through fetch directly. A 403 asking for a fresh confirmation opens the step-up dialog and sends the request once more.
  async function send(path, opts, retried) {
    const init = { method: opts.method || 'GET', credentials: 'same-origin', cache: 'no-store', headers: Object.assign({ 'X-Lanloom': '1' }, LL.pageSite() ? { 'X-Lanloom-Site': LL.pageSite() } : {}, opts.headers || {}) };
    if (opts.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
    if (opts.raw !== undefined) { init.headers['Content-Type'] = 'application/octet-stream'; init.body = opts.raw; }
    const r = await fetch(LL.url(path), init);
    if (r.ok) return r;
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty body */ }
    if (r.status === 403 && data.error === 'auth.err.stepup' && !retried && LL.stepUp && await LL.stepUp(data.methods || [])) return send(path, opts, true);
    const err = new Error(data.error || ('HTTP ' + r.status));
    err.status = r.status; err.key = data.error; err.vars = data.vars; err.wait = data.wait;
    if (r.status === 401 && !opts.allow401) LL.go('#/login');
    throw err;
  }
  async function sendJson(path, opts) { return (await send(path, opts)).json(); }
  async function saveAs(path, opts, fallbackName) {
    const r = await send(path, opts);
    const name = r.headers.get('X-Lanloom-File') || fallbackName || 'lanloom.llbackup';
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = h('a', { href: url, download: name, class: 'sr-only' });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    return name;
  }
  function ask(text, okLabel) {
    return new Promise(resolve => {
      let answered = false;
      const yes = h('button', { class: 'btn btn--danger', type: 'button', onclick: () => { answered = true; d.close(); resolve(true); } }, okLabel);
      const no = h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => d.close() }, t('access.cancel'));
      const d = LL.dialog(t('access.sure'), h('p', {}, text), h('div', { class: 'btn-row' }, no, yes));
      const obs = new MutationObserver(() => { if (!d.el.isConnected) { obs.disconnect(); if (!answered) resolve(false); } });
      obs.observe(document.body, { childList: true });
    });
  }
  function everyText(hours) { return EVERY_HOURS.includes(hours) ? t('backup.every.' + hours) : t('backup.every.other', { n: hours }); }

  // ------------------------------------------------------------------ passphrase with repeat
  function passFields(min, labelKey) {
    const a = h('input', { type: 'password', class: 'input', autocomplete: 'new-password', spellcheck: 'false', minlength: String(min) });
    const b = h('input', { type: 'password', class: 'input', autocomplete: 'new-password', spellcheck: 'false' });
    return {
      a, b,
      el: h('div', { class: 'grid-2' }, LL.field(t(labelKey || 'backup.pass.label'), a, t('backup.pass.hint', { n: min })), LL.field(t('backup.pass.repeat'), b)),
      clear() { a.value = ''; b.value = ''; },
      // '' when fine and filled, null when empty, else the error text
      problem(optional) {
        if (!a.value && !b.value && optional) return null;
        if (a.value.length < min) return t('backup.err.passphrase_short', { n: min });
        if (a.value !== b.value) return t('backup.pass.mismatch');
        return null;
      },
    };
  }

  // ------------------------------------------------------------------ the "now / in the backup" comparison
  function nameList(items, render) {
    if (!items.length) return h('span', { class: 'muted' }, t('backup.prev.none'));
    return h('div', { class: 'bk-names' }, items.map(render));
  }
  function previewView(p) {
    const row = (label, now, nu) => h('tr', {}, h('th', { scope: 'row' }, label),
      h('td', { 'data-label': t('backup.prev.now') }, now), h('td', { 'data-label': t('backup.prev.new') }, nu));
    const userChip = u => h('span', { class: 'bk-name' }, h('span', {}, u.username), roleChip(u.role));
    const nameChip = n => h('span', { class: 'bk-name' }, n);
    const count = (key, n) => h('span', {}, t(key, { n }));
    const manual = (n, m) => h('div', {}, count('backup.prev.devices', n), m ? h('div', { class: 'muted bk-sub' }, t('backup.prev.manual', { n: m })) : null);
    const table = h('table', { class: 'bk-diff' },
      h('thead', {}, h('tr', {}, h('th', {}, t('backup.prev.what')), h('th', {}, t('backup.prev.now')), h('th', {}, t('backup.prev.new')))),
      h('tbody', {},
        row(t('backup.prev.users'), nameList(p.users.now, userChip), nameList(p.users.new, userChip)),
        row(t('backup.prev.integrations'), nameList(p.integrations.now, nameChip), nameList(p.integrations.new, nameChip)),
        row(t('backup.prev.devices_row'), manual(p.devices.now, p.devices.manual_now), manual(p.devices.new, p.devices.manual_new)),
        row(t('backup.prev.connections'), count('backup.prev.entries', p.connections.now), count('backup.prev.entries', p.connections.new)),
        row(t('backup.prev.drafts'), count('backup.prev.draft_n', p.drafts.now), count('backup.prev.draft_n', p.drafts.new)),
        row(t('backup.prev.settings'), count('backup.prev.settings_n', p.settings.now), count('backup.prev.settings_n', p.settings.new)),
        row(t('backup.prev.secrets'), count('backup.prev.secrets_n', p.secrets.now), count('backup.prev.secrets_n', p.secrets.new)),
        row(t('backup.prev.history'), h('span', { class: 'muted' }, t('backup.prev.history_now')),
          h('span', {}, t(p.history.included ? 'backup.prev.history_in' : 'backup.prev.history_out'))),
        row(t('backup.prev.audit'), count('backup.prev.audit_n', p.audit.now),
          h('div', {}, count('backup.prev.audit_n', p.audit.new), h('div', { class: 'muted bk-sub' }, t(p.audit.imported ? 'backup.prev.audit_imported' : 'backup.prev.audit_kept'))))));
    return h('div', { class: 'stack bk-preview' },
      h('p', { class: 'muted' }, t('backup.prev.lead', { version: p.app, created: when(p.created) })),
      h('div', { class: 'bk-table-wrap' }, table),
      p.audit.source_ok === false ? h('div', { class: 'callout callout--warn' }, h('p', {}, t('backup.prev.audit_broken'))) : null,
      p.sessions_end || p.passkeys ? h('div', { class: 'callout callout--warn' },
        p.sessions_end ? [h('strong', {}, t('backup.prev.sessions_title', { n: p.sessions_end })), h('p', {}, t('backup.prev.sessions_text', { n: p.sessions_end }))] : null,
        p.passkeys ? h('p', {}, t('backup.prev.passkeys', { n: p.passkeys })) : null) : null,
      p.arch && p.this_arch && p.arch !== p.this_arch ? h('p', { class: 'muted' }, t('backup.prev.arch', { from: p.arch, to: p.this_arch })) : null);
  }

  // ------------------------------------------------------------------ restore flow (Settings and first start)
  // opts: { setup, code(): string, done(result) }. Returns { el, load(entry) } where entry is a stored file {name, ...header}.
  function restorePanel(opts) {
    const box = h('div', { class: 'bk-restore stack' });
    const msg = h('div', { class: 'msg', role: 'status' });
    let src = null;     // { upload } or { file }

    function pick() {
      src = null; LL.msg(msg, '');
      const input = h('input', { type: 'file', class: 'input', accept: '.llbackup,application/octet-stream', id: 'bk-file' });
      input.addEventListener('change', async () => {
        const f = input.files && input.files[0];
        if (!f) return;
        if (opts.setup && !(opts.code() || '').trim()) { LL.msg(msg, t('backup.setup.need_code'), 'error'); input.value = ''; return; }
        LL.msg(msg, t('backup.restore.reading'), 'info'); input.disabled = true;
        try {
          const r = await sendJson(opts.setup ? '/api/setup/restore/upload' : '/api/backup/upload',
            { method: 'POST', raw: f, headers: opts.setup ? { 'X-Lanloom-Setup-Code': opts.code().trim() } : {}, allow401: opts.setup });
          src = opts.setup ? {} : { upload: r.upload };
          LL.msg(msg, '');
          source(r.meta || {});
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); input.disabled = false; input.value = ''; }
      });
      fill(box, h('p', { class: 'muted' }, t(opts.setup ? 'backup.setup.lead' : 'backup.restore.lead')), LL.field(t('backup.restore.file'), input, t('backup.restore.file_hint')), msg);
    }

    function head(meta) {
      return h('dl', { class: 'facts bk-facts' },
        h('dt', {}, t('backup.restore.version')), h('dd', {}, meta.app || '—'),
        h('dt', {}, t('backup.restore.created')), h('dd', {}, when(meta.created)),
        h('dt', {}, t('backup.restore.content')), h('dd', {}, t(meta.history ? 'backup.with_history' : 'backup.without_history')));
    }
    function again() { return h('button', { class: 'btn btn--ghost', type: 'button', onclick: pick }, t('backup.restore.other')); }

    function source(meta) {
      const current = (LL.state && LL.state.version) || '';
      if (meta.compat === 'newer') {
        fill(box, head(meta), h('div', { class: 'msg msg--error' }, t('backup.err.newer', { version: meta.app || '', current })), h('div', { class: 'btn-row' }, again()));
        return;
      }
      const pass = h('input', { type: 'password', class: 'input', autocomplete: 'off', spellcheck: 'false', id: 'bk-restore-pass' });
      const go = h('button', { class: 'btn btn--primary', type: 'submit' }, t('backup.restore.check'));
      const form = h('form', { class: 'stack', onsubmit: async ev => {
        ev.preventDefault(); go.disabled = true; LL.msg(msg, '');
        try {
          const body = opts.setup ? { code: opts.code().trim(), passphrase: pass.value } : Object.assign({}, src, { passphrase: pass.value });
          const p = await sendJson(opts.setup ? '/api/setup/restore/check' : '/api/backup/check', { method: 'POST', json: body, allow401: opts.setup });
          preview(p, pass.value);
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); go.disabled = false; pass.select(); }
      } }, LL.field(t('backup.restore.pass'), pass), msg, h('div', { class: 'btn-row btn-row--wrap' }, again(), go));
      fill(box, head(meta),
        meta.compat === 'older' ? h('div', { class: 'callout' }, h('p', {}, t('backup.restore.older', { version: meta.app || '', current }))) : null, form);
      setTimeout(() => pass.focus(), 30);
    }

    function preview(p, passphrase) {
      const sure = h('input', { type: 'checkbox' });
      const go = h('button', { class: 'btn btn--danger', type: 'button', disabled: true, onclick: async () => {
        go.disabled = true; sure.disabled = true; LL.msg(msg, t('backup.restore.running'), 'info');
        try {
          const body = opts.setup ? { code: opts.code().trim(), passphrase } : Object.assign({}, src, { passphrase, confirm: true });
          const r = await sendJson(opts.setup ? '/api/setup/restore' : '/api/backup/restore', { method: 'POST', json: body, allow401: opts.setup });
          passphrase = '';
          opts.done(r, box);
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); sure.disabled = false; go.disabled = !sure.checked; }
      } }, t('backup.restore.go'));
      sure.addEventListener('change', () => { go.disabled = !sure.checked; });
      fill(box, previewView(p), h('label', { class: 'check' }, sure, h('span', {}, t('backup.restore.sure'))), msg,
        h('div', { class: 'btn-row btn-row--wrap' }, again(), go));
    }

    pick();
    return {
      el: box,
      load(entry) {
        src = { file: entry.name }; LL.msg(msg, '');
        source(entry);
        box.scrollIntoView({ behavior: 'smooth', block: 'center' });
      },
    };
  }

  // ------------------------------------------------------------------ Settings > Backup
  LL.backupPage = async function (pg) {
    const host = h('div', { class: 'bk-page' });
    const dynamic = h('div', { class: 'bk-page' });
    const restoreCard = h('div');
    let flow = null;
    pg.append(host);
    const first = await api('/api/backup');

    const done = async (r, box) => {
      fill(box, h('div', { class: 'callout callout--ok' }, h('strong', {}, t('backup.restore.done')), h('p', {}, t('backup.restore.done_text')),
        r && r.summary ? h('p', { class: 'muted' }, t('backup.restore.done_summary', { users: r.summary.users, devices: r.summary.devices, integrations: r.summary.integrations })) : null));
      setTimeout(async () => {
        try { LL.state = await api('/api/state', { allow401: true }); } catch (e) { /* the sign-in page loads it again */ }
        LL.go('#/login');
      }, 1500);
    };
    flow = restorePanel({ setup: false, done });
    const rc = card(t('backup.restore.title'), flow.el);
    const moveCard = card(t('backup.move.title'), [h('p', { class: 'muted' }, t('backup.move.lead')),
      h('ol', { class: 'bk-steps' }, ['backup.move.step1', 'backup.move.step2', 'backup.move.step3', 'backup.move.step4'].map(k => h('li', {}, t(k)))),
      h('p', { class: 'muted' }, t('backup.move.key'))]);

    function render(s) {
      const min = s.passphrase_min;
      const est = s.estimate || {};
      // ---- a) back up now
      const hist = h('input', { type: 'checkbox' });
      const pass = passFields(min);
      const msg = h('div', { class: 'msg', role: 'status' });
      const dl = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
        const bad = pass.problem(false);
        if (bad) { LL.msg(msg, bad, 'error'); return; }
        dl.disabled = true; LL.msg(msg, t('backup.export.working'), 'info');
        try {
          const name = await saveAs('/api/backup/export', { method: 'POST', json: { passphrase: pass.a.value, history: hist.checked } });
          pass.clear(); LL.msg(msg, t('backup.export.done', { name }), 'ok');
          refresh();
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); } finally { dl.disabled = false; }
      } }, t('backup.export.go'));
      const a = card(t('backup.export.title'), [
        h('p', { class: 'muted' }, t('backup.export.lead', { size: LL.fmtBytes(est.config_bytes) })),
        pass.el,
        h('label', { class: 'check' }, hist, h('span', {}, t('backup.include_history'), ' ', h('span', { class: 'muted' }, '(' + LL.fmtBytes(est.history_bytes) + ')'))),
        h('div', { class: 'callout callout--warn' }, h('p', {}, t('backup.pass.warning'))),
        msg, h('div', { class: 'btn-row btn-row--wrap bk-actions' }, dl)]);

      // ---- b) automatic backups
      const sch = s.schedule;
      const on = h('input', { type: 'checkbox', checked: !!sch.enabled });
      const every = h('select', { class: 'select', 'aria-label': t('backup.auto.every') }, s.every_choices.map(n => h('option', { value: String(n) }, everyText(n))));
      every.value = String(sch.every_hours);
      const keep = h('input', { type: 'number', class: 'input bk-keep', min: String(s.keep_range[0]), max: String(s.keep_range[1]), step: '1', value: String(sch.keep), inputmode: 'numeric' });
      const aHist = h('input', { type: 'checkbox', checked: !!sch.history });
      const aPass = passFields(min, s.key_set ? 'backup.auto.pass_change' : 'backup.auto.pass');
      const aMsg = h('div', { class: 'msg', role: 'status' });
      const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
        const bad = aPass.problem(true);
        if (bad) { LL.msg(aMsg, bad, 'error'); return; }
        if (on.checked && !s.key_set && !aPass.a.value) { LL.msg(aMsg, t('backup.err.no_key'), 'error'); return; }
        save.disabled = true;
        const body = { enabled: on.checked, every_hours: Number(every.value), keep: Number(keep.value), history: aHist.checked };
        if (aPass.a.value) body.passphrase = aPass.a.value;
        try { const r = await api('/api/backup/settings', { method: 'PUT', body }); aPass.clear(); LL.toast(t('settings.saved'), 'ok'); render(r); }
        catch (e) { LL.msg(aMsg, LL.errText(e), 'error'); save.disabled = false; }
      } }, t('backup.auto.save'));
      const runBtn = s.key_set ? h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
        runBtn.disabled = true; LL.msg(aMsg, t('backup.export.working'), 'info');
        try { const r = await api('/api/backup/run', { method: 'POST', body: {} }); LL.toast(t('backup.auto.ran', { name: r.name }), 'ok'); render(r.state); }
        catch (e) { LL.msg(aMsg, LL.errText(e), 'error'); runBtn.disabled = false; }
      } }, t('backup.auto.run')) : null;
      const b = card(t('backup.auto.title'), [
        s.stale ? h('div', { class: 'callout callout--warn' }, h('strong', {}, t('backup.auto.stale_title')), h('p', {}, t('backup.auto.stale', { age: s.newest ? LL.fmtAge(Date.now() / 1000 - s.newest) : '—' }))) : null,
        h('p', { class: 'muted' }, t('backup.auto.lead')),
        h('label', { class: 'check' }, on, h('span', {}, t('backup.auto.enable'))),
        h('div', { class: 'grid-2' }, LL.field(t('backup.auto.every'), every), LL.field(t('backup.auto.keep'), keep, t('backup.auto.keep_hint', { min: s.keep_range[0], max: s.keep_range[1] }))),
        h('label', { class: 'check' }, aHist, h('span', {}, t('backup.include_history'))),
        h('div', { class: 'bk-keyline' }, s.key_set ? h('span', { class: 'chip chip--ok' }, t('backup.auto.key_set')) : h('span', { class: 'chip chip--warn' }, t('backup.auto.key_missing'))),
        aPass.el,
        h('dl', { class: 'facts bk-facts' },
          h('dt', {}, t('backup.auto.last')), h('dd', {}, when(s.last_ok)),
          h('dt', {}, t('backup.auto.next')), h('dd', {}, sch.enabled && s.next_run ? when(s.next_run) : t('backup.auto.off')),
          s.last_error ? [h('dt', {}, t('backup.auto.error')), h('dd', { class: 'bk-error' }, LL.strings[s.last_error] ? t(s.last_error) : t('error.generic'))] : null,
          h('dt', {}, t('backup.auto.folder')), h('dd', {}, h('code', { class: 'mono' }, s.folder))),
        aMsg, h('div', { class: 'btn-row btn-row--wrap bk-actions' }, save, runBtn)]);

      // ---- c) stored files
      const files = s.files || [];
      const list = files.length ? h('ul', { class: 'bk-files' }, files.map(f => {
        const del = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
          if (!await ask(t('backup.files.delete_ask', { name: f.name }), t('backup.files.delete'))) return;
          try { render(await api('/api/backup/files/' + encodeURIComponent(f.name), { method: 'DELETE' })); LL.toast(t('backup.files.deleted'), 'ok'); }
          catch (e) { LL.toast(LL.errText(e), 'error'); }
        } }, t('backup.files.delete'));
        const get = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
          get.disabled = true;
          try { await saveAs('/api/backup/files/' + encodeURIComponent(f.name), {}, f.name); } catch (e) { LL.toast(LL.errText(e), 'error'); } finally { get.disabled = false; }
        } }, t('backup.files.download'));
        const res = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', disabled: !!f.damaged, onclick: () => flow.load(f) }, t('backup.files.restore'));
        return h('li', { class: 'bk-file' },
          h('div', { class: 'bk-file__main' },
            h('div', { class: 'bk-file__title' }, when(f.created || f.mtime)),
            h('div', { class: 'bk-file__meta muted' }, [f.app ? t('backup.files.version', { version: f.app }) : null, LL.fmtBytes(f.size)].filter(Boolean).join(' · ')),
            h('div', { class: 'bk-file__chips' },
              h('span', { class: 'chip chip--' + (f.auto ? 'muted' : 'accent') }, t(f.auto ? 'backup.files.auto' : 'backup.files.manual')),
              f.history ? h('span', { class: 'chip chip--muted' }, t('backup.with_history')) : null,
              f.compat === 'newer' ? h('span', { class: 'chip chip--warn' }, t('backup.files.newer')) : null,
              f.damaged ? h('span', { class: 'chip chip--danger' }, t('backup.files.damaged')) : null)),
          h('div', { class: 'bk-file__acts' }, get, res, del));
      })) : h('p', { class: 'empty-note' }, t('backup.files.empty'));
      const c = card(t('backup.files.title'), [h('p', { class: 'muted' }, t('backup.files.lead', { folder: s.folder })), list]);

      fill(dynamic, a, b, c);
    }
    async function refresh() { try { render(await api('/api/backup')); } catch (e) { /* the page keeps what it shows */ } }

    render(first);
    fill(host, dynamic, rc, moveCard);
    return null;
  };

  // ------------------------------------------------------------------ first start: restore instead of creating an account
  LL.setupRestore = function (app) {
    const code = h('input', { type: 'text', autocomplete: 'one-time-code', autocapitalize: 'characters', spellcheck: 'false', placeholder: 'ABCD-EFGH', maxlength: '9', class: 'mono' });
    const done = (r, box) => {
      const names = (r && r.users) || [];
      const go = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
        go.disabled = true;
        try { LL.state = await api('/api/state', { allow401: true }); } catch (e) { /* the sign-in page loads it again */ }
        LL.go('#/login');
      } }, t('backup.setup.signin'));
      fill(box, h('div', { class: 'callout callout--ok' }, h('strong', {}, t('backup.restore.done')),
        h('p', {}, t('backup.setup.done', { names: names.join(', ') }))), h('div', { class: 'btn-row' }, go));
    };
    const flow = restorePanel({ setup: true, code: () => code.value, done });
    app.replaceChildren(h('main', { class: 'wizard' },
      h('header', { class: 'wizard__head' }, h('div', { class: 'wizard__brand' }, LL.logo(30), h('span', { class: 'brand-name' }, 'Lanloom'),
        LL.state && LL.state.demo ? h('span', { class: 'chip chip--demo' }, t('app.demo')) : null)),
      h('section', { class: 'wizard__card card' },
        h('h1', {}, t('backup.setup.title')),
        h('p', { class: 'lead' }, t('backup.setup.intro')),
        LL.field(t('wizard.account.code'), code, t('backup.setup.code_hint')),
        flow.el,
        h('div', { class: 'wizard__actions' }, h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => LL.route() }, t('backup.setup.back')), h('span'))),
      LL.footer()));
    setTimeout(() => code.focus(), 30);
  };
})();
