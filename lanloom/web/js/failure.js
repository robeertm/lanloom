/* Lanloom — failure chain (S5): "What fails if … fails?", "What does … depend on?", "Simulate maintenance …" and the outage automatic.
   Data: GET /api/failure/<id>?dir=down|up (the server builds the graph, lanloom/failure.py), GET /api/failure/outages,
   GET|PUT /api/failure/deps/<id> (manual dependencies: hard, soft or none), GET /api/failure/board?view= (manual edges for the hover).
   On the map a bar above the board shows the numbers, the direction and a list by stages; the board lights the chain with stage marks
   (lanloom/web/board/board.js setChain) - only classes on the tiles and traces that are there, nothing is routed again. Simulating
   maintenance is the same chain "down" coloured by certainty (sure: stops - red, likely: impaired - yellow) with an end button; it is
   only shown, nothing is switched and no state changes. A real outage marks the probably affected parts dashed and shows a note
   above the board (names wrap, never cut off); the note rests while a chain is open and can be hidden until the outage changes.
   Phones get the same bar as a sheet with the list open. Palette: three commands (pick a part in a second step) and actions on hits.
   Interface: LL.failure.show(id, dir), .simulate(id), .close(), .attach(map) (views.js), .loadOutages(), .outageOf(id),
   .cardHint(id) and LL.failureSection(id) (the card). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const F = LL.failure = {};
  const KIND_ICON = { device: null, node: 'switch', container: 'container', connection: 'service', hub: 'hub', hub_device: 'iot', automation: 'service',
    script: 'service', entity: 'iot', internet: 'internet' };
  const PARTS = ['devices', 'hub_devices', 'automations', 'entities', 'connections'];
  const HIDE_KEY = 'll-fc-outage-hidden';
  const S = { id: null, dir: 'down', sim: false, d: null, busy: false, err: null, nr: 0, list: false, open: new Set([1, 2]),
    map: null, outage: null, outT: 0, outBusy: false, needs: null, needsKey: '' };
  const phone = () => window.matchMedia && window.matchMedia('(max-width: 760px)').matches;
  const enc = id => encodeURIComponent(id);

  // ------------------------------------------------------------------ names and texts
  function nodeName(n) {
    if (!n) return '';
    if (n.self) return t('kind.self');
    if (n.kind === 'internet') return t('kind.internet');
    return n.title || n.ip || n.entity_id || (n.dev_kind ? t('kind.' + n.dev_kind) : n.id);
  }
  F.nodeName = nodeName;
  function kindLabel(n) {
    if (n.self) return t('fail.kind.device');
    if (n.kind === 'device' || n.kind === 'node') return t('kind.' + (n.dev_kind || 'unknown'));
    return t('fail.kind.' + n.kind);
  }
  function icon(n) { return LL.kindIcon(n.kind === 'device' || n.kind === 'node' ? (n.self ? 'self' : n.dev_kind || 'unknown') : (KIND_ICON[n.kind] || 'unknown')); }
  // one edge "a depends on b" in words, b named
  function edgeText(why, b) {
    const w = why || {}, v = w.vars || {};
    const s = t(w.key === 'port' && v.port ? 'fail.why.port_n' : 'fail.why.' + (w.key || 'port'), { b, port: v.port || '' });
    return v.note ? s + ' – ' + v.note : s;
  }
  // the same edge in short, where the name of b is already shown next to it
  function howText(why) {
    const w = why || {}, v = w.vars || {};
    const s = t(w.key === 'port' && v.port ? 'fail.how.port_n' : 'fail.how.' + (w.key || 'port'), { port: v.port || '' });
    return v.note ? s + ' – ' + v.note : s;
  }
  // why a part is in the chain: down - it depends on the part before it; up - the part before it depends on it
  function whyText(item, dir) {
    const names = item.path_names || [];
    const prev = names.length > 1 ? names[names.length - 2] : '';
    return dir === 'up' ? t('fail.needed_by', { name: prev, how: howText(item.why) }) : edgeText(item.why, prev);
  }
  F.whyText = whyText;
  function partsText(c) {
    return PARTS.filter(p => c[p]).map(p => t('fail.part.' + p, { n: c[p] })).join(' · ');
  }
  // where a part of the chain sits on the board that is shown: its first tile that exists there
  function tileOf(item, ids) { return (item.tiles || []).find(x => ids.has(x)) || null; }
  function routeFor(n) { return n && ['hub_device', 'entity', 'automation', 'script'].includes(n.kind) ? 'map' : 'network'; }
  function nodeLink(n) {
    if (n.kind === 'device') return '#/devices/' + enc(n.id);
    if (n.kind === 'automation' || n.kind === 'script') return n.integ_kind ? null : (n.entity_id ? '#/automations/' + enc(String(n.id).split(':')[1]) + '/item/' + enc(n.entity_id) : null);
    const tile = (n.tiles || [])[0];
    return tile ? '#/' + routeFor(n) + '/' + enc(tile) : null;
  }

  // ------------------------------------------------------------------ the map: attach, apply to the board
  F.attach = function (m) {
    S.map = m;
    loadNeeds();
    F.loadOutages();
    const want = LL.query && LL.query.get('fc');
    if (want) {
      const dir = LL.query.get('fcd') || 'down';
      LL.query.delete('fc'); LL.query.delete('fcd');
      F.show(want, dir === 'sim' ? 'down' : dir, dir === 'sim' ? { sim: true } : undefined);
    } else render();
    return () => { if (S.map === m) { S.map = null; } };
  };
  F.refresh = function () { apply(); render(); loadNeeds(); };
  F.onFrame = function (f) {
    const sig = (f.structure || 0) + ':' + (f.mapver || 0);
    if (sig !== S.frameSig) { S.frameSig = sig; F.loadOutages(); }
  };
  function board() { const b = S.map && S.map.board(); return b && b.setChain ? b : null; }
  function viewIds() { const v = S.map && S.map.view(); return new Set(v ? v.components.map(c => c.id) : []); }
  async function loadNeeds() {
    const b = board(); if (!b || !S.map) return;
    const kind = S.map.kind === 'network' ? 'network' : 'overview';
    const v = S.map.view(), key = kind + ':' + (v ? v.structure : '');
    if (key === S.needsKey && S.needs) { b.setNeeds(S.needs); return; }
    S.needsKey = key;
    try { const r = await api('/api/failure/board?view=' + kind); S.needs = r.extra || []; const b2 = board(); if (b2) b2.setNeeds(S.needs); }
    catch (e) { S.needs = []; }
  }
  function apply() {
    const b = board(); if (!b) return;
    const ids = viewIds();
    if (S.id && S.d) {
      const tiles = {};
      S.d.items.forEach(it => {
        const x = tileOf(it, ids); if (!x) return;
        // the hub side of a device drawn as one tile with it: when only that side fails, the device itself runs on (impaired)
        const cert = it.side === 'hub' && S.dir === 'down' ? 'likely' : it.cert;
        const old = tiles[x];
        if (!old || (cert === 'sure' && old.cert !== 'sure') || (cert === old.cert && it.stage < old.stage)) tiles[x] = { stage: it.stage, cert };
      });
      const root = tileOf(S.d.root, ids);
      if (root) delete tiles[root];
      b.setChain({ root, mode: S.sim ? 'sim' : 'chain', tiles });
      b.setOutage(null);
      if (S.scroll && root && S.map.slot) {          // the root comes into view once per chain (not on every live update)
        S.scroll = false;
        const el = S.map.slot().parentElement && S.map.slot().parentElement.querySelector('.ll-tile[data-id="' + (window.CSS && CSS.escape ? CSS.escape(root) : root) + '"]');
        if (el) el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      }
      return;
    }
    b.setChain(null);
    if (outageShown()) {
      const tiles = {};
      S.outage.items.forEach(it => { const x = tileOf({ tiles: (it.tiles || []).slice(0, it.own == null ? undefined : it.own) }, ids); if (x) tiles[x] = 1; });
      S.outage.outages.forEach(o => { const x = tileOf(o, ids); if (x) delete tiles[x]; });
      b.setOutage({ tiles });
    } else b.setOutage(null);
  }

  // ------------------------------------------------------------------ public: show, simulate, close
  F.show = async function (id, dir, opt) {
    if (!id) return;
    const sim = !!(opt && opt.sim);
    dir = dir === 'up' && !sim ? 'up' : 'down';
    if (!S.map) {                                    // not on a map: go to the board the part lives on, the chain opens there
      const q = '?fc=' + enc(id) + '&fcd=' + (sim ? 'sim' : dir);
      let route = 'network';
      try { const r = await api('/api/failure/' + enc(id) + '?dir=' + dir); route = routeFor(r.root); } catch (e) { /* the map answers again */ }
      LL.go('#/' + route + q);
      return;
    }
    const nr = ++S.nr;
    if (S.id !== id || S.sim !== sim) { S.open = new Set([1, 2]); S.list = phone(); }
    Object.assign(S, { id, dir, sim, busy: true, err: null });
    render();
    let d;
    try { d = await api('/api/failure/' + enc(id) + '?dir=' + dir); }
    catch (e) { if (nr === S.nr) { S.busy = false; S.d = null; S.err = e.status === 404 ? t('fail.err.unknown') : LL.errText(e); apply(); render(); } return; }
    if (nr !== S.nr) return;
    S.d = d; S.busy = false; S.scroll = true;
    render(); apply();
    LL.announce && LL.announce(titleText() + '. ' + countText(), { now: true });
  };
  F.simulate = id => F.show(id, 'down', { sim: true });
  F.close = function () {
    S.nr++;
    Object.assign(S, { id: null, d: null, busy: false, err: null, sim: false });
    apply(); render();
  };
  F.active = () => !!S.id;
  F.data = () => S.d;                 // test hook
  F.state = () => ({ id: S.id, dir: S.dir, sim: S.sim, list: S.list });

  // ------------------------------------------------------------------ the bar above the board (a sheet on phones)
  function titleText() {
    const name = S.d ? nodeName(S.d.root) : '…';
    return t(S.sim ? 'fail.title.sim' : S.dir === 'up' ? 'fail.title.up' : 'fail.title.down', { name });
  }
  function countText() {
    if (!S.d) return '';
    const c = S.d.counts;
    if (!c.total) return t(S.dir === 'up' ? 'fail.none.up' : 'fail.none.down');
    const a = t(S.sim ? 'fail.count.stops' : S.dir === 'up' ? 'fail.count.required' : 'fail.count.sure', { n: c.sure });
    const b = c.likely ? t(S.sim ? 'fail.count.impaired' : S.dir === 'up' ? 'fail.count.partly' : 'fail.count.likely', { n: c.likely }) : '';
    return [a, b].filter(Boolean).join(' · ');
  }
  function legend() {
    const pair = S.sim ? ['fail.legend.stops', 'fail.legend.impaired'] : S.dir === 'up' ? ['fail.legend.required', 'fail.legend.partly'] : ['fail.legend.sure', 'fail.legend.likely'];
    return h('span', { class: 'fc-legend' }, h('span', { class: 'fc-key fc-key--sure' }, t(pair[0])), h('span', { class: 'fc-key fc-key--likely' }, t(pair[1])));
  }
  function render() {
    const host = S.map && S.map.slot && S.map.slot();
    if (!host) return;
    renderOutage(host);
    let bar = host.querySelector('.fc-bar');
    if (host.parentElement) host.parentElement.classList.toggle('fc-sim', !!(S.id && S.sim));
    if (!S.id) { if (bar) bar.remove(); host.classList.remove('has-fc'); return; }
    const fresh = !bar;
    if (fresh) bar = h('section', { class: 'fc-bar', role: 'region', 'aria-label': t('fail.region') });
    bar.classList.toggle('fc-bar--sim', S.sim);
    bar.classList.toggle('is-list', S.list);
    const dirSeg = S.sim ? null : h('div', { class: 'seg fc-seg', role: 'group', 'aria-label': t('fail.dir.label') },
      ['down', 'up'].map(d => h('button', { class: 'seg__btn' + (S.dir === d ? ' is-active' : ''), type: 'button', 'aria-pressed': S.dir === d ? 'true' : 'false',
        onclick: () => { if (S.dir !== d) F.show(S.id, d); } }, t('fail.dir.' + d))));
    const listBtn = S.d && S.d.counts.total ? h('button', { class: 'btn btn--ghost btn--sm fc-listbtn', type: 'button', 'aria-expanded': S.list ? 'true' : 'false',
      onclick: () => { S.list = !S.list; render(); } }, t(S.list ? 'fail.list.hide' : 'fail.list.show')) : null;
    const close = h('button', { class: S.sim ? 'btn btn--sm fc-end' : 'btn btn--ghost btn--icon btn--sm fc-close', type: 'button', 'aria-label': t(S.sim ? 'fail.sim.end' : 'fail.close'),
      title: t(S.sim ? 'fail.sim.end' : 'fail.close'), onclick: () => F.close() }, S.sim ? t('fail.sim.end') : '✕');
    const head = h('div', { class: 'fc-bar__head' },
      h('div', { class: 'fc-bar__title' }, h('span', { class: 'fc-bar__icon', 'aria-hidden': 'true' }), h('h2', {}, titleText())),
      h('div', { class: 'fc-bar__tools' }, dirSeg, listBtn, close));
    const sub = [];
    if (S.busy) sub.push(h('p', { class: 'fc-bar__line muted' }, t('fail.loading')));
    else if (S.err) sub.push(h('p', { class: 'fc-bar__line msg msg--warn' }, S.err));
    else if (S.d) {
      sub.push(h('p', { class: 'fc-bar__line' }, h('strong', {}, countText()), S.d.counts.total ? h('span', { class: 'muted' }, ' · ' + partsText(S.d.counts)) : null));
      if (S.d.counts.total) sub.push(legend());
      if (S.sim) sub.push(h('p', { class: 'fc-bar__note fineprint' }, t('fail.sim.note')));
    }
    const body = h('div', { class: 'fc-bar__body' }, sub);
    const kids = [head, body];
    if (S.list && S.d && S.d.counts.total) kids.push(listEl());
    bar.replaceChildren(...kids);
    if (fresh) host.prepend(bar);
    host.classList.add('has-fc');
  }
  function listEl() {
    const groups = new Map();
    S.d.items.forEach(it => { if (!groups.has(it.stage)) groups.set(it.stage, []); groups.get(it.stage).push(it); });
    const out = h('div', { class: 'fc-list' });
    groups.forEach((items, stage) => {
      const open = S.open.has(stage);
      const headBtn = h('button', { class: 'fc-stage-head', type: 'button', 'aria-expanded': open ? 'true' : 'false',
        onclick: () => { if (S.open.has(stage)) S.open.delete(stage); else S.open.add(stage); render(); } },
        h('span', { class: 'fc-stage-mark' }, String(stage)), h('span', {}, t('fail.stage', { n: stage })), h('span', { class: 'muted' }, ' · ' + t('fail.stage.n', { n: items.length })));
      const sec = h('div', { class: 'fc-stage' + (open ? ' is-open' : '') }, headBtn);
      if (open) sec.append(h('ul', { class: 'fc-rows' }, items.map(rowEl)));
      out.append(sec);
    });
    const more = Object.values(S.d.more || {}).reduce((a, b) => a + b, 0);
    if (more) out.append(h('p', { class: 'muted fc-more' }, t('fail.more', { n: more })));
    return out;
  }
  function rowEl(it) {
    const href = nodeLink(it);
    const name = nodeName(it);
    return h('li', { class: 'fc-row fc-row--' + it.cert },
      icon(it),
      h('span', { class: 'fc-row__main' },
        href ? h('a', { class: 'fc-row__name', href }, name) : h('span', { class: 'fc-row__name' }, name),
        h('span', { class: 'fc-row__why' }, kindLabel(it) + ' · ' + whyText(it, S.dir))),
      h('span', { class: 'chip chip--' + (it.cert === 'sure' ? (S.sim ? 'danger' : 'accent') : 'warn') + ' fc-row__cert' },
        t(S.sim ? (it.cert === 'sure' ? 'fail.cert.stops' : 'fail.cert.impaired') : S.dir === 'up' ? (it.cert === 'sure' ? 'fail.cert.required' : 'fail.cert.partly') : 'fail.cert.' + it.cert)));
  }

  // ------------------------------------------------------------------ the outage automatic
  F.loadOutages = async function (force) {
    if (S.outBusy) return S.outage;
    if (!force && S.outage && Date.now() - S.outT < 4000) { apply(); render(); return S.outage; }
    S.outBusy = true;
    try { S.outage = await api('/api/failure/outages'); S.outT = Date.now(); }
    catch (e) { /* keep the last answer */ }
    S.outBusy = false;
    apply(); render();
    document.dispatchEvent(new CustomEvent('ll-outage'));
    return S.outage;
  };
  const outSig = () => (S.outage ? S.outage.outages.map(o => o.id).join(',') : '');
  function hiddenSig() { try { return sessionStorage.getItem(HIDE_KEY) || ''; } catch (e) { return ''; } }
  function outageShown() { return !!(S.outage && S.outage.items.length && S.outage.outages.length && hiddenSig() !== outSig()); }
  function rootNames(roots) {
    const names = roots.map(nodeName);
    try { return new Intl.ListFormat(LL.locale(), { type: 'conjunction' }).format(names.slice(0, 6)) + (names.length > 6 ? ' …' : ''); }
    catch (e) { return names.join(', '); }
  }
  function renderOutage(host) {
    let note = host.querySelector('.fc-outage');
    if (S.id || !outageShown()) { if (note) note.remove(); return; }
    const o = S.outage, roots = o.outages.filter(r => r.counts && r.counts.total);
    if (!roots.length) { if (note) note.remove(); return; }
    const fresh = !note;
    if (fresh) note = h('section', { class: 'fc-outage', role: 'status', 'aria-label': t('fail.out.label') });
    const c = o.counts;
    const text = [t('fail.out.down', { names: rootNames(roots), n: roots.length }), t('fail.out.affected', { n: c.total })];
    if (c.automations) text.push(t('fail.out.autos', { n: c.automations }));
    note.replaceChildren(
      h('span', { class: 'fc-outage__icon', 'aria-hidden': 'true' }),
      h('div', { class: 'fc-outage__text' }, h('strong', { class: 'fc-outage__title' }, t('fail.out.title', { n: roots.length })), ' ', h('span', {}, text.join(' '))),
      h('div', { class: 'fc-outage__tools' },
        h('button', { class: 'btn btn--sm', type: 'button', onclick: () => F.show(roots[0].id, 'down') }, t('fail.out.show')),
        h('button', { class: 'btn btn--ghost btn--icon btn--sm', type: 'button', 'aria-label': t('fail.out.close'), title: t('fail.out.close'),
          onclick: () => { try { sessionStorage.setItem(HIDE_KEY, outSig()); } catch (e) { /* private mode */ } apply(); render(); } }, '✕')));
    if (fresh) host.prepend(note);
  }
  // which outage(s) a part probably suffers from: [root nodes] or null
  F.outageOf = function (id) {
    if (!S.outage || !S.outage.items.length) return null;
    const it = S.outage.items.find(x => x.id === id || (x.tiles || [])[0] === id);
    if (!it) return null;
    const roots = (it.causes || []).map(r => S.outage.outages.find(o => o.id === r)).filter(Boolean);
    return roots.length ? { roots, item: it } : null;
  };
  F.outageText = function (id) {
    const o = F.outageOf(id);
    return o ? t('fail.hint', { names: rootNames(o.roots), n: o.roots.length }) : '';
  };
  F.cardHint = function (id) {
    const text = F.outageText(id);
    if (!text) return null;
    const o = F.outageOf(id);
    return h('p', { class: 'msg msg--warn fc-hint' }, h('span', {}, text), ' ',
      h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => F.show(o.roots[0].id, 'down') }, t('fail.out.show')));
  };

  // ------------------------------------------------------------------ the card: "Dependencies"
  let candidates = null;
  async function loadCandidates() {
    if (!candidates) candidates = api('/api/failure/candidates').then(r => r.items).catch(() => { candidates = null; return []; });
    return candidates;
  }
  LL.failureSection = function (id) {
    const box = h('div', { class: 'fc-card' }, h('p', { class: 'muted' }, t('fail.loading')));
    const sec = h('section', { class: 'detail__sec fc-sec', 'data-sec': 'dependencies' }, h('h3', {}, t('fail.sec.title')), box);
    async function load() {
      let d;
      try { d = await api('/api/failure/deps/' + enc(id)); }
      catch (e) { if (e.status === 404) sec.remove(); else box.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!sec.isConnected && !box.isConnected) return;
      draw(d);
    }
    function draw(d) {
      const node = d.node.id;
      const kids = [h('p', { class: 'muted fc-card__lead' }, t('fail.sec.lead')),
        h('div', { class: 'fc-card__btns' },
          h('button', { class: 'btn btn--sm', type: 'button', onclick: () => F.show(node, 'down') }, t('fail.btn.down')),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => F.show(node, 'up') }, t('fail.btn.up')))];
      kids.push(h('h4', { class: 'fc-card__h' }, t('fail.sec.up')));
      if (!d.up.length) kids.push(h('p', { class: 'muted' }, t('fail.sec.up_none')));
      else kids.push(h('ul', { class: 'fc-rows fc-rows--card' }, d.up.map(u => {
        const href = nodeLink(u);
        return h('li', { class: 'fc-row fc-row--' + u.why.cert }, icon(u),
          h('span', { class: 'fc-row__main' }, href ? h('a', { class: 'fc-row__name', href }, nodeName(u)) : h('span', { class: 'fc-row__name' }, nodeName(u)),
            h('span', { class: 'fc-row__why' }, kindLabel(u) + ' · ' + howText(u.why))),
          h('span', { class: 'chip chip--' + (u.why.cert === 'sure' ? 'accent' : 'warn') + ' fc-row__cert' }, t(u.why.cert === 'sure' ? 'fail.cert.required' : 'fail.cert.partly')));
      })));
      if (d.up_more) kids.push(h('p', { class: 'muted' }, t('fail.more', { n: d.up_more })));
      kids.push(h('p', { class: 'muted fc-card__down' }, d.down_n ? t('fail.sec.down_n', { n: d.down_n }) : t('fail.sec.down_none')));
      if (d.manual.length || (d.manual_ok && LL.can('operate'))) kids.push(h('h4', { class: 'fc-card__h' }, t('fail.sec.manual')));
      if (d.manual.length) kids.push(h('ul', { class: 'fc-man' }, d.manual.map(m => h('li', { class: 'fc-man__row' },
        h('span', { class: 'chip chip--' + ({ hard: 'danger', soft: 'warn', none: 'muted' }[m.k] || 'muted') }, t('fail.man.chip.' + m.k)),
        h('span', { class: 'fc-man__main' }, h('span', { class: 'fc-man__name' }, m.node ? nodeName(m.node) : t('fail.man.missing', { id: m.target })),
          m.note ? h('span', { class: 'muted fc-man__note' }, m.note) : null),
        LL.can('operate') ? h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => save(node, m.target, null) }, t('fail.man.remove')) : null))));
      if (d.manual_ok && LL.can('operate')) kids.push(editor(node));
      box.replaceChildren(...kids);
    }
    function editor(node) {
      const target = h('select', { class: 'select select--sm fc-ed__target', 'aria-label': t('fail.man.target') }, h('option', { value: '' }, t('fail.man.pick')));
      const kind = h('select', { class: 'select select--sm fc-ed__kind', 'aria-label': t('fail.man.kind') },
        ['hard', 'soft', 'none'].map(k => h('option', { value: k }, t('fail.man.kind.' + k))));
      const note = h('input', { class: 'input-sm fc-ed__note', type: 'text', maxlength: '120', placeholder: t('fail.man.note'), 'aria-label': t('fail.man.note') });
      const btn = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => { if (target.value) save(node, target.value, kind.value, note.value); } }, t('fail.man.save'));
      loadCandidates().then(items => {
        items.filter(c => c.id !== node).forEach(c => target.append(h('option', { value: c.id }, nodeName(c) + (c.ip && c.title ? ' · ' + c.ip : '') + ' (' + kindLabel(c) + ')')));
      });
      return h('div', { class: 'fc-ed' },
        h('div', { class: 'fc-ed__row' }, h('label', { class: 'fc-ed__label' }, t('fail.man.target')), target),
        h('div', { class: 'fc-ed__row' }, h('label', { class: 'fc-ed__label' }, t('fail.man.kind')), kind),
        h('div', { class: 'fc-ed__row' }, h('label', { class: 'fc-ed__label' }, t('fail.man.note_label')), note),
        h('p', { class: 'fineprint fc-ed__help' }, t('fail.man.help')),
        h('div', { class: 'fc-ed__foot' }, btn));
    }
    async function save(node, target, kind, note) {
      try {
        await api('/api/failure/deps/' + enc(node), { method: 'PUT', body: { target, kind: kind || null, note: note || '' } });
        LL.toast(t(kind ? 'fail.man.saved' : 'fail.man.removed'), 'ok');
        S.needsKey = ''; loadNeeds();
        if (S.id) F.show(S.id, S.dir, S.sim ? { sim: true } : undefined);
        load();
      } catch (e) { LL.toast(LL.errText(e), 'error'); }
    }
    load();
    return sec;
  };

  // ------------------------------------------------------------------ palette: three commands (a part is picked in a second step) and actions
  const hitNode = hit => (hit.kind === 'device' ? hit.id : (hit.integ != null && hit.entity_id ? 'he:' + hit.integ + ':' + hit.entity_id : null));
  const pick = (label, run) => () => LL.openPalette('', { pick: { label, kinds: ['devices', 'automations', 'entities'], run: hit => { const n = hitNode(hit); if (n) run(n); } } });
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'failure-chain', group: 'commands', order: 24, label: 'fail.cmd.chain', keywords: () => t('fail.cmd.kw'), run: pick('fail.pick.chain', n => F.show(n, 'down')) });
    LL.registerCommand({ id: 'failure-depends', group: 'commands', order: 25, label: 'fail.cmd.depends', keywords: () => t('fail.cmd.kw'), run: pick('fail.pick.depends', n => F.show(n, 'up')) });
    LL.registerCommand({ id: 'failure-maintenance', group: 'commands', order: 26, label: 'fail.cmd.sim', keywords: () => t('fail.cmd.kw'), run: pick('fail.pick.sim', n => F.simulate(n)) });
  }
  if (LL.registerAction) {
    LL.registerAction({ id: 'failure-chain', kinds: ['device', 'automation', 'entity'], order: 33, label: 'fail.act.chain', available: hit => !!hitNode(hit), run: hit => F.show(hitNode(hit), 'down') });
    LL.registerAction({ id: 'failure-depends', kinds: ['device', 'automation', 'entity'], order: 34, label: 'fail.act.depends', available: hit => !!hitNode(hit), run: hit => F.show(hitNode(hit), 'up') });
  }
  window.addEventListener('hashchange', () => { if (S.id && !/^#\/(map|network)\b/.test(location.hash)) { S.id = null; S.d = null; S.sim = false; } });
})();
