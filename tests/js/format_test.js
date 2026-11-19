/* Text and number formatting under Node: the JavaScript side of the translation texts (js/textfmt.js and the English fallback inside
   the board modules). Prints one JSON object; tests/test_i18n_format.py compares it with lanloom/textfmt.py. */
'use strict';
const path = require('path');
const T = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'textfmt.js'));
const EM = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'board', 'endmarks.js'));

const texts = JSON.parse(process.argv[2]);          // [[text, vars, lang], ...]
const out = { fill: texts.map(([s, v, lang]) => T.fill(s, v, lang)) };
const nums = JSON.parse(process.argv[3]);           // [[lang, n, digits|null], ...]
out.num = nums.map(([lang, n, d]) => T.num(lang, n, d));
out.cat = ['en', 'de', 'fr', 'es', 'nl', 'it'].map(l => [0, 1, 2, 5, 21, 1000000].map(n => T.category(l, n)));
out.fallback = EM._internals.fill ? [1, 2, 7].map(n => EM._internals.fill('{n, plural, one {# tile} other {# tiles}} → {name}', { n, name: 'X' })) : null;
process.stdout.write(JSON.stringify(out));
