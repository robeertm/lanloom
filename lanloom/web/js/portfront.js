/* Lanloom — the front view ("faceplate") of a switch, router or access point: the ports drawn like the hardware (odd numbers
   on the top row, even below, SFP cages as a small group at the end), link and speed, power over Ethernet, uplink, the number
   of devices behind a port and the health of the last 24 hours. A port opens a panel right below the plate with its profile,
   untagged and tagged networks and power. Read-only: it only shows what the controller reports (GET /api/ports, or
   detail.portfront of a device card); the firewall editor hands in its own tools for the panel (opts.extra, js/portconf.js).
   The pure layout and wording helpers are exported for the Node test (tests/js/portfront_test.js). */
(function () {
  'use strict';

  // ------------------------------------------------------------------ pure helpers (no DOM)
  const LEVELS = ['ok', 'warn', 'bad'];
  const level = v => (LEVELS.includes(v) ? v : 'ok');
  const MARK = { ok: '', warn: '!', bad: '!!' };
  const byIdx = (a, b) => (a.idx - b.idx) || (String(a.id) < String(b.id) ? -1 : 1);

  // One band is a block of pads: one row (a small switch) or two rows with the odd numbers on top and the even numbers below.
  // maxCols is the number of columns that fit; a plate with more ports than that wraps into several bands.
  function portLayout(ports, maxCols) {
    const cols = Math.max(2, Math.floor(maxCols) || 12);
    const all = (ports || []).slice().sort(byIdx);
    const copper = all.filter(p => !p.sfp), sfp = all.filter(p => p.sfp);
    if (copper.length <= Math.min(8, cols)) return { bands: copper.length ? [{ rows: [copper], cols: copper.length }] : [], sfp, single: true };
    const bands = [];
    for (let i = 0; i < copper.length; i += cols * 2) {
      const chunk = copper.slice(i, i + cols * 2);
      const top = chunk.filter(p => p.idx % 2 === 1), bottom = chunk.filter(p => p.idx % 2 === 0);
      bands.push({ rows: [top, bottom], cols: Math.max(top.length, bottom.length) });
    }
    return { bands, sfp, single: false };
  }

  // The short speed on a pad: 100 (Mbit/s), 1G, 2.5G, 10G. fmt formats the number, tr translates the unit key.
  function speedLabel(mbit, fmt, tr) {
    const n = Number(mbit);
    if (!n || n < 0) return '';
    fmt = fmt || String;
    return n >= 1000 ? tr('pf.speed.g', { n: fmt(n / 1000) }) : fmt(n);
  }
  function speedFull(mbit, fmt, tr) {
    const n = Number(mbit);
    if (!n || n < 0) return '';
    fmt = fmt || String;
    return n >= 1000 ? tr('pf.speed.gbit', { n: fmt(n / 1000) }) : tr('pf.speed.mbit', { n: fmt(n) });
  }
  const levelKey = lv => 'pf.level.' + level(lv);          // the wording of the 24-hour health
  const levelMark = lv => MARK[level(lv)];                 // the symbol next to it, so colour is never the only signal
  // "3 link drops, 2 error episodes" or the calm sentence
  function healthText(p, tr) {
    const parts = [];
    if (p.flaps > 0) parts.push(tr('pf.flaps', { n: p.flaps }));
    if (p.errors > 0) parts.push(tr('pf.errors', { n: p.errors }));
    return parts.length ? parts.join(', ') : tr('pf.calm');
  }
  // How many columns fit into a width in pixels: a pad is at least 30 px wide plus a gap of 4 px, the plate has 14 px padding on both
  // sides. Next to the SFP group (84 px) only when the plate is wide enough; on a narrow one the group goes below the pads.
  const SFP_W = 84, WIDE = 420;
  const sfpBeside = width => !(width > 0) || width >= WIDE;
  function colsFor(width, hasSfp) {
    if (!(width > 0)) return 12;
    const reserved = hasSfp && sfpBeside(width) ? SFP_W : 0;
    return Math.max(4, Math.min(24, Math.floor((width - 28 - reserved + 4) / 34)));
  }

  if (typeof module !== 'undefined') {
    module.exports = { portLayout, speedLabel, speedFull, levelKey, levelMark, healthText, colsFor, sfpBeside, level };
  }
  if (typeof window === 'undefined' || !window.Lanloom) return;

  // ------------------------------------------------------------------ the DOM
  const LL = window.Lanloom;
  const { h, t } = LL;
  const ICON = {
    bolt: '<svg viewBox="0 0 12 12" width="10" height="10" fill="currentColor"><path d="M7 .6 2.4 6.8h2.7L4.6 11.4 9.6 5H6.8z"/></svg>',
    up: '<svg viewBox="0 0 12 12" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 10.5V2M2.6 5.4 6 2l3.4 3.4"/></svg>'
  };
  const ico = (name, cls) => { const s = h('span', { class: cls, 'aria-hidden': 'true' }); s.innerHTML = ICON[name]; return s; };
  const fmt = n => LL.fmtNum(n);

  const selected = new Map();            // front id -> port id: the open panel survives a redraw of the page
  let focusMem = null;                   // the pad that had the focus when the page redrew
  document.addEventListener('pointerdown', e => { if (!e.target.closest || !e.target.closest('.pf')) focusMem = null; }, true);

  function portName(p) { return p.name || t('pf.port', { n: p.idx }); }

  function padLabel(p) {
    const parts = [portName(p), p.link ? t('pf.link.up') : t('pf.link.down')];
    if (p.link && p.speed) parts.push(speedFull(p.speed, fmt, t));
    if (p.vlan != null) parts.push(t('pf.vlan', { id: p.vlan }));
    if (p.poe && p.poe.on) parts.push(t('pf.poe.short'));
    if (p.uplink) parts.push(t('pf.uplink'));
    if (p.n_devices > 1) parts.push(t('pf.devices_n', { n: p.n_devices }));
    if (p.level !== 'ok') parts.push(t(levelKey(p.level)));
    return parts.join(', ');
  }

  function pad(front, p, panelId) {
    const lv = level(p.level), poeOn = !!(p.poe && p.poe.on);
    const el = h('button', { type: 'button', class: 'pf-pad' + (p.link ? ' is-up' : ' is-down') + (p.sfp ? ' pf-pad--sfp' : '') + (p.uplink ? ' is-uplink' : '') + (p.planned ? ' is-planned' : '') + ' pf-pad--' + lv,
      'aria-label': padLabel(p), 'aria-pressed': 'false', 'aria-controls': panelId, tabindex: '-1', dataset: { port: p.id } });
    el.append(h('span', { class: 'pf-led', 'aria-hidden': 'true' }));
    const marks = h('span', { class: 'pf-marks', 'aria-hidden': 'true' });
    if (p.uplink) marks.append(ico('up', 'pf-mark pf-mark--up'));
    if (poeOn) marks.append(ico('bolt', 'pf-mark pf-mark--poe'));
    el.append(marks, h('span', { class: 'pf-n', 'aria-hidden': 'true' }, String(p.idx)));
    el.append(h('span', { class: 'pf-sp', 'aria-hidden': 'true' }, p.link ? (speedLabel(p.speed, fmt, t) || '•') : '–'));
    if (p.vlan != null && !p.sfp) el.append(h('span', { class: 'pf-vlan', 'aria-hidden': 'true' }, t('pf.vlan_short', { id: p.vlan })));
    if (p.n_devices > 1) el.append(h('span', { class: 'pf-count', 'aria-hidden': 'true' }, String(p.n_devices)));
    if (lv !== 'ok') el.append(h('span', { class: 'pf-health', 'aria-hidden': 'true' }, levelMark(lv)));
    return el;
  }

  function fact(label, value) { return value == null || value === '' ? null : [h('dt', {}, label), h('dd', {}, value)]; }

  function panel(front, p, extra) {
    const lv = level(p.level);
    const rows = [];
    rows.push(fact(t('pf.f.link'), [p.link ? t('pf.link.up') : t('pf.link.down'), p.link && p.speed ? speedFull(p.speed, fmt, t) : null,
      p.link && p.duplex ? t('pf.duplex.' + (p.duplex === 'half' ? 'half' : 'full')) : null].filter(Boolean).join(' · ')));
    const net = [p.network, p.vlan != null ? t('pf.vlan', { id: p.vlan }) : null].filter(Boolean).join(' · ');
    rows.push(fact(t('pf.f.profile'), p.profile));
    rows.push(fact(t('pf.f.network'), [net, p.tagged && !p.tagged_mode ? t('pf.tagged') : null].filter(Boolean).join(' · ')));
    if (p.tagged_mode) {
      rows.push(fact(t('pf.f.tagged'), p.tagged_mode === 'custom' ? ((p.tagged_names || []).join(', ') || t('pf.tagged_mode.none'))
        : t('pf.tagged_mode.' + (p.tagged_mode === 'all' ? 'all' : 'none'))));
    }
    if (p.poe && p.poe.capable !== false && !p.sfp) {
      rows.push(fact(t('pf.f.poe'), p.poe.on ? (p.poe.watts ? t('pf.poe.watts', { w: fmt(p.poe.watts) }) : t('pf.poe.on'))
        : t(p.poe_mode === 'off' ? 'pf.poe.switched_off' : 'pf.poe.off')));
    }
    if (p.uplink) rows.push(fact(t('pf.f.role'), t('pf.uplink_long')));
    rows.push(fact(t('pf.f.neighbor'), p.neighbor));
    if (p.devices && p.devices.length) {
      const more = Math.max(0, (p.n_devices || 0) - p.devices.length);
      rows.push(fact(t('pf.f.devices'), h('ul', { class: 'pf-devs' },
        p.devices.map(d => h('li', {}, LL.kindIcon ? LL.kindIcon(d.kind || 'unknown') : null, h('span', { class: 'pf-devs__name' }, d.name || d.ip || '—'),
          d.ip && d.name ? h('span', { class: 'muted mono' }, d.ip) : null)),
        more ? h('li', { class: 'muted pf-devs__more' }, t('pf.more', { n: more })) : null)));
    } else if (p.n_devices) rows.push(fact(t('pf.f.devices'), t('pf.devices_n', { n: p.n_devices })));
    rows.push(fact(t('pf.f.health'), h('span', { class: 'pf-hl pf-hl--' + lv }, lv !== 'ok' ? h('b', { 'aria-hidden': 'true' }, levelMark(lv)) : null,
      ' ', t(levelKey(lv)), ' — ', healthText(p, t))));
    if (p.rate && p.link) rows.push(fact(t('pf.f.rate'), t('pf.rate', { tx: LL.fmtRate(p.rate.tx || 0), rx: LL.fmtRate(p.rate.rx || 0) })));
    const more = extra ? (extra(p) || []).filter(Boolean) : [];
    return [h('h4', { class: 'pf-panel__title' }, portName(p), p.sfp ? h('span', { class: 'chip chip--muted' }, t('pf.sfp')) : null,
      p.planned ? h('span', { class: 'chip chip--accent' }, t('pf.planned')) : null),
      h('dl', { class: 'pf-facts' }, rows.filter(Boolean).flat())].concat(more);
  }

  function legend() {
    const item = (cls, label, sym) => h('li', {}, h('span', { class: 'pf-lg ' + cls, 'aria-hidden': 'true' }, sym || null), h('span', {}, label));
    const poe = ico('bolt', 'pf-lg pf-lg--poe'), upl = ico('up', 'pf-lg pf-lg--up');
    return h('ul', { class: 'pf-legend', 'aria-label': t('pf.legend') },
      h('li', {}, h('span', { class: 'pf-lg pf-lg--led is-up', 'aria-hidden': 'true' }), h('span', {}, t('pf.link.up'))),
      h('li', {}, h('span', { class: 'pf-lg pf-lg--led', 'aria-hidden': 'true' }), h('span', {}, t('pf.link.down'))),
      h('li', {}, poe, h('span', {}, t('pf.poe.short'))),
      h('li', {}, upl, h('span', {}, t('pf.uplink'))),
      item('pf-lg--warn', t('pf.level.warn'), '!'), item('pf-lg--bad', t('pf.level.bad'), '!!'));
  }

  function radios(front) {
    const rs = front.radios || [];
    if (!rs.length) return null;
    return h('ul', { class: 'pf-radios', 'aria-label': t('pf.radios') }, rs.map(r => h('li', { class: 'pf-radio' },
      h('strong', {}, t('pf.band', { n: fmt(parseFloat(r.band) || 0) })),
      r.channel != null ? h('span', { class: 'pf-radio__ch' }, t('pf.channel', { n: r.channel })) : null,
      r.clients != null ? h('span', { class: 'muted' }, t('pf.clients', { n: r.clients })) : null)));
  }

  function head(front) {
    const kind = front.kind === 'ap' ? 'ap' : front.kind === 'router' ? 'router' : 'switch';
    const lv = level(front.level);
    return h('header', { class: 'pf-head' }, LL.kindIcon ? LL.kindIcon(kind) : null,
      h('div', { class: 'pf-head__titles' }, h('strong', {}, front.name || front.id), front.model ? h('span', { class: 'muted mono' }, front.model) : null),
      h('span', { class: 'chip ' + (front.online ? 'chip--ok' : 'chip--danger') }, t(front.online ? 'fw.eq.online' : 'fw.eq.offline')),
      front.clients != null ? h('span', { class: 'chip chip--muted' }, t('pf.clients', { n: front.clients })) : null,
      lv !== 'ok' ? h('span', { class: 'chip pf-chip--' + lv }, levelMark(lv) + ' ' + t(levelKey(lv))) : null);
  }

  // LL.portFront(front, { head: false }) -> element. front is one entry of /api/ports (or detail.portfront).
  LL.portFront = function (front, opts) {
    opts = opts || {};
    front = front || {};
    const ports = front.ports || [];
    const isAp = front.kind === 'ap' || (front.radios || []).length > 0;
    const uid = 'pf-' + String(front.id || 'x').replace(/[^A-Za-z0-9_-]/g, '_');
    const panelId = uid + '-panel';
    const root = h('div', { class: 'pf' + (isAp ? ' pf--ap' : '') });
    if (opts.head !== false) root.append(head(front));
    const plateBox = h('div', { class: 'pf-plate-box' });
    const info = h('div', { class: 'pf-panel', id: panelId, role: 'region', 'aria-label': t('pf.panel'), 'aria-live': 'polite', hidden: true });
    let pads = [], tabStop = null, cols = 0, stacked = false;

    function select(pid, focus) {
      const same = selected.get(front.id) === pid;
      if (same || !pid) selected.delete(front.id); else selected.set(front.id, pid);
      refreshPanel();
      if (focus) { const el = pads.find(x => x.dataset.port === pid); if (el) el.focus({ preventScroll: true }); }
    }
    function refreshPanel() {
      const pid = selected.get(front.id), p = ports.find(x => x.id === pid);
      if (!p) { selected.delete(front.id); info.hidden = true; info.replaceChildren(); } else { info.replaceChildren(...panel(front, p, opts.extra)); info.hidden = false; }
      pads.forEach(el => { const on = !!p && el.dataset.port === p.id; el.classList.toggle('is-selected', on); el.setAttribute('aria-pressed', String(on)); el.setAttribute('aria-expanded', String(on)); });
    }
    function setStop(el) { pads.forEach(x => x.setAttribute('tabindex', x === el ? '0' : '-1')); tabStop = el; }

    // arrow keys move between the pads like on the hardware: left and right along a row, up and down between the two rows
    function onKey(e) {
      const el = e.target.closest('.pf-pad'); if (!el) return;
      const cell = el.parentElement, row = cell.parentElement;
      let to = null;
      const siblings = (r, n) => { const ch = [...r.children]; return ch[ch.indexOf(cell) + n]; };
      if (e.key === 'ArrowRight') to = siblings(row, 1);
      else if (e.key === 'ArrowLeft') to = siblings(row, -1);
      else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        const rows = [...row.parentElement.querySelectorAll(':scope > .pf-row')], i = rows.indexOf(row), r2 = rows[i + (e.key === 'ArrowDown' ? 1 : -1)];
        if (r2) to = r2.children[[...row.children].indexOf(cell)] || null;
      } else if (e.key === 'Home') to = pads[0] && pads[0].parentElement;
      else if (e.key === 'End') to = pads[pads.length - 1] && pads[pads.length - 1].parentElement;
      else if (e.key === 'Escape' && selected.has(front.id)) { select(selected.get(front.id), true); e.preventDefault(); return; }
      else return;
      e.preventDefault();
      const target = to && (to.matches('.pf-pad') ? to : to.querySelector('.pf-pad'));
      if (target) { setStop(target); target.focus(); }
    }

    function drawPlate(width) {
      cols = colsFor(width, ports.some(p => p.sfp));
      stacked = !sfpBeside(width);
      const lay = portLayout(ports, cols);
      const bands = lay.bands.map(b => h('div', { class: 'pf-band', style: '--pf-cols:' + b.cols },
        b.rows.map(r => h('div', { class: 'pf-row' }, r.map(p => h('div', { class: 'pf-cell' }, pad(front, p, panelId)))))));
      const sfp = lay.sfp.length ? h('div', { class: 'pf-sfp' }, h('span', { class: 'pf-sfp__label', 'aria-hidden': 'true' }, t('pf.sfp')),
        h('div', { class: 'pf-sfp__row' }, lay.sfp.map(p => h('div', { class: 'pf-cell' }, pad(front, p, panelId))))) : null;
      const plate = h('div', { class: 'pf-plate' + (stacked && sfp ? ' pf-plate--stack' : ''), role: 'group', 'aria-label': t('pf.plate', { name: front.name || '' }), onclick: e => {
        const b = e.target.closest('.pf-pad'); if (b) { setStop(b); select(b.dataset.port, false); } }, onkeydown: onKey }, h('div', { class: 'pf-bands' }, bands), sfp);
      pads = [...plate.querySelectorAll('.pf-pad')];
      const keep = pads.find(x => x.dataset.port === selected.get(front.id)) || (tabStop && pads.find(x => x.dataset.port === tabStop.dataset.port)) || pads[0];
      plateBox.replaceChildren(plate, legend());
      if (keep) setStop(keep);
      pads.forEach(el => el.addEventListener('focus', () => { focusMem = { front: front.id, port: el.dataset.port }; setStop(el); }));
      refreshPanel();
    }

    if (ports.length) {
      drawPlate(0);
      root.append(plateBox, info);
      if (typeof ResizeObserver !== 'undefined') {
        let last = 0, frame = 0;
        // the plate is drawn anew in the next frame, not inside the observer: redrawing there changes the size of what is
        // observed and the browser reports "ResizeObserver loop completed with undelivered notifications" as a page error
        const ro = new ResizeObserver(() => {
          if (!root.isConnected) { ro.disconnect(); return; }
          if (frame) return;
          frame = requestAnimationFrame(() => {
            frame = 0;
            if (!root.isConnected) { ro.disconnect(); return; }
            const w = Math.round(root.clientWidth);
            if (!w || w === last) return;
            last = w;
            if (colsFor(w, ports.some(p => p.sfp)) !== cols || sfpBeside(w) === stacked) drawPlate(w);
          });
        });
        ro.observe(root);
      }
      if (focusMem && focusMem.front === front.id) {            // the page drew anew while a pad had the focus: give it back
        const want = focusMem.port;
        requestAnimationFrame(() => { const el = pads.find(x => x.dataset.port === want); if (el && root.isConnected && (!document.activeElement || document.activeElement === document.body)) el.focus({ preventScroll: true }); });
      }
    }
    if (isAp) root.append(radios(front));
    if (front.roams && (front.roams.in != null || front.roams.out != null)) {
      root.append(h('p', { class: 'pf-roams muted' }, t('pf.roams', { n_in: front.roams.in || 0, n_out: front.roams.out || 0 })));
    }
    if (!ports.length && !isAp) root.append(h('p', { class: 'muted pf-none' }, t('pf.none')));
    return root;
  };

  // ------------------------------------------------------------------ data: one request per ~15 s for all fronts
  let cache = null, inflight = null;
  LL.portFrontsCached = () => (cache && Date.now() - cache.at < 15000 ? cache.fronts : null);
  LL.portFronts = function () {
    const c = LL.portFrontsCached();
    if (c) return Promise.resolve(c);
    if (inflight) return inflight;
    inflight = LL.api('/api/ports').then(d => { cache = { at: Date.now(), fronts: (d && d.fronts) || [] }; return cache.fronts; })
      .catch(() => []).then(r => { inflight = null; return r; });
    return inflight;
  };

  // ------------------------------------------------------------------ palette: "Switch ports" opens the firewall tab at the equipment card
  LL.registerCommand({ id: 'switch-ports', group: 'commands', order: 43, label: 'pf.cmd.ports', keywords: 'pf.cmd.ports_keywords',
    run: async () => {
      try {
        const v = await LL.api('/api/firewall');
        for (const s of (v.sources || []).filter(x => x.kind === 'live')) {
          const x = s.key === v.source ? v : await LL.api('/api/firewall?source=' + encodeURIComponent(s.key));
          if (x && x.state && (x.state.equipment || []).length) {
            try { localStorage.setItem('ll-fw-source', s.key); } catch (e) { /* ignore */ }
            break;
          }
        }
        sessionStorage.setItem('ll-fw-focus', 'equipment');
      } catch (e) { /* the tab opens anyway */ }
      LL.go('#/firewall');
    } });
})();
