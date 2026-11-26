/* Lanloom — Settings > API: own API tokens (everybody), tokens of others and webhooks (administrators), external reports,
   and the reference of the public API, built from the OpenAPI description the server makes from its own code.
   The server decides every right per route; this page only hides what a role cannot use. A token and a webhook secret
   are shown exactly once, right after they were made. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const card = (title, kids, cls) => h('section', { class: 'card settings-card' + (cls ? ' ' + cls : '') }, h('h2', {}, title), kids);
  const copyBtn = value => h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.copyText(value) }, t('access.copy'));
  const origin = () => (location.origin + LL.base).replace(/\/+$/, '');
  const LEVEL_CHIP = { ok: 'chip--ok', info: 'chip--muted', warn: 'chip--warn', danger: 'chip--danger' };
  const SCOPE_CHIP = { read: 'role-chip--viewer', operate: 'role-chip--operator', admin: 'role-chip--admin', ingest: 'role-chip--viewer' };
  const DAY = 86400;
  const V1 = '/api/v1';

  function confirmDo(title, text, yesLabel) {
    return new Promise(resolve => {
      let answered = false;
      const yes = h('button', { class: 'btn btn--danger', type: 'button', onclick: () => { answered = true; d.close(); resolve(true); } }, yesLabel);
      const no = h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => d.close() }, t('access.cancel'));
      const d = LL.dialog(title, h('p', {}, text), h('div', { class: 'btn-row' }, no, yes));
      const obs = new MutationObserver(() => { if (!d.el.isConnected) { obs.disconnect(); if (!answered) resolve(false); } });
      obs.observe(document.body, { childList: true });
      setTimeout(() => no.focus(), 30);
    });
  }

  // a value that is shown once: big, monospaced, with a copy button and the warning that it will not come back
  function onceBox(label, value, extra) {
    return h('div', { class: 'api-once', role: 'status' },
      h('div', { class: 'api-once__head' }, h('strong', {}, label), copyBtn(value)),
      h('code', { class: 'api-once__value mono', tabindex: '0' }, value),
      h('p', { class: 'api-once__warn' }, t('api.once.warn')), extra || null);
  }

  const expiresText = tk => {
    if (tk.expired) return h('span', { class: 'chip chip--danger' }, t('api.tokens.expired'));
    const left = tk.expires - Date.now() / 1000;
    const text = LL.fmtDate(tk.expires);
    return left < 14 * DAY ? h('span', { class: 'chip chip--warn', title: text }, t('api.tokens.expires_in', { n: Math.max(1, Math.ceil(left / DAY)) })) : text;
  };

  // ------------------------------------------------------------------ tokens
  function tokenTable(rows, withOwner, onRevoke) {
    if (!rows.length) return h('p', { class: 'muted' }, t(withOwner ? 'api.tokens.others_none' : 'api.tokens.none'));
    const heads = (withOwner ? ['api.tokens.owner'] : []).concat(['api.tokens.name', 'api.tokens.scope', 'api.tokens.expires', 'api.tokens.used', '']);
    return h('div', { class: 'acc-table api-table api-table--tokens' + (withOwner ? ' api-table--owner' : ''), role: 'table', 'aria-label': t('api.tokens.title') },
      h('div', { class: 'acc-row acc-row--head', role: 'row' }, heads.map(k => h('span', { role: 'columnheader' }, k ? t(k) : ''))),
      rows.map(tk => h('div', { class: 'acc-row' + (tk.expired ? ' is-disabled' : ''), role: 'row' },
        withOwner ? h('span', { role: 'cell', 'data-label': t('api.tokens.owner') }, tk.owner) : null,
        h('span', { role: 'cell', class: 'acc-cell--main' }, h('strong', {}, tk.name), h('span', { class: 'mono muted api-prefix' }, tk.prefix + '…')),
        h('span', { role: 'cell', 'data-label': t('api.tokens.scope') }, h('span', { class: 'chip role-chip ' + (SCOPE_CHIP[tk.scope] || '') }, t('api.scope.' + tk.scope))),
        h('span', { role: 'cell', class: 'api-cell--when', 'data-label': t('api.tokens.expires') }, expiresText(tk)),
        h('span', { role: 'cell', 'data-label': t('api.tokens.used') }, tk.last_used ? h('span', {}, LL.fmtDate(tk.last_used), tk.last_ip ? h('span', { class: 'mono muted api-ip' }, ' · ' + tk.last_ip) : null) : h('span', { class: 'muted' }, t('api.tokens.never'))),
        h('span', { role: 'cell', class: 'acc-cell--act' }, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => onRevoke(tk) }, t('api.tokens.revoke'))))));
  }

  function tokensCard(data, reload) {
    const out = h('div', {});
    const msg = h('div', { class: 'msg', role: 'status' });
    const name = h('input', { class: 'input', type: 'text', maxlength: '60', placeholder: t('api.tokens.name_ph'), autocomplete: 'off' });
    const scope = h('select', { class: 'select' }, data.scopes.map(s => h('option', { value: s }, t('api.scope.' + s))));
    const expiry = h('select', { class: 'select' }, data.expiry_days.map(n => h('option', { value: String(n) }, t('api.tokens.days', { n }))));
    expiry.value = String(data.default_expiry_days);
    const rate = h('select', { class: 'select' }, data.rates.map(n => h('option', { value: String(n) }, t('api.tokens.rate_n', { n }))));
    rate.value = String(data.default_rate);
    const scopeHint = h('p', { class: 'field__hint' });
    const showHint = () => { scopeHint.textContent = t('api.scope.' + scope.value + '.hint'); };
    scope.onchange = showHint; showHint();
    const create = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      create.disabled = true; LL.msg(msg, '');
      try {
        const tk = await api('/api/tokens', { method: 'POST', body: { name: name.value.trim(), scope: scope.value, expires_days: Number(expiry.value), rate: Number(rate.value) } });
        name.value = '';
        const example = 'curl -H "Authorization: Bearer ' + tk.token + '" ' + origin() + '/api/v1/status';
        out.replaceChildren(onceBox(t('api.tokens.made', { name: tk.name }), tk.token,
          h('div', { class: 'ag-snip' }, h('div', { class: 'ag-snip__head' }, h('strong', {}, t('api.tokens.try')), copyBtn(example)), h('pre', { class: 'ag-pre mono', tabindex: '0' }, example))));
        await reload(true);
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      create.disabled = false;
    } }, t('api.tokens.create'));
    const list = h('div', {}, tokenTable(data.tokens, false, revoke));
    async function revoke(tk) {
      if (!await confirmDo(t('api.tokens.revoke_title', { name: tk.name }), t('api.tokens.revoke_text'), t('api.tokens.revoke'))) return;
      try { await api('/api/tokens/' + tk.id, { method: 'DELETE' }); LL.toast(t('api.tokens.revoked'), 'ok'); await reload(true); }
      catch (e) { LL.toast(LL.errText(e), 'error'); }
    }
    const form = h('div', { class: 'api-form' },
      h('div', { class: 'grid-2' }, LL.field(t('api.tokens.name'), name, t('api.tokens.name_hint')), h('div', {}, LL.field(t('api.tokens.scope'), scope), scopeHint)),
      h('div', { class: 'grid-2' }, LL.field(t('api.tokens.expiry'), expiry, t('api.tokens.expiry_hint')), LL.field(t('api.tokens.rate'), rate, t('api.tokens.rate_hint'))),
      msg, h('div', { class: 'btn-row' }, create));
    return { el: card(t('api.tokens.title'), [h('p', { class: 'muted' }, t('api.tokens.lead')), list, out, h('hr'), h('h3', { class: 'api-sub' }, t('api.tokens.new')), form]),
      update(d) { list.replaceChildren(tokenTable(d.tokens, false, revoke)); }, revoke };
  }

  // ------------------------------------------------------------------ webhooks
  // an address breaks after a dot, slash or dash (never in the middle of a word): a <wbr> follows each of them
  function breakAtDots(text) {
    const out = [];
    String(text || '').split(/([./-])/).forEach(part => { if (!part) return; out.push(part); if (/^[./-]$/.test(part)) out.push(h('wbr')); });
    return out;
  }
  function hookState(hk) {
    if (hk.sample) return h('span', { class: 'chip chip--demo' }, t('api.hooks.sample'));
    if (!hk.enabled) return h('span', { class: 'chip chip--muted' }, t('api.hooks.off'));
    if (hk.fails >= 3) return h('span', { class: 'chip chip--danger', title: hk.last_key ? t(hk.last_key) : '' }, t('api.hooks.failing', { n: hk.fails }));
    if (hk.last_ok) return h('span', { class: 'chip chip--ok', title: LL.fmtDate(hk.last_ok) }, t('api.hooks.ok'));
    return h('span', { class: 'chip chip--muted' }, t('api.hooks.waiting'));
  }

  function hookDialog(view, hk, done) {
    const creating = !hk;
    const name = h('input', { class: 'input', type: 'text', maxlength: '60', value: hk ? hk.name : '', placeholder: t('api.hooks.name_ph') });
    const url = h('input', { class: 'input mono', type: 'url', maxlength: '500', placeholder: creating ? t('api.hooks.url_ph') : t('api.hooks.url_keep'), autocomplete: 'off', spellcheck: 'false' });
    const enabled = h('input', { type: 'checkbox', checked: hk ? hk.enabled : true });
    const groups = {};
    view.events.forEach(ev => { (groups[ev.group] = groups[ev.group] || []).push(ev); });
    const boxes = [];
    const evList = h('div', { class: 'api-events' }, Object.keys(groups).map(g => h('fieldset', { class: 'api-events__group' }, h('legend', {}, t('api.hooks.group.' + g)),
      groups[g].map(ev => {
        const cb = h('input', { type: 'checkbox', value: ev.id, checked: hk ? hk.events.includes(ev.id) : ['device.new', 'wan.down', 'wan.up'].includes(ev.id) });
        boxes.push(cb);
        return h('label', { class: 'check' + (ev.available ? '' : ' is-muted') }, cb, h('span', {}, t('webhooks.ev.' + ev.id), ' ', h('code', { class: 'mono muted' }, ev.id),
          ev.available ? null : h('small', { class: 'muted' }, ' · ' + t('api.hooks.unavailable'))));
      }))));
    const msg = h('div', { class: 'msg', role: 'status' });
    const body = h('div', { class: 'stack api-hook-dlg' }, h('p', { class: 'muted' }, t('api.hooks.dialog_lead')),
      LL.field(t('api.hooks.name'), name), LL.field(t('api.hooks.url'), url, t(creating ? 'api.hooks.url_hint' : 'api.hooks.url_keep_hint')),
      h('div', { class: 'field' }, h('span', { class: 'field__label' }, t('api.hooks.events')), evList),
      h('label', { class: 'check' }, enabled, h('span', {}, t('api.hooks.enabled'))), msg);
    const save = h('button', { class: 'btn btn--primary', type: 'button' }, t(creating ? 'api.hooks.add' : 'api.hooks.save'));
    const dlg = LL.dialog(creating ? t('api.hooks.new') : t('api.hooks.edit', { name: hk.name }), body, h('div', { class: 'btn-row' }, save));
    save.onclick = async () => {
      save.disabled = true; LL.msg(msg, '');
      const payload = { name: name.value.trim(), events: boxes.filter(b => b.checked).map(b => b.value), enabled: enabled.checked };
      if (url.value.trim()) payload.url = url.value.trim();
      try {
        const res = creating ? await api('/api/webhooks', { method: 'POST', body: payload }) : await api('/api/webhooks/' + encodeURIComponent(hk.id), { method: 'PUT', body: payload });
        dlg.close(); done(res);
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); save.disabled = false; }
    };
    setTimeout(() => name.focus(), 30);
  }

  function deliveriesTable(rows, names, onRetry) {
    if (!rows.length) return h('p', { class: 'muted' }, t('api.hooks.log_none'));
    const chip = { ok: 'chip--ok', pending: 'chip--warn', failed: 'chip--danger', dropped: 'chip--danger' };
    return h('div', { class: 'acc-table api-table api-table--log', role: 'table', 'aria-label': t('api.hooks.log') },
      h('div', { class: 'acc-row acc-row--head', role: 'row' }, ['api.hooks.log_when', 'api.hooks.name', 'api.hooks.log_event', 'api.hooks.log_result', ''].map(k => h('span', { role: 'columnheader' }, k ? t(k) : ''))),
      rows.map(d => h('div', { class: 'acc-row', role: 'row' },
        h('span', { role: 'cell', class: 'acc-cell--main' }, LL.fmtDate(d.created)),
        h('span', { role: 'cell', 'data-label': t('api.hooks.name') }, names[d.hook] || d.hook),
        h('span', { role: 'cell', 'data-label': t('api.hooks.log_event') }, h('code', { class: 'mono' }, d.event)),
        h('span', { role: 'cell', 'data-label': t('api.hooks.log_result') }, h('span', { class: 'chip ' + (chip[d.status] || 'chip--muted') }, t('api.hooks.status.' + d.status)),
          h('span', { class: 'muted api-log-detail' }, ' ' + [d.last_code ? 'HTTP ' + d.last_code : (d.last_key ? t(d.last_key) : ''), d.attempts > 1 ? t('api.hooks.attempts', { n: d.attempts }) : '', d.ms != null && d.status === 'ok' ? d.ms + ' ms' : ''].filter(Boolean).join(' · '))),
        h('span', { role: 'cell', class: 'acc-cell--act' }, (d.status === 'failed' || d.status === 'dropped') ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => onRetry(d) }, t('api.hooks.retry')) : null))));
  }

  function verifySnippet() {
    return ['import hashlib, hmac, time', '', 'def lanloom_ok(secret, headers, body: bytes) -> bool:',
      '    ts = headers["X-Lanloom-Timestamp"]',
      '    if abs(time.time() - int(ts)) > 300:      # older than 5 minutes: a replay',
      '        return False',
      '    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()',
      '    return hmac.compare_digest("v1=" + mac, headers["X-Lanloom-Signature"])'].join('\n');
  }

  function hooksCard(view, reload) {
    const out = h('div', {});
    const names = {};
    view.webhooks.forEach(x => { names[x.id] = x.name; });
    const after = res => {
      if (res.secret) out.replaceChildren(onceBox(t('api.hooks.secret_made', { name: res.name }), res.secret));
      else out.replaceChildren();
      LL.toast(t('settings.saved'), 'ok'); reload(true);
    };
    const list = !view.webhooks.length ? h('p', { class: 'muted' }, t('api.hooks.none')) :
      h('div', { class: 'acc-table api-table api-table--hooks', role: 'table', 'aria-label': t('api.hooks.title') },
        h('div', { class: 'acc-row acc-row--head', role: 'row' }, ['api.hooks.name', 'api.hooks.target', 'api.hooks.state', ''].map(k => h('span', { role: 'columnheader' }, k ? t(k) : ''))),
        view.webhooks.map(hk => h('div', { class: 'acc-row', role: 'row' },
          // the name with its events as small chips below it (a column of their own wrapped over five lines)
          h('span', { role: 'cell', class: 'acc-cell--main' }, h('strong', {}, hk.name),
            h('span', { class: 'api-evs', 'aria-label': t('api.hooks.events') }, hk.events.map(e => h('span', { class: 'chip api-ev' }, t('webhooks.ev.' + e))))),
          h('span', { role: 'cell', class: 'mono api-target', 'data-label': t('api.hooks.target') }, breakAtDots(hk.target), hk.https ? null : h('span', { class: 'chip chip--warn api-nohttps', title: t('api.hooks.http_hint') }, t('api.hooks.http'))),
          h('span', { role: 'cell', 'data-label': t('api.hooks.state') }, hookState(hk), hk.pending ? h('span', { class: 'muted' }, ' ' + t('api.hooks.pending', { n: hk.pending })) : null),
          h('span', { role: 'cell', class: 'acc-cell--act api-acts' },
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', disabled: view.demo || hk.sample, title: view.demo ? t('api.hooks.demo_hint') : null, onclick: async ev => {
              const b = ev.currentTarget; b.disabled = true;
              try { const r = await api('/api/webhooks/' + encodeURIComponent(hk.id) + '/test', { method: 'POST', body: {} });
                LL.toast(r.ok ? t('api.hooks.test_ok', { code: r.code, ms: r.ms }) : t('api.hooks.test_failed', { why: r.code ? 'HTTP ' + r.code : t(r.key) }), r.ok ? 'ok' : 'error'); reload(true); }
              catch (e) { LL.toast(LL.errText(e), 'error'); b.disabled = false; }
            } }, t('api.hooks.test')),
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => hookDialog(view, hk, after) }, t('api.hooks.change')),
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
              if (!await confirmDo(t('api.hooks.rotate_title', { name: hk.name }), t('api.hooks.rotate_text'), t('api.hooks.rotate'))) return;
              try { after(await api('/api/webhooks/' + encodeURIComponent(hk.id) + '/secret', { method: 'POST', body: {} })); } catch (e) { LL.toast(LL.errText(e), 'error'); }
            } }, t('api.hooks.rotate')),
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
              if (!await confirmDo(t('api.hooks.delete_title', { name: hk.name }), t('api.hooks.delete_text'), t('api.hooks.delete'))) return;
              try { await api('/api/webhooks/' + encodeURIComponent(hk.id), { method: 'DELETE' }); out.replaceChildren(); reload(true); } catch (e) { LL.toast(LL.errText(e), 'error'); }
            } }, t('api.hooks.delete'))))));
    const add = h('button', { class: 'btn btn--primary', type: 'button', disabled: view.webhooks.length >= view.max, onclick: () => hookDialog(view, null, after) }, t('api.hooks.new'));
    const retry = async d => { try { await api('/api/webhooks/deliveries/' + d.id + '/retry', { method: 'POST', body: {} }); LL.toast(t('api.hooks.retried'), 'ok'); reload(true); } catch (e) { LL.toast(LL.errText(e), 'error'); } };
    const verify = verifySnippet();
    const how = h('details', { class: 'api-details' }, h('summary', {}, t('api.hooks.how')),
      h('p', { class: 'muted' }, t('api.hooks.how_text', { s: view.tolerance_s })),
      h('div', { class: 'ag-snip' }, h('div', { class: 'ag-snip__head' }, h('strong', {}, t('api.hooks.verify')), copyBtn(verify)), h('pre', { class: 'ag-pre mono', tabindex: '0' }, verify)));
    return card(t('api.hooks.title'), [h('p', { class: 'muted' }, t('api.hooks.lead')), view.demo ? h('p', { class: 'msg msg--info' }, t('api.hooks.demo_hint')) : null,
      list, out, h('div', { class: 'btn-row' }, add), how, h('hr'), h('h3', { class: 'api-sub' }, t('api.hooks.log')), deliveriesTable(view.deliveries, names, retry)]);
  }

  // ------------------------------------------------------------------ external reports
  function ingestCard(rows) {
    const list = !rows.length ? h('p', { class: 'muted' }, t('api.ingest.none')) : h('ul', { class: 'api-ingest' }, rows.map(r => h('li', { class: 'api-ingest__item' },
      h('div', { class: 'api-ingest__head' }, h('span', { class: 'chip ' + (LEVEL_CHIP[r.level] || 'chip--muted') }, t('api.level.' + r.level)), h('strong', {}, r.title),
        h('span', { class: 'muted' }, r.source + ' · ' + LL.fmtDate(r.t))),
      r.summary ? h('p', { class: 'api-ingest__sum' }, r.summary) : null,
      r.items.length ? h('ul', { class: 'api-ingest__lines' }, r.items.map(i => h('li', { class: 'api-ingest__line api-ingest__line--' + i.level }, i.text))) : null)));
    const sample = JSON.stringify({ source: 'nightly-backup', title: 'Backup of the file server', level: 'ok', summary: '312 GB, 41 min', items: [{ text: '3 files skipped', level: 'info' }] });
    const curl = 'curl -X POST -H "Authorization: Bearer llt_…" -H "Content-Type: application/json" \\\n  -d \'' + sample + '\' \\\n  ' + origin() + '/api/v1/ingest';
    return card(t('api.ingest.title'), [h('p', { class: 'muted' }, t('api.ingest.lead')), list,
      h('details', { class: 'api-details' }, h('summary', {}, t('api.ingest.how')), h('div', { class: 'ag-snip' }, h('div', { class: 'ag-snip__head' }, h('strong', {}, t('api.ingest.example')), copyBtn(curl)), h('pre', { class: 'ag-pre mono', tabindex: '0' }, curl)))]);
  }

  // ------------------------------------------------------------------ reference (from /api/v1/openapi.json)
  function referenceCard(spec) {
    const ops = [];
    Object.keys(spec.paths).forEach(p => Object.keys(spec.paths[p]).forEach(m => ops.push(Object.assign({ path: p, method: m.toUpperCase() }, spec.paths[p][m]))));
    const tags = [...new Set(ops.map(o => o.tags[0]))];
    const auth = 'Authorization: Bearer llt_…';
    const head = h('div', { class: 'api-ref__head' },
      LL.facts([[t('api.ref.base'), h('code', { class: 'mono' }, origin() + V1)], [t('api.ref.auth'), h('code', { class: 'mono' }, auth)],
        [t('api.ref.version'), t('api.ref.version_text', { api: spec.info.version, app: spec.info['x-lanloom-version'] })]]),
      h('div', { class: 'btn-row' }, h('a', { class: 'btn btn--ghost btn--sm', href: LL.url('/api/v1/openapi.json'), download: 'lanloom-openapi.json' }, t('api.ref.download'))));
    const groups = tags.map(tag => h('div', { class: 'api-ref__group' }, h('h3', { class: 'api-sub' }, t('api.tag.' + tag)),
      h('ul', { class: 'api-ref__ops' }, ops.filter(o => o.tags[0] === tag).map(o => h('li', { class: 'api-op' },
        h('details', {}, h('summary', { class: 'api-op__sum' }, h('span', { class: 'api-op__method api-op__method--' + o.method.toLowerCase() }, o.method),
          h('code', { class: 'api-op__path mono' }, o.path), h('span', { class: 'chip role-chip ' + (SCOPE_CHIP[o['x-lanloom-scope']] || '') }, t('api.scope.' + o['x-lanloom-scope'])),
          h('span', { class: 'api-op__text' }, o.summary)),
        h('div', { class: 'api-op__body' }, o.description ? h('p', { class: 'muted' }, o.description) : null,
          o.parameters && o.parameters.length ? h('table', { class: 'mini-table api-op__params' }, h('thead', {}, h('tr', {}, h('th', {}, t('api.ref.param')), h('th', {}, t('api.ref.in')), h('th', {}, t('api.ref.about')))),
            h('tbody', {}, o.parameters.map(p => h('tr', {}, h('td', {}, h('code', { class: 'mono' }, p.name), p.required ? ' *' : ''), h('td', {}, p.in), h('td', {}, p.description || ''))))) : null,
          h('div', { class: 'ag-snip' }, h('div', { class: 'ag-snip__head' }, h('strong', {}, t('api.ref.example')), copyBtn(curlOf(o))), h('pre', { class: 'ag-pre mono', tabindex: '0' }, curlOf(o))))))))));
    return card(t('api.ref.title'), [h('p', { class: 'muted' }, t('api.ref.lead')), head, groups], 'api-ref');
  }
  function curlOf(o) {
    const path = o.path.replace('{device_id}', 'd-…').replace('{day}', new Date().toISOString().slice(0, 10)).replace('{subject}', 'dev:d-…');
    if (o.method === 'POST') return 'curl -X POST -H "Authorization: Bearer llt_…" -H "Content-Type: application/json" -d \'{"source":"…","title":"…"}\' ' + origin() + path;
    return 'curl -H "Authorization: Bearer llt_…" ' + origin() + path;
  }

  // ------------------------------------------------------------------ page
  LL.apiPage = async function (pg) {
    const host = h('div', { class: 'api-page' });
    pg.append(host);
    const admin = LL.can('admin');
    const load = () => Promise.all([api('/api/tokens'), admin ? api('/api/webhooks') : Promise.resolve(null), api('/api/v1/ingest?limit=20'), api('/api/v1/openapi.json')]);
    let [toks, hooks, ingested, spec] = await load();
    const tokens = tokensCard(toks, reload);
    const others = h('div', {});
    const hooksWrap = h('div', {});
    const ingestWrap = h('div', {});
    function renderOthers() {
      if (!admin) { others.replaceChildren(); return; }
      others.replaceChildren(card(t('api.tokens.others'), [h('p', { class: 'muted' }, t('api.tokens.others_lead')), tokenTable(toks.others || [], true, tokens.revoke)]));
    }
    function renderRest() {
      renderOthers();
      hooksWrap.replaceChildren(admin && hooks ? hooksCard(hooks, reload) : h('div', {}));
      ingestWrap.replaceChildren(ingestCard(ingested.reports));
    }
    async function reload() {
      try { [toks, hooks, ingested] = (await load()).slice(0, 3); } catch (e) { LL.toast(LL.errText(e), 'error'); return; }
      tokens.update(toks); renderRest();
    }
    host.append(tokens.el, others, hooksWrap, ingestWrap, referenceCard(spec));
    renderRest();
    return { destroy() {} };
  };
})();
