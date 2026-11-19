"""Translation module of the OPNsense family: REST API replies -> neutral model v2 (and the rule fields both halves use).

Pure functions, no I/O. Shared by the read half and the write half, so both compute the same neutral state and the
same policy ids.

``to_neutral(raw)`` takes what the read half collected (every key optional; a release or a key without a privilege
simply leaves it out)::

    version        product version ("25.7.3") or ""
    interfaces     rows of /api/interfaces/overview/interfacesInfo    identifier, description, addr4, vlan_tag, gateways
    filter         the "filter" object of /api/firewall/filter/get    rules (and on older releases source NAT, NPT, 1:1)
    rules          rows of /api/firewall/filter/searchRule            only rows marked "legacy" are used (releases from 25.1
                                                                      list the classic interface rules there too)
    groups         rows of /api/firewall/group/searchItem              interface groups (zones of their own)
    aliases        the "alias" object of /api/firewall/alias/get       port aliases put rules in plain words
    vlans          rows of /api/interfaces/vlan_settings/searchItem
    snat           rows of /api/firewall/source_nat/searchRule
    dnat           rows of /api/firewall/d_nat/searchRule              port forwarding (releases that have the API)
    kea            rows of /api/kea/dhcpv4/searchReservation           fixed addresses
    dnsmasq        rows of /api/dnsmasq/settings/searchHost            fixed addresses (the other address service)
    routes         rows of /api/routes/routes/searchroute
    gateways       rows of /api/routes/gateway/status
    vpn_tunnel / vpn_ipsec / vpn_ssl   rows of the VPN instance lists (tunnel instances, IPsec connections, SSL VPN)

and builds::

    zones          every assigned interface, every interface group, and the firewall itself (gateway zone)
    networks       interfaces with an address -> network (zone = the interface)
    policies       one neutral policy per rule and zone pair: a rule on an interface (direction in) goes from that
                   interface's zone to the zone of its destination (an interface's network, the firewall itself, an
                   address inside a known network, or any)
    port_forwards  destination NAT rules;  dhcp_reservations  fixed addresses (the hardware address only hashed)
    wans           interfaces with a gateway;  vpn  tunnel, IPsec and SSL VPN instances
    raw            source NAT, NPT, 1:1 NAT, static routes, interface groups, VLANs (shown, never written)
    rule_digest    one line per rule (name, on/off, fingerprint) for the night report
    findings       plain checks (management open on a WAN interface, everything from a WAN interface inside, any to
                   any, rules switched off, a gateway that is down)

Never copied: keys, secrets, certificates, pre-shared keys, passwords of dial-up links, user lists. The replies may
carry such values (the private key of a VPN tunnel instance, a PPPoE password in an interface's settings); only the
fields named here are taken. Neutral ids are opaque hashes; a hardware address only ever appears hashed.
"""
import hashlib
import ipaddress
import re

from ...firewall import neutral

SALT = "lanloom-opn:"
OWN_MARK = "[lanloom]"           # description prefix of the rules the write half creates (only those may be deleted)
MAX_ITEMS = 5000
MAX_PAIRS = 24
TEXT_MAX = 96
ANY = "*"
SELF = "self"                     # the zone of the firewall itself ("This Firewall")
LEGACY_ORDER = 4_000_000          # classic interface rules come after the rules of the rule API
# evaluation order of the rule API: floating rules (no interface, several, or "not" these), then rules of an interface
# group, then rules of one interface; within each by sequence
PRIO_FLOATING, PRIO_GROUP, PRIO_IFACE = 1_000_000, 2_000_000, 3_000_000

UUID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
IFACE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
ACTIONS = {"pass": "allow", "block": "block", "reject": "reject"}
ACTIONS_OUT = {v: k for k, v in ACTIONS.items()}
PROTOS = {"any": "any", "tcp": "tcp", "udp": "udp", "tcp/udp": "tcp_udp", "icmp": "icmp"}
PROTOS_OUT = {"any": "any", "tcp": "TCP", "udp": "UDP", "tcp_udp": "TCP/UDP", "icmp": "ICMP"}
WELL_KNOWN = {"http": "80", "https": "443", "ssh": "22", "domain": "53", "dns": "53", "smtp": "25", "ntp": "123",
              "telnet": "23", "ftp": "21", "imap": "143", "imaps": "993", "pop3": "110", "pop3s": "995",
              "submission": "587", "snmp": "161", "ldap": "389", "ldaps": "636", "rdp": "3389", "ms-wbt-server": "3389",
              "mqtt": "1883", "ipsec-nat-t": "4500", "isakmp": "500"}
ADMIN_PORTS = {"22": "SSH", "80": "HTTP", "443": "HTTPS"}
WAN_WORDS = ("wan",)


# ---------------------------------------------------------------------------- small helpers
def oid(prefix: str, vendor_id) -> str:
    return prefix + hashlib.sha256((SALT + prefix + str(vendor_id)).encode("utf-8")).hexdigest()[:12]


def text(value, limit: int = TEXT_MAX):
    if value is None or isinstance(value, (dict, list)):
        return None
    out = "".join(ch for ch in str(value) if ch >= " ")[:limit].strip()
    return out or None


def values(v) -> list:
    """The selected keys of a field in the shape of ``.../get`` (``{key: {"value": label, "selected": 1}}``) or the
    items of a comma list (search rows, plain fields)."""
    if isinstance(v, dict):
        out = []
        for k, o in v.items():
            if isinstance(o, dict) and str(o.get("selected", "")) in ("1", "True", "true"):
                out.append(str(k))
        return out
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if v is None:
        return []
    return [x.strip() for x in str(v).split(",") if x.strip()]


def value(v) -> str:
    got = values(v)
    return got[0] if got else ""


def options(v) -> dict:
    """All options of a field in the ``.../get`` shape: {key: label}."""
    if not isinstance(v, dict):
        return {}
    return {str(k): text(o.get("value"), 48) or str(k) for k, o in v.items() if isinstance(o, dict) and "value" in o}


def flag(v) -> bool:
    return str(value(v) if isinstance(v, (dict, list)) else (v if v is not None else "")).strip().lower() in ("1", "yes", "true", "on")


def rows(raw, key) -> list:
    got = raw.get(key) if isinstance(raw, dict) else None
    if isinstance(got, dict) and isinstance(got.get("rows"), list):
        got = got["rows"]
    if isinstance(got, dict) and isinstance(got.get("items"), list):
        got = got["items"]
    return [r for r in (got or [])[:MAX_ITEMS] if isinstance(r, dict)] if isinstance(got, list) else []


def ip_of(value_):
    try:
        return str(ipaddress.ip_address(str(value_).strip()))
    except ValueError:
        return None


def iface_net(addr):
    """``192.168.1.1/24`` -> (address, network) or (None, None)."""
    try:
        i = ipaddress.ip_interface(str(addr or "").strip())
    except ValueError:
        return None, None
    if i.version != 4 or i.network.prefixlen >= 32:
        return str(i.ip), None
    return str(i.ip), str(i.network)


def mac_hash(mac):
    m = re.sub(r"[^0-9a-f]", "", str(mac or "").lower())
    if len(m) != 12:
        return None
    return hashlib.sha256((SALT + "mac:" + m).encode()).hexdigest()[:16]


def parse_version(v) -> tuple:
    """``OPNsense 25.7.3_1-amd64`` / ``25.7.3`` -> (25, 7) or () when unknown."""
    m = re.search(r"(?<!\d)(\d{2})\.(\d{1,2})(?:\.\d+)?", str(v or ""))
    return (int(m.group(1)), int(m.group(2))) if m else ()


def native_rollback(version) -> bool:
    """Does this release give the rule API its own way back (savepoint, apply with a 60 second timer, cancel)?
    Releases 23.x carry the rule API as a plug-in that has it, 24.1 up to 26.1 have it built in, 26.7 dropped it
    (the GUI never used it). Unknown releases: None - the write half then asks for a savepoint and takes the answer."""
    ver = parse_version(version)
    if not ver:
        return None
    return (22, 1) <= ver < (26, 7)


def zone_id(key) -> str:
    k = str(key or "").strip().lower()
    if k in ("", "any", ANY):
        return ANY
    if k in (SELF, "(self)"):
        return oid("z", SELF)
    return oid("z", k)


def net_id(ident) -> str:
    return oid("n", str(ident).lower())


def policy_id(uuid, src_zone, dst_zone) -> str:
    return oid("p", "%s|%s|%s" % (uuid, src_zone, dst_zone))


def ports_text(spec, aliases=None):
    """A destination port field in plain form: ``80``, ``8000-8080`` (OPNsense writes ranges ``8000:8080``), a well
    known name as its number, a port alias as its content; anything else is returned as it is (named, not split)."""
    s = str(spec or "").strip()
    if not s or s.lower() == "any":
        return ""
    out = []
    for part in s.split(","):
        part = part.strip()
        low = part.lower()
        if re.fullmatch(r"\d{1,5}", part):
            out.append(part)
        elif re.fullmatch(r"\d{1,5}[:-]\d{1,5}", part):
            out.append(re.sub(r"[:]", "-", part))
        elif low in WELL_KNOWN:
            out.append(WELL_KNOWN[low])
        elif aliases and part in aliases and aliases[part].get("type") == "port" and aliases[part].get("content"):
            got = ports_text(",".join(aliases[part]["content"]), None)
            if got is None:
                return None
            out.append(got)
        else:
            return None
    return ",".join(out)


def ports_out(ports: str) -> str:
    """Neutral ports -> the rule API's destination port (one port or one range ``lo:hi``); "" for any."""
    if not ports:
        return ""
    if "," in ports:
        raise ValueError("one port or one range")
    return ports.replace("-", ":")


# ---------------------------------------------------------------------------- interfaces, zones, networks
class Topology:
    """Interfaces, groups and networks with the lookups rules need (which zone does a destination fall into)."""

    def __init__(self, raw):
        self.ifaces = {}                    # identifier -> {name, addr, cidr, vlan, wan, gateways, enabled, link}
        for r in rows(raw, "interfaces"):
            ident = str(r.get("identifier") or (r.get("config") or {}).get("identifier") or "").strip().lower()
            if not IFACE_RE.match(ident):
                continue
            addr, cidr = iface_net(r.get("addr4") or "")
            if not addr:
                for a in r.get("ipv4") or []:
                    if isinstance(a, dict):
                        addr, cidr = iface_net(a.get("ipaddr"))
                        if addr:
                            break
            gws = [text(g, 48) for g in (r.get("gateways") or []) if text(g, 48)] if isinstance(r.get("gateways"), list) else []
            link = str(r.get("link_type") or "").strip().lower()
            vlan = r.get("vlan_tag")
            try:
                vlan = int(vlan) if vlan not in (None, "") and 1 <= int(vlan) <= 4094 else None
            except (TypeError, ValueError):
                vlan = None
            self.ifaces[ident] = {"name": text(r.get("description"), 48) or ident.upper(), "addr": addr, "cidr": cidr,
                                  "vlan": vlan, "gateways": gws, "link": link or None,
                                  "enabled": r.get("enabled") is not False and str(r.get("enabled")) not in ("0", "False"),
                                  "status": text(r.get("status"), 24)}
        self.groups = {}
        for g in rows(raw, "groups"):
            name = str(g.get("ifname") or "").strip()
            if IFACE_RE.match(name.lower()):
                self.groups[name.lower()] = {"name": text(name, 48), "members": [m.lower() for m in values(g.get("members"))],
                                             "description": text(g.get("descr"), 96)}
        # older releases without the overview: the interfaces the rule API offers (identifier -> name)
        flt = raw.get("filter") if isinstance(raw, dict) and isinstance(raw.get("filter"), dict) else {}
        for rule in _rule_map(flt).values():
            for k, label in options(rule.get("interface")).items():
                k = k.lower()
                if IFACE_RE.match(k) and k not in self.ifaces and k not in self.groups:
                    self.ifaces[k] = {"name": label, "addr": None, "cidr": None, "vlan": None, "gateways": [], "link": None,
                                      "enabled": True, "status": None}
            break
        for k in list(self.groups):
            if k in self.ifaces:
                del self.groups[k]
        for ident, i in self.ifaces.items():
            i["wan"] = bool(i["gateways"]) or ident.startswith(WAN_WORDS) or (i["link"] in ("dhcp", "pppoe", "pptp", "l2tp")
                                                                            and not (i["cidr"] and _private(i["cidr"])))
        self._nets = [(ipaddress.ip_network(i["cidr"]), ident) for ident, i in self.ifaces.items() if i["cidr"]]
        self._nets.sort(key=lambda t: -t[0].prefixlen)

    def ident(self, ref) -> str:
        """An interface or group reference as identifier; the classic rules name interfaces by their description."""
        low = str(ref or "").strip().lower()
        if low in self.ifaces or low in self.groups:
            return low
        for k, i in self.ifaces.items():
            if str(i["name"]).lower() == low:
                return k
        for k, g in self.groups.items():
            if str(g["name"]).lower() == low:
                return k
        return low

    def is_wan(self, ident) -> bool:
        return bool(self.ifaces.get(ident, {}).get("wan"))

    def zone_of_address(self, spec):
        """Zone key for an address or network written in a rule; None when it lies outside every known network."""
        try:
            net = ipaddress.ip_network(str(spec).strip(), strict=False)
        except ValueError:
            return None
        for n, ident in self._nets:
            if net.version == n.version and net.subnet_of(n):
                return ident
        if net.version == 4 and not net.is_private:
            wans = [k for k, i in self.ifaces.items() if i["wan"]]
            return wans[0] if len(wans) == 1 else None
        return None

    def dest_zone(self, item):
        """(zone key, match item or None) for one entry of a source/destination network field."""
        low = str(item).strip().lower()
        if low in ("", "any"):
            return ANY, None
        if low in ("(self)", "this firewall"):
            return SELF, None
        if low in self.ifaces or low in self.groups:
            return low, None                                  # the interface's network
        if low.endswith("ip") and low[:-2] in self.ifaces:
            return SELF, None                                 # the interface's own address = the firewall
        z = self.zone_of_address(item)
        if z is not None:
            return z, {"kind": "ip", "item": text(item, 48)}
        return ANY, {"kind": "ip", "item": text(item, 48)}


def _private(cidr) -> bool:
    try:
        return ipaddress.ip_network(cidr).is_private
    except ValueError:
        return False


def _zones(topo: Topology) -> list:
    out = []
    for ident, i in topo.ifaces.items():
        out.append({"id": zone_id(ident), "name": i["name"], "kind": ident, "system": "external" if i["wan"] else None,
                    "builtin": True, "interface": True, "admin_services": [], "description": None})
    for key, g in topo.groups.items():
        out.append({"id": zone_id(key), "name": g["name"], "kind": "group", "system": None, "builtin": False,
                    "interface": False, "members": [zone_id(m) for m in g["members"] if m in topo.ifaces],
                    "admin_services": [], "description": g["description"]})
    if out:
        out.append({"id": zone_id(SELF), "name": "This firewall", "kind": SELF, "system": "gateway", "builtin": True,
                    "interface": False, "admin_services": [], "description": None})
    return out


def _networks(topo: Topology) -> tuple:
    nets, wans = [], []
    for ident, i in topo.ifaces.items():
        if i["wan"]:
            wans.append({"id": net_id(ident), "name": i["name"], "type": i["link"], "failover": None,
                         "enabled": i["enabled"], "gateways": list(i["gateways"]), "status": i["status"]})
        if not i["addr"] and not i["wan"]:
            continue
        nets.append({"id": net_id(ident), "name": i["name"], "zone": zone_id(ident), "cidr": None if i["wan"] else i["cidr"],
                     "vlan": i["vlan"], "management": False, "dhcp": None, "purpose": "wan" if i["wan"] else "lan",
                     "gateway_ip": i["addr"], "kind": "iface", "interface": ident})
    return nets, wans


# ---------------------------------------------------------------------------- aliases
def aliases_of(raw) -> dict:
    """name -> {type, content[]} from the ``alias`` object of /api/firewall/alias/get (or search rows)."""
    out = {}
    obj = raw.get("aliases") if isinstance(raw, dict) else None
    if isinstance(obj, dict):
        obj = obj.get("alias", obj)
        obj = obj.get("aliases", obj) if isinstance(obj, dict) else obj
        obj = obj.get("alias", obj) if isinstance(obj, dict) else obj
    items = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    for a in list(items)[:MAX_ITEMS]:
        if not isinstance(a, dict):
            continue
        name = text(a.get("name"), 64)
        if name:
            out[name] = {"type": value(a.get("type")).lower(), "content": values(a.get("content"))[:256],
                         "enabled": flag(a.get("enabled")) if a.get("enabled") is not None else True}
    return out


# ---------------------------------------------------------------------------- rules
def _rule_map(flt) -> dict:
    """uuid -> rule of the ``filter`` object of /api/firewall/filter/get."""
    rules = flt.get("rules") if isinstance(flt, dict) else None
    rules = rules.get("rule") if isinstance(rules, dict) else None
    if not isinstance(rules, dict):
        return {}
    return {str(k): v for k, v in list(rules.items())[:MAX_ITEMS] if isinstance(v, dict) and UUID_RE.match(str(k))}


def raw_rules(raw) -> list:
    """[(uuid or legacy ref, rule dict, legacy flag)] in evaluation order: the rules of the rule API by sequence, then
    the classic rules newer releases list in the search (read only)."""
    flt = raw.get("filter") if isinstance(raw, dict) else None
    out = []
    for uuid, r in _rule_map(flt).items():
        out.append((uuid, r, False))
    if not out:                                   # no model object (no privilege for it): the search rows of the rule API
        for r in rows(raw, "rules"):
            if UUID_RE.match(str(r.get("uuid") or "")) and not r.get("legacy"):
                out.append((str(r["uuid"]), r, False))
    out.sort(key=lambda t: (_int(t[1].get("sequence"), 1), t[0]))
    legacy = [r for r in rows(raw, "rules") if r.get("legacy")]
    for pos, r in enumerate(legacy):
        out.append(("legacy:%d:%s" % (pos, text(r.get("ref") or r.get("description"), 40) or ""), r, True))
    return out


def _int(v, default=0) -> int:
    try:
        return int(str(value(v) if isinstance(v, (dict, list)) else v).strip())
    except (TypeError, ValueError):
        return default


def _plain(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return ",".join(values(v))
    return str(v).strip()


def rule_parts(r: dict, aliases=None) -> dict:
    """The fields of one rule in plain values (both shapes: ``.../get`` and search rows)."""
    proto_key = value(r.get("protocol")).lower() or "any"
    return {"enabled": flag(r.get("enabled")) if r.get("enabled") is not None else not flag(r.get("disabled")),
            "action": value(r.get("action")).lower() or "pass",
            "quick": flag(r.get("quick")) if r.get("quick") is not None else True,
            "interfaces": [i.lower() for i in values(r.get("interface"))],
            "interfacenot": flag(r.get("interfacenot")),
            "direction": value(r.get("direction")).lower() or "in",
            "ipprotocol": value(r.get("ipprotocol")).lower() or "inet",
            "protocol": proto_key,
            "src": values(r.get("source_net")) or ["any"], "src_not": flag(r.get("source_not")),
            "src_port": _plain(r.get("source_port")),
            "dst": values(r.get("destination_net")) or ["any"], "dst_not": flag(r.get("destination_not")),
            "dst_port": _plain(r.get("destination_port")),
            "description": text(r.get("description"), 255) or "",
            "sequence": _int(r.get("sequence"), 1),
            "gateway": bool(value(r.get("gateway"))),
            "sched": bool(value(r.get("sched"))),
            "statetype": value(r.get("statetype")).lower() or "keep",
            "log": flag(r.get("log"))}


def display_name(parts: dict, ref: str) -> str:
    d = parts["description"]
    if d.startswith(OWN_MARK):
        d = d[len(OWN_MARK):].strip()
    return text(d, 96) or "#%d" % parts["sequence"] if not ref.startswith("legacy:") else (text(d, 96) or "classic rule")


def _match(items, opposite=False):
    if not items:
        return None
    m = {"kind": "ip", "items": [i for i in items if i][:40]}
    if opposite:
        m["opposite"] = True
    return m


def policies_of_rule(ref: str, parts: dict, topo: Topology, aliases: dict, legacy: bool, order: int) -> list:
    """Neutral policies of one rule (one per zone pair it covers)."""
    action = ACTIONS.get(parts["action"], "other")
    proto = PROTOS.get(parts["protocol"], parts["protocol"][:16] or "any")
    ports = ports_text(parts["dst_port"], aliases)
    ports_named = ports is None
    if ports_named:
        ports = text(parts["dst_port"], 48) or ""
    ifs = [topo.ident(i) for i in parts["interfaces"]]
    ifs = [i for i in ifs if i in topo.ifaces or i in topo.groups]
    floating = parts["interfacenot"] or len(ifs) != 1 or parts["direction"] not in ("in",)
    if parts["interfacenot"] or not ifs:
        here = [ANY]
    else:
        here = ifs
    # the side that is not the interface: destination for rules going in, source for rules going out
    far_items = parts["dst"] if parts["direction"] != "out" else parts["src"]
    near_items = parts["src"] if parts["direction"] != "out" else parts["dst"]
    far_not = parts["dst_not"] if parts["direction"] != "out" else parts["src_not"]
    near_not = parts["src_not"] if parts["direction"] != "out" else parts["dst_not"]
    far = {}
    for item in far_items:
        z, m = topo.dest_zone(item) if not far_not else (ANY, {"kind": "ip", "item": text(item, 48)})
        far.setdefault(z, []).append(m["item"] if m else None)
    near_match = []
    for item in near_items:
        z, m = topo.dest_zone(item)
        if z == ANY and m is None:
            continue
        if m is None and z in here:
            continue                                              # the interface's own network: the whole zone
        near_match.append(m["item"] if m else text(item, 48))
    if near_not and not near_match:
        near_match = [text(i, 48) for i in near_items]
    out = []
    pairs = [(a, b) for a in here for b in sorted(far)][:MAX_PAIRS]
    simple = (not floating and not legacy and len(pairs) == 1 and not near_match and not ports_named
              and not parts["src_port"] and not parts["gateway"] and not parts["sched"] and parts["quick"]
              and parts["statetype"] == "keep" and not parts["dst_not"] and not parts["src_not"]
              and all(x is None for x in far[pairs[0][1]]) and ANY not in pairs[0] if pairs else False)
    for a, b in pairs:
        if parts["direction"] == "out":
            src_key, dst_key = b, a
        else:
            src_key, dst_key = a, b
        far_items_here = [x for x in far[b] if x]
        src = {"zone": zone_id(src_key)}
        dst = {"zone": zone_id(dst_key)}
        near_m = _match(near_match, near_not)
        far_m = _match(far_items_here, far_not)
        if parts["direction"] == "out":
            if far_m:
                src["match"] = far_m
            if near_m:
                dst["match"] = near_m
        else:
            if near_m:
                src["match"] = near_m
            if far_m:
                dst["match"] = far_m
        out.append({"id": policy_id(ref, src["zone"], dst["zone"]), "name": display_name(parts, ref), "src": src, "dst": dst,
                    "action": action, "proto": proto, "ports": ports, "return_traffic": True, "enabled": parts["enabled"],
                    "order": order, "scheduled": parts["sched"], "states": [] if parts["statetype"] == "keep" else [parts["statetype"]],
                    "builtin": legacy, "editable": bool(simple), "vendor_ref": ref,
                    "own": (not legacy) and parts["description"].startswith(OWN_MARK),
                    "ipv6": parts["ipprotocol"] == "inet6", "log": parts["log"], "floating": floating,
                    "policy_route": parts["gateway"] or None})
    return out


def _policies(raw, topo: Topology, aliases: dict) -> list:
    pols = []
    for pos, (ref, r, legacy) in enumerate(raw_rules(raw)):
        parts = rule_parts(r, aliases)
        ifs = [topo.ident(i) for i in parts["interfaces"]]
        prio = PRIO_FLOATING if parts["interfacenot"] or len(ifs) != 1 else PRIO_GROUP if ifs[0] in topo.groups else PRIO_IFACE
        order = (LEGACY_ORDER + pos) if legacy else prio + parts["sequence"]
        pols.extend(policies_of_rule(ref, parts, topo, aliases, legacy, order))
    seen, out = set(), []
    for p in pols:
        if p["id"] not in seen:
            seen.add(p["id"])
            out.append(p)
    return out


def rule_fields(r: dict, topo: Topology, aliases=None) -> dict:
    """The neutral fields of one simple rule (one interface, going in): what a plan step compares and writes."""
    parts = rule_parts(r, aliases)
    pols = policies_of_rule("x", parts, topo, aliases or {}, False, parts["sequence"])
    if len(pols) != 1:
        return {}
    p = pols[0]
    return {f: p[f] for f in ("name", "src", "dst", "action", "proto", "ports", "enabled")}


# ---------------------------------------------------------------------------- NAT, reservations, VPN, routes
def _forwards(raw, aliases) -> list:
    out = []
    for pos, r in enumerate(rows(raw, "dnat")):
        if r.get("uuid") and not UUID_RE.match(str(r["uuid"])):
            continue
        name = text(r.get("descr") or r.get("description"), 96) or "#%d" % (pos + 1)
        target = ip_of(r.get("target")) or text(r.get("target"), 48)
        if not target:
            continue
        proto = PROTOS.get(value(r.get("protocol")).lower(), "tcp_udp")
        wan_port = ports_text(r.get("destination.port") or r.get("destination_port") or "", aliases)
        dst_port = ports_text(r.get("local-port") or r.get("local_port") or "", aliases)
        src = value(r.get("source.network") or r.get("source_net") or "any").lower()
        out.append({"id": oid("f", r.get("uuid") or name), "name": name, "proto": proto if proto in ("tcp", "udp") else "tcp_udp",
                    "wan_port": wan_port or None, "dst_ip": target, "dst_port": dst_port or wan_port or None,
                    "enabled": not flag(r.get("disabled")) if r.get("disabled") is not None else flag(r.get("enabled", "1")),
                    "source_limited": src not in ("", "any"), "order": pos})
    return out


def _reservations(raw) -> list:
    out = []
    for r in rows(raw, "kea"):
        ip = ip_of(r.get("ip_address"))
        if ip:
            h = mac_hash(r.get("hw_address"))
            out.append({"id": oid("r", "kea|%s|%s" % (h, ip)), "client": None, "name": text(r.get("hostname"), 64),
                        "ip": ip, "server": "kea"})
    for r in rows(raw, "dnsmasq"):
        ip = ip_of(value(r.get("ip")))
        if ip:
            h = mac_hash(value(r.get("hwaddr")))
            out.append({"id": oid("r", "dnsmasq|%s|%s" % (h, ip)), "client": None, "name": text(r.get("host"), 64),
                        "ip": ip, "server": "dnsmasq"})
    return out


def _vpn(raw) -> list:
    out = []
    for kind, key, name_f in (("tunnel", "vpn_tunnel", "name"), ("ipsec", "vpn_ipsec", "description"),
                              ("ssl", "vpn_ssl", "description")):
        for r in rows(raw, key):
            name = text(r.get(name_f) or r.get("description") or r.get("name"), 96)
            if name:
                out.append({"id": oid("v", "%s|%s" % (kind, r.get("uuid") or name)), "name": name, "kind": kind,
                            "type": text(r.get("role"), 24), "enabled": flag(r.get("enabled", "1"))})
    return out


def _raw_extra(raw, topo: Topology) -> list:
    out = []
    flt = raw.get("filter") if isinstance(raw.get("filter"), dict) else {}
    snat = rows(raw, "snat")
    if not snat:
        sn = flt.get("snatrules") if isinstance(flt.get("snatrules"), dict) else {}
        snat = [v for v in (sn.get("rule") or {}).values() if isinstance(v, dict)] if isinstance(sn.get("rule"), dict) else []
    for i, r in enumerate(snat[:MAX_ITEMS]):
        title = text(r.get("description"), 96) or "#%d" % (i + 1)
        out.append({"id": oid("x", "snat%s%d" % (r.get("uuid") or "", i)), "area": "nat", "editable": False, "title": title,
                    "enabled": flag(r.get("enabled", "1")) and not flag(r.get("nonat"))})
    for area in ("npt", "onetoone"):
        obj = flt.get(area) if isinstance(flt.get(area), dict) else {}
        items = obj.get("rule") if isinstance(obj.get("rule"), dict) else {}
        for k, r in list(items.items())[:MAX_ITEMS]:
            if isinstance(r, dict):
                out.append({"id": oid("x", area + str(k)), "area": "nat", "editable": False,
                            "title": text(r.get("description"), 96) or area, "enabled": flag(r.get("enabled", "1"))})
    for r in rows(raw, "routes"):
        dest = text(r.get("network"), 48)
        if dest:
            out.append({"id": oid("x", "route%s%s" % (dest, r.get("uuid") or "")), "area": "route", "editable": False,
                        "title": "%s via %s" % (dest, text(value(r.get("gateway")), 48) or "?"),
                        "enabled": not flag(r.get("disabled"))})
    for key, g in topo.groups.items():
        out.append({"id": oid("x", "group" + key), "area": "rule_group", "editable": False, "title": g["name"],
                    "count": len(g["members"])})
    for r in rows(raw, "vlans"):
        tag = _int(r.get("tag"), 0)
        if 1 <= tag <= 4094:
            out.append({"id": oid("x", "vlan%s%d" % (r.get("uuid") or r.get("vlanif") or "", tag)), "area": "vlan",
                        "editable": False, "title": "VLAN %d%s" % (tag, (" " + text(r.get("descr"), 48)) if text(r.get("descr"), 48) else "")})
    return out


def _gateway_status(raw) -> list:
    out = []
    for r in rows(raw, "gateways"):
        name = text(r.get("name"), 48)
        if name:
            st = str(r.get("status") or "").lower()
            out.append({"name": name, "down": st in ("down", "force_down") or "down" in st and "none" not in st})
    return out


# ---------------------------------------------------------------------------- plain checks
def findings(state: dict, gateways=()) -> list:
    out = []
    zones = {z["id"]: z for z in state.get("zones") or []}
    wan = {zid for zid, z in zones.items() if z.get("system") == "external"}
    self_z = zone_id(SELF)
    admin = {}
    seen = set()
    for p in state.get("policies") or []:
        if not p.get("enabled") or p.get("action") != "allow":
            continue
        s, d = p["src"], p["dst"]
        open_service = p.get("proto") == "any" and not p.get("ports")
        if s["zone"] in wan and d["zone"] in (self_z, ANY) and not s.get("match"):
            hit = [ADMIN_PORTS[x] for x in ADMIN_PORTS if open_service or x in str(p.get("ports") or "").split(",")]
            if hit:
                admin.setdefault(s["zone"], set()).update(hit)
        if p["vendor_ref"] in seen:
            continue
        if s["zone"] in wan and d["zone"] not in wan and d["zone"] != self_z and open_service and not d.get("match") \
                and not s.get("match"):
            seen.add(p["vendor_ref"])
            out.append({"id": oid("g", "wanin" + p["vendor_ref"]), "kind": "wan_inbound_open", "level": "alert",
                        "subject": p["id"], "params": {"rule": p["name"], "zone": zones.get(d["zone"], {}).get("name") or "*"}})
        elif s["zone"] == ANY and d["zone"] == ANY and open_service and not s.get("match") and not d.get("match"):
            seen.add(p["vendor_ref"])
            out.append({"id": oid("g", "anyany" + p["vendor_ref"]), "kind": "any_any", "level": "warn", "subject": p["id"],
                        "params": {"rule": p["name"]}})
    for zid in sorted(admin):
        z = zones[zid]
        z["admin_services"] = sorted(s.lower() for s in admin[zid])
        out.insert(0, {"id": oid("g", "admin" + zid), "kind": "admin_on_wan", "level": "alert", "subject": zid,
                       "params": {"zone": z["name"], "services": ", ".join(sorted(admin[zid]))}})
    off = sorted({p["vendor_ref"] for p in state.get("policies") or [] if not p.get("enabled")})
    if off:
        out.append({"id": oid("g", "disabled"), "kind": "rules_disabled", "level": "info", "subject": None,
                    "params": {"n": len(off)}})
    for g in gateways:
        if g["down"]:
            out.append({"id": oid("g", "gwdown" + g["name"]), "kind": "wan_down", "level": "alert", "subject": None,
                        "params": {"wan": g["name"]}})
    return out


def rule_digest(pols) -> list:
    """One short line per rule (name, on/off, fingerprint of what decides its effect) for the night report."""
    out = {}
    for p in pols:
        d = out.setdefault(p["vendor_ref"], {"name": p["name"], "enabled": p["enabled"], "allow": p["action"] == "allow", "parts": []})
        d["parts"].append(repr((p["src"], p["dst"], p["action"], p["proto"], p["ports"], p["scheduled"])))
    names = {}
    for d in out.values():
        names[d["name"]] = names.get(d["name"], 0) + 1
    seen = {}
    res = []
    for ref, d in out.items():
        name = d["name"]
        if names[name] > 1:                               # two rules with one description: keep them apart
            seen[name] = seen.get(name, 0) + 1
            name = "%s (%d)" % (name, seen[name])
        res.append({"name": name, "enabled": d["enabled"], "allow": d["allow"],
                    "sig": hashlib.sha256("\n".join(sorted(d["parts"])).encode("utf-8")).hexdigest()[:16]})
    return res


# ---------------------------------------------------------------------------- the whole state
def to_neutral(raw: dict) -> dict:
    """Neutral model v2 from whatever replies are present. Raises nothing for missing ones (they stay empty)."""
    raw = raw if isinstance(raw, dict) else {}
    topo = Topology(raw)
    aliases = aliases_of(raw)
    zones = _zones(topo)
    nets, wans = _networks(topo)
    pols = _policies(raw, topo, aliases)
    known = {z["id"] for z in zones}
    for p in pols:                                   # a rule may name an interface the account could not list
        for side in (p["src"], p["dst"]):
            if side["zone"] != ANY and side["zone"] not in known:
                zones.append({"id": side["zone"], "name": "?", "kind": None, "system": None, "builtin": False,
                              "interface": False, "admin_services": [], "description": None})
                known.add(side["zone"])
    gws = _gateway_status(raw)
    state = {"v": 2, "zones": zones, "networks": nets, "devices": [], "equipment": [], "ports": [], "policies": pols,
             "ssids": [], "dhcp_reservations": _reservations(raw), "port_forwards": _forwards(raw, aliases), "wans": wans,
             "vpn": _vpn(raw), "raw": _raw_extra(raw, topo)}
    state["allow"] = neutral.derive_allow(state, default="block") if pols else []
    external = {z["id"] for z in zones if z.get("system") == "external"}
    for z in zones:
        z["internet"] = any(a == z["id"] and (b in external or b == ANY) for a, b in state["allow"])
        z["discovery"] = False
    state["findings"] = findings(state, gws)
    state["rule_digest"] = rule_digest(pols)
    ver = parse_version(raw.get("version"))
    state["controller"] = {"version": ("%d.%d" % ver) if ver else (text(raw.get("version"), 24) or ""), "apis": ["rest"],
                           "native_rollback": native_rollback(raw.get("version"))}
    return state
