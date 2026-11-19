/* Lanloom — 3D view of the physical chain (core module, API = 1): the circuit board as a stack of boards, one layer per step of
   the chain — Internet on top, then the gateway, then switches and access points, then the devices — that can be turned, tilted
   and zoomed. Every component is a chip on its layer, every physical link a copper trace with a via down to the layer below, and
   the live traffic runs along the traces as dots, the same way it does on the flat board.

   It is the third face of the Network view, next to the board and the tree. It needs real graphics acceleration: a browser that
   only has a software renderer (or no WebGL at all) gets a short note and the flat board instead, so a small host never burns its
   processor on a picture (the Firefox lesson of the frame budget, B4). "Try anyway" stores ll-3d-soft=1 in this browser.

   Pure part (no DOM, tested under Node):
     layout(view, { zone, only }) → { nodes, links, layers, bounds, order }
       nodes   { id, kind, title, status, grade, metric, layer, x, y, z, parent, link }  (y is up, the Internet layer is the highest)
       links   { id (trace id or ''), a, b, kind, pts: [[x, y, z], ...], L }        a = the component above, b = the one below
       layers  { layer, y, x0, x1, z0, z1 }   one board per used layer, just big enough for its chips
       order   the ids in the order of the chain (keyboard order: PageDown / PageUp walk it)
     gpuVerdict(rendererName) → false for software renderers
     orbit(cam, aspect) → { view, proj, eye } matrices of the orbit camera; project(m, p, w, h) → screen point or null
   DOM part:
     available() → 'gpu' | 'soft' (software renderer, only after "Try anyway") | '' (no 3D here)
     mount(host, { view, t, locale, filter, onSelect, soft }) → { update(view), live(frame), highlight(id), setTheme(), focus(), stats(), destroy() }
       The scene is one focusable element (role="application") with its own keys: arrows turn and tilt, Shift+arrows move, + and -
       zoom, 0 resets the view, PageDown / PageUp walk the chain, Home / End jump to its ends, a letter jumps by name, Enter or Space
       opens the card of the element in focus. Mouse: drag turns, right drag or Shift+drag moves, the wheel zooms. Touch: one finger
       turns, two fingers zoom.
       Frame budget: nothing is drawn while nothing changes; the frame rate follows the motion setting (ll-motion: auto 30, full 60,
       saver 15, off = no moving dots) and steps down on its own (30 → 20 → 15 → 10) when a frame takes more than half of its time.
       Nothing is drawn while the tab is hidden or the scene is out of sight. With prefers-reduced-motion the traces only glow. */
(function (root, factory) {
  const T = typeof module === 'object' && module.exports ? require('./tree.js') : null;
  const S = factory(T);
  if (typeof module === 'object' && module.exports) module.exports = S;
  else {
    root.Lanloom = root.Lanloom || {};
    root.Lanloom.Scene3D = S;
  }
})(typeof self !== 'undefined' ? self : this, function (TreeArg) {
  'use strict';

  const KIND_LAYER = { internet: 0, router: 1, gateway: 1, firewall: 1, switch: 2, ap: 2 };
  const CELL = 1.25;                  // distance between two chips on a layer
  const CHIP = 0.62;                  // edge of a chip
  const LIFT = 0.012;                 // traces sit a hair above their board (no flicker against the board)
  const MARGIN = 0.9;                 // board border around its outer chips
  const GRADES = ['realtime', 'near', 'periodic', 'sluggish', 'interrupted', 'unknown'];
  const STEPS = [60, 30, 20, 15, 10];
  const MOTION = { auto: 30, full: 60, saver: 15, off: 0 };
  const LABELS_ALL = 60;              // up to this many devices every chip has its name; above, only the upper layers and the one in focus
  const tree = () => TreeArg || (typeof self !== 'undefined' && self.Lanloom && self.Lanloom.Tree) || null;

  // ---------------------------------------------------------------- pure part
  function gpuVerdict(name) {
    return !/swiftshader|llvmpipe|softpipe|software|basic render|microsoft basic/i.test(String(name || ''));
  }

  const kindLayer = k => (KIND_LAYER[k] != null ? KIND_LAYER[k] : 3);

  // footprint of a subtree on the layers below it (in cells): leaves in a square grid, branches packed in rows
  function measure(n) {
    const kids = n.children || [];
    if (!kids.length) { n._w = 1; n._d = 1; return; }
    kids.forEach(measure);
    const leaves = kids.filter(c => !(c.children || []).length), branches = kids.filter(c => (c.children || []).length);
    const cols = leaves.length ? Math.ceil(Math.sqrt(leaves.length)) : 0, rows = leaves.length ? Math.ceil(leaves.length / cols) : 0;
    // branches on shelves: a shelf is about as wide as the whole footprint is deep, so the board stays roughly square
    const area = branches.reduce((s, b) => s + (b._w + 1) * (b._d + 1), 0) + cols * rows;
    const target = Math.max(cols, Math.ceil(Math.sqrt(area) * 1.15), ...branches.map(b => b._w));
    const shelves = [];
    let cur = null;
    branches.forEach(b => {
      if (!cur || cur.w + (cur.items.length ? 1 : 0) + b._w > target) { cur = { w: 0, d: 0, items: [] }; shelves.push(cur); }
      cur.w += (cur.items.length ? 1 : 0) + b._w; cur.d = Math.max(cur.d, b._d); cur.items.push(b);
    });
    const bw = shelves.reduce((m, s) => Math.max(m, s.w), 0), bd = shelves.reduce((s, x) => s + x.d, 0) + Math.max(0, shelves.length - 1);
    n._shelves = shelves; n._leaves = leaves; n._cols = cols; n._rows = rows; n._bd = bd;
    n._w = Math.max(1, bw, cols);
    n._d = Math.max(1, bd + (leaves.length && shelves.length ? 1 : 0) + rows);
  }
  function place(n, x0, z0, out) {
    out.set(n.id, [x0 + n._w / 2, z0 + n._d / 2]);
    if (!(n.children || []).length) return;
    let z = z0;
    n._shelves.forEach(s => {
      let x = x0 + (n._w - s.w) / 2;
      s.items.forEach(b => { place(b, x, z + (s.d - b._d) / 2, out); x += b._w + 1; });
      z += s.d + 1;
    });
    if (n._leaves.length) {
      const lz = z0 + (n._shelves.length ? n._bd + 1 : 0), lx = x0 + (n._w - n._cols) / 2;
      n._leaves.forEach((c, i) => out.set(c.id, [lx + (i % n._cols) + 0.5, lz + Math.floor(i / n._cols) + 0.5]));
    }
  }

  function layout(view, opts) {
    const T = tree();
    const b = T ? T.build(view, opts || {}) : { roots: [], other: null, items: [] };
    const comps = new Map(((view && view.components) || []).map(c => [c.id, c]));
    const nodes = [], byId = new Map();
    // the layer of a component: its kind's layer, never above the one it hangs on (a switch behind a device stays below it)
    const walk = (n, parentLayer, parent) => {
      const layer = n.group ? 3 : Math.max(parentLayer, kindLayer(n.kind));
      if (!n.group) {
        const c = comps.get(n.id) || {};
        const node = { id: n.id, kind: n.kind, title: n.title || '', subtitle: n.subtitle || '', status: n.status, grade: GRADES.includes(n.grade) ? n.grade : 'unknown',
          metric: c.metric || '', layer, x: 0, y: 0, z: 0, parent: parent || null, link: n.link || null, count: n.count || 0 };
        nodes.push(node); byId.set(n.id, node);
      }
      (n.children || []).forEach(ch => walk(ch, layer, n.group ? null : n.id));
    };
    const tops = b.roots.concat(b.other ? [b.other] : []);
    tops.forEach(n => walk(n, 0, null));
    // one pseudo root holds everything, so the whole stack is laid out as one footprint (the loose devices sit at its back edge)
    const top = { id: '__top', children: tops };
    measure(top);
    const pos = new Map();
    place(top, 0, 0, pos);
    const used = [...new Set(nodes.map(n => n.layer))].sort((a, c) => a - c);
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    nodes.forEach(n => { const p = pos.get(n.id) || [0, 0]; n.x = p[0] * CELL; n.z = p[1] * CELL; x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x); z0 = Math.min(z0, n.z); z1 = Math.max(z1, n.z); });
    if (!nodes.length) { x0 = x1 = z0 = z1 = 0; }
    const cx = (x0 + x1) / 2, cz = (z0 + z1) / 2, span = Math.max(x1 - x0, z1 - z0, 2);
    const gap = Math.max(2.4, Math.min(7, span * 0.26));         // height between two boards grows with the size of the network
    const yOf = new Map(used.map((l, i) => [l, (used.length - 1 - i) * gap]));
    nodes.forEach(n => { n.x -= cx; n.z -= cz; n.y = yOf.get(n.layer); });
    const layers = used.map(l => {
      const on = nodes.filter(n => n.layer === l);
      return { layer: l, y: yOf.get(l), x0: Math.min(...on.map(n => n.x)) - MARGIN, x1: Math.max(...on.map(n => n.x)) + MARGIN,
        z0: Math.min(...on.map(n => n.z)) - MARGIN, z1: Math.max(...on.map(n => n.z)) + MARGIN };
    });
    // a trace runs from the chip above straight down (the via) to the board of the chip below, then along x and z on that board
    const links = [];
    nodes.forEach(n => {
      if (!n.parent) return;
      const p = byId.get(n.parent); if (!p) return;
      const y = n.y + LIFT;
      const pts = [[p.x, p.y, p.z]];
      if (p.y !== n.y) pts.push([p.x, y, p.z]);
      if (p.x !== n.x) pts.push([n.x, y, p.z]);
      pts.push([n.x, y, n.z]);
      const kind = n.link ? n.link.kind : 'cable';
      const seg = [0];
      for (let i = 1; i < pts.length; i++) seg.push(seg[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1], pts[i][2] - pts[i - 1][2]));
      links.push({ id: n.link && n.link.id != null ? String(n.link.id) : '', a: p.id, b: n.id, kind, pts, seg, L: seg[seg.length - 1] });
    });
    const order = b.items.filter(n => !n.group).map(n => n.id).filter(id => byId.has(id));
    const top0 = layers.length ? layers[0].y : 0;
    return { nodes, links, layers, order, bounds: { span: Math.max(span + 2 * MARGIN, 4), height: top0, x0: x0 - cx, x1: x1 - cx, z0: z0 - cz, z1: z1 - cz } };
  }

  // point at fraction u (0..1) of a link's polyline
  function along(link, u) {
    const d = Math.max(0, Math.min(1, u)) * link.L, s = link.seg, p = link.pts;
    let i = 1;
    while (i < s.length - 1 && s[i] < d) i++;
    const len = s[i] - s[i - 1] || 1, f = (d - s[i - 1]) / len;
    return [p[i - 1][0] + (p[i][0] - p[i - 1][0]) * f, p[i - 1][1] + (p[i][1] - p[i - 1][1]) * f, p[i - 1][2] + (p[i][2] - p[i - 1][2]) * f];
  }

  // ---- small matrix helpers (column-major, like WebGL expects)
  function perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
    return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
  }
  function lookAt(e, c, up) {
    let zx = e[0] - c[0], zy = e[1] - c[1], zz = e[2] - c[2];
    let l = Math.hypot(zx, zy, zz) || 1; zx /= l; zy /= l; zz /= l;
    let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
    l = Math.hypot(xx, xy, xz) || 1; xx /= l; xy /= l; xz /= l;
    const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
    return new Float32Array([xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
      -(xx * e[0] + xy * e[1] + xz * e[2]), -(yx * e[0] + yy * e[1] + yz * e[2]), -(zx * e[0] + zy * e[1] + zz * e[2]), 1]);
  }
  function mul(a, b) {
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      o[c * 4 + r] = s;
    }
    return o;
  }
  function orbit(cam, aspect) {
    const cp = Math.cos(cam.pitch), eye = [cam.tx + cam.dist * cp * Math.sin(cam.yaw), cam.ty + cam.dist * Math.sin(cam.pitch), cam.tz + cam.dist * cp * Math.cos(cam.yaw)];
    const view = lookAt(eye, [cam.tx, cam.ty, cam.tz], [0, 1, 0]);
    const proj = perspective(0.75, Math.max(0.2, aspect), Math.max(0.05, cam.dist / 200), cam.dist * 8 + 200);
    return { view, proj, vp: mul(proj, view), eye };
  }
  function project(m, p, w, h) {
    const x = m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12], y = m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13];
    const ww = m[3] * p[0] + m[7] * p[1] + m[11] * p[2] + m[15];
    if (ww <= 0.0001) return null;
    return { x: (x / ww * 0.5 + 0.5) * w, y: (1 - (y / ww * 0.5 + 0.5)) * h, w: ww };
  }
  function fitCamera(bounds) {
    const dist = Math.max(8, bounds.span * 1.15 + bounds.height * 0.9);
    return { yaw: 0.62, pitch: 0.52, dist, tx: 0, ty: bounds.height * 0.42, tz: 0, home: dist };
  }

  // "#0C8574", "#abc", "rgba(38, 72, 56, .14)" → [r, g, b, a] in 0..1
  function parseColor(s, dflt) {
    s = String(s || '').trim();
    let m = s.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
    if (m) {
      let h = m[1]; if (h.length === 3) h = h.split('').map(c => c + c).join('');
      return [parseInt(h.slice(0, 2), 16) / 255, parseInt(h.slice(2, 4), 16) / 255, parseInt(h.slice(4, 6), 16) / 255, 1];
    }
    m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+%?))?\s*\)$/i);
    if (m) {
      let a = m[4] == null ? 1 : (m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
      return [+m[1] / 255, +m[2] / 255, +m[3] / 255, a];
    }
    return dflt || [0.5, 0.5, 0.5, 1];
  }

  // dots: q-sized quanta of one second on one link and direction → how many to start (capped), the rest is carried
  function dotCount(quanta, carry, cap) {
    const total = Math.max(0, quanta || 0) + Math.max(0, carry || 0);
    const n = Math.min(cap, Math.floor(total));
    return { n, carry: Math.min(total - n, Math.max(8, (quanta || 0) * 4)), big: total > cap * 4 };
  }

  // ---------------------------------------------------------------- DOM part
  let probeCache = null;
  function probe() {
    if (probeCache != null) return probeCache;
    probeCache = '';
    try {
      const c = document.createElement('canvas');
      let gl = c.getContext('webgl', { failIfMajorPerformanceCaveat: true });
      let hard = !!gl;
      if (!gl) gl = c.getContext('webgl');
      if (!gl) return probeCache;
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      const name = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
      hard = hard && gpuVerdict(name);
      probeCache = hard ? 'gpu' : 'soft-only';
      const lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();
    } catch (e) { probeCache = ''; }
    return probeCache;
  }
  const softAllowed = () => { try { return localStorage.getItem('ll-3d-soft') === '1'; } catch (e) { return false; } };
  // 'gpu' = real acceleration · 'soft' = only a software renderer, and the person chose to try anyway · '' = flat board
  function available() {
    const p = probe();
    if (p === 'gpu') return 'gpu';
    if (p === 'soft-only' && softAllowed()) return 'soft';
    return '';
  }
  // is there WebGL at all (then "Try anyway" makes sense)?
  const canTry = () => probe() === 'soft-only';
  function allowSoft(on) { try { if (on) localStorage.setItem('ll-3d-soft', '1'); else localStorage.removeItem('ll-3d-soft'); } catch (e) { /* private mode */ } }

  const VS_SOLID = 'attribute vec3 p;attribute vec3 n;attribute vec4 c;uniform mat4 vp;varying vec4 vc;' +
    'void main(){vec3 L=normalize(vec3(.35,.9,.45));float d=.62+.38*max(dot(normalize(n),L),0.);vc=vec4(c.rgb*d,c.a);gl_Position=vp*vec4(p,1.);}';
  const FS_SOLID = 'precision mediump float;varying vec4 vc;void main(){gl_FragColor=vc;}';
  const VS_LINE = 'attribute vec3 p;attribute vec4 c;uniform mat4 vp;varying vec4 vc;void main(){vc=c;gl_Position=vp*vec4(p,1.);}';
  const FS_LINE = FS_SOLID;
  const VS_DOT = 'attribute vec3 p;attribute vec4 c;attribute float s;uniform mat4 vp;uniform float k;varying vec4 vc;' +
    'void main(){vc=c;gl_Position=vp*vec4(p,1.);gl_PointSize=clamp(s*k/gl_Position.w,2.,14.);}';
  const FS_DOT = 'precision mediump float;varying vec4 vc;void main(){vec2 q=gl_PointCoord*2.-1.;float r=dot(q,q);if(r>1.)discard;' +
    'gl_FragColor=vec4(mix(vec3(1.),vc.rgb,smoothstep(0.,.55,r)),vc.a*(1.-smoothstep(.7,1.,r)));}';

  function mount(host, opts) {
    const doc = host.ownerDocument || document;
    const t = opts.t || (k => k);
    const st = { view: opts.view, filter: opts.filter || (() => ({})), onSelect: opts.onSelect || (() => {}), locale: opts.locale, dead: false,
      L: null, cam: null, aim: null, selected: null, focus: null, hover: null, metrics: {}, grades: {}, level: new Map(),
      dots: [], carry: new Map(), raf: 0, last: 0, need: true, inView: true, typed: '', typedAt: 0,
      motion: 'auto', fpsCap: 30, fpsNow: 30, stats: { fps: 0, drawMs: 0, frames: 0, cap: 30, steps: 0, dots: 0, mode: '' }, fpsT: 0, fpsN: 0 };
    const el = (tag, cls, text) => { const e = doc.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

    const rootEl = el('div', 's3d');
    const stage = el('div', 's3d-stage');
    stage.tabIndex = 0;
    stage.setAttribute('role', 'application');
    stage.setAttribute('aria-roledescription', t('s3d.role'));
    stage.setAttribute('aria-label', t('s3d.label'));
    const canvas = el('canvas', 's3d-canvas');
    canvas.setAttribute('aria-hidden', 'true');
    const labels = el('div', 's3d-labels'); labels.setAttribute('aria-hidden', 'true');
    const status = el('div', 'sr-only'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const hint = el('p', 's3d-hint muted', t('s3d.hint'));
    hint.id = 's3d-hint-' + Math.random().toString(36).slice(2, 8);
    stage.setAttribute('aria-describedby', hint.id);
    const focusCard = el('div', 's3d-focus'); focusCard.hidden = true; focusCard.setAttribute('aria-hidden', 'true');
    const tools = el('div', 's3d-tools');
    const toolBtn = (key, sym, fn) => {
      const b = el('button', 's3d-tool', sym); b.type = 'button';
      b.setAttribute('aria-label', t(key)); b.title = t(key);
      b.addEventListener('click', () => { fn(); stage.focus({ preventScroll: true }); });
      return b;
    };
    tools.append(toolBtn('s3d.zoom_in', '+', () => zoom(1 / 1.25)), toolBtn('s3d.zoom_out', '−', () => zoom(1.25)),
      toolBtn('s3d.turn_left', '⟲', () => turn(-0.35, 0)), toolBtn('s3d.turn_right', '⟳', () => turn(0.35, 0)), toolBtn('s3d.reset', '⌂', () => reset()));
    const layerKey = el('ol', 's3d-layers'); layerKey.setAttribute('aria-label', t('s3d.layers'));
    const softNote = el('p', 's3d-soft muted'); softNote.hidden = true;
    stage.append(canvas, labels, focusCard);
    rootEl.append(stage, tools, layerKey, softNote, hint, status);
    host.appendChild(rootEl);

    // ---- WebGL
    const gl = canvas.getContext('webgl', { antialias: true, alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: false });
    if (!gl) { rootEl.remove(); return null; }
    st.stats.mode = available() || 'soft';
    if (st.stats.mode === 'soft') { softNote.hidden = false; softNote.textContent = t('s3d.soft_note'); }
    const prog = (vs, fs) => {
      const p = gl.createProgram();
      [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]].forEach(([k, src]) => { const s = gl.createShader(k); gl.shaderSource(s, src); gl.compileShader(s); gl.attachShader(p, s); });
      gl.linkProgram(p);
      const loc = {};
      ['p', 'n', 'c', 's'].forEach(a => { loc[a] = gl.getAttribLocation(p, a); });
      loc.vp = gl.getUniformLocation(p, 'vp'); loc.k = gl.getUniformLocation(p, 'k');
      return { p, loc };
    };
    const P = { solid: prog(VS_SOLID, FS_SOLID), line: prog(VS_LINE, FS_LINE), dot: prog(VS_DOT, FS_DOT) };
    const buf = () => gl.createBuffer();
    const B = { chips: buf(), boards: buf(), grid: buf(), traces: buf(), dots: buf() };
    const N = { chips: 0, boards: 0, grid: 0, traces: 0, dots: 0 };
    let colors = {};
    let traceData = null;                // Float32Array of the traces (positions + colours), colours rewritten when the glow changes

    function readTheme() {
      const cs = root0 => getComputedStyle(root0);
      const s = cs(doc.documentElement);
      const v = (name, d) => parseColor(s.getPropertyValue(name), d);
      colors = {
        board: v('--ll-pcb-board', [0.93, 0.95, 0.93, 1]), grid: v('--ll-pcb-grid', [0.15, 0.28, 0.22, 0.14]), tile: v('--ll-pcb-tile', [1, 1, 1, 1]),
        line: v('--ll-pcb-tile-line', [0.8, 0.84, 0.82, 1]), copper: v('--ll-pcb-copper', [0.69, 0.5, 0.25, 1]), accent: v('--ll-accent', [0.05, 0.48, 0.42, 1]),
        danger: v('--ll-danger', [0.84, 0.27, 0.27, 1]), silk: v('--ll-pcb-silk', [0.2, 0.27, 0.24, 1]), dot: v('--ll-pcb-dot-core', [1, 1, 1, 1]),
        zone: v('--ll-pcb-zone', [0.97, 0.98, 0.97, 1]), edge: v('--ll-pcb-zone-line', [0.75, 0.8, 0.77, 1])
      };
      GRADES.forEach(g => { colors['g_' + g] = v('--ll-grade-' + g, [0.5, 0.55, 0.58, 1]); });
      ['uplink', 'cable', 'radio', 'virtual', 'lan'].forEach(k => { colors['t_' + k] = v('--ll-pcb-trace-' + k, colors.copper); });
      const dark = (colors.board[0] + colors.board[1] + colors.board[2]) / 3 < 0.4;
      colors.dark = dark;
      gl.clearColor(0, 0, 0, 0);
    }

    // a box (6 faces, 36 vertices) into an array of [x, y, z, nx, ny, nz, r, g, b, a]
    function box(out, cx, cy, cz, sx, sy, sz, col, top) {
      const x0 = cx - sx / 2, x1 = cx + sx / 2, y0 = cy, y1 = cy + sy, z0 = cz - sz / 2, z1 = cz + sz / 2;
      const face = (a, b, c, d, n, k) => { for (const q of [a, b, c, a, c, d]) out.push(q[0], q[1], q[2], n[0], n[1], n[2], k[0], k[1], k[2], k[3]); };
      const tc = top || col;
      face([x0, y1, z0], [x0, y1, z1], [x1, y1, z1], [x1, y1, z0], [0, 1, 0], tc);
      face([x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1], [0, -1, 0], col);
      face([x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1], [0, 0, 1], col);
      face([x1, y0, z0], [x0, y0, z0], [x0, y1, z0], [x1, y1, z0], [0, 0, -1], col);
      face([x1, y0, z1], [x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [1, 0, 0], col);
      face([x0, y0, z0], [x0, y0, z1], [x0, y1, z1], [x0, y1, z0], [-1, 0, 0], col);
    }
    const mix = (a, b, f) => [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + ((b[3] == null ? 1 : b[3]) - a[3]) * f];
    const gradeOf = n => (n.status === 'down' ? 'interrupted' : (st.grades[n.id] || n.grade || 'unknown'));

    function buildBoards() {
      const L = st.L, out = [], grid = [];
      const board = colors.dark ? [colors.board[0] * 1.6 + 0.02, colors.board[1] * 1.6 + 0.04, colors.board[2] * 1.6 + 0.03, 0.82] : mix(colors.board, [0.86, 0.9, 0.87, 1], 0.35);
      board[3] = colors.dark ? 0.84 : 0.8;
      const g = colors.grid.slice(); g[3] = Math.min(0.5, g[3] * (colors.dark ? 3.2 : 1.8));
      const edge = colors.edge.slice(); edge[3] = 0.95;
      L.layers.forEach(l => {
        const w = l.x1 - l.x0, d = l.z1 - l.z0, y = l.y - 0.09;
        box(out, (l.x0 + l.x1) / 2, y, (l.z0 + l.z1) / 2, w, 0.08, d, board);
        // grid lines on the board (every two cells) and its outline
        const step = CELL * 2;
        for (let x = Math.ceil(l.x0 / step) * step; x <= l.x1; x += step) grid.push(x, l.y + 0.002, l.z0, ...g, x, l.y + 0.002, l.z1, ...g);
        for (let z = Math.ceil(l.z0 / step) * step; z <= l.z1; z += step) grid.push(l.x0, l.y + 0.002, z, ...g, l.x1, l.y + 0.002, z, ...g);
        const c = [[l.x0, l.z0], [l.x1, l.z0], [l.x1, l.z1], [l.x0, l.z1]];
        for (let i = 0; i < 4; i++) { const a = c[i], b2 = c[(i + 1) % 4]; grid.push(a[0], l.y + 0.004, a[1], ...edge, b2[0], l.y + 0.004, b2[1], ...edge); }
      });
      gl.bindBuffer(gl.ARRAY_BUFFER, B.boards); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(out), gl.STATIC_DRAW); N.boards = out.length / 10;
      gl.bindBuffer(gl.ARRAY_BUFFER, B.grid); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(grid), gl.STATIC_DRAW); N.grid = grid.length / 7;
    }
    const chipHeight = n => (n.layer === 0 ? 0.5 : n.layer === 1 ? 0.42 : n.layer === 2 ? 0.3 : 0.2);
    function buildChips() {
      const out = [], chain = chainSet();
      st.L.nodes.forEach(n => {
        const g = gradeOf(n), led = colors['g_' + g] || colors.g_unknown;
        let body = colors.dark ? mix(colors.tile, [1, 1, 1, 1], 0.12) : colors.tile;
        if (n.status === 'down') body = mix(body, colors.danger, 0.3);
        const on = n.id === st.selected || n.id === st.focus || n.id === st.hover;
        const s = CHIP * (n.layer <= 1 ? 1.25 : n.layer === 2 ? 1.1 : 1), h = chipHeight(n);
        const dim = chain && !chain.has(n.id);
        const bodyC = dim ? mix(body, colors.board, 0.55) : body;
        box(out, n.x, n.y, n.z, s, h, s, on ? mix(bodyC, colors.accent, 0.35) : bodyC, on ? colors.accent : mix(bodyC, colors.silk, 0.08));
        // the status light on top (like the light on a tile of the flat board)
        box(out, n.x + s * 0.27, n.y + h, n.z - s * 0.27, s * 0.22, 0.03, s * 0.22, dim ? mix(led, colors.board, 0.5) : led);
      });
      gl.bindBuffer(gl.ARRAY_BUFFER, B.chips); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(out), gl.DYNAMIC_DRAW); N.chips = out.length / 10;
    }
    // the chain of the element in focus (its way up to the Internet and its direct children) is lit, the rest steps back
    function chainSet() {
      const id = st.focus || st.hover || st.selected;
      if (!id || !st.L) return null;
      const by = st.nodeById, n = by.get(id); if (!n) return null;
      const s = new Set([id]);
      for (let p = n.parent; p; p = by.get(p) ? by.get(p).parent : null) s.add(p);
      st.L.nodes.forEach(c => { if (c.parent === id) s.add(c.id); });
      return s;
    }
    function buildTraces() {
      const out = [], chain = chainSet();
      st.L.links.forEach(lk => {
        const base = colors['t_' + lk.kind] || colors.t_cable;
        const lit = st.level.get(lk.id || lk.b) || 0;
        const on = !chain || (chain.has(lk.a) && chain.has(lk.b));
        let a = on ? 0.62 + 0.38 * lit : 0.16;
        const c = lk.kind === 'radio' || lk.kind === 'virtual' ? mix(base, colors.board, 0.15) : base;
        for (let i = 1; i < lk.pts.length; i++) {
          const p = lk.pts[i - 1], q = lk.pts[i];
          if (lk.kind === 'radio' || lk.kind === 'virtual') {
            // wireless and virtual links are dashed, like on the flat board
            const len = Math.hypot(q[0] - p[0], q[1] - p[1], q[2] - p[2]), dash = lk.kind === 'radio' ? 0.22 : 0.08, gapL = lk.kind === 'radio' ? 0.16 : 0.18;
            for (let u = 0; u < len; u += dash + gapL) {
              const f0 = u / len, f1 = Math.min(1, (u + dash) / len);
              out.push(p[0] + (q[0] - p[0]) * f0, p[1] + (q[1] - p[1]) * f0, p[2] + (q[2] - p[2]) * f0, c[0], c[1], c[2], a,
                p[0] + (q[0] - p[0]) * f1, p[1] + (q[1] - p[1]) * f1, p[2] + (q[2] - p[2]) * f1, c[0], c[1], c[2], a);
            }
          } else out.push(p[0], p[1], p[2], c[0], c[1], c[2], a, q[0], q[1], q[2], c[0], c[1], c[2], a);
        }
      });
      traceData = new Float32Array(out);
      gl.bindBuffer(gl.ARRAY_BUFFER, B.traces); gl.bufferData(gl.ARRAY_BUFFER, traceData, gl.DYNAMIC_DRAW); N.traces = out.length / 7;
    }

    // ---- labels (DOM: crisp text; only placed again when the camera moves)
    const labelEls = new Map();
    function buildLabels() {
      labels.replaceChildren(); labelEls.clear();
      const many = st.L.nodes.length > LABELS_ALL;
      st.L.nodes.forEach(n => {
        const e = el('span', 's3d-label s3d-label--l' + n.layer, n.title || n.id);
        e.dataset.id = n.id; e.dataset.always = (!many || n.layer <= 2) ? '1' : '';
        labels.appendChild(e); labelEls.set(n.id, e);
      });
    }
    function placeLabels(m, w, h) {
      const boxes = [];
      const want = [...st.L.nodes].sort((a, b) => a.layer - b.layer);
      const pri = new Set([st.focus, st.hover, st.selected].filter(Boolean));
      // the ones in focus first, then layer by layer; a label that would cover one already placed stays hidden
      want.sort((a, b) => (pri.has(b.id) ? 1 : 0) - (pri.has(a.id) ? 1 : 0));
      want.forEach(n => {
        const e = labelEls.get(n.id); if (!e) return;
        const p = project(m, [n.x, n.y + chipHeight(n) + 0.12, n.z], w, h);
        const show = p && (pri.has(n.id) || e.dataset.always === '1') && p.x > -40 && p.x < w + 40 && p.y > -10 && p.y < h + 10;
        if (!show) { if (!e.hidden) e.hidden = true; return; }
        const lw = Math.min(160, 7 * (e.textContent.length) + 10), lh = 18;
        const r = [p.x - lw / 2, p.y - lh - 2, p.x + lw / 2, p.y - 2];
        const hit = !pri.has(n.id) && boxes.some(b => r[0] < b[2] && r[2] > b[0] && r[1] < b[3] && r[3] > b[1]);
        if (hit) { if (!e.hidden) e.hidden = true; return; }
        boxes.push(r);
        e.hidden = false;
        e.classList.toggle('is-focus', pri.has(n.id));
        e.style.transform = 'translate(' + Math.round(p.x) + 'px,' + Math.round(p.y) + 'px) translate(-50%,-100%)';
      });
    }

    // ---- the element in focus: a small card in the corner and an announcement
    function describe(n) {
      const g = gradeOf(n);
      const kind = t('board.kind.' + n.kind) !== 'board.kind.' + n.kind ? t('board.kind.' + n.kind) : '';
      const m = st.metrics[n.id] != null ? st.metrics[n.id] : n.metric;
      return [n.title || n.id, kind, t('s3d.layer.' + n.layer), m ? localiseMetric(String(m)) : '', (n.status === 'down' ? t('board.status.down') + ' · ' : '') + t('board.grade.' + g)].filter(Boolean).join(', ');
    }
    function localiseMetric(s) {
      if (!/\d\.\d/.test(s)) return s;
      let dec = '.';
      try { const p = new Intl.NumberFormat(st.locale || undefined).formatToParts(1.1).find(x => x.type === 'decimal'); if (p) dec = p.value; } catch (e) { /* keep */ }
      return dec === '.' ? s : s.replace(/(\d)\.(\d)/g, '$1' + dec + '$2');
    }
    function showFocusCard() {
      const n = st.focus && st.nodeById.get(st.focus);
      if (!n) { focusCard.hidden = true; return; }
      focusCard.hidden = false;
      focusCard.replaceChildren(el('strong', '', n.title || n.id), el('span', 'muted', describe(n).split(', ').slice(1).join(' · ')));
    }

    // ---- camera
    function setLayout(L, keepCam) {
      st.L = L;
      st.nodeById = new Map(L.nodes.map(n => [n.id, n]));
      st.linkIdx = new Map();
      L.links.forEach((lk, i) => { if (lk.id) st.linkIdx.set(lk.id, i); });
      if (!keepCam || !st.cam) { st.cam = fitCamera(L.bounds); st.aim = null; }
      st.dots = st.dots.filter(d => d.li < L.links.length && L.links[d.li] && L.links[d.li].id === d.id);
      buildBoards(); buildChips(); buildTraces(); buildLabels(); renderLayerKey();
      if (st.focus && !st.nodeById.has(st.focus)) st.focus = null;
      showFocusCard();
      want();
    }
    function renderLayerKey() {
      layerKey.replaceChildren(...st.L.layers.map(l => {
        const li = el('li', 's3d-layers__item');
        li.append(el('span', 's3d-layers__n', String(st.L.layers.indexOf(l) + 1)), el('span', '', t('s3d.layer.' + l.layer)));
        return li;
      }));
    }
    const clampCam = () => {
      const c = st.cam;
      c.pitch = Math.max(0.12, Math.min(1.5, c.pitch));
      c.dist = Math.max(2.5, Math.min(c.home * 3, c.dist));
      const lim = st.L.bounds.span;
      c.tx = Math.max(-lim, Math.min(lim, c.tx)); c.tz = Math.max(-lim, Math.min(lim, c.tz));
      c.ty = Math.max(-2, Math.min(st.L.bounds.height + 3, c.ty));
    };
    function turn(dy, dp) { st.cam.yaw += dy; st.cam.pitch += dp; clampCam(); st.aim = null; want(); }
    function zoom(f) { st.cam.dist *= f; clampCam(); st.aim = null; want(); }
    function pan(dx, dz) {
      const c = st.cam, s = c.dist * 0.0016;
      c.tx += (Math.cos(c.yaw) * dx - Math.sin(c.yaw) * dz) * s; c.tz += (-Math.sin(c.yaw) * dx - Math.cos(c.yaw) * dz) * s;
      clampCam(); st.aim = null; want();
    }
    function reset() { const h = fitCamera(st.L.bounds); st.aim = { yaw: h.yaw, pitch: h.pitch, dist: h.dist, tx: h.tx, ty: h.ty, tz: h.tz }; want(); }
    function aimAt(id) {
      const n = st.nodeById.get(id); if (!n) return;
      const c = st.cam;
      st.aim = { yaw: c.yaw, pitch: c.pitch, dist: Math.min(c.dist, Math.max(6, c.home * 0.55)), tx: n.x, ty: n.y, tz: n.z };
      want();
    }
    const reduced = () => !!(root0win().matchMedia && root0win().matchMedia('(prefers-reduced-motion: reduce)').matches);
    function root0win() { return doc.defaultView || self; }

    // ---- frame loop
    function readMotion() {
      let m = 'auto';
      try { const v = localStorage.getItem('ll-motion'); if (MOTION[v] != null) m = v; } catch (e) { /* default */ }
      st.motion = m;
      st.fpsCap = m === 'auto' && st.stats.mode === 'soft' ? 15 : MOTION[m];
      if (st.fpsNow > st.fpsCap || !st.fpsNow) st.fpsNow = st.fpsCap || 15;
    }
    function want() { st.need = true; wake(); }
    function wake() {
      if (st.raf || st.dead || doc.hidden || !st.inView) return;
      st.raf = root0win().requestAnimationFrame(loop);
    }
    function size() {
      const dpr = Math.min(2, root0win().devicePixelRatio || 1);
      const w = Math.max(1, stage.clientWidth), h = Math.max(1, stage.clientHeight);
      // software rendering draws every pixel on the processor: half the pixels there
      const k = st.stats.mode === 'soft' ? Math.min(1, dpr) * 0.75 : dpr;
      const cw = Math.round(w * k), ch = Math.round(h * k);
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      return { w, h, k };
    }
    function loop(now) {
      st.raf = 0;
      if (st.dead) return;
      const gapMs = st.fpsNow ? 1000 / st.fpsNow - 2 : 1000 / 15;
      if (now - st.last < gapMs) { st.raf = root0win().requestAnimationFrame(loop); return; }
      st.last = now;
      let moving = false;
      if (st.aim) {
        const c = st.cam, a = st.aim; let done = true;
        ['yaw', 'pitch', 'dist', 'tx', 'ty', 'tz'].forEach(k => { const d = a[k] - c[k]; if (Math.abs(d) > 0.002) { c[k] += d * 0.22; done = false; } else c[k] = a[k]; });
        if (done) st.aim = null; else moving = true;
        st.need = true;
      }
      st.dots = st.dots.filter(d => now < d.t0 + d.du);
      const dotsOn = st.dots.length > 0;
      if (st.need || dotsOn) {
        const t0 = performance.now();
        draw(now, !!st.need);
        const ms = performance.now() - t0;
        st.stats.drawMs = Math.round(ms * 10) / 10; st.stats.frames++;
        budget(ms);
      }
      st.need = false;
      st.fpsN++;
      if (now - st.fpsT >= 1000) { st.stats.fps = Math.round(st.fpsN * 1000 / (now - st.fpsT)); st.fpsT = now; st.fpsN = 0; }
      if (moving || dotsOn) st.raf = root0win().requestAnimationFrame(loop);
      else st.stats.fps = 0;
    }
    let slowRun = 0, fastRun = 0;
    function budget(ms) {
      if (!st.fpsCap) return;
      const frame = 1000 / Math.max(1, st.fpsNow), i = STEPS.indexOf(st.fpsNow);
      if (ms > frame * 0.5) { fastRun = 0; if (++slowRun >= 6) { slowRun = 0; if (i >= 0 && i < STEPS.length - 1) { st.fpsNow = STEPS[i + 1]; st.stats.steps++; } } }
      else if (ms < frame * 0.2) { slowRun = 0; if (++fastRun >= 90) { fastRun = 0; if (i > 0 && STEPS[i - 1] <= st.fpsCap) st.fpsNow = STEPS[i - 1]; } }
      st.stats.cap = st.fpsNow;
    }
    let lastM = null;
    function draw(now, camChanged) {
      const sz = size();
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      const m = orbit(st.cam, sz.w / sz.h);
      lastM = { vp: m.vp, w: sz.w, h: sz.h };
      gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL);
      gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      const bindSolid = (b) => {
        const p = P.solid; gl.useProgram(p.p); gl.uniformMatrix4fv(p.loc.vp, false, m.vp);
        gl.bindBuffer(gl.ARRAY_BUFFER, b);
        gl.enableVertexAttribArray(p.loc.p); gl.vertexAttribPointer(p.loc.p, 3, gl.FLOAT, false, 40, 0);
        gl.enableVertexAttribArray(p.loc.n); gl.vertexAttribPointer(p.loc.n, 3, gl.FLOAT, false, 40, 12);
        gl.enableVertexAttribArray(p.loc.c); gl.vertexAttribPointer(p.loc.c, 4, gl.FLOAT, false, 40, 24);
      };
      const bindLine = (b) => {
        const p = P.line; gl.useProgram(p.p); gl.uniformMatrix4fv(p.loc.vp, false, m.vp);
        gl.bindBuffer(gl.ARRAY_BUFFER, b);
        gl.enableVertexAttribArray(p.loc.p); gl.vertexAttribPointer(p.loc.p, 3, gl.FLOAT, false, 28, 0);
        gl.enableVertexAttribArray(p.loc.c); gl.vertexAttribPointer(p.loc.c, 4, gl.FLOAT, false, 28, 12);
      };
      // chips (opaque) first, then the traces, then the see-through boards without writing depth, then the dots on top
      if (N.chips) { bindSolid(B.chips); gl.drawArrays(gl.TRIANGLES, 0, N.chips); }
      if (N.traces) { bindLine(B.traces); gl.drawArrays(gl.LINES, 0, N.traces); }
      gl.depthMask(false);
      if (N.grid) { bindLine(B.grid); gl.drawArrays(gl.LINES, 0, N.grid); }
      if (N.boards) { bindSolid(B.boards); gl.drawArrays(gl.TRIANGLES, 0, N.boards); }
      gl.depthMask(true);
      [P.solid, P.line].forEach(p => ['p', 'n', 'c'].forEach(a => { if (p.loc[a] >= 0) gl.disableVertexAttribArray(p.loc[a]); }));
      drawDots(now, m);
      if (camChanged) placeLabels(m.vp, sz.w, sz.h);
    }
    let dotBuf = new Float32Array(0);
    function drawDots(now, m) {
      const live = st.dots.filter(d => now >= d.t0);
      st.stats.dots = live.length;
      if (!live.length) return;
      if (dotBuf.length < live.length * 8) dotBuf = new Float32Array(Math.max(256, live.length * 2) * 8);
      let k = 0;
      for (const d of live) {
        const lk = st.L.links[d.li]; if (!lk) continue;
        const u = (now - d.t0) / d.du, p = along(lk, d.rev ? 1 - u : u);
        const c = colors['t_' + lk.kind] || colors.t_cable;
        dotBuf[k++] = p[0]; dotBuf[k++] = p[1] + 0.03; dotBuf[k++] = p[2];
        dotBuf[k++] = c[0]; dotBuf[k++] = c[1]; dotBuf[k++] = c[2]; dotBuf[k++] = 0.95; dotBuf[k++] = d.big ? 2 : 1;
      }
      const n = k / 8, p = P.dot;
      gl.useProgram(p.p); gl.uniformMatrix4fv(p.loc.vp, false, m.vp);
      gl.uniform1f(p.loc.k, canvas.height * 0.09);
      gl.bindBuffer(gl.ARRAY_BUFFER, B.dots); gl.bufferData(gl.ARRAY_BUFFER, dotBuf.subarray(0, k), gl.STREAM_DRAW);
      gl.enableVertexAttribArray(p.loc.p); gl.vertexAttribPointer(p.loc.p, 3, gl.FLOAT, false, 32, 0);
      gl.enableVertexAttribArray(p.loc.c); gl.vertexAttribPointer(p.loc.c, 4, gl.FLOAT, false, 32, 12);
      gl.enableVertexAttribArray(p.loc.s); gl.vertexAttribPointer(p.loc.s, 1, gl.FLOAT, false, 32, 28);
      gl.drawArrays(gl.POINTS, 0, n);
      ['p', 'c', 's'].forEach(a => gl.disableVertexAttribArray(p.loc[a]));
    }

    // ---- picking: the chip nearest to the pointer on screen
    function pick(x, y) {
      if (!lastM) return null;
      let best = null, bd = 26 * 26;
      st.L.nodes.forEach(n => {
        const p = project(lastM.vp, [n.x, n.y + chipHeight(n) / 2, n.z], lastM.w, lastM.h); if (!p) return;
        const d = (p.x - x) * (p.x - x) + (p.y - y) * (p.y - y);
        if (d < bd) { bd = d; best = n.id; }
      });
      return best;
    }
    const ptrs = new Map();
    let drag = null;
    stage.addEventListener('pointerdown', ev => {
      stage.focus({ preventScroll: true });
      ptrs.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      try { stage.setPointerCapture(ev.pointerId); } catch (e) { /* old browsers */ }
      drag = { x: ev.clientX, y: ev.clientY, moved: 0, panMode: ev.button === 2 || ev.shiftKey, pinch: ptrs.size === 2 ? pinchDist() : 0 };
    });
    const pinchDist = () => { const a = [...ptrs.values()]; return a.length < 2 ? 0 : Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y); };
    stage.addEventListener('pointermove', ev => {
      const r = stage.getBoundingClientRect();
      if (!drag) {
        if (ev.pointerType === 'mouse') {
          const id = pick(ev.clientX - r.left, ev.clientY - r.top);
          if (id !== st.hover) { st.hover = id; stage.style.cursor = id ? 'pointer' : ''; buildChips(); buildTraces(); want(); }
        }
        return;
      }
      if (!ptrs.has(ev.pointerId)) return;
      ptrs.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (ptrs.size === 2) {
        const d = pinchDist();
        if (drag.pinch && d) zoom(drag.pinch / d);
        drag.pinch = d; drag.moved += 10;
        return;
      }
      const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      drag.x = ev.clientX; drag.y = ev.clientY; drag.moved += Math.abs(dx) + Math.abs(dy);
      if (drag.panMode) pan(-dx, -dy); else turn(-dx * 0.008, dy * 0.006);
    });
    const end = ev => {
      ptrs.delete(ev.pointerId);
      if (!drag) return;
      const was = drag;
      if (ptrs.size === 0) drag = null;
      if (ev.type === 'pointerup' && was.moved < 6 && ptrs.size === 0) {
        const r = stage.getBoundingClientRect();
        const id = pick(ev.clientX - r.left, ev.clientY - r.top);
        if (id) { setFocus(id, false); select(id); }
      }
    };
    stage.addEventListener('pointerup', end);
    stage.addEventListener('pointercancel', end);
    stage.addEventListener('pointerleave', () => { if (st.hover && !drag) { st.hover = null; stage.style.cursor = ''; buildChips(); buildTraces(); want(); } });
    stage.addEventListener('contextmenu', ev => ev.preventDefault());
    stage.addEventListener('wheel', ev => { ev.preventDefault(); zoom(Math.exp(Math.max(-60, Math.min(60, ev.deltaY)) * 0.004)); }, { passive: false });

    function select(id) { st.selected = id; buildChips(); buildTraces(); want(); st.onSelect(id); }
    function setFocus(id, move) {
      st.focus = id || null;
      buildChips(); buildTraces(); showFocusCard();
      if (id && move !== false) aimAt(id);
      const n = id && st.nodeById.get(id);
      if (n) status.textContent = describe(n);
      want();
    }
    stage.addEventListener('keydown', ev => {
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      const order = st.L.order, i = st.focus ? order.indexOf(st.focus) : -1;
      let handled = true;
      const step = 0.12;
      switch (ev.key) {
        case 'ArrowLeft': if (ev.shiftKey) pan(-60, 0); else turn(-step, 0); break;
        case 'ArrowRight': if (ev.shiftKey) pan(60, 0); else turn(step, 0); break;
        case 'ArrowUp': if (ev.shiftKey) pan(0, -60); else turn(0, step * 0.6); break;
        case 'ArrowDown': if (ev.shiftKey) pan(0, 60); else turn(0, -step * 0.6); break;
        case '+': case '=': zoom(1 / 1.2); break;
        case '-': case '_': zoom(1.2); break;
        case '0': reset(); break;
        case 'PageDown': if (order.length) setFocus(order[Math.min(order.length - 1, i + 1)]); break;
        case 'PageUp': if (order.length) setFocus(order[Math.max(0, i < 0 ? 0 : i - 1)]); break;
        case 'Home': if (order.length) setFocus(order[0]); break;
        case 'End': if (order.length) setFocus(order[order.length - 1]); break;
        case 'Enter': case ' ': if (st.focus) select(st.focus); else if (order.length) setFocus(order[0]); break;
        default: {
          if (ev.key.length === 1 && /\S/.test(ev.key) && !/[0-9+\-=_]/.test(ev.key)) {   // a letter jumps to the next element whose name starts with it
            const t2 = Date.now();
            st.typed = (t2 - st.typedAt < 700 ? st.typed : '') + ev.key.toLowerCase(); st.typedAt = t2;
            const names = order.map(id => ((st.nodeById.get(id) || {}).title || id).toLowerCase());
            let hit = -1;
            for (let k = 1; k <= order.length && hit < 0; k++) { const j = (Math.max(0, i) + k) % order.length; if (names[j].startsWith(st.typed)) hit = j; }
            if (hit >= 0) setFocus(order[hit]);
          } else handled = false;
        }
      }
      if (handled) ev.preventDefault();
    });
    stage.addEventListener('focus', () => { if (!st.focus && st.L.order.length) setFocus(st.selected && st.nodeById.has(st.selected) ? st.selected : st.L.order[0], false); });

    // ---- sight, size, theme, lost context
    const io = typeof IntersectionObserver === 'function' ? new IntersectionObserver(es => { st.inView = es.some(e => e.isIntersecting); if (st.inView) want(); }) : null;
    if (io) io.observe(stage);
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => want()) : null;
    if (ro) ro.observe(stage);
    const onVis = () => { if (!doc.hidden) want(); };
    doc.addEventListener('visibilitychange', onVis);
    const onMotion = ev => { if (!ev || ev.key == null || ev.key === 'll-motion') { readMotion(); want(); } };
    root0win().addEventListener('storage', onMotion);
    canvas.addEventListener('webglcontextlost', ev => { ev.preventDefault(); st.dead = true; if (opts.onLost) opts.onLost(); });

    readTheme(); readMotion();
    setLayout(layout(st.view, st.filter()), false);
    return {
      update(view) { if (st.dead) return; st.view = view; setLayout(layout(view, st.filter()), true); },
      live(frame) {
        if (st.dead || !frame) return;
        let chips = false;
        if (frame.grades) for (const id in frame.grades) { const g = GRADES.includes(frame.grades[id]) ? frame.grades[id] : 'unknown'; if (st.grades[id] !== g) { st.grades[id] = g; chips = true; } }
        if (frame.metrics) for (const id in frame.metrics) st.metrics[id] = frame.metrics[id];
        if (chips) { buildChips(); want(); }
        // glow: the measured rate of a trace brightens it (log scale, like the flat board); it fades without reports
        const q = frame.q || 1, seen = new Set();
        let glow = false;
        if (frame.dots) for (const d of frame.dots) {
          const id = String(d[0]); if (!st.linkIdx.has(id)) continue;
          const v = Math.min(1, Math.log10(1 + (d[2] || 0) * q / 1024) / 4.6);
          if (!seen.has(id)) { seen.add(id); if (Math.abs((st.level.get(id) || 0) - v) > 0.04) { st.level.set(id, v); glow = true; } }
          else if (v > st.level.get(id)) { st.level.set(id, v); glow = true; }
        }
        st.level.forEach((v, id) => { if (!seen.has(id) && v > 0.01) { st.level.set(id, v * 0.6); glow = true; } });
        if (glow) { buildTraces(); want(); }
        if (st.focus && frame.metrics && frame.metrics[st.focus] !== undefined) showFocusCard();
        // dots along the traces, the same quanta as on the board; nothing moves without a live frame, with reduced motion or motion off
        if (!frame.dots || reduced() || !st.fpsCap || doc.hidden || !st.inView) return;
        const now = performance.now(), cap = st.motion === 'saver' ? 6 : 14, total = st.stats.mode === 'soft' ? 300 : 1500;
        for (const d of frame.dots) {
          const id = String(d[0]), li = st.linkIdx.get(id); if (li == null) continue;
          const key = id + (d[1] ? '<' : '>');
          const r = dotCount(d[2], st.carry.get(key), cap);
          st.carry.set(key, r.carry);
          const lk = st.L.links[li], du = Math.max(900, Math.min(4200, lk.L / 2.4 * 1000));
          for (let k = 0; k < r.n && st.dots.length < total; k++) st.dots.push({ li, id, rev: !!d[1], t0: now + (k + 0.15 + Math.random() * 0.7) / r.n * 1000, du, big: r.big });
        }
        wake();
      },
      highlight(id) {
        st.selected = id || null;
        if (id && st.nodeById.has(id)) { st.focus = id; aimAt(id); showFocusCard(); }
        buildChips(); buildTraces(); want();
      },
      focus() { stage.focus({ preventScroll: true }); },
      setTheme() { if (st.dead) return; readTheme(); buildBoards(); buildChips(); buildTraces(); want(); },
      stats() { return Object.assign({}, st.stats, { nodes: st.L ? st.L.nodes.length : 0, links: st.L ? st.L.links.length : 0, layers: st.L ? st.L.layers.length : 0 }); },
      camera() { return Object.assign({}, st.cam); },
      destroy() {
        st.dead = true;
        if (st.raf) root0win().cancelAnimationFrame(st.raf);
        if (io) io.disconnect(); if (ro) ro.disconnect();
        doc.removeEventListener('visibilitychange', onVis); root0win().removeEventListener('storage', onMotion);
        const lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();
        rootEl.remove();
      }
    };
  }

  return { layout, along, gpuVerdict, orbit, project, parseColor, dotCount, fitCamera, available, canTry, allowSoft, mount };
});
