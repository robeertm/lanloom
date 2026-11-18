/* Mode switch, device tags and network filter, network toggles of the board legend under Node: the pure helpers
   (lanloom/web/js/netmodes.js, lanloom/web/board/board.js). Prints one JSON object; tests/test_netmodes.py checks it. */
'use strict';
const path = require('path');
const N = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'js', 'netmodes.js'));
const B = require(path.join(__dirname, '..', '..', 'lanloom', 'web', 'board', 'board.js')).Lanloom.Board;

const dev = tags => ({ id: 'd', tags });
const devices = [
  dev({ vlan: 70, net: 'Home', ssid: 'Casa', band: '5ghz' }),
  dev({ vlan: 70, net: 'Home', ssid: null, band: null }),
  dev({ vlan: 20, net: 'Guests', ssid: 'Guest', band: '2.4ghz' }),
  dev({ vlan: 30, net: null, ssid: null, band: null }),
  dev({ vlan: null, net: 'Lab', ssid: 'Casa', band: '6ghz' }),
  dev(null),
  dev({ vlan: 70, net: 'Home', ssid: 'Casa', band: null })
];
const t = (k, v) => k + (v ? JSON.stringify(v) : '');
const z = (id, extra) => Object.assign({ id, kind: 'net', title: id, order: 1 }, extra);
const opts = N.netOptions(devices);

const out = {
  modes: N.MODES.map(m => [m[0], m[1]]),
  keys: devices.map(d => N.netKey(d.tags)),
  options: { nets: opts.nets.map(n => [n.value, n.name, n.vlan]), wireless: opts.wireless, wired: opts.wired },
  none: N.netOptions([dev(null), dev(null)]),
  match: ['', 'wired', 'net:Home', 'vlan:30', 'wireless:Casa', 'net:Lab', 'wireless:nope'].map(v => devices.map(d => N.matchNet(d, v) ? 1 : 0).join('')),
  clean: ['', 'wired', 'net:Home', 'net:Gone', 'wireless:Casa', 'wireless:Gone', 'vlan:30', 'vlan:99'].map(v => N.cleanNet(v, opts)),
  cleanNoWired: N.cleanNet('wired', N.netOptions([dev({ vlan: 1, net: 'A', ssid: 'x', band: null })])),
  chips: [
    { vlan: 70, net: 'Home', ssid: 'Casa', band: '5ghz' }, { vlan: null, net: 'Lab', ssid: null, band: null }, { vlan: 20, net: null, ssid: null, band: null },
    { vlan: 20, net: null, ssid: 'Guest', band: 'x' }, { vlan: 0, net: null, ssid: null, band: null }, null
  ].map(N.tagChips),
  label: [z('a', { vars: { name: ' Home ', vlan: 70 } }), z('b', { vars: { name: '', vlan: 20 } }), z('c', { vars: {} }), z('d', { title: 'Other network' }), z('e', { vars: { vlan: '' }, title: '' })].map(x => B.zoneLabel(x, t)),
  toggles: {
    one: B.zoneToggles([z('net:a')], null, t),
    none: B.zoneToggles(null, null, t),
    // only network zones, never the edge, the cabling, the guests of a host or a folded zone; order by the board order, then as given
    mixed: B.zoneToggles([z('edge', { kind: 'edge' }), z('net:b', { order: 2, vars: { name: 'B' } }), z('cabling', { kind: 'cabling' }), z('net:a', { order: 1, vars: { name: 'A' } }),
      z('guests:x', { kind: 'guests' }), z('net:f', { fold: 'x' }), z('net:c', { order: 2, vars: { vlan: 30 } })], null, t),
    active: B.zoneToggles([z('net:a'), z('net:b')], 'net:b', t).map(x => [x.id, x.on]),
    inactive: B.zoneToggles([z('net:a'), z('net:b')], '', t).map(x => [x.id, x.on]),
    other: B.zoneToggles([z('net:a'), z('net:b')], 'cabling', t).map(x => [x.id, x.on])
  }
};
process.stdout.write(JSON.stringify(out));
