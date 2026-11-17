/* Line filter of the board legend under Node: the pure helpers that decide what a hidden kind removes (lanloom/web/board/board.js).
   Prints one JSON object; tests/test_board_legend_filter.py checks it. */
'use strict';
const path = require('path');
const B = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'board', 'board.js')).Lanloom.Board;

const sorted = s => (s ? [...s].sort() : null);
const out = {
  keys: B.FILTER_KEYS,
  parse: ['["radio","cable"]', '["radio","nonsense",3]', '{"radio":true}', 'not json', '', null, '[]'].map(r => sorted(B.parseHidden(r))),
  visible: [[], ['radio'], ['unmeasured'], ['radio', 'flow']].map(h => sorted(B.visibleKinds(new Set(h)))),
  shown: [
    [{ kind: 'radio', measured: true }, []],
    [{ kind: 'radio', measured: true }, ['radio']],
    [{ kind: 'cable', measured: true }, ['radio']],
    [{ kind: 'cable', measured: false }, ['unmeasured']],
    [{ kind: 'cable', measured: true }, ['unmeasured']],
    [{ kind: 'odd', measured: true }, ['lan']],
    [{}, ['flow']],
    [undefined, ['unmeasured']]
  ].map(([t, h]) => B.traceShown(t, new Set(h))),
  version: B.version
};
process.stdout.write(JSON.stringify(out));
