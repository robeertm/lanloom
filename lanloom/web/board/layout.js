/* Lanloom — board layout: zones packed into a responsive grid, tiles in rows inside zones.
   Pure geometry (no DOM), usable in the window and under Node (tests). All numbers are CSS pixels. */
(function (root, factory) {
  const L = factory();
  if (typeof module === 'object' && module.exports) module.exports = L;
  else { root.Lanloom = root.Lanloom || {}; root.Lanloom.Layout = L; }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Metrics. Tile size is fixed per mode; gaps are channels for the traces (multiples of the routing grid).
  const DESKTOP = {
    margin: 24, zoneGap: 24, padX: 24, padTop: 40, padBottom: 24,
    tileW: 196, tileH: 68, gapX: 44, gapY: 36, maxGapX: 96,
    grid: 4, clearance: 2, legend: 72,
    // Router tuning of this board (host option of the core router): large tiles on a 4 px grid make the default, nearly exact
    // search far too slow, so the A* weighting is greedier; all costs stay at the router defaults (bundled traces need its
    // mostly orthogonal runs, which is also why the optional straightening pass stays off)
    router: { weight: 2.4 }
  };
  const COMPACT = {
    margin: 12, zoneGap: 18, padX: 12, padTop: 30, padBottom: 14,
    tileW: 0, tileH: 64, gapX: 16, gapY: 22, maxGapX: 16,
    grid: 3, clearance: 1.5, legend: 0, cols: 2
  };
  const COMPACT_MAX = 700;   // container widths up to this use the compact (phone) layout by default

  const KIND_ORDER = ['internet', 'router', 'switch', 'ap', 'hub', 'broker', 'nas', 'server', 'service', 'computer', 'self', 'phone', 'printer', 'tv', 'media', 'camera', 'iot', 'unknown'];

  function metrics(compact) { return Object.assign({}, compact ? COMPACT : DESKTOP); }

  // Group components into zones (unknown zone ids land in a synthetic "other" zone at the end)
  function groupZones(view) {
    const zones = (view.zones || []).map((z, i) => ({ id: String(z.id), title: z.title || '', kind: z.kind || null, span: z.span === 'full' ? 'full' : null, order: Number.isFinite(z.order) ? z.order : i, idx: i, items: [] }));
    const byId = new Map(zones.map(z => [z.id, z]));
    let other = null;
    (view.components || []).forEach(c => {
      let z = byId.get(String(c.zone));
      if (!z) {
        if (!other) { other = { id: '__other', title: null, kind: null, span: null, order: Infinity, idx: zones.length, items: [], synthetic: true }; zones.push(other); byId.set(other.id, other); }
        z = other;
      }
      z.items.push(c);
    });
    return zones.filter(z => z.items.length).sort((a, b) => (a.order - b.order) || (a.idx - b.idx));
  }

  const zoneWidth = (m, c) => 2 * m.padX + c * m.tileW + (c - 1) * m.gapX;
  const zoneHeight = (m, rows) => m.padTop + rows * m.tileH + Math.max(0, rows - 1) * m.gapY + m.padBottom;
  const colsFit = (m, w) => Math.max(1, Math.floor((w - 2 * m.padX + m.gapX) / (m.tileW + m.gapX)));

  // Pack non-full zones into rows: greedy by minimum width, then hand out remaining columns to the tallest zone.
  function packRow(m, zs, inner) {
    const cols = zs.map(z => Math.min(z.items.length, 2));
    const used = () => cols.reduce((s, c) => s + zoneWidth(m, c), 0) + (zs.length - 1) * m.zoneGap;
    for (let guard = 0; guard < 400; guard++) {
      let best = -1, bestRows = 0;
      zs.forEach((z, i) => {
        if (cols[i] >= z.items.length) return;
        const r = Math.ceil(z.items.length / cols[i]);
        if (r <= 1) return;
        if (r > bestRows || (r === bestRows && best >= 0 && z.items.length > zs[best].items.length)) { bestRows = r; best = i; }
      });
      if (best < 0) break;
      cols[best]++;
      if (used() > inner) { cols[best]--; break; }
    }
    return cols;
  }

  // Place the tiles of one zone into its box
  function placeTiles(m, z, box, cols, centre, tiles) {
    const innerW = box.w - 2 * m.padX;
    const n = z.items.length, rows = Math.ceil(n / cols);
    let gap = m.gapX, x0 = box.x + m.padX;
    if (cols > 1) {
      const spare = innerW - cols * m.tileW - (cols - 1) * m.gapX;
      gap = Math.min(m.maxGapX, m.gapX + Math.max(0, spare) / (cols - 1));
    }
    const gridW = cols * m.tileW + (cols - 1) * gap;
    if (centre || cols === 1) x0 = box.x + Math.round((box.w - gridW) / 2);
    else x0 = box.x + m.padX + Math.round((innerW - gridW) / 2);
    // vertical: rows start at the top, a shorter zone in a row keeps its tiles at the top
    z.items.forEach((c, i) => {
      const r = Math.floor(i / cols), k = i % cols;
      // last row with fewer tiles: same columns, left-aligned (keeps the grid), centred for centred zones
      let xr = x0;
      if (centre && r === rows - 1) { const left = n - r * cols; if (left < cols) xr = box.x + Math.round((box.w - (left * m.tileW + (left - 1) * gap)) / 2); }
      const x = Math.round(xr + k * (m.tileW + gap)), y = Math.round(box.y + m.padTop + r * (m.tileH + m.gapY));
      tiles[c.id] = { id: c.id, zone: z.id, x, y, w: m.tileW, h: m.tileH };
    });
  }

  /* compute(view, {width, compact}) →
     { w, h, compact, m, zones:[{id,title,x,y,w,h,count,cols,synthetic}], tiles:{id:{x,y,w,h,zone}}, order:[ids] } */
  function compute(view, opts) {
    opts = opts || {};
    const width = Math.max(240, Math.floor(opts.width || 1200));
    const compact = opts.compact == null ? width <= COMPACT_MAX : !!opts.compact;
    const m = metrics(compact);
    const inner = width - 2 * m.margin;
    if (compact) m.tileW = Math.floor((inner - 2 * m.padX - m.gapX) / 2);
    const zs = groupZones(view);
    const tiles = {}, zones = [];
    let y = m.margin;
    const addZone = (z, box, cols, centre) => {
      zones.push({ id: z.id, title: z.title, synthetic: !!z.synthetic, x: box.x, y: box.y, w: box.w, h: box.h, count: z.items.length, cols });
      placeTiles(m, z, box, cols, centre, tiles);
    };
    if (compact) {
      zs.forEach(z => {
        const rows = Math.ceil(z.items.length / m.cols);
        const box = { x: m.margin, y, w: inner, h: zoneHeight(m, rows) };
        addZone(z, box, m.cols, false);   // never centred: the rails rely on exactly two tile columns
        y += box.h + m.zoneGap;
      });
    } else {
      let row = [];
      const flush = () => {
        if (!row.length) return;
        const cols = packRow(m, row, inner);
        const widths = cols.map(c => zoneWidth(m, c));
        const used = widths.reduce((s, w) => s + w, 0) + (row.length - 1) * m.zoneGap;
        const spare = Math.max(0, inner - used), total = widths.reduce((s, w) => s + w, 0);
        const h = Math.max(...row.map((z, i) => zoneHeight(m, Math.ceil(z.items.length / cols[i]))));
        let x = m.margin;
        row.forEach((z, i) => {
          const last = i === row.length - 1;
          const w = last ? (m.margin + inner - x) : Math.round(widths[i] + spare * widths[i] / total);
          addZone(z, { x, y, w, h }, cols[i], false);
          x += w + m.zoneGap;
        });
        y += h + m.zoneGap;
        row = [];
      };
      zs.forEach(z => {
        if (z.span === 'full') {
          flush();
          const maxCols = colsFit(m, inner);
          const cols = Math.min(maxCols, z.items.length);
          const rows = Math.ceil(z.items.length / cols);
          const centre = z.items.length < maxCols;
          addZone(z, { x: m.margin, y, w: inner, h: zoneHeight(m, rows) }, cols, centre);
          y += zoneHeight(m, rows) + m.zoneGap;
          return;
        }
        const need = zoneWidth(m, Math.min(z.items.length, 2));
        const now = row.reduce((s, r) => s + zoneWidth(m, Math.min(r.items.length, 2)), 0) + row.length * m.zoneGap;
        if (row.length && now + need > inner) flush();
        row.push(z);
      });
      flush();
    }
    const h = Math.max(y - (zs.length ? m.zoneGap : 0) + m.margin + m.legend, 160);
    return { w: width, h, compact, m, zones, tiles, order: (view.components || []).map(c => c.id).filter(id => tiles[id]) };
  }

  // Router input from a layout and the view's traces (only traces whose both ends are visible)
  // opts = { Router, titleWidth: {zone: px}, groupOf(trace, end) → bundle key | null }
  function routerSpec(layout, view, opts) {
    opts = opts || {};
    const m = layout.m;
    const tiles = Object.values(layout.tiles).map(t => ({ id: t.id, x: t.x, y: t.y, w: t.w, h: t.h }));
    const soft = [], lines = [];
    layout.zones.forEach(z => {
      const tw = Math.min(z.w - 32, (opts.titleWidth && opts.titleWidth[z.id]) || 160);
      soft.push({ x: z.x + 10, y: z.y - 10, w: tw + 20, h: 22 });
      lines.push([z.x, z.y, z.x + z.w, z.y], [z.x, z.y + z.h, z.x + z.w, z.y + z.h], [z.x, z.y, z.x, z.y + z.h], [z.x + z.w, z.y, z.x + z.w, z.y + z.h]);
    });
    const seen = new Set();
    const nets = [];
    (view.traces || []).forEach(t => {
      if (!t || t.a === t.b || !layout.tiles[t.a] || !layout.tiles[t.b] || seen.has(t.id)) return;
      seen.add(t.id);
      const n = { id: String(t.id), a: t.a, b: t.b, kind: t.kind || 'lan' };
      // bundled traces: members of one group (same key at an end) may share cells and pads in the router
      if (opts.groupOf) { const ga = opts.groupOf(t, t.a), gb = opts.groupOf(t, t.b); if (ga) n.groupA = ga; if (gb) n.groupB = gb; }
      nets.push(n);
    });
    const R = opts.Router;
    const ordered = R ? R.orderNets(nets, layout.tiles) : nets;
    const spec = { w: layout.w, h: Math.ceil(layout.h), p: m.grid, kl: m.clearance, tiles, soft, lines, nets: ordered };
    if (m.router) Object.assign(spec, m.router);
    return spec;
  }

  // Structure key: changes only when ids or positions change (not on live frames or text edits)
  function structureKey(spec) {
    return JSON.stringify([spec.w, spec.h, spec.p, spec.tiles.map(t => [t.id, t.x, t.y, t.w, t.h]), spec.nets.map(n => [n.id, n.a, n.b])]);
  }

  return { compute, routerSpec, structureKey, groupZones, metrics, DESKTOP, COMPACT, COMPACT_MAX, KIND_ORDER };
});
