/* Lanloom — Settings > Host agents: pair a host agent (one-time code, snippets to copy), add a container source, rename and
   remove entries. The list is visible to everyone who may open the settings; the buttons follow the role (the server
   decides the same per route). The snippets are built here from the address of this page and the image the server names. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const card = (title, kids) => h('section', { class: 'card settings-card' }, h('h2', {}, title), kids);

  // copy to the clipboard (secure pages) with a fallback for plain http; the toast says what happened
  LL.copyText = LL.copyText || async function (value) {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
      else {
        const ta = h('textarea', { 'aria-hidden': 'true', tabindex: '-1', style: 'position:fixed;opacity:0;top:0;left:0' });
        ta.value = value; document.body.appendChild(ta); ta.select();
        const ok = document.execCommand('copy'); ta.remove(); if (!ok) throw new Error('copy');
      }
      LL.toast(t('agents.copied'), 'ok');
    } catch (e) { LL.toast(t('cmdk.copy_failed'), 'error'); }
  };
  const copyBtn = (value, small) => h('button', { class: 'btn btn--ghost' + (small === false ? '' : ' btn--sm'), type: 'button', onclick: () => LL.copyText(value) }, t('access.copy'));
  const serverOrigin = () => (location.origin + LL.base).replace(/\/+$/, '');

  // ------------------------------------------------------------------ the snippets (kept in step with docs/HOST-AGENT.md)
  function composeSnippet(image, origin, code) {
    return ['services:', '  lanloom-agent:', '    image: ' + image, '    container_name: lanloom-agent', '    command: ["python", "-m", "lanloom.agent", "run"]',
      '    restart: unless-stopped', '    network_mode: host', '    pid: host', '    user: "0:0"', '    read_only: true', '    cap_drop: [ALL]',
      '    security_opt: ["no-new-privileges:true"]', '    environment:', '      LANLOOM_AGENT_SERVER: "' + origin + '"', '      LANLOOM_AGENT_CODE: "' + code + '"',
      '    volumes:', '      - lanloom-agent:/var/lib/lanloom-agent', '      - /:/host:ro', '      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro',
      '      - /var/run/docker.sock:/var/run/docker.sock:ro', 'volumes:', '  lanloom-agent:'].join('\n');
  }
  function runSnippet(image, origin, code) {
    return 'docker run -d --name lanloom-agent --restart unless-stopped --network host --pid host --user 0:0 --read-only --cap-drop ALL ' +
      '--security-opt no-new-privileges:true -e LANLOOM_AGENT_SERVER=' + origin + ' -e LANLOOM_AGENT_CODE=' + code +
      ' -v lanloom-agent:/var/lib/lanloom-agent -v /:/host:ro -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro' +
      ' -v /var/run/docker.sock:/var/run/docker.sock:ro ' + image + ' python -m lanloom.agent run';
  }
  const snippetBox = (title, text) => h('div', { class: 'ag-snip' }, h('div', { class: 'ag-snip__head' }, h('strong', {}, title), copyBtn(text)), h('pre', { class: 'ag-pre mono', tabindex: '0' }, text));

  // ------------------------------------------------------------------ dialogs
  function confirmRemove(entry) {
    return new Promise(resolve => {
      let answered = false;
      const yes = h('button', { class: 'btn btn--danger', type: 'button', onclick: () => { answered = true; d.close(); resolve(true); } }, t('agents.remove.yes'));
      const no = h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => d.close() }, t('access.cancel'));
      const d = LL.dialog(t('agents.remove.title', { name: entry.name }), h('p', {}, t(entry.kind === 'proxy' ? 'agents.remove.text_source' : 'agents.remove.text_agent')), h('div', { class: 'btn-row' }, no, yes));
      const obs = new MutationObserver(() => { if (!d.el.isConnected) { obs.disconnect(); if (!answered) resolve(false); } });
      obs.observe(document.body, { childList: true });
      setTimeout(() => no.focus(), 30);
    });
  }

  function pairDialog(onDone) {
    const name = h('input', { class: 'input', type: 'text', maxlength: '60', placeholder: t('agents.pair.name_ph'), autocomplete: 'off' });
    const msg = h('div', { class: 'msg', role: 'status' });
    const body = h('div', { class: 'stack ag-pair' });
    const create = h('button', { class: 'btn btn--primary', type: 'button' }, t('agents.pair.create'));
    let tick = null, poll = null;
    const dlg = LL.dialog(t('agents.pair.title'), body);
    const stop = () => { clearInterval(tick); clearInterval(poll); };
    const watch = new MutationObserver(() => { if (!dlg.el.isConnected) { stop(); watch.disconnect(); if (onDone) onDone(); } });
    watch.observe(document.body, { childList: true });

    function start() {
      body.replaceChildren(h('p', { class: 'muted' }, t('agents.pair.lead')), LL.field(t('agents.pair.name'), name, t('agents.pair.name_hint')), msg, h('div', { class: 'btn-row' }, create));
      setTimeout(() => name.focus(), 30);
    }
    create.onclick = async () => {
      create.disabled = true; LL.msg(msg, '');
      let known;
      try { known = new Set((await api('/api/agents')).agents.map(a => a.id)); } catch (e) { known = new Set(); }
      let r;
      try { r = await api('/api/agents/invite', { method: 'POST', body: { name: name.value.trim() } }); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); create.disabled = false; return; }
      show(r, known);
    };
    function show(r, known) {
      const origin = serverOrigin();
      const left = h('span', { class: 'ag-code__left' });
      const status = h('p', { class: 'ag-wait muted', role: 'status' });
      const codeBox = h('div', { class: 'ag-code' }, h('code', { class: 'ag-code__value mono', 'aria-label': t('agents.pair.code') }, r.code),
        h('div', { class: 'ag-code__row' }, left, copyBtn(r.code)));
      body.replaceChildren(
        h('p', {}, t('agents.pair.steps')), codeBox, status,
        snippetBox(t('agents.snippet.compose'), composeSnippet(r.image, origin, r.code)),
        snippetBox(t('agents.snippet.run'), runSnippet(r.image, origin, r.code)),
        h('p', { class: 'muted fineprint' }, t('agents.snippet.optional')),
        h('p', { class: 'muted fineprint' }, t('agents.pair.handbook')));
      const show1 = () => {
        const s = Math.max(0, Math.round(r.expires - Date.now() / 1000));
        if (s <= 0) { left.textContent = t('agents.pair.expired'); codeBox.classList.add('is-expired'); clearInterval(tick); return; }
        left.textContent = t('agents.pair.left', { time: Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') });
      };
      show1(); tick = setInterval(show1, 1000);
      status.textContent = t('agents.pair.waiting');
      poll = setInterval(async () => {
        if (!dlg.el.isConnected) { stop(); return; }
        try {
          const now = (await api('/api/agents')).agents.filter(a => a.kind === 'agent' && !known.has(a.id));
          if (now.length) {
            stop(); codeBox.classList.add('is-used'); status.className = 'ag-wait msg msg--ok';
            status.textContent = t('agents.pair.done', { name: now[0].name });
            if (onDone) onDone();
          }
        } catch (e) { /* the next tick tries again */ }
      }, 3000);
    }
    start();
    return dlg;
  }

  function sourceDialog(onDone) {
    const name = h('input', { class: 'input', type: 'text', maxlength: '60', autocomplete: 'off' });
    const url = h('input', { class: 'input mono', type: 'text', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'none' });
    const out = h('div', { class: 'msg', role: 'status' });
    const test = h('button', { class: 'btn btn--ghost', type: 'button' }, t('agents.source.test'));
    const save = h('button', { class: 'btn btn--primary', type: 'button' }, t('agents.source.save'));
    test.onclick = async () => {
      test.disabled = true; LL.msg(out, t('agents.source.testing'), 'info');
      try {
        const r = await api('/api/agents/proxy/test', { method: 'POST', body: { url: url.value.trim() } });
        if (r.ok) LL.msg(out, t('agents.source.test_ok', { runtime: r.runtime || '—', version: r.version || '—', n: r.containers }), 'ok');
        else LL.msg(out, t(r.error || 'agent.err.containers_unreachable'), 'error');
      } catch (e) { LL.msg(out, LL.errText(e), 'error'); }
      test.disabled = false;
    };
    save.onclick = async () => {
      save.disabled = true;
      try {
        await api('/api/agents/proxy', { method: 'POST', body: { name: name.value.trim(), url: url.value.trim() } });
        d.close(); LL.toast(t('settings.saved'), 'ok'); if (onDone) onDone();
      } catch (e) { LL.msg(out, LL.errText(e), 'error'); save.disabled = false; }
    };
    const d = LL.dialog(t('agents.source.title'), h('div', { class: 'stack' }, h('p', { class: 'muted' }, t('agents.source.lead')),
      LL.field(t('agents.source.name'), name), LL.field(t('agents.source.url'), url, t('agents.source.url_hint')), out),
    h('div', { class: 'btn-row' }, test, save));
    setTimeout(() => name.focus(), 30);
    return d;
  }

  // ------------------------------------------------------------------ the page
  LL.agentsPage = async function (pg) {
    const host = h('div', { class: 'stack ag-page' });
    pg.append(host);
    let data = await api('/api/agents');
    let alive = true;
    const admin = LL.can('admin'), operate = LL.can('operate');

    function entryRow(a) {
      const nameEl = h('span', { class: 'ag-name' });
      const title = h('div', { class: 'ag-title' }, nameEl, h('span', { class: 'chip' }, t('running.kind.' + a.kind)),
        a.online ? h('span', { class: 'chip chip--ok' }, t('running.online')) : h('span', { class: 'chip chip--danger' }, t('running.offline')));
      const showName = () => { nameEl.replaceChildren(h('strong', { title: a.name }, a.name)); };
      showName();
      const facts = [a.last_seen ? t('agents.last_seen', { when: LL.fmtDate(a.last_seen) }) : t('running.never'), a.version ? t('running.version', { v: a.version }) : null,
        a.host && a.host !== a.name ? a.host : null, (a.ips || []).length ? a.ips.slice(0, 2).join(', ') : null, a.os, a.url].filter(Boolean);
      const buttons = h('div', { class: 'ag-actions' });
      const edit = () => {
        const input = h('input', { class: 'input input--sm', type: 'text', value: a.name, maxlength: '60', 'aria-label': t('agents.rename') });
        const msg = h('span', { class: 'msg', role: 'status' });
        const ok = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: async () => {
          try { await api('/api/agents/' + encodeURIComponent(a.id), { method: 'PATCH', body: { name: input.value.trim() } }); LL.toast(t('settings.saved'), 'ok'); reload(); }
          catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
        } }, t('agents.rename.save'));
        const cancel = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => { showName(); buttons.replaceChildren(...normal()); } }, t('access.cancel'));
        input.addEventListener('keydown', ev => { if (ev.key === 'Enter') ok.click(); if (ev.key === 'Escape') cancel.click(); });
        nameEl.replaceChildren(input); buttons.replaceChildren(ok, cancel, msg); input.focus(); input.select();
      };
      const normal = () => [
        operate ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: edit }, t('agents.rename')) : null,
        admin ? h('button', { class: 'btn btn--ghost btn--sm btn--danger-text', type: 'button', onclick: async () => {
          if (!(await confirmRemove(a))) return;
          try { await api('/api/agents/' + encodeURIComponent(a.id), { method: 'DELETE' }); LL.toast(t('agents.removed'), 'ok'); reload(); }
          catch (e) { LL.toast(LL.errText(e), 'error'); }
        } }, t('agents.remove')) : null].filter(Boolean);
      buttons.replaceChildren(...normal());
      return h('li', { class: 'ag-row' }, h('div', { class: 'ag-main' }, title, h('div', { class: 'ag-facts muted' }, facts.map(f => h('span', { title: String(f) }, String(f)))),
        a.update ? h('div', { class: 'ag-update' }, h('span', { class: 'chip chip--warn' }, t('running.update.title', { v: a.update.version })), h('code', { class: 'ag-update__image mono', title: a.update.image }, a.update.image), copyBtn(a.update.image)) : null,
        a.error ? h('p', { class: 'msg msg--error ag-error' }, t(a.error)) : null), buttons);
    }

    function render() {
      const intro = card(t('agents.title'), [h('p', { class: 'muted' }, t('agents.lead')),
        h('ul', { class: 'ag-points' }, ['agents.point.meta', 'agents.point.noport', 'agents.point.image'].map(k => h('li', {}, t(k))))]);
      const buttons = admin ? h('div', { class: 'btn-row btn-row--wrap ag-add' },
        h('button', { class: 'btn btn--primary', type: 'button', onclick: () => pairDialog(reload) }, t('agents.pair.button')),
        h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => sourceDialog(reload) }, t('agents.source.button'))) : h('p', { class: 'muted fineprint' }, t('agents.admin_only'));
      const list = data.agents.length ? h('ul', { class: 'ag-list' }, data.agents.map(entryRow)) : h('p', { class: 'muted' }, t('agents.none'));
      host.replaceChildren(intro, card(t('agents.list.title'), [list, buttons, data.server_version ? h('p', { class: 'muted fineprint' }, t('agents.server_version', { v: data.server_version })) : null]));
    }
    async function reload() {
      if (!alive) return;
      try { data = await api('/api/agents'); } catch (e) { return; }
      render();
    }
    render();
    const timer = setInterval(() => { if (!host.isConnected) { alive = false; clearInterval(timer); return; } if (!document.hidden && !host.querySelector('input')) reload(); }, 15000);
    if (admin && LL.query && LL.query.get('pair')) { history.replaceState(null, '', LL.base + '#/settings/agents'); pairDialog(reload); }
    return { destroy: () => { alive = false; clearInterval(timer); } };
  };
})();
