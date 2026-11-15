"""Physical topology: which device hangs on which port of which switch, access point or host.

Pure functions, no I/O. The core feeds in the inventory, the manual "connected via" entries and the latest
``topology`` events of the live sources; ``resolve()`` returns one physical link per device.

Sources and their fixed order of precedence (the first one that knows a device wins):

1. ``manual``      - the "connected via" field on the component card;
2. ``controller``  - client and device lists of a switch / access point controller (read-only account);
3. ``lldp``        - LLDP neighbours read over SNMP (links between switches, access points, routers);
4. ``fdb``         - the forwarding database of a switch over SNMP (which hardware address was learned on which port).

Within one source the more specific observation wins: a port with one address beats a port shared by many, a
real port beats the "towards the gateway" inference; then the newer observation.
Every other observation of a device stays visible: ``also`` (sources that agree) and ``conflicts`` (sources that
say something else, with what they say). Unknown values stay ``None``; nothing is guessed.

After the winners are chosen:
* a link that would close a loop is skipped (the next candidate of that device is tried);
* devices that share one port of a parent are refined: when exactly one of them is infrastructure (switch,
  access point) the others are "behind" it; when the others carry hardware addresses of virtual network cards and
  exactly one does not, they are guests of that host (medium ``virtual``); a port with very many addresses and no
  infrastructure on it is a trunk and is not assigned from the forwarding database.
"""
import re
import time

ORDER = ("manual", "controller", "lldp", "fdb")
MEDIA = ("wired", "radio", "virtual")
BANDS = ("2.4ghz", "5ghz", "6ghz")
INFRA_KINDS = ("router", "switch", "ap")
STALE_S = 1800.0          # observations older than this are ignored (sources refresh every minute)
TRUNK_MACS = 12           # a forwarding-database port with this many addresses and no infrastructure is a trunk
PORT_MAX = 24             # length limit of a port label
KIND_RANK = {"router": 0, "switch": 1, "ap": 2}

# First three octets of hardware addresses that hypervisors and container engines hand to virtual network cards.
# identifier: virtual NIC address prefixes (publicly registered blocks), no vendor names on purpose
VIRTUAL_PREFIXES = ("52:54:00", "bc:24:11", "00:16:3e", "00:0c:29", "00:50:56", "00:05:69", "00:15:5d", "08:00:27",
                    "00:1c:42", "02:42:")

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def norm_mac(value) -> str:
    text = str(value or "").strip().lower().replace("-", ":")
    if re.fullmatch(r"[0-9a-f]{12}", text):
        text = ":".join(text[i:i + 2] for i in range(0, 12, 2))
    return text if _MAC_RE.match(text) and text != "00:00:00:00:00:00" else ""


def is_virtual_mac(mac: str) -> bool:
    mac = norm_mac(mac)
    return bool(mac) and mac.startswith(VIRTUAL_PREFIXES)


def clean_port(value):
    if value is None:
        return None
    text = " ".join(str(value).split())[:PORT_MAX]
    return text or None


def port_key(port):
    """Natural sort key for port labels: 'Port 2' < 'Port 10', numbers before names."""
    if port is None:
        return (2, ())
    parts = re.split(r"(\d+)", str(port))
    return (0 if parts and parts[0] == "" else 1, tuple((0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts if p))


class _Cand:
    __slots__ = ("child", "parent", "port", "own_port", "medium", "band", "ssid", "source", "t", "shared", "weak",
                 "reason", "vars")

    def __init__(self, child, parent, source, t, port=None, own_port=None, medium=None, band=None, ssid=None,
                 shared=1, weak=False, reason=None, vars=None):
        self.child, self.parent, self.source, self.t = child, parent, source, float(t or 0)
        self.port, self.own_port = clean_port(port), clean_port(own_port)
        self.medium = medium if medium in MEDIA else None
        self.band = band if band in BANDS else None
        self.ssid = (str(ssid)[:32] or None) if ssid else None
        self.shared, self.weak, self.reason, self.vars = max(1, int(shared or 1)), bool(weak), reason, vars or {}

    def key(self):
        return (ORDER.index(self.source), self.weak, self.shared, -self.t)

    def view(self, now):
        return {"source": self.source, "parent": self.parent, "port": self.port, "own_port": self.own_port,
                "medium": self.medium, "age_s": None if self.source == "manual" else round(max(0.0, now - self.t), 1)}


class _Index:
    """Hardware address / address / name -> device id, plus virtual nodes for infrastructure outside the inventory."""

    def __init__(self, devices):
        self.devices = {d["id"]: d for d in devices}
        self.by_mac, self.by_ip, self.by_name = {}, {}, {}
        for d in devices:
            mac = norm_mac(d.get("mac"))
            if mac:
                self.by_mac[mac] = d["id"]
            if d.get("ip"):
                self.by_ip.setdefault(d["ip"], d["id"])
            for name in [d.get("title")] + list(d.get("hostnames") or []):
                n = str(name or "").strip().lower().split(".")[0]
                if n:
                    self.by_name.setdefault(n, d["id"])
        self.gateway = next((d["id"] for d in devices if d.get("is_gateway")), None)
        self.nodes = {}        # virtual id -> node
        self.infra = {i for i, d in self.devices.items() if d.get("kind") in INFRA_KINDS}

    def kind(self, node_id):
        if node_id in self.devices:
            return self.devices[node_id].get("kind")
        return (self.nodes.get(node_id) or {}).get("kind")

    def find(self, mac=None, ip=None, name=None):
        m = norm_mac(mac)
        if m and m in self.by_mac:
            return self.by_mac[m]
        if ip and ip in self.by_ip:
            return self.by_ip[ip]
        n = str(name or "").strip().lower().split(".")[0]
        if n and n in self.by_name:
            return self.by_name[n]
        return None

    def virtual(self, prefix, key, kind, title, mac=None, ip=None, source=None):
        vid = "%s:%s" % (prefix, re.sub(r"[^0-9a-zA-Z_.-]", "", str(key))[:40])
        node = self.nodes.get(vid)
        if not node:
            node = self.nodes[vid] = {"id": vid, "kind": kind if kind in INFRA_KINDS else "unknown", "title": title or "",
                                      "mac": norm_mac(mac) or None, "ip": ip or None, "source": source}
        if node["kind"] in INFRA_KINDS:
            self.infra.add(vid)
        return vid


# ---------------------------------------------------------------------------------------------- observations
def _from_controller(ix, ev, t, cands):
    nodes = []
    for d in ev.get("devices") or []:                            # first pass: every device gets its node (and kind)
        mac = norm_mac(d.get("mac"))
        if not mac:
            continue
        role = d.get("role") if d.get("role") in INFRA_KINDS else None
        node = ix.find(mac=mac, ip=d.get("ip"))
        if not node and role == "router" and ix.gateway:
            node = ix.gateway                                   # the controller's router is the network's gateway
        if not node and role:
            node = ix.virtual("ctl", mac.replace(":", ""), role, d.get("name"), mac=mac, ip=d.get("ip"), source="controller")
        if not node:
            continue
        if role:
            ix.infra.add(node)
        ix.by_mac.setdefault(mac, node)
        nodes.append((node, role, d.get("uplink") or None))
    for node, role, up in nodes:                                 # second pass: device uplinks
        if role == "router" or node == ix.gateway or not up or not norm_mac(up.get("mac")):
            continue                                             # the router's own uplink leads out of the network
        pmac = norm_mac(up["mac"])
        parent = ix.find(mac=pmac) or ix.virtual("ctl", pmac.replace(":", ""), "switch", None, mac=pmac, source="controller")
        cands.append(_Cand(node, parent, "controller", t, port=up.get("port"), own_port=up.get("own_port"),
                           medium=up.get("medium")))
    for c in ev.get("clients") or []:
        child = ix.find(mac=c.get("mac"), ip=c.get("ip"))
        pmac = norm_mac(c.get("parent_mac"))
        if not child or not pmac:
            continue
        parent = ix.find(mac=pmac) or ix.virtual("ctl", pmac.replace(":", ""), "ap" if c.get("medium") == "radio" else "switch",
                                                  None, mac=pmac, source="controller")
        cands.append(_Cand(child, parent, "controller", t, port=c.get("port"), medium=c.get("medium"),
                           band=c.get("band"), ssid=c.get("ssid")))


def _agent_node(ix, ev, integ_id):
    agent = ev.get("agent") or {}
    node = ix.find(mac=agent.get("mac"), ip=agent.get("ip") or ev.get("host"))
    if not node:
        node = "snmp:%s" % integ_id
        ix.nodes.setdefault(node, {"id": node, "kind": "switch", "title": agent.get("name") or "", "mac": norm_mac(agent.get("mac")) or None,
                                   "ip": agent.get("ip") or ev.get("host"), "source": "snmp", "integration": integ_id})
    ix.infra.add(node)
    return node


def _lldp_edges(ix, ev, integ_id, t, edges):
    agent = _agent_node(ix, ev, integ_id)
    trunk_ports = set()
    for n in ev.get("lldp") or []:
        caps = set(n.get("caps") or [])
        kind = "ap" if "ap" in caps else "router" if "router" in caps and "bridge" not in caps else "switch" if caps & {"bridge", "repeater"} else None
        node = ix.find(mac=n.get("chassis_mac") or n.get("port_mac"), ip=n.get("mgmt_ip") or n.get("chassis_ip"), name=n.get("sys_name"))
        if not node and kind:
            key = norm_mac(n.get("chassis_mac")).replace(":", "") or n.get("sys_name") or n.get("chassis_text") or n.get("mgmt_ip")
            if key:
                node = ix.virtual("lldp", key, kind, n.get("sys_name"), mac=n.get("chassis_mac"), ip=n.get("mgmt_ip"), source="lldp")
        if not node or node == agent:
            continue
        if kind:
            ix.infra.add(node)
        trunk_ports.add(n.get("local_port"))
        remote_port = n.get("port_desc") if n.get("port_mac") or not n.get("port_id") else n.get("port_id")
        edges.append({"a": agent, "a_port": n.get("local_port"), "b": node, "b_port": remote_port, "t": t})
    return trunk_ports


def _from_fdb(ix, ev, integ_id, t, cands, skip_ports, trunks, unknown):
    agent = _agent_node(ix, ev, integ_id)
    own = norm_mac((ev.get("agent") or {}).get("mac"))
    gw_mac = norm_mac((ix.devices.get(ix.gateway) or {}).get("mac")) if ix.gateway else ""
    ports = {}
    for row in ev.get("fdb") or []:
        mac = norm_mac(row.get("mac"))
        port = clean_port(row.get("port"))
        if not mac or not port or mac == own:
            continue
        ports.setdefault(port, set()).add(mac)
    for port, macs in sorted(ports.items(), key=lambda kv: port_key(kv[0])):
        if gw_mac and gw_mac in macs and agent != ix.gateway:
            # towards the gateway: this is the agent's own uplink port; nothing behind it is a direct neighbour
            cands.append(_Cand(agent, ix.gateway, "fdb", t, own_port=port, medium="wired", weak=True))
            continue
        if port in skip_ports:
            continue                                            # LLDP neighbour on it: everything behind belongs to that neighbour
        known = [ix.find(mac=m) for m in sorted(macs)]
        n_unknown = sum(1 for k in known if not k)
        known = [k for k in known if k and k != agent]
        if n_unknown:
            unknown[(agent, port)] = unknown.get((agent, port), 0) + n_unknown
        for child in known:
            cands.append(_Cand(child, agent, "fdb", t, port=port, shared=len(macs)))
        if len(macs) >= TRUNK_MACS:
            trunks.append({"parent": agent, "port": port, "macs": len(macs)})


def _orient(ix, edges, cands, t_now):
    """LLDP links have no direction: the end nearer to the gateway (fewer hops) is the parent."""
    adj = {}
    for e in edges:
        adj.setdefault(e["a"], set()).add(e["b"])
        adj.setdefault(e["b"], set()).add(e["a"])
    for c in cands:
        adj.setdefault(c.child, set()).add(c.parent)
        adj.setdefault(c.parent, set()).add(c.child)
    depth = {}
    if ix.gateway:
        depth[ix.gateway] = 0
        queue = [ix.gateway]
        while queue:
            nxt = []
            for node in queue:
                for peer in sorted(adj.get(node, ())):
                    if peer not in depth:
                        depth[peer] = depth[node] + 1
                        nxt.append(peer)
            queue = nxt
    seen = set()
    for e in edges:
        a, b = e["a"], e["b"]
        da, db = depth.get(a), depth.get(b)
        if da is not None and db is not None and da != db:
            a_parent = da < db
        elif (da is None) != (db is None):
            a_parent = da is not None
        else:
            ra, rb = KIND_RANK.get(ix.kind(a), 3), KIND_RANK.get(ix.kind(b), 3)
            a_parent = ra <= rb                                  # equal rank: the reporting agent is the parent
        parent, child = (a, b) if a_parent else (b, a)
        p_port, c_port = (e["a_port"], e["b_port"]) if a_parent else (e["b_port"], e["a_port"])
        key = (child, parent)
        if key in seen:                                          # reported from both sides: fill in what is missing
            for c in cands:
                if c.source == "lldp" and c.child == child and c.parent == parent:
                    c.port = c.port or clean_port(p_port)
                    c.own_port = c.own_port or clean_port(c_port)
            continue
        seen.add(key)
        cands.append(_Cand(child, parent, "lldp", e["t"], port=p_port, own_port=c_port, medium="wired"))


# ---------------------------------------------------------------------------------------------- resolution
def resolve(devices, manual=None, observations=None, now=None) -> dict:
    """devices: inventory rows (id, ip, mac, kind, title, hostnames, is_gateway); manual: {child: {parent, port, medium, t}};
    observations: {key: {"integ": id, "ev": topology event}}. Returns links, virtual nodes, children, trunks."""
    now = time.time() if now is None else now
    ix = _Index(devices)
    cands, edges, trunks, unknown = [], [], [], {}
    for child, m in sorted((manual or {}).items()):
        if child in ix.devices and m.get("parent"):
            cands.append(_Cand(child, m["parent"], "manual", m.get("t") or now, port=m.get("port"), medium=m.get("medium") or "wired"))
    obs = sorted((observations or {}).items())
    fresh = [(k, o) for k, o in obs if o.get("ev") and now - float(o["ev"].get("t") or 0) <= STALE_S]
    for _, o in fresh:
        if o["ev"].get("source") == "controller":
            _from_controller(ix, o["ev"], o["ev"].get("t"), cands)
    lldp_ports = {}
    for _, o in fresh:
        if o["ev"].get("source") == "snmp":
            lldp_ports[o.get("integ")] = _lldp_edges(ix, o["ev"], o.get("integ"), o["ev"].get("t"), edges)
    for _, o in fresh:
        if o["ev"].get("source") == "snmp":
            _from_fdb(ix, o["ev"], o.get("integ"), o["ev"].get("t"), cands, lldp_ports.get(o.get("integ"), set()), trunks, unknown)
    _orient(ix, edges, cands, now)
    for m in (manual or {}).values():                            # a manual parent is infrastructure as well
        if m.get("parent") and m.get("medium") != "virtual":
            ix.infra.add(m["parent"])

    by_child = {}
    for c in cands:
        if c.child != c.parent and (c.parent in ix.devices or c.parent in ix.nodes):
            by_child.setdefault(c.child, []).append(c)
    for lst in by_child.values():
        lst.sort(key=_Cand.key)

    parent_of, chosen, notes = {}, {}, {}

    def loops(child, parent):
        node, guard = parent, 0
        while node is not None and guard < 10000:
            if node == child:
                return True
            node, guard = parent_of.get(node), guard + 1
        return False

    for child in sorted(by_child, key=lambda c: (by_child[c][0].key(), c)):
        for cand in by_child[child]:
            if loops(child, cand.parent):
                notes.setdefault(child, []).append(cand)
                continue
            parent_of[child] = cand.parent
            chosen[child] = cand
            break

    for child, c in chosen.items():                              # agreeing sources fill in what the winner does not know
        for o in by_child.get(child, []):
            if o is not c and o.parent == c.parent and _agrees(o, c):
                c.port, c.own_port = c.port or o.port, c.own_port or o.own_port
                if c.medium in (None, o.medium):
                    c.medium = o.medium
                    c.band, c.ssid = c.band or o.band, c.ssid or o.ssid

    _refine_shared(ix, chosen, parent_of, trunks)
    _default_radio(ix, chosen)

    links = {}
    for child, c in chosen.items():
        others = by_child.get(child, [])
        also = sorted({o.source for o in others if o is not c and o.source != c.source and _agrees(o, c)}, key=ORDER.index)
        conflicts = [o.view(now) for o in others if o is not c and o.source != c.source and not _agrees(o, c)]
        seen_src, uniq = set(), []
        for v in conflicts:
            if v["source"] not in seen_src:
                seen_src.add(v["source"])
                uniq.append(v)
        links[child] = {"child": child, "parent": c.parent, "port": c.port, "own_port": c.own_port, "medium": c.medium,
                        "band": c.band, "ssid": c.ssid, "source": c.source,
                        "age_s": None if c.source == "manual" else round(max(0.0, now - c.t), 1),
                        "reason": c.reason, "vars": dict(c.vars), "also": also, "conflicts": uniq,
                        "loop": [o.view(now) for o in notes.get(child, [])]}
    children = {}
    for child, l in links.items():
        children.setdefault(l["parent"], []).append(child)
    for p in children:
        children[p].sort(key=lambda c: (port_key(links[c]["port"]), links[c]["medium"] or "", c))
    used = set(links) | {l["parent"] for l in links.values()}
    nodes = {vid: n for vid, n in ix.nodes.items() if vid in used}
    sig = repr(sorted((c, l["parent"], l["port"], l["own_port"], l["medium"], l["source"], l["reason"]) for c, l in links.items()))
    return {"links": links, "nodes": nodes, "children": children, "trunks": trunks,
            "unknown": [{"parent": p, "port": port, "count": n} for (p, port), n in sorted(unknown.items(), key=lambda kv: (kv[0][0], port_key(kv[0][1])))],
            "infra": sorted(ix.infra & used), "signature": sig}


def _agrees(o, c) -> bool:
    if o.parent != c.parent:
        return False
    return not (o.port and c.port and o.port != c.port)


def _refine_shared(ix, chosen, parent_of, trunks):
    groups = {}
    for child, c in chosen.items():
        if c.port is not None:
            groups.setdefault((c.parent, c.port), []).append(child)
    has_children = set(parent_of.values())
    trunk_ports = {(t["parent"], t["port"]) for t in trunks}
    for (parent, port), members in sorted(groups.items(), key=lambda kv: (kv[0][0], port_key(kv[0][1]))):
        if len(members) < 2:
            continue
        infra = [m for m in members if m in ix.infra or m in has_children]
        macs = {m: norm_mac((ix.devices.get(m) or {}).get("mac")) for m in members}
        hosts = [m for m in members if not is_virtual_mac(macs[m])]
        guests = [m for m in members if is_virtual_mac(macs[m]) and chosen[m].source != "manual"]
        manual = [m for m in members if chosen[m].source == "manual"]
        if len(infra) != 1 and guests and len(hosts) == 1:
            host = hosts[0]
            for m in guests:
                c = chosen[m]
                c.parent, c.port, c.own_port, c.medium, c.band, c.ssid = host, None, None, "virtual", None, None
                c.reason, c.vars = "topo.reason.guest", {"via": host, "port": port}
                parent_of[m] = host
            continue
        if not infra and len(manual) == 1:
            infra = manual                   # one cable per port: what else is learned there sits behind the device set by hand
        if len(infra) == 1:
            head = infra[0]
            for m in members:
                if m == head or chosen[m].source == "manual":
                    continue                                     # a manual entry is never moved
                c = chosen[m]
                c.parent, c.port, c.own_port = head, None, None
                if c.medium == "wired":
                    c.medium = None                              # behind an unmanaged device: the last hop is unknown
                c.reason, c.vars = "topo.reason.behind", {"via": head, "port": port}
                parent_of[m] = head
            continue
        members = [m for m in members if chosen[m].source != "manual"]
        if len(members) < 2:
            continue
        if (parent, port) in trunk_ports and not infra and all(chosen[m].source == "fdb" for m in members):
            for m in members:
                del chosen[m]
                parent_of.pop(m, None)
            continue
        for m in members:
            c = chosen[m]
            if not c.reason:
                c.reason, c.vars = "topo.reason.shared", {"n": len(members) - 1, "port": port}


def _default_radio(ix, chosen):
    """A client of an access point whose link type nobody knows is assumed to be wireless (an access point has no
    cable ports for clients to hang on). Only a link type that is unknown is filled in: a source that said cable,
    wireless or virtual, and every manual entry, keep their word. Switches and routers behind an access point are not
    clients and stay unknown."""
    for child, c in chosen.items():
        if c.medium is not None or c.source == "manual" or ix.kind(c.parent) != "ap":
            continue
        if ix.kind(child) in ("router", "switch"):
            continue
        c.medium = "radio"
        c.reason = "topo.reason.behind_ap" if c.reason == "topo.reason.behind" else (c.reason or "topo.reason.ap_default")


def subtree(children: dict, root: str) -> set:
    """All descendants of root (for the manual editor: a device cannot hang below itself)."""
    out, stack = set(), [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out
