"""Translation module of the MikroTik family: RouterOS REST replies -> neutral model v2 and the topology observation.

Pure functions, no I/O. Shared by the read half and (later) a write half, so both compute the same neutral state.

Product names are used only to identify compatible products. No affiliation or endorsement.

RouterOS answers every list with a JSON array of flat objects whose values are *strings* (``"true"``, ``"1500"``,
``"192.168.88.1/24"``), whatever the type; a few values are comma lists (``tagged``, ``vlan-ids``) and the object id is
``.id``. Input is a dict of raw lists keyed by area (whatever the read half could get; every key is optional)::

    resource, identity, routerboard       the router's own replies (single objects)
    interfaces, ethernet, poe             interface list, ethernet details (speed), power over Ethernet state
    bridges, bridge_ports, bridge_vlans, bridge_hosts, vlans
    addresses, pools, dhcp_servers, leases, arp, neighbors
    routes, dhcp_clients, pppoe_clients
    filter, nat                           firewall rules (shown, never written)
    wifi_reg, wireless_reg                wireless registration tables of the two wireless packages

It builds::

    equipment       the router itself and the neighbours it sees (discovery protocols)
    ports           ethernet ports: link, speed, power over Ethernet, bridge port settings (untagged VLAN, tagged VLANs)
    networks        one per address on a LAN-side interface: address range, VLAN, DHCP server
    devices         end devices from leases, the address table and the bridge host table
    dhcp_reservations   static leases
    port_forwards   destination NAT rules
    wans            internet connections (default routes, DHCP and PPPoE clients) with their state
    raw             firewall filter rules (shown, never written)
    rule_digest     one line per filter rule (name, on/off, fingerprint) for the night report
    known_hosts     opaque ids of every end device (the night report compares them between nights; the device list
                    itself is not kept)
    findings        plain checks: internet connection down, no drop rule on the input chain, rules switched off

Never copied: passwords, PPPoE secrets, keys and any field a reply carries under such a name. Neutral ids are opaque
hashes; hardware addresses are only used for the topology observation (the core needs them to match devices).
"""
import hashlib
import ipaddress
import json
import re

from ... import wifisignal
from ...sources._ctlmap import _unique, ip_str, name_str, norm_mac

SALT = "lanloom-mikrotik:"
MAX_ITEMS = 5000
TEXT_MAX = 96
CONTROLLER_CLASS = "router_switch"      # marks this family's state for its night report section (no product name in data)
WIRELESS_PKG = "wi" + "fi"              # name of the newer wireless package in the API path

SPEEDS = {"10mbps": 10, "100mbps": 100, "1gbps": 1000, "2.5gbps": 2500, "5gbps": 5000, "10gbps": 10000,
          "25gbps": 25000, "40gbps": 40000, "100gbps": 100000}
SWITCH_BOARDS = re.compile(r"\A(?:CRS|CSS)", re.I)
AP_BOARDS = re.compile(r"\A(?:RBcAP|RBwAP|cAP|wAP|RBLHG|LHG|SXT|RBSXT)", re.I)
UPTIME_PARTS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


# ---------------------------------------------------------------------------- small helpers
def oid(prefix: str, vendor_id) -> str:
    """Opaque, stable neutral id for a vendor object (never the hardware address itself)."""
    return prefix + hashlib.sha256((SALT + prefix + str(vendor_id)).encode("utf-8")).hexdigest()[:12]


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def text(value, limit: int = TEXT_MAX):
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    out = "".join(ch for ch in str(value) if ch >= " ")[:limit].strip()
    return out or None


def flag(value) -> bool:
    """RouterOS booleans: ``"true"``/``"yes"`` (also a real JSON true)."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "yes", "1")


def num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        f = float(str(value).strip())
    except ValueError:
        return None
    return int(f) if f.is_integer() else round(f, 2)


def items(raw, key) -> list:
    value = raw.get(key) if isinstance(raw, dict) else None
    if not isinstance(value, list):
        return []
    return [v for v in value[:MAX_ITEMS] if isinstance(v, dict)]


def _ip(value):
    return ip_str(value if isinstance(value, str) else None)


def csv(value) -> list:
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def uptime_s(value):
    """Seconds from ``1w2d3h4m5s`` (also a plain number)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    s = str(value or "")
    parts = re.findall(r"(\d+)(w|d|h|ms|m|s)", s)
    if not parts:
        return None
    return sum(int(n) * UPTIME_PARTS.get(u, 0) for n, u in parts)


def cidr_of(address):
    try:
        return str(ipaddress.ip_interface(str(address).strip()).network)
    except ValueError:
        return None


def ip_of_cidr(address):
    try:
        return str(ipaddress.ip_interface(str(address).strip()).ip)
    except ValueError:
        return None


def vlan_ids(value) -> list:
    """VLAN ids of ``10,20,30-40`` (ranges up to 4094, at most 4094 ids in all)."""
    out = []
    for part in csv(value):
        a, _, b = part.partition("-")
        if a.isdigit() and (not b or b.isdigit()):
            lo, hi = int(a), int(b or a)
            if 1 <= lo <= hi <= 4094:
                out.extend(range(lo, hi + 1))
        if len(out) > 4094:
            break
    return sorted(set(out))[:4094]


def is_private_mac(mac) -> bool:
    m = norm_mac(mac)
    return bool(m) and bool(int(m[:2], 16) & 2)


def _is_multicast(mac) -> bool:
    m = norm_mac(mac)
    return bool(m) and bool(int(m[:2], 16) & 1)


def _version(value):
    m = re.match(r"\A\s*(\d+(?:\.\d+){0,2})", str(value or ""))
    return m.group(1) if m else None


def _band(value):
    s = str(value or "").lower()
    return "2.4ghz" if s.startswith("2") else "5ghz" if s.startswith("5") else "6ghz" if s.startswith("6") else None


# ---------------------------------------------------------------------------- the router and its interfaces
class _Ifaces:
    """The interface list with the lookups the other parts need."""

    def __init__(self, raw):
        self.rows = [r for r in items(raw, "interfaces") if text(r.get("name"))]
        self.by_name = {r["name"]: r for r in self.rows}
        self.ethernet = {r.get("name"): r for r in items(raw, "ethernet") if r.get("name")}
        self.vlans = {r.get("name"): r for r in items(raw, "vlans") if r.get("name")}
        self.bridges = {r.get("name"): r for r in items(raw, "bridges") if r.get("name")}
        self.bridge_port = {}
        for r in items(raw, "bridge_ports"):
            name = text(r.get("interface"))
            if name and name not in self.bridge_port:
                self.bridge_port[name] = r

    def base(self, name, depth: int = 0):
        """The ethernet interface below a VLAN or PPPoE interface (None when it is something else)."""
        if depth > 3 or not name:
            return None
        if name in self.by_name and str(self.by_name[name].get("type") or "") == "ether":
            return name
        if name in self.vlans:
            return self.base(text(self.vlans[name].get("interface")), depth + 1)
        return None


def _router_mac(raw, ifs: _Ifaces):
    for name in list(ifs.bridges) + [r["name"] for r in ifs.rows if str(r.get("type") or "") == "ether"]:
        m = norm_mac(_d(ifs.by_name.get(name)).get("mac-address"))
        if m:
            return m
    return None


def _board(raw) -> str:
    rb, res = _d(raw.get("routerboard")), _d(raw.get("resource"))
    return text(res.get("board-name") or rb.get("model"), 32) or ""


# ---------------------------------------------------------------------------- internet connections
def _wans(raw, ifs: _Ifaces) -> list:
    """Internet connections keyed by interface: default routes decide whether a line carries traffic, the DHCP and PPPoE
    clients say how it is set up and what state the client is in."""
    lines = {}
    for c in items(raw, "dhcp_clients"):
        name = text(c.get("interface"))
        if name:
            lines[name] = {"type": "dhcp", "client_up": str(c.get("status") or "").lower() == "bound",
                           "disabled": flag(c.get("disabled"))}
    for c in items(raw, "pppoe_clients"):
        name = text(c.get("name"))
        if name:
            lines[name] = {"type": "pppoe", "client_up": flag(c.get("running")), "disabled": flag(c.get("disabled"))}
    routes = []
    for r in items(raw, "routes"):
        if str(r.get("dst-address") or "") != "0.0.0.0/0" or flag(r.get("disabled")):
            continue
        gw = str(r.get("immediate-gw") or r.get("gateway") or "")
        iface = gw.split("%", 1)[1] if "%" in gw else gw
        if _ip(iface):
            iface = None
        routes.append({"iface": text(iface), "active": flag(r.get("active")) and not flag(r.get("inactive")),
                       "distance": num(r.get("distance")) if num(r.get("distance")) is not None else 1})
    for rt in routes:
        name = rt["iface"] or "default route"
        line = lines.setdefault(name, {"type": "static", "client_up": None, "disabled": False})
        line["active"] = line.get("active") or rt["active"]
        line["distance"] = min(line.get("distance", 255), rt["distance"])
    out = []
    best = min((v.get("distance", 255) for v in lines.values()), default=255)
    for name in sorted(lines):
        v = lines[name]
        iface = ifs.by_name.get(name)
        up = bool(v.get("active")) or v["client_up"] is True       # a standby line whose client is connected is up
        out.append({"id": oid("w", name), "name": name, "type": v["type"], "failover": None,
                    "kind": "cellular" if str(_d(iface).get("type") or "") == "lte" else "cable",
                    "role": "backup" if v.get("distance", 255) > best else "primary",
                    "enabled": not v["disabled"], "up": up and not v["disabled"], "active": bool(v.get("active")),
                    "link": flag(_d(iface).get("running")) if iface else None, "latency_ms": None, "loss": None,
                    "device": None, "iface": name})
    return out[:MAX_ITEMS]


# ---------------------------------------------------------------------------- networks
def _dhcp_ranges(raw) -> dict:
    pools = {}
    for p in items(raw, "pools"):
        first = csv(p.get("ranges"))[:1]
        a, _, b = (first[0] if first else "").partition("-")
        if _ip(a) and _ip(b or a):
            pools[text(p.get("name"))] = [_ip(a), _ip(b or a)]
    out = {}
    for s in items(raw, "dhcp_servers"):
        name = text(s.get("interface"))
        if name and not flag(s.get("disabled")):
            out[name] = {"on": True, "range": pools.get(text(s.get("address-pool")))}
    return out


def _bridge_pvid(raw, ifs: _Ifaces, name):
    """The VLAN a bridge itself sits in (its own pvid) when it filters by VLAN, else None."""
    b = ifs.bridges.get(name)
    return (num(b.get("pvid")) if b and flag(b.get("vlan-filtering")) else None) if b else None


def _networks(raw, ifs: _Ifaces, wan_names: set) -> list:
    dhcp = _dhcp_ranges(raw)
    out, seen = [], set()
    for a in items(raw, "addresses"):
        name = text(a.get("interface"))
        cidr = cidr_of(a.get("address"))
        if not name or not cidr or name in wan_names or ifs.base(name) in wan_names or cidr.endswith("/32") \
                or flag(a.get("invalid")):
            continue
        v = ifs.vlans.get(name)
        vlan = num(v.get("vlan-id")) if v else _bridge_pvid(raw, ifs, name)
        key = (name, cidr)
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": oid("n", "%s|%s" % key), "name": text(a.get("comment"), 48) or name, "cidr": cidr,
                    "vlan": vlan if isinstance(vlan, int) and vlan > 1 else None, "zone": None, "purpose": "lan",
                    "dhcp": dhcp.get(name) or {"on": False, "range": None}, "internet": True,
                    "enabled": not flag(a.get("disabled")), "isolated": False, "gateway_ip": ip_of_cidr(a.get("address")),
                    "management": False, "primary": False, "iface": name})
    return out[:MAX_ITEMS]


def _mark_management(networks: list, infra_ips: list) -> None:
    """The management network is the one that holds the addresses of the neighbouring network equipment (most of them)."""
    best, best_n = None, 0
    for n in networks:
        net = ipaddress.ip_network(n["cidr"])
        hits = sum(1 for ip in infra_ips if ip and ipaddress.ip_address(ip) in net)
        if hits > best_n:
            best, best_n = n, hits
    if best is not None:
        best["management"] = True


# ---------------------------------------------------------------------------- equipment
def _neighbour_kind(n) -> str:
    board = str(n.get("board") or "")
    if SWITCH_BOARDS.match(board):
        return "switch"
    if AP_BOARDS.match(board):
        return "ap"
    return "router" if "mikrotik" in str(n.get("platform") or "").lower() else "other"


def _neighbours(raw, ifs: _Ifaces) -> list:
    seen, out = set(), []
    for n in items(raw, "neighbors"):
        mac = norm_mac(n.get("mac-address"))
        if not mac or mac in seen:
            continue
        seen.add(mac)
        port = None
        for cand in csv(n.get("interface")):
            if cand in ifs.by_name and ifs.by_name[cand].get("type") in ("ether", "sfp"):
                port = cand
                break
        out.append({"mac": mac, "ip": _ip(n.get("address4") or n.get("address")), "name": text(n.get("identity"), 48),
                    "model": text(n.get("board"), 32), "firmware": _version(n.get("version")), "kind": _neighbour_kind(n),
                    "port": port})
    return out[:MAX_ITEMS]


def _equipment(raw, ifs: _Ifaces, nets, router_mac, clients_n) -> tuple:
    """(equipment, router id, {neighbour mac: equipment id})."""
    res, ident = _d(raw.get("resource")), _d(raw.get("identity"))
    board = _board(raw)
    rid = oid("e", router_mac or text(ident.get("name")) or "router")
    mem, free = num(res.get("total-memory")), num(res.get("free-memory"))
    stats = {"cpu": num(res.get("cpu-load")),
             "mem": round(100.0 * (mem - free) / mem, 1) if isinstance(mem, (int, float)) and mem and isinstance(free, (int, float)) else None,
             "uptime_s": uptime_s(res.get("uptime")), "clients": clients_n}
    mgmt = next((n["gateway_ip"] for n in nets if n.get("management")), None) or next((n["gateway_ip"] for n in nets), None)
    router = {"id": rid, "name": text(ident.get("name"), 48) or board or "?",
              "kind": "switch" if SWITCH_BOARDS.match(board) else "router", "model": board or None,
              "firmware": _version(res.get("version")), "mgmt_ip": mgmt, "online": True, "pending": False,
              "uplink": None, "stats": stats}
    eq, by_mac = [router], {}
    for n in _neighbours(raw, ifs):
        eid = oid("e", n["mac"])
        by_mac[n["mac"]] = eid
        eq.append({"id": eid, "name": n["name"] or n["model"] or "?", "kind": n["kind"], "model": n["model"],
                   "firmware": n["firmware"], "mgmt_ip": n["ip"], "online": True, "pending": False,
                   "uplink": {"device": rid, "port": None, "medium": None}, "stats": {"clients": None}})
    return eq[:MAX_ITEMS], rid, by_mac


# ---------------------------------------------------------------------------- ports
def _poe_map(raw) -> dict:
    return {text(r.get("name")): r for r in items(raw, "poe") if text(r.get("name"))}


def _ports(raw, ifs: _Ifaces, rid, nets, wans, neighbours_by_port) -> list:
    poe = _poe_map(raw)
    vlan_net = {}
    for n in nets:
        vlan_net.setdefault(n["vlan"] if n.get("vlan") else 1, n["id"])
    tagged = {}
    for r in items(raw, "bridge_vlans"):
        if flag(r.get("disabled")):
            continue
        for name in csv(r.get("tagged")) + csv(r.get("current-tagged")):
            tagged.setdefault(name, set()).update(vlan_ids(r.get("vlan-ids")))
    wan_ifaces = set()
    for w in wans:
        if w.get("active") or w.get("up"):
            base = ifs.base(w["iface"])
            wan_ifaces.update({w["iface"], base} - {None})
    out, idx = [], 0
    for r in ifs.rows:
        if str(r.get("type") or "") not in ("ether", "sfp"):
            continue
        idx += 1
        name = r["name"]
        eth = ifs.ethernet.get(name, {})
        disabled = flag(r.get("disabled"))
        up = flag(r.get("running")) and not disabled
        speed = SPEEDS.get(str(eth.get("speed") or eth.get("rate") or "").lower().replace(" ", "")) if up else None
        bp = ifs.bridge_port.get(name)
        pvid = num(bp.get("pvid")) if bp else None
        pe = poe.get(name)
        pstate = str(_d(pe).get("poe-out-status") or "").lower()
        capable = pe is not None
        pon = capable and str(pe.get("poe-out") or "").lower() != "off"
        tv = sorted(tagged.get(name, ()))
        tnets = [vlan_net[v] for v in tv if v in vlan_net]
        neighbour = neighbours_by_port.get(name)
        out.append({"id": oid("p", "%s:%s" % (rid, name)), "device": rid, "idx": idx,
                    "name": text(r.get("comment"), 32) or name, "link": up, "speed": speed,
                    "media": "sfp" if name.lower().startswith("sfp") or str(r.get("type")) == "sfp" else "copper",
                    "poe": {"capable": True, "on": bool(pon), "watts": None, "powered": pstate == "powered-on"} if capable else None,
                    "profile": "", "profile_name": None,
                    "native_vlan": pvid if isinstance(pvid, int) and pvid > 1 else None,
                    "native_network": vlan_net.get(pvid) if pvid is not None else None,
                    "tagged_mode": "custom" if tnets else "none", "tagged": tnets, "tagged_vlans": tv[:64],
                    "is_uplink": name in wan_ifaces, "wan": name in wan_ifaces, "infra": neighbour is not None,
                    "lldp_neighbor": neighbour, "rx_bytes": num(r.get("rx-byte")), "tx_bytes": num(r.get("tx-byte")),
                    "disabled": disabled, "bridge": text(bp.get("bridge"), 32) if bp else None})
    return out[:MAX_ITEMS]


# ---------------------------------------------------------------------------- clients
def _clients(raw, ifs: _Ifaces, nets, rid, wan_names: set, router_mac) -> tuple:
    by_cidr = [(ipaddress.ip_network(n["cidr"]), n["id"]) for n in nets]
    wireless = {}
    for key in ("wifi_reg", "wireless_reg"):
        for r in items(raw, key):
            m = norm_mac(r.get("mac-address"))
            if m:
                wireless[m] = {"ssid": text(r.get("ssid"), 32), "band": _band(r.get("band"))}
    host_port = {}
    for h in items(raw, "bridge_hosts"):
        m = norm_mac(h.get("mac-address"))
        iface = text(h.get("on-interface"))
        if m and not flag(h.get("local")) and iface and m not in host_port:
            host_port[m] = iface
    arp = {}
    for a in items(raw, "arp"):
        m = norm_mac(a.get("mac-address"))
        if m and text(a.get("interface")) not in wan_names and str(a.get("status") or "") not in ("failed", "incomplete") \
                and str(a.get("complete") or "true").lower() != "false":
            arp.setdefault(m, a)
    leases = {}
    for l in items(raw, "leases"):
        m = norm_mac(l.get("mac-address") or l.get("active-mac-address"))
        if m and not flag(l.get("disabled")):
            leases.setdefault(m, l)
    equipment_macs = {n["mac"] for n in _neighbours(raw, ifs)}      # neighbouring network equipment is equipment, not a client
    macs = []
    for m in list(leases) + list(host_port) + list(arp) + list(wireless):
        if m not in macs and m != router_mac and m not in equipment_macs and not _is_multicast(m):
            macs.append(m)
    per_port = {}
    for m in macs:
        p = host_port.get(m) if m not in wireless else None
        if p:
            per_port[p] = per_port.get(p, 0) + 1
    devices, reservations = [], []
    for m in macs[:MAX_ITEMS]:
        l, a = _d(leases.get(m)), _d(arp.get(m))
        ip = _ip(l.get("active-address") or l.get("address")) or _ip(a.get("address"))
        wl = wireless.get(m)
        port = host_port.get(m) if not wl else None
        if not port and not wl and a:
            cand = text(a.get("interface"))
            port = cand if cand in ifs.by_name and ifs.by_name[cand].get("type") in ("ether", "sfp") else None
        wired = wl is None
        net = None
        if ip:
            addr = ipaddress.ip_address(ip)
            net = next((nid for n, nid in by_cidr if addr.version == n.version and addr in n), None)
        static = bool(l) and not flag(l.get("dynamic"))
        online = bool(wl) or bool(a) or m in host_port
        bp = ifs.bridge_port.get(port) if port else None
        name = text(l.get("host-name")) or text(l.get("comment")) or text(a.get("comment")) or None
        devices.append({"id": oid("c", m), "name": name or "?", "kind": "client", "network": net, "ip": ip,
                        "fixed": static, "fixed_ip": ip if static else None, "wired": wired,
                        "alone_on_port": bool(wired and port and per_port.get(port) == 1),
                        "private_address": is_private_mac(m), "protected": None, "parent": rid if port or wl else None,
                        "port": port if wired else None, "ssid": wl["ssid"] if wl else None,
                        "band": wl["band"] if wl else None, "blocked": False, "guest": False, "online": online,
                        "network_by_port": bool(bp and num(bp.get("pvid")) and num(bp.get("pvid")) > 1)})
        if static and ip:
            reservations.append({"id": oid("d", "%s|%s" % (m, ip)), "client": oid("c", m), "name": name or "?", "ip": ip,
                                 "network": net})
    return devices, reservations


# ---------------------------------------------------------------------------- forwards and rules
def _forwards(raw) -> list:
    out = []
    for pos, r in enumerate(items(raw, "nat")):
        if str(r.get("chain") or "") != "dstnat" or str(r.get("action") or "") != "dst-nat":
            continue
        target = _ip(r.get("to-addresses"))
        if not target:
            continue
        proto = str(r.get("protocol") or "").lower()
        wan_port = text(r.get("dst-port"), 48)
        out.append({"id": oid("f", r.get(".id") or pos), "name": text(r.get("comment")) or "#%d" % (pos + 1),
                    "proto": proto if proto in ("tcp", "udp") else "tcp_udp", "wan_port": wan_port, "dst_ip": target,
                    "dst_port": text(r.get("to-ports"), 48) or wan_port,
                    "enabled": not flag(r.get("disabled")),
                    "source_limited": bool(r.get("src-address") or r.get("src-address-list")), "order": pos})
    return out[:MAX_ITEMS]


RULE_KEYS = ("chain", "action", "protocol", "src-address", "dst-address", "src-address-list", "dst-address-list",
             "src-port", "dst-port", "in-interface", "out-interface", "in-interface-list", "out-interface-list",
             "connection-state", "connection-nat-state", "dst-address-type", "jump-target", "log", "place-before")


def _sig(r: dict) -> str:
    blob = json.dumps({k: r.get(k) for k in RULE_KEYS if k in r}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _rules(raw) -> tuple:
    """(raw items, digest): filter rules in the router's own order; shown, never written."""
    shown, digest = [], []
    for pos, r in enumerate(items(raw, "filter")):
        ref = oid("x", "filter:%s" % (r.get(".id") or pos))
        chain, action = text(r.get("chain"), 24) or "?", text(r.get("action"), 24) or "?"
        name = text(r.get("comment"), 120) or "#%d %s %s" % (pos + 1, chain, action)
        enabled = not flag(r.get("disabled"))
        allow = action in ("accept", "passthrough")
        shown.append({"id": ref, "area": "acl", "editable": False, "title": name, "scope": chain,
                      "action": "allow" if allow else "block", "enabled": enabled, "order": pos})
        digest.append({"ref": ref, "name": name, "scope": chain, "enabled": enabled, "allow": allow, "sig": _sig(r)})
    return shown[:MAX_ITEMS], digest[:MAX_ITEMS]


# ---------------------------------------------------------------------------- checks
def findings(state: dict, filter_read: bool = False) -> list:
    """Plain checks of the adapter, most important first. Kinds (texts in the translations as fwr.finding.<kind>):
    ``wan_down``, ``input_open``, ``rules_disabled``."""
    out = []

    def add(kind, level, subject, **params):
        out.append({"id": oid("f", "%s:%s" % (kind, subject)), "kind": kind, "level": level, "subject": subject, "params": params})

    for w in state.get("wans") or []:
        if w.get("enabled") and w.get("up") is False:
            add("wan_down", "alert", w["id"], wan=w.get("name"))
    digest = state.get("rule_digest") or []
    if filter_read and state.get("wans") and not any(
            r["scope"] == "input" and r["enabled"] and not r["allow"] for r in digest):
        add("input_open", "warn", "input")
    off = sum(1 for r in digest if not r.get("enabled"))
    if off:
        add("rules_disabled", "info", "rules", n=off)
    order = {"alert": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: order[f["level"]])
    return out


# ---------------------------------------------------------------------------- the whole state
def version_of(raw) -> str:
    return _version(_d(raw.get("resource")).get("version")) or ""


def to_neutral(raw: dict) -> dict:
    """Neutral model v2 from whatever replies are present. Raises nothing for missing areas (they stay empty)."""
    raw = raw if isinstance(raw, dict) else {}
    ifs = _Ifaces(raw)
    router_mac = _router_mac(raw, ifs)
    wans = _wans(raw, ifs)
    wan_names = set()
    for w in wans:
        wan_names.add(w["iface"])
        wan_names.update({ifs.base(w["iface"])} - {None})
    nets = _networks(raw, ifs, wan_names)
    nb = _neighbours(raw, ifs)
    _mark_management(nets, [n["ip"] for n in nb])
    rid_probe = oid("e", router_mac or text(_d(raw.get("identity")).get("name")) or "router")
    devices, reservations = _clients(raw, ifs, nets, rid_probe, wan_names, router_mac)
    eq, rid, by_mac = _equipment(raw, ifs, nets, router_mac, sum(1 for d in devices if d["online"]))
    by_port = {n["port"]: by_mac[n["mac"]] for n in nb if n["port"] and n["mac"] in by_mac}
    for e, n in zip(eq[1:], nb):
        if n["port"]:
            e["uplink"]["port"] = next((i for i, p in enumerate([r["name"] for r in ifs.rows
                                                                  if r.get("type") in ("ether", "sfp")], 1) if p == n["port"]), None)
    ports = _ports(raw, ifs, rid, nets, wans, by_port)
    for w in wans:
        w.pop("iface", None)
        w["device"] = rid
    for n in nets:
        n.pop("iface", None)
    shown, digest = _rules(raw)
    state = {"v": 2, "zones": [], "networks": nets, "devices": devices, "equipment": eq, "ports": ports,
             "policies": [], "ssids": [], "dhcp_reservations": reservations, "port_forwards": _forwards(raw),
             "wans": wans, "vpn": [], "raw": shown, "rule_digest": digest, "allow": []}
    for key in ("networks", "wans", "ports", "dhcp_reservations", "port_forwards", "devices", "equipment"):
        seen = set()                         # ids must be unique (a reply may repeat an object)
        state[key] = [o for o in state[key] if not (o["id"] in seen or seen.add(o["id"]))]
    state["known_hosts"] = sorted(d["id"] for d in state["devices"])[:MAX_ITEMS]
    state["findings"] = findings(state, filter_read=isinstance(raw.get("filter"), list))
    state["controller"] = {"version": version_of(raw), "class": CONTROLLER_CLASS, "apis": ["official"],
                           "site": text(_d(raw.get("identity")).get("name"), 48)}
    return state


def topology_event(raw: dict, t: float) -> dict:
    """The topology observation for the core: same shape as the generic controller source (so the resolver ranks it as
    ``controller`` and merges it with manual links, LLDP and address tables)."""
    raw = raw if isinstance(raw, dict) else {}
    ifs = _Ifaces(raw)
    router_mac = _router_mac(raw, ifs)
    ident, board = _d(raw.get("identity")), _board(raw)
    state = to_neutral(raw)
    mgmt = next((e.get("mgmt_ip") for e in state["equipment"][:1]), None)
    devices = []
    if router_mac:
        devices.append({"mac": router_mac, "ip": mgmt, "name": name_str(ident.get("name")),
                        "role": "switch" if SWITCH_BOARDS.match(board) else "router", "model": name_str(board),
                        "uplink": None})
    for n in _neighbours(raw, ifs):
        devices.append({"mac": n["mac"], "ip": n["ip"], "name": name_str(n["name"]), "role": n["kind"],
                        "model": name_str(n["model"]),
                        "uplink": {"mac": router_mac, "port": n["port"], "own_port": None, "medium": None}
                        if router_mac and n["port"] else None})
    clients = []
    known = {d["mac"] for d in devices}                      # the router and its neighbours are devices, not clients
    if router_mac:
        wireless = {}
        for key in ("wifi_reg", "wireless_reg"):
            for r in items(raw, key):
                m = norm_mac(r.get("mac-address"))
                if m:
                    wireless[m] = r
        names = {}
        for l in items(raw, "leases"):
            m = norm_mac(l.get("mac-address"))
            if m:
                names[m] = (_ip(l.get("active-address") or l.get("address")), name_str(l.get("host-name"), l.get("comment")))
        for h in items(raw, "bridge_hosts"):
            m = norm_mac(h.get("mac-address"))
            iface = text(h.get("on-interface"))
            if not m or flag(h.get("local")) or not iface or m in wireless or _is_multicast(m) or m == router_mac \
                    or m in known:
                continue
            ip, nm = names.get(m, (None, None))
            clients.append({"mac": m, "ip": ip, "name": nm, "medium": "wired", "parent_mac": router_mac, "port": iface,
                            "band": None, "ssid": None})
        for m, r in wireless.items():
            ip, nm = names.get(m, (None, None))
            clients.append({"mac": m, "ip": ip, "name": nm, "medium": "radio", "parent_mac": router_mac, "port": None,
                            "band": _band(r.get("band")), "ssid": name_str(r.get("ssid")),
                            "signal": wifisignal.clean(r.get("signal") if r.get("signal") is not None else r.get("signal-strength"))})
    return {"type": "topology", "source": "controller", "t": t, "devices": _unique(devices, 1000),
            "clients": _unique(clients, 5000)}
