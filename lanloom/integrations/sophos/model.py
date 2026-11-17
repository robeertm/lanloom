"""Translation module of the Sophos Firewall family: XML API replies -> neutral model v2.

Pure functions, no I/O. Shared by the read half and (later) a write half, so both compute the same neutral state.

``parse_reply(bytes)`` turns one reply document of the XML API into ``(meta, entities)``: ``meta`` holds the API
version, the sign-in result and a top-level status; ``entities`` maps an entity name (``Zone``, ``Interface``,
``FirewallRule``, ...) to a list of plain dicts (child tag -> text, nested dict or list when a tag repeats).
``to_neutral(raw)`` takes ``{"api_version": "...", "entities": {...}}`` and builds the neutral model::

    zones          Zone                       name, type (LAN, WAN, DMZ, VPN, WiFi, ...), services on the firewall
    networks       Interface, VLAN, Alias      static address -> network; zone; VLAN id; DHCP server present
    policies       FirewallRule (+ group)     one neutral policy per source zone x destination zone of a rule
    port_forwards  NATRule                    rules that translate the destination (and old DNAT rules)
    dhcp_reservations DHCPServer              fixed addresses (host name and address; the hardware address only hashed)
    wans           Interface in a WAN zone    name, address assignment
    vpn            VPNIPSecConnection, SSLVPNPolicy
    raw            source NAT, rule groups, static routes, other policy types (shown, never written)
    rule_digest    one line per rule (name, on/off, fingerprint) for the night report
    findings       plain checks: management services open on a WAN zone, rules from a WAN zone to inside without a
                   service limit, enabled rules from any zone to any zone with every service

Never copied: passwords, pre-shared keys, certificates, user lists. Some replies carry such values (the pre-shared key of an
IPsec connection, the PPPoE password of an interface, depending on the release encrypted or not); only the fields
named in this module are taken, the rest of the reply is dropped with it. Neutral ids are opaque hashes of names; a
hardware address only ever appears hashed.
"""
import hashlib
import ipaddress
import re
import xml.etree.ElementTree as ET

from ...firewall import neutral

SALT = "lanloom-sfw:"
MAX_ITEMS = 5000
MAX_PAIRS = 24                 # neutral policies made from one rule (source zones x destination zones)
TEXT_MAX = 96
ANY = "*"

ACTIONS = {"accept": "allow", "drop": "block", "reject": "reject"}
ZONE_SYSTEM = {"lan": "internal", "wan": "external", "dmz": "dmz", "vpn": "vpn", "wifi": "internal", "localhost": "gateway"}
BUILTIN_ZONES = {"lan", "wan", "dmz", "vpn", "wifi", "localhost"}
ADMIN_SERVICES = ("HTTPS", "SSH", "Telnet")       # management of the firewall itself; never wanted on a WAN zone
ALL_TIMES = {"", "all the time", "all time", "none"}
ANY_WORDS = {"", "any", "none"}


class ReplyError(ValueError):
    """The reply is not an XML API document Lanloom can read."""


# ---------------------------------------------------------------------------- reply document
def _plain(el):
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    out = {}
    for k in kids:
        v = _plain(k)
        if k.tag in out:
            if not isinstance(out[k.tag], list):
                out[k.tag] = [out[k.tag]]
            out[k.tag].append(v)
        else:
            out[k.tag] = v
    return out


def parse_reply(data: bytes) -> tuple:
    """(meta, entities) of one reply. Documents with a type declaration or entities are refused unread."""
    if not isinstance(data, (bytes, bytearray)) or not data.strip():
        raise ReplyError("empty reply")
    head = bytes(data[:4096]).upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in bytes(data).upper():
        raise ReplyError("declarations are not accepted")
    try:
        root = ET.fromstring(bytes(data))
    except ET.ParseError:
        raise ReplyError("reply is not XML") from None
    if root.tag != "Response":
        raise ReplyError("unexpected document")
    meta = {"api_version": text(root.get("APIVersion"), 24) or "", "login": None, "status": None, "status_code": None}
    entities = {}
    for child in root:
        if child.tag == "Login":
            meta["login"] = text("".join(child.itertext()), 80) or ""
            continue
        if child.tag == "Status":
            meta["status"] = text("".join(child.itertext()), 160) or ""
            meta["status_code"] = text(child.get("code"), 8)
            continue
        value = _plain(child)
        if isinstance(value, dict) and set(value) == {"Status"} and len(list(child)) == 1:
            meta.setdefault("entity_status", {})[child.tag] = text(child[0].get("code"), 8) or "?"
            continue                                     # "no record" or "not allowed" for this entity
        bucket = entities.setdefault(child.tag, [])
        if isinstance(value, dict) and len(bucket) < MAX_ITEMS:
            bucket.append(value)
    return meta, entities


# ---------------------------------------------------------------------------- small helpers
def oid(prefix: str, vendor_id) -> str:
    return prefix + hashlib.sha256((SALT + prefix + str(vendor_id)).encode("utf-8")).hexdigest()[:12]


def text(value, limit: int = TEXT_MAX):
    if value is None or isinstance(value, (dict, list)):
        return None
    out = "".join(ch for ch in str(value) if ch >= " ")[:limit].strip()
    return out or None


def many(d, *path) -> list:
    """Values at ``path`` inside a plain dict, always as a list (a single child is not a list in the reply)."""
    cur = [d]
    for key in path:
        nxt = []
        for c in cur:
            if isinstance(c, dict) and key in c:
                v = c[key]
                nxt.extend(v if isinstance(v, list) else [v])
        cur = nxt
    return cur


def names(d, *path) -> list:
    return [t for t in (text(v) for v in many(d, *path)) if t]


def one(d, *path):
    got = many(d, *path)
    return got[0] if got else None


def on(value) -> bool:
    return str(value or "").strip().lower() in ("enable", "enabled", "on", "yes", "true", "1")


def entities(raw, name) -> list:
    ents = raw.get("entities") if isinstance(raw, dict) else None
    got = ents.get(name) if isinstance(ents, dict) else None
    return [e for e in (got or [])[:MAX_ITEMS] if isinstance(e, dict)]


def _ip(value):
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def cidr_of(ip, mask):
    try:
        return str(ipaddress.ip_interface("%s/%s" % (str(ip).strip(), str(mask).strip())).network)
    except ValueError:
        return None


def _name_key(value) -> str:
    return str(value or "").strip().lower()


# ---------------------------------------------------------------------------- objects
class _Objects:
    """Address and service objects by name, to put rules in plain words (addresses, ports)."""

    def __init__(self, raw):
        self.hosts = {}
        for h in entities(raw, "IPHost"):
            n = text(h.get("Name"))
            if not n:
                continue
            kind = _name_key(h.get("HostType"))
            if kind == "network":
                value = cidr_of(h.get("IPAddress"), h.get("Subnet")) or text(h.get("IPAddress"))
            elif kind == "iprange":
                value = "%s-%s" % (text(h.get("StartIPAddress")) or "?", text(h.get("EndIPAddress")) or "?")
            elif kind == "iplist":
                value = text(h.get("ListOfIPAddresses"), 160)
            else:
                value = _ip(h.get("IPAddress")) or text(h.get("IPAddress"))
            self.hosts[n] = value
        for h in entities(raw, "FQDNHost"):
            if text(h.get("Name")):
                self.hosts[text(h.get("Name"))] = text(h.get("FQDN"), 120)
        self.groups = {text(g.get("Name")): names(g, "HostList", "Host") for g in entities(raw, "IPHostGroup") if text(g.get("Name"))}
        self.services = {}
        for s in entities(raw, "Services"):
            n = text(s.get("Name"))
            if not n:
                continue
            parts = []
            for d in many(s, "ServiceDetails", "ServiceDetail"):
                if not isinstance(d, dict):
                    continue
                proto = _name_key(d.get("Protocol") or d.get("ProtocolName") or s.get("Type"))
                port = text(d.get("DestinationPort"), 24) or ""
                if proto in ("tcp", "udp"):
                    parts.append((proto, "" if port in ("1:65535", "") else port))
                elif proto.startswith("icmp") or _name_key(s.get("Type")).startswith("icmp"):
                    parts.append(("icmp", ""))
                else:
                    parts.append(("other", text(d.get("ProtocolName") or d.get("Protocol"), 16) or ""))
            self.services[n] = parts
        self.service_groups = {text(g.get("Name")): names(g, "ServiceList", "Service") for g in entities(raw, "ServiceGroup")
                               if text(g.get("Name"))}

    def service_parts(self, service_names) -> tuple:
        """(proto, ports text) for a list of service names; ("any", "") when the list is empty or says any."""
        todo = [n for n in service_names if _name_key(n) not in ANY_WORDS]
        if not todo:
            return "any", ""
        parts, unknown, seen = [], [], set()
        while todo:
            n = todo.pop(0)
            if n in seen:
                continue
            seen.add(n)
            if n in self.service_groups:
                todo.extend(self.service_groups[n])
            elif n in self.services:
                parts.extend(self.services[n])
            else:
                unknown.append(n)
        protos = {p for p, _ in parts}
        ports = sorted({port for _, port in parts if port}, key=lambda x: (len(x), x))
        proto = protos.pop() if len(protos) == 1 and not unknown else "any"
        text_ports = ", ".join(ports + unknown)[:120]
        if not text_ports and parts:
            text_ports = ", ".join(sorted(seen))[:120] if proto == "any" and len(protos) > 1 else ""
        return proto, text_ports

    def address(self, name):
        if name in self.groups:
            return "%s (%d)" % (name, len(self.groups[name]))
        return self.hosts.get(name) or name


# ---------------------------------------------------------------------------- zones and networks
def _zones(raw) -> list:
    out, seen = [], set()
    for z in entities(raw, "Zone"):
        name = text(z.get("Name"), 48)
        if not name or _name_key(name) in seen:
            continue
        seen.add(_name_key(name))
        kind = _name_key(z.get("Type")) or _name_key(name)
        admin = []
        for svc in ADMIN_SERVICES:
            if many(z, "ApplianceAccess", "AdminServices", svc) and any(text(v) for v in many(z, "ApplianceAccess", "AdminServices", svc)):
                admin.append(svc.lower())
        out.append({"id": oid("z", _name_key(name)), "name": name, "kind": kind or None,
                    "system": ZONE_SYSTEM.get(kind), "builtin": _name_key(name) in BUILTIN_ZONES,
                    "admin_services": admin, "description": text(z.get("Description"), 120)})
    present = {_name_key(z["name"]) for z in out} | {z["kind"] for z in out if z["kind"]}
    for builtin in ("LAN", "WAN"):                       # always there on the firewall, even when a reply leaves them out
        if out and builtin.lower() not in present:
            out.append({"id": oid("z", builtin.lower()), "name": builtin, "kind": builtin.lower(),
                        "system": ZONE_SYSTEM[builtin.lower()], "builtin": True, "admin_services": [], "description": None})
    return out


def zone_id(name):
    key = _name_key(name)
    if key in ANY_WORDS:
        return ANY
    return oid("z", key)


def _iface_key(value) -> str:
    """Interface reference in other objects: ``Port1``, ``Port1 - 172.16.16.16`` or ``#Port1``."""
    return _name_key(str(value or "").lstrip("#").split(" - ")[0].split(":")[0])


def _networks(raw, zones) -> tuple:
    zone_by_name = {_name_key(z["name"]): z for z in zones}
    dhcp_on = set()
    for d in entities(raw, "DHCPServer"):
        if on(d.get("Status")) or d.get("Status") in (None, ""):
            dhcp_on.add(_iface_key(d.get("Interface")))
    nets, wans = [], []
    rows = [(i, "iface") for i in entities(raw, "Interface")] + [(v, "vlan") for v in entities(raw, "VLAN")] + \
           [(a, "alias") for a in entities(raw, "Alias")]
    seen = set()
    zone_of_iface = {_iface_key(i.get("Name") or i.get("Hardware")): text(i.get("NetworkZone") or i.get("Zone"), 48)
                     for i, k in rows if k != "alias"}
    for it, kind in rows:
        name = text(it.get("Name"), 48) or text(it.get("Hardware"), 48)
        if not name:
            continue
        zname = text(it.get("NetworkZone") or it.get("Zone"), 48) or ""
        if kind == "alias" and not zname:
            zname = zone_of_iface.get(_iface_key(it.get("Interface"))) or ""
        zone = zone_by_name.get(_name_key(zname))
        if not zone and zname and _name_key(zname) not in ANY_WORDS:
            zone = {"id": oid("z", _name_key(zname)), "name": zname, "system": ZONE_SYSTEM.get(_name_key(zname))}
        if kind != "alias" and not on(it.get("IPv4Configuration")) and it.get("IPv4Configuration") not in (None, ""):
            continue
        assign = _name_key(it.get("IPv4Assignment")) or ("static" if it.get("IPAddress") else "")
        ip = _ip(it.get("IPAddress"))
        cidr = cidr_of(ip, it.get("Netmask")) if ip and it.get("Netmask") else None
        nid = oid("n", _name_key(name))
        if nid in seen:
            continue
        seen.add(nid)
        vlan = None
        try:
            vlan = int(str(it.get("VLANID") or "").strip()) if it.get("VLANID") else None
        except ValueError:
            vlan = None
        is_wan = bool(zone) and zone.get("system") == "external"
        net = {"id": nid, "name": name, "zone": zone["id"] if zone else None, "cidr": None if is_wan else cidr, "vlan": vlan,
               "management": bool(zone) and not is_wan and "https" in (zone.get("admin_services") or []),
               "dhcp": _iface_key(name) in dhcp_on or _iface_key(it.get("Hardware")) in dhcp_on,
               "purpose": "wan" if is_wan else "lan", "gateway_ip": ip, "kind": kind,
               "assignment": assign or None}
        nets.append(net)
        if is_wan:
            wans.append({"id": nid, "name": name, "type": assign or None, "failover": None,
                         "enabled": _name_key(it.get("Status")) not in ("disabled", "disconnected") if it.get("Status") else True})
    return nets, wans


# ---------------------------------------------------------------------------- rules
def _side(objs, rule_part, nets_tag, key="Network") -> dict:
    items = [n for n in names(rule_part, nets_tag, key) if _name_key(n) not in ANY_WORDS]
    if not items:
        return {}
    return {"kind": "ip", "items": [objs.address(n) for n in items][:40]}


def _groups_of(raw) -> dict:
    out = {}
    for g in entities(raw, "FirewallRuleGroup"):
        gname = text(g.get("Name"))
        for r in names(g, "SecurityPolicyList", "SecurityPolicy"):
            out.setdefault(r, gname)
    return out


def _policies(raw, objs, zones) -> tuple:
    known = {z["id"] for z in zones}
    groups = _groups_of(raw)
    pols, other = [], []
    for pos, r in enumerate(entities(raw, "FirewallRule")):
        name = text(r.get("Name"), 96)
        if not name:
            continue
        ptype = _name_key(r.get("PolicyType")) or "network"
        part = r.get("NetworkPolicy") if isinstance(r.get("NetworkPolicy"), dict) else \
            r.get("UserPolicy") if isinstance(r.get("UserPolicy"), dict) else None
        enabled = on(r.get("Status"))
        if part is None:
            other.append({"id": oid("x", "rule" + name), "area": "rule_" + (re.sub(r"[^a-z]", "", ptype)[:20] or "other"),
                          "editable": False, "title": name, "enabled": enabled})
            continue
        action = ACTIONS.get(_name_key(part.get("Action")), "other")
        src_z = [zone_id(z) for z in names(part, "SourceZones", "Zone")] or [ANY]
        dst_z = [zone_id(z) for z in names(part, "DestinationZones", "Zone")] or [ANY]
        if ANY in src_z:
            src_z = [ANY]
        if ANY in dst_z:
            dst_z = [ANY]
        src_m = _side(objs, part, "SourceNetworks")
        dst_m = _side(objs, part, "DestinationNetworks")
        users = names(part, "Identity", "Member") if on(part.get("MatchIdentity")) else []
        if users and not src_m:
            src_m = {"kind": "other", "items": [], "count": len(users)}
        proto, ports = objs.service_parts(names(part, "Services", "Service"))
        sched = _name_key(part.get("Schedule"))
        pairs = [(a, b) for a in src_z for b in dst_z][:MAX_PAIRS]
        for a, b in pairs:
            pid = oid("p", "%s|%s|%s" % (name, a, b))
            src = {"zone": a if a == ANY or a in known else a}
            dst = {"zone": b}
            if src_m:
                src["match"] = dict(src_m)
            if dst_m:
                dst["match"] = dict(dst_m)
            pols.append({"id": pid, "name": name, "src": src, "dst": dst, "action": action, "proto": proto, "ports": ports,
                         "return_traffic": True, "enabled": enabled, "order": pos, "scheduled": sched not in ALL_TIMES,
                         "states": [], "builtin": False, "editable": False, "vendor_ref": name,
                         "group": groups.get(name), "ipv6": _name_key(r.get("IPFamily")) == "ipv6",
                         "log": on(part.get("LogTraffic")), "users": len(users) or None})
    return pols, other


def _forwards(raw, objs) -> tuple:
    out, other = [], []
    for pos, r in enumerate(entities(raw, "NATRule")):
        name = text(r.get("Name"), 96)
        if not name:
            continue
        dest = text(r.get("TranslatedDestination")) or ""
        enabled = on(r.get("Status"))
        if _name_key(dest) in ("original", "", "none"):
            other.append({"id": oid("x", "nat" + name), "area": "nat", "editable": False, "title": name, "enabled": enabled,
                          "source": text(r.get("TranslatedSource"), 48)})
            continue
        proto, wan_ports = objs.service_parts(names(r, "OriginalServices", "Service"))
        tsvc = text(r.get("TranslatedService")) or ""
        _, dst_ports = objs.service_parts([tsvc]) if _name_key(tsvc) not in ("original", "") else (proto, wan_ports)
        src_limited = bool([n for n in names(r, "OriginalSourceNetworks", "Network") if _name_key(n) not in ANY_WORDS])
        out.append({"id": oid("f", name), "name": name, "proto": proto if proto in ("tcp", "udp") else "tcp_udp" if proto == "any" else proto,
                    "wan_port": wan_ports or None, "dst_ip": objs.address(dest), "dst_port": dst_ports or None,
                    "enabled": enabled, "source_limited": src_limited, "order": pos})
    return out, other


def _reservations(raw) -> list:
    out = []
    for d in entities(raw, "DHCPServer"):          # StaticLease/Lease (API reference), StaticIPMapping/MACIPMapping (older exports)
        for m in many(d, "StaticLease", "Lease") + many(d, "StaticIPMapping", "MACIPMapping"):
            if not isinstance(m, dict):
                continue
            ip = _ip(m.get("IPAddress"))
            if not ip:
                continue
            out.append({"id": oid("r", "%s|%s" % (str(m.get("MACAddress") or "").lower(), ip)), "client": None,
                        "name": text(m.get("HostName"), 64), "ip": ip, "server": text(d.get("Name"), 48)})
    return out


def _active(status) -> bool:
    return _name_key(status) not in ("disable", "disabled", "inactive", "off", "no", "false", "0")


def _vpn(raw) -> list:
    out = []
    for v in entities(raw, "VPNIPSecConnection"):
        v = v.get("Configuration") if isinstance(v.get("Configuration"), dict) else v   # fields sit below <Configuration>
        name = text(v.get("Name"))
        if name:
            out.append({"id": oid("v", "ipsec" + name), "name": name, "kind": "ipsec",
                        "type": text(v.get("ConnectionType"), 24),
                        "enabled": _active(v.get("Status")) and _active(v.get("Active"))})
    for v in entities(raw, "SSLVPNPolicy"):
        v = v.get("TunnelPolicy") if isinstance(v.get("TunnelPolicy"), dict) else v      # fields sit below <TunnelPolicy>
        name = text(v.get("Name"))
        if name:
            out.append({"id": oid("v", "ssl" + name), "name": name, "kind": "ssl", "type": None, "enabled": True})
    return out


def _raw_extra(raw) -> list:
    out = []
    for g in entities(raw, "FirewallRuleGroup"):
        if text(g.get("Name")):
            out.append({"id": oid("x", "group" + text(g.get("Name"))), "area": "rule_group", "editable": False,
                        "title": text(g.get("Name")), "count": len(names(g, "SecurityPolicyList", "SecurityPolicy"))})
    for r in entities(raw, "UnicastRoute"):
        dest = cidr_of(r.get("DestinationIP"), r.get("Netmask")) or text(r.get("DestinationIP"))
        if dest:
            out.append({"id": oid("x", "route%s%s" % (dest, r.get("Gateway"))), "area": "route", "editable": False,
                        "title": "%s via %s" % (dest, text(r.get("Gateway"), 48) or "?"), "enabled": on(r.get("Status")) if r.get("Status") else True})
    return out


# ---------------------------------------------------------------------------- plain checks
def findings(state: dict) -> list:
    out = []
    zones = {z["id"]: z for z in state.get("zones") or []}
    wan = {zid for zid, z in zones.items() if z.get("system") == "external"}
    for zid in sorted(wan):
        z = zones[zid]
        if z.get("admin_services"):
            out.append({"id": oid("g", "admin" + zid), "kind": "admin_on_wan", "level": "alert", "subject": zid,
                        "params": {"zone": z["name"], "services": ", ".join(s.upper() for s in z["admin_services"])}})
    seen = set()
    for p in state.get("policies") or []:
        if not p.get("enabled") or p.get("action") != "allow" or p["vendor_ref"] in seen:
            continue
        s, d = p["src"], p["dst"]
        open_service = p.get("proto") == "any" and not p.get("ports")
        if s["zone"] in wan and d["zone"] not in wan and open_service and not d.get("match"):
            seen.add(p["vendor_ref"])
            out.append({"id": oid("g", "wanin" + p["vendor_ref"]), "kind": "wan_inbound_open", "level": "alert",
                        "subject": p["id"], "params": {"rule": p["name"], "zone": zones.get(d["zone"], {}).get("name") or "*"}})
        elif s["zone"] == ANY and d["zone"] == ANY and open_service and not s.get("match") and not d.get("match"):
            seen.add(p["vendor_ref"])
            out.append({"id": oid("g", "anyany" + p["vendor_ref"]), "kind": "any_any", "level": "warn", "subject": p["id"],
                        "params": {"rule": p["name"]}})
    off = sorted({p["vendor_ref"] for p in state.get("policies") or [] if not p.get("enabled")})
    if off:
        out.append({"id": oid("g", "disabled"), "kind": "rules_disabled", "level": "info", "subject": None,
                    "params": {"n": len(off)}})
    return out


def rule_digest(pols) -> list:
    """One short line per firewall rule (name, on/off, fingerprint of what decides its effect). Kept by the night
    report, which does not keep the full policy list, to tell added, removed, switched and changed rules."""
    out = {}
    for p in pols:
        ref = p["vendor_ref"]
        d = out.setdefault(ref, {"name": p["name"], "enabled": p["enabled"], "allow": p["action"] == "allow", "parts": []})
        d["parts"].append(repr((p["src"], p["dst"], p["action"], p["proto"], p["ports"], p["scheduled"])))
    return [{"name": d["name"], "enabled": d["enabled"], "allow": d["allow"],
             "sig": hashlib.sha256("\n".join(sorted(d["parts"])).encode("utf-8")).hexdigest()[:16]} for d in out.values()]


# ---------------------------------------------------------------------------- the whole state
def to_neutral(raw: dict) -> dict:
    """Neutral model v2 from whatever entities are present. Raises nothing for missing entities (they stay empty)."""
    raw = raw if isinstance(raw, dict) else {}
    objs = _Objects(raw)
    zones = _zones(raw)
    nets, wans = _networks(raw, zones)
    pols, other_rules = _policies(raw, objs, zones)
    fwd, other_nat = _forwards(raw, objs)
    known = {z["id"] for z in zones}
    for p in pols:                                      # a rule may name a zone the account could not list
        for side in (p["src"], p["dst"]):
            if side["zone"] != ANY and side["zone"] not in known:
                zones.append({"id": side["zone"], "name": "?", "kind": None, "system": None, "builtin": False,
                              "admin_services": [], "description": None})
                known.add(side["zone"])
    state = {"v": 2, "zones": zones, "networks": nets, "devices": [], "equipment": [], "ports": [], "policies": pols,
             "ssids": [], "dhcp_reservations": _reservations(raw), "port_forwards": fwd, "wans": wans, "vpn": _vpn(raw),
             "raw": other_rules + other_nat + _raw_extra(raw)}
    state["allow"] = neutral.derive_allow(state, default="block") if pols else []
    external = {z["id"] for z in zones if z.get("system") == "external"}
    for z in zones:
        z["internet"] = any(a == z["id"] and b in external for a, b in state["allow"])
        z["discovery"] = False
    state["findings"] = findings(state)
    state["rule_digest"] = rule_digest(pols)
    state["controller"] = {"version": text(raw.get("api_version"), 24) or "", "apis": ["xml"]}
    return state
