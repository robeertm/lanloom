/* Lanloom — command palette (S1): one box for everything. Ctrl/⌘ + K, "/" or the Search button in the top bar.

   Matches, as you type:
     views and commands   here in the page (js/fuzzy.js): every view, the settings pages, light/dark, colour theme, rate unit,
                          "Check for updates" ... — a feature adds its own entries with LL.registerCommand (js/app.js)
     devices, guests, services, automations, hub entities   on the server (GET /api/search, lanloom/palette.py), read only.
                          A comparison such as "> 25 °C" filters hub entities by their current value.
   Typos are forgiven, the last choices come first on an empty box ("Recent", kept in this browser only).
   A hit has actions (Open, Copy ...): → or the "⋯" button shows them. An action exists only when something registered it
   with LL.registerAction (js/app.js), so a view that is not there yet never offers one.
   A command with parse(query) -> argument (and parsedLabel(argument)) understands the typed text and is offered first with what it
   understood ("Rewind to 14:30", js/rewind.js); run(argument) gets it.
   Two-step commands ("Show failure chain …"): open(prefill, {pick: {label, kinds, run}}) asks for a part first - the box shows the
   label as a chip, lists only hits of the given groups and runs run(hit) with the chosen one.
   Keyboard: ↑ ↓ Home End move, Enter chooses, → actions, Esc back/close. Screen readers get a combobox with a listbox
   (aria-activedescendant) and a live result count; on a phone the palette is a full-screen sheet. Plain DOM, no dependencies. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const F = LL.Fuzzy;
  const recentKey = () => LL.siteKey('ll-recent');        // recent device hits belong to one site (M2)
  const MAX_RECENT = 8;
  const SHOW_TYPED = { cmd: 6, devices: 6, guests: 5, services: 6, automations: 5, entities: 8 };
  const GROUP_ORDER = ['cmd', 'devices', 'guests', 'services', 'automations', 'entities'];
  const HIT_KEYS = ['kind', 'id', 'title', 'ip', 'device', 'device_kind', 'device_title', 'comp', 'port', 'entity_id', 'host', 'guest_type', 'domain', 'area', 'unit'];

  const VALUE_RX = /(>=|<=|=>|=<|\u2265|\u2264|>|<|=)\s*-?\d+(?:[.,]\d+)?\s*[^\s\d]*/;   // a value comparison such as "> 25 °C" (same reading as the server)
  const text = v => (typeof v === 'function' ? v() : (v ? t(v) : ''));
  const isMac = /Mac/.test(navigator.platform || navigator.userAgent || '');
  const keyName = isMac ? '⌘K' : 'Ctrl K';

  // ------------------------------------------------------------------ icons (one line each, 16 px)
  const ICON = {
    cmd: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    view: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/><path d="M10.5 7h3.5v6.5"/>',
    device: '<rect x="3" y="5" width="18" height="11" rx="2"/><path d="M8 20h8M12 16v4"/>',
    guest: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
    service: '<path d="M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3"/>',
    automation: '<path d="M13 2.5L5 13.5h6l-1 8 8-11h-6z"/>',
    entity: '<circle cx="12" cy="12" r="3.2"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>',
    more: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    back: '<path d="M14 6l-6 6 6 6"/>'
  };
  function svg(name, size) {
    const el = h('span', { class: 'cmdk__icon', 'aria-hidden': 'true' });
    el.innerHTML = '<svg width="' + (size || 18) + '" height="' + (size || 18) + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + ICON[name] + '</svg>';
    return el;
  }

  // ------------------------------------------------------------------ recent choices (this browser only)
  function recents() {
    try { const v = JSON.parse(localStorage.getItem(recentKey()) || '[]'); return Array.isArray(v) ? v.slice(0, MAX_RECENT) : []; } catch (e) { return []; }
  }
  function remember(entry) {
    const key = e => e.k + ':' + (e.k === 'cmd' ? e.id : e.hit.kind + ':' + e.hit.id);
    const list = [entry].concat(recents().filter(e => e && e.k && key(e) !== key(entry))).slice(0, MAX_RECENT);
    try { localStorage.setItem(recentKey(), JSON.stringify(list)); } catch (e) { /* private mode */ }
  }
  const forget = () => { try { localStorage.removeItem(recentKey()); } catch (e) { /* ignore */ } };
  const slim = hit => { const o = {}; HIT_KEYS.forEach(k => { if (hit[k] != null) o[k] = hit[k]; }); return o; };

  // ------------------------------------------------------------------ copy
  async function copy(value) {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
      else {
        const ta = h('textarea', { 'aria-hidden': 'true', tabindex: '-1', style: 'position:fixed;opacity:0;top:0;left:0' }); ta.value = value;
        document.body.appendChild(ta); ta.select(); const ok = document.execCommand('copy'); ta.remove(); if (!ok) throw new Error('copy');
      }
      LL.toast(t('cmdk.copied', { value }), 'ok');
    } catch (e) { LL.toast(t('cmdk.copy_failed'), 'error'); }
  }

  // ------------------------------------------------------------------ built-in commands
  const VIEWS = [['map', 'nav.map'], ['network', 'nav.network'], ['paths', 'nav.paths'], ['devices', 'nav.devices'], ['firewall', 'nav.firewall'], ['reports', 'nav.reports']];
  VIEWS.forEach(([k, label], i) => LL.registerCommand({
    id: 'go.' + k, group: 'views', order: i, label: () => t('cmdk.go', { name: t(label) }), keywords: () => t('cmdk.kw.' + k), run: () => LL.go('#/' + k) }));
  // the Running view exists once a host agent or container source is paired
  [['running', 'nav.running', 4.4]].forEach(([k, label, order]) => LL.registerCommand({
    id: 'go.' + k, group: 'views', order, available: () => !!(LL.state && LL.state.agents > 0), label: () => t('cmdk.go', { name: t(label) }),
    keywords: () => t('cmdk.kw.' + k), run: () => LL.go('#/' + k) }));
  LL.registerCommand({ id: 'agents-pair', group: 'commands', order: 25, available: () => LL.can('admin'), label: 'cmdk.agents_pair', keywords: () => t('cmdk.kw.agents_pair'),
    run: () => LL.go('#/settings/agents?pair=1') });
  // containers of the open network map: unfold or fold them on every host at once
  [['containers-expand', 'cmdk.containers_expand', true, 26], ['containers-collapse', 'cmdk.containers_collapse', false, 27]].forEach(([id, label, on, order]) => LL.registerCommand({
    id, group: 'commands', order, available: () => (LL.foldHosts || []).length > 0 && LL.allUnfolded() !== on, label, keywords: () => t('cmdk.kw.containers'),
    run: () => LL.setAllUnfolded(on) }));
  // settings pages: [id, route part, label key, lowest role]
  const SETTINGS = [['settings', '', 'nav.settings', 'viewer'], ['account', '/account', 'settings.tab.account', 'viewer'], ['users', '/users', 'settings.tab.users', 'admin'],
    ['security', '/security', 'settings.tab.security', 'admin'], ['backup', '/backup', 'backup.tab', 'admin'], ['network', '/network', 'settings.tab.network', 'viewer'], ['agents', '/agents', 'agents.tab', 'viewer'], ['api', '/api', 'api.tab', 'viewer'], ['ai', '/ai', 'ai.tab', 'admin'],
    ['updates', '/updates', 'settings.updates', 'admin']];
  SETTINGS.forEach(([k, sub, label, role], i) => LL.registerCommand({
    id: 'go.settings.' + k, group: 'views', order: 20 + i, typedOnly: k !== 'settings', available: () => role === 'viewer' || LL.can(role === 'admin' ? 'admin' : 'operate'),
    label: () => (k === 'settings' ? t('cmdk.go', { name: t(label) }) : t('cmdk.go', { name: t('nav.settings') + ' › ' + t(label) })),
    keywords: () => t('cmdk.kw.' + k), run: () => LL.go('#/settings' + sub) }));
  LL.registerCommand({ id: 'go-report-settings', group: 'views', order: 30, typedOnly: true, available: () => LL.can('operate'), label: () => t('cmdk.go', { name: t('reports.settings') }),
    keywords: () => t('cmdk.kw.report_settings'), run: () => LL.go('#/settings/reports') });

  LL.registerCommand({ id: 'go.talk', group: 'views', order: 31, typedOnly: true, label: () => t('cmdk.go', { name: t('talk.chip') }),
    keywords: () => t('cmdk.kw.talk'), run: () => LL.go('#/map?talk=1') });
  LL.registerCommand({ id: 'go.network3d', group: 'views', order: 32, typedOnly: true, label: () => t('cmdk.go', { name: t('netview.3d_title') }),
    keywords: () => t('cmdk.kw.network3d'), run: () => LL.go('#/network?v=3d') });

  const cmdTheme = (id, mode, order) => LL.registerCommand({ id, group: 'commands', order, label: () => t('theme.' + mode), keywords: () => t('cmdk.kw.theme'),
    available: () => LL.theme() !== mode, run: () => { LL.setTheme(mode); LL.toast(t('theme.' + mode)); } });
  LL.registerCommand({ id: 'theme-toggle', group: 'commands', order: 0, label: 'theme.toggle', keywords: () => t('cmdk.kw.theme'),
    run: () => { const next = LL.effectiveTheme() === 'dark' ? 'light' : 'dark'; LL.setTheme(next); LL.toast(t('theme.' + next)); } });
  cmdTheme('theme-light', 'light', 1); cmdTheme('theme-dark', 'dark', 2); cmdTheme('theme-auto', 'auto', 3);
  LL.registerCommand({ id: 'unit-bits', group: 'commands', order: 10, label: 'cmdk.unit.bits', keywords: () => t('cmdk.kw.unit'),
    available: () => LL.can('admin') && LL.rateUnit !== 'bits', run: () => setUnit('bits') });
  LL.registerCommand({ id: 'unit-bytes', group: 'commands', order: 11, label: 'cmdk.unit.bytes', keywords: () => t('cmdk.kw.unit'),
    available: () => LL.can('admin') && LL.rateUnit !== 'bytes', run: () => setUnit('bytes') });
  async function setUnit(unit) {
    try { await api('/api/settings', { method: 'PUT', body: { rate_unit: unit } }); LL.rateUnit = unit; LL.toast(t('settings.saved'), 'ok'); LL.route(); }
    catch (e) { LL.toast(LL.errText(e), 'error'); }
  }
  LL.registerCommand({ id: 'update-check', group: 'commands', order: 20, label: 'cmdk.update', keywords: () => t('cmdk.kw.update'), available: () => LL.can('admin'),
    run: async () => {
      try {
        const s = await api('/api/update/check', { method: 'POST', body: {} });
        LL.toast(s.available ? t('update.available', { v: s.latest }) : (s.error ? t('update.check_failed') : t('update.current')), s.available ? 'ok' : 'info');
        if (s.available) LL.go('#/settings/updates');
      } catch (e) { LL.toast(LL.errText(e), 'error'); }
    } });
  LL.registerCommand({ id: 'whatsnew', group: 'commands', order: 19, label: 'cmdk.whatsnew', keywords: () => t('cmdk.kw.whatsnew'),
    available: () => !!(LL.state && LL.state.authenticated && LL.openWhatsNew), run: () => LL.openWhatsNew() });
  const holdCommand = (id, order, label, want) => LL.registerCommand({ id, group: 'commands', order, label, keywords: () => t('cmdk.kw.update'),
    available: () => LL.can('admin') && !!(LL.updateInfo && LL.updateInfo()) && !!LL.updateInfo().held === !want,
    run: async () => {
      try { await api('/api/update/settings', { method: 'PUT', body: { hold: want } }); LL.toast(t('settings.saved'), 'ok'); if (LL.checkUpdateNow) LL.checkUpdateNow(); LL.route(); }
      catch (e) { LL.toast(LL.errText(e), 'error'); }
    } });
  holdCommand('update-hold', 21, 'cmdk.hold', true); holdCommand('update-release', 22, 'cmdk.hold.release', false);
  LL.registerCommand({ id: 'recent-clear', group: 'commands', order: 30, label: 'cmdk.clear_recent', keywords: () => t('cmdk.kw.recent'), available: () => recents().length > 0,
    run: () => { forget(); LL.toast(t('cmdk.recent_cleared')); } });
  // one command per colour theme (the list of themes belongs to js/app.js, so it is read when the palette opens)
  const paletteCommands = () => (LL.PALETTES || []).map((p, i) => ({ id: 'palette-' + p, group: 'commands', order: 5 + i / 100, available: () => LL.palette() !== p,
    label: () => t('cmdk.colour', { name: LL.paletteName(p) }), keywords: () => t('cmdk.kw.theme'),
    run: () => { LL.setPalette(p); LL.toast(t('palette.changed', { name: LL.paletteName(p) })); } }));

  // ------------------------------------------------------------------ actions of a hit (a feature adds its own)
  const openDevice = (kind, order) => LL.registerAction({ id: 'open.' + kind, kinds: [kind], order, label: 'cmdk.act.open', available: hit => !!(kind === 'device' ? hit.id : hit.device),
    run: hit => LL.go('#/devices/' + encodeURIComponent(kind === 'device' ? hit.id : hit.device)) });
  openDevice('device', 0); openDevice('guest', 0); openDevice('service', 0);
  LL.registerAction({ id: 'open.hub', kinds: ['automation', 'entity'], order: 0, label: 'cmdk.act.open_hub', available: hit => !!hit.comp,
    run: hit => LL.go('#/map/' + encodeURIComponent(hit.comp)) });
  LL.registerAction({ id: 'copy.address', kinds: ['device', 'guest', 'service'], order: 10, label: 'cmdk.act.copy_ip', available: hit => !!hit.ip,
    run: hit => copy(hit.ip + (hit.kind === 'service' && hit.port ? ':' + hit.port : '')) });
  LL.registerAction({ id: 'copy.id', kinds: ['automation', 'entity'], order: 10, label: 'cmdk.act.copy_id', available: hit => !!hit.entity_id, run: hit => copy(hit.entity_id) });

  const actionsOf = hit => (LL.actions || []).filter(a => a.kinds.includes(hit.kind) && (!a.available || a.available(hit))).sort((a, b) => (a.order || 0) - (b.order || 0));

  // ------------------------------------------------------------------ what a row shows
  const kindLabel = hit => hit.kind === 'automation' ? t(hit.domain === 'script' ? 'cmdk.kind.script' : 'cmdk.kind.automation') : t('cmdk.kind.' + hit.kind);
  function hitTitle(hit) {
    if (hit.kind === 'device') return hit.self ? t('kind.self') : (hit.title || t('kind.' + (hit.device_kind || 'unknown')));
    return hit.title || hit.entity_id || hit.id;
  }
  function hitSub(hit) {
    const parts = [];
    if (hit.kind === 'device') parts.push(t('kind.' + (hit.device_kind || 'unknown')), hit.ip);
    else if (hit.kind === 'guest') parts.push(t('cmdk.kind.guest'), hit.host ? t('cmdk.on_host', { host: hit.host }) : null, hit.ip);
    else if (hit.kind === 'service') parts.push(hit.device_title || t('kind.' + (hit.device_kind || 'unknown')), hit.ip ? hit.ip + (hit.port ? ':' + hit.port : '') : null);
    else if (hit.kind === 'automation') parts.push(kindLabel(hit));
    else parts.push(hit.entity_id, hit.area);
    return parts.filter(Boolean).join(' · ');
  }
  function hitEnd(hit) {
    if (hit.kind === 'entity' && hit.state != null && hit.state !== '') return h('span', { class: 'cmdk__value' }, String(hit.state) + (hit.unit ? ' ' + hit.unit : ''));
    if (hit.kind === 'automation' && hit.state) return h('span', { class: 'chip chip--' + (hit.state === 'on' ? 'ok' : 'muted') }, String(hit.state));
    if (hit.status && (hit.kind === 'device' || hit.kind === 'service' || hit.kind === 'guest')) {
      const s = hit.status === 'running' ? 'up' : hit.status;
      return h('span', { class: 'status-dot status-dot--' + (['up', 'down', 'off'].includes(s) ? s : 'unknown'), title: t('board.status.' + (['up', 'down', 'off'].includes(s) ? s : 'unknown')) });
    }
    return null;
  }
  // the title with the typed words marked
  function marked(title, query) {
    const words = String(query || '').toLowerCase().split(/\s+/).filter(w => w.length > 1 && !/^[<>=≥≤]/.test(w));
    const low = title.toLowerCase(), hit = new Array(title.length).fill(false);
    words.forEach(w => { let at = low.indexOf(w); while (at >= 0) { for (let i = at; i < at + w.length; i++) hit[i] = true; at = low.indexOf(w, at + w.length); } });
    if (!hit.some(Boolean)) return title;
    const out = []; let i = 0;
    while (i < title.length) {
      let j = i; while (j < title.length && hit[j] === hit[i]) j++;
      out.push(hit[i] ? h('mark', {}, title.slice(i, j)) : title.slice(i, j)); i = j;
    }
    return out;
  }

  // ------------------------------------------------------------------ the palette
  let ui = null;

  function commandItems() {
    const all = (LL.commands || []).concat(paletteCommands());
    return all.filter(c => !c.available || c.available()).map((c, i) => ({ kind: 'cmd', cmd: c, title: text(c.label), extra: text(c.keywords), order: (c.group === 'views' ? 0 : 1000) + (c.order == null ? i : c.order) }));
  }

  function open(prefill, opts) {
    const pick = opts && opts.pick ? opts.pick : null;
    if (ui && pick) ui.close(true);
    else if (ui) { ui.input.focus(); if (prefill != null) { ui.input.value = prefill; ui.query(); } return; }
    document.querySelectorAll('.cmdk').forEach(e => e.remove());     // a palette still fading out (the first step of a two-step command) goes now
    const opener = document.activeElement;
    let rows = [], sel = 0, menu = null, seq = 0, timer = null, server = { q: null, groups: {}, value: null }, failed = false, busy = false;
    const input = h('input', { class: 'cmdk__input', type: 'text', role: 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': 'true', 'aria-controls': 'cmdk-list', autocomplete: 'off',
      autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false', enterkeyhint: 'go', placeholder: pick ? t('fail.pick.placeholder') : t('cmdk.placeholder'),
      'aria-label': pick ? text(pick.label) : t('cmdk.input_label') });
    const list = h('ul', { class: 'cmdk__list', id: 'cmdk-list', role: 'listbox', 'aria-label': t('cmdk.title') });
    const status = h('div', { class: 'sr-only', role: 'status', 'aria-live': 'polite' });
    const closeBtn = h('button', { class: 'cmdk__close', type: 'button', 'aria-label': t('cmdk.close'), onclick: () => close() }, h('span', { 'aria-hidden': 'true' }, 'Esc'), h('span', { class: 'cmdk__close-x', 'aria-hidden': 'true' }, '✕'));
    const head = h('div', { class: 'cmdk__head' }, svg('search', 20), pick ? h('span', { class: 'cmdk__pick' }, text(pick.label)) : null, input, closeBtn);
    const foot = h('div', { class: 'cmdk__foot', 'aria-hidden': 'true' },
      h('span', {}, h('kbd', {}, '↑'), h('kbd', {}, '↓'), ' ', t('cmdk.hint.move')), h('span', {}, h('kbd', {}, '↵'), ' ', t('cmdk.hint.choose')),
      h('span', {}, h('kbd', {}, '→'), ' ', t('cmdk.hint.actions')), h('span', {}, h('kbd', {}, 'Esc'), ' ', t('cmdk.hint.close')));
    const dialog = h('div', { class: 'cmdk__dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('cmdk.title') }, head, h('div', { class: 'cmdk__body' }, list), foot, status);
    const root = h('div', { class: 'cmdk' }, h('div', { class: 'cmdk__backdrop', onclick: () => close() }), dialog);
    document.body.appendChild(root);
    document.body.classList.add('cmdk-open');
    requestAnimationFrame(() => root.classList.add('is-open'));

    // ---- build the rows for the current query
    function build() {
      const q = input.value.trim();
      const out = [];
      const add = (gid, label, items, extra) => { if (items.length) out.push({ head: true, gid, label, extra }); items.forEach(x => out.push(Object.assign({ gid }, x))); };
      if (menu) return;
      if (pick) {
        (pick.kinds || []).forEach(g => add(g, t('cmdk.group.' + g), (server.groups[g] || []).slice(0, SHOW_TYPED[g]).map(hit => ({ kind: 'hit', hit }))));
        rows = out;
        return;
      }
      if (!q) {
        const rec = recents().map(e => (e.k === 'cmd' ? (() => { const c = commandItems().find(x => x.cmd.id === e.id); return c ? Object.assign({}, c, { recent: true }) : null; })()
          : { kind: 'hit', hit: e.hit, recent: true })).filter(Boolean);
        add('recent', t('cmdk.group.recent'), rec);
        const cmds = commandItems().filter(c => !c.cmd.typedOnly).sort((a, b) => a.order - b.order);
        add('views', t('cmdk.group.views'), cmds.filter(c => c.cmd.group === 'views'));
        add('commands', t('cmdk.group.commands'), cmds.filter(c => c.cmd.group !== 'views'));
      } else {
        const v = server.value;
        const local = F.rank(commandItems(), v ? '' : q, SHOW_TYPED.cmd).map(r => r.item);
        // commands that understand the typed text ("Rewind to 14:30") come first, with what they understood
        const parsed = v ? [] : commandItems().filter(c => c.cmd.parse).map(c => {
          const arg = c.cmd.parse(q);
          return arg ? Object.assign({}, c, { arg, title: c.cmd.parsedLabel ? c.cmd.parsedLabel(arg) : c.title }) : null;
        }).filter(Boolean);
        add('cmd', t('cmdk.group.cmd'), v ? [] : parsed.concat(local.filter(x => !parsed.some(p => p.cmd.id === x.cmd.id))));
        const cmp = v ? ((input.value.match(VALUE_RX) || [''])[0]).replace(/\s+/g, ' ') : '';
        GROUP_ORDER.slice(1).forEach(g => add(g, t('cmdk.group.' + g), (server.groups[g] || []).slice(0, SHOW_TYPED[g]).map(hit => ({ kind: 'hit', hit })), v && g === 'entities' ? cmp : null));
      }
      rows = out;
    }
    const selectable = () => rows.map((r, i) => (r.head ? -1 : i)).filter(i => i >= 0);

    // ---- draw
    function draw() {
      build();
      const q = input.value.trim();
      if (menu) { drawMenu(); return; }
      const idx = selectable();
      if (!idx.includes(sel)) sel = idx.length ? idx[0] : 0;
      const els = [];
      let gl = null;
      rows.forEach((r, i) => {
        if (r.head) {
          gl = { id: 'cmdk-g-' + r.gid, ul: h('ul', { role: 'group', 'aria-labelledby': 'cmdk-g-' + r.gid, class: 'cmdk__group' }) };
          els.push(h('li', { role: 'presentation', class: 'cmdk__section' }, h('div', { class: 'cmdk__label', id: gl.id }, r.label, r.extra ? h('span', { class: 'cmdk__extra' }, r.extra) : null), gl.ul));
          return;
        }
        gl.ul.appendChild(rowEl(r, i, q));
      });
      if (!idx.length && pick && !q) els.push(h('li', { role: 'presentation', class: 'cmdk__empty' }, t('fail.pick.hint')));
      else if (!idx.length) {
        els.push(h('li', { role: 'presentation', class: 'cmdk__empty' }, busy ? t('cmdk.searching') : failed ? t('cmdk.err') : t('cmdk.none', { q }),
          !busy && !failed ? h('span', { class: 'muted' }, t('cmdk.none.tip')) : null));
      }
      list.replaceChildren(...els);
      list.setAttribute('aria-busy', busy ? 'true' : 'false');
      mark();
      status.textContent = idx.length ? t('cmdk.results', { n: idx.length }) : (busy ? '' : t('cmdk.none', { q }));
    }
    function rowEl(r, i, q) {
      const hit = r.kind === 'hit' ? r.hit : null;
      const title = hit ? hitTitle(hit) : r.title;
      const more = hit && !pick && actionsOf(hit).length > 1 ? h('button', { class: 'cmdk__more', type: 'button', tabindex: '-1', 'aria-label': t('cmdk.actions_for', { name: title }), title: t('cmdk.more'),
        onclick: ev => { ev.stopPropagation(); sel = i; openMenu(hit); } }) : null;
      if (more) more.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' + ICON.more + '</svg>';
      return h('li', { role: 'option', id: 'cmdk-o' + i, class: 'cmdk__item' + (r.recent ? ' is-recent' : ''), 'aria-selected': 'false', dataset: { i: String(i) },
        onmousemove: () => { if (sel !== i) { sel = i; mark(); } }, onclick: () => choose(i) },
        svg(hit ? hit.kind : (r.cmd && r.cmd.group === 'views' ? 'view' : 'cmd')),
        h('span', { class: 'cmdk__main' }, h('span', { class: 'cmdk__title' }, marked(title, q)), hit ? h('span', { class: 'cmdk__sub' }, hitSub(hit)) : null),
        hit ? hitEnd(hit) : null, more);
    }
    function mark() {
      list.querySelectorAll('.cmdk__item').forEach(el => { const on = Number(el.dataset.i) === sel; el.classList.toggle('is-active', on); el.setAttribute('aria-selected', on ? 'true' : 'false'); });
      const el = list.querySelector('#cmdk-o' + sel);
      if (el) { input.setAttribute('aria-activedescendant', el.id); el.scrollIntoView({ block: 'nearest' }); } else input.removeAttribute('aria-activedescendant');
    }

    // ---- action menu of one hit
    function openMenu(hit) {
      const acts = actionsOf(hit);
      if (!acts.length) return;
      menu = { hit, acts, sel: 0 };
      drawMenu();
    }
    function drawMenu() {
      const title = hitTitle(menu.hit);
      const items = menu.acts.map((a, i) => h('li', { role: 'option', id: 'cmdk-m' + i, class: 'cmdk__item cmdk__item--action' + (i === menu.sel ? ' is-active' : ''), 'aria-selected': i === menu.sel ? 'true' : 'false',
        onmousemove: () => { if (menu.sel !== i) { menu.sel = i; drawMenu(); } }, onclick: () => runAction(a) }, h('span', { class: 'cmdk__main' }, h('span', { class: 'cmdk__title' }, text(a.label)))));
      const back = h('button', { class: 'cmdk__back', type: 'button', onclick: () => closeMenu() }, svg('back', 16), h('span', {}, t('cmdk.back')));
      list.replaceChildren(h('li', { role: 'presentation', class: 'cmdk__section' }, h('div', { class: 'cmdk__menuhead' }, back, h('span', { class: 'cmdk__menutitle' }, t('cmdk.actions_for', { name: title }))),
        h('ul', { role: 'group', 'aria-label': t('cmdk.actions_for', { name: title }), class: 'cmdk__group' }, items)));
      input.setAttribute('aria-activedescendant', 'cmdk-m' + menu.sel);
      status.textContent = t('cmdk.actions_for', { name: title });
    }
    function closeMenu() { menu = null; draw(); input.focus(); }
    function runAction(a) { const hit = menu ? menu.hit : null; if (!hit) return; remember({ k: 'hit', hit: slim(hit) }); close(true); a.run(hit); }

    // ---- choose
    function choose(i) {
      const r = rows[i]; if (!r || r.head) return;
      if (r.kind === 'cmd' && r.arg) { close(true); r.cmd.run(r.arg); return; }
      if (r.kind === 'cmd') { remember({ k: 'cmd', id: r.cmd.id }); close(true); r.cmd.run(); return; }
      if (pick) { close(true); pick.run(r.hit); return; }
      const acts = actionsOf(r.hit);
      if (!acts.length) { LL.toast(t('cmdk.no_action'), 'info'); return; }
      remember({ k: 'hit', hit: slim(r.hit) }); close(true); acts[0].run(r.hit);
    }

    // ---- server search (debounced, the newest answer wins)
    function query() {
      const q = input.value.trim();
      clearTimeout(timer);
      if (menu) menu = null;
      if (!q) { server = { q: null, groups: {}, value: null }; busy = false; failed = false; seq++; sel = 0; draw(); return; }
      busy = true; draw();
      const mine = ++seq;
      timer = setTimeout(async () => {
        try {
          const r = await api('/api/search?limit=8&q=' + encodeURIComponent(q.slice(0, 120)));
          if (mine !== seq) return;
          server = { q, groups: r.groups || {}, value: r.value || null }; failed = false;
        } catch (e) { if (mine !== seq) return; server = { q, groups: {}, value: null }; failed = true; }
        busy = false; sel = 0; draw();
      }, 90);
    }

    // ---- keys
    function onKey(ev) {
      if (ev.key === 'Escape') { ev.preventDefault(); if (menu) closeMenu(); else close(); return; }
      if (ev.key === 'Tab') {                                       // keep focus inside the dialog: the box and the close button
        ev.preventDefault(); (document.activeElement === input ? closeBtn : input).focus(); return;
      }
      if (ev.target === closeBtn) return;
      if (menu) {
        if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); menu.sel = (menu.sel + (ev.key === 'ArrowDown' ? 1 : menu.acts.length - 1)) % menu.acts.length; drawMenu(); }
        else if (ev.key === 'Enter') { ev.preventDefault(); runAction(menu.acts[menu.sel]); }
        else if (ev.key === 'ArrowLeft') { ev.preventDefault(); closeMenu(); }
        return;
      }
      const idx = selectable();
      const pos = idx.indexOf(sel);
      if (ev.key === 'ArrowDown') { ev.preventDefault(); if (idx.length) { sel = idx[(pos + 1) % idx.length]; mark(); } }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); if (idx.length) { sel = idx[(pos - 1 + idx.length) % idx.length]; mark(); } }
      else if (ev.key === 'Home' && !input.value) { ev.preventDefault(); if (idx.length) { sel = idx[0]; mark(); } }
      else if (ev.key === 'End' && !input.value) { ev.preventDefault(); if (idx.length) { sel = idx[idx.length - 1]; mark(); } }
      else if (ev.key === 'Enter') { ev.preventDefault(); choose(sel); }
      else if (ev.key === 'ArrowRight' && input.selectionStart === input.value.length) {
        const r = rows[sel];
        if (r && r.kind === 'hit' && !pick && actionsOf(r.hit).length > 1) { ev.preventDefault(); openMenu(r.hit); }
      }
    }
    root.addEventListener('keydown', onKey);
    input.addEventListener('input', query);

    function close(keepFocus) {
      if (!ui) return;
      clearTimeout(timer); seq++;
      root.classList.remove('is-open');
      document.body.classList.remove('cmdk-open');
      const gone = root; ui = null;
      setTimeout(() => gone.remove(), 140);
      if (!keepFocus && opener && opener.focus && document.contains(opener)) opener.focus();
    }
    ui = { input, query, close, root };
    if (prefill != null) input.value = prefill;
    draw();
    if (input.value) query();
    input.focus();
  }

  LL.openPalette = open;
  LL.closePalette = () => { if (ui) ui.close(); };

  // ------------------------------------------------------------------ the button in the top bar and the keys
  LL.paletteButton = function () {
    const b = h('button', { class: 'cmdk-pill', type: 'button', 'aria-haspopup': 'dialog', title: t('cmdk.button.hint', { keys: keyName }), 'aria-label': t('cmdk.button') + ' ' + keyName,
      onclick: () => open() }, h('span', { class: 'cmdk-pill__icon', 'aria-hidden': 'true' }), h('span', { class: 'cmdk-pill__label' }, t('cmdk.button')), ' ', h('kbd', { class: 'cmdk-pill__keys', 'aria-hidden': 'true' }, keyName));
    b.firstChild.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + ICON.search + '</svg>';
    return b;
  };
  const typing = el => el && (/^(input|textarea|select)$/i.test(el.tagName) || el.isContentEditable);
  document.addEventListener('keydown', ev => {
    if (!document.querySelector('.topbar')) return;                       // only inside the signed-in frame, never on sign-in or setup
    if ((ev.key === 'k' || ev.key === 'K') && (ev.metaKey || ev.ctrlKey) && !ev.altKey) { ev.preventDefault(); if (ui) ui.close(); else open(); return; }
    if (ev.key === '/' && !ev.metaKey && !ev.ctrlKey && !ev.altKey && !ui && !typing(ev.target)) { ev.preventDefault(); open(); }
  });
  // tests and other scripts reach the pure parts here
  LL.paletteInternals = { actionsOf, hitTitle, hitSub, recents, remember, forget, commandItems };
})();
