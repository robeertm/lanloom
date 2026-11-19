/* Lanloom — data paths: network routes (DW2).

   Which networks and places the data passes on the way to the most important destinations:
     Home → Provider → Exchange point → Transit … → Destination network → Destination,
   with the round-trip time added by each section. Places from router names and exchange points are sure; places from the
   city data or only a country are approximate: they read "≈ City", the line towards them is dashed and called "estimated".
   Data: `routes` of GET /api/paths (all) and GET /api/paths/country/<CC> (towards one country), condensed on the server: no
   router address, no host name, no destination address. The option is off by default (Settings > General > Network routes).
   Hooks: LL.pathsRoutes.card(d, opts) (section), LL.pathsRoutes.drawOn(svg, routes, opts) (line on a map, returns {scale, select}),
   LL.pathsRoutesSettingsCard(s) (settings). No inline handler, colours only from tokens, calm with reduced motion. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const NS = 'http://www.w3.org/2000/svg';
  const R = LL.pathsRoutes = {};
  const St = { sel: null, hover: null, maps: new Set() };

  const ms = v => (v == null ? '' : t('routes.ms', { v: v < 1 ? '<1' : LL.fmtNum(Math.round(v), 0) }));
  const cname = cc => (cc ? (LL.countryName ? LL.countryName(cc) : cc) : '');
  /* estimated: a place from the city data or only the country; the destination without a place of its own is drawn where its network is */
  const unsure = s => s.kind !== 'home' && (s.place ? !s.place.sure : !!(s.cc && s.kind !== 'target'));

  function nodeText(s, homeCc) {
    if (s.kind === 'home') return { title: t('routes.node.home'), sub: cname(s.cc || homeCc) };
    if (s.kind === 'target' && s.anycast) return { title: t('routes.node.anycast'), sub: t('routes.node.anycast_sub') };
    const title = s.operator || (s.asn ? 'AS' + s.asn : s.kind === 'exchange' ? t('routes.node.exchange') : t('routes.node.unknown_net'));
    let sub;
    if (s.kind === 'target') sub = s.city || (s.place && s.place.name) || cname(s.cc);
    else if (s.place && s.place.name) sub = (s.place.sure ? '' : '≈ ') + s.place.name;
    else if (s.cc) sub = '≈ ' + cname(s.cc);
    else sub = t('routes.node.place_unknown');
    return { title, sub, asn: s.kind !== 'exchange' && s.asn && s.operator ? 'AS' + s.asn : '' };
  }
  R._nodeText = nodeText;

  // ------------------------------------------------------------------ the chain
  function chain(r, homeCc) {
    const out = [];
    let prev = null;
    (r.sections || []).forEach((s, i) => {
      if (i > 0) {
        const delta = s.rtt != null && prev != null ? Math.max(s.rtt - prev, 0) : null;
        const est = unsure(s);
        out.push(h('li', { class: 'rt-seg' + (est ? ' is-est' : ''), 'aria-hidden': 'true' },
          h('span', {}, delta == null ? (s.kind === 'target' && !s.reached ? '…' : '') : t('routes.ms', { v: delta < 1 ? '<1' : '+' + LL.fmtNum(Math.round(delta), 0) })),
          est ? h('em', {}, t('routes.estimated')) : null));
      }
      if (s.rtt != null) prev = s.rtt;
      const tx = nodeText(s, homeCc);
      const label = t('routes.kind.' + s.kind) + ': ' + tx.title + (tx.sub ? ', ' + tx.sub : '') + (s.rtt != null ? ', ' + ms(s.rtt) : '') + (unsure(s) ? ' (' + t('routes.estimated') + ')' : '');
      out.push(h('li', { class: 'rt-node rt-' + s.kind + (unsure(s) ? ' is-est' : ''), 'aria-label': label },
        h('small', {}, t('routes.kind.' + s.kind)), h('b', { title: tx.title }, tx.title),
        tx.sub ? h('span', { title: tx.sub }, tx.sub) : null, tx.asn ? h('i', {}, tx.asn) : null));
    });
    return h('ol', { class: 'rt-chain' }, out);
  }

  function targetTitle(r) {
    if (r.anycast) return t('routes.node.anycast');
    return r.operator || (r.asn ? 'AS' + r.asn : cname(r.cc));
  }
  function routeEl(r, homeCc, opts) {
    const where = r.anycast ? t('routes.node.anycast_sub') : [r.city, cname(r.cc)].filter(Boolean).join(', ');
    const meta = [t('routes.method.' + (r.method || 'udp')), t('routes.measured', { age: LL.fmtAge(r.age_s) })];
    const el = h('div', { class: 'rt-route' + (St.sel === r.key ? ' is-sel' : ''), role: 'button', tabindex: '0', 'data-route': r.key,
      'aria-pressed': String(St.sel === r.key), 'aria-label': t('routes.route.aria', { target: targetTitle(r), where, rtt: r.rtt != null ? ms(r.rtt) : t('routes.rtt.unknown') }) },
    h('div', { class: 'rt-head' }, h('b', {}, targetTitle(r)), where ? h('span', {}, where) : null,
      r.changed_recent ? h('span', { class: 'rt-chip', title: t('routes.changed.hint') }, t('routes.changed')) : null,
      r.uncertain ? h('span', { class: 'rt-chip rt-chip--est', title: t('routes.estimated.hint') }, t('routes.estimated')) : null,
      h('em', {}, r.rtt != null ? ms(r.rtt) : '–'),
      h('small', {}, meta.join(' · ') + (r.reached ? '' : ' · ' + t('routes.not_reached')))),
    chain(r, homeCc));
    const pick = () => { St.sel = St.sel === r.key ? null : r.key; sync(); if (opts && opts.onSelect) opts.onSelect(St.sel ? r : null); };
    el.addEventListener('click', pick);
    el.addEventListener('keydown', ev => { if ((ev.key === 'Enter' || ev.key === ' ') && ev.target === el) { ev.preventDefault(); pick(); } });
    el.addEventListener('mouseenter', () => { St.hover = r.key; sync(); });
    el.addEventListener('mouseleave', () => { if (St.hover === r.key) { St.hover = null; sync(); } });
    el.addEventListener('focus', () => { St.hover = r.key; sync(); });
    el.addEventListener('blur', () => { if (St.hover === r.key) { St.hover = null; sync(); } });
    return el;
  }
  function sync() {
    document.querySelectorAll('.rt-route[data-route]').forEach(e => { const on = e.dataset.route === St.sel; e.classList.toggle('is-sel', on); e.setAttribute('aria-pressed', String(on)); });
    St.maps.forEach(m => { if (!m.svg.isConnected) St.maps.delete(m); else m.mark(); });
  }

  /* The section "Routes there" (country sheet) or "Network routes" (data paths view). */
  R.card = function (d, opts) {
    opts = opts || {};
    const rs = (d && d.routes) || [];
    const homeCc = opts.home || (d && d.home_cc) || '';
    if (!rs.length && !(d && d.routes_on)) return null;
    const head = h('header', { class: (opts.cls || 'pc') + '-card__head' }, h(opts.h || 'h3', {}, t(opts.title || 'routes.title')),
      h('span', { class: 'muted' }, rs.length ? t('routes.count', { n: rs.length }) : t('routes.sub')));
    const body = rs.length ? h('div', { class: 'rt-list' }, rs.slice(0, opts.max || 12).map(r => routeEl(r, homeCc, opts)))
      : h('p', { class: 'muted rt-empty' }, t('routes.none_yet'));
    const foot = h('p', { class: 'muted rt-foot' }, t('routes.foot'));
    return h('section', { class: 'card ' + (opts.cls || 'pc') + '-card rt-card', id: opts.id || null }, head, body, foot);
  };
  R.selected = () => St.sel;
  R.select = key => { St.sel = key; sync(); };

  // ------------------------------------------------------------------ the line on a map
  const svgEl = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]); return e; };
  /* Positions of a route: sections with a place, or their country (approximate); the home is the home country. */
  function points(r, o) {
    const out = [];
    let lastCc = null;
    for (const s of r.sections || []) {
      let xy = null, sure = true;
      if (s.kind === 'home') { xy = o.point(s.cc || o.home); lastCc = s.cc || o.home; }
      else if (s.place && s.place.lat != null && s.place.lon != null) { xy = o.project(s.place.lon, s.place.lat); sure = !!s.place.sure; lastCc = s.place.cc || s.cc || lastCc; }
      else if (s.cc && !s.anycast && s.cc !== lastCc) { xy = o.point(s.cc); sure = false; lastCc = s.cc; }   // only the country: its middle, once
      if (!xy) continue;
      const last = out[out.length - 1];
      if (last && Math.hypot(last.x - xy[0], last.y - xy[1]) < 0.05) { if (s.kind === 'exchange') last.kind = 'exchange'; last.sure = last.sure && sure; continue; }
      out.push({ x: xy[0], y: xy[1], kind: s.kind, sure, name: nodeText(s, o.home).title });
    }
    return out;
  }
  R.points = points;
  /* Draws every route as a polyline (sure parts solid, estimated parts dashed); returns {scale(f), fit(key), el}. */
  R.drawOn = function (svg, routes, o) {
    if (!svg) return null;
    const old = svg.querySelector('.rt-lines');
    if (old) old.remove();
    const rs = (routes || []).slice(0, 20);
    if (!rs.length || !o || !o.project || !o.point) return null;
    const g = svgEl('g', { class: 'rt-lines' + (o.quiet ? ' is-quiet' : '') });
    const all = {};
    rs.slice().reverse().forEach(r => {
      const p = points(r, o);
      if (p.length < 2) return;
      all[r.key] = p;
      const e = svgEl('g', { class: 'rt-line', 'data-route': r.key });
      for (let i = 1; i < p.length; i++) {
        const est = !p[i].sure || !p[i - 1].sure;
        const seg = svgEl('path', { class: 'rt-path' + (est ? ' is-est' : ''), d: 'M' + p[i - 1].x.toFixed(2) + ' ' + p[i - 1].y.toFixed(2) + 'L' + p[i].x.toFixed(2) + ' ' + p[i].y.toFixed(2) });
        if (est) seg.appendChild(svgEl('title', {})).textContent = t('routes.estimated');
        e.appendChild(seg);
      }
      p.forEach((q, i) => {
        const r0 = i === 0 ? 3.4 : q.kind === 'exchange' ? 3.6 : i === p.length - 1 ? 3.2 : 2.4;
        const c = svgEl('circle', { class: 'rt-pt rt-pt--' + (i === 0 ? 'home' : q.kind) + (q.sure ? '' : ' is-est'), cx: q.x.toFixed(2), cy: q.y.toFixed(2), r: r0, 'data-r': r0 });
        c.appendChild(svgEl('title', {})).textContent = (i === 0 ? t('routes.node.home') : q.name) + (q.sure ? '' : ' (' + t('routes.estimated') + ')');
        e.appendChild(c);
      });
      g.appendChild(e);
    });
    const before = o.before ? svg.querySelector(o.before) : null;
    svg.insertBefore(g, before);
    const m = {
      svg, el: g,
      mark() {
        const on = St.hover || St.sel;
        g.classList.toggle('has-sel', !!on);
        g.querySelectorAll('.rt-line').forEach(e => e.classList.toggle('is-sel', e.dataset.route === on));
      },
      scale(f) { g.querySelectorAll('circle[data-r]').forEach(c => c.setAttribute('r', (parseFloat(c.dataset.r) / f).toFixed(3))); },
      box(key) {
        const p = all[key];
        if (!p) return null;
        return { x0: Math.min(...p.map(q => q.x)), x1: Math.max(...p.map(q => q.x)), y0: Math.min(...p.map(q => q.y)), y1: Math.max(...p.map(q => q.y)) };
      }
    };
    St.maps.add(m);
    m.mark();
    return m;
  };

  // ------------------------------------------------------------------ settings card (off by default)
  /* The consent is the text above the switch: what is sent, how often, from where, what is stored, which rights it needs. */
  LL.pathsRoutesSettingsCard = function (s) {
    const admin = LL.can('admin'), operate = LL.can('operate');
    const on = h('input', { type: 'checkbox', checked: !!s.paths_routes });
    const targets = h('input', { class: 'input', type: 'number', min: '1', max: '20', step: '1', value: String(s.paths_routes_targets || 5), inputmode: 'numeric' });
    const every = h('input', { class: 'input', type: 'number', min: '1', max: '168', step: '1', value: String(s.paths_routes_interval_h || 6), inputmode: 'numeric' });
    const pdb = h('input', { type: 'checkbox', checked: !!s.paths_routes_peeringdb });
    [on, targets, every, pdb].forEach(e => { e.disabled = !admin; });
    const status = h('div', { class: 'rt-set-status' });
    const msg = h('div', { class: 'msg' });
    const measure = h('button', { class: 'btn btn--ghost', type: 'button', hidden: true, onclick: async () => {
      LL.msg(msg, '');
      try { await api('/api/paths/routes/measure', { method: 'POST', body: {} }); LL.msg(msg, t('settings.routes.measuring'), 'ok'); setTimeout(refresh, 1500); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('settings.routes.measure'));
    async function refresh() {
      let r;
      try { r = await api('/api/paths/routes'); } catch (e) { status.replaceChildren(); return; }
      const st = r.status || {};
      let state;
      if (!st.enabled) state = t('settings.routes.state.off');
      else if (st.possible === false) state = t('settings.routes.state.impossible', { reason: t('settings.routes.reason.' + (st.reason && /^[a-z_]+$/.test(st.reason) ? st.reason : 'sockets')) });
      else if (st.running) state = t('settings.routes.state.running');
      else state = t('settings.routes.state.on', { method: t('routes.method.' + (st.method || 'udp')) });
      const ex = st.exchanges || {};
      status.replaceChildren(LL.facts([
        [t('settings.routes.state'), h('span', { class: st.enabled && st.possible === false ? 'nf-bad' : st.enabled ? 'nf-ok' : '' }, state)],
        st.enabled && st.possible ? [t('settings.routes.methods'), ['udp', 'tcp'].map(m => t('routes.method.' + m) + ': ' + t(st.methods && st.methods[m] ? 'settings.routes.yes' : 'settings.routes.no')).join(' · ')] : null,
        st.last_run ? [t('settings.routes.last_run'), new Date(st.last_run * 1000).toLocaleString(LL.lang) + ' · ' + t('settings.routes.probes', { n: st.probes_last_run || 0 })] : null,
        st.skipped ? [t('settings.routes.skipped'), t('settings.routes.skipped.' + (st.skipped === 'scan' ? 'scan' : 'load'))] : null,
        st.enabled ? [t('settings.routes.stored'), String(st.stored || 0)] : null,
        st.enabled && st.peeringdb ? [t('settings.routes.exchanges'), t('settings.routes.exchanges.' + (ex.status || 'off')) + (ex.month ? ' · ' + ex.month : '')] : null]));
      measure.hidden = !(operate && st.enabled && st.possible !== false && !st.demo);
      if (st.running) setTimeout(() => { if (status.isConnected) refresh(); }, 4000);
    }
    const save = h('button', { class: 'btn btn--ghost', type: 'button', disabled: admin ? null : true, onclick: async () => {
      LL.msg(msg, '');
      const n = parseInt(targets.value, 10), hrs = parseInt(every.value, 10);
      if (!(n >= 1 && n <= 20)) { LL.msg(msg, t('settings.routes.invalid_targets'), 'error'); targets.focus(); return; }
      if (!(hrs >= 1 && hrs <= 168)) { LL.msg(msg, t('settings.routes.invalid_interval'), 'error'); every.focus(); return; }
      try {
        await api('/api/settings', { method: 'PUT', body: { paths_routes: on.checked, paths_routes_targets: n, paths_routes_interval_h: hrs, paths_routes_peeringdb: pdb.checked } });
        LL.msg(msg, t('settings.saved'), 'ok'); setTimeout(refresh, 600);
      } catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('settings.paths.save'));
    const clear = admin ? h('button', { class: 'btn btn--ghost', type: 'button', onclick: async () => {
      if (!window.confirm(t('settings.routes.clear_confirm'))) return;
      try { await api('/api/paths/routes/clear', { method: 'POST', body: {} }); LL.msg(msg, t('settings.saved'), 'ok'); setTimeout(refresh, 300); }
      catch (e) { LL.msg(msg, LL.errText(e), 'error'); }
    } }, t('settings.routes.clear')) : null;
    refresh();
    return h('section', { class: 'card settings-card rt-set', id: 'settings-routes' }, h('h2', {}, t('settings.routes.title')),
      h('p', { class: 'muted' }, t('settings.routes.text')),
      h('p', { class: 'muted fineprint' }, t('settings.routes.consent')),
      h('label', { class: 'check' }, on, h('span', {}, t('settings.routes.enable'))),
      h('div', { class: 'grid-2' }, LL.field(t('settings.routes.targets'), targets, t('settings.routes.targets.hint')),
        LL.field(t('settings.routes.interval'), every, t('settings.routes.interval.hint'))),
      h('label', { class: 'check' }, pdb, h('span', {}, t('settings.routes.peeringdb'))),
      h('p', { class: 'muted fineprint' }, t('settings.routes.peeringdb.consent')),
      status, msg,
      h('div', { class: 'btn-row btn-row--wrap' }, save, measure, clear),
      h('p', { class: 'muted fineprint' }, t('settings.routes.places_attr')));
  };

  // ------------------------------------------------------------------ command palette
  LL.registerCommand({ id: 'routes-settings', group: 'views', order: 41, label: () => t('cmdk.paths_routes'), keywords: () => t('cmdk.kw.paths_routes'),
    run: () => LL.go('#/settings/routes') });
})();
