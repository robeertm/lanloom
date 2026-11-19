/* Lanloom — host health and early warnings (W1): the "Host health" section of the device card (health ring, round gauges,
   freshness of every source, the trend of memory, swap, disk, load and temperature), the overview cards "Early warnings"
   and "Waiting on someone", and the palette entry. Colours: green below 70 %, yellow 70–90 %, red above 90 %; a source that
   stopped delivering is dimmed ("stale · x min ago"), never red. Everything here only reads. */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const SVGNS = 'http://www.w3.org/2000/svg';
  const ORDER = ['cpu', 'mem', 'disk', 'temp', 'swap', 'load', 'freq'];
  const alive = el => el.isConnected;
  const fx = v => String(Math.round(v * 100) / 100);          // SVG geometry: always a decimal point

  function svg(tag, attrs, kids) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
    (kids || []).forEach(k => k && el.appendChild(k));
    return el;
  }
  const num = (v, d) => LL.fmtNum(v, d == null ? (Math.abs(v) < 10 ? 1 : 0) : d);
  function valueText(metric, v) {
    if (v == null) return '—';
    if (metric === 'temp') return t('hh.unit.celsius', { v: num(v) });
    return t('hh.unit.pct', { v: num(v) });
  }
  // share of a gauge's arc: temperature on 0..100 °C, everything else on 0..100 %
  const share = v => Math.max(0, Math.min(1, (v || 0) / 100));

  // ------------------------------------------------------------------ round instruments
  function arc(cx, cy, r, from, to) {
    const p = a => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
    const [x1, y1] = p(from), [x2, y2] = p(to);
    return 'M' + fx(x1) + ' ' + fx(y1) + ' A' + r + ' ' + r + ' 0 ' + (to - from > Math.PI ? 1 : 0) + ' 1 ' + fx(x2) + ' ' + fx(y2);
  }
  function gauge(g) {
    const start = Math.PI * 0.75, span = Math.PI * 1.5, end = start + span * Math.max(0.001, share(g.value));
    const label = t('hh.metric.' + g.metric);
    const pic = svg('svg', { class: 'hh-gauge__pic', viewBox: '0 0 100 84', 'aria-hidden': 'true' }, [
      svg('path', { class: 'hh-gauge__track', d: arc(50, 50, 38, start, start + span) }),
      g.value != null ? svg('path', { class: 'hh-gauge__bar', d: arc(50, 50, 38, start, end) }) : null]);
    let sub = '';
    if (g.stale) sub = t('hh.stale', { age: LL.fmtAge(g.age_s) });
    else if (g.metric === 'load') sub = t('hh.load.sub', { load: num(g.raw, 2), n: g.cores });
    else if (g.metric === 'freq') sub = g.throttled ? t('hh.freq.throttled') : t('hh.freq.sub');
    const tip = label + ': ' + valueText(g.metric, g.value) + ' · ' + t('hh.source.' + g.source) + (sub ? ' · ' + sub : '');
    return h('div', { class: 'hh-gauge hh-gauge--' + g.level + (g.stale ? ' is-stale' : ''), dataset: { metric: g.metric }, title: tip, role: 'img', 'aria-label': tip },
      h('div', { class: 'hh-gauge__dial' }, pic, h('b', { class: 'hh-gauge__value' }, valueText(g.metric, g.value))),
      h('span', { class: 'hh-gauge__label' }, label),
      h('span', { class: 'hh-gauge__sub' }, sub || ' '));
  }
  function ring(r) {
    const c = 2 * Math.PI * 44, part = r.share == null ? 0 : Math.max(0.02, Math.min(1, r.share / 100));
    const pic = svg('svg', { class: 'hh-ring__pic', viewBox: '0 0 100 100', 'aria-hidden': 'true' }, [
      svg('circle', { class: 'hh-ring__track', cx: 50, cy: 50, r: 44 }),
      part ? svg('circle', { class: 'hh-ring__bar', cx: 50, cy: 50, r: 44, 'stroke-dasharray': fx(c * part) + ' ' + fx(c), transform: 'rotate(-90 50 50)' }) : null]);
    return h('div', { class: 'hh-ring hh-ring--' + r.level }, pic,
      h('div', { class: 'hh-ring__mid' }, h('b', {}, r.metric ? valueText(r.metric, r.value) : '—'), h('span', {}, r.metric ? t('hh.metric.' + r.metric) : t('hh.ring.none'))));
  }

  // ------------------------------------------------------------------ trends
  function spark(points, level) {
    const pts = (points || []).filter(p => p[1] != null);
    if (pts.length < 2) return null;
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), lo = Math.min(...ys), hi = Math.max(...ys), dy = (hi - lo) || 1;
    const d = pts.map((p, i) => (i ? 'L' : 'M') + fx(2 + (p[0] - x0) / ((x1 - x0) || 1) * 76) + ' ' + fx(20 - (p[1] - lo) / dy * 16)).join(' ');
    return svg('svg', { class: 'hh-spark hh-spark--' + level, viewBox: '0 0 80 22', 'aria-hidden': 'true' }, [svg('path', { d })]);
  }
  function trendText(f) {
    const metric = t('hh.metric.' + f.metric);
    if (f.level === 'warn') return t(f.eta_h === 0 ? 'hh.fc.at_limit' : 'hh.fc.warn', { metric, eta: LL.fmtAge((f.eta_h || 0) * 3600) });
    if (f.shape === 'rising') return f.eta_h != null ? t('hh.fc.rising_eta', { metric, eta: LL.fmtAge(f.eta_h * 3600) }) : t('hh.fc.rising', { metric });
    return t('hh.fc.falling', { metric });
  }
  function rate(f) {
    if (f.slope_h == null) return '';
    const day = f.slope_h * 24, abs = Math.abs(day);
    const v = f.metric === 'temp' ? t('hh.unit.celsius', { v: num(abs, abs < 1 ? 2 : 1) }) : t('hh.unit.pct', { v: num(abs, abs < 1 ? 2 : 1) });
    return t(day >= 0 ? 'hh.fc.per_day_up' : 'hh.fc.per_day_down', { v });
  }
  function trendRow(f) {
    const sub = [t('hh.fc.now', { v: valueText(f.metric, f.current) })];
    if (f.limit != null && f.direction !== -1 && f.shape !== 'falling') sub.push(t('hh.fc.limit', { v: valueText(f.metric, f.limit) }));
    const r = rate(f);
    if (r) sub.push(r);
    sub.push(t('hh.fc.window.' + f.window, { n: f.n }));
    return h('li', { class: 'hh-fc__row hh-fc__row--' + f.level },
      h('span', { class: 'hh-fc__main' }, h('span', { class: 'hh-fc__title' }, trendText(f)), h('span', { class: 'hh-fc__sub' }, sub.join(' · '))),
      spark(f.points, f.level));
  }

  // ------------------------------------------------------------------ the card section
  LL.hostHealthSection = function (deviceId) {
    const body = h('div', { class: 'hh' }, h('div', { class: 'skeleton' }));
    const sec = h('section', { class: 'detail__sec', 'data-sec': 'hosthealth' }, h('h3', {}, t('hh.title')), body);
    async function load() {
      let d;
      try { d = await api('/api/hosthealth/' + encodeURIComponent(deviceId)); } catch (e) { sec.remove(); return; }
      if (!d.known) { sec.remove(); return; }
      const gauges = (d.gauges || []).slice().sort((a, b) => ORDER.indexOf(a.metric) - ORDER.indexOf(b.metric));
      const r = d.ring || {};
      const head = r.level === 'stale' ? t('hh.state.stale', { age: LL.fmtAge(r.age_s) })
        : r.level === 'none' ? t('hh.state.none') : t('hh.state.' + r.level, { metric: t('hh.metric.' + r.metric), v: valueText(r.metric, r.value) });
      const srcs = (d.sources || []).map(s => h('li', { class: 'hh-src' + (s.stale ? ' is-stale' : '') },
        h('i', { class: 'hh-src__dot', 'aria-hidden': 'true' }),
        h('span', {}, t('hh.source.' + s.source) + (s.label ? ' · ' + s.label : '')),
        h('span', { class: 'muted' }, s.stale ? t('hh.stale', { age: LL.fmtAge(s.age_s) }) : t('hh.fresh', { age: LL.fmtAge(s.age_s) }))));
      const moving = (d.forecasts || []).filter(f => f.level === 'warn' || f.level === 'info');
      moving.sort((a, b) => (a.level === 'warn' ? 0 : 1) - (b.level === 'warn' ? 0 : 1));
      const calm = (d.forecasts || []).filter(f => f.level === 'calm').map(f => t('hh.metric.' + f.metric));
      const few = (d.forecasts || []).filter(f => f.level === 'nodata').length;
      body.replaceChildren(
        h('div', { class: 'hh__top' }, ring(r),
          h('div', { class: 'hh__head' }, h('strong', { class: 'hh__state hh__state--' + r.level }, head), h('ul', { class: 'hh-srcs' }, srcs))),
        h('div', { class: 'hh-gauges' }, gauges.map(gauge)),
        h('div', { class: 'hh-fc' }, h('h4', { class: 'hh-fc__head' }, t('hh.fc.title')),
          moving.length ? h('ul', { class: 'hh-fc__list' }, moving.map(trendRow)) : null,
          calm.length ? h('p', { class: 'hh-fc__calm muted' }, t('hh.fc.calm', { list: calm.join(', ') })) : null,
          !moving.length && !calm.length ? h('p', { class: 'hh-fc__calm muted' }, t(few ? 'hh.fc.collecting' : 'hh.fc.none')) : null));
    }
    load();
    const timer = setInterval(() => { if (!alive(sec)) { clearInterval(timer); return; } if (!document.hidden) load(); }, 60000);
    return sec;
  };

  // ------------------------------------------------------------------ overview: early warnings and waiting on someone
  if (LL.registerOverviewCard) {
    LL.registerOverviewCard({ id: 'forecast', order: 12, available: d => (d.forecasts || []).length > 0, title: () => t('ov.card.forecast'),
      count: d => d.forecasts.length,
      render: d => h('ul', { class: 'note-list hh-ov' }, d.forecasts.map(f => h('li', { class: 'note-row note-row--' + ((f.eta_h || 0) < 24 ? 'danger' : 'warn') },
        h('a', { class: 'note-row__link', href: '#/devices/' + encodeURIComponent(f.device) }, h('i', { class: 'note-row__dot', 'aria-hidden': 'true' }),
          h('span', { class: 'note-row__main' },
            h('span', { class: 'note-row__title' }, t('hh.ov.line', { name: f.title, metric: t('hh.metric.' + f.metric), eta: LL.fmtAge((f.eta_h || 0) * 3600) })),
            h('span', { class: 'note-row__sub' }, t('hh.ov.sub', { now: valueText(f.metric, f.current), limit: valueText(f.metric, f.limit) }))))))) });
    LL.registerOverviewCard({ id: 'waiting', order: 14, available: d => (d.waiting || []).length > 0, title: () => t('ov.card.waiting'),
      count: d => d.waiting.length,
      render: d => h('div', {}, h('p', { class: 'muted ov-lead' }, t('ov.waiting.lead')),
        h('ul', { class: 'note-list hh-wait' }, d.waiting.map(n => {
          const tx = LL.noteText(n);
          const since = n.waiting && n.waiting.since ? t('ov.waiting.since', { age: LL.fmtAge(d.t - n.waiting.since) }) : '';
          return h('li', { class: 'note-row note-row--waiting' },
            h('a', { class: 'note-row__link', href: n.link || '#/overview' }, h('i', { class: 'note-row__dot', 'aria-hidden': 'true' }),
              h('span', { class: 'note-row__main' }, h('span', { class: 'note-row__title' }, tx.title),
                h('span', { class: 'note-row__sub' }, [since, tx.sub].filter(Boolean).join(' · ')))));
        }))) });
  }

  // ------------------------------------------------------------------ palette
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'go.forecast', group: 'views', order: 4.6, label: () => t('cmdk.go', { name: t('ov.card.forecast') }), keywords: () => t('cmdk.kw.forecast'),
      run: () => {
        LL.go('#/overview');
        let n = 0;
        const find = () => {
          const el = document.querySelector('[data-card="forecast"]') || (n > 20 ? document.querySelector('.ov-health') : null);
          if (el) { el.scrollIntoView({ block: 'start', behavior: 'smooth' }); return; }
          if (++n < 40) setTimeout(find, 150);
        };
        setTimeout(find, 150);
      } });
  }
})();
