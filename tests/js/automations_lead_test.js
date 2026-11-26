/* The one-sentence summary of an automation ("When ..., and only if ..., then ...") under Node: a condition that holds other conditions
   is told as one clause (its parts joined), never as its list heading ("all of this holds:"). Loads js/automations.js against a stand-in of
   the page. Prints one JSON object; tests/test_automations_lead_js.py checks it. */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const noop = new Proxy(function () { return noop; }, { get: (_, k) => (k === Symbol.toPrimitive ? () => '' : noop), apply: () => noop, construct: () => ({}) });
const texts = {
  'auto.plain.lead': 'When {when}, then {then}.',
  'auto.plain.lead_only': 'When {when} and {only}, then {then}.',
  'auto.plain.lead_not': 'it is not true that {parts}',
  'auto.plain.lead_more': 'more steps follow',
  'auto.plain.nothing': 'nothing',
  'auto.plain.lead_run': 'When it runs: {then}.',
  'auto.ex.c.all': 'all of this holds:', 'auto.ex.c.and': 'all of this holds:', 'auto.ex.c.or': 'one of these holds:', 'auto.ex.c.not': 'none of these holds:',
  'auto.ex.t.state': '{who} changes', 'auto.ex.c.closed': '{who} is closed', 'auto.ex.c.locked': '{who} is locked', 'auto.ex.a.on': '{what} turns on',
};
const t = (k, v) => String(texts[k] != null ? texts[k] : k).replace(/\{(\w+)\}/g, (_, n) => (v && v[n] != null ? v[n] : ''));
const LL = new Proxy({ t, h: noop, api: noop, strings: texts, locale: () => 'en', state: {}, fmtNum: String, hubValue: String }, { get: (o, k) => (k in o ? o[k] : noop), set: (o, k, v) => { o[k] = v; return true; } });
const win = { Lanloom: LL, matchMedia: () => ({ matches: false }), addEventListener() {}, document: noop, localStorage: { getItem: () => null, setItem() {} } };
const ctx = vm.createContext(Object.assign(win, { window: win, document: noop, navigator: {}, Intl, URLSearchParams, console }));
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'automations.js'), 'utf8'), ctx);

const c = (k, v, sub) => ({ k: 'auto.ex.c.' + k, v: v || {}, sub });
const step = { k: 'auto.ex.a.on', v: { what: ['the light'] } };
const trig = { k: 'auto.ex.t.state', v: { who: ['the door'] } };
const out = {
  plain: LL.autoLead({ when: [trig], only: [c('closed', { who: ['the door'] })], then: [step] }),
  all: LL.autoLead({ when: [trig], only: [c('all', {}, [c('closed', { who: ['the door'] }), c('locked', { who: ['the lock'] })])], then: [step] }),
  or: LL.autoLead({ when: [trig], only: [c('or', {}, [c('closed', { who: ['the door'] }), c('locked', { who: ['the lock'] })])], then: [step] }),
  not: LL.autoLead({ when: [trig], only: [c('not', {}, [c('closed', { who: ['the door'] })])], then: [step] }),
  nested: LL.autoLead({ when: [trig], only: [c('and', {}, [c('closed', { who: ['the door'] }), c('or', {}, [c('locked', { who: ['the lock'] }), c('closed', { who: ['the gate'] })])])], then: [step] }),
};
process.stdout.write(JSON.stringify(out));
