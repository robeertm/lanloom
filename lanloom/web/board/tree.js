/* Lanloom — tree view of the physical chain (core module, API = 1): Internet → gateway → switches and access points →
   devices and guests, with the port, the kind of link, the live traffic as a number and the state of every element.

   It is the second face of the Network view and the text alternative of the circuit board: everything the board shows
   as a picture is a line here that a screen reader announces, the keyboard walks and a phone can fold.

   Pure part (no DOM, tested under Node):  build(view, { zone, only }) → { roots, other, count, problems, items }
     roots   top-level nodes (Internet first), other = devices that sit in no chain (a group node, or null)
     node    { id, kind, title, subtitle, status, grade, metric, link, children, depth, count, problems }
     link    null or { id (of the trace), kind: cable|radio|virtual|uplink, port, own_port, band, ssid, medium, measured }
   DOM part:  mount(host, { view, t, locale, filter, onSelect }) → { update(view), live(frame), highlight(id), setTheme(), destroy(), focus() }
     role="tree" with treeitems (WAI-ARIA tree pattern): one tab stop (roving), arrow keys move, Right/Left open and close or go
     to the child / parent, Home/End, * opens the level, typing a letter jumps, Enter or Space opens the component's card.
     Open and closed branches are remembered in this module, so the live rhythm can draw the tree again without folding it. */
(function (root, factory) {
  const T = factory();
  if (typeof module === 'object' && module.exports) module.exports = T;
  else {
    root.Lanloom = root.Lanloom || {};
    root.Lanloom.Tree = T;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const GRADES = ['realtime', 'near', 'periodic', 'sluggish', 'interrupted', 'unknown'];
  const STATUSES = ['up', 'down', 'unknown'];
  const MEDIUM_RANK = { cable: 0, radio: 1, virtual: 2 };
  const KIND_RANK = { internet: 0, router: 1, switch: 2, ap: 3 };
  const OTHER_ID = 'tree:other';
  const AUTO_OPEN_ROWS = 60;          // branches open on first sight while the tree stays at most this many rows tall

  // ---------------------------------------------------------------- pure part
  const natural = (a, b) => {
    const x = String(a == null ? '' : a).match(/\d+|\D+/g) || [], y = String(b == null ? '' : b).match(/\d+|\D+/g) || [];
    for (let i = 0; i < Math.min(x.length, y.length); i++) {
      const nx = /^\d/.test(x[i]), ny = /^\d/.test(y[i]);
      if (nx && ny) { const d = parseInt(x[i], 10) - parseInt(y[i], 10); if (d) return d; }
      else if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
    }
    return x.length - y.length;
  };

  function linkOf(trace) {
    if (!trace) return null;
    const p = trace.phys || {};
    const kind = trace.kind === 'uplink' ? 'uplink' : ({ radio: 'radio', virtual: 'virtual' }[trace.kind] || 'cable');
    return { id: trace.id == null ? null : trace.id, kind, port: p.port || null, own_port: p.own_port || null, band: p.band || null, ssid: p.ssid || null, medium: p.medium || null, measured: !!trace.measured };
  }

  const isProblem = c => c.status === 'down' || c.grade === 'interrupted';
  // "4.9 MB/s" comes with a decimal point from the server; show the reader's own separator
  function localiseMetric(s, locale) {
    if (!/\d\.\d/.test(s)) return s;
    let dec = '.';
    try { const p = new Intl.NumberFormat(locale || undefined).formatToParts(1.1).find(x => x.type === 'decimal'); if (p) dec = p.value; } catch (e) { /* keep the point */ }
    return dec === '.' ? s : s.replace(/(\d)\.(\d)/g, '$1' + dec + '$2');
  }

  function build(view, opts) {
    opts = opts || {};
    const comps = (view && view.components) || [], traces = (view && view.traces) || [];
    const byId = new Map(comps.map(c => [c.id, c]));
    // one parent per component: the uplink of the gateway first, then the physical links (the first one wins)
    const parent = new Map();
    const take = tr => { if (byId.has(tr.a) && byId.has(tr.b) && tr.a !== tr.b && !parent.has(tr.b)) parent.set(tr.b, { parent: tr.a, trace: tr }); };
    traces.filter(tr => tr.kind === 'uplink').forEach(take);
    traces.filter(tr => typeof tr.id === 'string' && tr.id.startsWith('p:')).forEach(take);
    // a link that would close a loop is dropped (the server does not send one; a half-drawn view must not hang the page)
    for (const id of [...parent.keys()]) {
      const seen = new Set([id]);
      let cur = parent.get(id) && parent.get(id).parent;
      while (cur && parent.has(cur)) { if (seen.has(cur)) { parent.delete(id); break; } seen.add(cur); cur = parent.get(cur).parent; }
    }
    const kids = new Map();
    comps.forEach(c => { const p = parent.get(c.id); if (p) { if (!kids.has(p.parent)) kids.set(p.parent, []); kids.get(p.parent).push(c.id); } });
    const rankOf = id => { const l = parent.get(id); return l ? (MEDIUM_RANK[linkOf(l.trace).kind] || 0) : 0; };
    kids.forEach(list => list.sort((a, b) => rankOf(a) - rankOf(b)
      || natural((parent.get(a).trace.phys || {}).port, (parent.get(b).trace.phys || {}).port)
      || natural((byId.get(a).title || ''), (byId.get(b).title || ''))));

    // what stays after the filter: matches, their ancestors and the edge (Internet and gateway keep their place for orientation)
    let keep = null;
    if (opts.zone || opts.only) {
      keep = new Set();
      comps.forEach(c => {
        const inZone = !opts.zone || c.zone === opts.zone;
        const ok = c.zone === 'edge' || (inZone && (opts.only !== 'problems' || isProblem(c)));
        if (!ok) return;
        for (let cur = c.id; cur && !keep.has(cur); cur = parent.has(cur) ? parent.get(cur).parent : null) keep.add(cur);
      });
    }

    const make = (id, depth) => {
      const c = byId.get(id), l = parent.get(id);
      const n = { id, kind: c.kind || 'unknown', title: c.title || '', subtitle: c.subtitle || '', status: c.status || 'unknown', grade: c.grade || 'unknown',
        metric: c.metric || '', link: l ? linkOf(l.trace) : null, children: [], depth, count: 0, problems: isProblem(c) ? 1 : 0, zone: c.zone || '' };
      (kids.get(id) || []).filter(k => !keep || keep.has(k)).forEach(k => {
        const ch = make(k, depth + 1);
        n.children.push(ch); n.count += 1 + ch.count; n.problems += ch.problems;
      });
      return n;
    };
    const rootIds = comps.filter(c => !parent.has(c.id) && (!keep || keep.has(c.id))).map(c => c.id);
    const rank = id => { const c = byId.get(id); return KIND_RANK[c.kind] != null ? KIND_RANK[c.kind] : 9; };
    rootIds.sort((a, b) => rank(a) - rank(b) || natural(byId.get(a).title || '', byId.get(b).title || ''));
    const all = rootIds.map(id => make(id, 1));
    const roots = all.filter(n => n.children.length || n.kind === 'internet');
    const loose = all.filter(n => !roots.includes(n));
    let other = null;
    if (loose.length) {
      other = { id: OTHER_ID, kind: 'group', title: '', subtitle: '', status: 'unknown', grade: 'unknown', metric: '', link: null, children: loose.map(n => reDepth(n, 2)),
        depth: 1, count: loose.length, problems: loose.reduce((s, n) => s + n.problems, 0), group: true };
    }
    const items = [];
    const walk = n => { items.push(n); n.children.forEach(walk); };
    roots.forEach(walk); if (other) walk(other);
    const real = items.filter(n => !n.group);
    return { roots, other, count: real.length, problems: real.filter(n => n.problems && (n.status === 'down' || n.grade === 'interrupted')).length, items };
  }
  function reDepth(n, depth) { n.depth = depth; n.children.forEach(c => reDepth(c, depth + 1)); return n; }

  // ---------------------------------------------------------------- DOM part
  const OPEN = new Map();             // id → true | false: what the person chose (kept while the tree is drawn again)
  const AUTO = new Map();             // id → what the first sight decided (stays stable when devices come and go)
  const STORE_KEY = 'll-tree-open';
  try { const raw = typeof localStorage !== 'undefined' && localStorage.getItem(STORE_KEY); if (raw) Object.entries(JSON.parse(raw)).forEach(([k, v]) => OPEN.set(k, !!v)); } catch (e) { /* no storage */ }
  const saveOpen = () => {
    try {
      const entries = [...OPEN.entries()].slice(-400);
      localStorage.setItem(STORE_KEY, JSON.stringify(Object.fromEntries(entries)));
    } catch (e) { /* private mode */ }
  };

  function decideAuto(roots, other) {
    // breadth first: open the branches level by level until the tree has grown to AUTO_OPEN_ROWS rows
    let rows = roots.length + (other ? 1 : 0), queue = roots.concat(other ? [other] : []);
    while (queue.length) {
      const next = [];
      for (const n of queue) {
        if (AUTO.has(n.id)) { if (AUTO.get(n.id)) { rows += 0; next.push(...n.children); } continue; }
        if (!n.children.length) continue;
        const open = n.id !== OTHER_ID && rows + n.children.length <= AUTO_OPEN_ROWS;
        AUTO.set(n.id, open);
        if (open) { rows += n.children.length; next.push(...n.children); }
      }
      queue = next;
    }
  }
  const isOpen = id => (OPEN.has(id) ? OPEN.get(id) : !!AUTO.get(id));

  function mount(host, o) {
    const doc = host.ownerDocument;
    const el = (tag, cls, text) => { const e = doc.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
    const S = {
      t: o.t, locale: o.locale, onSelect: o.onSelect || (() => {}), filter: o.filter || (() => ({})), view: o.view, dead: false,
      id: 'tr' + Math.random().toString(36).slice(2, 7), items: new Map(), selected: null, tab: null, typed: '', typedAt: 0, frameMetrics: {}, frameGrades: {}, chainFor: null
    };
    const t = (k, v) => S.t(k, v);
    const rootEl = el('div', 'll-tree');
    const summary = el('p', 'll-tree__summary');
    summary.id = 'tree-summary-' + Math.random().toString(36).slice(2, 8);
    const hint = el('p', 'll-tree__hint muted');
    const tree = el('ul', 'll-tree__list');
    tree.setAttribute('role', 'tree');
    tree.setAttribute('aria-label', t('tree.label'));
    tree.setAttribute('aria-describedby', summary.id);
    const empty = el('p', 'll-tree__empty muted');
    rootEl.append(summary, hint, tree, empty);
    host.appendChild(rootEl);

    const grade = g => (GRADES.includes(g) ? g : 'unknown');
    const linkText = l => {
      if (!l) return '';
      if (l.kind === 'uplink') return t('board.trace.uplink');
      const parts = [t('board.trace.' + l.kind)];
      if (l.kind === 'radio' && l.band) parts.push(t('tree.band.' + l.band.replace('.', '_')));
      if (l.kind === 'radio' && l.ssid) parts.push(l.ssid);
      if (l.port) parts.push(t('tree.port', { port: l.port }) + (l.own_port && l.kind !== 'radio' ? ' → ' + l.own_port : ''));
      return parts.join(' · ');
    };
    // what the state column shows: the live grade as a word, with the status in front when the device is offline (never colour alone)
    const stateShown = n => (n.status === 'down' ? t('board.status.down') + ' · ' : '') + t('board.grade.' + grade(n.grade));
    const metricOf = n => { const m = S.frameMetrics[n.id]; return m === undefined ? n.metric : (m == null ? '' : m); };
    // the name starts with the text of the row in reading order (a voice control user can say what is on screen), then adds what the picture says
    const labelOf = (n, metric) => {
      if (n.group) return t('tree.other.title') + ' ' + t('tree.other.sub') + ' ' + t('tree.other.count', { count: n.count });
      const sub = n.subtitle && n.subtitle !== n.title ? n.subtitle : t('board.kind.' + (n.kind || 'unknown'));
      const shown = [n.title || n.id, sub, n.link ? linkText(n.link) : '', metric, stateShown(n), n.children.length ? t('tree.children', { count: n.count }) : ''].filter(Boolean).join(' ');
      return shown + ', ' + t('board.kind.' + (n.kind || 'unknown')) + ', ' + t('board.status.' + (STATUSES.includes(n.status) ? n.status : 'unknown'));
    };
    const fmt = m => localiseMetric(String(m), S.locale);

    let uid = 0;
    function row(n) {
      // the tree pattern with a flat row: <li role=none> holds the treeitem (the row, which carries the focus and the name) and, beside it,
      // the group of its children (owned by the row). So the name of an element never swallows the names of everything below it.
      const li = el('li'); li.setAttribute('role', 'none');
      const r = el('div', 'tr-row');
      r.setAttribute('role', 'treeitem');
      r.id = S.id + '-' + (uid++);
      r.dataset.id = n.id; r.dataset.kind = n.kind; r.dataset.status = n.status; r.dataset.grade = grade(n.grade);
      r.tabIndex = -1;
      const tog = el('span', 'tr-tog'); tog.setAttribute('aria-hidden', 'true');
      const icon = el('span', 'tr-icon'); icon.setAttribute('aria-hidden', 'true');
      const Icons = window.Lanloom && window.Lanloom.Icons;
      if (!n.group && Icons) icon.innerHTML = Icons.svg(Icons.PATHS[n.kind] ? n.kind : 'unknown', 18);
      else if (n.group && Icons) icon.innerHTML = Icons.svg('unknown', 18);
      const main = el('span', 'tr-main');
      main.append(el('span', 'tr-title', n.group ? t('tree.other.title') : (n.title || n.id)), ' ');       // blanks between the parts: the text reads as words
      const sub = n.group ? t('tree.other.sub') : (n.subtitle && n.subtitle !== n.title ? n.subtitle : t('board.kind.' + (n.kind || 'unknown')));
      main.append(el('span', 'tr-sub', sub));
      const aside = el('span', 'tr-aside');
      const link = el('span', 'tr-link', n.link ? linkText(n.link) : '');
      if (n.link) link.dataset.kind = n.link.kind; else link.hidden = true;
      const metric = el('span', 'tr-metric'); metric.dataset.empty = '1';
      const state = el('span', 'tr-state');
      const dot = el('i', 'tr-dot'); dot.setAttribute('aria-hidden', 'true');
      const stext = el('span', 'tr-state__text', n.group ? '' : stateShown(n));
      state.append(dot, ' ', stext);
      if (n.group) state.hidden = true;
      const cnt = el('span', 'tr-count', n.children.length ? (n.group ? t('tree.other.count', { count: n.count }) : t('tree.children', { count: n.count })) : '');
      if (!n.children.length) cnt.hidden = true;
      aside.append(link, ' ', metric, ' ', state, ' ', cnt);
      if (n.problems && n.children.length) cnt.dataset.problems = String(n.problems);
      r.append(tog, icon, ' ', main, ' ', aside);
      li.appendChild(r);
      const e = { n, li, row: r, metric, stext, link, cnt, parent: null, group: null, sub };
      S.items.set(n.id, e);
      const m = metricOf(n);
      setMetric(e, m);
      r.setAttribute('aria-label', labelOf(n, m ? fmt(m) : ''));
      if (n.children.length) {
        const open = isOpen(n.id);
        r.setAttribute('aria-expanded', open ? 'true' : 'false');
        const g = el('ul', 'll-tree__group'); g.setAttribute('role', 'group'); g.hidden = !open;
        g.id = r.id + '-g';
        r.setAttribute('aria-owns', g.id);
        e.group = g;
        const set = n.children.length;
        n.children.forEach((c, i) => {
          const ce = row(c); ce.parent = e;
          ce.row.setAttribute('aria-posinset', String(i + 1)); ce.row.setAttribute('aria-setsize', String(set));
          ce.row.setAttribute('aria-level', String(n.depth + 1));
          g.appendChild(ce.li);
        });
        li.appendChild(g);
      }
      return e;
    }
    function setMetric(e, m) {
      const text = m ? fmt(m) : '';
      if (e.metric.textContent !== text) e.metric.textContent = text;
      e.metric.dataset.empty = text ? '' : '1';
      e.metric.hidden = !text;
    }

    function render() {
      const active = doc.activeElement && tree.contains(doc.activeElement) ? doc.activeElement.dataset.id : null;
      const scroll = rootEl.scrollTop;
      const f = S.filter() || {};
      const b = build(S.view, f);
      decideAuto(b.roots, b.other);
      S.items.clear();
      const tops = b.roots.concat(b.other ? [b.other] : []);
      const frag = doc.createDocumentFragment();
      tops.forEach((n, i) => {
        const e = row(n);
        e.row.setAttribute('aria-posinset', String(i + 1)); e.row.setAttribute('aria-setsize', String(tops.length));
        e.row.setAttribute('aria-level', '1');
        frag.appendChild(e.li);
      });
      tree.replaceChildren(frag);
      S.built = b;
      const probs = b.problems;
      summary.textContent = b.count ? t('tree.summary', { count: b.count }) + (probs ? ' ' + t('tree.summary_problems', { count: probs }) : '') : '';
      hint.textContent = b.count ? t('tree.hint') : '';
      empty.textContent = b.count ? '' : t(f.only || f.zone ? 'mapfilter.none' : 'tree.empty');
      empty.hidden = !!b.count;
      // the one tab stop: the item that had it, else the selected one, else the first
      const tabId = S.tab && S.items.has(S.tab) ? S.tab : (S.selected && S.items.has(S.selected) ? S.selected : (tops[0] && tops[0].id));
      setTab(tabId);
      if (S.selected) mark(S.selected);
      rootEl.scrollTop = scroll;
      if (active && S.items.has(active) && visible(S.items.get(active))) S.items.get(active).row.focus({ preventScroll: true });
    }

    const visible = e => { for (let p = e.parent; p; p = p.parent) if (!isOpen(p.n.id)) return false; return true; };
    function setTab(id) {
      if (S.tab && S.items.has(S.tab)) S.items.get(S.tab).row.tabIndex = -1;
      S.tab = id;
      if (id && S.items.has(id)) S.items.get(id).row.tabIndex = 0;
    }
    function mark(id) {
      S.items.forEach(e => e.row.removeAttribute('aria-selected'));
      const e = S.items.get(id);
      if (e) e.row.setAttribute('aria-selected', 'true');
    }
    function setOpen(e, open, remember) {
      if (!e.group || isOpen(e.n.id) === open) return;
      OPEN.set(e.n.id, open); if (remember !== false) saveOpen();
      e.row.setAttribute('aria-expanded', open ? 'true' : 'false');
      e.group.hidden = !open;
    }
    function reveal(id) {
      const e = S.items.get(id); if (!e) return null;
      for (let p = e.parent; p; p = p.parent) setOpen(p, true, false);
      saveOpen();
      return e;
    }
    const flat = () => [...S.items.values()].filter(visible);
    function go(e) { if (!e) return; setTab(e.n.id); e.row.focus(); e.row.scrollIntoView({ block: 'nearest' }); }
    function activate(e) {
      if (e.n.group) { setOpen(e, !isOpen(e.n.id)); return; }
      S.selected = e.n.id; mark(e.n.id); setTab(e.n.id);
      S.onSelect(e.n.id);
    }

    // ---- chain view: the way from the focused element up to the Internet is lit (the partners of the chain), also by focus
    function chain(id) {
      if (S.chainFor === id) return;
      S.chainFor = id;
      S.items.forEach(e => { e.row.classList.remove('is-chain', 'is-self'); });
      const e = id && S.items.get(id);
      if (!e) return;
      e.row.classList.add('is-self');
      for (let p = e.parent; p; p = p.parent) p.row.classList.add('is-chain');
      e.n.children.forEach(c => { const ce = S.items.get(c.id); if (ce && visible(ce)) ce.row.classList.add('is-chain'); });
    }
    tree.addEventListener('focusin', ev => { const li = ev.target.closest && ev.target.closest('[role=treeitem]'); if (li && ev.target === li) { chain(li.dataset.id); setTab(li.dataset.id); } });
    tree.addEventListener('focusout', ev => { if (!ev.relatedTarget || !tree.contains(ev.relatedTarget)) chain(null); });
    tree.addEventListener('mouseover', ev => { const li = ev.target.closest && ev.target.closest('[role=treeitem]'); if (li && !tree.contains(doc.activeElement)) chain(li.dataset.id); });
    tree.addEventListener('mouseleave', () => { if (!tree.contains(doc.activeElement)) chain(null); });

    tree.addEventListener('click', ev => {
      const r = ev.target.closest('.tr-row'); if (!r) return;
      const li = r, e = S.items.get(li.dataset.id); if (!e) return;
      if (ev.target.closest('.tr-tog')) { setOpen(e, !isOpen(e.n.id)); setTab(e.n.id); li.focus({ preventScroll: true }); return; }
      li.focus({ preventScroll: true });
      activate(e);
    });
    tree.addEventListener('keydown', ev => {
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      const li = ev.target; if (!li || li.getAttribute('role') !== 'treeitem') return;
      const e = S.items.get(li.dataset.id); if (!e) return;
      const list = flat(), i = list.indexOf(e);
      let handled = true;
      switch (ev.key) {
        case 'ArrowDown': go(list[Math.min(list.length - 1, i + 1)]); break;
        case 'ArrowUp': go(list[Math.max(0, i - 1)]); break;
        case 'Home': go(list[0]); break;
        case 'End': go(list[list.length - 1]); break;
        case 'ArrowRight':
          if (e.group && !isOpen(e.n.id)) setOpen(e, true);
          else if (e.group) go(S.items.get(e.n.children[0].id));
          break;
        case 'ArrowLeft':
          if (e.group && isOpen(e.n.id)) setOpen(e, false);
          else if (e.parent) go(e.parent);
          break;
        case 'Enter': case ' ': activate(e); break;
        case '*': (e.parent ? e.parent.n.children : S.built.roots.concat(S.built.other ? [S.built.other] : [])).forEach(c => { const ce = S.items.get(c.id); if (ce) setOpen(ce, true); }); break;
        default: {
          if (ev.key.length === 1 && /\S/.test(ev.key)) {      // typing a letter jumps to the next element whose name starts with it
            const now = Date.now();
            S.typed = (now - S.typedAt < 700 ? S.typed : '') + ev.key.toLowerCase(); S.typedAt = now;
            const names = list.map(x => (x.n.group ? t('tree.other.title') : (x.n.title || x.n.id)).toLowerCase());
            let hit = -1;
            for (let k = 1; k <= list.length && hit < 0; k++) { const j = (i + k) % list.length; if (names[j].startsWith(S.typed)) hit = j; }
            if (hit >= 0) go(list[hit]);
          } else handled = false;
        }
      }
      if (handled) ev.preventDefault();
    });

    render();
    return {
      update(view) { if (S.dead) return; S.view = view; render(); },
      live(frame) {
        if (S.dead || !frame) return;
        if (frame.metrics) for (const id in frame.metrics) {
          S.frameMetrics[id] = frame.metrics[id];
          const e = S.items.get(id); if (!e) continue;
          const m = frame.metrics[id] == null ? '' : frame.metrics[id];
          const text = m ? fmt(m) : '';
          if (e.metric.textContent === text) continue;
          setMetric(e, m);
          e.row.setAttribute('aria-label', labelOf(e.n, text));
        }
        if (frame.grades) for (const id in frame.grades) {
          const g = grade(frame.grades[id]);
          S.frameGrades[id] = g;
          const e = S.items.get(id); if (!e || e.n.grade === g) continue;
          e.n.grade = g; e.row.dataset.grade = g;
          e.stext.textContent = stateShown(e.n);
          e.row.setAttribute('aria-label', labelOf(e.n, e.metric.textContent));
        }
      },
      highlight(id) {
        S.selected = id || null;
        if (!id) { mark(null); return; }
        const e = reveal(id);
        if (!e) { mark(null); return; }
        mark(id);
        if (!tree.contains(doc.activeElement)) setTab(id);
        e.row.scrollIntoView({ block: 'nearest' });
      },
      focus() { const e = S.tab && S.items.get(S.tab); if (e) e.row.focus({ preventScroll: true }); },
      setTheme() { /* the tree uses the page's colour tokens */ },
      destroy() { S.dead = true; rootEl.remove(); S.items.clear(); }
    };
  }

  return { build, mount, natural, OTHER_ID };
});
