/* Lanloom — rewind (S7): play the last 24 hours (or 7 days) back on the board.

   Data: GET /api/rewind?range=24h|7d&view=overview|network (lanloom/rewind.py) - one answer per range holds the traffic of every
   trace as level characters, the 5-minute states of the tiles that had trouble, the gaps when Lanloom did not run, the incidents
   and a few events. Nothing is asked again while playing.

   The bar sits above the board: play / pause, step, speed 60× / 300× / 900× / 3600×, range 24 hours / 7 days, back to live.
   The time line under it shows the activity, the incidents as coloured bands (outage red, flapping and blips yellow,
   maintenance grey), the gaps hatched; a click or drag moves the cursor. "At this moment" lists the incidents and the parts
   that were down at the cursor (on phones the bar is a sheet and this list is open).

   Keys while the rewind is open: Space play / pause, ← / → one step (Shift: one hour, in 7 days one day), 1-4 speed, W 24 hours /
   7 days, R from the start of the range, Esc back to live. On a map R opens the rewind.

   Targets register themselves (LL.rewind.target(id, {apply(state), leave()})): the map and the network board (statuses of the
   tiles, dots and rates of that moment) and the phone list; a feature that wants to follow the cursor adds its own.
   Interface: LL.rewind.open(at, range), .close(), .active(), .attach(map) (views.js), .chip(), .at(), .parseTime(text). */
(function () {
  'use strict';
  const LL = window.Lanloom;
  const { h, t, api } = LL;
  const R = LL.rewind = {};
  const ALPHA = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_';
  const LUT = Array.from({ length: 64 }, (_, i) => (i ? Math.pow(2, (i - 0.5) / 2) : 0));      // the middle of each level band (lanloom/rewind.py)
  const SPEEDS = [60, 300, 900, 3600];
  const RANGES = ['24h', '7d'];
  const KIND_TOKEN = { outage: '--ll-danger', flapping: '--ll-warn', blip: '--ll-warn', maintenance: '--ll-text-3' };
  const ST = { u: 'up', d: 'down', p: 'partial', '.': 'unknown' };
  const S = { open: false, range: '24h', d: null, busy: false, err: null, at: null, playing: false, speed: 300, map: null, raf: 0, last: 0,
    pushed: 0, targets: new Map(), nr: 0, list: false, want: null };
  const phone = () => window.matchMedia && window.matchMedia('(max-width: 760px)').matches;
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const loc = () => LL.locale();

  // ------------------------------------------------------------------ data
  function decode(d) {
    const tr = {};
    for (const id in d.traffic) {
      const [a, b] = d.traffic[id];
      const x = new Uint8Array(a.length), y = new Uint8Array(b.length);
      for (let i = 0; i < a.length; i++) { x[i] = ALPHA.indexOf(a[i]); y[i] = ALPHA.indexOf(b[i]); }
      tr[id] = [x, y];
    }
    const tot = new Uint8Array(d.totals.length);
    for (let i = 0; i < d.totals.length; i++) tot[i] = ALPHA.indexOf(d.totals[i]);
    return Object.assign({}, d, { tr, tot, end: d.t0 + d.n * d.step });
  }
  const clampAt = ts => (S.d ? Math.max(S.d.t0, Math.min(S.d.now, ts)) : ts);
  function idxAt(ts) { return Math.max(0, Math.min(S.d.n - 1, Math.floor((ts - S.d.t0) / S.d.step))); }
  function slotAt(ts) { return Math.floor((ts - S.d.slot_t0) / S.d.slot); }

  // what the board shows at the cursor: statuses, rates and dots, the incidents and down parts of that moment
  function stateAt(ts) {
    const d = S.d, i = idxAt(ts), si = slotAt(ts);
    const status = {}, down = [];
    for (const id in d.states) {
      const ch = d.states[id][si];
      if (ch && ch !== 'u') { status[id] = ST[ch] || 'unknown'; if (ch === 'd' || ch === 'p') down.push(id); }
    }
    const gapCh = d.gap ? d.gap[si] : 'u';
    const rates = {}, dots = [];
    const q = d.q || 64000;
    for (const id in d.tr) {
      const a = LUT[d.tr[id][0][i]], b = LUT[d.tr[id][1][i]];
      if (!a && !b) continue;
      rates[id] = [Math.round(a), Math.round(b)];
      if (a) dots.push([id, 0, Math.max(1, Math.min(6, Math.round(a / q)))]);
      if (b) dots.push([id, 1, Math.max(1, Math.min(6, Math.round(b / q)))]);
    }
    const incidents = (d.marks || []).filter(m => m.start <= ts && (m.end == null || m.end >= ts));
    // the tiles' live figures belong to now: in the past they stay empty (the dots carry the traffic of that moment)
    const metrics = {};
    const v = S.map && S.map.view && S.map.view();
    if (v) v.components.forEach(c => { metrics[c.id] = null; });
    return { at: ts, index: i, slot: si, status, fallback: gapCh === '.' ? 'unknown' : 'up', gap: gapCh === '.', down, rates, dots, incidents,
      frame: { t: ts, q: d.q, dots, rates, metrics, rewind: true } };
  }

  async function load(range) {
    const view = S.map && S.map.kind === 'network' ? 'network' : 'overview';
    const nr = ++S.nr;
    S.busy = true; S.err = null; render();
    let d;
    try { d = await api('/api/rewind?range=' + range + '&view=' + view); }
    catch (e) { if (nr === S.nr) { S.busy = false; S.err = LL.errText(e); render(); } return false; }
    if (nr !== S.nr) return false;
    S.d = decode(d); S.range = range; S.busy = false;
    return true;
  }

  // ------------------------------------------------------------------ open, close, play
  R.open = async function (at, range) {
    range = RANGES.includes(range) ? range : (at && Date.now() / 1000 - at > 86400 ? '7d' : S.range);
    if (!S.map) {                                         // not on a map: the map opens with the rewind
      S.want = { at: at || null, range };
      LL.go('#/map');
      return;
    }
    const was = S.open;
    S.open = true; S.list = phone();
    if (!was) S.speed = range === '7d' ? 3600 : 300;
    if (!S.d || S.d.range !== range || Date.now() / 1000 - S.d.now > 60) { if (!(await load(range))) return; }
    S.at = clampAt(at || S.d.t0);
    S.playing = !at;
    S.last = 0;
    push(true); render(); loop();
    const act = document.activeElement;
    if (act && act.classList && act.classList.contains('mf-chip--rewind') && bar) { const pb = bar.querySelector('.rw-play'); if (pb) pb.focus({ preventScroll: true }); }
    LL.announce && LL.announce(t('rw.opened', { time: stamp(S.at, true) }), { now: true });
  };
  R.close = function () {
    if (!S.open) return;
    S.open = false; S.playing = false; clearInterval(S.raf); S.raf = 0;
    S.targets.forEach(x => { try { x.leave && x.leave(); } catch (e) { /* a target must not stop the others */ } });
    render();
    LL.announce && LL.announce(t('rw.closed'), { now: true });
  };
  R.active = () => S.open;
  R.at = () => (S.open ? S.at : null);
  R.state = () => ({ open: S.open, range: S.range, at: S.at, playing: S.playing, speed: S.speed });      // test hook
  R.target = function (id, x) { S.targets.set(id, x); if (S.open && S.d) { try { x.apply(stateAt(S.at)); } catch (e) { /* later */ } } return () => { if (S.targets.get(id) === x) S.targets.delete(id); }; };

  function push(force) {
    if (!S.open || !S.d) return;
    const st = stateAt(S.at);
    S.cur = st;
    S.targets.forEach(x => { try { x.apply(st); } catch (e) { /* a target must not stop the others */ } });
    if (force || !S.playing) S.pushed = performance.now();
  }
  // a steady 10 Hz clock (not animation frames: those rest in background tabs and power-saving browsers, the time would stand still)
  function loop() {
    clearInterval(S.raf);
    S.last = 0;
    S.raf = setInterval(() => {
      if (!S.open) { clearInterval(S.raf); S.raf = 0; return; }
      const now = performance.now();
      if (S.playing && S.d) {
        const dt = S.last ? Math.min(0.5, (now - S.last) / 1000) : 0;
        S.at += dt * S.speed;
        if (S.at >= S.d.now) { S.at = S.d.now; S.playing = false; renderHead(); }
        drawCursor();
        if (now - S.pushed >= 1000 || !S.playing) { S.pushed = now; push(); renderNow(); renderTime(); }
      }
      S.last = now;
    }, 100);
  }
  function seek(ts, play) {
    if (!S.d) return;
    S.at = clampAt(ts);
    if (play != null) S.playing = play;
    push(true); drawCursor(); renderNow(); renderTime(); renderHead();
  }
  function setSpeed(v) { S.speed = v; renderHead(); }
  async function setRange(r) {
    if (r === S.range && S.d) return;
    const keep = S.at;
    if (!(await load(r))) return;
    S.speed = r === '7d' ? 3600 : 300;
    S.at = clampAt(keep || S.d.t0);
    push(true); render();
  }
  function togglePlay() {
    if (!S.d) return;
    if (!S.playing && S.at >= S.d.now - S.d.step) S.at = S.d.t0;      // at the end: play from the start again
    S.playing = !S.playing; S.last = 0;
    renderHead(); push(true);
  }
  function stepBy(n, big) {
    if (!S.d) return;
    const unit = big ? (S.range === '7d' ? 86400 : 3600) : S.d.step;
    seek(S.at + n * unit, false);
  }

  // ------------------------------------------------------------------ the map: attach, the board as target
  R.attach = function (m) {
    S.map = m;
    const off = R.target('board-' + m.kind, {
      apply(st) {
        const b = m.board(); if (!b) return;
        if (b.setRewind) b.setRewind({ status: st.status, fallback: st.fallback });
        if (b.live) b.live(st.frame);
        if (b.setOutage) b.setOutage(null);
      },
      leave() {
        const b = m.board();
        if (b && b.setRewind) b.setRewind(null);
        if (m.onLeave) m.onLeave();
      }
    });
    const want = S.want || (LL.query && LL.query.get('rw') ? { at: Number(LL.query.get('rw')) || null, range: LL.query.get('rwr') || null } : null);
    S.want = null;
    if (LL.query) { LL.query.delete('rw'); LL.query.delete('rwr'); }
    if (want) setTimeout(() => R.open(want.at, want.range || undefined), 0);
    else if (S.open) { S.open = false; S.d = null; }
    return () => { off(); if (S.map === m) { S.map = null; if (S.open) { S.open = false; S.playing = false; clearInterval(S.raf); } } };
  };
  R.chip = function () {
    return h('button', { class: 'mf-chip mf-chip--rewind' + (S.open ? ' is-on' : ''), type: 'button', 'aria-pressed': S.open ? 'true' : 'false',
      title: t('rw.chip.title'), onclick: () => (S.open ? R.close() : R.open()) }, h('span', { class: 'rw-chip-icon', 'aria-hidden': 'true' }), t('rw.chip'));
  };

  // ------------------------------------------------------------------ texts
  function stamp(ts, long) {
    const o = S.range === '7d' || long ? { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false } : { hour: '2-digit', minute: '2-digit', hour12: false };
    return new Date(ts * 1000).toLocaleString(loc(), o);
  }
  const ago = ts => t('time.ago', { age: LL.fmtAge(Math.max(0, Date.now() / 1000 - ts)) });
  function partName(id) {
    const v = S.map && S.map.view && S.map.view();
    const c = v && v.components.find(x => x.id === id);
    return c ? (c.title || t('kind.' + (c.kind || 'unknown'))) : id;
  }
  function incName(m) {
    if (m.root === 'lanloom') return 'Lanloom';
    if (m.root === 'internet') return t('kind.internet');
    return m.title || partName(m.root);
  }

  // ------------------------------------------------------------------ the bar
  let bar = null, canvas = null, els = {};
  function host() { return S.map && S.map.slot && S.map.slot(); }
  function render() {
    const hst = host();
    if (!hst) return;
    hst.classList.toggle('has-rw', S.open);
    if (!S.open) { if (bar) bar.remove(); bar = null; document.querySelectorAll('.mf-chip--rewind').forEach(c => { c.classList.remove('is-on'); c.setAttribute('aria-pressed', 'false'); }); return; }
    document.querySelectorAll('.mf-chip--rewind').forEach(c => { c.classList.add('is-on'); c.setAttribute('aria-pressed', 'true'); });
    const fresh = !bar || !bar.isConnected;
    if (fresh) bar = h('section', { class: 'rw-bar', role: 'region', 'aria-label': t('rw.region') });
    bar.classList.toggle('is-list', S.list);
    els.head = h('div', { class: 'rw-bar__head' });
    els.time = h('div', { class: 'rw-time', 'aria-live': 'off' });
    els.now = h('div', { class: 'rw-now' });
    canvas = h('canvas', { class: 'rw-line', height: '46', role: 'slider', tabindex: '0', 'aria-label': t('rw.slider'), 'aria-valuemin': '0', 'aria-valuemax': '100' });
    els.ticks = h('div', { class: 'rw-ticks', 'aria-hidden': 'true' });
    const kids = [els.head];
    if (S.busy && !S.d) kids.push(h('p', { class: 'muted rw-msg' }, t('rw.loading')));
    else if (S.err) kids.push(h('p', { class: 'msg msg--warn rw-msg' }, S.err));
    if (S.d) kids.push(h('div', { class: 'rw-main' }, els.time, h('div', { class: 'rw-track' }, canvas, els.ticks)), els.now);
    bar.replaceChildren(...kids);
    if (fresh) { hst.append(bar); }
    renderHead(); renderTime(); renderNow();
    if (S.d) { wireCanvas(); setTimeout(() => { drawLine(); drawTicks(); }, 0); }
  }
  function renderHead() {
    if (!els.head) return;
    const play = h('button', { class: 'btn btn--primary btn--sm rw-play', type: 'button', 'aria-label': t(S.playing ? 'rw.pause' : 'rw.play'), title: t(S.playing ? 'rw.pause' : 'rw.play') + ' (' + t('rw.key.space') + ')',
      onclick: togglePlay }, h('span', { class: 'rw-ico rw-ico--' + (S.playing ? 'pause' : 'play'), 'aria-hidden': 'true' }), h('span', { class: 'rw-play__text' }, t(S.playing ? 'rw.pause' : 'rw.play')));
    const back = h('button', { class: 'btn btn--ghost btn--icon btn--sm', type: 'button', 'aria-label': t('rw.back'), title: t('rw.back') + ' (←)', onclick: () => stepBy(-1) }, '‹');
    const fwd = h('button', { class: 'btn btn--ghost btn--icon btn--sm', type: 'button', 'aria-label': t('rw.fwd'), title: t('rw.fwd') + ' (→)', onclick: () => stepBy(1) }, '›');
    const speeds = h('div', { class: 'seg rw-seg', role: 'group', 'aria-label': t('rw.speed') }, SPEEDS.map((v, i) => h('button', {
      class: 'seg__btn' + (S.speed === v ? ' is-active' : ''), type: 'button', 'aria-pressed': S.speed === v ? 'true' : 'false', title: t('rw.speed.title', { n: v, key: i + 1 }),
      onclick: () => setSpeed(v) }, v + '×')));
    const ranges = h('div', { class: 'seg rw-seg', role: 'group', 'aria-label': t('rw.range') }, RANGES.map(r => h('button', {
      class: 'seg__btn' + (S.range === r ? ' is-active' : ''), type: 'button', 'aria-pressed': S.range === r ? 'true' : 'false', title: t('rw.range.title'),
      onclick: () => setRange(r) }, t('rw.range.' + r))));
    const listBtn = phone() ? null : h('button', { class: 'btn btn--ghost btn--sm rw-listbtn', type: 'button', 'aria-expanded': S.list ? 'true' : 'false',
      onclick: () => { S.list = !S.list; bar.classList.toggle('is-list', S.list); renderHead(); renderNow(); } }, t(S.list ? 'rw.list.hide' : 'rw.list.show'));
    const close = h('button', { class: 'btn btn--sm rw-live', type: 'button', title: t('rw.live.title'), onclick: () => R.close() }, h('span', { class: 'rw-live__dot', 'aria-hidden': 'true' }), t('rw.live'));
    const hadPlay = document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('rw-play');
    els.head.replaceChildren(
      h('div', { class: 'rw-bar__title' }, h('span', { class: 'rw-bar__icon', 'aria-hidden': 'true' }), h('h2', {}, t('rw.title.' + S.range))),
      h('div', { class: 'rw-bar__tools' }, h('div', { class: 'rw-transport' }, back, play, fwd), speeds, ranges, listBtn, close));
    if (hadPlay) play.focus({ preventScroll: true });
  }
  function renderTime() {
    if (!els.time || !S.d) return;
    const st = S.cur || stateAt(S.at);
    els.time.replaceChildren(
      h('b', { class: 'rw-time__clock' }, stamp(S.at, true)),
      h('span', { class: 'rw-time__ago muted' }, Math.abs(S.d.now - S.at) < 90 ? t('rw.at_now') : ago(S.at)),
      st.gap ? h('span', { class: 'chip chip--muted rw-time__chip' }, t('rw.gap')) : st.down.length ? h('span', { class: 'chip chip--danger rw-time__chip' }, t('rw.down_n', { n: st.down.length })) : h('span', { class: 'chip chip--ok rw-time__chip' }, t('rw.all_up')));
    if (canvas) { const p = Math.round((S.at - S.d.t0) / (S.d.now - S.d.t0) * 100); canvas.setAttribute('aria-valuenow', String(p)); canvas.setAttribute('aria-valuetext', stamp(S.at, true)); }
  }
  function renderNow() {
    if (!els.now || !S.d) return;
    const st = S.cur || stateAt(S.at);
    const rows = [];
    st.incidents.forEach(m => rows.push(h('li', { class: 'rw-inc rw-inc--' + m.kind },
      h('span', { class: 'rw-inc__mark', 'aria-hidden': 'true' }),
      h('span', { class: 'rw-inc__main' }, h('span', { class: 'rw-inc__name' }, incName(m)),
        h('span', { class: 'muted rw-inc__sub' }, t('inc.kind.' + m.kind) + (m.members ? ' · ' + t('inc.members_n', { n: m.members }) : ''))),
      h('a', { class: 'btn btn--ghost btn--sm', href: '#/incidents/' + encodeURIComponent(m.id) }, t('rw.open_file')))));
    const extra = st.down.filter(id => !st.incidents.some(m => m.root === id));
    if (S.list && extra.length) rows.push(h('li', { class: 'rw-down' }, h('span', { class: 'muted' }, t('rw.down_list') + ' '), extra.slice(0, 12).map(partName).join(', ') + (extra.length > 12 ? ' …' : '')));
    if (!rows.length) { els.now.replaceChildren(S.list ? h('p', { class: 'muted rw-calm' }, t('rw.calm')) : ''); els.now.hidden = !S.list; return; }
    els.now.hidden = false;
    els.now.replaceChildren(h('ul', { class: 'rw-incs', 'aria-label': t('rw.now_label') }, S.list ? rows : rows.slice(0, 2)));
  }

  // ------------------------------------------------------------------ the time line
  function fit() {
    const dpr = window.devicePixelRatio || 1, w = Math.max(120, canvas.clientWidth || 600), hh = 46;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(hh * dpr);
    const g = canvas.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, hh);
    return { g, w, h: hh };
  }
  let lineImg = null;
  function drawLine() {
    if (!canvas || !canvas.isConnected || !S.d) return;
    const d = S.d, { g, w, h: hh } = fit();
    const x = ts => (ts - d.t0) / (d.now - d.t0) * w;
    g.fillStyle = css('--ll-surface-2') || '#f4f4f4'; g.fillRect(0, 10, w, hh - 10);
    // gaps: Lanloom did not run (hatched)
    if (d.gap) {
      g.strokeStyle = css('--ll-border'); g.lineWidth = 1;
      for (let i = 0; i < d.gap.length; i++) {
        if (d.gap[i] !== '.') continue;
        const x0 = x(d.slot_t0 + i * d.slot), x1 = x(d.slot_t0 + (i + 1) * d.slot);
        g.save(); g.beginPath(); g.rect(x0, 10, Math.max(1, x1 - x0), hh - 10); g.clip();
        for (let k = x0 - hh; k < x1; k += 5) { g.beginPath(); g.moveTo(k, hh); g.lineTo(k + hh, 10); g.stroke(); }
        g.restore();
      }
    }
    // activity (all traces together; the levels are logarithmic already)
    const n = d.tot.length;
    let lo = 63, hi = 0;
    for (let i = 0; i < n; i++) if (d.tot[i]) { lo = Math.min(lo, d.tot[i]); hi = Math.max(hi, d.tot[i]); }
    lo = Math.max(0, lo - 2);
    const yOf = lv => hh - (lv ? (lv - lo) / Math.max(1, hi - lo) : 0) * (hh - 16);       // the curve uses the height between the quietest and the busiest step
    g.beginPath(); g.moveTo(0, hh);
    for (let i = 0; i < n; i++) g.lineTo((i + 0.5) / n * w, yOf(d.tot[i]));
    g.lineTo(w, hh); g.closePath();
    g.fillStyle = css('--ll-accent-soft') || 'rgba(0,0,0,.08)'; g.fill();
    g.strokeStyle = css('--ll-accent'); g.lineWidth = 1.2; g.beginPath();
    for (let i = 0; i < n; i++) { const xx = (i + 0.5) / n * w, yy = yOf(d.tot[i]); if (i) g.lineTo(xx, yy); else g.moveTo(xx, yy); }
    g.stroke();
    // incidents: bands on top, a soft tint down the line
    (d.marks || []).forEach(m => {
      const x0 = x(Math.max(d.t0, m.start)), x1 = Math.max(x0 + 3, x(m.end == null ? d.now : m.end));
      const col = css(KIND_TOKEN[m.kind] || '--ll-warn');
      g.globalAlpha = 0.14; g.fillStyle = col; g.fillRect(x0, 10, x1 - x0, hh - 10);
      g.globalAlpha = 1; g.fillRect(x0, 2, x1 - x0, 6);
    });
    // events: thin ticks at the bottom (new device, internet line, own update)
    g.fillStyle = css('--ll-text-3');
    (d.events || []).forEach(ev => { g.fillRect(Math.round(x(ev.t)), hh - 5, 1, 5); });
    lineImg = g.getImageData(0, 0, canvas.width, canvas.height);
    drawCursor();
  }
  function drawCursor() {
    if (!canvas || !canvas.isConnected || !S.d || !lineImg) return;
    const g = canvas.getContext('2d'), dpr = window.devicePixelRatio || 1;
    g.save(); g.setTransform(1, 0, 0, 1, 0, 0); g.putImageData(lineImg, 0, 0); g.restore();
    const w = canvas.width / dpr, hh = canvas.height / dpr;
    const xx = Math.round((S.at - S.d.t0) / (S.d.now - S.d.t0) * w);
    g.fillStyle = css('--ll-text'); g.fillRect(xx - 1, 0, 2, hh);
    g.beginPath(); g.arc(xx, 9, 4.5, 0, Math.PI * 2); g.fill();
  }
  function drawTicks() {
    if (!els.ticks || !S.d) return;
    const d = S.d, span = d.now - d.t0, every = S.range === '7d' ? 86400 : (canvas.clientWidth < 520 ? 6 * 3600 : 3 * 3600);
    const first = Math.ceil(d.t0 / every) * every, out = [];
    const tz = new Date().getTimezoneOffset() * 60;
    for (let ts = first + (S.range === '7d' ? tz : 0); ts <= d.now; ts += every) {
      const p = (ts - d.t0) / span * 100;
      if (p < 3 || p > 97) continue;
      out.push(h('span', { class: 'rw-tick', style: 'left:' + (Math.round(p * 100) / 100) + '%' },
        S.range === '7d' ? new Date(ts * 1000).toLocaleDateString(loc(), { weekday: 'short', day: 'numeric' }) : new Date(ts * 1000).toLocaleTimeString(loc(), { hour: '2-digit', minute: '2-digit', hour12: false })));
    }
    els.ticks.replaceChildren(...out);
  }
  function wireCanvas() {
    const tsOf = ev => { const r = canvas.getBoundingClientRect(); return S.d.t0 + Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)) * (S.d.now - S.d.t0); };
    let drag = false, was = false;
    canvas.addEventListener('pointerdown', ev => { drag = true; was = S.playing; canvas.setPointerCapture(ev.pointerId); seek(tsOf(ev), false); });
    canvas.addEventListener('pointermove', ev => {
      const ts = tsOf(ev);
      if (drag) { seek(ts, false); return; }
      const m = (S.d.marks || []).find(x => x.start <= ts && (x.end == null ? S.d.now : x.end) >= ts);
      canvas.title = stamp(ts, true) + (m ? ' · ' + t('inc.kind.' + m.kind) + ': ' + incName(m) : '');
    });
    const end = () => { if (drag) { drag = false; if (was) { S.playing = true; S.last = 0; renderHead(); } } };
    canvas.addEventListener('pointerup', end); canvas.addEventListener('pointercancel', end);
    canvas.addEventListener('dblclick', ev => { const ts = tsOf(ev); const m = (S.d.marks || []).find(x => x.start <= ts && (x.end == null ? S.d.now : x.end) >= ts); if (m) LL.go('#/incidents/' + encodeURIComponent(m.id)); });
    if (typeof ResizeObserver !== 'undefined') {
      let w = 0;
      new ResizeObserver(() => { if (canvas.isConnected && canvas.clientWidth !== w) { w = canvas.clientWidth; drawLine(); drawTicks(); } }).observe(canvas);
    }
  }

  // ------------------------------------------------------------------ keys
  function typing(ev) {
    const el = ev.target;
    return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }
  window.addEventListener('keydown', ev => {
    if (ev.metaKey || ev.ctrlKey || ev.altKey || typing(ev) || document.querySelector('.modal, .cmdk')) return;
    if (!S.open) {
      if ((ev.key === 'r' || ev.key === 'R') && S.map && !ev.repeat) {
        const panel = document.querySelector('.detail');
        if (panel && !panel.hidden && panel.contains(document.activeElement)) return;
        ev.preventDefault(); R.open();
      }
      return;
    }
    let done = true;
    if (ev.key === ' ' || ev.key === 'Spacebar') {
      const el = ev.target;                            // a focused control keeps its own Space (the play button toggles by itself)
      if (el && /^(BUTTON|A|CANVAS)$/.test(el.tagName) && !el.classList.contains('mf-chip--rewind') && el !== canvas) return;
      togglePlay();
    }
    else if (ev.key === 'ArrowLeft') stepBy(-1, ev.shiftKey);
    else if (ev.key === 'ArrowRight') stepBy(1, ev.shiftKey);
    else if (/^[1-4]$/.test(ev.key)) setSpeed(SPEEDS[Number(ev.key) - 1]);
    else if (ev.key === 'w' || ev.key === 'W') setRange(S.range === '24h' ? '7d' : '24h');
    else if (ev.key === 'r' || ev.key === 'R') seek(S.d ? S.d.t0 : S.at, true);
    else if (ev.key === 'Escape') R.close();
    else done = false;
    if (done) { ev.preventDefault(); ev.stopPropagation(); }
  }, true);

  // ------------------------------------------------------------------ "Rewind to 14:30" (palette), deep links
  // understood: 14:30 · 2:30 pm · yesterday 14:30 · Mon 9:00 · 3 h / 90 min ago (English and German words)
  R.parseTime = function (text, now) {
    now = now || Date.now() / 1000;
    let s = String(text || '').toLowerCase().trim();
    if (!s) return null;
    const ago = s.match(/^(\d+(?:[.,]\d+)?)\s*(h|std|stunden?|hours?|m|min|minuten?|minutes?|d|tage?|days?)\b/);
    if (ago) {
      const v = parseFloat(ago[1].replace(',', '.')), u = ago[2][0];
      const sec = u === 'h' || u === 's' ? v * 3600 : u === 'd' || u === 't' ? v * 86400 : v * 60;
      return sec > 0 && sec <= 7 * 86400 ? now - sec : null;
    }
    let dayOff = null;
    if (/^(yesterday|gestern)\b/.test(s)) { dayOff = 1; s = s.replace(/^(yesterday|gestern)\s*/, ''); }
    else if (/^(today|heute)\b/.test(s)) { dayOff = 0; s = s.replace(/^(today|heute)\s*/, ''); }
    const days = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'], tage = ['so', 'mo', 'di', 'mi', 'do', 'fr', 'sa'];
    const wd = s.match(/^([a-zäöü]{2,})\.?\s+/);
    let want = null;
    if (wd) {
      const w = wd[1].slice(0, 3), w2 = wd[1].slice(0, 2);
      want = days.indexOf(w) >= 0 ? days.indexOf(w) : tage.indexOf(w2) >= 0 && w.length >= 2 && !days.includes(w) ? tage.indexOf(w2) : null;
      if (want != null) s = s.slice(wd[0].length);
    }
    const m = s.match(/^(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm|uhr)?$/);
    if (!m) return null;
    let hh = Number(m[1]); const mm = Number(m[2] || 0);
    if (m[3] === 'pm' && hh < 12) hh += 12;
    if (m[3] === 'am' && hh === 12) hh = 0;
    if (hh > 23 || mm > 59 || (!m[2] && !m[3])) return null;
    const d = new Date(now * 1000);
    d.setHours(hh, mm, 0, 0);
    if (want != null) { let back = (d.getDay() - want + 7) % 7; d.setDate(d.getDate() - back); if (d.getTime() / 1000 > now) d.setDate(d.getDate() - 7); }
    else if (dayOff != null) d.setDate(d.getDate() - dayOff);
    else if (d.getTime() / 1000 > now) d.setDate(d.getDate() - 1);      // a time later than now: yesterday
    const ts = d.getTime() / 1000;
    return ts <= now && now - ts <= 7 * 86400 ? ts : null;
  };
  if (LL.registerCommand) {
    LL.registerCommand({ id: 'rewind', group: 'commands', order: 23, label: 'rw.cmd', keywords: () => t('rw.cmd.kw'), run: () => R.open() });
    LL.registerCommand({ id: 'rewind-7d', group: 'commands', order: 23.1, typedOnly: true, label: 'rw.cmd.week', keywords: () => t('rw.cmd.kw'), run: () => R.open(null, '7d') });
    // "Rewind to <time>": the palette asks parse(query) and shows the command with the time it understood
    LL.registerCommand({ id: 'rewind-to', group: 'commands', order: 22.9, typedOnly: true, label: 'rw.cmd.to_hint', keywords: () => t('rw.cmd.kw'),
      parse: q => { const m = String(q || '').match(/^\s*(?:rewind(?:\s+to)?|zurückspulen(?:\s+auf)?|rückblick(?:\s+auf)?|go\s+back\s+to)\s+(.+)$/i); const ts = m ? R.parseTime(m[1]) : null; return ts ? { at: ts } : null; },
      parsedLabel: arg => t('rw.cmd.to', { time: new Date(arg.at * 1000).toLocaleString(loc(), { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) }),
      run: arg => R.open(arg && arg.at ? arg.at : null) });
  }
  window.addEventListener('hashchange', () => { if (S.open && !/^#\/(map|network)\b/.test(location.hash)) { S.open = false; S.playing = false; clearInterval(S.raf); } });
})();
