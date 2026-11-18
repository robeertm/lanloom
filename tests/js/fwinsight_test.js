/* Firewall board under Node: the geometry and the small decisions of lanloom/web/js/fwinsight.js.
   Prints one JSON object; tests/test_fwinsight.py checks it. */
'use strict';
const path = require('path');
const F = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'fwinsight.js'));

// ---- messages: variables may be messages themselves
const tr = (key, vars) => key + '(' + Object.keys(vars || {}).sort().map(k => k + '=' + vars[k]).join(',') + ')';
const msg = { key: 'a', vars: { src: { key: 'b', vars: { x: 1, y: { key: 'c', vars: {} } } }, n: 3 }, notes: [{ key: 'n1', vars: {} }, { key: 'n2', vars: { z: 'q' } }] };

// ---- layout: every pad inside the board, no two pads touching, the same answer every time
const rects = (lay) => lay.pos.map(p => ({ x0: p.x - lay.pad.w / 2, x1: p.x + lay.pad.w / 2, y0: p.y - lay.pad.h / 2, y1: p.y + lay.pad.h / 2 }));
const touch = (a, b, m) => !(a.x1 + m <= b.x0 || b.x1 + m <= a.x0 || a.y1 + m <= b.y0 || b.y1 + m <= a.y0);
const layouts = {};
[false, true].forEach(narrow => {
  const res = [];
  for (let n = 0; n <= 16; n++) {
    const lay = F.boardLayout(n, narrow);
    const rs = rects(lay);
    let inside = rs.every(r => r.x0 >= 0 && r.y0 >= 0 && r.x1 <= lay.w && r.y1 <= lay.h);
    let clash = 0;
    for (let i = 0; i < rs.length; i++) for (let j = i + 1; j < rs.length; j++) if (touch(rs[i], rs[j], 6)) clash++;
    res.push({ n, count: lay.pos.length, inside, clash, same: JSON.stringify(lay) === JSON.stringify(F.boardLayout(n, narrow)), w: lay.w, h: lay.h });
  }
  layouts[narrow ? 'narrow' : 'wide'] = res;
});

// ---- edges
const size = { w: 158, h: 68 };
const A = { x: 200, y: 200 }, B = { x: 700, y: 260 };
const ab = F.edgeGeometry(A, B, size), ba = F.edgeGeometry(B, A, size);
const side = (g, a, b) => Math.sign((b.x - a.x) * (g.ctl.y - a.y) - (b.y - a.y) * (g.ctl.x - a.x));
const inPad = (p, c) => Math.abs(p.x - c.x) < size.w / 2 - 0.01 && Math.abs(p.y - c.y) < size.h / 2 - 0.01;
const edge = {
  sides: [side(ab, A, B), side(ba, B, A)],                      // both curves bend to the right of their own direction
  opposite: side(ab, A, B) === -side(ba, A, B) && side(ab, A, B) !== 0,      // the line there and the one back lie on different sides
  startOutside: !inPad(ab.start, A), endOutside: !inPad(ab.end, B),
  startOnBorder: Math.abs(Math.abs(ab.start.x - A.x) - (size.w / 2 + 2)) < 6 || Math.abs(Math.abs(ab.start.y - A.y) - (size.h / 2 + 2)) < 6,
  arrowTip: ab.arrow[0].x === Math.round(ab.end.x * 10) / 10 && ab.arrow[0].y === Math.round(ab.end.y * 10) / 10,
  arrowPoints: ab.arrow.length,
  pathShape: /^M[\d.\-]+ [\d.\-]+ Q[\d.\-]+ [\d.\-]+ [\d.\-]+ [\d.\-]+$/.test(ab.d),
  straightUp: (() => { const g = F.edgeGeometry({ x: 300, y: 400 }, { x: 300, y: 100 }, size); return [g.start.y < 400, g.end.y > 100, isFinite(g.mid.x)]; })(),
  samePoint: (() => { const g = F.edgeGeometry(A, A, size); return [isFinite(g.start.x), isFinite(g.end.y)]; })()
};
const cp = [F.curvePoint(ab, 0), F.curvePoint(ab, 1)];
edge.curveEnds = [Math.abs(cp[0].x - ab.start.x) < 0.2 && Math.abs(cp[0].y - ab.start.y) < 0.2, Math.abs(cp[1].x - ab.end.x) < 0.2 && Math.abs(cp[1].y - ab.end.y) < 0.2];
const mid = F.curvePoint(ab, 0.5);
const free = F.placeLabel(ab, { w: 40, h: 16 }, [], []);
const dodge = F.placeLabel(ab, { w: 40, h: 16 }, [{ x: mid.x, y: mid.y, w: 40, h: 16 }], []);
const walled = F.placeLabel(ab, { w: 40, h: 16 }, [{ x: 450, y: 230, w: 900, h: 400 }], []);
edge.labels = { middle: free.x === mid.x && free.y === mid.y, moved: dodge != null && (dodge.x !== mid.x || dodge.y !== mid.y), none: walled };

// ---- width by hits
const widths = [0, 1, 10, 100, 9000, 100000].map(n => F.hitWidth(n, 100000));
const hit = {
  widths, rising: widths.every((w, i) => i === 0 || w >= widths[i - 1]), lo: widths[0], hi: widths[widths.length - 1],
  noMax: F.hitWidth(50, 0), over: F.hitWidth(1e9, 100), custom: F.hitWidth(10, 10, 2, 5), none: F.hitWidth(null, 10)
};

// ---- the rest
const kinds = [
  { allow: true, hits: 0, blocked: 0 }, { allow: false, hits: 0, blocked: 0 }, { allow: false, hits: 5, blocked: 0 },
  { allow: false, hits: 0, blocked: 2 }, { allow: true, hits: 3, blocked: 2 }].map(F.edgeKinds);
const out = {
  plain: F.plainText(msg, tr), notes: F.plainNotes(msg, tr), plainNone: [F.plainText(null, tr), F.plainText('x', tr), F.plainText({ nothing: 1 }, tr), F.plainText({ key: 'k' }, tr), F.plainNotes(null, tr), F.plainNotes({ key: 'k' }, tr)],
  layouts, edge, hit, kinds,
  summary: [F.summarize([]), F.summarize([{ level: 'info' }, { level: 'warn' }]), F.summarize([{ level: 'warn' }, { level: 'alert' }, { level: 'info' }]), F.summarize(null), F.summarize([{}])],
  sorted: F.sortFindings([{ level: 'info', id: 1 }, { level: 'alert', id: 2 }, { level: 'warn', id: 3 }, { level: 'alert', id: 4 }]).map(f => f.id),
  ports: ['', '  ', null, '443', ' 80 ', '0', '65535', '65536', '12ab', '-1', '1.5', '123456', '٣'].map(x => F.parsePort(x)),
  query: [F.askQuery('int:5', { from: 'internet', to: 'zone:z_a', proto: 'tcp', port: '443' }), F.askQuery(null, { from: 'a b', to: 'n_1', proto: '', port: '' }),
    F.askQuery('x', { from: 'a', to: 'b', proto: 'icmp', port: '99999' }), F.askQuery('x', { from: 'a', to: 'b', proto: 'udp', port: '53' })],
  key: F.pairKey('a', 'b'), version: F.version
};
process.stdout.write(JSON.stringify(out));
