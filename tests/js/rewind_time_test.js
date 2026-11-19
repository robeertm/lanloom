/* "Rewind to <time>" under Node (js/rewind.js R.parseTime): a minimal page stub, the times are read with a fixed "now". Prints a JSON list. */
'use strict';
const path = require('path');
const noop = () => {};
global.window = { Lanloom: { h: noop, t: k => k, api: noop, registerCommand: noop, locale: () => 'en' }, addEventListener: noop, matchMedia: () => ({ matches: false }) };
global.document = { querySelectorAll: () => [], querySelector: () => null, documentElement: {} };
global.getComputedStyle = () => ({ getPropertyValue: () => '' });
require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'rewind.js'));
const cases = JSON.parse(process.argv[2]), now = Number(process.argv[3]);
process.stdout.write(JSON.stringify(cases.map(c => window.Lanloom.rewind.parseTime(c, now))));
