/* 3D view (pure part): one layer per step of the chain, traces with a via down to the board below, camera and projection. */
'use strict';
const S = require('../../lanloom/web/board/scene3d.js');
const comp = (id, kind, title, extra) => Object.assign({ id, kind, title, subtitle: '', status: 'up', grade: 'realtime', metric: '', zone: 'core' }, extra || {});
const view = {
  components: [
    comp('net', 'internet', 'Internet', { zone: 'edge' }), comp('gw', 'router', 'Gateway'), comp('sw', 'switch', 'Switch'), comp('ap', 'ap', 'Access point'),
    comp('pc', 'computer', 'Desk PC'), comp('ph', 'phone', 'Phone', { status: 'down' }), comp('nas', 'nas', 'Storage'), comp('vm', 'server', 'Guest'),
    comp('sw2', 'switch', 'Desk switch'), comp('lone', 'iot', 'Lonely sensor')
  ],
  traces: [
    { id: 'u:gw', a: 'net', b: 'gw', kind: 'uplink' },
    { id: 'p:1', a: 'gw', b: 'sw', kind: 'lan', phys: { port: '1' } },
    { id: 'p:2', a: 'sw', b: 'ap', kind: 'lan', phys: { port: '4' } },
    { id: 'p:3', a: 'sw', b: 'pc', kind: 'lan', phys: { port: '5' } },
    { id: 'p:4', a: 'ap', b: 'ph', kind: 'radio', phys: { band: '5' } },
    { id: 'p:5', a: 'sw', b: 'nas', kind: 'lan', phys: { port: '6' } },
    { id: 'p:6', a: 'nas', b: 'vm', kind: 'virtual' },
    { id: 'p:7', a: 'nas', b: 'sw2', kind: 'lan', phys: { port: '2' } },        // a switch behind a device stays on the device layer
    { id: 'flow:1', a: 'pc', b: 'lone', kind: 'flow' }
  ]
};
const out = {};
const L = S.layout(view, {});
const by = id => L.nodes.find(n => n.id === id);
out.layers = Object.fromEntries(L.nodes.map(n => [n.id, n.layer]));
out.layerY = L.layers.map(l => l.y);
out.ys = Object.fromEntries(L.nodes.map(n => [n.id, n.y]));
out.order = L.order;
out.linkIds = L.links.map(l => l.id).sort();
const lk = L.links.find(l => l.id === 'p:3');
out.p3 = { a: lk.a, b: lk.b, first: lk.pts[0], last: lk.pts[lk.pts.length - 1], n: lk.pts.length, L: lk.L };
out.p3pc = [by('pc').x, by('pc').z]; out.p3sw = [by('sw').x, by('sw').y, by('sw').z];
out.radioKind = L.links.find(l => l.id === 'p:4').kind;
// every chip of one layer sits on its own spot
const spots = new Set(L.nodes.map(n => n.layer + ':' + n.x.toFixed(3) + ':' + n.z.toFixed(3)));
out.distinct = spots.size === L.nodes.length;
// every chip lies on its board
out.onBoard = L.nodes.every(n => { const l = L.layers.find(x => x.layer === n.layer && x.loose === n.loose); return n.x > l.x0 && n.x < l.x1 && n.z > l.z0 && n.z < l.z1 && n.y === l.y; });
// along(): the ends and the middle of a trace
out.along0 = S.along(lk, 0); out.along1 = S.along(lk, 1);
// problems only: the chain above the problem stays, the rest goes
const P = S.layout(view, { only: 'problems' });
out.problems = P.nodes.map(n => n.id).sort();
// renderer names
out.gpu = ['ANGLE (Vendor, Integrated GPU, OpenGL 4.1)', 'V3D 7.1', 'SwiftShader Device (Subzero)', 'llvmpipe (LLVM 15.0.7, 128 bits)', 'Basic Render Driver', 'Mali-G610'].map(S.gpuVerdict);
// camera: the projected target lands in the middle of the screen; a point behind the eye is not drawn
const cam = S.fitCamera(L.bounds, 16 / 9);
const m = S.orbit(cam, 16 / 9);
const c = S.project(m.vp, [cam.tx, cam.ty, cam.tz], 1600, 900);
out.centre = [Math.round(c.x), Math.round(c.y)];
out.behind = S.project(m.vp, [m.eye[0] * 3 - cam.tx * 2, m.eye[1] * 3 - cam.ty * 2, m.eye[2] * 3 - cam.tz * 2], 1600, 900);
out.colors = [S.parseColor('#0C8574'), S.parseColor('#abc'), S.parseColor('rgba(38, 72, 56, .14)'), S.parseColor('nonsense', [1, 2, 3, 4])];
out.dots = [S.dotCount(3.5, 0, 14), S.dotCount(100, 0, 14), S.dotCount(0.4, 0.7, 14)];
// a large network: 1 gateway, 8 switches with 30 devices each - the board stays roughly square and the layout is quick
const big = { components: [comp('net', 'internet', 'Internet', { zone: 'edge' }), comp('gw', 'router', 'Gateway')], traces: [{ id: 'u', a: 'net', b: 'gw', kind: 'uplink' }] };
for (let s = 0; s < 8; s++) {
  big.components.push(comp('s' + s, 'switch', 'Switch ' + s)); big.traces.push({ id: 'p:s' + s, a: 'gw', b: 's' + s, kind: 'lan', phys: { port: String(s) } });
  for (let d = 0; d < 30; d++) { const id = 'd' + s + '_' + d; big.components.push(comp(id, 'computer', 'Device ' + id)); big.traces.push({ id: 'p:' + id, a: 's' + s, b: id, kind: 'lan', phys: { port: String(d) } }); }
}
const t0 = Date.now();
const BL = S.layout(big, {});
out.bigMs = Date.now() - t0;
const dev = BL.layers.find(l => l.layer === 3);
out.bigAspect = (dev.x1 - dev.x0) / (dev.z1 - dev.z0);
out.bigNodes = BL.nodes.length; out.bigLinks = BL.links.length;
out.bigDistinct = new Set(BL.nodes.map(n => n.layer + ':' + n.x.toFixed(3) + ':' + n.z.toFixed(3))).size === BL.nodes.length;
out.looseBoards = L.layers.filter(l => l.loose).length;
const lb = L.layers.find(l => l.loose), db = L.layers.find(l => l.layer === 3 && !l.loose);
out.apart = lb.x0 >= db.x1 || lb.x1 <= db.x0 || lb.z0 >= db.z1 || lb.z1 <= db.z0;
out.empty = S.layout({ components: [], traces: [] }, {}).nodes.length;
process.stdout.write(JSON.stringify(out));
