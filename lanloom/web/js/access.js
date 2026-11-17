/* Lanloom — access: step-up confirmation, security notices, invitation and reset links, second-factor set-up,
   Settings > Account, Settings > Users, Settings > Security (policy and audit log).
   The server decides every right (lanloom/access.py); this file only shows what a role can use. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const card = (title, kids, cls) => h('section', { class: 'card settings-card' + (cls ? ' ' + cls : '') }, h('h2', {}, title), kids);
  // replaceChildren() would print "null" for the optional parts; this drops them
  const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false));
  const roleChip = role => h('span', { class: 'chip role-chip role-chip--' + role }, t('role.name.' + role));

  function copyText(text, btn) {
    const done = () => { if (btn) { const old = btn.textContent; btn.textContent = t('access.copied'); setTimeout(() => { btn.textContent = old; }, 1600); } };
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(done, () => fallback()); return; }
    fallback();
    function fallback() {
      const ta = h('textarea', { class: 'sr-only', readonly: true }); ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { /* the text stays selectable on screen */ }
      ta.remove();
    }
  }
  // a confirmation in the app's own dialog (no browser pop-up)
  function ask(text, okLabel) {
    return new Promise(resolve => {
      let answered = false;
      const yes = h('button', { class: 'btn btn--danger', type: 'button', onclick: () => { answered = true; d.close(); resolve(true); } }, okLabel);
      const no = h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => d.close() }, t('access.cancel'));
      const d = LL.dialog(t('access.sure'), h('p', {}, text), h('div', { class: 'btn-row' }, no, yes));
      const obs = new MutationObserver(() => { if (!d.el.isConnected) { obs.disconnect(); if (!answered) resolve(false); } });
      obs.observe(document.body, { childList: true });
      setTimeout(() => yes.focus(), 30);
    });
  }
  function linkFor(token) { return location.origin + LL.base + '#/invite/' + token; }
  const agentText = a => (LL.strings['agent.' + a] ? t('agent.' + a) : (a || t('agent.unknown')));
  function when(ts) { return ts ? LL.fmtDate(ts) : '—'; }
  function day(ts) {
    if (!ts) return '—';
    try { return new Date(ts * 1000).toLocaleDateString(LL.locale(), { day: 'numeric', month: 'short' }); } catch (e) { return when(ts); }
  }

  // ------------------------------------------------------------------ step-up: confirm a critical action
  let stepUpOpen = null;
  LL.stepUp = function (methods) {
    if (stepUpOpen) return stepUpOpen;
    stepUpOpen = new Promise(resolve => {
      let kind = methods.includes('passkey') && LL.passkeySupported() ? 'passkey' : (methods.find(m => m !== 'passkey') || 'password');
      const msg = h('div', { class: 'msg' });
      const input = h('input', { class: 'input code-input', type: 'text', autocomplete: 'one-time-code', spellcheck: 'false', autocapitalize: 'none', id: 'stepup-input' });
      const label = h('label', { class: 'field__label', for: 'stepup-input' });
      const field = h('div', { class: 'field' }, label, input);
      const ok = h('button', { class: 'btn btn--primary', type: 'submit' }, t('stepup.confirm'));
      const pkBtn = h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: () => viaPasskey() }, t('stepup.passkey'));
      const others = h('div', { class: 'second-switch' });
      const form = h('form', { class: 'stack', onsubmit: async ev => {
        ev.preventDefault(); ok.disabled = true; LL.msg(msg, '');
        try {
          const b = kind === 'password' ? { kind, password: input.value } : { kind, code: input.value };
          await api('/api/account/stepup', { method: 'POST', body: b, noStepUp: true });
          finish(true);
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); ok.disabled = false; input.select(); }
      } }, field, h('div', { class: 'btn-row' }, ok));
      let resolved = false;
      const d = LL.dialog(t('stepup.title'), h('div', { class: 'stack stepup' }, h('p', { class: 'muted' }, t('stepup.lead')), pkBtn, form, msg, others));
      const observer = new MutationObserver(() => { if (!d.el.isConnected) finish(false); });
      observer.observe(document.body, { childList: true });
      function finish(v) { if (resolved) return; resolved = true; observer.disconnect(); if (d.el.isConnected) d.close(); stepUpOpen = null; resolve(v); }
      async function viaPasskey() {
        LL.msg(msg, '');
        try {
          const o = await api('/api/account/stepup/options', { method: 'POST', body: {}, noStepUp: true });
          const credential = await LL.passkeyAssert(o);
          await api('/api/account/stepup', { method: 'POST', body: { kind: 'passkey', ceremony: o.ceremony, credential }, noStepUp: true });
          finish(true);
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      }
      function show() {
        pkBtn.hidden = kind !== 'passkey';
        form.hidden = kind === 'passkey';
        label.textContent = t('stepup.label.' + kind);
        input.type = kind === 'password' ? 'password' : 'text';
        input.setAttribute('autocomplete', kind === 'password' ? 'current-password' : 'one-time-code');
        input.setAttribute('inputmode', kind === 'totp' ? 'numeric' : 'text');
        input.value = '';
        fill(others, ...methods.filter(m => m !== kind && (m !== 'passkey' || LL.passkeySupported())).map(m =>
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => { kind = m; show(); if (m !== 'passkey') input.focus(); } }, t('stepup.use.' + m))));
        if (kind !== 'passkey') setTimeout(() => input.focus(), 30);
      }
      show();
    });
    return stepUpOpen;
  };

  // ------------------------------------------------------------------ security notices (banner under the top bar)
  LL.noticeBar = function () {
    const n = (LL.state && LL.state.notices) || 0;
    if (!n) return null;
    const bar = h('div', { class: 'notice-bar', role: 'status' });
    api('/api/account/notices').then(r => {
      if (!r.notices.length) { bar.remove(); return; }
      const items = r.notices.slice(0, 3).map(x => h('li', {}, h('strong', {}, t('notice.title.' + x.kind)), ' ',
        h('span', {}, t('notice.text.' + x.kind, Object.assign({}, x.data || {}, { when: LL.fmtDate(x.t), agent: agentText((x.data || {}).agent) })))));
      const more = r.notices.length > 3 ? h('li', { class: 'muted' }, t('notice.more', { n: r.notices.length - 3 })) : null;
      fill(bar, h('span', { class: 'notice-bar__icon', 'aria-hidden': 'true' }, '!'), h('ul', { class: 'notice-bar__list' }, items, more),
        h('div', { class: 'notice-bar__tools' },
          h('a', { class: 'btn btn--ghost btn--sm', href: '#/settings/account' }, t('notice.review')),
          h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: async () => {
            try { await api('/api/account/notices/seen', { method: 'POST', body: {} }); LL.state.notices = 0; bar.remove(); } catch (e) { LL.toast(LL.errText(e), 'error'); }
          } }, t('notice.ok'))));
    }).catch(() => bar.remove());
    return bar;
  };

  // ------------------------------------------------------------------ one-time codes from an authenticator app
  function totpSetup(onDone) {
    const box = h('div', { class: 'totp-setup stack' });
    const msg = h('div', { class: 'msg' });
    (async () => {
      let s;
      try { s = await api('/api/account/totp/setup', { method: 'POST', body: {} }); } catch (e) { fill(box, h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
      const qrEl = h('div', { class: 'totp-qr', role: 'img', 'aria-label': t('totp.qr_label') });
      qrEl.innerHTML = s.qr;                                    // drawn by the server (lanloom/qr.py), path data only
      const grouped = s.secret.replace(/(.{4})/g, '$1 ').trim();
      const copyBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => copyText(s.secret, copyBtn) }, t('access.copy'));
      const code = h('input', { class: 'input code-input', type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '8', id: 'totp-code', placeholder: '123456' });
      const go = h('button', { class: 'btn btn--primary', type: 'submit' }, t('totp.turn_on'));
      fill(box, 
        h('ol', { class: 'totp-steps' },
          h('li', {}, t('totp.step1')),
          h('li', {}, t('totp.step2'), h('div', { class: 'totp-pair' }, qrEl, h('div', { class: 'totp-key' }, h('span', { class: 'field__label' }, t('totp.key')),
            h('code', { class: 'mono totp-key__text' }, grouped), copyBtn, h('a', { class: 'totp-key__link', href: s.uri }, t('totp.open_app'))))),
          h('li', {}, t('totp.step3'))),
        h('form', { class: 'stack', onsubmit: async ev => {
          ev.preventDefault(); go.disabled = true; LL.msg(msg, '');
          try { const r = await api('/api/account/totp/enable', { method: 'POST', body: { code: code.value } }); fill(box, recoveryCodes(r.recovery_codes, onDone)); }
          catch (e) { LL.msg(msg, LL.errText(e), 'error'); go.disabled = false; code.select(); }
        } }, LL.field(t('totp.code'), code), msg, h('div', { class: 'btn-row' }, go)));
    })();
    return box;
  }

  function recoveryCodes(codes, onDone) {
    const text = codes.join('\n');
    const copyBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => copyText(text, copyBtn) }, t('access.copy'));
    const dl = h('a', { class: 'btn btn--ghost btn--sm', download: 'lanloom-recovery-codes.txt', href: 'data:text/plain;charset=utf-8,' + encodeURIComponent(t('recovery.file_head') + '\n\n' + text + '\n') }, t('recovery.download'));
    return h('div', { class: 'stack recovery' },
      h('div', { class: 'callout callout--warn' }, h('strong', {}, t('recovery.title')), h('p', {}, t('recovery.lead'))),
      h('ol', { class: 'recovery__grid mono' }, codes.map(c => h('li', {}, c))),
      h('div', { class: 'btn-row btn-row--wrap' }, copyBtn, dl, h('button', { class: 'btn btn--primary', type: 'button', onclick: () => onDone && onDone() }, t('recovery.saved'))));
  }

  // ------------------------------------------------------------------ invitation and reset link (signed out)
  LL.view('invite', async function (app, rest) {
    const token = (rest || []).join('/');
    let info;
    try { info = await api('/api/invite/check', { method: 'POST', body: { token }, allow401: true }); }
    catch (e) {
      LL.authPage(app, h('h1', { class: 'auth-card__title' }, t('invite.invalid.title')), h('p', { class: 'muted' }, t(e.status === 429 ? 'auth.err.rate' : 'invite.invalid.text', { s: e.wait || 60 })),
        h('a', { class: 'btn btn--primary btn--block', href: '#/login' }, t('invite.to_login')));
      return;
    }
    const reset = info.kind === 'reset';
    const user = h('input', { type: 'text', autocomplete: 'username', autocapitalize: 'none', spellcheck: 'false', required: true, maxlength: '64' });
    const pass = h('input', { type: 'password', autocomplete: 'new-password', required: true });
    const pass2 = h('input', { type: 'password', autocomplete: 'new-password', required: true });
    const msg = h('div', { class: 'msg' });
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, t(reset ? 'invite.reset.submit' : 'invite.submit'));
    const form = h('form', { class: 'stack', onsubmit: async ev => {
      ev.preventDefault(); LL.msg(msg, '');
      if (pass.value !== pass2.value) { LL.msg(msg, t('wizard.account.mismatch'), 'error'); return; }
      submit.disabled = true;
      try {
        const r = await api('/api/invite/accept', { method: 'POST', body: { token, username: user.value, password: pass.value }, allow401: true });
        if (r.login) {
          LL.authPage(app, h('h1', { class: 'auth-card__title' }, t('invite.reset.done.title')), h('p', { class: 'muted' }, t('invite.reset.done.text', { user: r.username })),
            h('a', { class: 'btn btn--primary btn--block', href: '#/login' }, t('invite.to_login')));
          return;
        }
        LL.afterSignIn(r);
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); submit.disabled = false; }
    } },
    reset ? h('p', {}, t('invite.reset.lead', { user: info.username })) : h('p', {}, t('invite.lead'), ' ', roleChip(info.role)),
    reset ? null : LL.field(t('login.username'), user),
    LL.field(t('invite.password'), pass, t('wizard.account.password_hint')), LL.field(t('invite.password2'), pass2),
    reset && info.clear_2fa ? h('p', { class: 'muted' }, t('invite.reset.cleared')) : null,
    h('p', { class: 'muted field__hint' }, t('invite.expires', { when: when(info.expires) })), msg, submit);
    LL.authPage(app, h('h1', { class: 'auth-card__title' }, t(reset ? 'invite.reset.title' : 'invite.title')), form);
  });

  // ------------------------------------------------------------------ set-up page when the second factor is required
  LL.view('enroll', async function (app) {
    let me;
    try { me = await api('/api/account'); } catch (e) { return; }
    const body = h('div', { class: 'stack enroll' });
    const done = () => LL.go('#/overview');
    const choose = () => fill(body, 
      h('p', {}, t('enroll.lead', { role: t('role.name.' + me.role) })),
      h('div', { class: 'enroll__choices' },
        h('button', { class: 'choice-card', type: 'button', onclick: () => fill(body, totpSetup(done), back()) },
          h('strong', {}, t('enroll.app.title')), h('span', { class: 'muted' }, t('enroll.app.text'))),
        LL.passkeySupported() ? h('button', { class: 'choice-card', type: 'button', onclick: async () => {
          try { await LL.passkeyRegister(LL.deviceName()); done(); } catch (e) { LL.toast(LL.errText(e), 'error'); }
        } }, h('strong', {}, t('enroll.passkey.title')), h('span', { class: 'muted' }, t('enroll.passkey.text'))) :
          h('div', { class: 'choice-card is-disabled' }, h('strong', {}, t('enroll.passkey.title')), h('span', { class: 'muted' }, t('passkey.needs_secure')))));
    const back = () => h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: choose }, t('enroll.back')));
    choose();
    LL.authPage(app, h('h1', { class: 'auth-card__title' }, t('enroll.title')), body,
      h('p', { class: 'auth-card__foot muted' }, h('a', { href: '#/login', onclick: async ev => { ev.preventDefault(); await api('/api/logout', { method: 'POST', body: {} }).catch(() => {}); LL.go('#/login'); } }, t('settings.logout'))));
    app.querySelector('.auth-card').classList.add('auth-card--wide');
  });

  // ------------------------------------------------------------------ Settings > Account
  LL.accountPage = async function (pg) {
    const [me, pk, ses] = await Promise.all([api('/api/account'), api('/api/passkeys'), api('/api/account/sessions')]);
    const local = me.source !== 'proxy' && me.source !== 'sample';
    // profile + password
    const cur = h('input', { type: 'password', autocomplete: 'current-password' });
    const nw = h('input', { type: 'password', autocomplete: 'new-password' });
    const pwMsg = h('div', { class: 'msg' });
    pg.append(card(t('account.title', { user: me.username }), [
      LL.facts([[t('account.role'), roleChip(me.role)], [t('account.method'), t('account.method.' + (me.method || 'password'))],
        me.public_computer ? [t('account.public'), t('account.public.yes')] : null].filter(Boolean)),
      h('p', { class: 'muted' }, t('settings.role.' + me.role)),
      me.source === 'proxy' ? h('p', { class: 'callout' }, t('account.proxy')) : null,
      local ? [h('hr'), h('h3', {}, t('settings.pw.change')),
        h('div', { class: 'grid-2' }, LL.field(t('settings.pw.current'), cur), LL.field(t('settings.pw.new'), nw, t('wizard.account.password_hint'))), pwMsg,
        h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
          try { await api('/api/account/password', { method: 'POST', body: { current: cur.value, password: nw.value } }); LL.msg(pwMsg, t('settings.pw.changed'), 'ok'); cur.value = nw.value = ''; }
          catch (e) { LL.msg(pwMsg, LL.errText(e), 'error'); }
        } }, t('settings.pw.change')))] : null]));

    // second factor
    const tfa = h('div', { class: 'stack' });
    const reload = () => LL.route();
    function renderTfa() {
      const f = me.factors;
      const status = f.any ? h('span', { class: 'chip chip--ok' }, t('tfa.on')) : h('span', { class: 'chip ' + (me.mfa.required ? 'chip--warn' : 'chip--muted') }, t('tfa.off'));
      const need = me.mfa.required && !f.any ? h('div', { class: 'callout callout--warn' }, t(me.mfa.due ? 'tfa.required_by' : 'tfa.required', { when: when(me.mfa.due) })) : null;
      const pkList = h('ul', { class: 'plain-list' }, pk.passkeys.length ? pk.passkeys.map(p => h('li', { class: 'row-between' },
        h('span', {}, p.name, h('span', { class: 'muted' }, ' · ' + LL.fmtDate(p.created))),
        h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', onclick: async () => {
          try { await api('/api/passkeys/' + p.id, { method: 'DELETE' }); reload(); } catch (e) { LL.toast(LL.errText(e), 'error'); }
        } }, t('passkey.remove')))) : [h('li', { class: 'muted' }, t('passkey.none'))]);
      const totpRow = h('div', { class: 'row-between tfa-row' },
        h('div', {}, h('strong', {}, t('tfa.app')), h('div', { class: 'muted' }, t(f.totp ? 'tfa.app.on' : 'tfa.app.off'))),
        f.totp ? h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', onclick: async () => {
          if (!await ask(t('tfa.app.off_confirm'), t('tfa.app.turn_off'))) return;
          try { await api('/api/account/totp', { method: 'DELETE' }); reload(); } catch (e) { LL.toast(LL.errText(e), 'error'); }
        } }, t('tfa.app.turn_off')) : h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => {
          fill(tfa, totpSetup(reload), h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: renderTfa }, t('enroll.back'))));
        } }, t('tfa.app.set_up')));
      const recRow = f.any ? h('div', { class: 'row-between tfa-row' },
        h('div', {}, h('strong', {}, t('recovery.title')), h('div', { class: 'muted' }, t('recovery.left', { n: f.recovery_left }))),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
          try { const r = await api('/api/account/recovery', { method: 'POST', body: {} }); fill(tfa, recoveryCodes(r.recovery_codes, reload)); }
          catch (e) { LL.toast(LL.errText(e), 'error'); }
        } }, t(f.recovery_left ? 'recovery.renew' : 'recovery.create'))) : null;
      const pkMsg = h('div', { class: 'msg' });
      fill(tfa, h('div', { class: 'row-between' }, h('p', { class: 'muted' }, t('tfa.lead')), status), need, totpRow, recRow, h('hr'),
        h('h3', {}, t('passkey.title')), h('p', { class: 'muted' }, t('tfa.passkey_counts')), LL.passkeySupported() ? null : h('p', { class: 'muted' }, t('passkey.needs_secure')), pkList, pkMsg,
        LL.passkeySupported() ? h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
          try { await LL.passkeyRegister(LL.deviceName()); LL.msg(pkMsg, t('passkey.added'), 'ok'); setTimeout(reload, 600); } catch (e) { LL.msg(pkMsg, LL.errText(e), 'error'); }
        } }, t('passkey.add'))) : null);
    }
    if (local) { renderTfa(); pg.append(card(t('tfa.title'), tfa)); }

    // sessions
    pg.append(card(t('sessions.title'), [h('p', { class: 'muted' }, t('sessions.lead')), sessionsTable(ses.sessions, async sid => {
      await api('/api/account/sessions/' + encodeURIComponent(sid), { method: 'DELETE' }); reload();
    }), h('div', { class: 'btn-row btn-row--wrap' },
      h('button', { class: 'btn btn--danger-ghost', type: 'button', onclick: async () => {
        if (!await ask(t('sessions.all_confirm'), t('sessions.end_all'))) return;
        await api('/api/account/sessions', { method: 'DELETE' }); LL.go('#/login');
      } }, t('sessions.end_all')),
      h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => { await api('/api/logout', { method: 'POST', body: {} }); LL.go('#/login'); } }, t('settings.logout')))]));
  };

  function sessionsTable(rows, onEnd) {
    if (!rows.length) return h('p', { class: 'muted' }, t('sessions.none'));
    return h('div', { class: 'acc-table acc-table--sessions', role: 'table', 'aria-label': t('sessions.title') },
      h('div', { class: 'acc-row acc-row--head', role: 'row' }, ['sessions.device', 'sessions.address', 'sessions.seen', ''].map(k => h('span', { role: 'columnheader' }, k ? t(k) : ''))),
      rows.map(s => h('div', { class: 'acc-row' + (s.current ? ' is-current' : ''), role: 'row' },
        h('span', { role: 'cell', class: 'acc-cell--main' }, agentText(s.agent), s.current ? h('span', { class: 'chip chip--accent' }, t('sessions.this')) : null,
          s.public_computer ? h('span', { class: 'chip chip--muted' }, t('sessions.public')) : null),
        h('span', { role: 'cell', class: 'mono', 'data-label': t('sessions.address') }, s.ip || '—'),
        h('span', { role: 'cell', 'data-label': t('sessions.seen') }, LL.fmtDate(s.last_seen)),
        h('span', { role: 'cell', class: 'acc-cell--act' }, !s.current && onEnd ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => onEnd(s.id) }, t('sessions.end')) : null))));
  }

  // ------------------------------------------------------------------ Settings > Users (admin)
  LL.usersPage = async function (pg) {
    const data = await api('/api/users');
    const reload = () => LL.route();
    // invite
    const role = h('select', { class: 'select' }, data.roles.map(r => h('option', { value: r }, t('role.name.' + r))));
    role.value = 'viewer';
    const note = h('input', { class: 'input', maxlength: '80', placeholder: t('users.invite.note_ph') });
    const out = h('div', {});
    const inviteBtn = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      try {
        const r = await api('/api/users/invite', { method: 'POST', body: { role: role.value, note: note.value } });
        fill(out, linkBox(t('users.invite.ready', { role: t('role.name.' + r.role), when: when(r.expires) }), linkFor(r.token)));
        note.value = '';
        renderInvites((await api('/api/users')).invites);
      } catch (e) { LL.toast(LL.errText(e), 'error'); }
    } }, t('users.invite.create'));
    const invites = h('div', {});
    function renderInvites(list) {
      fill(invites, list.length ? h('ul', { class: 'plain-list invite-list' }, list.map(i => h('li', { class: 'row-between' },
        h('span', {}, i.kind === 'reset' ? t('users.invite.reset_for', { user: i.username || '?' }) : roleChip(i.role), i.note ? h('span', {}, ' ' + i.note) : null,
          h('span', { class: 'muted' }, ' · ' + t('users.invite.until', { when: when(i.expires) }) + ' · ' + i.created_by)),
        h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', onclick: async () => {
          await api('/api/users/invites/' + i.id, { method: 'DELETE' }); renderInvites((await api('/api/users')).invites);
        } }, t('users.invite.revoke'))))) : h('p', { class: 'muted' }, t('users.invite.none')));
    }
    renderInvites(data.invites);
    pg.append(card(t('users.invite.title'), [h('p', { class: 'muted' }, t('users.invite.lead')),
      h('div', { class: 'grid-2' }, LL.field(t('users.role'), role), LL.field(t('users.invite.note'), note)),
      h('div', { class: 'btn-row' }, inviteBtn), out, h('h3', {}, t('users.invite.open')), invites]));

    // users
    const list = h('div', { class: 'acc-table acc-table--users', role: 'table', 'aria-label': t('users.title') },
      h('div', { class: 'acc-row acc-row--head', role: 'row' }, ['users.name', 'users.role', 'users.tfa', 'users.last', ''].map(k => h('span', { role: 'columnheader' }, k ? t(k) : ''))),
      data.users.map(u => userRow(u)));
    function userRow(u) {
      const me = u.id === data.me;
      const sel = h('select', { class: 'select select--sm', 'aria-label': t('users.role_of', { user: u.username }), disabled: u.source === 'sample', onchange: async () => {
        try { await api('/api/users/' + u.id, { method: 'PATCH', body: { role: sel.value } }); LL.toast(t('settings.saved'), 'ok'); if (me) LL.route(); }
        catch (e) { sel.value = u.role; LL.toast(LL.errText(e), 'error'); }
      } }, data.roles.map(r => h('option', { value: r }, t('role.name.' + r))));
      sel.value = u.role;
      const tfa = u.totp || u.passkeys ? h('span', { class: 'chip chip--ok' }, [u.totp ? t('users.tfa.app') : null, u.passkeys ? t('users.tfa.passkeys', { n: u.passkeys }) : null].filter(Boolean).join(' + '))
        : u.source === 'proxy' ? h('span', { class: 'chip chip--muted' }, t('users.tfa.proxy'))
        : h('span', { class: 'chip ' + (u.mfa.overdue ? 'chip--danger' : u.mfa.required ? 'chip--warn' : 'chip--muted') }, t(u.mfa.overdue ? 'users.tfa.overdue' : u.mfa.required ? 'users.tfa.due' : 'users.tfa.none', { when: day(u.mfa.due) }));
      const menu = h('details', { class: 'acc-menu' }, h('summary', { class: 'btn btn--ghost btn--sm' }, t('users.actions')),
        h('div', { class: 'acc-menu__list' },
          u.source === 'local' ? h('button', { type: 'button', onclick: () => resetDialog(u) }, t('users.reset')) : null,
          h('button', { type: 'button', onclick: () => sessionsDialog(u) }, t('users.sessions', { n: u.sessions })),
          me ? null : h('button', { type: 'button', onclick: async () => {
            try { await api('/api/users/' + u.id, { method: 'PATCH', body: { disabled: !u.disabled } }); reload(); } catch (e) { LL.toast(LL.errText(e), 'error'); }
          } }, t(u.disabled ? 'users.enable' : 'users.disable')),
          me ? null : h('button', { type: 'button', class: 'is-danger', onclick: async () => {
            if (!await ask(t('users.delete_confirm', { user: u.username }), t('users.delete'))) return;
            try { await api('/api/users/' + u.id, { method: 'DELETE' }); reload(); } catch (e) { LL.toast(LL.errText(e), 'error'); }
          } }, t('users.delete'))));
      return h('div', { class: 'acc-row' + (u.disabled ? ' is-disabled' : ''), role: 'row' },
        h('span', { role: 'cell', class: 'acc-cell--main' }, h('strong', {}, u.username), me ? h('span', { class: 'chip chip--accent' }, t('users.you')) : null,
          u.disabled ? h('span', { class: 'chip chip--muted' }, t('users.disabled')) : null, u.source === 'proxy' ? h('span', { class: 'chip chip--muted' }, t('users.via_proxy')) : null,
          u.source === 'sample' ? h('span', { class: 'chip chip--muted' }, t('users.sample')) : null),
        h('span', { role: 'cell', 'data-label': t('users.role') }, sel),
        h('span', { role: 'cell', 'data-label': t('users.tfa') }, tfa),
        h('span', { role: 'cell', 'data-label': t('users.last') }, when(u.last_login)),
        h('span', { role: 'cell', class: 'acc-cell--act' }, u.source === 'sample' ? null : menu));
    }
    pg.append(card(t('users.title'), [h('p', { class: 'muted' }, t('users.lead')), list, h('p', { class: 'field__hint' }, t('users.roles_hint'))]));
  };

  function linkBox(title, link) {
    const input = h('input', { class: 'input mono', readonly: true, value: link, 'aria-label': title, onfocus: ev => ev.target.select() });
    const btn = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => copyText(link, btn) }, t('access.copy'));
    return h('div', { class: 'link-box callout' }, h('strong', {}, title), h('div', { class: 'link-box__row' }, input, btn), h('p', { class: 'muted' }, t('users.link_once')));
  }

  function resetDialog(u) {
    const clear = h('input', { type: 'checkbox' });
    const out = h('div', {});
    const go = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      try { const r = await api('/api/users/' + u.id + '/reset', { method: 'POST', body: { clear_2fa: clear.checked } }); fill(out, linkBox(t('users.reset.ready', { when: when(r.expires) }), linkFor(r.token))); go.disabled = true; }
      catch (e) { LL.toast(LL.errText(e), 'error'); }
    } }, t('users.reset.create'));
    LL.dialog(t('users.reset.title', { user: u.username }), h('div', { class: 'stack' }, h('p', { class: 'muted' }, t('users.reset.lead')),
      h('label', { class: 'check' }, clear, h('span', {}, t('users.reset.clear_2fa'))), h('div', { class: 'btn-row' }, go), out));
  }

  async function sessionsDialog(u) {
    const body = h('div', { class: 'stack' });
    const d = LL.dialog(t('users.sessions_of', { user: u.username }), body);
    async function render() {
      const r = await api('/api/users/' + u.id + '/sessions');
      fill(body, sessionsTable(r.sessions, null), r.sessions.length ? h('div', { class: 'btn-row' }, h('button', { class: 'btn btn--danger-ghost', type: 'button', onclick: async () => {
        try { await api('/api/users/' + u.id + '/sessions', { method: 'DELETE' }); render(); } catch (e) { LL.toast(LL.errText(e), 'error'); }
      } }, t('users.end_all'))) : null);
    }
    try { await render(); } catch (e) { d.close(); LL.toast(LL.errText(e), 'error'); }
  }

  // ------------------------------------------------------------------ Settings > Security (admin): policy + audit log
  LL.securityPage = async function (pg) {
    const s = await api('/api/security');
    const req = {};
    const roleChecks = h('div', { class: 'stack-sm' }, ['viewer', 'operator', 'admin'].map(r => {
      req[r] = h('input', { type: 'checkbox', checked: !!s.policy.mfa_required[r] });
      return h('label', { class: 'check' }, req[r], h('span', {}, t('security.require', { role: t('role.name.' + r) })));
    }));
    const grace = h('select', { class: 'select' }, s.grace_choices.map(n => h('option', { value: String(n) }, n ? t('security.grace.h', { n: n >= 48 && n % 24 === 0 ? n / 24 : n, unit: t(n >= 48 && n % 24 === 0 ? 'security.unit.days' : 'security.unit.hours') }) : t('security.grace.none'))));
    grace.value = String(s.policy.mfa_grace_hours);
    const keep = h('select', { class: 'select' }, s.retention_choices.map(n => h('option', { value: String(n) }, t('security.keep.days', { n }))));
    keep.value = String(s.retention);
    const msg = h('div', { class: 'msg' });
    const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      try {
        await api('/api/security', { method: 'PUT', body: { mfa_required: { viewer: req.viewer.checked, operator: req.operator.checked, admin: req.admin.checked },
          mfa_grace_hours: Number(grace.value), audit_retention_days: Number(keep.value) } });
        LL.msg(msg, t('settings.saved'), 'ok');
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('security.save'));
    const p = s.proxy;
    pg.append(card(t('security.title'), [h('p', { class: 'muted' }, t('security.lead', { min: s.stepup_minutes })),
      h('h3', {}, t('security.tfa')), roleChecks,
      h('div', { class: 'grid-2' }, LL.field(t('security.grace'), grace, t('security.grace.hint')), LL.field(t('security.keep'), keep, t('security.keep.hint'))),
      msg, h('div', { class: 'btn-row' }, save), h('hr'),
      h('h3', {}, t('security.signin')),
      LL.facts([[t('security.proxy'), p.enabled ? t('security.proxy.on', { header: p.header }) : t('security.proxy.off')],
        p.enabled ? [t('security.proxy.roles'), p.group_roles.length ? p.group_roles.map(g => g[0] + ' → ' + t('role.name.' + g[1])).join(', ') : '—'] : null,
        p.enabled ? [t('security.proxy.default'), p.default_role ? t('role.name.' + p.default_role) : t('security.proxy.refuse')] : null,
        [t('security.base'), s.base_path || t('security.base.none')],
        [t('security.invite'), t('security.invite.hours', { n: s.invite_hours })]].filter(Boolean)),
      h('p', { class: 'field__hint' }, t('security.env_hint'))]));
    pg.append(auditCard());
  };

  function auditCard() {
    const actor = h('select', { class: 'select select--sm', 'aria-label': t('audit.f.user') });
    const action = h('select', { class: 'select select--sm', 'aria-label': t('audit.f.action') });
    const outcome = h('select', { class: 'select select--sm', 'aria-label': t('audit.f.outcome') }, ['', 'ok', 'denied', 'failed'].map(o => h('option', { value: o }, o ? t('audit.outcome.' + o) : t('audit.f.any_outcome'))));
    const period = h('select', { class: 'select select--sm', 'aria-label': t('audit.f.period') }, [['', 'audit.f.all'], ['86400', 'audit.f.day'], ['604800', 'audit.f.week'], ['2592000', 'audit.f.month']].map(([v, k]) => h('option', { value: v }, t(k))));
    const q = h('input', { type: 'search', class: 'input input-sm', placeholder: t('audit.f.search'), 'aria-label': t('audit.f.search') });
    const chain = h('span', { class: 'chip chip--muted' }, '…');
    const rows = h('div', { class: 'acc-table acc-table--audit', role: 'table', 'aria-label': t('audit.title') });
    const more = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', hidden: true }, t('audit.more'));
    const exportA = h('a', { class: 'btn btn--ghost btn--sm', href: '#', download: '' }, t('audit.export'));
    let last = null, filled = false;
    const params = () => {
      const o = new URLSearchParams();
      if (actor.value) o.set('actor', actor.value);
      if (action.value) o.set('action', action.value);
      if (outcome.value) o.set('outcome', outcome.value);
      if (period.value) o.set('since', String(Math.floor(Date.now() / 1000) - Number(period.value)));
      if (q.value.trim()) o.set('q', q.value.trim());
      return o;
    };
    const head = () => h('div', { class: 'acc-row acc-row--head', role: 'row' }, ['audit.when', 'audit.who', 'audit.what', 'audit.target', 'audit.from'].map(k => h('span', { role: 'columnheader' }, t(k))));
    async function load(append) {
      const o = params(); o.set('limit', '50'); if (append && last) o.set('before', String(last));
      exportA.href = LL.url('/api/audit/export?' + params().toString());
      let r;
      try { r = await api('/api/audit?' + o.toString()); } catch (e) { fill(rows, h('p', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!filled) {
        fill(actor, h('option', { value: '' }, t('audit.f.any_user')), ...r.users.map(u => h('option', { value: u }, u)));
        const groups = [...new Set(r.actions.map(a => a.split('.')[0] + '.'))];
        fill(action, h('option', { value: '' }, t('audit.f.any_action')), ...groups.map(g => h('option', { value: g }, t('audit.group.' + g.slice(0, -1)))),
          ...r.actions.map(a => h('option', { value: a }, '· ' + actionText(a))));
        filled = true;
      }
      chain.className = 'chip ' + (r.chain.ok ? 'chip--ok' : 'chip--danger');
      chain.textContent = r.chain.ok ? t('audit.chain.ok', { n: r.chain.count }) : t('audit.chain.bad', { id: r.chain.broken_at, why: t(r.chain.reason) });
      if (!append) fill(rows, head());
      r.entries.forEach(e => rows.append(h('div', { class: 'acc-row audit-row audit-row--' + e.outcome, role: 'row' },
        h('span', { role: 'cell', class: 'audit-when', 'data-label': t('audit.when') }, LL.fmtDate(e.t)),
        h('span', { role: 'cell', class: 'acc-cell--main', 'data-label': t('audit.who') }, e.actor || t('audit.nobody'), e.role ? h('span', { class: 'muted' }, ' · ' + t('role.name.' + e.role)) : null),
        h('span', { role: 'cell', 'data-label': t('audit.what') }, actionText(e.action), e.outcome !== 'ok' ? h('span', { class: 'chip ' + (e.outcome === 'denied' ? 'chip--danger' : 'chip--warn') }, t('audit.outcome.' + e.outcome)) : null,
          detailText(e.detail) ? h('span', { class: 'muted audit-detail' }, detailText(e.detail)) : null),
        h('span', { role: 'cell', class: 'truncate', 'data-label': t('audit.target') }, e.target || '—'),
        h('span', { role: 'cell', class: 'mono', 'data-label': t('audit.from') }, e.ip || '—', e.agent ? h('span', { class: 'muted' }, ' · ' + agentText(e.agent)) : null))));
      if (!r.entries.length && !append) rows.append(h('p', { class: 'muted' }, t('audit.none')));
      last = r.entries.length ? r.entries[r.entries.length - 1].id : last;
      more.hidden = r.entries.length < 50;
    }
    let timer = null;
    [actor, action, outcome, period].forEach(el => el.addEventListener('change', () => { last = null; load(false); }));
    q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { last = null; load(false); }, 300); });
    more.addEventListener('click', () => load(true));
    load(false);
    return card(t('audit.title'), [h('div', { class: 'row-between audit-head' }, h('p', { class: 'muted' }, t('audit.lead')), chain),
      h('div', { class: 'audit-filters' }, actor, action, outcome, period, q, exportA), rows, h('div', { class: 'btn-row' }, more)], 'settings-card--wide');
  }
  function actionText(a) { const k = 'audit.action.' + a; return LL.strings[k] ? t(k) : a; }
  function detailText(d) {
    if (!d || typeof d !== 'object') return '';
    const parts = [];
    for (const [k, v] of Object.entries(d)) {
      if (v == null || v === '' || v === false || (Array.isArray(v) && !v.length)) continue;
      if (v === true) { parts.push(LL.strings['audit.d.' + k] ? t('audit.d.' + k) : k); continue; }
      const val = Array.isArray(v) ? v.join(', ') : (typeof v === 'object' ? Object.entries(v).map(([a, b]) => a + ' ' + (Array.isArray(b) ? b.join('→') : b)).join(', ') : String(v));
      parts.push((LL.strings['audit.d.' + k] ? t('audit.d.' + k) : k) + ': ' + val);
    }
    return parts.join(' · ');
  }
})();
