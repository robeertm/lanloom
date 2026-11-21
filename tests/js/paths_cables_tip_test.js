/* Cable tip lines (lanloom#12) under Node: a cable without operator has no operator line and no node turns into the text "null". */
'use strict';
const path = require('path');
global.window = global;
global.localStorage = { getItem: () => null, setItem() {} };
global.location = { hash: '#/paths' };
global.history = { state: null, replaceState() {} };
global.CustomEvent = function () {};
global.document = { addEventListener() {}, querySelectorAll: () => [], dispatchEvent() {} };
global.addEventListener = () => {};
const h = (tag, _a, ...kids) => ({ tag, text: kids.filter(k => k != null).join('') });
global.Lanloom = { h, t: (k, v) => k + (v ? JSON.stringify(v) : ''), api: () => Promise.resolve(null), base: '/', registerCommand() {},
  query: new URLSearchParams(''), fmtNum: n => String(n) };
require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'paths_cables.js'));
const C = global.Lanloom.pathsCables;
const lines = e => C.tipNodes(e).map(n => n.text);
process.stdout.write(JSON.stringify({
  none: lines({ n: 'TAGIDE', km: 1432, op: null }),
  missing: lines({ n: 'TAGIDE', km: 1432 }),
  empty: lines({ n: 'X', km: 1, op: '  ' }),
  literal: lines({ n: 'X', km: 1, op: 'null' }),
  withOp: lines({ n: 'SEA-ME-WE', km: 20000, op: 'Consortium' }),
}));
