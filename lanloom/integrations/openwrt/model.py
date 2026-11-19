"""Translation module of the OpenWrt family: ubus/UCI replies -> neutral model v2 (and the rule fields both halves use).

Pure functions, no I/O. Shared by the read half and the write half, so both compute the same neutral state and the
same policy ids. It also holds the checks of the JSON-RPC calls the two halves may send (``call_body``,
``call_check``): the transport's allow-list uses them below the adapter.

``to_neutral(raw)`` takes what the read half collected (every key optional; a call the account may not make or a
package that is not installed simply leaves it out)::

    version    release ("23.05.5", "24.10.0", "SNAPSHOT") or ""
    board      reply of ``system board``           hostname, model, board_name, release {version, target, ...}
    network    ``uci get network``  values          interfaces, devices (bridges, VLAN devices), bridge VLANs
    firewall   ``uci get firewall`` values          defaults, zones, forwardings, rules, redirects, includes
    dhcp       ``uci get dhcp``     values          address pools per interface, fixed addresses (host sections)
    wireless   ``uci get wireless`` values          radios and wireless interfaces
    ifaces     reply of ``network.interface dump``   live state of every interface (up, addresses, device)
    devices    reply of ``network.device status``   live state of every network device (hardware address)
    hints      reply of ``luci-rpc getHostHints``    known hosts: hardware address -> name and addresses
    leases     reply of ``luci-rpc getDHCPLeases``   current address leases
    air        {ifname: {"info": iwinfo info, "assoc": [iwinfo assoclist rows]}}   radios on air, associated clients

and builds::

    zones          every firewall zone (a zone is a set of interfaces) and the router itself (gateway zone)
    networks       interfaces with an address -> network (zone = the firewall zone that lists the interface)
    policies       firewall rules in configuration order (a rule from zone A to zone B; no destination zone = to the
                   router itself; no source zone = from the router), then the zone forwardings (A may reach B), then
                   each zone's input policy (traffic to the router); traffic between two zones that nothing decides
                   follows the global forward policy (``defaults``)
    port_forwards  redirects with target DNAT;  dhcp_reservations  fixed addresses (the hardware address only hashed)
    ssids          wireless interfaces in access point mode (bands, security, hidden, on/off, clients on air)
    devices        known clients (leases, host hints, associated wireless clients) - ids are hashes
    equipment      the router itself;  wans  interfaces of an external zone (masquerading or named wan)
    raw            rules that only mark or skip tracking, source NAT, includes, bridge VLANs (shown, never written)
    rule_digest    one line per rule (name, on/off, fingerprint) for the night report
    findings       plain checks (management open on the internet side, everything from the internet side inside,
                   any to any, forward policy accept, rules switched off, an open wireless network, a WAN down)

Never copied: wireless keys and passwords, dial-up passwords, private and pre-shared keys of tunnels, modem PINs,
certificates, anything else whose option name says it is a secret (``scrub``). Neutral ids are opaque hashes; a
hardware address only ever appears hashed in the state (the topology observation for the core carries the clients'
hardware addresses, like every controller source).
"""
import hashlib
import ipaddress
import re

from ...firewall import neutral

SALT = "lanloom-owrt:"
OWN_MARK = "[lanloom]"           # name prefix of the rules the write half creates (only those may be deleted)
MAX_ITEMS = 5000
TEXT_MAX = 96
ANY = "*"
SELF = "self"                     # the zone of the router itself (rules without a destination zone)
ZERO_SID = "0" * 32               # the session id of a call before the sign-in
APPLY_TIMEOUT = 90                # seconds of the router's own way back (the web interface never uses less)
FORWARD_ORDER = 2_000_000         # zone forwardings come after the rules of their zone
INPUT_ORDER = 3_000_000           # a zone's input policy comes after every rule
TARGETS = {"ACCEPT": "allow", "REJECT": "reject", "DROP": "block"}
TARGETS_OUT = {v: k for k, v in TARGETS.items()}
PROTOS = {"all": "any", "any": "any", "tcp": "tcp", "udp": "udp", "tcp udp": "tcp_udp", "tcpudp": "tcp_udp",
          "icmp": "icmp"}
PROTOS_OUT = {"any": "all", "tcp": "tcp", "udp": "udp", "tcp_udp": "tcp udp", "icmp": "icmp"}
ADMIN_PORTS = {"22": "SSH", "80": "HTTP", "443": "HTTPS"}
IPV6_ONLY = ("dhcpv6", "6in4", "6to4", "6rd", "dslite", "map")     # a down IPv6 link alone is no "internet down"
BANDS = {"2g": "2.4ghz", "5g": "5ghz", "6g": "6ghz", "60g": "60ghz", "11g": "2.4ghz", "11b": "2.4ghz", "11a": "5ghz",
         "11ng": "2.4ghz", "11na": "5ghz", "11ac": "5ghz", "11ax": None}
RADIO_SECTION, AP_SECTION = "wifi-device", "wifi-iface"     # section types of the wireless configuration (identifiers)
SECURITY = {"none": "open", "owe": "owe", "wep": "wep", "wep-open": "wep", "wep-shared": "wep"}
ZONE_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}\Z")
SECTION_RE = re.compile(r"\A[A-Za-z0-9_]{1,64}\Z")
IFNAME_RE = re.compile(r"\A[A-Za-z0-9_.@:-]{1,32}\Z")
SID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
# options whose value is a secret (wireless keys, dial-up passwords, tunnel keys, modem PINs, certificates) - never kept
SECRET_OPTION = re.compile(r"(?i)(\Akey\d?\Z|pass|psk|secret|private|preshared|priv_key|cert|\Aca\Z|token|\Apin|pincode|"
                           r"\Aauth\b|auth_secret|sae_|identity|anonymous_identity|password)")
# options of a rule that make it more than "zone A to zone B, one service": shown, never rewritten by the editor
RULE_EXTRA = ("src_ip", "dest_ip", "src_mac", "src_port", "icmp_type", "limit", "limit_burst", "extra", "start_date",
              "stop_date", "start_time", "stop_time", "weekdays", "monthdays", "utc_time", "ipset", "set_mark",
              "set_xmark", "mark", "helper", "set_helper", "device", "direction", "dscp", "set_dscp", "src_dport")
RULE_FIELDS = ("name", "src", "dest", "proto", "dest_port", "target", "enabled", "family")


# ---------------------------------------------------------------------------- small helpers
def oid(prefix: str, vendor_id) -> str:
    return prefix + hashlib.sha256((SALT + prefix + str(vendor_id)).encode("utf-8")).hexdigest()[:12]


def text(value, limit: int = TEXT_MAX):
    if value is None or isinstance(value, (dict, list)):
        return None
    out = "".join(ch for ch in str(value) if ch >= " ")[:limit].strip()
    return out or None


def items(v) -> list:
    """A UCI option as a list: a list option, or a string with space-separated items."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()][:200]
    if v is None or isinstance(v, (dict, bool)):
        return []
    return [x for x in str(v).split() if x][:200]


def flag(v, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "yes", "on", "true", "enabled")


def norm_mac(mac):
    m = re.sub(r"[^0-9a-f]", "", str(mac or "").lower())
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else None


def mac_hash(mac):
    m = norm_mac(mac)
    if not m:
        return None
    return hashlib.sha256((SALT + "mac:" + m.replace(":", "")).encode()).hexdigest()[:16]


def private_mac(mac) -> bool:
    m = norm_mac(mac)
    return bool(m) and bool(int(m[:2], 16) & 2)


def ip_of(value_):
    try:
        a = ipaddress.ip_address(str(value_).strip())
    except ValueError:
        return None
    return str(a)


def scrub(obj, depth=0):
    """A copy of a reply without any option whose name says it carries a secret. Only fields with a plain value
    (or a list of them) are dropped: a key that maps to an object is data (a section name), not an option name."""
    if depth > 12:
        return None
    if isinstance(obj, dict):
        return {k: scrub(v, depth + 1) for k, v in obj.items()
                if isinstance(v, dict) or not SECRET_OPTION.search(str(k))}
    if isinstance(obj, list):
        return [scrub(v, depth + 1) for v in obj[:MAX_ITEMS]]
    return obj


def parse_version(v) -> tuple:
    """``23.05.5`` / ``OpenWrt 24.10.0`` -> (23, 5); ``SNAPSHOT`` -> (99, 0); () when unknown."""
    s = str(v or "")
    if "snapshot" in s.lower():
        return (99, 0)
    m = re.search(r"(?<!\d)(\d{2})\.(\d{2})(?:\.\d+)?", s)
    return (int(m.group(1)), int(m.group(2))) if m else ()


def native_rollback(version) -> bool:
    """Does this release give UCI its own way back over ubus (``uci apply`` with ``rollback`` and ``timeout``, then
    ``uci confirm``)? Every release since 18.06 has it in rpcd; unknown releases: None (the write half asks)."""
    ver = parse_version(version)
    if not ver:
        return None
    return ver >= (18, 6)


def firewall_generation(version) -> str:
    """``fw4`` (nftables, 22.03 and later) or ``fw3`` (iptables, up to 21.02); "" when unknown."""
    ver = parse_version(version)
    if not ver:
        return ""
    return "fw4" if ver >= (22, 3) else "fw3"


def zone_id(name) -> str:
    k = str(name or "").strip()
    if k in ("", ANY):
        return ANY
    if k == SELF:
        return oid("z", "\x00self")
    return oid("z", k)


def net_id(iface) -> str:
    return oid("n", str(iface))


def policy_id(ref) -> str:
    return oid("p", str(ref))


def sections(cfg, kind=None) -> list:
    """The sections of a ``uci get`` reply in configuration order: [(name, section dict)]."""
    vals = cfg.get("values") if isinstance(cfg, dict) and isinstance(cfg.get("values"), dict) else cfg
    if not isinstance(vals, dict):
        return []
    out = []
    for name, s in vals.items():
        if not isinstance(s, dict) or not SECTION_RE.match(str(name)):
            continue
        if kind and s.get(".type") != kind:
            continue
        out.append((str(name), s))
    out.sort(key=lambda t: (_int(t[1].get(".index"), 10 ** 6), t[0]))
    return out[:MAX_ITEMS]


ANON_RE = re.compile(r"\Acfg[0-9a-f]{6}\Z")
OWN_SECTION_RE = re.compile(r"\Alanloom_[0-9a-f]{12}\Z")     # the named sections the write half creates


def anonymous(name: str, s: dict) -> bool:
    if isinstance(s.get(".anonymous"), bool):
        return s[".anonymous"]
    return bool(ANON_RE.match(str(name)))


def stable_refs(secs, keyfn) -> list:
    """[(section name, stable reference, section)]. UCI names an anonymous section when it loads the file, from its
    position and content (``cfg0a1b2c``), so that name changes after every commit. A named section keeps its name
    (``s:<name>``); an anonymous one is referred to by ``keyfn(section)`` (its name option, a zone pair) and its
    position among the sections with the same key (``a:<key>#<n>``)."""
    out, count = [], {}
    for name, s in secs:
        if not anonymous(name, s):
            out.append((name, "s:" + name, s))
            continue
        key = str(keyfn(s) or "")[:120]
        count[key] = count.get(key, 0) + 1
        out.append((name, "a:%s#%d" % (key, count[key]), s))
    return out


def _rule_key(s: dict) -> str:
    n = text(s.get("name"), 120)
    if n:
        return n
    return "?%s>%s" % (s.get("src") or "", s.get("dest") or "")


def rule_refs(raw) -> list:
    """[(section name, stable reference, section)] of the firewall rules, in configuration order."""
    return stable_refs(sections(raw.get("firewall") if isinstance(raw, dict) else None, "rule"), _rule_key)


def _int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def ports_text(spec):
    """A UCI port option in plain form: ``80``, ``8000-8080`` (UCI writes ``8000-8080`` or ``8000:8080``), several
    ports joined by commas; None when it holds something else (a name)."""
    parts = items(spec) if not isinstance(spec, str) or "," not in spec else [p.strip() for p in spec.split(",")]
    out = []
    for p in parts:
        if re.fullmatch(r"\d{1,5}", p) and 1 <= int(p) <= 65535:
            out.append(str(int(p)))
        elif re.fullmatch(r"\d{1,5}[-:]\d{1,5}", p):
            a, b = re.split(r"[-:]", p)
            out.append("%d-%d" % (int(a), int(b)))
        else:
            return None
    return ",".join(out)


def ports_out(ports: str) -> str:
    """Neutral ports -> one UCI port value (a port or one range ``lo-hi``); "" for any."""
    if not ports:
        return ""
    if "," in ports:
        raise ValueError("one port or one range")
    m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", ports)
    if not m or not 1 <= int(m.group(1)) <= 65535 or (m.group(2) and not int(m.group(1)) <= int(m.group(2)) <= 65535):
        raise ValueError("port")
    return ports


def proto_key(v) -> str:
    """The protocol option of a rule as one key: ``all`` for none, ``tcp udp`` for the default pair."""
    got = [p.lower() for p in items(v)]
    if not got:
        return "tcp udp"                                # UCI's default protocol of a rule
    if set(got) == {"tcp", "udp"}:
        return "tcp udp"
    return " ".join(got)


# ---------------------------------------------------------------------------- JSON-RPC calls (both halves)
def call_body(sid: str, obj: str, method: str, args: dict, n: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": int(n), "method": "call", "params": [sid, obj, method, dict(args)]}


def _plain_text(v, limit=96) -> bool:
    return isinstance(v, str) and 0 < len(v) <= limit and not re.search(r"[\x00-\x1f\x7f]", v)


LOGIN_ARGS = {"username": lambda v: _plain_text(v, 64) and bool(re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", v)),
              "password": lambda v: isinstance(v, str) and 0 < len(v) <= 256,
              "timeout": lambda v: isinstance(v, int) and not isinstance(v, bool) and 60 <= v <= 3600}


def call_check(allowed: dict):
    """A check of the ``params`` of one JSON-RPC call: ``[session, object, method, args]`` where (object, method) is a
    key of ``allowed`` and ``args`` has exactly the fields its spec names, each value passing its check. A spec is
    ``{field: check}``, or a list of such dicts (any one may match). The sign-in call alone carries the all-zero
    session; every other call a real session id."""
    def check(params) -> bool:
        if not isinstance(params, list) or len(params) != 4:
            return False
        sid, obj, method, args = params
        if not isinstance(sid, str) or not SID_RE.match(sid) or not isinstance(args, dict):
            return False
        spec = allowed.get((obj, method)) if isinstance(obj, str) and isinstance(method, str) else None
        if spec is None:
            return False
        if (obj, method) == ("session", "login") and sid != ZERO_SID:
            return False
        if (obj, method) != ("session", "login") and sid == ZERO_SID:
            return False
        for one in (spec if isinstance(spec, list) else [spec]):
            if set(args) == set(one) and all(one[k](args[k]) is True for k in args):
                return True
        return False
    return check


ENVELOPE = {"jsonrpc": lambda v: v == "2.0",
            "id": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10 ** 9,
            "method": lambda v: v == "call"}


def reply(body):
    """(status code, data) of one JSON-RPC reply. Status 0 is success; a JSON-RPC error is returned as a negative
    code (-32002 = access denied, the session is gone or may not make this call)."""
    if not isinstance(body, dict):
        return -1, None
    if isinstance(body.get("error"), dict):
        try:
            return int(body["error"].get("code")), None
        except (TypeError, ValueError):
            return -1, None
    res = body.get("result")
    if not isinstance(res, list) or not res or not isinstance(res[0], int):
        return -1, None
    return res[0], (res[1] if len(res) > 1 else None)


# ---------------------------------------------------------------------------- interfaces, zones, networks
class Topology:
    """Firewall zones, interfaces and networks with the lookups rules need."""

    def __init__(self, raw):
        raw = raw if isinstance(raw, dict) else {}
        live = {}
        dump = raw.get("ifaces")
        for r in (dump.get("interface") if isinstance(dump, dict) else dump) or []:
            if isinstance(r, dict) and isinstance(r.get("interface"), str):
                live[r["interface"]] = r
        self.ifaces = {}                       # interface name -> {name, addr, cidr, vlan, proto, up, device}
        for name, s in sections(raw.get("network"), "interface"):
            if name == "loopback" or str(s.get("proto") or "") == "none" and name not in live:
                continue
            lv = live.get(name) or {}
            addr, cidr = None, None
            for a in lv.get("ipv4-address") or []:
                if isinstance(a, dict) and ip_of(a.get("address")):
                    addr, cidr = _net(a.get("address"), a.get("mask"))
                    break
            if not addr:
                ips = items(s.get("ipaddr"))
                if ips:
                    addr, cidr = _net(ips[0].split("/")[0], ips[0].split("/")[1] if "/" in ips[0] else s.get("netmask"))
            dev = str(lv.get("l3_device") or lv.get("device") or s.get("device") or s.get("ifname") or "")
            self.ifaces[name] = {"name": text(name, 32), "addr": addr, "cidr": cidr, "vlan": _vlan_of(dev, raw),
                                 "proto": text(s.get("proto"), 16) or "none", "device": text(dev.split()[0] if dev else None, 32),
                                 "up": lv.get("up") if isinstance(lv.get("up"), bool) else None,
                                 "enabled": not flag(s.get("disabled")) and flag(s.get("auto"), True)}
        self.zones = {}                        # zone name -> {networks, input, output, forward, masq, external}
        for _, s in sections(raw.get("firewall"), "zone"):
            name = str(s.get("name") or "").strip()
            if not ZONE_RE.match(name) or name in self.zones:
                continue
            nets = [n for n in items(s.get("network")) if IFNAME_RE.match(n)]
            masq = flag(s.get("masq"))
            self.zones[name] = {"networks": nets, "input": str(s.get("input") or "").upper() or None,
                                "output": str(s.get("output") or "").upper() or None,
                                "forward": str(s.get("forward") or "").upper() or None, "masq": masq,
                                "external": masq or name.lower().startswith("wan") or any(n.lower().startswith("wan") for n in nets),
                                "enabled": not flag(s.get("disabled"))}
        self.zone_of_iface = {}
        for zname, z in self.zones.items():
            for n in z["networks"]:
                self.zone_of_iface.setdefault(n, zname)
        d = next((s for _, s in sections(raw.get("firewall"), "defaults")), {})
        self.defaults = {k: (str(d.get(k) or "").upper() or None) for k in ("input", "output", "forward")}
        self._nets = []
        for iname, i in self.ifaces.items():
            z = self.zone_of_iface.get(iname)
            if i["cidr"] and z:
                self._nets.append((ipaddress.ip_network(i["cidr"]), z))
        self._nets.sort(key=lambda t: -t[0].prefixlen)

    def is_external(self, zone) -> bool:
        return bool(self.zones.get(zone, {}).get("external"))

    def zone_of_address(self, spec):
        try:
            net = ipaddress.ip_network(str(spec).strip(), strict=False)
        except ValueError:
            return None
        for n, z in self._nets:
            if net.version == n.version and net.subnet_of(n):
                return z
        return None


def _net(addr, mask):
    try:
        if mask is None or mask == "":
            i = ipaddress.ip_interface(str(addr).strip())
        else:
            i = ipaddress.ip_interface("%s/%s" % (str(addr).strip(), str(mask).strip()))
    except ValueError:
        return None, None
    if i.version != 4:
        return None, None
    if i.network.prefixlen >= 32:
        return str(i.ip), None
    return str(i.ip), str(i.network)


def _vlan_of(dev: str, raw) -> int:
    """The VLAN of a device name (``eth0.30``, ``br-lan.30``, ``lan1.30``) or of a VLAN device section."""
    dev = str(dev or "").split()[0] if dev else ""
    m = re.search(r"\.(\d{1,4})\Z", dev)
    if m and 1 <= int(m.group(1)) <= 4094:
        return int(m.group(1))
    for _, s in sections(raw.get("network"), "device"):
        if str(s.get("name") or "") == dev and str(s.get("type") or "") in ("8021q", "8021ad"):
            vid = _int(s.get("vid"), 0)
            return vid if 1 <= vid <= 4094 else None
    return None


def _zones(topo: Topology) -> list:
    out = []
    for name, z in topo.zones.items():
        out.append({"id": zone_id(name), "name": text(name, 32), "kind": name, "system": "external" if z["external"] else None,
                    "builtin": True, "interface": True, "admin_services": [], "description": None,
                    "networks": [net_id(n) for n in z["networks"] if n in topo.ifaces],
                    "input": z["input"], "forward": z["forward"], "masq": z["masq"]})
    if out:
        out.append({"id": zone_id(SELF), "name": "This router", "kind": SELF, "system": "gateway", "builtin": True,
                    "interface": False, "admin_services": [], "description": None})
    return out


def _dhcp_pools(raw, topo: Topology) -> dict:
    """interface -> {on, range} from the address pools of the dhcp configuration."""
    out = {}
    for _, s in sections(raw.get("dhcp"), "dhcp"):
        iface = str(s.get("interface") or "")
        if iface not in topo.ifaces:
            continue
        cidr = topo.ifaces[iface]["cidr"]
        on = not flag(s.get("ignore"))
        rng = None
        if on and cidr:
            net = ipaddress.ip_network(cidr)
            start, limit = _int(s.get("start"), 100), _int(s.get("limit"), 150)
            first = int(net.network_address) + max(1, start)
            last = min(first + max(1, limit) - 1, int(net.broadcast_address) - 1)
            if first <= last:
                rng = [str(ipaddress.ip_address(first)), str(ipaddress.ip_address(last))]
        out[iface] = {"on": on, "range": rng}
    return out


def _networks(raw, topo: Topology) -> tuple:
    pools = _dhcp_pools(raw, topo)
    nets, wans = [], []
    for iname, i in topo.ifaces.items():
        zname = topo.zone_of_iface.get(iname)
        external = topo.is_external(zname) if zname else False
        if external:
            wans.append({"id": net_id(iname), "name": i["name"], "type": i["proto"], "failover": None,
                         "enabled": i["enabled"], "gateways": [],
                         "status": None if i["up"] is None else ("up" if i["up"] else "down")})
        if not i["addr"] and not external:
            continue
        low = iname.lower()
        purpose = "wan" if external else ("guest" if "guest" in low else "iot" if low in ("iot", "smart") else "lan")
        nets.append({"id": net_id(iname), "name": i["name"], "zone": zone_id(zname) if zname else ANY,
                     "cidr": None if external else i["cidr"], "vlan": i["vlan"], "management": False,
                     "dhcp": pools.get(iname), "purpose": purpose, "gateway_ip": i["addr"], "kind": "iface",
                     "interface": iname})
    return nets, wans


# ---------------------------------------------------------------------------- rules
def rule_parts(s: dict) -> dict:
    """The fields of one rule section in plain values."""
    extra = sorted(k for k in RULE_EXTRA if s.get(k) not in (None, "", []))
    return {"name": text(s.get("name"), 120) or "",
            "src": str(s.get("src")).strip() if s.get("src") not in (None, "") else None,
            "dest": str(s.get("dest")).strip() if s.get("dest") not in (None, "") else None,
            "proto": proto_key(s.get("proto")),
            "dest_port": " ".join(items(s.get("dest_port"))),
            "target": str(s.get("target") or "DROP").strip().upper(),
            "enabled": flag(s.get("enabled"), True),
            "family": str(s.get("family") or "any").strip().lower(),
            "src_ip": items(s.get("src_ip")), "dest_ip": items(s.get("dest_ip")),
            "extra": extra,
            "scheduled": any(k in extra for k in ("start_time", "stop_time", "weekdays", "monthdays", "start_date", "stop_date"))}


def display_name(parts: dict, ref: str) -> str:
    n = parts["name"]
    if n.startswith(OWN_MARK):
        n = n[len(OWN_MARK):].strip()
    if text(n, 96):
        return text(n, 96)
    m = re.search(r"#(\d+)\Z", ref)
    return "rule %s" % (m.group(1) if m else ref[-12:])


def _zone_ref(topo: Topology, name, missing: str) -> str:
    if name is None:
        return SELF if missing == "self" else ANY
    if name == ANY:
        return ANY
    return name


def _match(items_, opposite=False):
    if not items_:
        return None
    m = {"kind": "ip", "items": [text(i, 48) for i in items_ if text(i, 48)][:40]}
    if opposite:
        m["opposite"] = True
    return m


def policy_of_rule(ref: str, parts: dict, topo: Topology, order: int) -> dict:
    """One neutral policy for one rule (a UCI rule always names one source and one destination)."""
    src = _zone_ref(topo, parts["src"], "self")       # no source zone: traffic the router itself sends
    dst = _zone_ref(topo, parts["dest"], "self")      # no destination zone: traffic to the router itself
    action = TARGETS.get(parts["target"], "other")
    proto = PROTOS.get(parts["proto"], parts["proto"][:16] or "any")
    ports = ports_text(parts["dest_port"]) if parts["dest_port"] else ""
    named = ports is None
    if named:
        ports = text(parts["dest_port"], 48) or ""
    s_side, d_side = {"zone": zone_id(src)}, {"zone": zone_id(dst)}
    sm, dm = _match(parts["src_ip"]), _match(parts["dest_ip"])
    if sm:
        s_side["match"] = sm
    if dm:
        d_side["match"] = dm
    simple = (src not in (ANY, SELF) and src in topo.zones and (dst in (ANY, SELF) or dst in topo.zones)
              and action in ("allow", "block", "reject") and proto in PROTOS_OUT and not named
              and "," not in ports and not parts["extra"] and not parts["src_ip"] and not parts["dest_ip"]
              and parts["family"] in ("any", "", "ipv4", "ipv6") and not (proto in ("any", "icmp") and ports))
    return {"id": policy_id(ref), "name": display_name(parts, ref), "src": s_side, "dst": d_side,
            "action": action, "proto": proto, "ports": ports, "return_traffic": True, "enabled": parts["enabled"],
            "order": order, "scheduled": parts["scheduled"], "states": [], "builtin": False, "editable": bool(simple),
            "vendor_ref": ref, "own": bool(OWN_SECTION_RE.match(ref[2:])) and ref.startswith("s:")
            and parts["name"].startswith(OWN_MARK), "ipv6": parts["family"] == "ipv6",
            "family": parts["family"] if parts["family"] in ("ipv4", "ipv6") else "any"}


def rule_fields(s: dict, topo: Topology) -> dict:
    """The neutral fields of one rule: what a plan step compares and writes."""
    p = policy_of_rule("x", rule_parts(s), topo, 0)
    return {f: p[f] for f in ("name", "src", "dst", "action", "proto", "ports", "enabled")}


def _policies(raw, topo: Topology) -> tuple:
    pols, marks = [], []
    for pos, (name, ref, s) in enumerate(rule_refs(raw)):
        parts = rule_parts(s)
        if parts["target"] not in TARGETS:
            marks.append((ref, parts))                  # marks, tracking helpers: no verdict, shown only
            continue
        p = policy_of_rule(ref, parts, topo, pos + 1)
        if ref.startswith("a:?"):
            p["editable"] = False                       # a rule without a name has no reference that survives a change
        pols.append(p)
    fwds = stable_refs(sections(raw.get("firewall"), "forwarding"), lambda s: "%s>%s" % (s.get("src"), s.get("dest")))
    for pos, (name, ref, s) in enumerate(fwds):
        src, dst = str(s.get("src") or ""), str(s.get("dest") or "")
        if src not in topo.zones or dst not in topo.zones:
            continue
        pols.append({"id": policy_id("fwd:" + ref), "name": "%s → %s" % (src, dst), "src": {"zone": zone_id(src)},
                     "dst": {"zone": zone_id(dst)}, "action": "allow", "proto": "any", "ports": "", "return_traffic": True,
                     "enabled": not flag(s.get("disabled")) and flag(s.get("enabled"), True), "order": FORWARD_ORDER + pos,
                     "scheduled": False, "states": [], "builtin": True, "editable": False, "vendor_ref": "fwd:" + ref,
                     "own": False, "ipv6": False, "kind": "forwarding"})
    for pos, (zname, z) in enumerate(topo.zones.items()):
        verdict = TARGETS.get(z["input"] or topo.defaults.get("input") or "")
        if verdict:
            pols.append({"id": policy_id("input:" + zname), "name": "%s → router" % zname, "src": {"zone": zone_id(zname)},
                         "dst": {"zone": zone_id(SELF)}, "action": verdict, "proto": "any", "ports": "",
                         "return_traffic": True, "enabled": z["enabled"], "order": INPUT_ORDER + pos, "scheduled": False,
                         "states": [], "builtin": True, "editable": False, "vendor_ref": "input:" + zname, "own": False,
                         "ipv6": False, "kind": "zone_input"})
    return pols, marks


# ---------------------------------------------------------------------------- forwards, reservations, wireless, clients
def _forwards(raw, topo: Topology) -> tuple:
    out, other = [], []
    reds = stable_refs(sections(raw.get("firewall"), "redirect"), lambda s: text(s.get("name"), 96) or "?")
    for pos, (_, name, s) in enumerate(reds):
        target = str(s.get("target") or "DNAT").upper()
        title = text(s.get("name"), 96) or "#%d" % (pos + 1)
        if target != "DNAT":
            other.append({"id": oid("x", "redir" + name), "area": "nat", "editable": False, "title": title,
                          "enabled": flag(s.get("enabled"), True)})
            continue
        proto = proto_key(s.get("proto"))
        wan_port = ports_text(" ".join(items(s.get("src_dport")))) if s.get("src_dport") else ""
        dst_port = ports_text(" ".join(items(s.get("dest_port")))) if s.get("dest_port") else ""
        dst_ip = ip_of(s.get("dest_ip")) or text(s.get("dest_ip"), 48)
        out.append({"id": oid("f", name), "name": title, "proto": PROTOS.get(proto, "tcp_udp") if proto != "all" else "tcp_udp",
                    "wan_port": wan_port or None, "dst_ip": dst_ip, "dst_port": dst_port or wan_port or None,
                    "enabled": flag(s.get("enabled"), True), "source_limited": bool(items(s.get("src_ip"))),
                    "own": str(s.get("name") or "").startswith(OWN_MARK), "order": pos,
                    "src_zone": zone_id(s.get("src")) if s.get("src") else None})
    return out, other


def _reservations(raw, nets) -> list:
    by_cidr = [(ipaddress.ip_network(n["cidr"]), n["id"]) for n in nets if n.get("cidr")]
    out = []
    for name, s in sections(raw.get("dhcp"), "host"):
        ip = ip_of(s.get("ip"))
        macs = [norm_mac(m) for m in items(s.get("mac"))]
        macs = [m for m in macs if m]
        if not ip or not macs:
            continue
        a = ipaddress.ip_address(ip)
        out.append({"id": oid("r", "%s|%s" % (mac_hash(macs[0]), ip)), "client": oid("c", macs[0]),
                    "name": text(s.get("name"), 64), "ip": ip,
                    "network": next((nid for n, nid in by_cidr if a.version == n.version and a in n), None)})
    return out


def _security(enc: str) -> str:
    e = str(enc or "none").strip().lower()
    base = e.split("+")[0]
    if base in SECURITY:
        return SECURITY[base]
    if base.startswith(("psk", "sae")):
        return "wpa_personal"
    if base.startswith(("wpa", "wpa2", "wpa3")):
        return "wpa_enterprise"
    return "other"


def _band_of(dev: dict):
    for k in ("band", "hwmode"):
        v = str(dev.get(k) or "").strip().lower()
        if v in BANDS and BANDS[v]:
            return BANDS[v]
    ch = _int(dev.get("channel"), 0)
    if 1 <= ch <= 14:
        return "2.4ghz"
    if ch >= 32:
        return "5ghz"
    return None


def _on_air(raw) -> dict:
    """(ssid, band) and (ssid, None) -> {"clients": n, "bands": set} from the live wireless state."""
    out = {}
    air = raw.get("air") if isinstance(raw.get("air"), dict) else {}
    for ifname, w in list(air.items())[:64]:
        info = w.get("info") if isinstance(w, dict) and isinstance(w.get("info"), dict) else {}
        ssid = text(info.get("ssid"), 32)
        if not ssid:
            continue
        freq = _int(info.get("frequency"), 0)
        band = "2.4ghz" if 2400 <= freq < 2500 else "5ghz" if 5000 <= freq < 5900 else "6ghz" if 5925 <= freq < 7200 else None
        n = len([r for r in (w.get("assoc") or []) if isinstance(r, dict)])
        for key in ((ssid, band), (ssid, None)):
            o = out.setdefault(key, {"clients": 0, "bands": set()})
            o["clients"] += n
            if band:
                o["bands"].add(band)
    return out


def _ssids(raw, nets) -> list:
    radios = {name: s for name, s in sections(raw.get("wireless"), RADIO_SECTION)}
    by_iface = {n["interface"]: n["id"] for n in nets if n.get("interface")}
    air = _on_air(raw)
    out = []
    ifaces = stable_refs(sections(raw.get("wireless"), AP_SECTION), lambda s: "%s|%s" % (s.get("device"), s.get("ssid")))
    for _, name, s in ifaces:
        mode = str(s.get("mode") or "ap").lower()
        ssid = text(s.get("ssid"), 32)
        if mode != "ap" or not ssid:
            continue
        radio = radios.get(str(s.get("device") or "")) or {}
        band = _band_of(radio)
        live = air.get((ssid, band)) if band else air.get((ssid, None))
        nw = next((by_iface[n] for n in items(s.get("network")) if n in by_iface), None)
        enabled = not flag(s.get("disabled")) and not flag(radio.get("disabled"))
        out.append({"id": oid("s", name), "name": ssid, "network": nw, "bands": [band] if band else sorted((live or {}).get("bands") or []),
                    "security": _security(s.get("encryption")), "hidden": flag(s.get("hidden")), "enabled": enabled,
                    "guest": flag(s.get("isolate")), "radio": text(s.get("device"), 32),
                    "channel": text(radio.get("channel"), 8), "on_air": bool(live) if raw.get("air") is not None else None,
                    "clients": live["clients"] if live else (0 if raw.get("air") is not None else None),
                    "wpa3": str(s.get("encryption") or "").lower().startswith("sae")})
    return out


def router_mac(raw, topo: Topology):
    """The router's own hardware address: the device of its first inside interface."""
    devs = raw.get("devices") if isinstance(raw.get("devices"), dict) else {}
    order = sorted(topo.ifaces, key=lambda n: (topo.is_external(topo.zone_of_iface.get(n)), n != "lan", n))
    for n in order:
        dev = topo.ifaces[n]["device"]
        m = norm_mac((devs.get(dev) or {}).get("macaddr")) if dev else None
        if m:
            return m
    return None


def _wireless_clients(raw) -> dict:
    """hardware address -> {ssid, band, signal} of every associated wireless client."""
    out = {}
    air = raw.get("air") if isinstance(raw.get("air"), dict) else {}
    for ifname, w in list(air.items())[:64]:
        if not isinstance(w, dict):
            continue
        info = w.get("info") if isinstance(w.get("info"), dict) else {}
        freq = _int(info.get("frequency"), 0)
        band = "2.4ghz" if 2400 <= freq < 2500 else "5ghz" if 5000 <= freq < 5900 else "6ghz" if 5925 <= freq < 7200 else None
        for r in (w.get("assoc") or [])[:MAX_ITEMS]:
            m = norm_mac(r.get("mac")) if isinstance(r, dict) else None
            if m:
                out[m] = {"ssid": text(info.get("ssid"), 32), "band": band,
                          "signal": r.get("signal") if isinstance(r.get("signal"), int) else None}
    return out


def _clients(raw, nets, reservations) -> list:
    by_cidr = [(ipaddress.ip_network(n["cidr"]), n["id"]) for n in nets if n.get("cidr")]
    fixed = {r["client"]: r["ip"] for r in reservations}
    radio = _wireless_clients(raw)
    known = {}
    leases = raw.get("leases") if isinstance(raw.get("leases"), dict) else {}
    for r in (leases.get("dhcp_leases") or [])[:MAX_ITEMS]:
        m = norm_mac(r.get("macaddr")) if isinstance(r, dict) else None
        if m:
            k = known.setdefault(m, {"ip": None, "name": None, "lease": False})
            k["ip"] = k["ip"] or ip_of(r.get("ipaddr"))
            k["name"] = k["name"] or text(r.get("hostname"), 64)
            k["lease"] = True
    hints = raw.get("hints") if isinstance(raw.get("hints"), dict) else {}
    for mac, h in list(hints.items())[:MAX_ITEMS]:
        m = norm_mac(mac)
        if m and isinstance(h, dict):
            k = known.setdefault(m, {"ip": None, "name": None, "lease": False})
            ips = [ip_of(a) for a in h.get("ipaddrs") or [] if ip_of(a)]
            k["ip"] = k["ip"] or (ips[0] if ips else None)
            k["name"] = k["name"] or text(h.get("name"), 64)
    for m in radio:
        known.setdefault(m, {"ip": None, "name": None, "lease": False})
    air_read = raw.get("air") is not None
    out = []
    for m, k in sorted(known.items())[:MAX_ITEMS]:
        cid = oid("c", m)
        ip = k["ip"]
        net = None
        if ip:
            a = ipaddress.ip_address(ip)
            net = next((nid for n, nid in by_cidr if a.version == n.version and a in n), None)
        r = radio.get(m)
        out.append({"id": cid, "name": k["name"] or "?", "kind": "client", "network": net, "ip": ip,
                    "fixed": cid in fixed, "fixed_ip": fixed.get(cid), "wired": (r is None) if air_read else None,
                    "alone_on_port": False, "private_address": private_mac(m), "protected": None,
                    "parent": oid("e", "router") if r else None, "ssid": r["ssid"] if r else None,
                    "band": r["band"] if r else None, "blocked": False, "online": bool(r or k["lease"]),
                    "network_by_port": False})
    return out


def _equipment(raw, topo: Topology) -> list:
    b = raw.get("board") if isinstance(raw.get("board"), dict) else {}
    rel = b.get("release") if isinstance(b.get("release"), dict) else {}
    if not b and not topo.ifaces:
        return []
    lan = next((i["addr"] for n, i in topo.ifaces.items() if i["addr"] and not topo.is_external(topo.zone_of_iface.get(n))), None)
    wan = any(z["external"] for z in topo.zones.values())
    has_radio = bool(sections(raw.get("wireless"), RADIO_SECTION))
    return [{"id": oid("e", "router"), "name": text(b.get("hostname"), 48) or "Router",
             "kind": "router" if wan or not has_radio else "ap", "model": text(b.get("model"), 64),
             "firmware": text(rel.get("version"), 24), "mgmt_ip": lan, "uplink": None,
             "target": text(rel.get("target"), 32)}]


def _raw_extra(raw, topo: Topology, marks, other_nat) -> list:
    out = list(other_nat)
    for name, parts in marks:
        out.append({"id": oid("x", "rule" + name), "area": "rule", "editable": False,
                    "title": display_name(parts, name), "enabled": parts["enabled"]})
    for _, name, s in stable_refs(sections(raw.get("firewall"), "include"), lambda s: s.get("path")):
        out.append({"id": oid("x", "inc" + name), "area": "include", "editable": False,
                    "title": text(s.get("path"), 96) or name, "enabled": flag(s.get("enabled"), True)})
    for _, name, s in stable_refs(sections(raw.get("network"), "bridge-vlan"), lambda s: "%s|%s" % (s.get("device"), s.get("vlan"))):
        vid = _int(s.get("vlan"), 0)
        if 1 <= vid <= 4094:
            out.append({"id": oid("x", "bv" + name), "area": "vlan", "editable": False,
                        "title": "VLAN %d (%s)" % (vid, text(s.get("device"), 24) or "?")})
    for _, name, s in stable_refs(sections(raw.get("network"), "route"), lambda s: s.get("target")):
        target = text(s.get("target"), 48)
        if target:
            out.append({"id": oid("x", "route" + name), "area": "route", "editable": False,
                        "title": "%s via %s" % (target, text(s.get("gateway"), 48) or text(s.get("interface"), 32) or "?"),
                        "enabled": not flag(s.get("disabled"))})
    return out


# ---------------------------------------------------------------------------- plain checks
def findings(state: dict, topo: Topology) -> list:
    out = []
    zones = {z["id"]: z for z in state.get("zones") or []}
    wan = {zid for zid, z in zones.items() if z.get("system") == "external"}
    self_z = zone_id(SELF)
    admin = {}
    seen = set()
    for p in sorted(state.get("policies") or [], key=lambda p: p.get("order", 0)):
        if not p.get("enabled") or p.get("action") != "allow":
            continue
        s, d = p["src"], p["dst"]
        open_service = p.get("proto") == "any" and not p.get("ports")
        if (s["zone"] in wan or s["zone"] == ANY) and d["zone"] in (self_z, ANY) and not s.get("match"):
            hit = [ADMIN_PORTS[x] for x in ADMIN_PORTS
                   if open_service or (p.get("proto") in ("tcp", "tcp_udp") and x in str(p.get("ports") or "").split(","))]
            for z in ([s["zone"]] if s["zone"] in wan else sorted(wan)):
                if hit:
                    admin.setdefault(z, set()).update(hit)
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
        z["admin_services"] = sorted(x.lower() for x in admin[zid])
        out.insert(0, {"id": oid("g", "admin" + zid), "kind": "admin_on_wan", "level": "alert", "subject": zid,
                       "params": {"zone": z["name"], "services": ", ".join(sorted(admin[zid]))}})
    if topo.defaults.get("forward") == "ACCEPT" and wan:
        out.append({"id": oid("g", "fwdaccept"), "kind": "forward_accept", "level": "warn", "subject": None, "params": {}})
    off = sorted({p["vendor_ref"] for p in state.get("policies") or [] if not p.get("enabled") and not p.get("builtin")})
    if off:
        out.append({"id": oid("g", "disabled"), "kind": "rules_disabled", "level": "info", "subject": None,
                    "params": {"n": len(off)}})
    for s in state.get("ssids") or []:
        if s.get("security") == "open" and s.get("enabled"):
            out.append({"id": oid("g", "open" + s["id"]), "kind": "ssid_open", "level": "info" if s.get("guest") else "warn",
                        "subject": s["id"], "params": {"ssid": s["name"]}})
    for w in state.get("wans") or []:
        if w.get("enabled") and w.get("status") == "down" and w.get("type") not in IPV6_ONLY:
            out.append({"id": oid("g", "wandown" + w["id"]), "kind": "wan_down", "level": "alert", "subject": None,
                        "params": {"wan": w["name"]}})
    return out


def rule_digest(pols) -> list:
    """One short line per rule (name, on/off, fingerprint of what decides its effect) for the night report. Zone
    forwardings and input policies count as rules of their own."""
    res, names = [], {}
    for p in pols:
        names[p["name"]] = names.get(p["name"], 0) + 1
    seen = {}
    for p in pols:
        name = p["name"]
        if names[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            name = "%s (%d)" % (name, seen[name])
        sig = repr((p["src"], p["dst"], p["action"], p["proto"], p["ports"], p.get("scheduled")))
        res.append({"name": name, "enabled": p["enabled"], "allow": p["action"] == "allow",
                    "sig": hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]})
    return res


# ---------------------------------------------------------------------------- the whole state
def to_neutral(raw: dict) -> dict:
    """Neutral model v2 from whatever replies are present. Raises nothing for missing ones (they stay empty)."""
    raw = raw if isinstance(raw, dict) else {}
    topo = Topology(raw)
    zones = _zones(topo)
    nets, wans = _networks(raw, topo)
    pols, marks = _policies(raw, topo)
    known = {z["id"] for z in zones}
    for p in pols:                                   # a rule may name a zone that does not exist (any more)
        for side in (p["src"], p["dst"]):
            if side["zone"] != ANY and side["zone"] not in known:
                zones.append({"id": side["zone"], "name": "?", "kind": None, "system": None, "builtin": False,
                              "interface": False, "admin_services": [], "description": None})
                known.add(side["zone"])
                p["editable"] = False
    fwds, other_nat = _forwards(raw, topo)
    reservations = _reservations(raw, nets)
    ssids = _ssids(raw, nets)
    state = {"v": 2, "zones": zones, "networks": nets, "devices": _clients(raw, nets, reservations),
             "equipment": _equipment(raw, topo), "ports": [], "policies": pols, "ssids": ssids,
             "dhcp_reservations": reservations, "port_forwards": fwds, "wans": wans, "vpn": [],
             "raw": _raw_extra(raw, topo, marks, other_nat)}
    default = "allow" if topo.defaults.get("forward") == "ACCEPT" else "block"
    state["allow"] = neutral.derive_allow(state, default=default) if pols or zones else []
    external = {z["id"] for z in zones if z.get("system") == "external"}
    for z in zones:
        z["internet"] = z.get("system") == "external" or any(a == z["id"] and b in external for a, b in state["allow"])
        z["discovery"] = False
    state["findings"] = findings(state, topo)
    state["rule_digest"] = rule_digest([p for p in pols if p.get("kind") != "zone_input"] +
                                       [p for p in pols if p.get("kind") == "zone_input"])
    ver = parse_version(raw.get("version"))
    vtext = "snapshot" if ver == (99, 0) else ("%d.%02d" % ver if ver else (text(raw.get("version"), 24) or ""))
    state["controller"] = {"version": vtext, "apis": ["ubus"], "native_rollback": native_rollback(raw.get("version")),
                           "firewall": firewall_generation(raw.get("version")) or None, "class": "router"}
    return state
