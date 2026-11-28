// O12: the room of a hit area (extracted from js/handy.js between the hit-room markers): only the part of a neighbour that can be seen counts
const fs = require('fs'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'handy.js'), 'utf8');
const m = /\/\/ <hit-room>([\s\S]*?)\/\/ <\/hit-room>/.exec(src);
if (!m) { console.error('markers missing'); process.exit(1); }
const { seenPart, gaps } = new Function(m[1] + '; return { seenPart, gaps };')();
const box = (left, top, w, h) => ({ left, top, right: left + w, bottom: top + h });
// the case of the 1.66.5 gate: a header button 290..334 x 8..44, the page scroller starts under the two-row header (105) and is scrolled
// so far that an "Explain" button of a report row lies at 315 x 47.8 - laid out behind the header, cut away by the scroller
const header = box(290, 8, 44, 36);
const scroller = box(0, 105, 390, 680);
const hidden = box(315, 47.8, 44, 44);
const half = box(315, 90, 44, 44);           // half under the header edge: the lower part can be seen from 105 on
const below = box(8, 52, 374, 44);           // the site switch row of the header itself, not clipped
const out = {
  hiddenSeen: seenPart(hidden, [scroller]),
  halfSeen: seenPart(half, [scroller]),
  noClip: seenPart(below, []),
  twoClips: seenPart(box(10, 10, 100, 100), [box(0, 50, 400, 400), box(0, 0, 60, 400)]),
  withHidden: gaps(header, [seenPart(hidden, [scroller]), below]),
  withoutClipLogic: gaps(header, [hidden, below]),
  withHalf: gaps(header, [seenPart(half, [scroller]), below]),
  sides: gaps(box(100, 100, 30, 30), [box(60, 105, 20, 20), box(150, 100, 20, 20), box(100, 40, 30, 20), box(105, 170, 10, 10)]),
};
console.log(JSON.stringify(out));
