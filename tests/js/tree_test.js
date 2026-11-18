/* Tree view (pure part): the chain Internet → gateway → switch → access point → devices, one parent per node, filter keeps the way up. */
'use strict';
const Tree = require('../../lanloom/web/board/tree.js');
const comp = (id, kind, title, extra) => Object.assign({ id, kind, title, subtitle: '', status: 'up', grade: 'realtime', metric: '', zone: 'core' }, extra || {});
const view = {
  components: [
    comp('net', 'internet', 'Internet', { zone: 'edge' }), comp('gw', 'router', 'Gateway'), comp('sw', 'switch', 'Switch'), comp('ap', 'ap', 'Access point'),
    comp('pc', 'pc', 'Desk PC', { zone: 'clients' }), comp('ph', 'phone', 'Phone', { zone: 'mobile', status: 'down' }), comp('cam', 'camera', 'Camera', { grade: 'interrupted' }),
    comp('lone', 'iot', 'Lonely sensor', { zone: 'automation' }), comp('p10', 'pc', 'Port 10 device'), comp('p2', 'pc', 'Port 2 device')
  ],
  traces: [
    { id: 'u:gw', a: 'net', b: 'gw', kind: 'uplink' },
    { id: 'p:1', a: 'gw', b: 'sw', kind: 'lan', phys: { port: '1', own_port: 'uplink' } },
    { id: 'p:2', a: 'sw', b: 'ap', kind: 'lan', phys: { port: '4' } },
    { id: 'p:3', a: 'sw', b: 'pc', kind: 'lan', phys: { port: '5' } },
    { id: 'p:4', a: 'ap', b: 'ph', kind: 'radio', phys: { band: '5', ssid: 'Home' } },
    { id: 'p:5', a: 'sw', b: 'cam', kind: 'lan', phys: { port: '6' } },
    { id: 'p:6', a: 'sw', b: 'p10', kind: 'lan', phys: { port: '10' } },
    { id: 'p:7', a: 'sw', b: 'p2', kind: 'lan', phys: { port: '2' } },
    { id: 'p:8', a: 'cam', b: 'sw', kind: 'lan', phys: { port: '1' } },       // would close a loop: dropped
    { id: 'flow:1', a: 'pc', b: 'lone', kind: 'flow' }                          // not a physical link: ignored
  ]
};
const out = {};
const b = Tree.build(view, {});
out.rootIds = b.roots.map(n => n.id);
out.depthIds = [];
(function walk(n) { out.depthIds.push(n.depth + ':' + n.id); n.children.forEach(walk); })(b.roots[0]);
out.count = b.count;
out.problems = b.problems;
out.otherIds = b.other ? b.other.children.map(n => n.id) : [];
const sw = b.items.find(n => n.id === 'sw');
out.switchChildren = sw.children.map(n => n.id);                                // cable by port in natural order, then radio, 2 before 10
out.apChild = b.items.find(n => n.id === 'ph').link;
out.pcLink = b.items.find(n => n.id === 'pc').link;
out.swCount = sw.count;
const f = Tree.build(view, { only: 'problems' });
out.problemsOnly = f.items.filter(n => !n.group).map(n => n.id);
const z = Tree.build(view, { zone: 'mobile' });
out.zoneMobile = z.items.filter(n => !n.group).map(n => n.id);
out.empty = Tree.build({ components: [], traces: [] }, {}).count;
out.natural = ['10', '2', '1', 'a', '9'].sort(Tree.natural);
// a trace that names an unknown node does not break anything
out.broken = Tree.build({ components: [comp('a', 'router', 'A')], traces: [{ id: 'p:x', a: 'a', b: 'zzz', kind: 'lan' }, { id: 'p:y', a: 'a', b: 'a', kind: 'lan' }] }, {}).count;
console.log(JSON.stringify(out));
