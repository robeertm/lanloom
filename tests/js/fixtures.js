/* Synthetic, neutral board views for tests and demos (no real names or addresses; RFC 1918 example ranges only).
   makeView(n, {long, seed}) → map model {view, zones, components, traces} with roughly 1.7 traces per component. */
(function (root, factory) {
  const F = factory();
  if (typeof module === 'object' && module.exports) module.exports = F;
  else { root.Lanloom = root.Lanloom || {}; root.Lanloom.Fixtures = F; }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function rng(seed) {
    let s = (seed >>> 0) || 1;
    return () => { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; };
  }
  const GRADES = ['realtime', 'realtime', 'realtime', 'near', 'near', 'periodic', 'sluggish', 'interrupted', 'unknown'];
  const ZONES = [
    { id: 'edge', title: 'Internet edge', kind: 'edge', span: 'full' },
    { id: 'core', title: 'Core network', kind: 'network' },
    { id: 'servers', title: 'Servers and storage', kind: 'servers' },
    { id: 'services', title: 'Services', kind: 'services' },
    { id: 'clients', title: 'Workstations', kind: 'clients' },
    { id: 'mobile', title: 'Mobile devices', kind: 'mobile' },
    { id: 'automation', title: 'Home automation', kind: 'automation' },
    { id: 'media', title: 'Media and entertainment', kind: 'media' }
  ];
  // [zone, kind, title, badges]
  const POOL = [
    ['servers', 'nas', 'Storage server', ['SMB', 'HTTPS']],
    ['servers', 'server', 'Build server', ['SSH', 'HTTP']],
    ['services', 'broker', 'MQTT broker', ['MQTT']],
    ['services', 'service', 'DNS resolver', ['DNS']],
    ['automation', 'hub', 'Automation hub', ['HTTP', 'WS']],
    ['clients', 'computer', 'Office desktop', ['SSH']],
    ['clients', 'self', 'This host', ['HTTP']],
    ['mobile', 'phone', 'Phone', []],
    ['clients', 'printer', 'Printer', ['IPP']],
    ['media', 'tv', 'Living room TV', ['UPnP']],
    ['media', 'media', 'Media player', ['UPnP', 'HTTP']],
    ['automation', 'camera', 'Camera', ['RTSP']],
    ['automation', 'iot', 'Sensor', ['MQTT']],
    ['services', 'service', 'Reverse proxy', ['HTTPS', 'TLS']],
    ['servers', 'server', 'Backup server', ['SSH', 'SMB']],
    ['clients', 'computer', 'Laptop', []],
    ['mobile', 'phone', 'Tablet', []],
    ['automation', 'iot', 'Smart plug', ['HTTP']],
    ['services', 'service', 'Time server', ['NTP']],
    ['media', 'media', 'Speaker', ['mDNS']],
    ['clients', 'unknown', 'Unknown device', []],
    ['automation', 'iot', 'Thermostat', ['MQTT']]
  ];
  const LONG = 'Extraordinarily long component title that will never fit into any tile at all';

  function fmtRate(r) {
    if (r >= 1e6) return (r / 1e6).toFixed(1) + ' MB/s';
    if (r >= 1e3) return Math.round(r / 1e3) + ' kB/s';
    return Math.round(r) + ' B/s';
  }

  function makeView(n, opts) {
    opts = opts || {};
    const rnd = rng(opts.seed || 7);
    const long = !!opts.long;
    const comps = [], traces = [];
    const add = (c) => { comps.push(c); return c; };
    let ip = 10;
    const mk = (zone, kind, title, badges, extra) => add(Object.assign({
      id: 'c' + comps.length, zone, kind,
      title: long ? LONG + ' ' + comps.length : title,
      subtitle: long ? '192.168.100.' + (ip++ % 250) + ' · a very long subtitle that keeps on going and going' : '192.168.1.' + (ip++ % 250),
      status: rnd() < 0.06 ? 'down' : rnd() < 0.08 ? 'unknown' : 'up',
      grade: GRADES[Math.floor(rnd() * GRADES.length)],
      metric: long ? '1023.9 MB/s down · 512.4 MB/s up · 99 999 packets' : (rnd() < 0.85 ? fmtRate(Math.pow(10, 2 + rnd() * 5)) : null),
      badges: long ? ['VERYLONGBADGE', 'ANOTHERLONGBADGE', 'THIRDBADGE'] : badges.slice(0, 3)
    }, extra || {}));
    const inet = mk('edge', 'internet', 'Internet', [], { subtitle: long ? 'Public uplink with an extremely long description text' : 'Uplink', metric: '24.6 MB/s' });
    const router = mk('edge', 'router', 'Router', ['SNMP', 'DNS']);
    traces.push({ id: 't' + traces.length, a: inet.id, b: router.id, kind: 'uplink', measured: true });
    // switches and access points scale with size
    const nSw = Math.max(1, Math.round(n / 30)), nAp = Math.max(1, Math.round(n / 40));
    const infra = [];
    for (let i = 0; i < nSw; i++) {
      const s = mk('core', 'switch', i ? 'Switch ' + (i + 1) : 'Core switch', ['SNMP']);
      traces.push({ id: 't' + traces.length, a: router.id, b: s.id, kind: 'uplink', measured: true });
      infra.push(s);
    }
    for (let i = 0; i < nAp; i++) {
      const a = mk('core', 'ap', 'Access point ' + (i + 1), ['SNMP']);
      traces.push({ id: 't' + traces.length, a: infra[i % infra.length].id, b: a.id, kind: 'uplink', measured: true });
      infra.push(a);
    }
    const counter = {};
    while (comps.length < n) {
      const p = POOL[Math.floor(rnd() * POOL.length)];
      counter[p[2]] = (counter[p[2]] || 0) + 1;
      const title = counter[p[2]] > 1 ? p[2] + ' ' + counter[p[2]] : p[2];
      const c = mk(p[0], p[1], title, p[3]);
      const parent = (p[1] === 'phone' || p[1] === 'iot' || p[1] === 'camera') ? infra[infra.length - 1 - Math.floor(rnd() * nAp)] : infra[Math.floor(rnd() * nSw)];
      traces.push({ id: 't' + traces.length, a: parent.id, b: c.id, kind: 'lan', measured: rnd() < 0.8 });
    }
    // a few logical traces: services, integrations, flows
    const of = k => comps.filter(c => c.kind === k);
    const hubs = of('hub'), brokers = of('broker'), servers = of('server').concat(of('nas')), clients = of('computer').concat(of('self'));
    of('iot').concat(of('camera')).forEach((c, i) => { const h = hubs[i % Math.max(1, hubs.length)] || brokers[0]; if (h && rnd() < 0.7) traces.push({ id: 't' + traces.length, a: h.id, b: c.id, kind: 'integration', measured: rnd() < 0.6 }); });
    of('service').forEach((c, i) => { const s = servers[i % Math.max(1, servers.length)]; if (s) traces.push({ id: 't' + traces.length, a: s.id, b: c.id, kind: 'service', measured: rnd() < 0.5 }); });
    clients.forEach((c, i) => { const s = servers[i % Math.max(1, servers.length)]; if (s && rnd() < 0.8) traces.push({ id: 't' + traces.length, a: c.id, b: s.id, kind: 'flow', measured: true }); });
    if (brokers.length && hubs.length) traces.push({ id: 't' + traces.length, a: hubs[0].id, b: brokers[0].id, kind: 'integration', measured: true });
    // optional extra logical traces (dense boards for performance tests): flows between random endpoints
    if (opts.traces) {
      const ends = comps.filter(c => c.kind !== 'internet');
      let guard = 0;
      while (traces.length < opts.traces && guard++ < 10000) {
        const a = ends[Math.floor(rnd() * ends.length)], b = ends[Math.floor(rnd() * ends.length)];
        if (a === b || traces.some(t => (t.a === a.id && t.b === b.id) || (t.a === b.id && t.b === a.id))) continue;
        traces.push({ id: 't' + traces.length, a: a.id, b: b.id, kind: rnd() < 0.5 ? 'flow' : 'service', measured: rnd() < 0.7 });
      }
    }
    const zones = ZONES.map((z, i) => Object.assign({ order: i }, z, long ? { title: z.title + ' with a ridiculously long zone title for overflow tests' } : {}));
    return { view: 'overview', generated: 0, zones, components: comps, traces };
  }

  // Synthetic live frame for a view: dots per measured trace, rates, a few grade/metric changes
  function makeFrame(view, t, opts) {
    opts = opts || {};
    const rnd = rng(Math.floor(t * 1000) + 3);
    const scale = opts.scale || 1;
    const dots = [], rates = {}, grades = {}, metrics = {};
    view.traces.forEach(tr => {
      if (!tr.measured) return;
      const base = tr.kind === 'uplink' ? 30 : tr.kind === 'flow' ? 10 : 3;
      const ab = Math.max(0, Math.round(base * scale * rnd() * 1.6)), ba = Math.max(0, Math.round(base * scale * rnd() * 0.8));
      if (ab) dots.push([tr.id, 0, ab]);
      if (ba) dots.push([tr.id, 1, ba]);
      rates[tr.id] = [ab * 65536, ba * 65536];
    });
    view.components.forEach(c => {
      if (rnd() < 0.05) grades[c.id] = GRADES[Math.floor(rnd() * GRADES.length)];
      if (c.metric && rnd() < 0.3) metrics[c.id] = opts.long ? c.metric : fmtRate(Math.pow(10, 2 + rnd() * 5));
    });
    return { t, q: 65536, dots, rates, grades, metrics, overall: { grade: 'near', sources: [] } };
  }

  return { makeView, makeFrame, rng };
});
