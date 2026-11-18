/* Lanloom — page shell around every signed-in view: the "Right now" rail (key L), the "About this view" rail (key I),
   remembered open/closed states of collapsible parts, and the commands for both rails.

   Both rails sit beside the page on wide screens and slide over it on smaller ones (phones: a sheet with a close button).
   Whether a rail is shown is remembered per browser; without a choice "About this view" is open from 1900 px on.
   Collapsible parts (<details> and "show all" lists) keep their state in this module while the live rhythm draws the
   content again (LL.keepOpen). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const DOCK_FROM = 1280;              // px: from here the rails sit beside the page instead of over it
  const ABOUT_OPEN_FROM = 1900;        // px: "About this view" is open by default from here (until the user chooses)
  const NOW_EVERY_MS = 5000;           // refresh of the "Right now" rail while it is shown
  const MANUAL = 'https://github.com/robeertm/lanloom/blob/main/docs/manual/';
  // one manual page per view (docs/manual/<name>.md); a view without an entry here has the page of its own name (a test checks every page)
  const MANUAL_PAGE = { paths: 'data-paths', reports: 'nightly-reports' };
  const ABOUT_VIEWS = ['overview', 'map', 'network', 'paths', 'devices', 'firewall', 'reports', 'automations', 'running', 'settings'];

  // ------------------------------------------------------------------ remembered open states (survive live redraws)
  const openState = new Map();
  LL.keepOpen = {
    get: (id, dflt) => (openState.has(id) ? openState.get(id) : !!dflt),
    set: (id, open) => { openState.set(id, !!open); },
    // a <details> whose open state is kept under id while its content is drawn again
    details(id, dflt, summary, ...body) {
      const el = h('details', { class: 'keep', dataset: { keep: id } }, h('summary', {}, summary), ...body);
      el.open = LL.keepOpen.get(id, dflt);
      el.addEventListener('toggle', () => openState.set(id, el.open));
      return el;
    }
  };

  // ------------------------------------------------------------------ rail preferences
  const pref = (key, dflt) => { try { const v = localStorage.getItem(key); return v == null ? dflt : v === '1'; } catch (e) { return dflt; } };
  const setPref = (key, v) => { try { localStorage.setItem(key, v ? '1' : '0'); } catch (e) { /* private mode */ } };
  const docked = () => window.innerWidth >= DOCK_FROM;
  const rails = { now: null, about: null, view: null, body: null };
  function wanted(which) {
    if (which === 'about') return pref('ll-rail-about', window.innerWidth >= ABOUT_OPEN_FROM);
    return pref('ll-rail-now', false);
  }
  // on smaller screens a rail slides over the page; it opens only on request there (never on its own after a navigation)
  const shown = { now: null, about: null };
  function isShown(which) {
    if (!docked()) return shown[which] === true;
    return shown[which] == null ? wanted(which) : shown[which];
  }
  function setRail(which, on) {
    shown[which] = on;
    if (docked()) setPref(which === 'about' ? 'll-rail-about' : 'll-rail-now', on);
    if (on && !docked()) shown[which === 'now' ? 'about' : 'now'] = false;   // one sheet at a time
    applyRails();
  }
  LL.toggleRail = which => setRail(which, !isShown(which));
  window.addEventListener('resize', () => { if (rails.body) applyRails(); });

  // ------------------------------------------------------------------ header buttons
  const ICON_NOW = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2.5-6 5 12 2.5-6h4"/></svg>';
  const ICON_ABOUT = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.1"/></svg>';
  LL.railButtons = function () {
    const btn = (which, icon, label, key) => {
      const text = t(label) + ' (' + key + ')';
      const b = h('button', { class: 'icon-btn rail-btn rail-btn--' + which, type: 'button', 'aria-label': text, title: text, 'aria-keyshortcuts': key, onclick: () => LL.toggleRail(which) });
      b.setAttribute('aria-pressed', 'false'); b.setAttribute('aria-controls', 'rail-' + which);
      b.innerHTML = icon; return b;
    };
    return [btn('now', ICON_NOW, 'rail.now.title', 'L'), btn('about', ICON_ABOUT, 'rail.about.title', 'I')];
  };

  // ------------------------------------------------------------------ body: page + rails
  LL.shellBody = function (main, active) {
    stopNow();
    rails.view = active;
    const nowRail = h('aside', { class: 'rail rail--now', id: 'rail-now', 'aria-label': t('rail.now.title'), hidden: true });
    const aboutRail = h('aside', { class: 'rail rail--about', id: 'rail-about', 'aria-label': t('rail.about.title'), hidden: true });
    const scrim = h('div', { class: 'rail-scrim', hidden: true, onclick: () => { shown.now = false; shown.about = false; applyRails(); } });
    rails.now = nowRail; rails.about = aboutRail;
    rails.body = h('div', { class: 'shell' }, main, nowRail, aboutRail, scrim);
    rails.scrim = scrim;
    // the page is drawn after this call; apply on the next frame so the header buttons exist
    requestAnimationFrame(applyRails);
    return rails.body;
  };

  function applyRails() {
    if (!rails.body || !rails.body.isConnected) { stopNow(); return; }
    const dock = docked();
    rails.body.classList.toggle('shell--dock', dock);
    for (const which of ['now', 'about']) {
      const on = isShown(which);
      const el = rails[which];
      const was = !el.hidden;
      el.hidden = !on;
      rails.body.classList.toggle('shell--' + which, on);
      document.querySelectorAll('.rail-btn--' + which).forEach(b => { b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.classList.toggle('is-on', on); });
      if (on && !was) (which === 'now' ? startNow : drawAbout)();
      if (!on && which === 'now') stopNow();
    }
    rails.scrim.hidden = dock || !(isShown('now') || isShown('about'));
    // the board measures its host; tell it the room changed
    window.dispatchEvent(new Event('ll-layout'));
  }

  function railHead(which, title) {
    return h('header', { class: 'rail__head' }, h('h2', {}, title),
      h('button', { class: 'icon-btn rail__close', type: 'button', 'aria-label': t('detail.close'), onclick: () => setRail(which, false) }, '✕'));
  }

  // ------------------------------------------------------------------ "About this view"
  function drawAbout() {
    const v = ABOUT_VIEWS.includes(rails.view) ? rails.view : 'map';
    const paras = String(t('about.' + v + '.text')).split('\n\n').map(p => h('p', {}, p));
    const tips = LL.strings['about.' + v + '.tips'] ? h('ul', { class: 'about-tips' }, String(t('about.' + v + '.tips')).split('\n').map(x => h('li', {}, x))) : null;
    rails.about.replaceChildren(railHead('about', t('rail.about.title')),
      h('div', { class: 'rail__body about' }, h('h3', { class: 'about__title' }, t('about.' + v + '.title')), paras, tips,
        h('p', { class: 'about__keys muted' }, t('rail.keys')),
        h('a', { class: 'btn btn--ghost btn--sm about__more', href: MANUAL + (MANUAL_PAGE[v] || v) + '.md', target: '_blank', rel: 'noopener noreferrer' }, t('rail.about.manual'))));
  }

  // ------------------------------------------------------------------ "Right now"
  let nowTimer = null, nowSeq = 0;
  function stopNow() { clearInterval(nowTimer); nowTimer = null; nowSeq++; }
  function startNow() {
    stopNow();
    const seq = nowSeq;
    rails.now.replaceChildren(railHead('now', t('rail.now.title')), h('div', { class: 'rail__body' }, h('div', { class: 'skeleton' })));
    const load = async () => {
      if (document.hidden || !rails.now || rails.now.hidden) return;
      let d; try { d = await api('/api/overview/now'); } catch (e) { return; }
      if (seq !== nowSeq) return;
      drawNow(d);
    };
    load();
    nowTimer = setInterval(load, NOW_EVERY_MS);
  }
  LL.noteText = function (n) {
    const v = Object.assign({}, n.vars || {});
    if (v.age_dur != null) v.age = LL.fmtAge(v.age_dur);
    if (v.kind_key) v.kind = t(v.kind_key);
    if (v.name_key) v.name = t(v.name_key);
    v.reason = v.reason_key ? t(v.reason_key) : '';
    if (v.day && /^\d{4}-\d{2}-\d{2}$/.test(v.day)) {
      try { v.day = new Date(v.day + 'T12:00:00').toLocaleDateString(LL.locale(), { dateStyle: 'medium' }); } catch (e) { /* keep */ }
    }
    return { title: t(n.key, v), sub: n.sub ? t(n.sub, v) : '' };
  };
  // one noteworthy issue: level dot, one line, one sub line; a link to where it can be looked at
  LL.noteRow = function (n) {
    const tx = LL.noteText(n);
    return h('li', { class: 'note-row note-row--' + n.level },
      h('a', { class: 'note-row__link', href: n.link || '#/overview' }, h('i', { class: 'note-row__dot', 'aria-hidden': 'true' }),
        h('span', { class: 'note-row__main' }, h('span', { class: 'note-row__title' }, tx.title), tx.sub ? h('span', { class: 'note-row__sub' }, tx.sub) : null)));
  };
  const nameOf = e => (e.kind === 'self' ? t('kind.self') : e.kind === 'internet' ? t('kind.internet') : (e.title || t('kind.' + (e.kind || 'unknown'))));
  LL.changeText = function (c) {
    const d = c.data || {};
    const name = d.title || d.name || (d.kind ? t('kind.' + d.kind) : '');
    return t('change.' + c.kind, { name, reason: d.key ? t(d.key) : '', version: d.version || '', from: d.from || '', to: d.to || '' });
  };
  const changeLink = c => (c.data && c.data.id && /^d-/.test(c.data.id) && c.kind.startsWith('dev_') ? '#/devices/' + encodeURIComponent(c.data.id)
    : c.kind.startsWith('src_') ? '#/settings' : c.kind.startsWith('wan_') ? '#/map/internet' : null);
  const changeLevel = k => ({ dev_offline: 'warn', dev_gone: 'warn', src_fail: 'warn', wan_down: 'danger', dev_new: 'info' }[k] || 'ok');
  function section(id, title, count, body, empty) {
    return LL.keepOpen.details('now:' + id, true, h('span', { class: 'rail-sec__title' }, title, count != null ? h('span', { class: 'rail-sec__n' }, String(count)) : null),
      body && body.childNodes.length ? body : h('p', { class: 'rail-empty muted' }, empty));
  }
  function drawNow(d) {
    const body = rails.now.querySelector('.rail__body');
    if (!body) return;
    const scroll = body.scrollTop;
    const notes = h('ul', { class: 'note-list' }, d.notes.map(LL.noteRow));
    const talks = h('ul', { class: 'talk-list' }, d.talks.map(tk => h('li', { class: 'talk' },
      h('a', { class: 'talk__link', href: tk.kind === 'topic' ? '#/map' : '#/map/' + encodeURIComponent(tk.b.kind === 'self' ? tk.a.id : tk.b.id) },
        h('span', { class: 'talk__trace talk__trace--' + tk.kind, 'aria-hidden': 'true' }),
        h('span', { class: 'talk__names' }, h('span', { class: 'talk__a' }, nameOf(tk.a)), h('span', { class: 'talk__arrow', 'aria-hidden': 'true' }, '↔'), h('span', { class: 'talk__b' }, nameOf(tk.b))),
        h('span', { class: 'talk__rate' }, LL.fmtRate(tk.r0 + tk.r1))))));
    const changes = h('ul', { class: 'change-list' }, d.changes.map(c => {
      const link = changeLink(c);
      const inner = [h('i', { class: 'change__dot change__dot--' + changeLevel(c.kind), 'aria-hidden': 'true' }), h('span', { class: 'change__text' }, LL.changeText(c)),
        h('span', { class: 'change__when muted' }, t('time.ago', { age: LL.fmtAge(Date.now() / 1000 - c.t) }))];
      return h('li', { class: 'change' }, link ? h('a', { class: 'change__link', href: link }, inner) : h('span', { class: 'change__link' }, inner));
    }));
    const fresh = h('ul', { class: 'change-list' }, d.new.map(n => h('li', { class: 'change' }, h('a', { class: 'change__link', href: '#/devices/' + encodeURIComponent(n.id) },
      LL.kindIcon ? LL.kindIcon(n.kind) : null, h('span', { class: 'change__text' }, n.title || n.ip, h('span', { class: 'muted' }, ' · ' + t('ident.origin_short.' + (n.origin || 'no_mac')))),
      h('span', { class: 'change__when muted' }, t('time.ago', { age: LL.fmtAge(Date.now() / 1000 - n.first_seen) }))))));
    body.replaceChildren(
      section('notes', t('rail.now.notes'), d.notes.length, notes, t('rail.now.notes_none')),
      section('talks', t('rail.now.talks'), d.talks.length, talks, t('rail.now.talks_none')),
      section('changes', t('rail.now.changes'), d.changes.length, changes, t('rail.now.changes_none')),
      section('new', t('rail.now.new'), d.new.length, fresh, t('rail.now.new_none')),
      h('p', { class: 'rail-foot muted' }, t('rail.now.updated', { time: new Date(d.t * 1000).toLocaleTimeString(LL.locale(), { hour12: false }) })));
    body.scrollTop = scroll;
  }

  // ------------------------------------------------------------------ keys L and I (not while typing, not with modifiers)
  document.addEventListener('keydown', ev => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.repeat) return;
    const el = ev.target;
    if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
    if (document.querySelector('.modal')) return;
    if (!rails.body || !rails.body.isConnected) return;
    const k = ev.key.toLowerCase();
    if (k === 'l') { ev.preventDefault(); LL.toggleRail('now'); }
    else if (k === 'i') { ev.preventDefault(); LL.toggleRail('about'); }
    else if (k === 'escape' && !docked() && (isShown('now') || isShown('about'))) { shown.now = false; shown.about = false; applyRails(); }
  });

  // ------------------------------------------------------------------ commands (command palette register)
  LL.commands = LL.commands || [];
  LL.registerCommand = LL.registerCommand || function (cmd) { LL.commands = LL.commands.filter(c => c.id !== cmd.id).concat([cmd]); };
  LL.registerCommand({ id: 'go.overview', group: 'views', order: -1, label: () => t('cmdk.go', { name: t('nav.overview') }), keywords: () => t('cmdk.kw.overview'), run: () => LL.go('#/overview') });
  LL.registerCommand({ id: 'rail-now', group: 'commands', order: 40, label: 'cmdk.rail.now', keywords: () => t('cmdk.kw.rail'), run: () => LL.toggleRail('now') });
  LL.registerCommand({ id: 'rail-about', group: 'commands', order: 41, label: 'cmdk.rail.about', keywords: () => t('cmdk.kw.rail'), run: () => LL.toggleRail('about') });
  LL.registerCommand({ id: 'live-grade', group: 'commands', order: 42, label: 'cmdk.live_grade', keywords: () => t('cmdk.kw.live'), run: () => LL.openLiveGrade && LL.openLiveGrade() });
  LL.registerCommand({ id: 'live-reconnect', group: 'commands', order: 43, label: 'livestate.reconnect', keywords: () => t('cmdk.kw.live'),
    available: () => !!(LL.live && LL.live.state() !== 'live'), run: () => LL.live.reconnect() });
})();
