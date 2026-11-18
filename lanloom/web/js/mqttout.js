/* Lanloom — Settings › MQTT output: a few of Lanloom's own figures as sensors of a home-automation hub (MQTT discovery,
   lanloom/mqtt_out.py). Off by default; administrators only. Shows the broker, the topics, the entities with the value
   they would carry right now, and the state of the connection. Only counts, a level, a flag and a time are published. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;

  LL.registerCommand({ id: 'go-mqtt-output', group: 'views', order: 32, typedOnly: true, available: () => LL.can('admin'),
    label: () => t('cmdk.go', { name: t('nav.settings') + ' › ' + t('mqtt_out.title') }), keywords: () => t('cmdk.kw.mqtt_out'),
    run: () => LL.go('#/settings/mqtt') });

  LL.mqttOutCard = function () {
    const sec = h('section', { class: 'card settings-card mqo-card', id: 'mqtt-output' }, h('h2', {}, t('mqtt_out.title')),
      h('div', { class: 'skeleton' }));
    let view = null;
    let timer = null;
    const msg = h('div', { class: 'msg' });

    function stateLine(st) {
      const now = Date.now() / 1000;
      const cls = st.state === 'connected' ? 'mqo-ok' : st.state === 'failed' ? 'mqo-bad' : 'mqo-idle';
      const text = st.state === 'failed' ? t('mqtt_out.state.failed', { reason: t(st.error || 'mqtt_out.err.protocol') })
        : t('mqtt_out.state.' + (st.state || 'off'));
      return LL.facts([
        [t('mqtt_out.state'), h('span', { class: 'mqo-state ' + cls }, h('i', { class: 'mqo-dot', 'aria-hidden': 'true' }), text)],
        st.since && st.state !== 'off' ? [t('mqtt_out.since'), LL.fmtAge(now - st.since)] : null,
        st.state === 'connected' ? [t('mqtt_out.published'), LL.fmtNum(st.published || 0, 0)] : null,
        st.last ? [t('mqtt_out.last'), t('mqtt_out.ago', { age: LL.fmtAge(now - st.last) })] : null]);
    }

    function draw(v) {
      view = v;
      const s = v.settings;
      const on = h('input', { type: 'checkbox', checked: !!s.enabled });
      const broker = h('select', { class: 'select', 'aria-label': t('mqtt_out.broker') },
        h('option', { value: 'manual' }, t('mqtt_out.broker.manual')),
        v.addon ? h('option', { value: 'addon', disabled: !v.addon_broker }, t('mqtt_out.broker.addon')) : null);
      broker.value = s.broker === 'addon' && v.addon ? 'addon' : 'manual';
      const host = h('input', { class: 'input mono', type: 'text', value: s.host || '', placeholder: '192.0.2.10', autocomplete: 'off', spellcheck: 'false' });
      const port = h('input', { class: 'input mono', type: 'number', min: '1', max: '65535', inputmode: 'numeric', value: String(s.port || 1883) });
      const user = h('input', { class: 'input', type: 'text', value: s.username || '', autocomplete: 'off' });
      const pass = h('input', { class: 'input', type: 'password', value: '', autocomplete: 'new-password',
        placeholder: v.password_set ? t('mqtt_out.password.kept') : '' });
      const tls = h('input', { type: 'checkbox', checked: !!s.tls });
      const selfSigned = h('input', { type: 'checkbox', checked: !!s.allow_self_signed });
      const disc = h('input', { class: 'input mono', type: 'text', value: s.discovery_prefix, spellcheck: 'false' });
      const base = h('input', { class: 'input mono', type: 'text', value: s.base_topic, spellcheck: 'false' });
      const node = h('input', { class: 'input mono', type: 'text', value: s.node_id, spellcheck: 'false', pattern: '[a-z0-9_]{1,40}' });
      const manual = h('div', { class: 'mqo-manual' },
        h('div', { class: 'grid-2' }, LL.field(t('mqtt_out.host'), host, t('mqtt_out.host.hint')), LL.field(t('mqtt_out.port'), port)),
        h('div', { class: 'grid-2' }, LL.field(t('mqtt_out.username'), user), LL.field(t('mqtt_out.password'), pass, v.password_set ? t('mqtt_out.password.hint') : null)),
        h('div', { class: 'mqo-checks' }, h('label', { class: 'check' }, tls, h('span', {}, t('mqtt_out.tls'))),
          h('label', { class: 'check' }, selfSigned, h('span', {}, t('mqtt_out.self_signed')))));
      const addonHint = h('p', { class: 'muted fineprint mqo-addon' }, t(v.addon_broker ? 'mqtt_out.broker.addon.hint' : 'mqtt_out.broker.addon.none'));
      const sync = () => { const a = broker.value === 'addon'; manual.hidden = a; addonHint.hidden = !a; };
      broker.onchange = sync;
      sync();
      const adv = h('details', { class: 'mqo-adv' }, h('summary', {}, t('mqtt_out.topics')),
        h('div', { class: 'mqo-topics' }, LL.field(t('mqtt_out.discovery'), disc, t('mqtt_out.discovery.hint')),
          LL.field(t('mqtt_out.base'), base, t('mqtt_out.base.hint')), LL.field(t('mqtt_out.node'), node, t('mqtt_out.node.hint'))));
      const body = () => {
        const b = { enabled: on.checked, broker: broker.value, discovery_prefix: disc.value.trim(), base_topic: base.value.trim(), node_id: node.value.trim() };
        if (broker.value === 'manual') {
          Object.assign(b, { host: host.value.trim(), port: Number(port.value), username: user.value.trim(), tls: tls.checked, allow_self_signed: selfSigned.checked });
          if (pass.value) b.password = pass.value;
        }
        return b;
      };
      const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
        LL.msg(msg, '');
        save.disabled = true;
        try { draw(await api('/api/mqtt-out', { method: 'PUT', body: body() })); LL.msg(msg, t(on.checked ? 'mqtt_out.saved_on' : 'mqtt_out.saved_off'), 'ok'); schedule(); }
        catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        finally { save.disabled = false; }
      } }, t('mqtt_out.save'));
      const test = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
        LL.msg(msg, t('mqtt_out.testing'), 'info');
        test.disabled = true;
        try {
          const r = await api('/api/mqtt-out/test', { method: 'POST', body: body() });
          LL.msg(msg, r.ok ? t('mqtt_out.test.ok') : t('mqtt_out.test.failed', { reason: t(r.error || 'mqtt_out.err.protocol') }), r.ok ? 'ok' : 'error');
        } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        finally { test.disabled = false; }
      } }, t('mqtt_out.test'));
      const rows = v.entities.map(e => h('tr', {},
        h('td', {}, h('span', { class: 'mqo-name' }, e.name), h('span', { class: 'mqo-comp muted' }, t('mqtt_out.comp.' + e.component))),
        h('td', { class: 'num mono' }, e.value == null || e.value === 'None' ? '—' : valueText(e)),
        h('td', { class: 'mono mqo-topic truncate', title: e.state_topic }, e.state_topic)));
      const table = h('div', { class: 'mqo-wrap' }, h('table', { class: 'mini-table mqo-table' },
        h('caption', { class: 'sr-only' }, t('mqtt_out.entities')),
        h('thead', {}, h('tr', {}, h('th', {}, t('mqtt_out.entity')), h('th', { class: 'num' }, t('mqtt_out.value')), h('th', {}, t('mqtt_out.state_topic')))),
        h('tbody', {}, rows)));
      sec.replaceChildren(...[h('h2', {}, t('mqtt_out.title')),
        h('p', { class: 'muted' }, t('mqtt_out.text')),
        h('p', { class: 'muted fineprint mqo-privacy' }, t('mqtt_out.privacy')),
        h('label', { class: 'check' }, on, h('span', {}, t('mqtt_out.enable'))),
        v.addon ? LL.field(t('mqtt_out.broker'), broker) : null, addonHint, manual, adv,
        h('div', { class: 'mqo-status' }, stateLine(v.status)),
        h('h3', { class: 'mqo-sub' }, t('mqtt_out.entities')), table,
        h('p', { class: 'muted fineprint' }, t('mqtt_out.entities.hint')),
        msg, h('div', { class: 'btn-row' }, save, test)].filter(Boolean));          // replaceChildren would write null as text
      if (!v.addon) addonHint.hidden = true;
    }

    function valueText(e) {
      if (e.component === 'binary_sensor') return t(e.value === 'ON' ? 'mqtt_out.val.on' : 'mqtt_out.val.off');
      if (e.object === 'health') return t('mqtt_out.val.health.' + e.value);
      if (e.object === 'nightly_report') { const d = new Date(e.value); return isNaN(d) ? e.value : LL.fmtDate(d.getTime() / 1000); }
      return LL.fmtNum(Number(e.value), 0);
    }

    async function refreshStatus() {
      if (!sec.isConnected) { clearInterval(timer); timer = null; return; }
      try {
        const v = await api('/api/mqtt-out');
        const box = sec.querySelector('.mqo-status');
        if (box) box.replaceChildren(stateLine(v.status));
      } catch (e) { /* the last state stays */ }
    }
    function schedule() { if (!timer) timer = setInterval(refreshStatus, 5000); }

    api('/api/mqtt-out').then(v => { draw(v); if (v.settings.enabled) schedule(); })
      .catch(e => sec.replaceChildren(h('h2', {}, t('mqtt_out.title')), h('p', { class: 'msg msg--error' }, LL.errText(e))));
    return sec;
  };
})();
