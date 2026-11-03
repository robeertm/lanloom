/* End and port marks: pure logic under Node (placement search, bundling equal edge targets into one label, stable bundle name
   with a 1.5× lead over 8 s, detail levels, texts from neutral facts). Prints one JSON object; the pytest wrapper asserts on it. */
'use strict';
const path = require('path');
const B = path.join(__dirname, '..', '..', 'lanloom', 'web', 'board');
const EM = require(path.join(B, 'endmarks.js'));
const PM = require(path.join(B, 'portmarks.js'));
const I = EM._internals;
const out = {};

// text width without a canvas: 6 px per character
I.setMeasure(t => t.length * 6);
PM._internals.setMeasure(t => t.length * 6);
I.setFont(11);

// ------------------------------------------------------------------ stable bundle name (1.5× lead, 8 s)
{
  const F = I.bundleLead, sp = new Map(), M = ['a', 'b'], nm = id => id.toUpperCase();
  const z = (g1, g2) => [{ id: 'alpha', weight: g1 }, { id: 'beta', weight: g2 }];
  const leadAt = (g1, g2, t) => F(sp, 't', M, z(g1, g2), t, nm)[0].id;
  const r = {};
  r.firstSimilar = leadAt(100, 90, 0);
  r.firstSimilarSwapped = F(new Map(), 't', M, z(90, 100), 0, nm)[0].id;
  r.firstClear = F(new Map(), 't', M, z(10, 100), 0, nm)[0].id;
  const seq = []; for (let t = 1000; t <= 30000; t += 1000) seq.push(leadAt(t % 2000 ? 100 : 90, t % 2000 ? 90 : 100, t));
  r.alternating = [...new Set(seq)];
  let t = 40000; const after = (g1, g2, dt) => { t += dt; return leadAt(g1, g2, t); };
  r.peak = [after(10, 300, 0), after(10, 300, 5000), after(100, 90, 1000)];
  r.sustained = [after(10, 300, 1000), after(10, 300, 4000), after(10, 300, 4000), after(10, 300, 1000)];
  r.back = [after(300, 10, 1000), after(10, 300, 1000)];
  r.order = F(sp, 't', M, [{ id: 'x', weight: 5 }, { id: 'alpha', weight: 1 }, { id: 'beta', weight: 9 }], t + 1000, nm).map(q => q.id);
  r.gone = F(sp, 't', M, [{ id: 'x', weight: 5 }], t + 2000, nm)[0].id;
  out.name = r;
}

// ------------------------------------------------------------------ placement search
{
  const V = { x0: 4, y0: 4, x1: 996, y1: 596 };
  const inImage = r => r.x0 >= V.x0 && r.y0 >= V.y0 && r.x1 <= V.x1 && r.y1 <= V.y1;
  const place = I.makePlacer(inImage);
  const H = I.height();
  const obstacle = [{ x0: 90, y0: 90, x1: 200, y1: 120 }];
  const cand = [w => [100, 100], w => [100, 140], w => [100, 180]];
  const a = place(['→ storage'], cand, [obstacle]);
  const b = place(['→ storage'], cand, [[{ x0: 0, y0: 0, x1: 1000, y1: 170 }]]);
  // a long text does not fit at the right edge → the shorter variant is taken there
  const long = '→ ' + 'x'.repeat(36), shortT = '→ 1';
  const c = place([long, shortT], [w => [990 - w, 300]], []);
  const d = place([long, shortT], [w => [960, 300]], []);
  const e = place(['→ storage'], cand, [[{ x0: 0, y0: 0, x1: 1000, y1: 600 }]]);
  out.place = { height: H, a: a && [a.r.x0, a.r.y0, a.short], b: b && [b.r.x0, b.r.y0], c: c && [c.text === long, c.r.x1 <= V.x1], d: d && [d.text, d.short], e };
  // labels are never wider than MAXW: the text is shortened with an ellipsis
  out.place.steps = I.steps('→ ', 'a-very-long-peer-name-that-does-not-fit-anywhere.example', '');
}

// ------------------------------------------------------------------ bundling equal edge targets into one label
{
  const host = { color: () => '#888', peerName: id => id, nodeName: nid => nid.toUpperCase(), t: null };
  const fake = Object.create(EM.Endmarks.prototype);
  Object.assign(fake, { host, locale: 'en', t: (k, v) => String(EM.STRINGS[k]).replace(/\{(\w+)\}/g, (m, x) => v[x]) });
  const end = (inner, x, peers) => ({ inner, rid: '__edge', host: false, g: 1, weight: 1, b: { id: 't:' + inner, kind: 'flow', exit: { s: 't', end: [x, 0] } }, pad: { x, y: 0, s: 't' },
    peers: peers.map(([id, o, i]) => ({ id, out: o, in: i, weight: o + i })) });
  const run = ends => {
    fake.ends = ends; fake._bundlePairs = new Map(); fake._bundleNames = new Map();
    const V = { x0: 4, y0: 4, x1: 1196, y1: 796 }, specs = [], taken = [];
    const inImage = r => r.x0 >= V.x0 && r.y0 >= V.y0 && r.x1 <= V.x1 && r.y1 <= V.y1;
    const ctx = { tx: x => x, ty: y => y + 40, V, inside: (x, y, R) => x >= R.x0 && x <= R.x1 && y >= R.y0 && y <= R.y1,
      hard: [], soft: [], tiles: [], taken, specs, placeAt: I.makePlacer(inImage) };
    const done = fake.placeBundles(ctx);
    return { done: [...done].map(e => e.inner).sort(), specs: specs.map(s => ({ text: s.text, bundle: s.bundle, sources: s.sources.map(q => q.nid), entries: s.entries.map(e => e.id), bus: s.bus.length })) };
  };
  out.bundle = {
    same: run([end('a', 100, [['cloud', 500, 0]]), end('b', 220, [['cloud', 300, 0]]), end('c', 340, [['cloud', 100, 50]])]),
    related: run([end('a', 100, [['cloud', 500, 0], ['dns', 10, 0]]), end('b', 220, [['dns', 300, 300]])]),
    far: run([end('a', 100, [['cloud', 500, 0]]), end('b', 1100, [['cloud', 300, 0]])]),
    different: run([end('a', 100, [['cloud', 500, 0]]), end('b', 220, [['backup', 300, 0]])])
  };
}

// ------------------------------------------------------------------ detail levels
{
  const D = PM._internals.detail;
  out.lod = {
    endFont: [0.5, 1, 1.3, 2, 4].map(I.fontFor),
    logical: [0.6, 0.7, 1].map(s => D(s, false, false).calm),
    network: [0.8, 0.86, 1].map(s => D(s, true, false).calm),
    touch: D(2, false, true).calm,
    portFont: [0.5, 1, 1.1, 1.2, 3].map(s => D(s, false, false).fs)
  };
}

// ------------------------------------------------------------------ geometry and arrows
out.exit = [
  I.exitPoint([[50, 50], [50, 900]], { x0: 6, y0: 6, x1: 494, y1: 494 }),
  I.exitPoint([[50, 50], [600, 50]], { x0: 6, y0: 6, x1: 494, y1: 494 }),
  I.exitPoint([[50, 50], [60, 60]], { x0: 6, y0: 6, x1: 494, y1: 494 })
];
out.arrow = [I.arrowFor(100, 0), I.arrowFor(0, 100), I.arrowFor(100, 20), I.arrowFor(100, 10)];

// ------------------------------------------------------------------ port mark texts from neutral facts
{
  const M = PM._internals.markOf, tr = { id: 't1', a: 'x', b: 'y', pts: [[0, 0], [10, 0]] };
  const m = f => { const r = M(Object.assign({ trace: tr, end: 'b' }, f)); return r && { kind: r.kind, short: r.short, long: r.long, title: r.title || null, group: r.group }; };
  out.facts = {
    service: m({ link: 'service', targetNode: 'y', targetName: 'Broker', servicePorts: [1883], listedPorts: [1883, 8883], seen: true }),
    serviceTwo: m({ link: 'service', targetNode: 'y', targetName: 'Storage', servicePorts: [445, 22], listedPorts: [22, 445], seen: true }),
    serviceNone: m({ link: 'service', targetNode: 'y', targetName: 'X', servicePorts: [], listedPorts: [] }),
    uplink: m({ link: 'uplink', parentName: 'Router', deviceName: 'Switch', uplinkPort: 3, ownPort: 9, ownMedium: 'SFP+' }),
    uplinkNone: m({ link: 'uplink', parentName: 'Router', deviceName: 'Switch' }),
    radioUplink: m({ link: 'uplink', parentName: 'Router', radio: true }),
    wired: m({ link: 'wired', parentName: 'Switch', port: 4, ownPort: 'eth0' }),
    wiredUnknown: m({ link: 'wired', parentName: 'Switch' }),
    radio: m({ link: 'radio', parentName: 'AP', radio: { band: '5ghz', ssid: 'home' } }),
    radioStd: m({ link: 'radio', parentName: 'AP', radio: { band: '11ax' } }),
    chip: PM.chipHtml({ link: 'wired', port: 10, portMedium: 'SFP+', ownPort: 'eth0' }),
    chipNone: PM.chipHtml({ link: 'radio', radio: {} })
  };
}
console.log(JSON.stringify(out));
