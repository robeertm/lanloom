/* Lanloom — AI features (KI2): "Ask your network" panel, the plain-words paragraph of a nightly report,
   identification help on a device, firewall suggestions as a draft proposal, and the feature switches in Settings > AI.
   API: POST /api/ai/ask {question, history, lang, preview?}, POST /api/ai/report {day}, POST /api/ai/identify {id},
   POST /api/ai/hints/<device> {action: accept|dismiss|forget}, POST /api/ai/firewall {lang, preview?}, GET /api/ai/jobs/<id>.
   Everything is read-only: the AI never changes a setting. A firewall suggestion only reaches the draft through the
   user's click and then goes through the usual preview and confirmation. Without an AI set up nothing here appears. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const STORE_BASE = 'll-ask';
  const store = () => LL.siteKey(STORE_BASE);
  const MAX_TURNS = 30;
  const SPARK = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5l1.9 4.6 4.6 1.9-4.6 1.9L12 16.5l-1.9-4.6L5.5 10l4.6-1.9z"/><path d="M18.5 15.5l.8 1.9 1.9.8-1.9.8-.8 1.9-.8-1.9-1.9-.8 1.9-.8z"/></svg>';

  const ready = what => !!(LL.state && LL.state.ai && LL.state.ai[what]);
  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  function secs(ms) { return LL.fmtNum(Math.max(0, ms || 0) / 1000, ms < 10000 ? 1 : 0); }
  async function waitJob(id, onState) {
    const until = Date.now() + 6 * 60 * 1000;
    for (;;) {
      const j = await api('/api/ai/jobs/' + id);
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
  function metaText(r) {
    return t(r.route === 'local' && !r.pseudonymised ? 'ai.answered.local' : 'ai.answered.public', { label: r.label, model: r.model, s: secs(r.ms) });
  }
  LL.aiMetaText = metaText;

  // ------------------------------------------------------------------ command register (picked up by the command palette)
  LL.commands = LL.commands || [];
  LL.registerCommand = LL.registerCommand || function (cmd) {
    LL.commands = LL.commands.filter(c => c.id !== cmd.id).concat([cmd]);
  };
  LL.registerCommand({ id: 'ai.ask', label: 'ask.command', keywords: 'ask.command.keywords', available: () => ready('ask'), run: () => LL.openAsk() });

  // ------------------------------------------------------------------ "Ask your network": button in the top bar
  LL.aiAskButton = function () {
    if (!ready('ask')) return null;
    const b = h('button', { class: 'ask-pill', type: 'button', title: t('ask.open'), 'aria-label': t('ask.open'), onclick: () => LL.openAsk() },
      h('span', { class: 'ask-pill__icon', 'aria-hidden': 'true' }), h('span', { class: 'ask-pill__label' }, t('ask.button')));
    b.firstChild.innerHTML = SPARK;
    return b;
  };

  // conversation of this browser tab (never stored on the server)
  function loadTurns() { try { const v = JSON.parse(sessionStorage.getItem(store()) || '[]'); return Array.isArray(v) ? v.slice(-MAX_TURNS) : []; } catch (e) { return []; } }
  function saveTurns(turns) { try { sessionStorage.setItem(store(), JSON.stringify(turns.slice(-MAX_TURNS))); } catch (e) { /* private mode */ } }

  let panel = null;
  LL.openAsk = function (question) {
    if (panel) { panel.focus(question); return; }
    panel = askPanel();
    document.body.appendChild(panel.el);
    requestAnimationFrame(() => panel.el.classList.add('is-open'));
    panel.focus(question);
  };
  LL.closeAsk = function () { if (panel) panel.close(); };
  window.addEventListener('hashchange', () => { if (panel && window.matchMedia('(max-width: 700px)').matches) panel.close(); });

  function askPanel() {
    let turns = loadTurns();
    let busy = false;
    const list = h('div', { class: 'ask__list', role: 'log', 'aria-live': 'polite', 'aria-label': t('ask.title') });
    const input = h('textarea', { class: 'input ask__input', rows: '2', maxlength: '500', placeholder: t('ask.placeholder'), 'aria-label': t('ask.placeholder') });
    const send = h('button', { class: 'btn btn--primary ask__send', type: 'submit', 'aria-label': t('ask.send') }, t('ask.send'));
    const previewBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: showPreview }, t('ai.preview'));
    const clearBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => { turns = []; saveTurns(turns); draw(); input.focus(); } }, t('ask.clear'));
    const previewBox = h('div', { class: 'ai-preview ask__preview', hidden: true });
    const close = h('button', { class: 'icon-btn ask__close', type: 'button', 'aria-label': t('ask.close'), title: t('ask.close'), onclick: () => api_.close() }, '✕');
    const form = h('form', { class: 'ask__form', onsubmit: ev => { ev.preventDefault(); ask(input.value); } }, input, send);
    input.addEventListener('keydown', ev => { if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); ask(input.value); } });
    const icon = h('span', { class: 'ask__icon', 'aria-hidden': 'true' });
    icon.innerHTML = SPARK;
    const el = h('aside', { class: 'ask', role: 'dialog', 'aria-modal': 'false', 'aria-label': t('ask.title') },
      h('header', { class: 'ask__head' }, icon, h('div', { class: 'ask__titles' }, h('h2', {}, t('ask.title')), h('p', { class: 'muted' }, t('ask.subtitle'))), close),
      list, previewBox,
      h('footer', { class: 'ask__foot' }, form, h('div', { class: 'ask__tools' }, previewBtn, clearBtn, h('span', { class: 'muted ask__never' }, t('ask.read_only')))));
    el.addEventListener('keydown', ev => { if (ev.key === 'Escape') api_.close(); });

    function suggestions() {
      return h('div', { class: 'ask__empty' }, h('p', { class: 'muted' }, t('ask.empty')),
        h('div', { class: 'ask__chips' }, ['ask.example.offline', 'ask.example.unknown', 'ask.example.internet', 'ask.example.unusual'].map(k =>
          h('button', { class: 'chip-btn', type: 'button', onclick: () => ask(t(k)) }, t(k)))));
    }
    function answerEl(turn) {
      const links = turn.links || [];
      const byRef = {};
      links.forEach(l => { byRef[l.ref] = l; });
      const paras = String(turn.text || '').split(/\n{2,}/).map(p => {
        const out = h('p', {});
        let last = 0;
        const rx = /\[(D\d{1,3}|view:[a-z]{3,12})\]/g;
        let m;
        while ((m = rx.exec(p))) {
          out.append(p.slice(last, m.index));
          const l = byRef[m[1]];
          if (l) out.append(refLink(l, true));
          last = m.index + m[0].length;
        }
        out.append(p.slice(last));
        return out;
      });
      const chips = links.length ? h('div', { class: 'ask__links' }, links.map(l => refLink(l, false))) : null;
      return h('div', { class: 'ask__msg ask__msg--ai' }, h('div', { class: 'ask__bubble' }, paras, chips), h('div', { class: 'muted ask__meta' }, turn.meta || ''));
    }
    function refLink(l, inline) {
      const label = l.kind === 'device' ? (l.title || t('kind.unknown')) : t('ask.view.' + l.id);
      return h('a', { class: inline ? 'ask__ref' : 'ask__chip', href: l.href, title: t(l.kind === 'device' ? 'ask.open_device' : 'ask.open_view', { name: label }),
        'aria-label': t(l.kind === 'device' ? 'ask.open_device' : 'ask.open_view', { name: label }) }, inline ? '↗' : label);
    }
    function draw(pending) {
      const items = turns.map(turn => turn.role === 'user'
        ? h('div', { class: 'ask__msg ask__msg--me' }, h('div', { class: 'ask__bubble' }, turn.text))
        : turn.error ? h('div', { class: 'ask__msg ask__msg--ai' }, h('div', { class: 'msg msg--error' }, t(turn.error)))
          : answerEl(turn));
      if (pending) items.push(pending);
      list.replaceChildren(...(items.length ? items : [suggestions()]));
      list.scrollTop = list.scrollHeight;
    }
    function history() {
      return turns.filter(x => !x.error).slice(-6).map(x => ({ role: x.role, text: String(x.text || '').slice(0, 1200) }));
    }
    async function ask(q) {
      q = String(q || '').trim();
      if (busy) return;
      if (!q) { input.focus(); return; }              // an empty Send leads into the field (a tap that does nothing looks broken)
      busy = true; send.disabled = true; previewBox.hidden = true;
      const hist = history();
      turns.push({ role: 'user', text: q.slice(0, 500) });
      input.value = '';
      const status = h('div', { class: 'msg msg--info ask__status' }, t('ai.job.queued'));
      draw(h('div', { class: 'ask__msg ask__msg--ai' }, status));
      try {
        const job = await api('/api/ai/ask', { method: 'POST', body: { question: q, history: hist, lang: LL.lang } });
        const j = await waitJob(job.id, x => { status.textContent = jobText(x); });
        if (j.state === 'done') turns.push({ role: 'assistant', text: j.result.text, links: j.result.links || [], meta: metaText(j.result) });
        else turns.push({ role: 'assistant', error: j.error || 'ai.err.http' });
      } catch (e) { turns.push({ role: 'assistant', error: e.key && LL.strings[e.key] ? e.key : 'error.generic' }); }
      saveTurns(turns);
      busy = false; send.disabled = false;
      draw();
      input.focus();
    }
    async function showPreview() {
      if (!previewBox.hidden) { previewBox.hidden = true; return; }
      const q = input.value.trim() || t('ask.example.offline');
      try { LL.aiDrawPreview(previewBox, await api('/api/ai/ask', { method: 'POST', body: { question: q, history: history(), lang: LL.lang, preview: true } })); }
      catch (e) { previewBox.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); }
      previewBox.prepend(h('div', { class: 'ask__preview-head' }, h('b', {}, t('ask.preview_title')), h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('ask.close'), onclick: () => { previewBox.hidden = true; } }, '✕')));
      previewBox.hidden = false;
    }
    const api_ = {
      el,
      focus(q) { if (q) input.value = q; setTimeout(() => input.focus(), 30); },
      close() { el.classList.remove('is-open'); setTimeout(() => el.remove(), 180); panel = null; }
    };
    draw();
    return api_;
  }

  // ------------------------------------------------------------------ nightly report: "In plain words"
  LL.aiPlainBlock = function (rep, onChange) {
    if (rep.preview) return null;
    const may = ready('report') && LL.can('operate');
    if (!rep.plain && !may) return null;            // without AI the paragraph is simply missing
    const box = h('section', { class: 'rp-plain' + (rep.plain ? '' : ' rp-plain--empty'), 'aria-label': t('report.plain.title') });
    const msg = h('div', { class: 'msg', hidden: true });
    const make = may ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
      make.disabled = true; msg.hidden = false; msg.className = 'msg msg--info'; msg.textContent = t('ai.job.queued');
      try {
        const job = await api('/api/ai/report', { method: 'POST', body: { day: rep.day, lang: LL.lang } });
        const j = await waitJob(job.id, x => { msg.textContent = jobText(x); });
        if (j.state === 'done') { rep.plain = j.result; if (onChange) onChange(rep); else box.replaceWith(LL.aiPlainBlock(rep)); return; }
        msg.className = 'msg msg--error'; msg.textContent = t(j.error || 'ai.err.http');
      } catch (e) { msg.className = 'msg msg--error'; msg.textContent = LL.errText(e); }
      make.disabled = false;
    } }, t(rep.plain ? 'report.plain.again' : 'report.plain.make')) : null;
    const icon = h('span', { class: 'rp-plain__icon', 'aria-hidden': 'true' });
    icon.innerHTML = SPARK;
    const head = h('div', { class: 'rp-plain__head' }, icon, h('b', {}, t('report.plain.title')), h('span', { class: 'chip chip--muted' }, t('report.plain.ai')), make);
    if (rep.plain) {
      box.append(head, h('p', { class: 'rp-plain__text' }, rep.plain.text),
        h('p', { class: 'muted rp-plain__meta' }, t('report.plain.truth') + ' · ' + metaText(rep.plain)), msg);
    } else {
      box.append(head, h('p', { class: 'muted rp-plain__text' }, t('report.plain.none')), msg);
    }
    return box;
  };

  // ------------------------------------------------------------------ device: identification help
  LL.aiHintSection = function (dev) {
    if (dev.is_self) return null;
    const hint = dev.ai_hint;
    const may = LL.can('operate');
    const canAsk = ready('identify') && may && !hint && (dev.certainty !== 'sure' || dev.kind === 'unknown');
    if (!hint && !canAsk) return null;
    const box = h('section', { class: 'detail__sec ai-hint' + (hint ? ' ai-hint--' + hint.state : '') }, h('h3', {}, t('hint.title')));
    const msg = h('div', { class: 'msg', hidden: true });
    async function decide(action) {
      try { await api('/api/ai/hints/' + encodeURIComponent(dev.id), { method: 'POST', body: { action } }); LL.toast(t('hint.saved.' + action), 'ok'); LL.route(); }
      catch (e) { msg.hidden = false; msg.className = 'msg msg--error'; msg.textContent = LL.errText(e); }
    }
    if (!hint) {
      const btn = h('button', { class: 'btn btn--ghost btn--sm ai-explain', type: 'button', onclick: async () => {
        btn.disabled = true; msg.hidden = false; msg.className = 'msg msg--info'; msg.textContent = t('ai.job.queued');
        try {
          const job = await api('/api/ai/identify', { method: 'POST', body: { id: dev.id, lang: LL.lang } });
          const j = await waitJob(job.id, x => { msg.textContent = jobText(x); });
          if (j.state === 'done') { LL.route(); return; }
          msg.className = 'msg msg--error'; msg.textContent = t(j.error || 'ai.err.http');
        } catch (e) { msg.className = 'msg msg--error'; msg.textContent = LL.errText(e); }
        btn.disabled = false;
      } }, h('span', { class: 'ai-explain__icon', 'aria-hidden': 'true' }, '✦'), h('span', {}, t('hint.ask')));
      box.append(h('p', { class: 'muted' }, t('hint.ask_text')), h('div', { class: 'btn-row' }, btn), msg);
      return box;
    }
    const kind = h('span', { class: 'ai-hint__kind' }, LL.kindIcon ? LL.kindIcon(hint.kind) : null, h('b', {}, t('kind.' + hint.kind)), hint.what ? h('span', { class: 'muted' }, ' · ' + hint.what) : null);
    box.append(...[h('div', { class: 'ai-hint__main' }, h('span', { class: 'chip chip--' + (hint.state === 'accepted' ? 'ok' : 'warn') }, t('hint.state.' + hint.state)), kind),
      hint.reason ? h('p', { class: 'ai-hint__reason' }, hint.reason) : null,
      h('p', { class: 'muted ai-hint__meta' }, t(hint.state === 'accepted' ? 'hint.accepted_text' : 'hint.open_text'))].filter(Boolean));
    if (may) {
      box.append(h('div', { class: 'btn-row' }, hint.state === 'open'
        ? [hint.kind !== 'unknown' ? h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => decide('accept') }, t('hint.accept')) : null,
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => decide('dismiss') }, t('hint.dismiss'))]
        : h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => decide('forget') }, t('hint.forget'))));
    }
    box.append(msg);
    return box;
  };

  // ------------------------------------------------------------------ firewall: suggestions as a draft proposal
  LL.aiFirewallButton = function (view, currentMoves, onTake) {
    if (!ready('firewall') || !LL.can('operate') || !view || view.mode === 'live' || !view.state) return null;
    return h('button', { class: 'btn btn--ghost btn--sm ai-explain', type: 'button', onclick: () => openFirewall(currentMoves, onTake) },
      h('span', { class: 'ai-explain__icon', 'aria-hidden': 'true' }, '✦'), h('span', { class: 'ai-explain__label' }, t('fwai.button')));
  };

  async function openFirewall(currentMoves, onTake) {
    const status = h('div', { class: 'msg msg--info' }, t('ai.job.queued'));
    const body = h('div', { class: 'stack fwai' }, h('p', { class: 'muted' }, t('fwai.text')), status);
    const take = h('button', { class: 'btn btn--primary', type: 'button', disabled: true }, t('fwai.take'));
    const previewBox = h('div', { class: 'ai-preview', hidden: true });
    const previewBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: async () => {
      if (!previewBox.hidden) { previewBox.hidden = true; return; }
      try { LL.aiDrawPreview(previewBox, await api('/api/ai/firewall', { method: 'POST', body: { lang: LL.lang, preview: true } })); previewBox.hidden = false; }
      catch (e) { previewBox.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); previewBox.hidden = false; }
    } }, t('ai.preview'));
    body.append(previewBox);
    const dlg = LL.dialog(t('fwai.title'), body, h('div', { class: 'btn-row fwai__foot' }, previewBtn, h('span', { class: 'muted ai-never' }, t('fwai.never')), take));
    dlg.el.classList.add('modal--ai');
    try {
      const job = await api('/api/ai/firewall', { method: 'POST', body: { lang: LL.lang } });
      const j = await waitJob(job.id, x => { status.textContent = jobText(x); });
      if (j.state !== 'done') { status.className = 'msg msg--error'; status.textContent = t(j.error || 'ai.err.http'); return; }
      const r = j.result;
      status.remove();
      if (!r.moves.length) { body.insertBefore(h('div', { class: 'msg msg--ok' }, r.summary || t('fwai.none')), previewBox); body.insertBefore(h('p', { class: 'muted fwai__meta' }, metaText(r)), previewBox); return; }
      const boxes = r.moves.map(m => {
        const cb = h('input', { type: 'checkbox', checked: true });
        return [m, cb, h('label', { class: 'fwai__row' }, cb, h('span', { class: 'fwai__move' }, h('b', {}, m.device_title), h('span', { class: 'muted' }, ' → '), h('b', {}, m.network_name),
          m.reason ? h('span', { class: 'fwai__reason muted' }, m.reason) : null))];
      });
      body.insertBefore(h('div', { class: 'stack' }, r.summary ? h('p', {}, r.summary) : null, h('div', { class: 'fwai__list' }, boxes.map(b => b[2])),
        h('p', { class: 'muted fwai__meta' }, metaText(r))), previewBox);
      take.disabled = false;
      take.onclick = async () => {
        const moves = Object.assign({}, currentMoves || {});
        boxes.forEach(([m, cb]) => { if (cb.checked) moves[m.device] = m.network; });
        take.disabled = true;
        try { await onTake(moves); dlg.close(); LL.toast(t('fwai.taken'), 'ok'); }
        catch (e) { LL.toast(LL.errText(e), 'error'); take.disabled = false; }
      };
    } catch (e) { status.className = 'msg msg--error'; status.textContent = LL.errText(e); }
  }

  // ------------------------------------------------------------------ Settings > AI: what the AI may do
  LL.aiFeaturesCard = function (data, onSaved) {
    const f = (data.settings && data.settings.features) || {};
    const msg = h('div', { class: 'msg' });
    const boxes = ['report_plain', 'deliver_plain', 'nightly_identify'].map(k => {
      const cb = h('input', { type: 'checkbox', checked: !!f[k] });
      return [k, cb, h('label', { class: 'check ai-feature' }, cb, h('span', {}, h('span', { class: 'ai-feature__label' }, t('ai.feature.' + k)), h('span', { class: 'muted ai-feature__hint' }, t('ai.feature.' + k + '.hint'))))];
    });
    const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      const features = {}; boxes.forEach(([k, cb]) => { features[k] = cb.checked; });
      try { onSaved(await api('/api/ai', { method: 'PUT', body: { settings: { features } } }), t('settings.saved')); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('ai.save'));
    return h('section', { class: 'card settings-card ai-card', id: 'ai-features' }, h('h2', {}, t('ai.features.title')),
      h('p', { class: 'muted' }, t('ai.features.text')), h('div', { class: 'ai-allow' }, boxes.map(b => b[2])), msg, h('div', { class: 'btn-row' }, save));
  };
})();
