/* Lanloom — several sites in one instance (M1): the site switcher in the header (remembered per browser), the
   "All sites" page with every released site at a glance, adding / renaming / removing sites and reading one site of a
   controller (administrators), the palette commands "Switch site", the overview card and the site picker of the
   user administration.

   The server decides which site a request belongs to and only among the sites released for the person
   (lanloom/sites.py); the browser only remembers the last one (cookie ll_site, localStorage ll-site). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const KEY = 'll-site', COOKIE = 'll_site';
  const EVERY_MS = 15000;
  const REACH = ['host', 'controller', 'agent', 'snmp', 'hub'];

  // ------------------------------------------------------------------ remembered site (per browser)
  function readCookie() {
    const m = document.cookie.match(/(?:^|;\s*)ll_site=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : null;
  }
  function writeCookie(id) {
    document.cookie = COOKIE + '=' + encodeURIComponent(id) + '; path=' + (LL.base || '/') + '; max-age=34560000; SameSite=Strict' +
      (location.protocol === 'https:' ? '; Secure' : '');
  }
  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function remember(id) {
    try { localStorage.setItem(KEY, id); } catch (e) { /* private mode: the cookie alone */ }
    LL.rememberTabSite(id);
    if (readCookie() !== id) writeCookie(id);
  }
  LL.rememberSite = remember;                       // app.js: after a resync or an unknown site (M1T), the cookie follows too
  (function syncAtLoad() {
    let own = null; try { own = sessionStorage.getItem('lanloom.site.tab'); } catch (e) { /* storage closed */ }
    const s = own || stored();                      // this tab's own site first (M2), else the last one used in this browser
    if (s && readCookie() !== s) writeCookie(s);    // before the first request
  })();

  LL.site = () => (LL.state && LL.state.site) || null;
  LL.siteName = s => (s && s.name) || (s && s.home ? t('site.home') : '') || t('site.unnamed');
  // parts that work on the home site only (firewall editor, AI assistant)
  LL.atHome = () => { const s = LL.site(); return !s || !s.id || s.home === true; };
  LL.switchSite = function (id, hash) {
    if (LL.switching()) return;
    LL.leaveSite();                       // running requests are cancelled, the live feed closes quietly: no late answer of the old site is drawn
    remember(id);
    sitesCache = null;
    LL.dropTabContext();                  // a card to open, a step, a focus of the site that is left: never carried into the next one (M1T)
    // the address decides site and view after the reload: a given target, else the view alone - never a part, filter or sheet
    // of the site that is left (an id there means another thing, or nothing, in the next site)
    history.replaceState(null, '', LL.base + (hash || LL.viewOnly(location.hash)));
    location.reload();                    // every view, the live feed and the caches start fresh with the new site
  };
  const withoutSite = LL.withoutSite;     // '#/devices/d-1?x=1&site=s-12345678' -> '#/devices/d-1?x=1' 
  // a link with ?site=<id> (M2): the page of that site is opened, only when the person may see it; otherwise the link opens here
  LL.siteDeepLink = async function () {
    const want = LL.query.get('site');
    const clean = withoutSite(location.hash);
    const strip = () => { history.replaceState(null, '', LL.base + clean); LL.query.delete('site'); };
    if (!want || want === (LL.site() || {}).id) { strip(); return false; }
    let list = null;
    try { list = await LL.siteList(); } catch (e) { list = null; }
    if (list && (list.sites || []).some(s => s.id === want)) { LL.switchSite(want, clean); return true; }
    strip();
    if (list) LL.toast(t('site.err.unknown'), 'error');
    return false;
  };

  let sitesCache = null;
  LL.siteList = function (fresh) {
    if (!sitesCache || fresh) sitesCache = api('/api/sites').catch(e => { sitesCache = null; throw e; });
    return sitesCache;
  };

  // ------------------------------------------------------------------ header switcher
  const CARET = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
  const PIN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/></svg>';
  LL.siteSwitcher = function () {
    const s = LL.site();
    if (!s || !s.id) return null;
    if (stored() !== s.id) remember(s.id);         // the server picked another site (no longer released): follow it
    if (!s.several) return null;
    registerCommands();
    const label = LL.siteName(s);
    const btn = h('button', { class: 'site-switch' + (s.remote ? ' site-switch--remote' : ''), type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      title: t('site.switch.title', { name: label }), 'aria-label': t('site.switch.title', { name: label }), dataset: { site: s.id } },
    h('span', { class: 'site-switch__pin', 'aria-hidden': 'true' }), h('span', { class: 'site-switch__name', 'data-clip-ok': '' }, label),   // full name in the title
    h('span', { class: 'site-switch__caret', 'aria-hidden': 'true' }));
    btn.querySelector('.site-switch__pin').innerHTML = PIN;
    btn.querySelector('.site-switch__caret').innerHTML = CARET;
    btn.addEventListener('click', () => openMenu(btn));
    return btn;
  };

  let menuEl = null;
  function closeMenu(focusBack) {
    if (!menuEl) return;
    const { el, btn, off } = menuEl;
    menuEl = null;
    el.remove(); off();
    btn.setAttribute('aria-expanded', 'false');
    if (focusBack) btn.focus();
  }
  async function openMenu(btn) {
    if (menuEl) { closeMenu(true); return; }
    const el = h('div', { class: 'site-menu card', role: 'menu', 'aria-label': t('site.switch.menu') }, h('div', { class: 'skeleton site-menu__skel' }));
    document.body.appendChild(el);
    const place = () => {
      const r = btn.getBoundingClientRect();
      if (window.innerWidth <= 700) { el.style.left = '8px'; el.style.right = '8px'; el.style.top = (r.bottom + 6) + 'px'; }
      else { el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - el.offsetWidth - 8)) + 'px'; el.style.top = (r.bottom + 6) + 'px'; }
    };
    const onDoc = ev => { if (menuEl && !el.contains(ev.target) && !btn.contains(ev.target)) closeMenu(false); };
    const onKey = ev => {
      if (!menuEl) return;
      const items = [...el.querySelectorAll('[role^="menuitem"]')];
      const i = items.indexOf(document.activeElement);
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closeMenu(true); }
      else if (ev.key === 'ArrowDown') { ev.preventDefault(); (items[i + 1] || items[0]).focus(); }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
      else if (ev.key === 'Tab') closeMenu(false);
    };
    const onResize = () => place();
    setTimeout(() => document.addEventListener('pointerdown', onDoc), 0);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onResize);
    menuEl = { el, btn, off: () => { document.removeEventListener('pointerdown', onDoc); document.removeEventListener('keydown', onKey, true); window.removeEventListener('resize', onResize); } };
    btn.setAttribute('aria-expanded', 'true');
    place();
    let data;
    try { data = await LL.siteList(true); } catch (e) { el.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); return; }
    if (!menuEl || menuEl.el !== el) return;
    const cur = (LL.site() || {}).id;
    el.replaceChildren(
      h('div', { class: 'site-menu__head' }, t('site.switch.menu')),
      ...data.sites.map(s => h('button', { class: 'site-menu__item' + (s.id === cur ? ' is-current' : ''), type: 'button', role: 'menuitemradio', 'aria-checked': s.id === cur ? 'true' : 'false',
        onclick: () => { closeMenu(false); if (s.id !== cur) LL.switchSite(s.id, '#/overview'); } },
      h('span', { class: 'site-menu__check', 'aria-hidden': 'true' }, s.id === cur ? '✓' : ''),
      h('span', { class: 'site-menu__main' }, h('span', { class: 'site-menu__name' }, LL.siteName(s)),
        h('span', { class: 'site-menu__sub' }, t(s.home ? 'site.kind.local' : 'site.kind.remote'))))),
      h('div', { class: 'site-menu__sep', role: 'separator' }),
      h('button', { class: 'site-menu__item site-menu__all', type: 'button', role: 'menuitem', onclick: () => { closeMenu(false); LL.go('#/sites'); } },
        h('span', { class: 'site-menu__check', 'aria-hidden': 'true' }), h('span', { class: 'site-menu__main' }, h('span', { class: 'site-menu__name' }, t('site.all')),
          h('span', { class: 'site-menu__sub' }, t('site.all.sub', { n: data.sites.length })))));
    place();
    const first = el.querySelector('.is-current') || el.querySelector('[role^="menuitem"]');
    if (first) first.focus();
  }

  // ------------------------------------------------------------------ palette commands
  LL.registerCommand({ id: 'go.sites', group: 'views', order: 0, label: () => t('cmdk.go', { name: t('nav.sites') }), keywords: () => t('cmdk.kw.site'),
    available: () => !!(LL.site() && (LL.site().several || LL.can('admin'))), run: () => LL.go('#/sites') });
  let commandsFor = null;
  function registerCommands() {
    const cur = (LL.site() || {}).id;
    if (commandsFor === cur) return;
    commandsFor = cur;
    LL.siteList().then(data => {
      for (const s of data.sites) {
        LL.registerCommand({ id: 'site.switch.' + s.id, group: 'commands', order: 30, label: () => t('cmdk.site_switch', { name: LL.siteName(s) }),
          keywords: () => t('cmdk.kw.site'), available: () => (LL.site() || {}).id !== s.id, run: () => LL.switchSite(s.id, '#/overview') });
      }
    }).catch(() => {});
  }

  // ------------------------------------------------------------------ one site card
  const levelOf = s => (s.wan && s.wan.state === 'down' ? 'danger' : (s.level || 'ok'));
  function reachChips(s) {
    const list = REACH.filter(r => (s.reach || []).includes(r));
    if (!list.length) return h('span', { class: 'chip chip--warn' }, t('site.reach.none'));
    return list.map(r => h('span', { class: 'chip chip--muted site-reach site-reach--' + r }, t('site.reach.' + r)));
  }
  function siteCard(s, cur, admin, onChange) {
    const lv = levelOf(s);
    const d = s.devices || {};
    const name = LL.siteName(s);
    const top = (s.top || [])[0];
    const noteLine = top ? LL.noteText({ key: top.key, vars: top.vars || {}, sub: top.sub }).title : t('site.card.calm');
    const facts = h('dl', { class: 'site-card__facts' },
      h('div', {}, h('dt', {}, t('site.card.devices')), h('dd', {}, s.empty ? t('site.card.no_devices') : t('site.card.online', { online: d.online || 0, total: d.total || 0 }))),
      h('div', {}, h('dt', {}, t('site.card.new')), h('dd', {}, LL.fmtNum(d.new || 0, 0))),
      h('div', {}, h('dt', {}, t('site.card.internet')), h('dd', { class: 'site-wan site-wan--' + ((s.wan || {}).state || 'unknown') }, t('site.wan.' + ((s.wan || {}).state || 'unknown')))),
      h('div', {}, h('dt', {}, t('site.card.notes')), h('dd', {}, LL.fmtNum(s.notes || 0, 0))));
    const actions = h('div', { class: 'site-card__actions' },
      s.id === cur ? h('a', { class: 'btn btn--ghost btn--sm', href: '#/overview' }, t('site.card.open_here'))
        : h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => LL.switchSite(s.id, '#/overview') }, t('site.card.open')),
      admin ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => renameDialog(s, onChange) }, t('site.rename')) : null,
      admin && !s.home ? h('button', { class: 'btn btn--danger-ghost btn--sm', type: 'button', onclick: () => removeDialog(s, onChange) }, t('site.remove')) : null);
    return h('article', { class: 'card site-card site-card--' + lv + (s.id === cur ? ' is-current' : ''), dataset: { site: s.id } },
      h('header', { class: 'site-card__head' },
        h('i', { class: 'site-card__dot site-card__dot--' + lv, 'aria-hidden': 'true' }),
        h('h2', { class: 'site-card__name' }, name),
        h('span', { class: 'chip ' + (s.home ? 'chip--accent' : 'chip--muted') }, t(s.home ? 'site.kind.local' : 'site.kind.remote')),
        s.id === cur ? h('span', { class: 'chip chip--ok site-card__here' }, t('site.card.here')) : null),
      h('p', { class: 'site-card__state site-card__state--' + lv }, h('span', { class: 'sr-only' }, t('site.level.' + lv) + ': '), noteLine),
      facts,
      h('div', { class: 'site-card__reach' }, h('span', { class: 'site-card__reach-label muted' }, t('site.card.reach')), reachChips(s)),
      actions);
  }

  // ------------------------------------------------------------------ administration
  function renameDialog(s, onChange) {
    const input = h('input', { class: 'input', maxlength: '60', value: s.name || '', placeholder: s.home ? t('site.home') : '' });
    const msg = h('div', { class: 'msg' });
    let d;
    const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
      try { await api('/api/sites/' + encodeURIComponent(s.id), { method: 'PATCH', body: { name: input.value } }); d.close(); LL.toast(t('settings.saved'), 'ok'); onChange(); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('site.rename.save'));
    d = LL.dialog(t('site.rename.title', { name: LL.siteName(s) }), h('div', { class: 'stack' }, LL.field(t('site.name'), input, s.home ? t('site.rename.home_hint') : null), msg), save);
    setTimeout(() => input.focus(), 30);
  }
  function removeDialog(s, onChange) {
    const msg = h('div', { class: 'msg' });
    const confirm = h('input', { class: 'input', 'aria-label': t('site.remove.type', { name: s.name }) });
    let d;
    const go = h('button', { class: 'btn btn--danger', type: 'button', disabled: true, onclick: async () => {
      try { await api('/api/sites/' + encodeURIComponent(s.id), { method: 'DELETE' }); d.close(); LL.toast(t('site.remove.done', { name: s.name }), 'ok');
        if ((LL.site() || {}).id === s.id) LL.switchSite('home', '#/sites'); else onChange(); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('site.remove'));
    confirm.addEventListener('input', () => { go.disabled = confirm.value.trim() !== s.name; });
    d = LL.dialog(t('site.remove.title', { name: s.name }), h('div', { class: 'stack' }, h('p', {}, t('site.remove.lead')),
      LL.field(t('site.remove.type', { name: s.name }), confirm), msg), go);
  }

  function addCard(onChange) {
    const name = h('input', { class: 'input', maxlength: '60', placeholder: t('site.add.ph') });
    const msg = h('div', { class: 'msg' });
    const add = h('button', { class: 'btn btn--primary', type: 'submit' }, t('site.add.submit'));
    const form = h('form', { class: 'site-add', onsubmit: async ev => {
      ev.preventDefault(); add.disabled = true; LL.msg(msg, '');
      try { const s = await api('/api/sites', { method: 'POST', body: { name: name.value } }); name.value = ''; LL.toast(t('site.add.done', { name: s.name }), 'ok'); onChange(); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
      add.disabled = false;
    } }, LL.field(t('site.name'), name), h('div', { class: 'site-add__btn' }, add));
    return h('section', { class: 'card site-admin' }, h('h2', {}, t('site.add.title')), h('p', { class: 'muted' }, t('site.add.lead')), form, msg,
      h('p', { class: 'field__hint' }, t('site.add.next')));
  }

  function controllerCard(onChange) {
    const body = h('div', { class: 'stack' }, h('div', { class: 'skeleton' }));
    const card = h('section', { class: 'card site-admin' }, h('h2', {}, t('site.ctl.title')), h('p', { class: 'muted' }, t('site.ctl.lead')), body);
    api('/api/integrations').then(data => {
      const list = (data.integrations || data || []).filter(i => i && i.config && Object.prototype.hasOwnProperty.call(i.config, 'site'));
      if (!list.length) { body.replaceChildren(h('p', { class: 'muted' }, t('site.ctl.none'))); return; }
      const sel = h('select', { class: 'select' }, list.map(i => h('option', { value: String(i.id) }, i.name)));
      const out = h('div', { class: 'stack' });
      const show = h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
        show.disabled = true; out.replaceChildren(h('div', { class: 'skeleton' }));
        try {
          const r = await api('/api/sites/controller/' + sel.value);
          if (!r.ok) { out.replaceChildren(h('p', { class: 'msg msg--error' }, t(r.key))); return; }
          const others = r.sites.filter(x => x.ref !== r.current);
          if (!others.length) { out.replaceChildren(h('p', { class: 'muted' }, t('site.ctl.only_one'))); return; }
          const radios = others.map((x, i) => h('label', { class: 'check' }, h('input', { type: 'radio', name: 'ctl-site', value: x.ref, checked: i === 0 }),
            h('span', {}, x.name, x.name !== x.ref ? h('span', { class: 'muted mono' }, ' (' + x.ref + ')') : null)));
          const nm = h('input', { class: 'input', maxlength: '60', value: others[0].name });
          radios.forEach(l => l.querySelector('input').addEventListener('change', ev => { const x = others.find(o => o.ref === ev.target.value); if (x) nm.value = x.name; }));
          const msg = h('div', { class: 'msg' });
          const go = h('button', { class: 'btn btn--primary', type: 'button', onclick: async () => {
            const ref = (card.querySelector('input[name="ctl-site"]:checked') || {}).value;
            go.disabled = true;
            try { const s = await api('/api/sites/from-controller', { method: 'POST', body: { integration: Number(sel.value), ref, name: nm.value } });
              LL.toast(t('site.add.done', { name: s.name }), 'ok'); onChange(); }
            catch (e) { LL.msg(msg, LL.errText(e), 'error'); go.disabled = false; }
          } }, t('site.ctl.create'));
          out.replaceChildren(h('fieldset', { class: 'site-ctl__list' }, h('legend', {}, t('site.ctl.pick')), radios), LL.field(t('site.name'), nm), h('div', { class: 'btn-row' }, go), msg,
            h('p', { class: 'field__hint' }, t('site.ctl.hint')));
        } catch (e) { out.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); }
        finally { show.disabled = false; }
      } }, t('site.ctl.show'));
      body.replaceChildren(h('div', { class: 'site-ctl__row' }, LL.field(t('site.ctl.connection'), sel), h('div', { class: 'site-add__btn' }, show)), out);
    }).catch(e => body.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))));
    return card;
  }

  function reportCard(mode) {
    const opts = ['per_site', 'combined'].map(m => h('label', { class: 'check site-report__opt' }, h('input', { type: 'radio', name: 'site-report', value: m, checked: m === mode,
      onchange: async () => {
        try { await api('/api/sites/report', { method: 'PUT', body: { mode: m } }); LL.toast(t('settings.saved'), 'ok'); } catch (e) { LL.toast(LL.errText(e), 'error'); }
      } }), h('span', {}, h('strong', {}, t('site.report.mode.' + m)), h('span', { class: 'field__hint' }, t('site.report.mode.' + m + '.hint')))));
    return h('section', { class: 'card site-admin' }, h('h2', {}, t('site.report.mode.title')), h('div', { class: 'stack-sm' }, opts));
  }

  function howCard() {
    return h('section', { class: 'card site-how' }, h('h2', {}, t('site.how.title')),
      h('ul', { class: 'site-how__list' }, ['controller', 'agent', 'scan'].map(k => h('li', {}, h('strong', {}, t('site.how.' + k + '.title')), ' ', t('site.how.' + k + '.text')))));
  }

  // ------------------------------------------------------------------ "All sites" page
  async function sitesView(app) {
    const fr = LL.frame(app, 'sites');
    const admin = LL.can('admin');
    const stamp = h('span', { class: 'muted ov-stamp' });
    const grid = h('div', { class: 'site-grid' }, h('div', { class: 'skeleton site-skel' }));
    const extra = h('div', { class: 'site-extra' });
    const summary = h('p', { class: 'site-summary muted' });
    const page = h('div', { class: 'page page--wide site-page' },
      h('div', { class: 'page__head' }, h('h1', {}, t('site.all')), stamp), summary, grid, extra);
    fr.main.append(page);
    let alive = true, fw = null;
    async function load() {
      if (!alive || document.hidden) return;
      let ov, list;
      try { [ov, list] = await Promise.all([api('/api/sites/overview'), LL.siteList(true)]); }
      catch (e) { grid.replaceChildren(h('div', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!alive) return;
      if (fw) fw.ok();
      const cur = (LL.site() || {}).id;
      const bad = ov.sites.filter(s => levelOf(s) === 'danger' || levelOf(s) === 'warn').length;
      summary.textContent = t(bad ? 'site.summary.bad' : 'site.summary.ok', { n: ov.sites.length, online: ov.online, total: ov.devices, bad });
      grid.replaceChildren(...ov.sites.map(s => siteCard(s, cur, admin, reload)));
      stamp.textContent = t('ov.stamp', { time: new Date(ov.t * 1000).toLocaleTimeString(LL.locale(), { hour12: false, hour: '2-digit', minute: '2-digit' }) });
      if (!extra.childNodes.length) {
        extra.append(howCard());
        if (admin) extra.append(addCard(reload), controllerCard(reload), reportCard(list.report_mode));
      }
    }
    function reload() { extra.replaceChildren(); LL.siteList(true).then(() => LL.route()); }
    fw = LL.fresh.watch(grid, EVERY_MS, load);
    await load();
    const timer = setInterval(load, EVERY_MS);
    return { destroy: () => { alive = false; clearInterval(timer); fw.destroy(); fr.destroy(); } };
  }
  LL.view('sites', sitesView);

  // ------------------------------------------------------------------ overview card: every site at a glance
  if (LL.registerOverviewCard) {
    LL.registerOverviewCard({ id: 'sites', order: 5, available: () => !!(LL.site() && LL.site().several), title: () => t('site.all'),
      render: () => {
        const box = h('div', { class: 'ov-sites' }, h('div', { class: 'skeleton' }));
        api('/api/sites/overview').then(ov => {
          const cur = (LL.site() || {}).id;
          box.replaceChildren(h('ul', { class: 'ov-list ov-sites__list' }, ov.sites.map(s => {
            const lv = levelOf(s), d = s.devices || {};
            return h('li', { class: 'ov-site' },
              h('i', { class: 'site-card__dot site-card__dot--' + lv, 'aria-hidden': 'true' }),
              h('span', { class: 'ov-site__main' }, h('span', { class: 'ov-site__name' }, LL.siteName(s), s.id === cur ? h('span', { class: 'chip chip--ok' }, t('site.card.here')) : null),
                h('span', { class: 'ov-site__sub' }, [t('site.card.online', { online: d.online || 0, total: d.total || 0 }), t('site.wan.' + ((s.wan || {}).state || 'unknown'))].join(' · '))),
              s.id === cur ? null : h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => LL.switchSite(s.id, '#/overview') }, t('site.card.open')));
          })), h('a', { class: 'btn btn--ghost btn--sm', href: '#/sites' }, t('site.all.open')));
        }).catch(e => box.replaceChildren(h('p', { class: 'ov-calm' }, LL.errText(e))));
        return box;
      } });
  }

  // ------------------------------------------------------------------ site picker (user administration, invitations)
  // {el, value()}: "all sites (also later ones)" or a list of site ids; administrators always see every site.
  LL.sitePicker = function (sites, current, disabled) {
    const all = h('input', { type: 'checkbox', checked: current === '*' || current == null, disabled: !!disabled });
    const boxes = sites.map(s => h('input', { type: 'checkbox', value: s.id, checked: Array.isArray(current) ? current.includes(s.id) : true, disabled: !!disabled }));
    const sync = () => { boxes.forEach(b => { b.disabled = !!disabled || all.checked; if (all.checked) b.checked = true; }); };
    all.addEventListener('change', sync); sync();
    const el = h('div', { class: 'site-picker stack-sm' },
      h('label', { class: 'check' }, all, h('span', {}, h('strong', {}, t('site.pick.all')), h('span', { class: 'field__hint' }, t('site.pick.all_hint')))),
      h('div', { class: 'site-picker__list' }, sites.map((s, i) => h('label', { class: 'check' }, boxes[i], h('span', {}, LL.siteName(s))))));
    return { el, value: () => (all.checked ? '*' : boxes.filter(b => b.checked).map(b => b.value)) };
  };
})();
