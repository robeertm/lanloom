/* Lanloom — Settings > Notifications: own rules per event (mode, minimum severity, channels), quiet hours, web push on
   this device, planned maintenance, shared limits and the heartbeat (administrators), and the history.
   Calm by default: every event goes into the nightly report until somebody asks for more. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const card = (title, kids, cls) => h('section', { class: 'card settings-card' + (cls ? ' ' + cls : '') }, h('h2', {}, title), kids);
  const DAYS = [0, 1, 2, 3, 4, 5, 6];
  const SEV_CHIP = { info: 'chip--muted', warning: 'chip--warn', critical: 'chip--danger' };
  const STATUS_CHIP = { sent: 'chip--ok', bundled: 'chip--ok', failed: 'chip--danger', quiet: 'chip--accent', maintenance: 'chip--accent',
    flap: 'chip--muted', cooldown: 'chip--muted', capped: 'chip--warn', nightly: 'chip--muted', nochannel: 'chip--warn', pending: 'chip--muted' };

  // ------------------------------------------------------------------ service worker + push on this device
  LL.pushSupported = () => !!(window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && window.Notification);
  async function swReg() {
    if (!('serviceWorker' in navigator)) return null;
    try {
      const reg = await navigator.serviceWorker.getRegistration(LL.base);
      if (reg) return reg;
      return await Promise.race([navigator.serviceWorker.ready, new Promise(r => setTimeout(() => r(null), 5000))]);
    } catch (e) { return null; }
  }
  async function currentSub() {
    const reg = await swReg();
    if (!reg || !reg.pushManager) return null;
    try { return await reg.pushManager.getSubscription(); } catch (e) { return null; }
  }
  LL.pushEnable = async function (vapid) {
    if (!LL.pushSupported()) throw Object.assign(new Error('notify.push.unsupported'), { key: 'notify.push.unsupported' });
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') throw Object.assign(new Error('notify.push.denied'), { key: 'notify.push.denied' });
    await LL.swRegister();
    const reg = await swReg();
    if (!reg) throw Object.assign(new Error('notify.push.unsupported'), { key: 'notify.push.unsupported' });
    let sub = await reg.pushManager.getSubscription();
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: new Uint8Array(LL.unb64(vapid)) });
    const json = sub.toJSON();
    return api('/api/notify/push', { method: 'POST', body: { subscription: { endpoint: json.endpoint, keys: json.keys }, label: LL.deviceName() } });
  };
  LL.pushDisable = async function () {
    const sub = await currentSub();
    if (!sub) return;
    try { await api('/api/notify/push/forget', { method: 'POST', body: { endpoint: sub.endpoint } }); } catch (e) { /* the server forgets it on the next failed send */ }
    try { await sub.unsubscribe(); } catch (e) { /* already gone */ }
  };

  // ------------------------------------------------------------------ helpers
  function sel(value, options, onchange, attrs, big) {
    const s = h('select', Object.assign({ class: big ? 'select' : 'select select--sm' }, attrs || {}), options.map(([v, label]) => h('option', { value: v }, label)));
    s.value = value;
    s.addEventListener('change', () => onchange(s.value, s));
    return s;
  }
  const dayName = d => new Date(Date.UTC(2024, 0, 1 + d)).toLocaleDateString(LL.locale(), { weekday: 'short', timeZone: 'UTC' });   // 1 Jan 2024 was a Monday
  const toLocalInput = ts => { const d = new Date(ts * 1000); const p = n => String(n).padStart(2, '0'); return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()); };
  const fromLocalInput = v => (v ? new Date(v).getTime() / 1000 : NaN);
  function windowWhen(w) {
    if (w.kind === 'once') return t('notify.maint.once_when', { start: LL.fmtDate(w.start), end: LL.fmtDate(w.end) });
    return t('notify.maint.weekly_when', { days: w.days.map(dayName).join(', '), from: w.from, to: w.to });
  }
  function eventText(e) {
    const v = Object.assign({}, e.vars || {});
    if (e.event === 'security_notice') return t('notify.ev.security_notice') + (v.kind ? ' · ' + t('notice.title.' + v.kind).replace(/:$/, '') : '');
    const subject = v.title || v.name || v.version || '';
    return t('notify.ev.' + e.event) + (subject ? ' · ' + subject : '');
  }

  // ------------------------------------------------------------------ page
  LL.notifyPage = async function (pg) {
    if (LL.pushSupported()) LL.swRegister();
    const host = h('div', { class: 'nt-page' });
    pg.append(host);
    let data = await api('/api/notify');
    let devices = null;
    const operator = LL.can('operate'), admin = LL.can('admin');

    async function savePrefs(patch, okText) {
      const res = await api('/api/notify/prefs', { method: 'PUT', body: patch });
      data.prefs = res.prefs; data.channel_state = res.channel_state;
      LL.toast(okText || t('settings.saved'), 'ok');
    }

    // ---- intro + state
    function intro() {
      const active = (data.maintenance || []).find(w => w.id === data.active_window);
      const rules = data.prefs.rules;
      const loud = Object.keys(rules).filter(k => rules[k].mode === 'immediate' || rules[k].mode === 'hourly').length;
      return card(t('notify.title'), [h('p', { class: 'muted' }, t('notify.lead')),
        h('div', { class: 'nt-state' }, h('span', { class: 'chip ' + (loud ? 'chip--accent' : 'chip--muted') }, loud ? t('notify.state.rules', { n: loud }) : t('notify.state.calm')),
          data.prefs.quiet.enabled ? h('span', { class: 'chip chip--muted' }, t('notify.state.quiet', { from: data.prefs.quiet.from, to: data.prefs.quiet.to })) : null,
          active ? h('span', { class: 'chip chip--warn' }, t('notify.state.maintenance', { title: active.title || t('notify.maint.untitled') })) : null)]);
    }

    // ---- this device (web push)
    function pushCard() {
      const box = h('div', { class: 'stack-sm' });
      const msg = h('div', { class: 'msg', role: 'status' });
      async function render() {
        const sub = LL.pushSupported() ? await currentSub() : null;
        const mine = sub ? true : false;
        const rows = data.push.devices.map(d => h('li', { class: 'nt-dev' },
          h('span', { class: 'nt-dev__name' }, d.label || t('notify.push.device')),
          h('span', { class: 'muted nt-dev__meta' }, d.last_err ? t('notify.push.last_err', { why: t(d.last_err) }) : (d.last_ok ? t('notify.push.last_ok', { when: LL.fmtDate(d.last_ok) }) : t('notify.push.since', { when: LL.fmtDate(d.created) }))),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
            try { await api('/api/notify/push/' + d.id, { method: 'DELETE' }); data.push.devices = data.push.devices.filter(x => x.id !== d.id); render(); LL.toast(t('notify.push.removed'), 'ok'); }
            catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
          } }, t('notify.push.remove'))));
        let action;
        if (!LL.pushSupported()) action = h('p', { class: 'callout callout--info nt-callout' }, t(window.isSecureContext ? 'notify.push.unsupported' : 'notify.push.needs_https'));
        else if (!data.push.vapid) action = h('p', { class: 'muted' }, t('notify.err.push_unavailable'));
        else if (mine) action = h('div', { class: 'btn-row btn-row--wrap' },
          h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => { await LL.pushDisable(); data = await api('/api/notify'); render(); LL.toast(t('notify.push.off_done'), 'ok'); } }, t('notify.push.off')),
          h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => test('webpush', msg) }, t('notify.test.send')));
        else action = h('div', { class: 'btn-row btn-row--wrap' }, h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
          LL.msg(msg, '');
          try { await LL.pushEnable(data.push.vapid); data = await api('/api/notify'); render(); LL.toast(t('notify.push.on_done'), 'ok'); }
          catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        } }, t('notify.push.on')));
        box.replaceChildren(h('p', { class: 'muted' }, t('notify.push.lead')), action,
          rows.length ? h('ul', { class: 'nt-devs', 'aria-label': t('notify.push.devices') }, rows) : h('p', { class: 'muted fineprint' }, t('notify.push.none')), msg);
      }
      render();
      return card(t('notify.push.title'), [box]);
    }

    async function test(ch, msgEl) {
      LL.msg(msgEl, t('notify.test.working'), 'info');
      try {
        const res = await api('/api/notify/test', { method: 'POST', body: { channel: ch } });
        LL.msg(msgEl, res.ok ? t('notify.test.ok', { ch: t('notify.ch.' + ch) }) : t('notify.test.failed', { why: t(res.key) }), res.ok ? 'ok' : 'error');
      } catch (e) { LL.msg(msgEl, LL.errText(e), 'error'); }
    }

    // ---- rules
    function rulesCard() {
      const wrap = h('div', { class: 'nt-rules' });
      const msg = h('div', { class: 'msg', role: 'status' });
      const groups = [];
      data.events.forEach(e => { if (!groups.includes(e.group)) groups.push(e.group); });
      function row(e) {
        const r = data.prefs.rules[e.id];
        const off = r.mode === 'off' || r.mode === 'nightly';
        const update = async patch => {
          try { await savePrefs({ rules: { [e.id]: patch } }); render(); }
          catch (err) { LL.msg(msg, LL.errText(err), 'error'); render(); }
        };
        const mode = sel(r.mode, data.modes.map(m => [m, t('notify.mode.' + m)]), v => update({ mode: v }), { 'aria-label': t('notify.col.mode') + ' · ' + t('notify.ev.' + e.id) });
        const min = sel(r.min, data.severities.map(s => [s, t('notify.min.' + s)]), v => update({ min: v }), { 'aria-label': t('notify.col.min') + ' · ' + t('notify.ev.' + e.id), disabled: off });
        const chans = h('div', { class: 'nt-chans', role: 'group', 'aria-label': t('notify.col.channels') + ' · ' + t('notify.ev.' + e.id) }, data.channels.map(ch => {
          const usable = data.channel_state[ch] && data.channel_state[ch].usable;
          const on = r.channels.includes(ch);
          if (!usable && !on) return null;
          const cb = h('input', { type: 'checkbox', checked: on, disabled: off, onchange: () => update({ channels: cb.checked ? r.channels.concat([ch]) : r.channels.filter(c => c !== ch) }) });
          return h('label', { class: 'nt-chan' + (usable ? '' : ' is-unusable'), title: usable ? t('notify.ch.' + ch) : t('notify.ch.not_ready') }, cb, h('span', {}, t('notify.ch.' + ch)));
        }).filter(Boolean));
        if (!chans.children.length) chans.append(h('span', { class: 'muted fineprint' }, t('notify.ch.none')));
        const warn = !off && !r.channels.some(ch => data.channel_state[ch] && data.channel_state[ch].usable) ? h('div', { class: 'nt-rule__warn' }, t('notify.rule.no_channel')) : null;
        return h('div', { class: 'nt-rule' + (off ? ' is-calm' : '') },
          h('div', { class: 'nt-rule__name' }, h('span', {}, t('notify.ev.' + e.id)), h('span', { class: 'chip ' + SEV_CHIP[e.severity] }, t('notify.sev.' + e.severity)),
            h('div', { class: 'nt-rule__hint muted' }, t('notify.hint.' + e.id)), warn),
          h('div', { class: 'nt-rule__mode' }, mode), h('div', { class: 'nt-rule__min' }, min), h('div', { class: 'nt-rule__chans' }, chans));
      }
      function render() {
        wrap.replaceChildren(h('div', { class: 'nt-rule nt-rule--head', 'aria-hidden': 'true' }, h('div', {}, t('notify.col.event')), h('div', {}, t('notify.col.mode')),
          h('div', {}, t('notify.col.min')), h('div', {}, t('notify.col.channels'))),
          ...groups.map(g => h('div', { class: 'nt-group' }, h('h3', { class: 'nt-group__title' }, t('notify.group.' + g)), data.events.filter(e => e.group === g).map(row))));
      }
      async function preset(kind) {
        const rules = {};
        const push = data.channel_state.webpush && data.channel_state.webpush.usable ? ['webpush'] : data.channels.filter(c => data.channel_state[c] && data.channel_state[c].usable).slice(0, 1);
        data.events.forEach(e => {
          if (kind === 'calm') rules[e.id] = { mode: 'nightly' };
          else rules[e.id] = e.severity === 'critical' ? { mode: 'immediate', channels: push, min: 'info' } : (e.severity === 'warning' ? { mode: 'hourly', channels: push, min: 'warning' } : { mode: 'nightly' });
        });
        try { await savePrefs({ rules }, t('notify.preset.done')); render(); } catch (err) { LL.msg(msg, LL.errText(err), 'error'); }
      }
      render();
      return card(t('notify.rules.title'), [h('p', { class: 'muted' }, t('notify.rules.lead')),
        h('div', { class: 'btn-row btn-row--wrap' }, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => preset('calm') }, t('notify.preset.calm')),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => preset('important') }, t('notify.preset.important'))),
        wrap, msg]);
    }

    // ---- quiet hours + own address + channel tests
    function quietCard() {
      const q = data.prefs.quiet;
      const msg = h('div', { class: 'msg', role: 'status' });
      const on = h('input', { type: 'checkbox', checked: q.enabled });
      const from = h('input', { type: 'time', value: q.from, required: true });
      const to = h('input', { type: 'time', value: q.to, required: true });
      const save = async () => {
        LL.msg(msg, '');
        try { await savePrefs({ quiet: { enabled: on.checked, from: from.value, to: to.value } }); }
        catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      };
      [on, from, to].forEach(el => el.addEventListener('change', save));
      return card(t('notify.quiet.title'), [h('p', { class: 'muted' }, t('notify.quiet.lead', { tz: data.tz })),
        h('label', { class: 'check' }, on, h('span', {}, t('notify.quiet.on'))),
        h('div', { class: 'grid-2 nt-times' }, LL.field(t('notify.quiet.from'), from), LL.field(t('notify.quiet.to'), to)), msg]);
    }
    function channelsCard() {
      const msg = h('div', { class: 'msg', role: 'status' });
      const mail = h('input', { type: 'email', value: data.prefs.email || '', placeholder: t('notify.email.placeholder'), autocomplete: 'email', spellcheck: 'false' });
      mail.addEventListener('change', async () => { try { await savePrefs({ email: mail.value.trim() }); render(); } catch (e) { LL.msg(msg, LL.errText(e), 'error'); } });
      const list = h('ul', { class: 'nt-chlist' });
      function render() {
        list.replaceChildren(...data.channels.filter(c => c !== 'webpush').map(ch => {
          const ok = data.channel_state[ch] && data.channel_state[ch].usable;
          return h('li', {}, h('span', { class: 'nt-chlist__name' }, t('notify.ch.' + ch)),
            h('span', { class: 'chip ' + (ok ? 'chip--ok' : 'chip--muted') }, t(ok ? 'notify.ch.ready' : 'notify.ch.not_ready')),
            ok ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => test(ch, msg) }, t('notify.test.send')) : h('span'));
        }));
      }
      render();
      return card(t('notify.channels.title'), [h('p', { class: 'muted' }, t(admin ? 'notify.channels.lead_admin' : 'notify.channels.lead')),
        list, LL.field(t('notify.email.label'), mail, t('notify.email.hint')),
        admin ? h('div', { class: 'btn-row btn-row--wrap' }, h('a', { class: 'btn btn--ghost btn--sm', href: '#/settings' }, t('notify.channels.setup'))) : null, msg]);
    }

    // ---- planned maintenance
    function maintCard() {
      const listBox = h('div', { class: 'nt-maintlist' });
      const msg = h('div', { class: 'msg', role: 'status' });
      function render() {
        const ws = (data.maintenance || []).filter(w => !w.past).concat((data.maintenance || []).filter(w => w.past));
        listBox.replaceChildren(ws.length ? h('ul', { class: 'nt-wlist' }, ws.map(w => h('li', { class: 'nt-win' + (w.active ? ' is-active' : '') + (w.past ? ' is-past' : '') },
          h('div', { class: 'nt-win__main' }, h('span', { class: 'nt-win__title' }, w.title || t('notify.maint.untitled')),
            w.active ? h('span', { class: 'chip chip--warn' }, t('notify.maint.active')) : (w.past ? h('span', { class: 'chip chip--muted' }, t('notify.maint.past')) : null),
            h('div', { class: 'muted nt-win__when' }, windowWhen(w) + ' · ' + (w.devices.length ? t('notify.maint.devices_n', { n: w.devices.length }) : t('notify.maint.all')))),
          operator ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
            try { await api('/api/notify/maintenance/' + encodeURIComponent(w.id), { method: 'DELETE' }); data.maintenance = data.maintenance.filter(x => x.id !== w.id); render(); LL.toast(t('notify.maint.removed'), 'ok'); }
            catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
          } }, t(w.active ? 'notify.maint.end' : 'notify.maint.remove')) : null))) : h('p', { class: 'muted fineprint' }, t('notify.maint.none')));
      }
      render();
      const kids = [h('p', { class: 'muted' }, t('notify.maint.lead')), listBox];
      if (operator) {
        const kind = sel('once', [['once', t('notify.maint.kind.once')], ['weekly', t('notify.maint.kind.weekly')]], () => sync(), null, true);
        const title = h('input', { type: 'text', maxlength: '80', placeholder: t('notify.maint.title_ph') });
        const start = h('input', { type: 'datetime-local', value: toLocalInput(Date.now() / 1000) });
        const dur = sel('3600', [['1800', t('time.minutes', { n: 30 })], ['3600', t('time.hours', { n: 1 })], ['7200', t('time.hours', { n: 2 })], ['14400', t('time.hours', { n: 4 })], ['28800', t('time.hours', { n: 8 })], ['86400', t('time.hours', { n: 24 })]], () => {}, null, true);
        const days = h('div', { class: 'nt-days', role: 'group', 'aria-label': t('notify.maint.days') }, DAYS.map(d => h('label', { class: 'nt-day' }, h('input', { type: 'checkbox', value: String(d) }), h('span', {}, dayName(d)))));
        const wfrom = h('input', { type: 'time', value: '02:00' }), wto = h('input', { type: 'time', value: '04:00' });
        const scope = sel('all', [['all', t('notify.maint.all')], ['some', t('notify.maint.some')]], () => sync(), null, true);
        const filter = h('input', { type: 'search', class: 'input-sm', placeholder: t('notify.maint.filter') });
        const devList = h('div', { class: 'nt-devpick choice-list choice-list--compact' });
        const picked = new Set();
        async function fillDevices() {
          if (!devices) devices = (await api('/api/devices')).devices || [];
          const q = filter.value.trim().toLowerCase();
          devList.replaceChildren(...devices.filter(d => !q || (d.title + ' ' + d.ip).toLowerCase().includes(q)).slice(0, 60).map(d => {
            const cb = h('input', { type: 'checkbox', checked: picked.has(d.id), onchange: () => { cb.checked ? picked.add(d.id) : picked.delete(d.id); } });
            return h('label', { class: 'choice' }, cb, h('span', { class: 'choice__main' }, h('span', { class: 'choice__title' }, d.title || d.ip), h('span', { class: 'muted mono' }, d.ip || '')));
          }));
        }
        filter.addEventListener('input', fillDevices);
        const onceBox = h('div', { class: 'grid-2' }, LL.field(t('notify.maint.start'), start), LL.field(t('notify.maint.duration'), dur));
        const weekBox = h('div', { class: 'stack-sm' }, LL.field(t('notify.maint.days'), days), h('div', { class: 'grid-2 nt-times' }, LL.field(t('notify.quiet.from'), wfrom), LL.field(t('notify.quiet.to'), wto)));
        const devBox = h('div', { class: 'stack-sm' }, filter, devList);
        function sync() { onceBox.hidden = kind.value !== 'once'; weekBox.hidden = kind.value !== 'weekly'; devBox.hidden = scope.value !== 'some'; if (!devBox.hidden) fillDevices(); }
        const add = async body => {
          LL.msg(msg, '');
          try { const res = await api('/api/notify/maintenance', { method: 'POST', body }); data.maintenance = (data.maintenance || []).concat([res.window]); if (res.window.active) data.active_window = res.window.id; render(); LL.toast(t('notify.maint.added'), 'ok'); }
          catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        };
        const now1h = h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => { const s = Date.now() / 1000; add({ kind: 'once', title: t('notify.maint.quick_title'), start: s, end: s + 3600 }); } }, t('notify.maint.quick'));
        const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: () => {
          const body = { kind: kind.value, title: title.value.trim(), devices: scope.value === 'some' ? [...picked] : [] };
          if (kind.value === 'once') { body.start = fromLocalInput(start.value); body.end = body.start + Number(dur.value); }
          else { body.days = [...days.querySelectorAll('input:checked')].map(i => Number(i.value)); body.from = wfrom.value; body.to = wto.value; }
          add(body);
        } }, t('notify.maint.add'));
        const form = h('details', { class: 'nt-form' }, h('summary', {}, t('notify.maint.new')),
          h('div', { class: 'stack-sm' }, h('div', { class: 'grid-2' }, LL.field(t('notify.maint.kind'), kind), LL.field(t('notify.maint.name'), title)), onceBox, weekBox,
            LL.field(t('notify.maint.scope'), scope), devBox, h('div', { class: 'btn-row btn-row--wrap' }, save)));
        sync();
        kids.push(h('div', { class: 'btn-row btn-row--wrap' }, now1h), form);
      }
      kids.push(msg);
      return card(t('notify.maint.title'), kids);
    }

    // ---- shared limits (admin)
    async function sharedCard() {
      const s = data.settings;
      const msg = h('div', { class: 'msg', role: 'status' });
      const put = async patch => { try { const res = await api('/api/notify/settings', { method: 'PUT', body: patch }); data.settings = res.settings; LL.toast(t('settings.saved'), 'ok'); } catch (e) { LL.msg(msg, LL.errText(e), 'error'); } };
      const cap = sel(String(s.daily_cap), data.choices.daily_cap.map(v => [String(v), t('notify.shared.cap_n', { n: v })]), v => put({ daily_cap: Number(v) }), null, true);
      const hold = sel(String(s.hold_rounds), data.choices.hold_rounds.map(v => [String(v), t('notify.shared.hold_n', { n: v })]), v => put({ hold_rounds: Number(v) }), null, true);
      const cool = sel(String(s.cooldown_h), data.choices.cooldown_h.map(v => [String(v), t('time.hours', { n: v })]), v => put({ cooldown_h: Number(v) }), null, true);
      let nets = [];
      try { nets = ((await api('/api/networks')).networks || []).map(n => n.cidr); } catch (e) { /* only offered */ }
      const chosen = new Set(s.protected_cidrs);
      const all = [...new Set(nets.concat(s.protected_cidrs))];
      const box = h('div', { class: 'choice-list choice-list--compact' });
      const renderNets = () => box.replaceChildren(...all.map(c => {
        const cb = h('input', { type: 'checkbox', checked: chosen.has(c), onchange: () => { cb.checked ? chosen.add(c) : chosen.delete(c); put({ protected_cidrs: [...chosen] }); } });
        return h('label', { class: 'choice' }, cb, h('span', { class: 'choice__main' }, h('span', { class: 'choice__title mono' }, c)));
      }));
      renderNets();
      const custom = h('input', { type: 'text', class: 'input-sm', placeholder: t('notify.shared.cidr_ph'), spellcheck: 'false' });
      const addNet = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
        const v = custom.value.trim(); if (!v) return;
        LL.msg(msg, '');
        try {
          const res = await api('/api/notify/settings', { method: 'PUT', body: { protected_cidrs: [...chosen, v] } });
          data.settings = res.settings; chosen.clear();
          res.settings.protected_cidrs.forEach(c => { chosen.add(c); if (!all.includes(c)) all.push(c); });
          custom.value = ''; renderNets(); LL.toast(t('settings.saved'), 'ok');
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      } }, t('notify.shared.cidr_add'));
      return card(t('notify.shared.title'), [h('p', { class: 'muted' }, t('notify.shared.lead')),
        h('div', { class: 'nt-grid3' }, LL.field(t('notify.shared.cap'), cap, t('notify.shared.cap_hint')), LL.field(t('notify.shared.hold'), hold, t('notify.shared.hold_hint')),
          LL.field(t('notify.shared.cooldown'), cool, t('notify.shared.cooldown_hint'))),
        h('h3', {}, t('notify.shared.protected')), h('p', { class: 'muted fineprint' }, t('notify.shared.protected_hint')), box,
        h('div', { class: 'nt-inline' }, custom, addNet), msg]);
    }

    // ---- heartbeat (admin)
    function heartbeatCard() {
      let hb = data.heartbeat;
      const msg = h('div', { class: 'msg', role: 'status' });
      const on = h('input', { type: 'checkbox', checked: hb.enabled });
      const url = h('input', { type: 'url', placeholder: hb.url_set ? t('notify.hb.url_saved') : t('notify.hb.url_ph'), autocomplete: 'off', spellcheck: 'false' });
      const every = sel(String(hb.every_min), data.choices.heartbeat.map(v => [String(v), t('time.minutes', { n: v })]), v => put({ every_min: Number(v) }), null, true);
      const state = h('p', { class: 'muted nt-hb-state' });
      const show = () => { state.textContent = !hb.enabled ? t('notify.hb.off') : (hb.ok === false ? t('notify.hb.failing', { why: t(hb.key || 'report.send.error'), n: hb.fails }) : (hb.last_ok ? t('notify.hb.ok', { when: LL.fmtDate(hb.last_ok) }) : t('notify.hb.waiting'))); state.className = 'nt-hb-state ' + (hb.enabled && hb.ok === false ? 'msg msg--error' : 'muted'); };
      async function put(patch) {
        LL.msg(msg, '');
        try { hb = (await api('/api/notify/heartbeat', { method: 'PUT', body: patch })).heartbeat; data.heartbeat = hb; on.checked = hb.enabled; url.value = ''; url.placeholder = hb.url_set ? t('notify.hb.url_saved') : t('notify.hb.url_ph'); show(); LL.toast(t('settings.saved'), 'ok'); }
        catch (e) { LL.msg(msg, LL.errText(e), 'error'); on.checked = hb.enabled; }
      }
      on.addEventListener('change', () => put(url.value.trim() ? { url: url.value.trim(), enabled: on.checked } : { enabled: on.checked }));
      url.addEventListener('change', () => { if (url.value.trim()) put({ url: url.value.trim() }); });
      show();
      return card(t('notify.hb.title'), [h('p', { class: 'muted' }, t('notify.hb.lead')),
        h('div', { class: 'grid-2' }, LL.field(t('notify.hb.url'), url, t('notify.hb.url_hint')), LL.field(t('notify.hb.every'), every)),
        h('label', { class: 'check' }, on, h('span', {}, t('notify.hb.on'))), state, msg,
        h('div', { class: 'btn-row btn-row--wrap' },
          hb.url_set ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => put({ url: '', enabled: false }) }, t('notify.hb.forget')) : null,
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
            try { const res = await api('/api/notify/heartbeat/test', { method: 'POST', body: {} }); hb = res.heartbeat; data.heartbeat = hb; show(); LL.msg(msg, res.ok ? t('notify.hb.test_ok') : t('notify.test.failed', { why: t(res.key) }), res.ok ? 'ok' : 'error'); }
            catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
          } }, t('notify.hb.test')))]);
    }

    // ---- history
    function historyCard() {
      const list = h('ol', { class: 'nt-hist', 'aria-label': t('notify.hist.title') });
      const more = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', hidden: true }, t('notify.hist.more'));
      let last = 0;
      async function load(before) {
        const res = await api('/api/notify/history?limit=30' + (before ? '&before=' + before : ''));
        res.items.forEach(e => {
          last = e.id;
          const who = e.outcome ? Object.entries(e.outcome).map(([k, v]) => (k === '*' ? t('notify.hist.everyone') : k) + ': ' + t('notify.status.' + (v === 'sending' ? 'pending' : v))).join(' · ') : '';
          list.append(h('li', { class: 'nt-hist__row' },
            h('span', { class: 'nt-hist__time muted' }, LL.fmtDate(e.t)),
            h('span', { class: 'nt-hist__what' }, h('span', { class: 'nt-sev nt-sev--' + e.severity, title: t('notify.sev.' + e.severity) }), eventText(e),
              e.resolved ? h('span', { class: 'muted' }, ' · ' + t('notify.hist.resolved', { after: LL.fmtAge(e.resolved - e.t) })) : null),
            h('span', { class: 'chip ' + (STATUS_CHIP[e.status] || 'chip--muted'), title: who || t('notify.status.' + e.status + '.hint') }, t('notify.status.' + e.status))));
        });
        more.hidden = res.items.length < 30;
        if (!list.children.length) list.replaceWith(h('p', { class: 'muted fineprint' }, t('notify.hist.none')));
      }
      more.addEventListener('click', () => load(last));
      load();
      return card(t('notify.hist.title'), [h('p', { class: 'muted' }, t('notify.hist.lead')), list, h('div', { class: 'btn-row' }, more)]);
    }

    const parts = [intro(), pushCard(), rulesCard(), quietCard(), channelsCard(), maintCard()];
    if (admin) parts.push(await sharedCard(), heartbeatCard());
    parts.push(historyCard());
    host.replaceChildren(...parts);
    if (location.hash.indexOf('maintenance') >= 0) { const m = host.querySelector('.nt-form'); if (m) { m.open = true; m.scrollIntoView({ block: 'center' }); } }
    return {};
  };

  // ------------------------------------------------------------------ commands (Ctrl K)
  LL.registerCommand({ id: 'go.settings.notifications', group: 'views', order: 27, typedOnly: true, label: () => t('cmdk.go', { name: t('nav.settings') + ' › ' + t('notify.tab') }),
    keywords: () => t('cmdk.kw.notifications'), run: () => LL.go('#/settings/notifications') });
  LL.registerCommand({ id: 'notify-maint-1h', group: 'commands', order: 50, label: 'notify.maint.quick', keywords: () => t('cmdk.kw.maintenance'), available: () => LL.can('operate'),
    run: async () => {
      const s = Date.now() / 1000;
      try { await api('/api/notify/maintenance', { method: 'POST', body: { kind: 'once', title: t('notify.maint.quick_title'), start: s, end: s + 3600 } }); LL.toast(t('notify.maint.added'), 'ok'); }
      catch (e) { LL.toast(LL.errText(e), 'error'); }
    } });
  LL.registerCommand({ id: 'notify-push-on', group: 'commands', order: 51, label: 'notify.push.on', keywords: () => t('cmdk.kw.notifications'), available: () => LL.pushSupported(),
    run: async () => {
      try { const d = await api('/api/notify'); await LL.pushEnable(d.push.vapid); LL.toast(t('notify.push.on_done'), 'ok'); }
      catch (e) { LL.toast(LL.errText(e), 'error'); }
    } });

  // ------------------------------------------------------------------ service worker (push + offline shell)
  // Registered only where it is needed: on this page (to switch push on), in the installed app (offline shell) and in a
  // browser that already receives push. A plain browser tab never gets one, so nothing sits between it and the server.
  LL.swRegister = function () {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return Promise.resolve(null);
    return navigator.serviceWorker.register(LL.url('sw.js'), { scope: LL.base }).catch(() => null);
  };
  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      const standalone = window.matchMedia && window.matchMedia('(display-mode: standalone)').matches;
      if (standalone || (window.Notification && Notification.permission === 'granted')) LL.swRegister();
    });
  }
})();
