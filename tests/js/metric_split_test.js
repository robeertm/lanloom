/* The split rate of a tile ("↓7.3 ↑434 kB/s") in words for a screen reader, under Node (lanloom/web/board/board.js).
   Prints one JSON object; tests/test_board_metric_split.py checks it. */
'use strict';
const path = require('path');
const B = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'board', 'board.js')).Lanloom.Board;

const t = (k, v) => B.STRINGS[k].replace(/\{(\w+)\}/g, (_, n) => v[n]);
const out = {
  split: B.speakMetric('↓7.3 ↑434 kB/s', t),
  comma: B.speakMetric('↓7,3 ↑434 kB/s', t),
  plain: ['1.5 MB/s', '', '12 msg/s', '↓ broken'].map(s => B.speakMetric(s, t))
};
// fitText: a split rate that does not fit gives up "/s" first, then falls back to the usual cut with an ellipsis
global.document = { createElement: () => ({ getContext: () => ({ font: '', measureText: t => ({ width: t.length * 6 }) }) }) };
const f = { font: '11px x', ls: 0, upper: false };
out.fit = ['\u21932.8 \u21910.2 MB/s', '\u2193986 \u2191146 kB/s', '12 MB/s'].map(s => [90, 80, 60].map(w => B.fitText(f, s, w)));
console.log(JSON.stringify(out));
