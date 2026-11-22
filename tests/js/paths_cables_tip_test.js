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
  facts: lines({ n: 'Atlas Link', km: 6400, op: null, own: 'Atlas Telco, Coast Carrier', yr: 2004, cap: 40, wd: 'Q21', src: 'osm' }),
  sameOwner: lines({ n: 'A', km: 5, op: 'Atlas Telco', own: 'Atlas Telco', src: 'osm' }),
  national: lines({ n: 'Coral Link', km: 300, op: 'Island Telco', src: 'noaa' }),
  part: lines({ n: 'Coral Link', km: 300, src: 'noaa', part: 1, yr: null, cap: 'null', own: 'null' }),
  attr: (C.openSources({ sources: [{ short: 'NOAA / BOEM', url: 'https://example.org/a', license: 'CC0 1.0', license_url: 'https://example.org/l' },
                                   { short: 'Wikidata', url: 'https://example.org/b', license: 'CC0 1.0', license_url: 'https://example.org/l' },
                                   { short: '', url: 'x' }] }) || []).length,
  attrNone: C.openSources({}),
}));
