/* Lanloom — first-start setup wizard and the integration forms (shared with Settings). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const STEPS = ['account', 'networks', 'scan', 'review', 'connect', 'done'];
  const PHASES = ['neighbors', 'mdns', 'ssdp', 'ports', 'banners'];

  function stepKey() { try { return sessionStorage.getItem('ll-wiz') || 'networks'; } catch (e) { return 'networks'; } }
  function setStep(s) { try { sessionStorage.setItem('ll-wiz', s); } catch (e) { /* ignore */ } }

  function shell(app, step, body) {
    const idx = STEPS.indexOf(step);
    const stepper = h('ol', { class: 'stepper', 'aria-label': t('wizard.progress') },
      STEPS.map((s, i) => h('li', { class: 'stepper__item' + (i < idx ? ' is-done' : '') + (i === idx ? ' is-current' : ''), 'aria-current': i === idx ? 'step' : null },
        h('span', { class: 'stepper__num' }, i < idx ? '✓' : String(i + 1)), h('span', { class: 'stepper__label' }, t('wizard.step.' + s)))));
    app.replaceChildren(h('main', { class: 'wizard' },
      h('header', { class: 'wizard__head' }, h('div', { class: 'wizard__brand' }, LL.logo(30), h('span', { class: 'brand-name' }, 'Lanloom'),
        LL.state && LL.state.demo ? h('span', { class: 'chip chip--demo' }, t('app.demo')) : null), langSwitch()),
      stepper,
      h('section', { class: 'wizard__card card' }, body),
      LL.footer()));
  }

  function langSwitch() {
    const sel = h('select', { class: 'select select--sm', 'aria-label': t('settings.language'), onchange: async () => { await LL.loadLang(sel.value); LL.route(); } },
      h('option', { value: 'en' }, 'English'), h('option', { value: 'de' }, 'Deutsch'));
    sel.value = LL.lang;
    return sel;
  }

  function actions(...kids) { return h('div', { class: 'wizard__actions' }, ...kids); }

  // ------------------------------------------------------------------ 1 account
  function accountStep(app) {
    const code = h('input', { type: 'text', autocomplete: 'one-time-code', autocapitalize: 'characters', spellcheck: 'false', placeholder: 'ABCD-EFGH', maxlength: '9', class: 'mono' });
    const user = h('input', { type: 'text', autocomplete: 'username', autocapitalize: 'none', spellcheck: 'false', value: 'admin' });
    const pass = h('input', { type: 'password', autocomplete: 'new-password', minlength: '10' });
    const pass2 = h('input', { type: 'password', autocomplete: 'new-password' });
    const msg = h('div', { class: 'msg' });
    const btn = h('button', { class: 'btn btn--primary', type: 'submit' }, t('wizard.account.create'));
    const form = h('form', { class: 'stack', onsubmit: async (ev) => {
      ev.preventDefault(); LL.msg(msg, '');
      if (pass.value !== pass2.value) { LL.msg(msg, t('wizard.account.mismatch'), 'error'); return; }
      btn.disabled = true;
      try {
        await api('/api/setup/account', { method: 'POST', body: { code: code.value, username: user.value, password: pass.value, language: LL.lang }, allow401: true });
        LL.state = await api('/api/state');
        if (LL.passkeySupported()) passkeyOffer(app); else { setStep('networks'); LL.route(); }
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); btn.disabled = false; }
    } },
    h('div', { class: 'callout' }, h('strong', {}, t('wizard.account.code_title')), h('p', {}, t('wizard.account.code_text')),
      h('code', { class: 'cmd' }, 'docker logs lanloom 2>&1 | grep -i "setup code"')),
    LL.field(t('wizard.account.code'), code),
    h('div', { class: 'grid-2' }, LL.field(t('login.username'), user), h('div')),
    h('div', { class: 'grid-2' }, LL.field(t('login.password'), pass, t('wizard.account.password_hint')), LL.field(t('wizard.account.password2'), pass2)),
    msg, actions(h('span'), btn));
    shell(app, 'account', [h('h1', {}, t('wizard.account.title')), h('p', { class: 'lead' }, t('wizard.account.lead')), form]);
    setTimeout(() => { if (document.activeElement === document.body) code.focus(); }, 30);
  }

  function passkeyOffer(app) {
    const msg = h('div', { class: 'msg' });
    shell(app, 'account', [h('h1', {}, t('wizard.passkey.title')), h('p', { class: 'lead' }, t('wizard.passkey.lead')), msg,
      actions(h('button', { class: 'btn btn--ghost', onclick: () => { setStep('networks'); LL.route(); } }, t('wizard.passkey.skip')),
        h('button', { class: 'btn btn--primary', onclick: async () => {
          try { await LL.passkeyRegister(LL.deviceName()); LL.toast(t('passkey.added'), 'ok'); setStep('networks'); LL.route(); }
          catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        } }, t('wizard.passkey.add')))]);
  }

  // ------------------------------------------------------------------ 2 networks
  async function networksStep(app) {
    shell(app, 'networks', [h('h1', {}, t('wizard.networks.title')), h('p', { class: 'lead' }, t('wizard.networks.lead')), h('div', { class: 'skeleton' })]);
    let data;
    try { data = await api('/api/networks'); } catch (e) { data = { networks: [], host_mode: false }; }
    const chosen = new Set(data.networks.filter(n => n.allowed && (n.gateway || data.networks.length === 1)).map(n => n.cidr));
    const list = h('div', { class: 'choice-list' });
    const manual = h('input', { type: 'text', placeholder: '192.168.1.0/24', class: 'mono', spellcheck: 'false' });
    const msg = h('div', { class: 'msg' });
    const extra = [];
    function render() {
      list.replaceChildren(...data.networks.concat(extra).map(n => {
        const cb = h('input', { type: 'checkbox', checked: chosen.has(n.cidr), disabled: !n.allowed, onchange: () => { cb.checked ? chosen.add(n.cidr) : chosen.delete(n.cidr); } });
        return h('label', { class: 'choice' + (n.allowed ? '' : ' is-disabled') }, cb,
          h('span', { class: 'choice__main' }, h('span', { class: 'choice__title mono' }, n.cidr),
            h('span', { class: 'choice__sub' }, [n.iface ? t('wizard.networks.iface', { name: n.iface }) : null, n.self_ip ? t('wizard.networks.self', { ip: n.self_ip }) : null,
              n.gateway ? t('wizard.networks.gateway', { ip: n.gateway }) : null, n.size ? t('wizard.networks.size', { n: n.size }) : null].filter(Boolean).join(' · '))),
          n.allowed ? null : h('span', { class: 'chip chip--warn' }, t(n.reason || 'scan.unsafe.public')));
      }));
      if (!data.networks.length && !extra.length) list.replaceChildren(h('div', { class: 'empty-note' }, t('wizard.networks.none')));
    }
    render();
    const add = h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => {
      const v = manual.value.trim(); if (!v) return;
      if (!/^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/.test(v)) { LL.msg(msg, t('wizard.networks.bad_cidr'), 'error'); return; }
      LL.msg(msg, ''); extra.push({ cidr: v, allowed: true }); chosen.add(v); manual.value = ''; render();
    } }, t('wizard.networks.add'));
    const warn = data.host_mode ? null : h('div', { class: 'callout callout--warn' }, h('strong', {}, t('wizard.networks.isolated_title')), h('p', {}, t('wizard.networks.isolated_text')));
    shell(app, 'networks', [h('h1', {}, t('wizard.networks.title')), h('p', { class: 'lead' }, t('wizard.networks.lead')), warn, list,
      h('div', { class: 'inline-form' }, LL.field(t('wizard.networks.manual'), manual, t('wizard.networks.manual_hint')), add), msg,
      actions(h('span'), h('button', { class: 'btn btn--primary', onclick: () => {
        if (!chosen.size) { LL.msg(msg, t('wizard.networks.pick_one'), 'error'); return; }
        try { sessionStorage.setItem('ll-cidrs', JSON.stringify([...chosen])); } catch (e) { /* ignore */ }
        setStep('scan'); LL.route();
      } }, t('wizard.next')))]);
  }

  // ------------------------------------------------------------------ 3 scan
  async function scanStep(app) {
    let cidrs = [];
    try { cidrs = JSON.parse(sessionStorage.getItem('ll-cidrs') || '[]'); } catch (e) { /* ignore */ }
    const status = await api('/api/scan');
    const running = status.current && status.current.state === 'running';
    const deep = h('input', { type: 'checkbox' });
    const consent = h('input', { type: 'checkbox', disabled: true });
    deep.addEventListener('change', () => { consent.disabled = !deep.checked; if (!deep.checked) consent.checked = false; });
    const msg = h('div', { class: 'msg' });
    const startBtn = h('button', { class: 'btn btn--primary', onclick: async () => {
      LL.msg(msg, '');
      if (deep.checked && !consent.checked) { LL.msg(msg, t('scan.unsafe.consent'), 'error'); return; }
      startBtn.disabled = true;
      try { await api('/api/scan', { method: 'POST', body: { cidrs, deep: deep.checked, consent: consent.checked } }); progressView(app, cidrs); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); startBtn.disabled = false; }
    } }, t('wizard.scan.start'));
    if (running) { progressView(app, cidrs); return; }
    shell(app, 'scan', [h('h1', {}, t('wizard.scan.title')), h('p', { class: 'lead' }, t('wizard.scan.lead', { nets: cidrs.join(', ') || '—' })),
      h('div', { class: 'scan-modes' },
        h('div', { class: 'mode-card is-selected' }, h('div', { class: 'mode-card__head' }, h('span', { class: 'chip chip--ok' }, t('wizard.scan.default')), h('strong', {}, t('wizard.scan.gentle'))),
          h('ul', { class: 'ticks' }, ['gentle_1', 'gentle_2', 'gentle_3', 'gentle_4'].map(k => h('li', {}, t('wizard.scan.' + k))))),
        h('div', { class: 'mode-card' }, h('div', { class: 'mode-card__head' }, h('label', { class: 'check' }, deep, h('strong', {}, t('wizard.scan.deep')))),
          h('p', { class: 'muted' }, t('wizard.scan.deep_text')),
          h('label', { class: 'check check--consent' }, consent, h('span', {}, t('wizard.scan.consent'))))),
      h('p', { class: 'fineprint' }, t('wizard.scan.fineprint')), msg,
      actions(h('button', { class: 'btn btn--ghost', onclick: () => { setStep('networks'); LL.route(); } }, t('wizard.back')), startBtn)]);
  }

  function progressView(app, cidrs) {
    const bar = h('div', { class: 'progress__bar' });
    const phaseList = h('ol', { class: 'phases' }, PHASES.map(p => h('li', { dataset: { p } }, h('span', { class: 'phase-dot' }), t('scan.phase.' + p))));
    const counter = h('div', { class: 'found-counter' }, h('span', { class: 'found-counter__n' }, '0'), h('span', { class: 'found-counter__l' }, t('wizard.scan.found')));
    const sub = h('div', { class: 'muted' }, '');
    const cancel = h('button', { class: 'btn btn--ghost', onclick: () => api('/api/scan', { method: 'DELETE' }) }, t('wizard.scan.cancel'));
    shell(app, 'scan', [h('h1', {}, t('wizard.scan.running')), h('p', { class: 'lead' }, t('wizard.scan.running_lead', { nets: cidrs.join(', ') })),
      h('div', { class: 'scan-live' }, counter, h('div', { class: 'scan-live__right' }, h('div', { class: 'progress' }, bar), sub, phaseList)),
      actions(cancel, h('span'))]);
    const timer = setInterval(async () => {
      let s; try { s = (await api('/api/scan')).current; } catch (e) { return; }
      const pct = s.total ? Math.min(100, Math.round(100 * PHASES.indexOf(s.phase) / PHASES.length + 100 / PHASES.length * (s.done / s.total))) : 4;
      bar.style.width = (s.state === 'done' ? 100 : Math.max(4, pct)) + '%';
      counter.firstChild.textContent = String(s.found || 0);
      sub.textContent = s.total ? t('wizard.scan.addresses', { done: s.done, total: s.total }) : '';
      const pi = PHASES.indexOf(s.phase);
      phaseList.querySelectorAll('li').forEach((li, i) => { li.className = i < pi || s.state === 'done' ? 'is-done' : (i === pi ? 'is-current' : ''); });
      if (s.state !== 'running') {
        clearInterval(timer);
        if (s.state === 'done') { setStep('review'); setTimeout(LL.route, 600); }
        else { setStep('scan'); LL.toast(t(s.error || 'wizard.scan.stopped'), 'warn'); LL.route(); }
      }
    }, 700);
    return { destroy: () => clearInterval(timer) };
  }

  // ------------------------------------------------------------------ 4 review
  const KINDS = ['router', 'switch', 'ap', 'nas', 'server', 'computer', 'phone', 'printer', 'tv', 'media', 'camera', 'hub', 'iot', 'service', 'broker', 'unknown'];
  LL.KINDS = KINDS;
  LL.kindIcon = function (kind) {
    const span = h('span', { class: 'kind-icon', 'aria-hidden': 'true' });
    if (LL.Icons && LL.Icons.svg) span.innerHTML = LL.Icons.svg(kind);
    return span;
  };
  LL.kindSelect = function (value, onchange) {
    const sel = h('select', { class: 'select select--sm', 'aria-label': t('device.kind'), onchange: () => onchange(sel.value) },
      KINDS.map(k => h('option', { value: k }, t('kind.' + k))));
    if (value === 'self') { sel.appendChild(h('option', { value: 'self' }, t('kind.self'))); sel.disabled = true; }
    sel.value = value;
    return sel;
  };
  LL.certaintyChip = c => h('span', { class: 'chip chip--' + ({ sure: 'ok', probable: 'warn', unknown: 'muted' }[c] || 'muted') }, t('certainty.' + (c || 'unknown')));

  async function reviewStep(app) {
    const data = await api('/api/devices');
    const devs = data.devices;
    const counts = { sure: 0, probable: 0, unknown: 0 };
    devs.forEach(d => { counts[d.certainty] = (counts[d.certainty] || 0) + 1; });
    const grid = h('div', { class: 'review-grid' }, devs.map(d => {
      const name = h('input', { class: 'input-plain', value: d.title || '', placeholder: t('kind.' + d.kind), 'aria-label': t('device.name'), maxlength: '48',
        onchange: () => api('/api/devices/' + encodeURIComponent(d.id), { method: 'PATCH', body: { title: name.value } }) });
      return h('div', { class: 'review-card' }, LL.kindIcon(d.kind),
        h('div', { class: 'review-card__main' }, name, h('div', { class: 'review-card__sub mono' }, d.ip + (d.services.length ? ' · ' + d.services.slice(0, 3).join(' ') : ''))),
        h('div', { class: 'review-card__side' }, d.is_self ? h('span', { class: 'chip chip--accent' }, t('kind.self')) :
          LL.kindSelect(d.kind, v => api('/api/devices/' + encodeURIComponent(d.id), { method: 'PATCH', body: { kind: v } })), LL.certaintyChip(d.certainty)));
    }));
    shell(app, 'review', [h('h1', {}, t('wizard.review.title', { n: devs.length })),
      h('p', { class: 'lead' }, t('wizard.review.lead', { sure: counts.sure || 0, probable: counts.probable || 0, unknown: counts.unknown || 0 })),
      devs.length ? grid : h('div', { class: 'empty-note' }, t('wizard.review.none')),
      actions(h('button', { class: 'btn btn--ghost', onclick: () => { setStep('scan'); LL.route(); } }, t('wizard.review.rescan')),
        h('button', { class: 'btn btn--primary', onclick: () => { setStep('connect'); LL.route(); } }, t('wizard.next')))]);
  }

  // ------------------------------------------------------------------ 5 integrations
  const FORMS = {
    hub: [
      { k: 'url', type: 'url', ph: 'http://192.168.1.20:8123', req: true },
      { k: 'token', secret: true, type: 'password', req: true },
      { k: 'allow_self_signed', type: 'checkbox' }],
    mqtt: [
      { k: 'host', ph: '192.168.1.20', req: true }, { k: 'port', type: 'number', ph: '1883' },
      { k: 'username' }, { k: 'password', secret: true, type: 'password' },
      { k: 'tls', type: 'checkbox' }, { k: 'allow_self_signed', type: 'checkbox' }],
    snmp: [
      { k: 'host', ph: '192.168.1.1', req: true }, { k: 'version', type: 'select', options: ['2c', '3'] },
      { k: 'community', secret: true, type: 'password', when: { version: '2c' } },
      { k: 'user', when: { version: '3' } }, { k: 'auth_proto', type: 'select', options: ['sha', 'sha256', 'md5'], when: { version: '3' } },
      { k: 'auth_key', secret: true, type: 'password', when: { version: '3' } }, { k: 'priv_proto', type: 'select', options: ['aes'], when: { version: '3' } },
      { k: 'priv_key', secret: true, type: 'password', when: { version: '3' } }, { k: 'uplink', ph: 'wan' }]
  };

  LL.integrationCard = function (kind, existing, onChange) {
    const inputs = {};
    const fields = FORMS[kind];
    const body = h('div', { class: 'integ__form', hidden: !!existing });
    const msg = h('div', { class: 'msg' });
    const name = h('input', { type: 'text', value: existing ? existing.name : t('integ.' + kind + '.title'), maxlength: '48' });
    const rows = [LL.field(t('integ.field.name'), name)];
    for (const f of fields) {
      let el;
      if (f.type === 'checkbox') {
        el = h('input', { type: 'checkbox', checked: existing ? !!existing.config[f.k] : false });
        inputs[f.k] = el;
        rows.push(h('label', { class: 'check', dataset: { k: f.k } }, el, h('span', {}, t('integ.field.' + f.k))));
        continue;
      }
      if (f.type === 'select') {
        el = h('select', { class: 'select' }, f.options.map(o => h('option', { value: o }, t('integ.opt.' + o) === 'integ.opt.' + o ? o : t('integ.opt.' + o))));
        el.value = existing && existing.config[f.k] ? existing.config[f.k] : f.options[0];
        el.addEventListener('change', applyWhen);
      } else {
        el = h('input', { type: f.type || 'text', placeholder: f.secret && existing && existing.secrets[f.k] ? t('integ.secret_set') : (f.ph || ''),
          autocomplete: f.secret ? 'new-password' : 'off', spellcheck: 'false', value: !f.secret && existing && existing.config[f.k] != null ? existing.config[f.k] : '' });
      }
      inputs[f.k] = el;
      const wrap = LL.field(t('integ.field.' + f.k) + (f.req ? '' : ' ' + t('integ.optional')), el, t('integ.hint.' + kind + '.' + f.k) === 'integ.hint.' + kind + '.' + f.k ? null : t('integ.hint.' + kind + '.' + f.k));
      wrap.dataset.k = f.k;
      rows.push(wrap);
    }
    function applyWhen() {
      for (const f of fields) {
        if (!f.when) continue;
        const show = Object.entries(f.when).every(([k, v]) => inputs[k] && inputs[k].value === v);
        const row = body.querySelector('[data-k="' + f.k + '"]');
        if (row) row.hidden = !show;
      }
    }
    function collect() {
      const config = {}, secrets = {};
      for (const f of fields) {
        const el = inputs[f.k];
        const row = body.querySelector('[data-k="' + f.k + '"]');
        if (row && row.hidden) continue;
        const v = f.type === 'checkbox' ? el.checked : el.value.trim();
        if (f.secret) { if (v) secrets[f.k] = v; } else if (v !== '' && v !== false) config[f.k] = f.type === 'number' ? Number(v) : v;
      }
      return { kind, name: name.value, config, secrets, id: existing ? existing.id : undefined };
    }
    const testBtn = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
      LL.msg(msg, t('integ.testing')); testBtn.disabled = true;
      try {
        const r = await api('/api/integrations/test', { method: 'POST', body: collect() });
        const det = r.details || {};
        const extra = Object.keys(det).length ? ' — ' + Object.entries(det).filter(([, v]) => v != null && v !== '').map(([k, v]) => t('integ.detail.' + k) + ': ' + v).join(', ') : '';
        LL.msg(msg, (r.ok ? t('integ.test_ok') : t(r.key || 'integ.test_failed')) + (r.ok ? extra : ''), r.ok ? 'ok' : 'error');
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      testBtn.disabled = false;
    } }, t('integ.test'));
    const saveBtn = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      saveBtn.disabled = true;
      try { const r = await api('/api/integrations', { method: 'POST', body: collect() }); LL.toast(t('integ.saved'), 'ok'); if (onChange) onChange(r); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      saveBtn.disabled = false;
    } }, t('integ.save'));
    const delBtn = existing ? h('button', { class: 'btn btn--danger-ghost', type: 'button', onclick: async () => {
      if (!confirm(t('integ.delete_confirm'))) return;
      await api('/api/integrations/' + existing.id, { method: 'DELETE' }); if (onChange) onChange(null);
    } }, t('integ.delete')) : null;
    body.append(...rows, msg, h('div', { class: 'integ__actions' }, delBtn || h('span'), h('div', { class: 'btn-row' }, testBtn, saveBtn)));
    applyWhen();
    const statusChip = existing ? h('span', { class: 'chip chip--' + (existing.status && existing.status.ok === false ? 'danger' : (existing.status && existing.status.ok ? 'ok' : 'muted')) },
      existing.status && existing.status.key ? t(existing.status.key) : t('integ.saved_state')) : null;
    const toggle = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', 'aria-expanded': existing ? 'false' : 'true', onclick: () => {
      body.hidden = !body.hidden; toggle.setAttribute('aria-expanded', String(!body.hidden)); toggle.textContent = body.hidden ? t('integ.edit') : t('integ.close');
    } }, existing ? t('integ.edit') : t('integ.close'));
    return h('article', { class: 'integ card card--flat' },
      h('header', { class: 'integ__head' }, LL.kindIcon({ hub: 'hub', mqtt: 'broker', snmp: 'router' }[kind]),
        h('div', { class: 'integ__titles' }, h('h3', {}, existing ? existing.name : t('integ.' + kind + '.title')), h('p', { class: 'muted' }, t('integ.' + kind + '.desc'))),
        statusChip, toggle),
      body);
  };

  async function connectStep(app) {
    const wrap = h('div', { class: 'stack' });
    async function render() {
      const list = (await api('/api/integrations')).integrations;
      const cards = ['hub', 'mqtt', 'snmp'].map(kind => {
        const ex = list.filter(i => i.kind === kind);
        return ex.length ? ex.map(e => LL.integrationCard(kind, e, render)) : [LL.integrationCard(kind, null, render)];
      }).flat();
      // collapse fresh forms in the wizard so the page stays short
      cards.forEach(c => { const b = c.querySelector('.integ__form'); const tg = c.querySelector('.integ__head .btn'); if (b && !b.hidden) { b.hidden = true; tg.textContent = t('integ.setup'); tg.setAttribute('aria-expanded', 'false'); } });
      wrap.replaceChildren(...cards);
    }
    shell(app, 'connect', [h('h1', {}, t('wizard.connect.title')), h('p', { class: 'lead' }, t('wizard.connect.lead')),
      h('div', { class: 'callout callout--info' }, h('p', {}, t('wizard.connect.safety'))), wrap,
      actions(h('button', { class: 'btn btn--ghost', onclick: () => { setStep('review'); LL.route(); } }, t('wizard.back')),
        h('button', { class: 'btn btn--primary', onclick: () => { setStep('done'); LL.route(); } }, t('wizard.next')))]);
    await render();
  }

  // ------------------------------------------------------------------ 6 done
  async function doneStep(app) {
    const s = await api('/api/settings');
    const rescan = h('select', { class: 'select' }, [0, 1, 3, 6, 12, 24].map(n => h('option', { value: String(n) }, n ? t('settings.rescan.every', { n }) : t('settings.rescan.never'))));
    rescan.value = String(s.rescan_hours);
    const upd = h('input', { type: 'checkbox', checked: !!s.update_check });
    shell(app, 'done', [h('h1', {}, t('wizard.done.title')), h('p', { class: 'lead' }, t('wizard.done.lead')),
      h('div', { class: 'stack' }, LL.field(t('settings.rescan'), rescan, t('settings.rescan.hint')),
        h('label', { class: 'check' }, upd, h('span', {}, t('settings.update_check'))), h('p', { class: 'field__hint' }, t('settings.update_check.hint'))),
      actions(h('button', { class: 'btn btn--ghost', onclick: () => { setStep('connect'); LL.route(); } }, t('wizard.back')),
        h('button', { class: 'btn btn--primary', onclick: async () => {
          await api('/api/settings', { method: 'PUT', body: { rescan_hours: Number(rescan.value), update_check: upd.checked, setup_done: true, language: LL.lang } });
          try { sessionStorage.removeItem('ll-wiz'); } catch (e) { /* ignore */ }
          LL.go('#/map');
        } }, t('wizard.done.open')))]);
  }

  LL.view('setup', async function (app, rest, st) {
    if (st.setup_needed) return accountStep(app);
    const step = rest[0] && STEPS.includes(rest[0]) ? rest[0] : stepKey();
    setStep(step);
    if (step === 'scan') return scanStep(app);
    if (step === 'review') return reviewStep(app);
    if (step === 'connect') return connectStep(app);
    if (step === 'done') return doneStep(app);
    return networksStep(app);
  });
})();
