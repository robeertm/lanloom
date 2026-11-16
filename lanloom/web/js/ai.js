/* Lanloom — AI: settings page (Settings > AI), "Explain" button and dialog.
   API: GET/PUT /api/ai, POST /api/ai/models, POST /api/ai/test, GET /api/ai/jobs/<id>, POST /api/ai/explain {subject, ref, preview?},
   GET /api/ai/local, POST /api/ai/local/{discover,install,use,remove}, POST /api/ai/pairing.
   The browser never contacts a model service: every check runs on the Lanloom server. Keys are write-only. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;

  function card(title, kids, id) { return h('section', { class: 'card settings-card ai-card', id: id || null }, h('h2', {}, title), kids); }
  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  function secs(ms) { return LL.fmtNum(Math.max(0, ms || 0) / 1000, ms < 10000 ? 1 : 0); }

  // ------------------------------------------------------------------ jobs: acknowledged at once, answer by polling
  async function waitJob(id, onState, path) {
    const until = Date.now() + 6 * 60 * 1000;
    for (;;) {
      const j = await api((path || '/api/ai/jobs/') + id);
      if (onState) onState(j);
      if (j.state === 'done' || j.state === 'error') return j;
      if (Date.now() > until) return { state: 'error', error: 'ai.err.timeout' };
      await sleep(j.state === 'answering' ? 700 : 1100);
    }
  }
  function jobText(j) {
    if (j.state === 'queued') return t('ai.job.queued');
    if (j.state === 'answering') return t('ai.job.answering', { s: Math.round(j.age_s || 0) });
    return t('ai.job.waiting', { s: Math.round(j.age_s || 0) });
  }

  // ------------------------------------------------------------------ provider form (one per scope)
  function providerForm(scope, data, onSaved) {
    const s = data.settings[scope];
    const list = data.providers.filter(p => p.scope === scope);
    const kind = h('select', { class: 'select', 'aria-label': t('ai.provider') }, list.map(p => h('option', { value: p.kind }, p.label)));
    kind.value = list.some(p => p.kind === s.kind) ? s.kind : (list[0] && list[0].kind);
    const url = h('input', { type: 'url', class: 'input', value: s.url || '', spellcheck: 'false', autocomplete: 'off', inputmode: 'url' });
    const key = h('input', { type: 'password', class: 'input', autocomplete: 'off', spellcheck: 'false' });
    const model = h('input', { type: 'text', class: 'input', id: 'ai-model-' + scope, value: s.model || '', spellcheck: 'false', autocomplete: 'off', list: 'ai-models-' + scope });
    const models = h('datalist', { id: 'ai-models-' + scope });
    const enabled = h('input', { type: 'checkbox', checked: !!s.enabled });
    const msg = h('div', { class: 'msg' });
    const answer = h('div', { class: 'ai-answer', hidden: true });
    const urlField = LL.field(t('ai.url'), url, t('ai.url.hint'));
    const keyField = LL.field(t('ai.key'), key, '');
    const keyHint = keyField.querySelector('.field__hint') || keyField.appendChild(h('div', { class: 'field__hint' }));
    const clearKey = h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', hidden: true, onclick: async () => {
      try { onSaved(await api('/api/ai', { method: 'PUT', body: { settings: { [scope]: { enabled: false } }, secrets: { ['clear_' + scope + '_key']: true } } }), t('ai.key.removed')); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('ai.key.remove'));
    function prov() { return list.find(p => p.kind === kind.value) || {}; }
    function sync() {
      const p = prov();
      urlField.hidden = !!p.fixed_url || p.key === undefined || p.kind === 'demo' || p.kind === 'demo_cloud';
      url.placeholder = p.hint || '';
      keyField.hidden = p.key === 'none';
      const saved = p.kind === s.kind && s.has_key;
      key.placeholder = saved ? t('ai.key.saved') : (p.key === 'required' ? t('ai.key.required') : t('ai.key.optional'));
      keyHint.textContent = t('ai.key.hint');
      clearKey.hidden = !saved;
    }
    kind.addEventListener('change', () => { model.value = ''; models.replaceChildren(); answer.hidden = true; sync(); });
    sync();
    function draft() {
      const d = { scope, kind: kind.value, url: url.value.trim(), model: model.value.trim() };
      if (key.value.trim()) d.key = key.value.trim();
      return d;
    }
    async function loadModels() {
      LL.msg(msg, t('ai.models.loading'), 'info'); loadBtn.disabled = true;
      try {
        const r = await api('/api/ai/models', { method: 'POST', body: draft() });
        if (!r.ok) { LL.msg(msg, t(r.error || 'ai.err.http'), 'error'); return false; }
        models.replaceChildren(...r.models.map(m => h('option', { value: m })));
        if (!model.value && r.recommended) model.value = r.recommended;
        LL.msg(msg, r.models.length ? t('ai.models.found', { n: r.models.length, model: r.recommended || '—' }) : t('ai.models.none'), r.models.length ? 'ok' : 'info');
        return !!model.value;
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); return false; } finally { loadBtn.disabled = false; }
    }
    const loadBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => loadModels() }, t('ai.models.load'));
    const testBtn = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
      testBtn.disabled = true; answer.hidden = true;
      try {
        if (!model.value.trim() && !(await loadModels())) return;
        const job = await api('/api/ai/test', { method: 'POST', body: draft() });
        LL.msg(msg, t('ai.job.queued'), 'info');
        const j = await waitJob(job.id, x => LL.msg(msg, jobText(x), 'info'));
        if (j.state === 'done') {
          LL.msg(msg, t('ai.test.ok', { s: secs(j.result.ms), model: j.result.model }), 'ok');
          answer.replaceChildren(h('span', { class: 'ai-answer__label' }, t('ai.test.answer')), h('q', {}, j.result.text));
          answer.hidden = false;
        } else LL.msg(msg, t(j.error || 'ai.err.http'), 'error');
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); } finally { testBtn.disabled = false; }
    } }, t('ai.test'));
    const saveBtn = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      saveBtn.disabled = true;
      const body = { settings: { [scope]: { kind: kind.value, url: url.value.trim(), model: model.value.trim(), enabled: enabled.checked } }, secrets: {} };
      if (scope === 'local') body.settings.local.origin = 'manual';
      if (key.value.trim()) body.secrets[scope + '_key'] = key.value.trim();
      try { const r = await api('/api/ai', { method: 'PUT', body }); key.value = ''; onSaved(r, t('settings.saved')); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); } finally { saveBtn.disabled = false; }
    } }, t('ai.save'));
    return h('div', { class: 'ai-form' },
      h('div', { class: 'grid-2' }, LL.field(t('ai.provider'), kind), urlField),
      h('div', { class: 'grid-2' }, keyField, h('div', { class: 'field' }, h('label', { class: 'field__label', for: model.id }, t('ai.model')),
        h('div', { class: 'ai-model-row' }, model, loadBtn), models, h('div', { class: 'field__hint' }, t('ai.model.hint')))),
      clearKey.hidden ? null : h('div', { class: 'btn-row' }, clearKey),
      h('label', { class: 'check' }, enabled, h('span', {}, t(scope === 'local' ? 'ai.local.enable' : 'ai.public.enable'))),
      msg, answer,
      h('div', { class: 'btn-row btn-row--wrap' }, saveBtn, testBtn));
  }

  // ------------------------------------------------------------------ one-click local setup
  const SETUP_STATES = ['installing', 'waiting', 'pulling', 'testing'];
  function setupView(st) {
    if (!st || st.state === 'idle') return null;
    const pct = st.total ? Math.round(100 * st.completed / st.total) : null;
    const label = st.state === 'pulling' ? t('ai.setup.pulling', { model: st.model || '', pct: pct == null ? '…' : pct })
      : st.state === 'ready' ? t('ai.setup.ready', { model: st.model || '', s: secs(st.ms) })
      : st.state === 'failed' ? t(st.message_key || 'ai.err.install_failed') : t('ai.setup.' + st.state);
    const bar = SETUP_STATES.includes(st.state)
      ? h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct || 0) },
        h('div', { class: 'progress__bar', style: 'width:' + Math.max(4, pct || (st.state === 'testing' ? 96 : 8)) + '%' })) : null;
    return h('div', { class: 'ai-setup ai-setup--' + st.state }, h('div', { class: 'msg msg--' + (st.state === 'failed' ? 'error' : st.state === 'ready' ? 'ok' : 'info') }, label), bar);
  }

  function adviceView(adv, host) {
    if (!adv) return null;
    const facts = t('ai.hw.facts', { ram: host.ram_gb || '?', cores: host.cores || '?' });
    const main = adv.speed === 'unsuitable' ? t('ai.hw.none') : t('ai.hw.pick', { model: adv.model, gb: adv.download_gb, size: t('ai.hw.size.' + adv.size) });
    return h('div', { class: 'ai-advice ai-advice--' + adv.speed },
      h('div', { class: 'ai-advice__main' }, h('span', { class: 'chip chip--' + ({ good: 'ok', ok: 'ok', slow: 'warn', unsuitable: 'danger' }[adv.speed] || 'muted') }, t('ai.hw.speed.' + adv.speed)),
        h('span', {}, main)),
      h('div', { class: 'muted ai-advice__facts' }, facts),
      adv.notes && adv.notes.length ? h('ul', { class: 'ai-notes' }, adv.notes.map(k => h('li', {}, t(k)))) : null);
  }

  function localOneClick(data, refresh) {
    const box = h('div', { class: 'stack ai-oneclick' });
    const setupBox = h('div', {});
    const msg = h('div', { class: 'msg' });
    let timer = null;
    async function follow() {
      clearTimeout(timer);
      try {
        const r = await api('/api/ai/local');
        setupBox.replaceChildren(setupView(r.setup) || '');
        if (SETUP_STATES.includes(r.setup.state)) timer = setTimeout(follow, 1500);
        else if (r.setup.state === 'ready') { timer = null; refresh(t('ai.setup.done')); }
      } catch (e) { timer = setTimeout(follow, 4000); }
    }
    box._stop = () => clearTimeout(timer);
    const st = data.setup || {};
    setupBox.replaceChildren(setupView(st) || '');
    if (SETUP_STATES.includes(st.state)) follow();
    // (a) on this computer, through the updater sidecar
    const install = h('button', { class: 'btn btn--primary', type: 'button', disabled: !data.sidecar || (data.advice && data.advice.speed === 'unsuitable'), onclick: async () => {
      install.disabled = true; LL.msg(msg, '');
      try { setupBox.replaceChildren(setupView(await api('/api/ai/local/install', { method: 'POST', body: {} })) || ''); follow(); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); install.disabled = false; }
    } }, t('ai.local.install'));
    box.append(...[h('h3', {}, t('ai.local.here')), adviceView(data.advice, data.host),
      data.sidecar ? h('p', { class: 'muted' }, t('ai.local.here.text')) : h('p', { class: 'muted' }, t('ai.local.no_sidecar')),
      data.sidecar ? null : h('pre', { class: 'ai-cmd' }, 'sh install.sh --with-ai'),
      h('div', { class: 'btn-row btn-row--wrap' }, install,
        data.settings.local.origin === 'sidecar' && data.sidecar ? h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', onclick: async () => {
          try { await api('/api/ai/local/remove', { method: 'POST', body: {} }); refresh(t('ai.local.removed')); } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        } }, t('ai.local.remove')) : null),
      setupBox, msg].filter(Boolean));
    // (b) find a server Lanloom can reach: known addresses only
    const found = h('div', { class: 'stack' });
    const findBtn = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
      findBtn.disabled = true; found.replaceChildren(h('div', { class: 'msg msg--info' }, t('ai.find.running')));
      try { drawFound(await api('/api/ai/local/discover', { method: 'POST', body: {} })); } catch (e) { found.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); }
      finally { findBtn.disabled = false; }
    } }, t('ai.find'));
    function drawFound(r) {
      if (!r.found.length) { found.replaceChildren(h('p', { class: 'muted' }, t('ai.find.none', { n: r.probed }))); return; }
      found.replaceChildren(h('ul', { class: 'ai-found' }, r.found.map(f => h('li', { class: 'ai-found__row' },
        h('div', { class: 'ai-found__main' }, h('div', { class: 'ai-found__title' }, f.title || (f.this_host ? t('ai.find.this_host') : f.url)),
          h('div', { class: 'muted mono ai-found__meta' }, f.url + ' · ' + (f.models.length ? t('ai.find.models', { n: f.models.length }) : t('ai.find.no_models')))),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async (ev) => {
          ev.target.disabled = true;
          try { setupBox.replaceChildren(setupView(await api('/api/ai/local/use', { method: 'POST', body: { url: f.url } })) || ''); follow(); }
          catch (e) { LL.msg(msg, LL.errText(e), 'error'); ev.target.disabled = false; }
        } }, f.models.length ? t('ai.find.use') : t('ai.find.use_pull'))))));
    }
    if (data.discovered && data.discovered.t) drawFound(data.discovered);
    box.append(h('hr'), h('h3', {}, t('ai.find.title')), h('p', { class: 'muted' }, t('ai.find.text')), h('div', { class: 'btn-row' }, findBtn), found);
    // (c) another computer: setup script with a one-time code
    box.append(h('hr'), anotherComputer(data, refresh));
    return box;
  }

  function anotherComputer(data, refresh) {
    const wrap = h('div', { class: 'stack' });
    const sugg = h('div', {});
    const codeBox = h('div', {});
    const origin = location.origin;
    api('/api/ai/local').then(r => {
      if (!r.suggestions.length) { sugg.replaceChildren(h('p', { class: 'muted' }, t('ai.other.no_suggestions'))); return; }
      sugg.replaceChildren(h('p', { class: 'muted' }, t('ai.other.suggest')), h('ul', { class: 'ai-sugg' }, r.suggestions.map(s => h('li', {},
        h('span', { class: 'ai-sugg__title' }, s.title), h('span', { class: 'chip' }, t('kind.' + s.kind)),
        h('span', { class: 'chip chip--' + (s.status === 'up' ? 'ok' : 'muted') }, t('board.status.' + (s.status || 'unknown')))))));
    }).catch(() => {});
    let poll = null;
    const make = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
      const r = await api('/api/ai/pairing', { method: 'POST', body: {} });
      const q = '?code=' + encodeURIComponent(r.code);
      const cmd = (os) => os.shell === 'ps1' ? '& ([scriptblock]::Create((irm "' + origin + '/api/ai/installer/' + os.id + q + '")))'
        : 'curl -fsSL "' + origin + '/api/ai/installer/' + os.id + q + '" | sh';
      codeBox.replaceChildren(h('div', { class: 'ai-code' },
        h('div', { class: 'ai-code__head' }, h('span', { class: 'muted' }, t('ai.other.code')), h('b', { class: 'mono ai-code__value' }, r.code),
          h('span', { class: 'muted' }, t('ai.other.valid', { when: LL.fmtDate(r.expires) }))),
        ...r.os.map(os => h('div', { class: 'ai-code__os' },
          h('div', { class: 'ai-code__os-head' }, h('b', {}, os.label),
            h('a', { class: 'btn btn--ghost btn--sm', href: '/api/ai/installer/' + os.id + q, download: '' }, t('ai.other.download'))),
          h('pre', { class: 'ai-cmd' }, cmd(os)))),
        h('p', { class: 'muted' }, t('ai.other.after'))));
      clearInterval(poll);
      const startedOrigin = data.settings.local.origin, startedUrl = data.settings.local.url;
      poll = setInterval(async () => {
        if (!document.body.contains(codeBox)) { clearInterval(poll); return; }
        try { const d = await api('/api/ai'); if (d.settings.local.origin === 'paired' && (startedOrigin !== 'paired' || d.settings.local.url !== startedUrl)) { clearInterval(poll); refresh(t('ai.other.paired', { model: d.settings.local.model })); } }
        catch (e) { /* keep polling */ }
      }, 4000);
    } }, t('ai.other.make'));
    wrap.append(h('h3', {}, t('ai.other.title')), h('p', { class: 'muted' }, t('ai.other.text')), sugg, h('div', { class: 'btn-row' }, make), codeBox);
    return wrap;
  }

  // ------------------------------------------------------------------ public: privacy rules
  function privacyCard(data, onSaved) {
    const s = data.settings;
    const msg = h('div', { class: 'msg' });
    const boxes = data.functions.map(f => {
      const cb = h('input', { type: 'checkbox', checked: !!s.allow[f] });
      return [f, cb, h('label', { class: 'check' }, cb, h('span', {}, t('ai.allow.' + f)))];
    });
    const limit = h('input', { type: 'number', class: 'input ai-limit', min: '0', max: '1000', step: '1', value: String(s.daily_limit) });
    const order = h('select', { class: 'select' }, ['local_first', 'public_first'].map(o => h('option', { value: o }, t('ai.order.' + o))));
    order.value = s.order;
    const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      const allow = {}; boxes.forEach(([f, cb]) => { allow[f] = cb.checked; });
      try { onSaved(await api('/api/ai', { method: 'PUT', body: { settings: { allow, daily_limit: Number(limit.value), order: order.value } } }), t('settings.saved')); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('ai.save'));
    const u = data.usage;
    return h('div', { class: 'stack ai-privacy' },
      h('h3', {}, t('ai.privacy.title')),
      h('ul', { class: 'ai-notes ai-notes--rules' }, ['ai.privacy.rule1', 'ai.privacy.rule2', 'ai.privacy.rule3', 'ai.privacy.rule4'].map(k => h('li', {}, t(k)))),
      h('div', { class: 'ai-allow' }, h('div', { class: 'field__label' }, t('ai.allow.title')), boxes.map(b => b[2])),
      h('div', { class: 'grid-2' }, LL.field(t('ai.limit'), limit, t('ai.limit.hint', { used: u.public, limit: u.limit })), LL.field(t('ai.order'), order, t('ai.order.hint'))),
      msg, h('div', { class: 'btn-row' }, save));
  }

  // ------------------------------------------------------------------ the page
  LL.aiSettingsPage = async function (page) {
    let data = await api('/api/ai');
    const holder = h('div', { class: 'stack ai-page' });
    page.append(holder);
    let oneClick = null;
    function draw(note) {
      if (oneClick && oneClick._stop) oneClick._stop();
      const s = data.settings, r = data.ready;
      const on = s.local.enabled || s.public.enabled;
      const status = !on ? t('ai.status.off') : [s.local.enabled ? t('ai.status.local', { model: s.local.model || '—' }) : null,
        s.public.enabled ? t('ai.status.public', { label: s.public.label, model: s.public.model || '—' }) : null].filter(Boolean).join(' · ');
      const intro = card(t('ai.title'), [
        h('p', {}, t('ai.intro')),
        h('div', { class: 'ai-status' }, h('span', { class: 'chip chip--' + (on ? 'ok' : 'muted') }, on ? t('ai.on') : t('ai.off')), h('span', {}, status)),
        h('p', { class: 'muted' }, on ? t('ai.usage', { local: data.usage.local, public: data.usage.public, limit: data.usage.limit }) : t('ai.nothing_leaves')),
        r && !Object.values(r).some(Boolean) && on ? h('p', { class: 'msg msg--info' }, t('ai.not_ready')) : null,
        note ? h('div', { class: 'msg msg--ok' }, note) : null,
        h('p', { class: 'muted ai-never' }, t('ai.never_writes'))], 'ai-intro');
      oneClick = localOneClick(data, refresh);
      const local = card(t('ai.local.title'), [h('p', { class: 'muted' }, t('ai.local.text')), oneClick, h('hr'),
        h('details', { class: 'ai-manual', open: s.local.origin === 'manual' || (s.local.enabled && !s.local.origin) || null },
          h('summary', {}, t('ai.local.manual')), providerForm('local', data, onSaved))], 'ai-local');
      const pub = card(t('ai.public.title'), [h('p', { class: 'muted' }, t('ai.public.text')), providerForm('public', data, onSaved), h('hr'), privacyCard(data, onSaved),
        h('p', { class: 'muted ai-notice' }, data.notice ? t('ai.notice') : '')], 'ai-public');
      holder.replaceChildren(intro, local, pub);
    }
    async function refresh(note) { data = await api('/api/ai'); await LL.refreshAiState(); draw(note); }
    function onSaved(d, note) { data = d; LL.refreshAiState(); draw(note); }
    draw();
    return { destroy() { if (oneClick && oneClick._stop) oneClick._stop(); } };
  };

  LL.refreshAiState = async function () {
    try { const st = await api('/api/state', { allow401: true }); if (LL.state) LL.state.ai = st.ai || {}; } catch (e) { /* keep */ }
  };

  // ------------------------------------------------------------------ "Explain"
  LL.aiExplainButton = function (subject, ref, opts) {
    const ready = LL.state && LL.state.ai && LL.state.ai[subject];
    if (!ready) return null;                      // without an AI set up the button does not exist
    opts = opts || {};
    return h('button', { class: 'btn btn--ghost btn--sm ai-explain' + (opts.small ? ' ai-explain--small' : ''), type: 'button',
      title: t('ai.explain.title'), 'aria-label': t('ai.explain.title'), onclick: (ev) => { ev.stopPropagation(); openExplain(subject, ref, opts.title); } },
    h('span', { class: 'ai-explain__icon', 'aria-hidden': 'true' }, '✦'), h('span', { class: 'ai-explain__label' }, t('ai.explain')));
  };

  async function openExplain(subject, ref, title) {
    const status = h('div', { class: 'msg msg--info' }, t('ai.job.queued'));
    const out = h('div', { class: 'ai-explain__answer', hidden: true });
    const meta = h('div', { class: 'muted ai-explain__meta' });
    const previewBox = h('div', { class: 'ai-preview', hidden: true });
    const previewBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
      if (!previewBox.hidden) { previewBox.hidden = true; return; }
      try { drawPreview(previewBox, await api('/api/ai/explain', { method: 'POST', body: { subject, ref, lang: LL.lang, preview: true } })); previewBox.hidden = false; }
      catch (e) { previewBox.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); previewBox.hidden = false; }
    } }, t('ai.preview'));
    const dlg = LL.dialog(title ? t('ai.explain.dialog_of', { name: title }) : t('ai.explain.dialog'),
      h('div', { class: 'stack' }, status, out, meta, previewBox),
      h('div', { class: 'btn-row ai-explain__foot' }, previewBtn, h('span', { class: 'muted ai-never' }, t('ai.never_writes_short'))));
    dlg.el.classList.add('modal--ai');
    try {
      const job = await api('/api/ai/explain', { method: 'POST', body: { subject, ref, lang: LL.lang } });
      const j = await waitJob(job.id, x => { status.textContent = jobText(x); });
      if (j.state === 'done') {
        status.remove();
        out.replaceChildren(...String(j.result.text).split(/\n{2,}/).map(p => h('p', {}, p)));
        out.hidden = false;
        meta.textContent = t(j.result.route === 'local' && !j.result.pseudonymised ? 'ai.answered.local' : 'ai.answered.public',
          { label: j.result.label, model: j.result.model, s: secs(j.result.ms) });
      } else { status.className = 'msg msg--error'; status.textContent = t(j.error || 'ai.err.http'); }
    } catch (e) { status.className = 'msg msg--error'; status.textContent = LL.errText(e); }
  }

  function drawPreview(box, p) {
    if (!p.routes.length) { box.replaceChildren(h('p', { class: 'muted' }, t('ai.preview.none'))); return; }
    box.replaceChildren(...p.routes.map(r => h('div', { class: 'ai-preview__route' },
      h('div', { class: 'ai-preview__head' }, h('b', {}, r.label), h('span', { class: 'chip chip--' + (r.pseudonymise ? 'warn' : 'ok') }, t(r.pseudonymise ? 'ai.preview.outside' : 'ai.preview.inside'))),
      r.blocked ? h('div', { class: 'msg msg--error' }, t('ai.err.privacy')) : null,
      h('p', { class: 'muted' }, r.pseudonymise ? t('ai.preview.hidden', { n: r.hidden.length }) : t('ai.preview.real')),
      h('pre', { class: 'ai-preview__text' }, r.user),
      h('details', {}, h('summary', {}, t('ai.preview.system')), h('pre', { class: 'ai-preview__text' }, r.system)))),
    p.allowed_public || !p.public_ready ? '' : h('p', { class: 'muted' }, t('ai.preview.public_off', { label: p.public_label })));
  }
})();
