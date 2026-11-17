/* The command palette's matcher under Node (js/fuzzy.js). Prints one JSON object; tests/test_palette.py compares the scores with
   lanloom/palette.py, because views and commands are matched in the page and devices and hub entities on the server - the two must rank alike. */
'use strict';
const path = require('path');
const F = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'fuzzy.js'));

const cases = JSON.parse(process.argv[2]);          // [[query, title, extra], ...]
const out = { fold: cases.map(c => F.fold(c[1])), scores: [], typo: [], dist: [] };
for (const [q, title, extra] of cases) {
  const tokens = F.words(q);
  out.scores.push(F.score(tokens, { title, extra }, false));
  out.typo.push(F.score(tokens, { title, extra }, true));
}
for (const [a, b] of JSON.parse(process.argv[3])) out.dist.push(F.distance(a, b, 2));
const items = JSON.parse(process.argv[4]).map((title, i) => ({ title, extra: '', order: i }));
out.rank = F.rank(items, process.argv[5], 0).map(r => r.item.title);
process.stdout.write(JSON.stringify(out));
