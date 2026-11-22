/* Axis block (lanloom/web/js/axis.js) and the label arrangement of the 3D view (scene3d.arrangeLabels). One JSON object; tests/test_axis_js.py checks it. */
'use strict';
const A = require('../../lanloom/web/js/axis.js');
const S = require('../../lanloom/web/board/scene3d.js');
const out = {};
out.nice = [5.2, 1.1, 0.07, 130, 2.6, 9, 1000].map(v => A.niceCeil(v));
const calm = Array.from({ length: 200 }, (_, i) => 100 + (i % 7) * 20);
out.calm = A.scale(calm);
const spiky = calm.concat([2000]);
out.spiky = A.scale(spiky);
out.pct = A.scale([3, 99, 12], { pct: true });
out.flat = A.scale([5, 5, 5, 5]);
out.small = A.ticks({ lo: 0, hi: 10 }, 3, true);
out.normal = A.ticks({ lo: 0, hi: 9 }, 3, false);
out.empty = A.scale([]);
// labels: measured boxes never overlap, stay in the picture, the one in focus is always shown
const items = [];
for (let i = 0; i < 12; i++) items.push({ x: 300 + (i % 3) * 5, y: 200, w: 80, h: 22, pri: i === 0 });
items.push({ x: -30, y: 50, w: 100, h: 22 }); items.push({ x: 790, y: 395, w: 120, h: 22 });
const res = S.arrangeLabels(items, 800, 400, []);
const boxes = res.filter(r => !r.hidden).map(r => [r.x - Math.min(r.item.w, 800) / 2, r.y - r.item.h, r.x + Math.min(r.item.w, 800) / 2, r.y]);
let overlap = 0;
for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) { const a = boxes[i], b = boxes[j]; if (a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1]) overlap++; }
out.overlap = overlap;
out.inside = boxes.every(b => b[0] >= 0 && b[1] >= 0 && b[2] <= 800 && b[3] <= 400);
out.shown = boxes.length; out.hidden = res.filter(r => r.hidden).length;
out.focusShown = !res[0].hidden;
out.lifted = res.slice(1, 6).filter(r => !r.hidden && r.y < 200).length;
const again = S.arrangeLabels(items, 800, 400, []);
out.same = JSON.stringify(again.map(r => [r.x, r.y, r.hidden])) === JSON.stringify(res.map(r => [r.x, r.y, r.hidden]));
process.stdout.write(JSON.stringify(out));
