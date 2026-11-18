/* Lanloom — accessibility helpers shared by every view.

   - LL.prefs                 small per-browser choices (a string under a key), safe without storage
   - LL.announce(text, opt)   spoken status for screen readers through one polite live region outside the page. Live messages
                              are throttled: at most one announcement every GAP_MS, the newest text per key wins, three at most
                              are read in one go ("... and 2 more changes"). { now: true } is for the answer to an own action.
   - LL.a11y.watch(frame)     announces the important status changes of the live frame: a device that stopped talking or is back,
                              the overall grade, the live connection. The first frame only records the state; every device has its
                              own cool-down, so a flapping device cannot talk all day.
   - LL.a11y.help()           the keyboard help (key ?, also in the command palette)
   - LL.a11y.tableAlt(...)    a closed "Show as table" part under a diagram: the numbers behind the picture
   - LL.a11y.trapFocus(el)    keeps Tab inside a dialog and gives focus back to where it came from when it closes */
(function () {
  'use strict';
  const LL = window.Lanloom = window.Lanloom || {};
  const GAP_MS = 5000;               // between two announcements of the live frame
  const COOLDOWN_MS = 60000;         // per device: the same kind of change is announced again after this long at the earliest
  const MAX_ITEMS = 3;

  // ------------------------------------------------------------------ small preferences
  LL.prefs = {
    get(key, dflt) { try { const v = localStorage.getItem(key); return v == null ? dflt : v; } catch (e) { return dflt; } },
    set(key, value) { try { localStorage.setItem(key, String(value)); } catch (e) { /* private mode */ } }
  };

  // ------------------------------------------------------------------ announcements
  let region = null;
  function ensureRegion() {
    if (region && region.isConnected) return region;
    region = document.createElement('div');
    region.id = 'll-announcer';
    region.className = 'sr-only';
    region.setAttribute('role', 'status');
    region.setAttribute('aria-live', 'polite');
    region.setAttribute('aria-atomic', 'true');
    document.body.appendChild(region);
    return region;
  }
  const queue = new Map();
  let timer = null, lastAt = 0;
  function speak(text) {
    const r = ensureRegion();
    r.textContent = '';
    setTimeout(() => { r.textContent = text; }, 60);       // an empty region first, so the same text is read again
  }
  function flush() {
    timer = null;
    if (!queue.size) return;
    const items = [...queue.values()];
    queue.clear();
    lastAt = Date.now();
    let text = items.slice(0, MAX_ITEMS).join('. ');
    if (items.length > MAX_ITEMS) text += '. ' + LL.t('a11y.more', { count: items.length - MAX_ITEMS });
    speak(text);
  }
  LL.announce = function (text, opt) {
    if (!text) return;
    opt = opt || {};
    if (opt.now) { speak(String(text)); return; }
    queue.set(opt.key || String(text), String(text));
    if (!timer) timer = setTimeout(flush, Math.max(0, GAP_MS - (Date.now() - lastAt)));
  };

  // ------------------------------------------------------------------ live frame watcher
  const names = new Map();           // component id → title (the map view hands them over)
  const prev = { grades: null, overall: null };
  const cool = new Map();
  LL.a11y = LL.a11y || {};
  LL.a11y.setNames = function (components) {
    names.clear();
    (components || []).forEach(c => { if (c.title) names.set(c.id, c.title); });
  };
  LL.a11y.watch = function (frame) {
    if (!frame) return;
    const now = Date.now();
    if (frame.grades) {
      const first = prev.grades === null;
      const before = prev.grades || {};
      for (const id in frame.grades) {
        const g = frame.grades[id], p = before[id];
        if (first || p === undefined || p === g) continue;
        const name = names.get(id);
        if (!name) continue;
        const lost = g === 'interrupted', back = p === 'interrupted' && g !== 'interrupted';
        if (!lost && !back) continue;
        if (now - (cool.get(id + (lost ? ':l' : ':b')) || 0) < COOLDOWN_MS) continue;
        cool.set(id + (lost ? ':l' : ':b'), now);
        LL.announce(LL.t(lost ? 'a11y.live.lost' : 'a11y.live.back', { name }), { key: id });
      }
      prev.grades = Object.assign({}, before, frame.grades);
    }
    const og = frame.overall && frame.overall.grade;
    if (og) {
      if (prev.overall !== null && prev.overall !== og && (og === 'interrupted' || prev.overall === 'interrupted')) {
        LL.announce(LL.t('a11y.live.overall', { grade: LL.t('board.grade.' + og) }), { key: 'overall' });
      }
      prev.overall = og;
    }
  };
  LL.a11y.watchState = function (state) {
    if (state === 'offline') LL.announce(LL.t('a11y.live.offline'), { key: 'state', now: false });
    else if (state === 'live') LL.announce(LL.t('a11y.live.online'), { key: 'state' });
  };

  // ------------------------------------------------------------------ focus handling for dialogs
  const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
  LL.a11y.trapFocus = function (el, opener) {
    const from = opener || document.activeElement;
    const items = () => [...el.querySelectorAll(FOCUSABLE)].filter(x => x.offsetParent !== null || x === document.activeElement);
    const first = items().find(x => !x.classList.contains('icon-btn')) || items()[0];
    if (first) first.focus({ preventScroll: true });
    else { el.tabIndex = -1; el.focus({ preventScroll: true }); }
    const onKey = ev => {
      if (ev.key !== 'Tab') return;
      const list = items();
      if (!list.length) { ev.preventDefault(); return; }
      const a = list[0], z = list[list.length - 1];
      if (ev.shiftKey && (document.activeElement === a || !el.contains(document.activeElement))) { ev.preventDefault(); z.focus(); }
      else if (!ev.shiftKey && (document.activeElement === z || !el.contains(document.activeElement))) { ev.preventDefault(); a.focus(); }
    };
    el.addEventListener('keydown', onKey);
    return function release() {
      el.removeEventListener('keydown', onKey);
      // a view that was drawn again meanwhile replaced the element: find its successor by the id it carried
      let back = from && from.isConnected ? from : null;
      if (!back && from && from.dataset && from.dataset.id) back = document.querySelector('[data-id="' + (window.CSS && CSS.escape ? CSS.escape(from.dataset.id) : from.dataset.id) + '"]');
      if (back && back.focus) back.focus({ preventScroll: true });
    };
  };

  // ------------------------------------------------------------------ table alternative of a diagram
  // rows: [[cell, cell ...], ...] (strings); the part stays closed until asked for, a screen reader finds it right after the picture
  LL.a11y.tableAlt = function (caption, head, rows, id) {
    const h = LL.h, t = LL.t;
    const table = h('table', { class: 'mini-table alt-table' }, h('caption', { class: 'sr-only' }, caption),
      h('thead', {}, h('tr', {}, head.map((c, i) => h('th', { scope: 'col', class: i ? 'num' : '' }, c)))),
      h('tbody', {}, rows.map(r => h('tr', {}, r.map((c, i) => (i ? h('td', { class: 'num' }, c) : h('th', { scope: 'row' }, c)))))));
    const el = LL.keepOpen && id ? LL.keepOpen.details(id, false, t('a11y.table.show'), table) : h('details', { class: 'keep' }, h('summary', {}, t('a11y.table.show')), table);
    el.classList.add('alt');
    // the rows can be handed over again when the numbers change; the part keeps its open or closed state
    el.setRows = rows2 => table.tBodies[0].replaceChildren(...rows2.map(r => h('tr', {}, r.map((c, i) => (i ? h('td', { class: 'num' }, c) : h('th', { scope: 'row' }, c))))));
    return el;
  };

  // ------------------------------------------------------------------ keyboard help (key ?)
  const kbd = (id, label) => LL.h('kbd', { class: 'kbd' }, label != null ? label : LL.t('a11y.key.' + id));
  function helpBody() {
    const h = LL.h, t = LL.t;
    const grp = (title, rows) => h('section', { class: 'kb-group' }, h('h3', {}, title),
      h('dl', { class: 'kb-list' }, rows.map(([keys, what]) => [h('dt', {}, keys), h('dd', {}, t(what))])));
    const or = (...k) => k.flatMap((x, i) => (i ? [h('span', { class: 'kb-or', 'aria-hidden': 'true' }, ' / '), x] : [x]));      // a new separator each time: one node cannot sit in two rows
    return h('div', { class: 'kb-help' },
      h('p', { class: 'muted' }, t('a11y.help.lead')),
      grp(t('a11y.help.everywhere'), [
        [kbd('slash'), 'a11y.help.search'], [kbd('ctrl_k'), 'a11y.help.search'],
        [kbd('l'), 'a11y.help.rail_now'], [kbd('i'), 'a11y.help.rail_about'], [kbd('question'), 'a11y.help.this'],
        [or(kbd('tab'), kbd('shift_tab')), 'a11y.help.tab'], [kbd('enter'), 'a11y.help.enter'], [kbd('esc'), 'a11y.help.esc']]),
      grp(t('a11y.help.tree'), [
        [or(kbd('up', '↑'), kbd('down', '↓')), 'a11y.help.tree_move'], [kbd('right', '→'), 'a11y.help.tree_open'], [kbd('left', '←'), 'a11y.help.tree_close'],
        [or(kbd('home'), kbd('end')), 'a11y.help.tree_ends'], [or(kbd('enter'), kbd('space')), 'a11y.help.tree_card'], [kbd('star'), 'a11y.help.tree_all'], [t('a11y.help.tree_letter_key'), 'a11y.help.tree_letter']]),
      grp(t('a11y.help.board'), [[or(kbd('tab'), kbd('shift_tab')), 'a11y.help.board_tab'], [or(kbd('enter'), kbd('space')), 'a11y.help.board_card']]));
  }
  LL.a11y.help = function () {
    if (document.querySelector('.modal--keys')) return null;
    const d = LL.dialog(LL.t('a11y.help.title'), helpBody());
    d.el.classList.add('modal--keys');
    // on a small screen the list scrolls; a scrolling area without anything to focus cannot be reached by keyboard, so it can take the focus itself
    const body = d.el.querySelector('.modal__body');
    if (body) { body.tabIndex = 0; body.setAttribute('role', 'region'); body.setAttribute('aria-label', LL.t('a11y.help.title')); }
    return d;
  };
  const typing = el => el && (/^(input|textarea|select)$/i.test(el.tagName) || el.isContentEditable);
  document.addEventListener('keydown', ev => {
    if (ev.key !== '?' || ev.ctrlKey || ev.metaKey || ev.altKey || typing(ev.target)) return;
    if (!document.querySelector('.topbar') || document.querySelector('.modal')) return;      // only inside the signed-in frame, never on top of another dialog
    ev.preventDefault();
    LL.a11y.help();
  });
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'keys-help', group: 'commands', order: 44, label: 'a11y.help.title', keywords: () => LL.t('a11y.help.kw'), run: () => LL.a11y.help() });
  }
  document.addEventListener('DOMContentLoaded', ensureRegion);
  if (document.readyState !== 'loading') ensureRegion();
})();
