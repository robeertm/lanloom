/* Infrastructure strands of the Real cables view (lanloom/web/js/paths_infra.js) under Node with a minimal DOM stand-in: the shares of the
   countries add up to the traffic, equal strands are bundled, nothing is an arc and nothing is dashed, a satellite hop shows up with its symbol,
   the legend names only the lines that are drawn, the country sheet cuts a way at the first hub and goes on over the inland net.
   Prints one JSON object; tests/test_paths_infra_js.py checks it. */
'use strict';
const path = require('path');
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.dataset = {}; this.text = ''; this.html = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[k.slice(5)] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  appendChild(c) { this.kids.push(c); return c; }
  set textContent(v) { this.text = v; }
  get textContent() { return this.text; }
  set innerHTML(v) { this.html = v; }
  cloneNode() { const c = new El(this.tag); c.attrs = Object.assign({}, this.attrs); c.text = this.text; return c; }
  all() { return [this].concat(...this.kids.map(k => k.all())); }
}
const store = {};
global.window = global;
global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
global.location = { hash: '#/paths' };
global.history = { state: null, replaceState() {} };
global.CustomEvent = function (type, o) { this.type = type; this.detail = o && o.detail; };
const events = [];
global.document = { createElementNS: (ns, tag) => new El(tag), addEventListener() {}, querySelectorAll: () => [], dispatchEvent: e => events.push(e.type) };
global.addEventListener = () => {};
let fetched = [];
global.Lanloom = { h: (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids }), t: (k, p) => k + (p ? JSON.stringify(p) : ''), api: u => { fetched.push(u); return Promise.resolve(NET); },
  base: '/', registerCommand() {}, query: new URLSearchParams(''), fmtBytes: b => b + ' B', fmtNum: n => String(n), countryName: cc => cc };
const root = path.join(__dirname, '..', '..', 'lanloom', 'web', 'js');
require(path.join(root, 'paths_cables.js'));
require(path.join(root, 'paths_infra.js'));
const LL = global.Lanloom, I = LL.pathsInfra;
const NET = {
  stand: 's1',
  edges: { 1: { kind: 'cable', p: [[0, 0], [10, 0]], km: 1, name: 'Test cable' }, 2: { kind: 'land', p: [[10, 0], [20, 5]], km: 1, name: null },
    3: { kind: 'sat', p: [[5, 5], [30, 40]], km: 1, name: null }, 4: { kind: 'radio', p: [[10, 0], [12, 1]], km: 1, name: null },
    5: { kind: 'land', p: [[20, 5], [26, 9]], km: 1, name: null }, 6: { kind: 'access', p: [[10, 0], [8, -2]], km: 1, name: null } },
  hubs: { a: { name: 'Alpha', cc: 'XA', p: [0, 0] }, b: { name: 'Beta', cc: 'XA', p: [20, 5] }, c: { name: 'Gamma', cc: 'XA', p: [26, 9] } },
  countries: { XA: { path: ['1', '2'], origin: 'cable', via: ['Alpha'], cables: ['Test cable'], km: 3, sat: { path: ['1'], gateway: [10, 0], gateway_name: 'Alpha' } },
    XB: { path: ['3'], origin: 'sat', via: [], cables: [], gateway_name: 'Alpha', km: 2 },
    XC: { path: ['1', '4'], origin: 'radio', via: [], cables: [], km: 2, sat: { path: ['1'], gateway: [10, 0], gateway_name: 'Alpha' } } },
  routes: { k1: { path: ['1', '2'], measured_edges: 1, origin: 'route', places: 2, via: ['Alpha'], cables: [] },
    k2: { path: ['1'], measured_edges: 1, origin: 'route', places: 1, via: ['Alpha'], cables: [], end: [10, 0], conflict: { rtt: 19, km: 9000, place: 'Alpha' } },
    k3: { path: ['1', '4'], measured_edges: 2, origin: 'route', places: 2, via: [], cables: [] } },
  inland: { XA: { hubs: { a: [0, 0], b: [20, 5], c: [26, 9] }, paths: { 'a|b': ['1', '2'], 'b|c': ['5'], 'a|c': ['1', '2', '5'] } } }
};
const targets = [{ cc: 'XA', tx: 40, rx: 60 }, { cc: 'XB', tx: 5, rx: 15 }, { cc: 'XC', tx: 10, rx: 10 }];
const routes = [{ key: 'k1', cc: 'XA', asn: 1 }, { key: 'k2', cc: 'XA', asn: 2 }, { key: 'k3', cc: 'XC', asn: 3, sat: 'leo' }];
const out = {};
out.off = I.on() === true;
const sh = I.shares(NET, targets, routes, { out: cc => cc !== 'XA', point: cc => ({ XA: [26, 9], XB: [30, 40], XC: [20, 30] })[cc] });
out.shareSum = sh.reduce((a, s) => a + s.bytes, 0);
out.targetSum = targets.reduce((a, x) => a + x.tx + x.rx, 0);
out.shareCount = sh.length;
const d = I.draw(NET, sh, { maxW: 4 });
const els = d.g.all();
out.streamSum = d.streams.reduce((a, s) => a + s.rx + s.tx, 0);
out.noArc = els.every(e => !/pw-arc|arc/.test(e.attrs.class || ''));
out.noDash = els.every(e => !('stroke-dasharray' in e.attrs) && !/dasharray/.test(e.attrs.style || ''));
out.kinds = els.filter(e => /\bin-k\b/.test(e.attrs.class || '')).map(e => (e.attrs.class.match(/in-k--(\w+)/) || [0, e.attrs.class.includes('in-sat') ? 'sat' : '?'])[1]).sort();
out.bundled = els.filter(e => e.attrs['data-e'] === '1').length;           // the cable edge 1 is used by many ways: one element
out.measuredFirst = els.filter(e => e.attrs['data-e'] === '1')[0].attrs.class.includes('is-measured');
out.sats = els.filter(e => e.attrs['data-sat']).length;
out.arts = [...d.arts].sort();
out.legend = I.legendItems('x', d.arts).map(s => s.kids[0].attrs.class);
out.legendNoSea = !out.legend.some(c => /sea/.test(c));
out.conflictEnd = sh.filter(s => s.end).map(s => [s.end, s.conflict && s.conflict.place]);
out.endText = I.endText('XA', sh);
out.satShare = sh.filter(s => s.cc === 'XC').map(s => [s.origin, s.sat && s.sat.orbit]);
// the country sheet: the way is cut at the first hub of the country, goes on over the inland net to the hub nearest the bubble, a short feeder ends it
const sheet = I.sheet(NET, { cc: 'XA', routes: [{ key: 'k1', cc: 'XA', asn: 1, city: 'Cityton' }], cities: [{ name: 'Cityton', operators: [{ asn: 1 }] }], operators: [{ asn: 1 }], totals: { tx: 40, rx: 60 } },
  [{ x: 27, y: 10, p: { name: 'Cityton', bytes: 100 } }], { frame: { x0: 18, y0: 0, x1: 30, y1: 12 }, out: false });
out.sheet = sheet.map(s => ({ ids: s.ids, tail: s.tail, measured: s.measured, bytes: s.bytes, origin: s.origin }));
const sd = I.draw(NET, sheet, { maxW: 3.4 });
out.sheetStreams = sd.streams.length;
out.sheetNoArc = sd.g.all().every(e => !/arc/.test(e.attrs.class || ''));
// satellite network in the sheet: from the earth station straight to the bubble
const sheet2 = I.sheet(NET, { cc: 'XA', routes: [], cities: [], operators: [{ asn: 9, sat: 'leo' }], totals: { tx: 1, rx: 1 } }, [{ x: 27, y: 10, p: { name: '', bytes: 2 } }], { frame: null, out: true });
out.sheetSat = sheet2.map(s => [s.origin, s.sat && s.sat.orbit, s.sat && s.sat.gw]);
// data: fetched once per stand, never without the view, the URL carries the stand
I.need('s1').then(() => I.need('s1')).then(() => {
  out.fetched = fetched;
  out.event = events.filter(e => e === 'lanloom-paths-infra').length;
  out.data = !!I.data() && I.data().stand;
  process.stdout.write(JSON.stringify(out));
});
