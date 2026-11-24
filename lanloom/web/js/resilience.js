/* Lanloom — resilience (S13): how well the network copes when something fails. A sub-page of the control room (#/resilience,
   #/resilience/scenarios, #/resilience/layers, #/resilience/part/<id>), the risk lens on the maps and a section on the component card.

   Data: GET /api/resilience (score, grade, 90 days of history, top 10 weak spots, scenarios, layers), GET /api/resilience/part/<id>
   (the seven axes with reasons), GET /api/resilience/scenario/<id>?minute= (the power cut at a minute of the slider), GET
   /api/resilience/lens?view= (risk per tile). Writes (operators): PUT /api/resilience/part/<id> {power, spare, note} and PUT
   /api/resilience/settings {runtime_min, reserve_min}. Everything is a simulation: nothing is switched. Reasons are i18n keys with
   variables (rs.why.*), every level carries a word and a number next to its colour.
   Map: the lens tints the tiles by risk (board.setLens), toggled with the "Risk lens" chip above the board, the palette or the B key;
   a scenario opens in the failure bar (LL.failure.scenario). Interface: LL.resilience.attach(map), .refresh(), .toggleLens(),
   .lensOn(), LL.resilienceSection(id) (the card). */
(function () {
  'use strict';
  // ---- pure helpers (no DOM, t() passed in; exported for tests/js/resilience_test.js)
  const PARTS = ['devices', 'hub_devices', 'automations', 'entities', 'connections'];
  function axisStep(a) {
    if (a && a.counts === false) return 'na';
    const w = a ? a.weak : null;
    return w == null ? 'unknown' : w <= 0.15 ? 'good' : w <= 0.45 ? 'fair' : w <= 0.75 ? 'weak' : 'bad';
  }
  // the word of a square: the status the "Why?" panel shows plus the rating its colour stands for (never colour alone,
  // docs/ACCESSIBILITY.md). A fully weak axis is not always missing: "mains only" and a low availability are rated fully
  // weak too (#20). Unknown and "does not count" say it once.
  function cellLabel(axis, a, tr) {
    const step = axisStep(a), name = tr('rs.axis.' + axis);
    if (!a || !a.value) return name + ': ' + tr('rs.weak.' + step);
    const status = tr('rs.value.' + a.value);
    return step === 'unknown' || step === 'na' ? name + ': ' + status : name + ': ' + status + ' · ' + tr('rs.weak.' + step);
  }
  // "41 hub devices · 457 entities · 4 connections": only the groups above zero, each with its own plural (fail.part.*)
  function partsLine(c, tr) { return PARTS.filter(p => c && c[p]).map(p => tr('fail.part.' + p, { n: c[p] })).join(' · '); }
  if (typeof module !== 'undefined' && module.exports) module.exports = { PARTS, axisStep, cellLabel, partsLine };
  if (typeof window === 'undefined' || !window.Lanloom || !window.Lanloom.h) return;

  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const LEVELS = ['low', 'moderate', 'elevated', 'high', 'critical'];
  const SIX = ['spare', 'backup', 'power', 'currency', 'watch', 'health'];
  const LAYER_ORDER = ['services', 'platforms', 'servers', 'network', 'gateway', 'internet', 'power'];   // top to bottom: what rests on what
  const LENS_KEY = 'll-risk-lens';
  const enc = encodeURIComponent;
  const S = { map: null, lens: false, lensData: null, lensKey: '', lensBusy: false, peek: null, peekAt: 0, peeking: false };
  const R = LL.resilience = {};
  const num = (v, d) => LL.fmtNum(v, d == null ? 0 : d);

  LL.NAV_RESILIENCE_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/><path d="M8.5 12.5l2.4 2.4 4.6-5"/></svg>';

  // ------------------------------------------------------------------ small parts
  const lvl = l => (LEVELS.includes(l) ? l : 'none');
  const levelWord = l => (LEVELS.includes(l) ? t('rs.level.' + l) : t('rs.level.none'));
  function riskChip(risk, level) {
    return h('span', { class: 'rs-risk rs-risk--' + lvl(level), title: t('rs.risk.title') },
      h('i', { class: 'rs-risk__dot', 'aria-hidden': 'true' }), h('span', { class: 'rs-risk__word' }, levelWord(level)),
      risk == null ? null : h('span', { class: 'rs-risk__num' }, num(risk)));
  }
  // rs.why.impact_also carries the groups as numbers; its {parts} is filled here from fail.part.* (zeros dropped, one plural
  // per group). This is the only place rs.why.* is rendered: anywhere else that line would show {parts} literally.
  function why(r) {
    const v = r.vars || {};
    return LL.fillText('rs.why.' + r.key, r.key === 'impact_also' ? Object.assign({}, v, { parts: partsLine(v, t) }) : v);
  }
  function partName(x) {
    if (!x) return '';
    if (x.id === 'internet' || x.kind === 'internet') return t('kind.internet');
    if (x.dev_kind === 'self' || x.self) return x.title || t('kind.self');
    return x.title || x.name || x.ip || x.id;
  }
  function partHref(x) {
    const tile = (x.tiles || [])[0] || x.id;
    return x.kind === 'hub_device' ? '#/map/' + enc(tile) : '#/network/' + enc(tile);
  }
  function minutes(s) { return s == null ? '—' : t('rs.minutes', { n: Math.round(s / 60) }); }
  function weakCell(axis, a) {
    const step = axisStep(a), label = cellLabel(axis, a, t);
    return h('span', { class: 'rs-cell rs-cell--' + step, title: label, role: 'img', 'aria-label': label },
      h('span', { class: 'rs-cell__k', 'aria-hidden': 'true' }, t('rs.axis.short.' + axis)));
  }

  // ------------------------------------------------------------------ score and history
  function spark(hist) {
    const pts = (hist || []).slice(-90);
    if (pts.length < 2) return h('p', { class: 'rs-spark__none muted' }, t('rs.history.none'));
    const W = 240, H = 46, lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
    const min = Math.max(0, Math.min(lo - 5, 100 - 20)), max = Math.min(100, Math.max(hi + 5, min + 20));
    const x = i => (i * (W - 4)) / (pts.length - 1) + 2, y = v => H - 3 - ((v - min) / (max - min)) * (H - 6);
    const d = pts.map((p, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p[1]).toFixed(1)).join(' ');
    const el = h('span', { class: 'rs-spark', role: 'img', 'aria-label': t('rs.history.label', { n: pts.length, from: pts[0][1], to: pts[pts.length - 1][1] }) });
    el.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="' + H + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<path d="' + d + '" fill="none" stroke="currentColor" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/></svg>';
    return el;
  }
  function trend(hist) {
    const p = hist || [];
    if (p.length < 2) return null;
    const now = p[p.length - 1][1], back = p[Math.max(0, p.length - 8)][1], dlt = now - back;
    if (!dlt) return h('span', { class: 'rs-trend muted' }, t('rs.trend.same'));
    return h('span', { class: 'rs-trend rs-trend--' + (dlt > 0 ? 'up' : 'down') }, t(dlt > 0 ? 'rs.trend.up' : 'rs.trend.down', { n: Math.abs(dlt) }));
  }
  function hero(d) {
    const sc = d.score;
    if (!sc) return h('section', { class: 'card rs-empty' }, h('h2', {}, t('rs.empty.title')), h('p', { class: 'muted' }, t('rs.empty.text')));
    const counts = LEVELS.slice().reverse().filter(l => sc.levels[l]).map(l => h('li', { class: 'rs-count rs-count--' + l }, riskChip(null, l), h('strong', {}, num(sc.levels[l]))));
    return h('section', { class: 'rs-hero rs-hero--' + sc.grade, 'aria-label': t('rs.hero.label') },
      h('div', { class: 'rs-grade', role: 'img', 'aria-label': t('rs.grade.label', { grade: sc.grade, score: sc.score }) },
        h('span', { class: 'rs-grade__letter', 'aria-hidden': 'true' }, sc.grade), h('span', { class: 'rs-grade__score', 'aria-hidden': 'true' }, t('rs.grade.score', { score: sc.score }))),
      h('div', { class: 'rs-hero__text' },
        h('strong', { class: 'rs-hero__title' }, t('rs.grade.word.' + sc.grade)),
        h('span', { class: 'rs-hero__sub' }, t('rs.hero.sub', { n: sc.n })),
        h('ul', { class: 'rs-counts' }, counts)),
      h('div', { class: 'rs-hero__hist' }, h('span', { class: 'rs-hero__histlabel muted' }, t('rs.history.title')), spark(d.history), trend(d.history)));
  }

  // ------------------------------------------------------------------ weak spots
  function recText(rec) { return rec ? t('rs.rec.' + rec.key) : t('rs.rec.none'); }
  function weakRow(w, i, open) {
    const det = h('div', { class: 'rs-weak__more' });
    const btn = h('button', { class: 'btn btn--ghost btn--sm rs-weak__why', type: 'button', 'aria-expanded': 'false' }, t('rs.weak.why'));
    btn.onclick = () => {
      const on = btn.getAttribute('aria-expanded') !== 'true';
      btn.setAttribute('aria-expanded', on ? 'true' : 'false');
      if (!on) { det.replaceChildren(); return; }
      det.replaceChildren(h('p', { class: 'muted' }, t('rs.loading')));
      partCard(w.id, det, { compact: true });
    };
    const row = h('li', { class: 'rs-weak rs-weak--' + lvl(w.level), dataset: { id: w.id } },
      h('span', { class: 'rs-weak__rank', 'aria-hidden': 'true' }, String(i + 1)),
      h('div', { class: 'rs-weak__main' },
        h('div', { class: 'rs-weak__head' },
          h('a', { class: 'rs-weak__name', href: partHref(w) }, partName(w)),
          h('span', { class: 'rs-weak__kind muted' }, w.dev_kind ? t('kind.' + w.dev_kind) : t('fail.kind.' + w.kind))),
        h('div', { class: 'rs-weak__rec' }, h('span', {}, recText(w.rec)), w.rec ? h('span', { class: 'chip chip--muted rs-effort' }, t('rs.effort.' + w.rec.effort)) : null)),
      h('div', { class: 'rs-weak__axes', 'aria-label': t('rs.axes.label') }, SIX.map(k => weakCell(k, w.axes[k]))),
      riskChip(w.risk, w.level), btn, det);
    if (open) setTimeout(() => btn.click(), 0);
    return row;
  }
  function weakSection(d, openId) {
    const list = d.weak_spots || [];
    const body = list.length ? h('ol', { class: 'rs-weaklist' }, list.map((w, i) => weakRow(w, i, w.id === openId)))
      : h('p', { class: 'muted' }, t('rs.weak.none'));
    return h('section', { class: 'card rs-card', 'data-sec': 'weak', tabindex: '-1', 'aria-labelledby': 'rs-h-weak' },
      h('div', { class: 'rs-sec__head' }, h('h2', { id: 'rs-h-weak' }, t('rs.weak.title')), h('span', { class: 'chip chip--muted' }, t('rs.weak.of', { n: list.length, total: d.ranked_n }))),
      h('p', { class: 'muted rs-sec__lead' }, t('rs.weak.lead')),
      h('div', { class: 'rs-legend', 'aria-hidden': 'true' }, SIX.map(k => h('span', { class: 'rs-legend__item' }, h('b', {}, t('rs.axis.short.' + k)), ' ' + t('rs.axis.' + k)))),
      body);
  }

  // ------------------------------------------------------------------ the card of one part (seven axes, own fields)
  async function partCard(id, box, opt) {
    let d;
    try { d = await api('/api/resilience/part/' + enc(id)); }
    catch (e) { box.replaceChildren(h('p', { class: 'msg msg--error' }, e.status === 404 ? t('rs.err.unknown') : LL.errText(e))); return null; }
    drawPart(d, box, opt || {});
    return d;
  }
  function drawPart(d, box, opt) {
    const kids = [];
    if (!d.rated) kids.push(h('p', { class: 'muted' }, t('rs.part.not_rated')));
    else {
      if (!opt.compact) kids.push(h('div', { class: 'rs-part__top' }, riskChip(d.risk, d.level),
        d.rank ? h('span', { class: 'chip chip--muted' }, t('rs.part.rank', { n: d.rank })) : null,
        d.rec ? h('span', { class: 'rs-part__rec' }, recText(d.rec)) : null));
      kids.push(h('ul', { class: 'rs-axes' }, d.axes.map(a => h('li', { class: 'rs-axis' },
        a.axis === 'impact' ? h('span', { class: 'rs-cell rs-cell--impact', 'aria-hidden': 'true' }, h('span', { class: 'rs-cell__k' }, t('rs.axis.short.impact'))) : weakCell(a.axis, a),
        h('div', { class: 'rs-axis__main' },
          h('div', { class: 'rs-axis__head' }, h('strong', {}, t('rs.axis.' + a.axis)),
            a.value ? h('span', { class: 'rs-pill rs-pill--' + (a.axis === 'impact' ? 'impact' : axisStep(a)) }, t('rs.value.' + a.value)) : null),
          ...axisText(a.why || []), axisLink(a, d, box))))));
    }
    if (d.fields && d.fields.note) kids.push(h('p', { class: 'rs-part__note' }, h('span', { class: 'muted' }, t('rs.field.note') + ': '), d.fields.note));
    if (d.editable && LL.can('operate')) kids.push(editor(d, box, opt));
    else if (d.fields && (d.fields.power || d.fields.spare)) kids.push(h('p', { class: 'muted' }, [d.fields.power ? t('rs.field.power') + ': ' + t('rs.power.' + d.fields.power) : '', d.fields.spare ? t('rs.field.spare') + ': ' + t('rs.spare.' + d.fields.spare) : ''].filter(Boolean).join(' · ')));
    box.replaceChildren(...kids);
  }
  // one layout on every axis: the first reason is the plain sentence, the others go on one small detail line (#20)
  function axisText(list) {
    if (!list.length) return [];
    return [h('p', { class: 'rs-axis__lead' }, why(list[0])),
      list.length > 1 ? h('p', { class: 'rs-axis__detail' }, list.slice(1).map(why).join(' · ')) : null];
  }
  // a link only where a person can really change something
  function axisLink(a, d, box) {
    const may = d.editable && LL.can('operate'), weak = a.weak == null ? 1 : a.weak, node = d.node || {};
    const btn = (key, run) => h('button', { class: 'link-btn rs-axis__link', type: 'button', onclick: run }, t(key));
    const field = name => () => {
      const el = box.querySelector('select[name="' + name + '"]');
      if (el) { el.scrollIntoView({ block: 'nearest' }); el.focus({ preventScroll: true }); }
    };
    if (a.axis === 'impact') return a.value === 'parts' && LL.failure ? btn('rs.part.show_chain', () => LL.failure.show(node.id, 'down')) : null;
    if ((a.axis === 'spare' || a.axis === 'power') && may && weak >= 0.5) return btn('rs.part.link.' + a.axis, field(a.axis));
    if (a.axis === 'backup' && a.value === 'none') return h('a', { class: 'rs-axis__link', href: '#/backups' }, t('rs.part.link.backup'));
    if (a.axis === 'health' && a.value === 'measured' && weak >= 0.8 && node.kind === 'device' && !node.self)
      return h('a', { class: 'rs-axis__link', href: '#/devices/' + enc(node.id) }, t('rs.part.link.sleepy'));
    return null;
  }
  function editor(d, box, opt) {
    const f = d.fields || {};
    const sel = (name, values, cur) => {
      const s = h('select', { class: 'select select--sm', name, 'aria-label': t('rs.field.' + name) }, h('option', { value: '' }, t('rs.field.unset')),
        values.map(v => h('option', { value: v }, t('rs.' + name + '.' + v))));
      s.value = cur || '';
      return s;
    };
    const power = sel('power', d.power, f.power), spare = sel('spare', d.spare, f.spare);
    const note = h('input', { class: 'input-sm rs-ed__note', type: 'text', maxlength: '120', value: f.note || '', placeholder: t('rs.field.note_ph'), 'aria-label': t('rs.field.note') });
    const save = h('button', { class: 'btn btn--primary btn--sm', type: 'button' }, t('rs.field.save'));
    save.onclick = async () => {
      save.disabled = true;
      try {
        await api('/api/resilience/part/' + enc(d.node.id), { method: 'PUT', body: { power: power.value || null, spare: spare.value || null, note: note.value || '' } });
        LL.toast(t('rs.field.saved'), 'ok');
        S.lensKey = ''; S.peekAt = 0;
        await partCard(d.node.id, box, opt);
        if (opt.onSaved) opt.onSaved();
        if (S.lens) loadLens(true);
      } catch (e) { LL.toast(LL.errText(e), 'error'); save.disabled = false; }
    };
    return h('div', { class: 'rs-ed' },
      h('h4', { class: 'rs-ed__h' }, t('rs.field.title')),
      h('div', { class: 'rs-ed__grid' },
        h('label', { class: 'rs-ed__label' }, t('rs.field.power'), power),
        h('label', { class: 'rs-ed__label' }, t('rs.field.spare'), spare),
        h('label', { class: 'rs-ed__label rs-ed__label--wide' }, t('rs.field.note'), note)),
      h('p', { class: 'fineprint' }, t('rs.field.help')),
      h('div', { class: 'rs-ed__foot' }, save));
  }

  // ------------------------------------------------------------------ scenarios
  function countsLine(c) {
    return partsLine(c, t);
  }
  function names(list) {
    if (!list || !list.length) return '';
    try { return new Intl.ListFormat(LL.locale(), { type: 'conjunction' }).format(list); } catch (e) { return list.join(', '); }
  }
  function showBtn(sid, minute) {
    return h('button', { class: 'btn btn--sm rs-show', type: 'button', onclick: () => LL.failure && LL.failure.scenario(sid, typeof minute === 'function' ? minute() : minute) }, t('rs.sc.show'));
  }
  function group(g) {
    const items = g.items.slice(0, 12);
    return h('div', { class: 'rs-group rs-group--' + g.id },
      h('h4', { class: 'rs-group__h' }, t('rs.sc.group.' + g.id, { n: g.items.length })),
      h('ul', { class: 'rs-group__list' }, items.map(x => h('li', {}, h('a', { href: '#/network/' + enc(x.id) }, x.name),
        x.off_s != null && g.id === 'ups' ? h('span', { class: 'muted' }, ' · ' + t('rs.sc.until', { time: minutes(x.off_s) })) : null)),
        g.items.length > items.length ? h('li', { class: 'muted' }, t('rs.more', { n: g.items.length - items.length })) : null));
  }
  function powerCard(sc) {
    const max = sc.max_min || 240;
    const start = sc.cut_s != null ? Math.min(max, Math.ceil(sc.cut_s / 60) + 1) : 0;
    const out = h('p', { class: 'rs-sc__now', 'aria-live': 'polite' });
    const slider = h('input', { class: 'rs-slider', type: 'range', min: '0', max: String(max), step: '1', value: String(start), 'aria-label': t('rs.sc.power.slider') });
    const label = h('output', { class: 'rs-slider__val' });
    let seq = 0, timer = 0;
    async function update() {
      const m = Number(slider.value);
      label.textContent = t('rs.minutes', { n: m });
      const my = ++seq;
      let d;
      try { d = await api('/api/resilience/scenario/power?minute=' + m); } catch (e) { if (my === seq) out.textContent = LL.errText(e); return; }
      if (my !== seq) return;
      const off = d.counts.sure, running = d.running || [];
      out.replaceChildren(...[h('strong', {}, t('rs.sc.power.at', { n: m, off })), ' ',
        h('span', {}, running.length ? t('rs.sc.power.running', { n: running.length }) : t('rs.sc.power.all_off')),
        countsLine(d.counts) ? h('span', { class: 'muted' }, ' · ' + countsLine(d.counts)) : null].filter(Boolean));
    }
    slider.oninput = () => { clearTimeout(timer); label.textContent = t('rs.minutes', { n: Number(slider.value) }); timer = setTimeout(update, 180); };
    setTimeout(update, 0);
    const marks = (sc.timeline || []).map(p => h('li', { class: 'rs-tl__pt' }, h('span', { class: 'rs-tl__m' }, t('rs.minutes', { n: p.min })), h('strong', {}, num(p.counts.sure))));
    let sentence;
    if (sc.no_ups) sentence = t('rs.sc.power.no_ups');
    else if (sc.cut_s == null) sentence = t('rs.sc.power.no_runtime');
    else sentence = t(sc.basis === 'live' ? 'rs.sc.power.live' : 'rs.sc.power.setting', { time: minutes(sc.cut_s), ups: names(sc.ups_names), reserve: sc.reserve_min || 0 });
    const hub = sc.hub_off_min == null ? null : h('p', { class: 'rs-sc__hub' + (sc.hub_off_min === 0 ? ' is-bad' : '') }, sc.hub_off_min === 0 ? t('rs.sc.power.hub_now') : t('rs.sc.power.hub_at', { n: Math.round(sc.hub_off_min) }));
    return h('article', { class: 'rs-sc rs-sc--power', 'data-sc': 'power' },
      h('div', { class: 'rs-sc__head' }, scIcon('power'), h('h3', {}, t('rs.sc.power.title'))),
      h('p', { class: 'rs-sc__lead' }, sentence), hub,
      h('div', { class: 'rs-slider__row' }, h('span', { class: 'muted rs-slider__end' }, t('rs.minutes', { n: 0 })), slider, h('span', { class: 'muted rs-slider__end' }, t('rs.minutes', { n: max })), label),
      out,
      h('ol', { class: 'rs-tl', 'aria-label': t('rs.sc.timeline') }, marks),
      h('div', { class: 'rs-groups' }, (sc.groups || []).map(group)),
      h('div', { class: 'rs-sc__foot' }, showBtn('power', () => Number(slider.value))));
  }
  function internetCard(sc) {
    const lead = sc.reserve ? t(sc.on_reserve ? 'rs.sc.internet.on_reserve' : 'rs.sc.internet.reserve', { name: sc.reserve }) : t('rs.sc.internet.no_reserve');
    const local = sc.has_hub ? (sc.hub_off ? t('rs.sc.internet.hub_off') : t('rs.sc.internet.local', { n: sc.autos.total - sc.autos.off, total: sc.autos.total })) : null;
    const need = sc.counts.sure + sc.counts.likely;
    return h('article', { class: 'rs-sc rs-sc--internet', 'data-sc': 'internet' },
      h('div', { class: 'rs-sc__head' }, scIcon('internet'), h('h3', {}, t('rs.sc.internet.title'))),
      h('p', { class: 'rs-sc__lead' }, lead),
      local ? h('p', {}, local) : null,
      h('p', {}, need ? t('rs.sc.internet.need', { n: need, names: names(sc.names) || '…' }) : t('rs.sc.internet.need_none')),
      h('p', { class: 'fineprint' }, t('rs.sc.internet.help')),
      h('div', { class: 'rs-sc__foot' }, need ? showBtn('internet') : null));
  }
  function nodeCard(sc) {
    const c = sc.counts;
    return h('article', { class: 'rs-sc rs-sc--node', 'data-sc': sc.id },
      h('div', { class: 'rs-sc__head' }, scIcon(sc.role === 'gateway' ? 'gateway' : 'server'), h('h3', {}, t('rs.sc.node.title', { name: sc.name })), riskChip(sc.risk, sc.level)),
      h('p', { class: 'rs-sc__lead' }, c.sure ? t('rs.sc.node.lead', { n: c.sure }) : t('rs.sc.node.none')),
      c.sure ? h('p', { class: 'muted' }, countsLine(c)) : null,
      sc.names && sc.names.length ? h('p', {}, t('rs.sc.node.among', { names: names(sc.names) })) : null,
      sc.hub_off ? h('p', { class: 'rs-sc__hub is-bad' }, t('rs.sc.node.hub_off')) : null,
      h('p', { class: 'rs-sc__spare' }, t('rs.sc.node.spare.' + (sc.spare || 'unknown'))),
      h('div', { class: 'rs-sc__foot' }, c.sure ? showBtn(sc.id) : null, h('a', { class: 'btn btn--ghost btn--sm', href: '#/resilience/part/' + enc(sc.node) }, t('rs.sc.node.part'))));
  }
  const SC_ICON = {
    power: '<path d="M13 2.5L5 13.5h6l-1 8 8-11h-6z"/>',
    internet: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.4 3.8 5.2 3.8 8.5s-1.2 6.1-3.8 8.5c-2.6-2.4-3.8-5.2-3.8-8.5S9.4 5.9 12 3.5z"/>',
    gateway: '<rect x="3" y="12" width="18" height="7" rx="2"/><path d="M7 15.5h.01M11 15.5h.01M8 12l-2-6M16 12l2-6"/>',
    server: '<rect x="4" y="3.5" width="16" height="7" rx="1.5"/><rect x="4" y="13.5" width="16" height="7" rx="1.5"/><path d="M8 7h.01M8 17h.01"/>'
  };
  function scIcon(k) { const el = h('span', { class: 'rs-sc__icon', 'aria-hidden': 'true' }); el.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + SC_ICON[k] + '</svg>'; return el; }
  function scenariosSection(d) {
    const cards = (d.scenarios || []).map(sc => (sc.kind === 'power' ? powerCard(sc) : sc.kind === 'internet' ? internetCard(sc) : nodeCard(sc)));
    return h('section', { class: 'card rs-card', 'data-sec': 'scenarios', tabindex: '-1', 'aria-labelledby': 'rs-h-sc' },
      h('div', { class: 'rs-sec__head' }, h('h2', { id: 'rs-h-sc' }, t('rs.sc.title'))),
      h('p', { class: 'muted rs-sec__lead' }, t('rs.sc.lead')),
      cards.length ? h('div', { class: 'rs-scgrid' }, cards) : h('p', { class: 'muted' }, t('rs.sc.none')));
  }

  // ------------------------------------------------------------------ layers: what the network rests on
  function layersSection(d) {
    const rows = (d.layers || []).slice().sort((a, b) => LAYER_ORDER.indexOf(a.id) - LAYER_ORDER.indexOf(b.id));
    const body = rows.map(l => {
      const breaks = Object.entries(l.breaks || {}).filter(([, n]) => n).sort((a, b) => LAYER_ORDER.indexOf(a[0]) - LAYER_ORDER.indexOf(b[0]));
      const members = l.id === 'internet' ? (l.reserve ? [t('rs.layer.internet.with', { name: l.reserve })] : [t('kind.internet')])
        : l.id === 'power' ? (l.ups || []).map(u => u.name + (u.runtime_s ? ' · ' + minutes(u.runtime_s) : '')) : l.members;
      return h('li', { class: 'rs-layer rs-layer--' + l.id + ' rs-layer--lv-' + lvl(l.level), dataset: { layer: l.id } },
        h('div', { class: 'rs-layer__name' }, h('strong', {}, t('rs.layer.' + l.id)), h('span', { class: 'muted' }, t('rs.layer.n', { n: l.n }))),
        h('div', { class: 'rs-layer__members' }, members.slice(0, 5).join(' · ') + (members.length > 5 ? ' · ' + t('rs.more', { n: members.length - 5 }) : '')),
        h('div', { class: 'rs-layer__breaks' }, breaks.length
          ? [h('span', { class: 'muted' }, t('rs.layer.breaks')), ...breaks.map(([k, n]) => h('span', { class: 'chip chip--muted' }, t('rs.layer.short.' + k) + ' ' + num(n)))]
          : h('span', { class: 'muted' }, l.id === 'power' ? t('rs.layer.power.see') : t('rs.layer.breaks_none'))),
        h('div', { class: 'rs-layer__risk' }, l.level ? riskChip(l.risk, l.level) : null,
          l.weakest && l.weakest.id ? h('a', { class: 'rs-layer__weak', href: '#/resilience/part/' + enc(l.weakest.id) }, t('rs.layer.weakest', { name: l.weakest.name || t('kind.internet') })) : null));
    });
    return h('section', { class: 'card rs-card', 'data-sec': 'layers', tabindex: '-1', 'aria-labelledby': 'rs-h-layers' },
      h('div', { class: 'rs-sec__head' }, h('h2', { id: 'rs-h-layers' }, t('rs.layer.title'))),
      h('p', { class: 'muted rs-sec__lead' }, t('rs.layer.lead')),
      body.length ? h('ol', { class: 'rs-layers' }, body) : h('p', { class: 'muted' }, t('rs.layer.none')));
  }

  // ------------------------------------------------------------------ settings (battery figures) and open power sources
  function settingsSection(d, reload) {
    const cfg = d.settings || {};
    const kids = [];
    if (d.unknown_power_n) kids.push(h('p', { class: 'rs-open' }, t('rs.open.power', { n: d.unknown_power_n, names: names(d.unknown_power.slice(0, 6)) + (d.unknown_power_n > 6 ? ' …' : '') })),
      h('p', { class: 'fineprint' }, t('rs.open.help')));
    if ((d.ups || []).length) kids.push(h('ul', { class: 'rs-ups' }, d.ups.map(u => h('li', {}, h('strong', {}, u.name), ' · ',
      u.runtime_s ? t('rs.ups.runtime', { time: minutes(u.runtime_s) }) : t('rs.ups.no_runtime'), u.load != null ? h('span', { class: 'muted' }, ' · ' + t('rs.ups.load', { n: num(u.load) })) : null))));
    else kids.push(h('p', { class: 'muted' }, t('rs.ups.none')));
    if (LL.can('operate')) {
      const rt = h('input', { class: 'input-sm', type: 'number', min: '1', max: '600', step: '1', value: cfg.runtime_min == null ? '' : String(cfg.runtime_min), 'aria-label': t('rs.set.runtime') });
      const rs = h('input', { class: 'input-sm', type: 'number', min: '0', max: '120', step: '1', value: String(cfg.reserve_min || 0), 'aria-label': t('rs.set.reserve') });
      const btn = h('button', { class: 'btn btn--sm', type: 'button' }, t('rs.field.save'));
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api('/api/resilience/settings', { method: 'PUT', body: { runtime_min: rt.value === '' ? null : Number(rt.value), reserve_min: Number(rs.value || 0) } });
          LL.toast(t('rs.field.saved'), 'ok'); reload();
        } catch (e) { LL.toast(LL.errText(e), 'error'); btn.disabled = false; }
      };
      kids.push(h('div', { class: 'rs-set' },
        h('label', { class: 'rs-ed__label' }, t('rs.set.runtime'), rt, h('span', { class: 'fineprint' }, t('rs.set.runtime_help'))),
        h('label', { class: 'rs-ed__label' }, t('rs.set.reserve'), rs, h('span', { class: 'fineprint' }, t('rs.set.reserve_help'))),
        h('div', { class: 'rs-ed__foot' }, btn)));
    }
    return h('section', { class: 'card rs-card', 'data-sec': 'settings', 'aria-labelledby': 'rs-h-set' },
      h('div', { class: 'rs-sec__head' }, h('h2', { id: 'rs-h-set' }, t('rs.set.title'))), ...kids);
  }
  function howSection(d) {
    return h('details', { class: 'card rs-card rs-how' },
      h('summary', {}, t('rs.how.title')),
      h('p', {}, t('rs.how.text')),
      h('ul', { class: 'rs-how__list' }, ['impact'].concat(SIX).map(k => h('li', {}, h('strong', {}, t('rs.axis.' + k) + ': '), t('rs.how.' + k)))),
      h('p', { class: 'fineprint' }, t('rs.how.grade', { a: (d.grades[0] || {}).from, b: (d.grades[1] || {}).from, c: (d.grades[2] || {}).from, d: (d.grades[3] || {}).from, e: (d.grades[4] || {}).from })));
  }

  // ------------------------------------------------------------------ the page
  async function view(app, rest) {
    const sub = rest && rest[0];
    const partId = sub === 'part' && rest[1] ? decodeURIComponent(rest.slice(1).join('/')) : null;
    const fr = LL.frame(app, 'controlroom', h('div', { class: 'page page--wide rs-page' }));
    const page = fr.main.firstChild;
    const body = h('div', { class: 'rs-body' }, h('div', { class: 'skeleton' }));
    page.append(h('header', { class: 'page__head rs-head' },
      h('div', {}, h('nav', { class: 'rs-crumb muted', 'aria-label': t('rs.crumb') }, h('a', { href: '#/controlroom' }, t('nav.controlroom')), ' › '), h('h1', {}, t('rs.title')), h('p', { class: 'muted rs-lead' }, t('rs.lead'))),
      h('div', { class: 'btn-row btn-row--wrap rs-head__btns' },
        h('a', { class: 'btn btn--sm', href: '#/network?lens=1' }, t('rs.lens.open')),
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/resilience/scenarios' }, t('rs.sc.title')))), body);
    let first = true;
    async function load() {
      let d;
      try { d = await api('/api/resilience'); }
      catch (e) { body.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); return; }
      if (!page.isConnected) return;
      const y = fr.main.scrollTop;
      const kids = [hero(d)];
      if (partId) {
        const box = h('div', { class: 'rs-part' }, h('p', { class: 'muted' }, t('rs.loading')));
        const sec = h('section', { class: 'card rs-card rs-partcard', 'data-sec': 'part', tabindex: '-1' }, h('div', { class: 'rs-sec__head' }, h('h2', { class: 'rs-part__title' }, t('rs.part.title'))), box);
        kids.push(sec);
        partCard(partId, box, { onSaved: () => load() }).then(p => { if (p) sec.querySelector('.rs-part__title').textContent = t('rs.part.title_of', { name: partName(p.node) }); });
      }
      kids.push(weakSection(d, null), scenariosSection(d), layersSection(d), settingsSection(d, load), howSection(d));
      body.replaceChildren(...kids);
      S.peek = { t: Date.now(), d }; S.peekAt = Date.now();
      if (first) {
        first = false;
        const target = sub && body.querySelector('[data-sec="' + (sub === 'part' ? 'part' : sub) + '"]');
        if (target) { target.scrollIntoView({ block: 'start' }); target.focus({ preventScroll: true }); }
      } else fr.main.scrollTop = y;
    }
    await load();
    return fr;
  }
  LL.view('resilience', (app, rest) => view(app, rest));

  // ------------------------------------------------------------------ the card section on the map
  LL.resilienceSection = function (id) {
    const box = h('div', { class: 'rs-part rs-part--card' }, h('p', { class: 'muted' }, t('rs.loading')));
    const sec = h('section', { class: 'detail__sec rs-sec', 'data-sec': 'resilience' }, h('h3', {}, t('rs.sec.title')), box);
    api('/api/resilience/part/' + enc(id)).then(d => {
      if (!d.rated && !d.editable) { sec.remove(); return; }
      drawPart(d, box, { compact: false });
      box.append(h('div', { class: 'btn-row btn-row--wrap' }, h('a', { class: 'btn btn--ghost btn--sm', href: '#/resilience/part/' + enc(d.node.id) }, t('rs.sec.open'))));
    }).catch(e => { if (e.status === 404) sec.remove(); else box.replaceChildren(h('p', { class: 'msg msg--error' }, LL.errText(e))); });
    return sec;
  };

  // ------------------------------------------------------------------ the risk lens on the map
  function board() { const b = S.map && S.map.board(); return b && b.setLens ? b : null; }
  function lensBar() {
    const host = S.map && S.map.slot && S.map.slot();
    if (!host) return;
    let bar = host.querySelector('.rs-lensbar');
    if (!S.lens) { if (bar) bar.remove(); return; }
    const fresh = !bar;
    if (fresh) bar = h('section', { class: 'rs-lensbar', role: 'region', 'aria-label': t('rs.lens.title') });
    const sc = S.lensData && S.lensData.score;
    bar.replaceChildren(
      h('div', { class: 'rs-lensbar__title' }, h('strong', {}, t('rs.lens.title')), sc ? h('span', { class: 'muted' }, ' · ' + t('rs.lens.score', { grade: sc.grade, score: sc.score })) : null),
      h('ul', { class: 'rs-lensbar__keys' }, LEVELS.map(l => h('li', {}, riskChip(null, l)))),
      h('div', { class: 'rs-lensbar__tools' },
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/resilience' }, t('rs.lens.more')),
        h('button', { class: 'btn btn--ghost btn--icon btn--sm', type: 'button', 'aria-label': t('rs.lens.off'), title: t('rs.lens.off'), onclick: () => R.toggleLens(false) }, '✕')));
    if (fresh) host.prepend(bar);
  }
  async function loadLens(force) {
    const b = board();
    if (!b || !S.map) return;
    if (!S.lens) { b.setLens(null); lensBar(); return; }
    const kind = S.map.kind === 'network' ? 'network' : 'overview';
    const v = S.map.view ? S.map.view() : null;
    const key = kind + ':' + (v ? v.structure : '');
    if (!force && key === S.lensKey && S.lensData) { b.setLens({ tiles: S.lensData.tiles }); lensBar(); return; }
    if (S.lensBusy) { S.lensAgain = true; return; }       // one request at a time; a wish that came meanwhile runs right after it
    S.lensBusy = true;
    try { S.lensData = await api('/api/resilience/lens?view=' + kind); S.lensKey = key; }
    catch (e) { S.lensData = { tiles: {} }; }
    S.lensBusy = false;
    if (S.lensAgain) { S.lensAgain = false; loadLens(true); return; }
    const b2 = board();
    if (b2 && S.lens) b2.setLens({ tiles: S.lensData.tiles });
    lensBar();
  }
  R.attach = function (m) {
    S.map = m;
    try { S.lens = localStorage.getItem(LENS_KEY) === '1'; } catch (e) { S.lens = false; }
    if (LL.query && LL.query.get('lens') === '1') { S.lens = true; LL.query.delete('lens'); try { localStorage.setItem(LENS_KEY, '1'); } catch (e) { /* private mode */ } }
    setTimeout(() => loadLens(), 0);
    return () => { if (S.map === m) S.map = null; };
  };
  R.refresh = function () { if (S.map) loadLens(); };
  R.lensOn = () => S.lens;
  R.toggleLens = function (on) {
    S.lens = on == null ? !S.lens : !!on;
    try { localStorage.setItem(LENS_KEY, S.lens ? '1' : '0'); } catch (e) { /* private mode */ }
    if (!S.map) { if (S.lens) LL.go('#/network?lens=1'); return; }
    loadLens(true);
    if (LL.announce) LL.announce(t(S.lens ? 'rs.lens.on_said' : 'rs.lens.off_said'), { now: true });
    document.dispatchEvent(new CustomEvent('ll-lens'));
  };
  // B toggles the lens on a map (not while typing, in a dialog or the palette)
  document.addEventListener('keydown', ev => {
    if (!S.map || ev.key == null || ev.key.toLowerCase() !== 'b' || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const el = ev.target;
    if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
    if (document.querySelector('.modal, .cmdk')) return;
    ev.preventDefault();
    R.toggleLens();
  });

  // ------------------------------------------------------------------ the start page: grade and the worst spots
  function peek() {
    if (S.peeking || Date.now() - S.peekAt < 60000) return;
    S.peeking = true; S.peekAt = Date.now();
    api('/api/resilience').then(d => {
      const had = S.peek && S.peek.d.score ? S.peek.d.score.score : null;
      S.peek = { t: Date.now(), d };
      if (had !== (d.score ? d.score.score : null) && LL.overviewRedraw) LL.overviewRedraw();
    }).catch(() => null).finally(() => { S.peeking = false; });
  }
  if (LL.registerOverviewCard) {
    LL.registerOverviewCard({ id: 'resilience', order: 13.5, title: () => t('rs.title'),
      available: () => { peek(); return !!(S.peek && S.peek.d.score); },
      render: () => {
        const d = S.peek.d, sc = d.score;
        const top = (d.weak_spots || []).filter(w => ['high', 'critical', 'elevated'].includes(w.level)).slice(0, 3);
        return h('div', { class: 'rs-ov' },
          h('div', { class: 'rs-ov__grade' }, h('span', { class: 'rs-ov__letter rs-hero--' + sc.grade, 'aria-hidden': 'true' }, sc.grade),
            h('span', {}, h('strong', {}, t('rs.grade.word.' + sc.grade)), h('span', { class: 'muted' }, ' · ' + t('rs.grade.score', { score: sc.score })))),
          top.length ? h('ul', { class: 'rs-ov__list' }, top.map(w => h('li', {}, riskChip(w.risk, w.level), ' ', h('a', { href: '#/resilience/part/' + enc(w.id) }, partName(w))))) : h('p', { class: 'muted' }, t('rs.ov.calm')),
          h('div', { class: 'btn-row btn-row--wrap' }, h('a', { class: 'btn btn--ghost btn--sm', href: '#/resilience' }, t('rs.ov.open')),
            h('a', { class: 'btn btn--ghost btn--sm', href: '#/resilience/scenarios' }, t('rs.sc.title'))));
      } });
  }

  // ------------------------------------------------------------------ palette
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'go.resilience', group: 'views', order: 3.64, label: () => t('cmdk.go', { name: t('rs.title') }), keywords: () => t('rs.cmd.kw'), run: () => LL.go('#/resilience') });
    LL.registerCommand({ id: 'risk-lens', group: 'commands', order: 23, label: () => t(S.lens ? 'rs.cmd.lens_off' : 'rs.cmd.lens_on'), keywords: () => t('rs.cmd.kw_lens'), run: () => R.toggleLens() });
    LL.registerCommand({ id: 'emergency-scenarios', group: 'commands', order: 23.1, label: 'rs.cmd.scenarios', keywords: () => t('rs.cmd.kw_scenarios'), run: () => LL.go('#/resilience/scenarios') });
    LL.registerCommand({ id: 'scenario-power', group: 'commands', order: 23.2, typedOnly: true, label: 'rs.cmd.power', keywords: () => t('rs.cmd.kw_scenarios'), run: () => LL.failure && LL.failure.scenario('power') });
    LL.registerCommand({ id: 'scenario-internet', group: 'commands', order: 23.3, typedOnly: true, label: 'rs.cmd.internet', keywords: () => t('rs.cmd.kw_scenarios'), run: () => LL.failure && LL.failure.scenario('internet') });
  }
  if (LL.registerAction) {
    LL.registerAction({ id: 'risk.part', kinds: ['device'], order: 6, label: 'rs.act.risk', available: hit => !!hit.id, run: hit => LL.go('#/resilience/part/' + enc(hit.id)) });
  }
})();
