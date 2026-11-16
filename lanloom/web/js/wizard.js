/* Lanloom — first-start setup wizard and the integration forms (shared with Settings). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const STEPS = ['account', 'networks', 'scan', 'review', 'connect', 'done'];
  const PHASES = ['neighbors', 'mdns', 'ssdp', 'ports', 'banners', 'identify'];

  function stepKey() { try { return sessionStorage.getItem('ll-wiz') || 'networks'; } catch (e) { return 'networks'; } }
  function setStep(s) { try { sessionStorage.setItem('ll-wiz', s); } catch (e) { /* ignore */ } }

  function shell(app, step, body) {
    const idx = STEPS.indexOf(step);
    const stepper = h('ol', { class: 'stepper', 'aria-label': t('wizard.progress') },
      STEPS.map((s, i) => h('li', { class: 'stepper__item' + (i < idx ? ' is-done' : '') + (i === idx ? ' is-current' : ''), 'aria-current': i === idx ? 'step' : null },
        h('span', { class: 'stepper__num' }, i < idx ? '✓' : String(i + 1)), h('span', { class: 'stepper__label' }, t('wizard.step.' + s)))));
    app.replaceChildren(h('main', { class: 'wizard' },
      h('header', { class: 'wizard__head' }, h('div', { class: 'wizard__brand' }, LL.logo(30), h('span', { class: 'brand-name' }, 'Lanloom'),
        LL.state && LL.state.demo ? h('span', { class: 'chip chip--demo' }, t('app.demo')) : null),
        h('div', { class: 'wizard__head-right' }, LL.state && LL.state.setup_done ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: leaveAgain }, t('setup.again.leave')) : null,
          langSwitch())),
      stepper,
      h('section', { class: 'wizard__card card' }, body),
      LL.footer()));
  }

  function langSwitch() {
    const sel = h('select', { class: 'select select--sm', 'aria-label': t('settings.language'), onchange: async () => { await LL.loadLang(sel.value); LL.route(); } },
      LL.langOptions());
    sel.value = LL.lang;
    return sel;
  }

  function actions(...kids) { return h('div', { class: 'wizard__actions' }, ...kids); }
  function leaveAgain() { try { sessionStorage.removeItem('ll-wiz-again'); sessionStorage.removeItem('ll-wiz'); } catch (e) { /* ignore */ } LL.go('#/map'); }
  LL.setupAgain = function () { try { sessionStorage.setItem('ll-wiz-again', '1'); } catch (e) { /* ignore */ } setStep('networks'); LL.go('#/setup'); };
  LL.setupAgainActive = function () { try { return sessionStorage.getItem('ll-wiz-again') === '1'; } catch (e) { return false; } };
  // the sample network: a read-only demo view next to the real inventory, left with one click
  LL.demoView = async function (on) {
    await api('/api/setup/demo', { method: 'POST', body: { on: !!on } });
    LL.state = await api('/api/state');
    LL.go(on ? '#/map' : (LL.state.setup_done ? '#/map' : '#/setup'));
  };
  function fmtDuration(sec) { sec = Math.max(0, Math.round(sec)); return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); }
  LL.fmtSetupDuration = fmtDuration;

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

  // ------------------------------------------------------------------ 2 networks (detected, proposed, agreed)
  async function networksStep(app) {
    shell(app, 'networks', [h('h1', {}, t('wizard.networks.title')), h('p', { class: 'lead' }, t('setup.detect.lead')), h('div', { class: 'skeleton' })]);
    let data;
    try { data = await api('/api/setup/discover'); } catch (e) { data = { networks: [], host_mode: false, dns: [], dhcp: [] }; }
    let saved = [];
    try { saved = JSON.parse(sessionStorage.getItem('ll-cidrs') || '[]'); } catch (e) { /* ignore */ }
    const chosen = new Set(saved.length ? saved.filter(c => data.networks.some(n => n.cidr === c && n.allowed)) : data.networks.filter(n => n.suggested).map(n => n.cidr));
    const list = h('div', { class: 'choice-list' });
    const manual = h('input', { type: 'text', placeholder: '192.168.1.0/24', class: 'mono', spellcheck: 'false' });
    const msg = h('div', { class: 'msg' });
    const consent = h('input', { type: 'checkbox' });
    const extra = [];
    function render() {
      list.replaceChildren(...data.networks.concat(extra).map(n => {
        const cb = h('input', { type: 'checkbox', checked: chosen.has(n.cidr), disabled: !n.allowed, onchange: () => { cb.checked ? chosen.add(n.cidr) : chosen.delete(n.cidr); } });
        const sub = [n.iface ? t('wizard.networks.iface', { name: n.iface }) : null, n.self_ip ? t('wizard.networks.self', { ip: n.self_ip }) : null,
          n.gateway ? t('wizard.networks.gateway', { ip: n.gateway }) : null, n.size ? t('wizard.networks.size', { n: n.size }) : null,
          n.neighbours ? t('setup.detect.neighbours', { n: n.neighbours }) : null].filter(Boolean).join(' · ');
        return h('label', { class: 'choice' + (n.allowed ? '' : ' is-disabled') }, cb,
          h('span', { class: 'choice__main' }, h('span', { class: 'choice__title mono' }, n.cidr), h('span', { class: 'choice__sub' }, sub),
            n.narrowed ? h('span', { class: 'choice__sub' }, t('setup.detect.narrowed', { net: n.network })) : null),
          n.allowed ? (n.suggested ? h('span', { class: 'chip chip--ok' }, t('setup.detect.proposed')) : (n.routed ? h('span', { class: 'chip chip--muted' }, t('setup.detect.routed')) : null))
            : h('span', { class: 'chip chip--warn' }, t(n.reason || 'scan.unsafe.public')));
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
    const found = LL.facts([[t('setup.detect.gateway'), data.gateway || t('setup.detect.unknown')], [t('setup.detect.dns'), (data.dns || []).join(', ') || t('setup.detect.unknown')],
      [t('setup.detect.dhcp'), (data.dhcp || []).join(', ') || t('setup.detect.unknown')]]);
    function pick() {
      if (!chosen.size) { LL.msg(msg, t('wizard.networks.pick_one'), 'error'); return null; }
      if (!consent.checked) { LL.msg(msg, t('setup.detect.consent_needed'), 'error'); return null; }
      try { sessionStorage.setItem('ll-cidrs', JSON.stringify([...chosen])); } catch (e) { /* ignore */ }
      return [...chosen];
    }
    const go = h('button', { class: 'btn btn--primary', onclick: async () => {
      const cidrs = pick(); if (!cidrs) return;
      go.disabled = true;
      try { await api('/api/scan', { method: 'POST', body: { cidrs, deep: false, consent: false } }); setStep('scan'); progressView(app, cidrs); }
      catch (e) { if (e && e.key === 'scan.err.running') { setStep('scan'); progressView(app, cidrs); return; } LL.msg(msg, LL.errText(e), 'error'); go.disabled = false; }
    } }, t('setup.detect.scan_now'));
    const options = h('button', { class: 'btn btn--ghost', onclick: () => { if (pick()) { setStep('scan'); LL.route(); } } }, t('setup.detect.options'));
    const demo = LL.state && LL.state.demo ? null : h('div', { class: 'callout callout--info setup-demo' }, h('strong', {}, t('setup.demo.title')), h('p', {}, t('setup.demo.text')),
      h('div', {}, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.demoView(true).catch(e => LL.msg(msg, LL.errText(e), 'error')) }, t('setup.demo.start'))));
    shell(app, 'networks', [h('h1', {}, t('wizard.networks.title')), h('p', { class: 'lead' }, t('setup.detect.lead')), warn,
      h('section', { class: 'setup-found' }, h('h2', { class: 'setup-sub' }, t('setup.detect.found')), found), list,
      h('div', { class: 'inline-form' }, LL.field(t('wizard.networks.manual'), manual, t('wizard.networks.manual_hint')), add),
      h('label', { class: 'check setup-consent' }, consent, h('span', {}, t('wizard.scan.consent'))), msg,
      actions(options, go), demo]);
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
    const snmpPub = h('input', { type: 'checkbox' });
    const msg = h('div', { class: 'msg' });
    const startBtn = h('button', { class: 'btn btn--primary', onclick: async () => {
      LL.msg(msg, '');
      if (deep.checked && !consent.checked) { LL.msg(msg, t('scan.unsafe.consent'), 'error'); return; }
      startBtn.disabled = true;
      try { await api('/api/scan', { method: 'POST', body: { cidrs, deep: deep.checked, consent: consent.checked, snmp_public: snmpPub.checked } }); progressView(app, cidrs); }
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
      h('label', { class: 'check' }, snmpPub, h('span', {}, t('scan.snmp_public'))), h('p', { class: 'muted fineprint' }, t('scan.snmp_public.hint')),
      h('p', { class: 'fineprint' }, t('wizard.scan.fineprint')), msg,
      actions(h('button', { class: 'btn btn--ghost', onclick: () => { setStep('networks'); LL.route(); } }, t('wizard.back')), startBtn)]);
  }

  function progressView(app, cidrs) {
    const bar = h('div', { class: 'progress__bar' });
    const phaseList = h('ol', { class: 'phases' }, PHASES.map(p => h('li', { dataset: { p } }, h('span', { class: 'phase-dot' }), t('scan.phase.' + p))));
    const counter = h('div', { class: 'found-counter' }, h('span', { class: 'found-counter__n' }, '0'), h('span', { class: 'found-counter__l' }, t('wizard.scan.found')));
    const sub = h('div', { class: 'muted' }, '');
    const clock = h('span', { class: 'setup-clock mono', 'aria-live': 'off' }, '');
    const cancel = h('button', { class: 'btn btn--ghost', onclick: () => api('/api/scan', { method: 'DELETE' }) }, t('wizard.scan.cancel'));
    const first = h('div', { class: 'callout callout--ok setup-first', hidden: true });
    shell(app, 'scan', [h('h1', {}, t('wizard.scan.running')), h('p', { class: 'lead' }, t('wizard.scan.running_lead', { nets: cidrs.join(', ') })),
      h('div', { class: 'scan-live' }, counter, h('div', { class: 'scan-live__right' }, h('div', { class: 'progress' }, bar), sub, phaseList)),
      first, h('p', { class: 'muted fineprint' }, t('setup.time.elapsed'), ' ', clock),
      actions(cancel, h('span'))]);
    let t0 = null;
    api('/api/setup/timing').then(tm => { t0 = tm.started; }).catch(() => {});
    const tick = () => { if (t0) clock.textContent = fmtDuration(Date.now() / 1000 - t0); };
    function showFirst(n) {
      if (!first.hidden) { first.querySelector('strong').textContent = t('setup.first.title', { n }); return; }
      first.append(h('strong', {}, t('setup.first.title', { n })), h('p', {}, t('setup.first.text')),
        h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: async () => {
          await api('/api/settings', { method: 'PUT', body: { setup_done: true, language: LL.lang } });
          try { sessionStorage.setItem('ll-wiz', 'connect'); sessionStorage.removeItem('ll-wiz-again'); } catch (e) { /* ignore */ }
          LL.go('#/map');
        } }, t('setup.first.open'))));
      first.hidden = false;
    }
    const timer = setInterval(async () => {
      tick();
      let s; try { s = (await api('/api/scan')).current; } catch (e) { return; }
      if (s.preview && s.state === 'running' && !(LL.state && LL.state.setup_done)) showFirst(s.preview);
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
      { k: 'priv_key', secret: true, type: 'password', when: { version: '3' } }, { k: 'uplink', ph: 'wan' }],
    controller: [
      { k: 'url', type: 'url', ph: 'https://192.168.1.2:8443', req: true },
      { k: 'username', req: true }, { k: 'password', secret: true, type: 'password', req: true },
      { k: 'site', ph: 'default' }, { k: 'style', type: 'select', options: ['auto', 'console', 'classic'] },
      { k: 'allow_self_signed', type: 'checkbox' }]
  };

  // device-family adapters (built in, listed by the server with their form); their names come from the server
  LL.families = [];
  LL.loadIntegrations = async function () {
    const data = await api('/api/integrations');
    LL.families = data.families || [];
    return data;
  };
  const family = kind => (FORMS[kind] ? null : LL.families.find(f => f.family === kind) || null);
  const famTitle = fam => { if (fam.sample) return t(fam.sample === 'hypervisor' ? 'integ.family.sample_hv' : 'integ.family.sample'); const k = 'integrations.family.' + fam.family; return t(k) === k ? fam.label : t(k); };
  LL.integTitle = kind => { const fam = family(kind); return fam ? famTitle(fam) : t('integ.' + kind + '.title'); };
  LL.integKinds = () => ['hub', 'mqtt', 'snmp', 'controller'].concat(LL.families.map(f => f.family));

  LL.integrationCard = function (kind, existing, onChange, opts) {
    const inputs = {};
    const pre = (opts && opts.prefill) || {};
    const fam = family(kind);
    const fields = FORMS[kind] || (fam ? fam.form : []);
    // texts per kind of family (a hypervisor reads guests), never per product: 'integ.hint.hv.<field>' before 'integ.hint.family.<field>'
    const famKind = fam && ((fam.capabilities || {}).read || []).includes('guests') ? 'hv' : 'family';
    const hintKey = k => fam ? (t('integ.hint.' + famKind + '.' + k) !== 'integ.hint.' + famKind + '.' + k ? 'integ.hint.' + famKind + '.' + k : 'integ.hint.family.' + k) : 'integ.hint.' + kind + '.' + k;
    const body = h('div', { class: 'integ__form', hidden: !!existing });
    const msg = h('div', { class: 'msg' });
    const name = h('input', { type: 'text', value: existing ? existing.name : ((opts && opts.name) || LL.integTitle(kind)), maxlength: '48' });
    const rows = [LL.field(t('integ.field.name'), name)];
    for (const f of fields) {
      let el;
      if (f.type === 'checkbox') {
        el = h('input', { type: 'checkbox', checked: existing ? !!existing.config[f.k] : !!pre[f.k] });
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
          autocomplete: f.secret ? 'new-password' : 'off', spellcheck: 'false',
          value: !f.secret && existing && existing.config[f.k] != null ? existing.config[f.k] : (!f.secret && pre[f.k] != null ? String(pre[f.k]) : '') });
      }
      inputs[f.k] = el;
      const wrap = LL.field(t('integ.field.' + f.k) + (f.req ? '' : ' ' + t('integ.optional')), el, t(hintKey(f.k)) === hintKey(f.k) ? null : t(hintKey(f.k)));
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
        const fix = !r.ok && r.category ? ' ' + t('setup.fix.' + r.category) : '';
        LL.msg(msg, (r.ok ? t('integ.test_ok') : t(r.key || 'integ.test_failed')) + (r.ok ? extra : fix), r.ok ? 'ok' : 'error');
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
    if (opts && opts.bare) { body.hidden = false; return h('div', { class: 'integ integ--bare' }, body); }   // inside a suggestion: the form only
    const statusChip = existing ? h('span', { class: 'chip chip--' + (existing.status && existing.status.ok === false ? 'danger' : (existing.status && existing.status.ok ? 'ok' : 'muted')) },
      existing.status && existing.status.key ? t(existing.status.key) : t('integ.saved_state')) : null;
    const toggle = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', 'aria-expanded': existing ? 'false' : 'true', onclick: () => {
      body.hidden = !body.hidden; toggle.setAttribute('aria-expanded', String(!body.hidden)); toggle.textContent = body.hidden ? t('integ.edit') : t('integ.close');
    } }, existing ? t('integ.edit') : t('integ.close'));
    return h('article', { class: 'integ card card--flat' },
      h('header', { class: 'integ__head' }, LL.kindIcon({ hub: 'hub', mqtt: 'broker', snmp: 'router', controller: 'switch' }[kind] || 'router'),
        h('div', { class: 'integ__titles' }, h('h3', {}, existing ? existing.name : LL.integTitle(kind)),
          h('p', { class: 'muted' }, fam ? (famKind === 'hv' ? t('integ.family.desc.hv') : t('integ.family.desc')) : t('integ.' + kind + '.desc')),
          fam && fam.docs ? h('p', { class: 'integ__fine muted' }, t('integ.family.notice')) : null),
        statusChip, toggle),
      body);
  };

  // one guide per kind of source: numbered steps from the translations (setup.guide.<guide>.1 ... n)
  LL.setupGuide = function (guide, steps) {
    const n = (steps && steps[guide]) || 4;
    const items = [];
    for (let i = 1; i <= n; i++) items.push(h('li', {}, t('setup.guide.' + guide + '.' + i)));
    return h('div', { class: 'setup-guide' }, h('h4', {}, t('setup.guide.' + guide + '.title')), h('ol', { class: 'setup-guide__steps' }, items),
      h('p', { class: 'muted fineprint' }, t('setup.guide.readonly')));
  };
  function suggestionCard(sg, guides, onChange) {
    const titleKey = 'setup.suggest.kind.' + sg.guide;
    const reasons = h('div', { class: 'chip-row' }, sg.reasons.map(r => h('span', { class: 'chip chip--muted' }, t(r.key, r.params || {}))));
    const body = h('div', { class: 'setup-suggest__body', hidden: true });
    const btn = h('button', { class: 'btn btn--' + (sg.configured ? 'ghost' : 'primary') + ' btn--sm', type: 'button', 'aria-expanded': 'false', onclick: () => {
      if (!body.childNodes.length) body.append(LL.setupGuide(sg.guide, guides), LL.integrationCard(sg.kind, null, onChange, { prefill: sg.prefill, name: sg.title, bare: true }));
      body.hidden = !body.hidden; btn.setAttribute('aria-expanded', String(!body.hidden));
      btn.textContent = body.hidden ? (sg.configured ? t('setup.suggest.again') : t('integ.setup')) : t('integ.close');
    } }, sg.configured ? t('setup.suggest.again') : t('integ.setup'));
    return h('article', { class: 'setup-suggest card card--flat' + (sg.configured ? ' is-configured' : '') },
      h('header', { class: 'setup-suggest__head' }, LL.kindIcon({ hub: 'hub', mqtt: 'broker', snmp: 'router', controller: 'switch', family: 'switch', hv: 'server' }[sg.guide] || sg.device_kind || 'unknown'),
        h('div', { class: 'setup-suggest__titles' }, h('h3', {}, t(titleKey) === titleKey ? LL.integTitle(sg.kind) : t(titleKey)),
          h('p', { class: 'muted' }, h('span', {}, sg.title), ' · ', h('span', { class: 'mono' }, sg.ip)), reasons),
        sg.configured ? h('span', { class: 'chip chip--ok' }, t('setup.suggest.connected')) : LL.certaintyChip(sg.certainty), btn),
      body);
  }

  async function connectStep(app) {
    const sugWrap = h('div', { class: 'stack' }, h('div', { class: 'skeleton' }));
    const wrap = h('div', { class: 'stack' });
    async function renderSuggestions() {
      let data;
      try { await LL.loadIntegrations(); data = await api('/api/setup/suggestions'); } catch (e) { data = { suggestions: [] }; }
      const list = data.suggestions || [];
      sugWrap.replaceChildren(...(list.length ? list.map(sg => suggestionCard(sg, data.guides, () => { renderSuggestions(); render(); }))
        : [h('div', { class: 'empty-note' }, t('setup.suggest.none'))]));
    }
    async function render() {
      const list = (await LL.loadIntegrations()).integrations;
      const cards = LL.integKinds().map(kind => {
        const ex = list.filter(i => i.kind === kind);
        return ex.length ? ex.map(e => LL.integrationCard(kind, e, () => { render(); renderSuggestions(); })) : [LL.integrationCard(kind, null, () => { render(); renderSuggestions(); })];
      }).flat();
      // collapse fresh forms in the wizard so the page stays short
      cards.forEach(c => { const b = c.querySelector('.integ__form'); const tg = c.querySelector('.integ__head .btn'); if (b && !b.hidden) { b.hidden = true; tg.textContent = t('integ.setup'); tg.setAttribute('aria-expanded', 'false'); } });
      wrap.replaceChildren(...cards);
    }
    shell(app, 'connect', [h('h1', {}, t('wizard.connect.title')), h('p', { class: 'lead' }, t('setup.suggest.lead')),
      h('div', { class: 'callout callout--info' }, h('p', {}, t('wizard.connect.safety'))),
      h('h2', { class: 'setup-sub' }, t('setup.suggest.title')), sugWrap,
      h('details', { class: 'setup-more' }, h('summary', {}, t('setup.suggest.more')), wrap),
      actions(h('button', { class: 'btn btn--ghost', onclick: () => { setStep('review'); LL.route(); } }, t('wizard.back')),
        h('button', { class: 'btn btn--primary', onclick: () => { setStep('done'); LL.route(); } }, t('wizard.next')))]);
    await Promise.all([renderSuggestions(), render()]);
  }

  // ------------------------------------------------------------------ 6 done
  async function doneStep(app) {
    const s = await api('/api/settings');
    const rescan = h('select', { class: 'select' }, [0, 1, 3, 6, 12, 24].map(n => h('option', { value: String(n) }, n ? t('settings.rescan.every', { n }) : t('settings.rescan.never'))));
    rescan.value = String(s.rescan_hours);
    const upd = h('input', { type: 'checkbox', checked: !!s.update_check });
    let tm = {};
    try { tm = await api('/api/setup/timing'); } catch (e) { /* ignore */ }
    const timeNote = tm.first_picture_s != null ? h('div', { class: 'callout callout--ok' }, h('strong', {}, t('setup.time.first', { time: fmtDuration(tm.first_picture_s) })),
      h('p', {}, t('setup.time.first_text'))) : null;
    shell(app, 'done', [h('h1', {}, t('wizard.done.title')), h('p', { class: 'lead' }, t('wizard.done.lead')), timeNote,
      h('div', { class: 'stack' }, LL.field(t('settings.rescan'), rescan, t('settings.rescan.hint')),
        h('label', { class: 'check' }, upd, h('span', {}, t('settings.update_check'))), h('p', { class: 'field__hint' }, t('settings.update_check.hint'))),
      actions(h('button', { class: 'btn btn--ghost', onclick: () => { setStep('connect'); LL.route(); } }, t('wizard.back')),
        h('button', { class: 'btn btn--primary', onclick: async () => {
          await api('/api/settings', { method: 'PUT', body: { rescan_hours: Number(rescan.value), update_check: upd.checked, setup_done: true, language: LL.lang } });
          try { sessionStorage.removeItem('ll-wiz'); sessionStorage.removeItem('ll-wiz-again'); } catch (e) { /* ignore */ }
          LL.go('#/map');
        } }, t('wizard.done.open')))]);
  }

  LL.view('setup', async function (app, rest, st) {
    if (st.setup_needed) return accountStep(app);
    const step = rest[0] && STEPS.includes(rest[0]) ? rest[0] : stepKey();
    setStep(step);
    // a step in the address (#/setup/<step>) is taken once; the next steps move on from the stored step
    if (rest[0]) history.replaceState(null, '', LL.base + '#/setup');
    if (step === 'scan') return scanStep(app);
    if (step === 'review') return reviewStep(app);
    if (step === 'connect') return connectStep(app);
    if (step === 'done') return doneStep(app);
    return networksStep(app);
  });
})();
