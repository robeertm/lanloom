"""Translation module of the UniFi family: controller objects -> neutral model v2 (and, later, back).

Pure functions, no I/O. Shared by the read half and (from stage B on) the write half, so app and executor compute
the same neutral state and the same plan fingerprint.

Input is a dict of raw replies keyed by area (whatever the read half could get; every key is optional)::

    official API (integration v1)       classic API (view-only account)
    info        {applicationVersion}    sysinfo      stat/sysinfo
    o_networks  networks (details)      networkconf  rest/networkconf
    o_zones     firewall/zones          zone         v2 firewall/zone
    o_policies  firewall/policies       policies     v2 firewall-policies
    o_wifi      wifi/broadcasts         wlanconf     rest/wlanconf
    o_devices   devices (details)       device       stat/device
    o_clients   clients                 sta          stat/sta
    o_wans      wans                    user         rest/user        (fixed addresses, names)
    o_vpn       vpn servers + tunnels   portconf     rest/portconf    (port profiles)
    o_acl       acl-rules               portforward  rest/portforward
    o_dns       dns/policies            firewallgroup rest/firewallgroup
                                        firewallrule rest/firewallrule (rules of releases before zones)

The official API wins wherever it has an area; the classic API fills what it lacks (hit counters, ports and port
profiles, port forwarding, fixed addresses, the port a client sits on). Classic objects carry the official id in
``external_id``; neutral ids are made from that id when it exists, so both APIs give the same neutral ids.

Never copied: passphrases and pre-shared keys, RADIUS settings, serial numbers, account data. Neutral ids are opaque
hashes; hardware addresses are only used for the topology observation (the core needs them to match devices).
"""
import copy
import hashlib
import ipaddress
import re

from ...firewall import neutral
from ...sources._ctlmap import _unique, map_client, map_device, norm_mac

SALT = "lanloom-unifi:"
MAX_ITEMS = 5000
TEXT_MAX = 96

ACTIONS = {"ALLOW": "allow", "BLOCK": "block", "REJECT": "reject"}
TARGETS = {"ANY": "any", "NETWORK": "network", "IP": "ip", "IP_ADDRESS": "ip", "CLIENT": "client", "MAC": "client",
           "MAC_ADDRESS": "client", "REGION": "region", "APP": "app", "APPLICATION": "app", "APP_CATEGORY": "app",
           "APPLICATION_CATEGORY": "app", "DOMAIN": "domain", "WEB": "domain", "PORT": "any", "IID": "ip",
           "IPV6_IID": "ip", "VPN_SERVER": "vpn", "SITE_TO_SITE_VPN_TUNNEL": "vpn"}
PURPOSES = {"corporate": "lan", "guest": "guest", "vlan-only": "lan", "wan": "wan", "remote-user-vpn": "vpn",
            "site-vpn": "vpn", "vpn-client": "vpn"}
BANDS = {"2g": "2.4ghz", "5g": "5ghz", "6g": "6ghz", "2.4": "2.4ghz", "5": "5ghz", "6": "6ghz"}
SECURITY = {"open": "open", "wep": "wep", "wpapsk": "wpa_personal", "wpaeap": "wpa_enterprise"}
DEVICE_KINDS = {"usw": "switch", "uap": "ap", "ugw": "router", "udm": "router", "uxg": "router",  # identifier: device type codes
                "gateway": "router", "switching": "switch", "accessPoint": "ap"}
SYSTEM_ZONES = {"internal": "internal", "external": "external", "gateway": "gateway", "vpn": "vpn",
                "hotspot": "hotspot", "dmz": "dmz"}


# ---------------------------------------------------------------------------- small helpers
def oid(prefix: str, vendor_id) -> str:
    """Opaque, stable neutral id for a vendor object (never the hardware address itself)."""
    return prefix + hashlib.sha256((SALT + prefix + str(vendor_id)).encode("utf-8")).hexdigest()[:12]


def _d(value) -> dict:
    """A dict from an untrusted reply field (anything else counts as empty)."""
    return value if isinstance(value, dict) else {}


def text(value, limit: int = TEXT_MAX):
    if value is None or isinstance(value, (dict, list)):
        return None
    out = "".join(ch for ch in str(value) if ch >= " ")[:limit].strip()
    return out or None


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
    """The list inside a reply: classic ``{"meta", "data": [...]}``, official pages ``{"data": [...]}``, or a list."""
    value = raw.get(key) if isinstance(raw, dict) else None
    if isinstance(value, dict):
        value = value.get("data")
    if not isinstance(value, list):
        return []
    return [v for v in value[:MAX_ITEMS] if isinstance(v, dict)]


def cidr_of(subnet):
    try:
        return str(ipaddress.ip_interface(str(subnet).strip()).network)
    except ValueError:
        return None


def is_private_mac(mac) -> bool:
    m = norm_mac(mac)
    return bool(m) and bool(int(m[:2], 16) & 2)


def _ip(value):
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def version_tuple(version) -> tuple:
    parts = re.findall(r"\d+", str(version or ""))[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


# ---------------------------------------------------------------------------- id maps
class _Ids:
    """Classic ids -> official ids (``external_id``), so references resolve to the same neutral id."""

    def __init__(self, raw):
        self.net, self.zone = {}, {}
        for n in items(raw, "networkconf"):
            if n.get("_id"):
                self.net[n["_id"]] = n.get("external_id") or n["_id"]
        for z in items(raw, "zone"):
            if z.get("_id"):
                self.zone[z["_id"]] = z.get("external_id") or z["_id"]

    def net_id(self, classic_or_official):
        if not classic_or_official:
            return None
        return oid("n", self.net.get(classic_or_official, classic_or_official))

    def zone_id(self, classic_or_official):
        if not classic_or_official:
            return None
        return oid("z", self.zone.get(classic_or_official, classic_or_official))


# ---------------------------------------------------------------------------- networks
def _networks_classic(raw, ids) -> list:
    out = []
    for n in items(raw, "networkconf"):
        purpose = PURPOSES.get(str(n.get("purpose") or ""), "lan")
        vlan = num(n.get("vlan")) if n.get("vlan_enabled", True) is not False else None
        dhcp = None
        if purpose in ("lan", "guest"):
            dhcp = {"on": bool(n.get("dhcpd_enabled")), "range": [_ip(n.get("dhcpd_start")), _ip(n.get("dhcpd_stop"))]
                    if n.get("dhcpd_enabled") else None}
        out.append({"id": ids.net_id(n.get("_id")), "name": text(n.get("name")) or "?", "cidr": cidr_of(n.get("ip_subnet")),
                    "vlan": vlan if isinstance(vlan, int) and vlan > 0 else None, "zone": ids.zone_id(n.get("firewall_zone_id")),
                    "purpose": purpose, "dhcp": dhcp, "internet": n.get("internet_access_enabled") is not False,
                    "enabled": n.get("enabled") is not False, "isolated": bool(n.get("network_isolation_enabled")),
                    "gateway_ip": str(ipaddress.ip_interface(n["ip_subnet"]).ip) if cidr_of(n.get("ip_subnet")) else None,
                    "management": False})
    return out


def _networks_official(raw) -> list:
    out = []
    for n in items(raw, "o_networks"):
        v4 = n.get("ipv4Configuration") if isinstance(n.get("ipv4Configuration"), dict) else {}
        cidr = gw = None
        if v4.get("hostIpAddress") and isinstance(v4.get("prefixLength"), int):
            cidr = cidr_of("%s/%d" % (v4["hostIpAddress"], v4["prefixLength"]))
            gw = _ip(v4["hostIpAddress"])
        dh = v4.get("dhcpConfiguration") if isinstance(v4.get("dhcpConfiguration"), dict) else {}
        rng = dh.get("ipAddressRange") if isinstance(dh.get("ipAddressRange"), dict) else {}
        mode = str(dh.get("mode") or "").upper()
        dhcp = {"on": mode == "SERVER", "range": [_ip(rng.get("start")), _ip(rng.get("end"))] if rng else None} if dh else None
        vlan = num(n.get("vlanId"))
        out.append({"id": oid("n", n.get("id")), "name": text(n.get("name")) or "?", "cidr": cidr,
                    "vlan": vlan if isinstance(vlan, int) and vlan > 1 else None,
                    "zone": oid("z", n["zoneId"]) if n.get("zoneId") else None,
                    "purpose": "lan", "dhcp": dhcp, "internet": n.get("internetAccessEnabled") is not False,
                    "enabled": n.get("enabled") is not False, "isolated": bool(n.get("isolationEnabled")),
                    "gateway_ip": gw, "management": False,
                    "managed_by": text(n.get("management"), 16)})
    return out


def _merge_by_id(primary: list, secondary: list, fill=()) -> list:
    """Objects of the preferred API; fields listed in ``fill`` come from the other API when missing; objects only
    the other API knows are kept as they are."""
    sec = {o["id"]: o for o in secondary}
    out = []
    for o in primary:
        other = sec.pop(o["id"], None)
        if other:
            o = dict(o)
            for k in fill:
                if o.get(k) in (None, "", [], "lan") and other.get(k) not in (None, "", []):
                    o[k] = other[k]
        out.append(o)
    return out + list(sec.values())


def _mark_management(networks: list, infra_ips: list) -> None:
    """The management network is the one that holds the addresses of the network equipment (most of them)."""
    best, best_n = None, 0
    for n in networks:
        if not n.get("cidr"):
            continue
        net = ipaddress.ip_network(n["cidr"])
        hits = sum(1 for ip in infra_ips if ip and ipaddress.ip_address(ip) in net)
        if hits > best_n:
            best, best_n = n, hits
    if best is not None:
        best["management"] = True


# ---------------------------------------------------------------------------- zones and policies
def _zone_kind(name: str, builtin: bool):
    return SYSTEM_ZONES.get(str(name or "").strip().lower()) if builtin else None


def _zones_classic(raw, ids) -> list:
    out = []
    for z in items(raw, "zone"):
        builtin = bool(z.get("default_zone")) or bool(z.get("attr_no_edit"))
        out.append({"id": ids.zone_id(z.get("_id")), "name": text(z.get("name")) or "?", "builtin": builtin,
                    "system": _zone_kind(z.get("name"), builtin), "editable": not z.get("attr_no_edit"),
                    "networks": sorted({ids.net_id(n) for n in z.get("network_ids") or [] if n})})
    return out


def _zones_official(raw) -> list:
    out = []
    for z in items(raw, "o_zones"):
        origin = str(_d(z.get("metadata")).get("origin") or "")
        builtin = origin == "SYSTEM_DEFINED"
        out.append({"id": oid("z", z.get("id")), "name": text(z.get("name")) or "?", "builtin": builtin,
                    "system": _zone_kind(z.get("name"), builtin), "editable": origin == "USER_DEFINED",
                    "networks": sorted({oid("n", n) for n in z.get("networkIds") or [] if n})})
    return out


def _groups(raw) -> dict:
    out = {}
    for g in items(raw, "firewallgroup"):
        members = [text(m, 48) for m in g.get("group_members") or [] if text(m, 48)][:64]
        out[g.get("_id")] = {"name": text(g.get("name")), "type": text(g.get("group_type"), 24), "members": members}
    return out


def _side_classic(s, ids, groups) -> tuple:
    """(neutral side, ports text) of one classic policy side."""
    s = s if isinstance(s, dict) else {}
    side = {"zone": ids.zone_id(s.get("zone_id"))}
    kind = TARGETS.get(str(s.get("matching_target") or "ANY").upper(), "other")
    if kind != "any":
        match = {"kind": kind}
        if kind == "network":
            match["items"] = sorted({ids.net_id(n) for n in s.get("network_ids") or [] if n})
            match["opposite"] = bool(s.get("match_opposite_networks"))
        elif kind == "ip":
            if s.get("ip_group_id") and s.get("ip_group_id") in groups:
                g = groups[s["ip_group_id"]]
                match["group"] = g["name"]
                match["items"] = g["members"]
            else:
                match["items"] = [text(i, 48) for i in s.get("ips") or [] if text(i, 48)][:64]
            match["opposite"] = bool(s.get("match_opposite_ips"))
        elif kind == "client":
            match["count"] = len(s.get("client_macs") or ([s["mac"]] if s.get("mac") else []))
            match["opposite"] = bool(s.get("match_opposite_clients") or s.get("match_opposite_macs"))
        elif kind == "region":
            match["items"] = [text(r, 4).upper() for r in s.get("regions") or [] if text(r, 4)][:300]
            match["opposite"] = bool(s.get("match_opposite_regions"))
        elif kind == "domain":
            match["items"] = [text(d, 120) for d in s.get("web_domains") or s.get("domains") or [] if text(d, 120)][:50]
        elif kind == "app":
            match["count"] = len(s.get("app_ids") or s.get("app_category_ids") or [])
        side["match"] = match
    ports = None
    pm = str(s.get("port_matching_type") or "ANY").upper()
    if pm == "SPECIFIC" and s.get("port"):
        ports = re.sub(r"[^0-9,\-]", "", str(s["port"]))[:120] or None
    elif pm == "OBJECT" and s.get("port_group_id") in groups:
        ports = ",".join(groups[s["port_group_id"]]["members"])[:120] or None
    if ports and s.get("match_opposite_ports"):
        ports = "!" + ports
    return side, ports


# a classic rule the write half can carry over unchanged in every field it does not touch (zone firewall stage): only
# these keys, each with a value Lanloom can express; anything else keeps the rule shown but read-only
POLICY_KEYS = frozenset(("_id", "action", "connection_state_type", "connection_states", "create_allow_respond", "destination",
                         "enabled", "icmp_typename", "icmp_v6_typename", "index", "ip_version", "logging", "match_ip_sec",
                         "match_opposite_protocol", "name", "predefined", "protocol", "schedule", "source"))
SIDE_KEYS = frozenset(("zone_id", "matching_target", "port_matching_type", "match_opposite_ports", "port", "network_ids",
                       "match_opposite_networks"))
WRITE_PROTOCOLS = {"all": "any", "tcp": "tcp", "udp": "udp", "tcp_udp": "tcp_udp"}


def _side_editable(s, dst: bool) -> bool:
    if not isinstance(s, dict) or set(s) - SIDE_KEYS or not s.get("zone_id"):
        return False
    target = str(s.get("matching_target") or "ANY").upper()
    if target == "NETWORK":
        if len(s.get("network_ids") or []) != 1 or s.get("match_opposite_networks"):
            return False
    elif target != "ANY":
        return False
    pm = str(s.get("port_matching_type") or "ANY").upper()
    if s.get("match_opposite_ports"):
        return False
    if pm == "SPECIFIC":
        return dst and bool(re.match(r"\A\d{1,5}(?:-\d{1,5})?(?:,\d{1,5}(?:-\d{1,5})?){0,31}\Z", str(s.get("port") or "")))
    return pm == "ANY" and not s.get("port")


def classic_editable(r) -> bool:
    """Can the write half change this classic rule without losing anything it does not understand?"""
    if not isinstance(r, dict) or set(r) - POLICY_KEYS or r.get("predefined"):
        return False
    if str(r.get("action") or "").upper() not in ("ALLOW", "BLOCK", "REJECT"):
        return False
    if str(_d(r.get("schedule")).get("mode") or "ALWAYS").upper() != "ALWAYS" or set(_d(r.get("schedule"))) - {"mode"}:
        return False
    if str(r.get("connection_state_type") or "ALL").upper() != "ALL" or r.get("connection_states"):
        return False
    if r.get("match_ip_sec") or r.get("match_opposite_protocol"):
        return False
    if str(r.get("protocol") or "all").lower() not in WRITE_PROTOCOLS:
        return False
    if str(r.get("ip_version") or "BOTH").upper() not in ("BOTH", "IPV4"):
        return False
    if str(r.get("icmp_typename") or "ANY").upper() != "ANY" or str(r.get("icmp_v6_typename") or "ANY").upper() != "ANY":
        return False
    return _side_editable(r.get("source"), False) and _side_editable(r.get("destination"), True)


def _proto(value, opposite=False) -> str:
    p = str(value or "all").lower()
    p = "any" if p in ("all", "any", "") else p
    return ("!" + p) if opposite and p != "any" else p


def _policies_classic(raw, ids) -> list:
    groups = _groups(raw)
    out = []
    for r in items(raw, "policies"):
        src, sports = _side_classic(r.get("source"), ids, groups)
        dst, dports = _side_classic(r.get("destination"), ids, groups)
        if sports:
            src["ports"] = sports
        editable = classic_editable(r)
        if editable:                    # one network of the zone: the plan step shape {"zone", "net"}
            for side, s in ((src, r.get("source")), (dst, r.get("destination"))):
                if side.get("match", {}).get("kind") == "network":
                    side.pop("match")
                    side["net"] = ids.net_id((s.get("network_ids") or [None])[0])
        hit = num(r.get("hits"))
        out.append({"id": oid("r", r.get("_id")), "name": text(r.get("name"), 120) or "?", "src": src, "dst": dst,
                    "action": ACTIONS.get(str(r.get("action") or "").upper(), "other"),
                    "proto": _proto(r.get("protocol"), r.get("match_opposite_protocol")), "ports": dports or "",
                    "ip_version": {"IPV4": "v4", "IPV6": "v6"}.get(str(r.get("ip_version") or "").upper(), "both"),
                    "return_traffic": bool(r.get("create_allow_respond")), "enabled": bool(r.get("enabled")),
                    "order": num(r.get("index")) or 0, "hits": hit if isinstance(hit, int) else None,
                    "last_hit": round(num(r.get("last_hit")) / 1000.0) if num(r.get("last_hit")) else None,
                    "logging": bool(r.get("logging")), "builtin": bool(r.get("predefined")),
                    "scheduled": str(_d(r.get("schedule")).get("mode") or "ALWAYS").upper() != "ALWAYS",
                    "states": sorted(str(x).lower() for x in r.get("connection_states") or [] if isinstance(x, str))[:4]
                    if str(r.get("connection_state_type") or "ALL").upper() != "ALL" else [],
                    "editable": editable, "vendor_ref": oid("v", r.get("_id"))})
    return out


def zone_refs(raw) -> dict:
    """Neutral zone id -> {"id": classic id, "key": system key or "", "builtin", "record": {"name", "network_ids"}}."""
    ids = _Ids(raw)
    out = {}
    for z in items(raw, "zone"):
        if z.get("_id"):
            builtin = bool(z.get("default_zone")) or bool(z.get("attr_no_edit"))
            out[ids.zone_id(z["_id"])] = {"id": str(z["_id"]), "key": str(z.get("zone_key") or _zone_kind(z.get("name"), builtin) or ""),
                                          "builtin": builtin,
                                          "record": {"name": z.get("name"), "network_ids": list(z.get("network_ids") or [])}}
    return out


def policy_refs(raw) -> dict:
    """Neutral rule id -> {"id": classic id, "record": the rule as stored now}."""
    out = {}
    for r in items(raw, "policies"):
        if r.get("_id"):
            out[oid("r", r["_id"])] = {"id": str(r["_id"]), "record": dict(r)}
    return out


def _filter_official(f) -> tuple:
    f = f if isinstance(f, dict) else {}
    kind = TARGETS.get(str(f.get("type") or "ANY").upper(), "other")
    match, ports = None, None
    if kind != "any" and f:
        match = {"kind": kind}
        if kind == "network":
            nf = _d(f.get("networkFilter"))
            match["items"] = sorted({oid("n", n) for n in nf.get("networkIds") or [] if n})
            match["opposite"] = bool(nf.get("matchOpposite"))
        elif kind == "ip":
            af = _d(f.get("ipAddressFilter"))
            vals = []
            for it in af.get("items") or []:
                if isinstance(it, dict):
                    v = it.get("value") or ("%s-%s" % (it.get("start"), it.get("stop")) if it.get("start") else None)
                    if text(v, 48):
                        vals.append(text(v, 48))
            match["items"] = vals[:64]
            if af.get("trafficMatchingListId"):
                match["group"] = "list"
            match["opposite"] = bool(af.get("matchOpposite"))
        elif kind == "client":
            mf = _d(f.get("macAddressFilter"))
            match["count"] = len(mf.get("macAddresses") or []) if isinstance(mf, dict) else 0
        elif kind == "region":
            rf = _d(f.get("regionFilter"))
            match["items"] = [text(r, 4).upper() for r in rf.get("regions") or [] if text(r, 4)][:300]
            match["opposite"] = bool(rf.get("matchOpposite"))
        elif kind == "domain":
            df = _d(f.get("domainFilter"))
            match["items"] = [text(d, 120) for d in df.get("domains") or [] if text(d, 120)][:50]
    pf = f.get("portFilter") if isinstance(f.get("portFilter"), dict) else None
    if pf:
        vals = []
        for it in pf.get("items") or []:
            if isinstance(it, dict):
                vals.append(str(it.get("value")) if it.get("value") is not None else "%s-%s" % (it.get("start"), it.get("stop")))
        ports = ",".join(vals)[:120] or ("list" if pf.get("trafficMatchingListId") else None)
        if ports and pf.get("matchOpposite"):
            ports = "!" + ports
    return match, ports


def _policies_official(raw) -> list:
    out = []
    for r in items(raw, "o_policies"):
        src_raw, dst_raw = _d(r.get("source")), _d(r.get("destination"))
        src = {"zone": oid("z", src_raw["firewallZoneId"]) if src_raw.get("firewallZoneId") else None}
        dst = {"zone": oid("z", dst_raw["firewallZoneId"]) if dst_raw.get("firewallZoneId") else None}
        sm, sports = _filter_official(src_raw.get("trafficFilter"))
        dm, dports = _filter_official(dst_raw.get("trafficFilter"))
        if sm:
            src["match"] = sm
        if sports:
            src["ports"] = sports
        if dm:
            dst["match"] = dm
        action = r.get("action") if isinstance(r.get("action"), dict) else {}
        scope = r.get("ipProtocolScope") if isinstance(r.get("ipProtocolScope"), dict) else {}
        pf = scope.get("protocolFilter") if isinstance(scope.get("protocolFilter"), dict) else {}
        proto = "any"
        if str(pf.get("type") or "").upper() == "NAMED_PROTOCOL":
            proto = _proto(_d(pf.get("protocol")).get("name"), pf.get("matchOpposite"))
        elif str(pf.get("type") or "").upper() == "PRESET":
            proto = "tcp_udp"
        elif str(pf.get("type") or "").upper() == "PROTOCOL_NUMBER":
            proto = "#%s" % pf.get("protocolNumber")
        origin = str(_d(r.get("metadata")).get("origin") or "")
        out.append({"id": oid("r", r.get("id")), "name": text(r.get("name"), 120) or "?", "src": src, "dst": dst,
                    "action": ACTIONS.get(str(action.get("type") or "").upper(), "other"), "proto": proto,
                    "ports": dports or "",
                    "ip_version": {"IPV4": "v4", "IPV6": "v6"}.get(str(scope.get("ipVersion") or "").upper(), "both"),
                    "return_traffic": bool(action.get("allowReturnTraffic")), "enabled": bool(r.get("enabled")),
                    "order": num(r.get("index")) or 0, "hits": None, "last_hit": None,
                    "logging": bool(r.get("loggingEnabled")), "builtin": origin == "SYSTEM_DEFINED",
                    "scheduled": str(_d(r.get("schedule")).get("mode") or "ALWAYS").upper() not in ("ALWAYS", ""),
                    "states": sorted(str(x).lower() for x in r.get("connectionStateFilter") or [] if isinstance(x, str))[:4],
                    "editable": False, "vendor_ref": oid("v", r.get("id"))})
    return out


def _legacy_rules(raw) -> list:
    """Rules of releases before zone-based firewalls: shown, never written."""
    out = []
    for r in items(raw, "firewallrule"):
        out.append({"id": oid("x", r.get("_id")), "area": "legacy_rule", "editable": False,
                    "title": text(r.get("name"), 120) or "?", "action": text(r.get("action"), 12),
                    "ruleset": text(r.get("ruleset"), 16), "enabled": bool(r.get("enabled")), "order": num(r.get("rule_index"))})
    return out


# ---------------------------------------------------------------------------- equipment, ports, clients
TAGGED_MODES = {"auto": "all", "block_all": "none", "custom": "custom"}
FORWARD_MODES = {"all": "all", "native": "none", "customize": "custom"}
PORT_KEYS = ("name", "portconf_id", "native_networkconf_id", "tagged_vlan_mgmt", "forward", "excluded_networkconf_ids",
             "poe_mode", "op_mode")


def default_network(nets_raw) -> str:
    """The classic id of the site's default network (the untagged network of a port that names none)."""
    for n in nets_raw:
        if str(n.get("attr_hidden_id") or "") == "LAN" and n.get("_id"):
            return str(n["_id"])
    for n in nets_raw:
        if n.get("attr_no_delete") and str(n.get("purpose") or "") == "corporate" and n.get("_id"):
            return str(n["_id"])
    return ""


def taggable(nets_raw) -> list:
    """Classic ids of the networks a switch port can carry tagged: local networks with a VLAN of their own."""
    return sorted(str(n["_id"]) for n in nets_raw if n.get("_id") and str(n.get("purpose") or "") in ("corporate", "guest", "vlan-only")
                  and n.get("vlan_enabled", True) is not False and num(n.get("vlan")))


def port_settings(pt: dict, override, profiles: dict, nets_raw, default_net: str) -> dict:
    """The settings of one switch port as the controller keeps them: the switch's override for the port first, then
    what the port table reports, then the port's profile, then the site's default network. ``tagged`` lists the classic
    ids of the networks carried tagged when the mode is "custom"."""
    o = override if isinstance(override, dict) else {}

    def get(key):
        if key in o:
            return o.get(key)
        return pt.get(key) if isinstance(pt, dict) else None
    pid = get("portconf_id") or ""
    prof = profiles.get(pid) or {}
    native = get("native_networkconf_id") or (prof.get("native_networkconf_id") if prof else None)
    mode = TAGGED_MODES.get(str(get("tagged_vlan_mgmt") or "")) or FORWARD_MODES.get(str(get("forward") or ""))
    if prof and not (get("tagged_vlan_mgmt") or get("forward")):
        mode = TAGGED_MODES.get(str(prof.get("tagged_vlan_mgmt") or "")) or FORWARD_MODES.get(str(prof.get("forward") or ""))
    if not native and mode in ("all", "none", "custom"):
        native = default_net or None
    tagged = []
    if mode == "custom":
        excl = set(get("excluded_networkconf_ids") or (prof.get("excluded_networkconf_ids") if prof else None) or [])
        tagged = [k for k in taggable(nets_raw) if k not in excl and k != native]
    op = str(get("op_mode") or "switch")
    if isinstance(pt, dict) and pt.get("aggregated_by"):
        op = "aggregate"
    poe_mode = get("poe_mode") if (isinstance(pt, dict) and pt.get("port_poe")) else None
    return {"name": get("name"), "portconf_id": str(pid) if pid else "", "native": str(native) if native else None,
            "mode": mode, "tagged": tagged, "op_mode": op if op in ("switch", "mirror", "aggregate") else op[:16],
            "poe_mode": str(poe_mode) if poe_mode else None}


def _wan_port(p: dict) -> bool:
    """A gateway port that carries an internet uplink (its network is a WAN, or it has an address of its own outside
    the local networks)."""
    name = str(p.get("network_name") or "").lower()
    return name.startswith("wan") or (bool(name) and name != "lan")


def _profiles_classic(raw, ids) -> list:
    nets_raw = items(raw, "networkconf")
    vlan_of = {n.get("_id"): num(n.get("vlan")) for n in nets_raw}
    out = []
    for pr in items(raw, "portconf"):
        if not pr.get("_id"):
            continue
        conf = port_settings({}, pr, {}, nets_raw, default_network(nets_raw))
        poe = pr.get("poe_mode")
        out.append({"id": oid("pp", pr["_id"]), "name": text(pr.get("name")) or "?",
                    "native_network": ids.net_id(conf["native"]) if conf["native"] else None,
                    "native_vlan": vlan_of.get(conf["native"]) if conf["native"] else None,
                    "tagged_mode": conf["mode"], "tagged": [ids.net_id(k) for k in conf["tagged"]],
                    "poe": None if poe in (None, "") else poe != "off"})
    return out


def _kind_classic(d) -> str:
    return DEVICE_KINDS.get(str(d.get("type") or ""), "other")


def _equipment_classic(raw) -> tuple:
    """(equipment, ports, mac -> equipment id)."""
    devs = items(raw, "device")
    by_mac = {norm_mac(d.get("mac")): oid("e", norm_mac(d.get("mac"))) for d in devs if norm_mac(d.get("mac"))}
    profiles = {p.get("_id"): p for p in items(raw, "portconf")}
    ids = _Ids(raw)
    nets_raw = items(raw, "networkconf")
    vlan_of = {n.get("_id"): num(n.get("vlan")) for n in nets_raw}
    default_net = default_network(nets_raw)
    eq, ports = [], []
    for d in devs:
        mac = norm_mac(d.get("mac"))
        if not mac:
            continue
        up = d.get("uplink") if isinstance(d.get("uplink"), dict) else (d.get("last_uplink") if isinstance(d.get("last_uplink"), dict) else {})
        umac = norm_mac(up.get("uplink_mac"))
        stats = d.get("system-stats") if isinstance(d.get("system-stats"), dict) else {}
        eid = by_mac[mac]
        own_up = num(up.get("port_idx"))
        eq.append({"id": eid, "name": text(d.get("name")) or text(d.get("model")) or "?", "kind": _kind_classic(d),
                   "model": text(d.get("model"), 32), "firmware": text(d.get("version"), 32), "mgmt_ip": _ip(d.get("ip")),
                   "online": num(d.get("state")) == 1,
                   "uplink": {"device": by_mac.get(umac), "port": num(up.get("uplink_remote_port")),
                              "medium": {"wire": "wired", "wireless": "radio"}.get(up.get("type"))} if umac else None,
                   "stats": {"cpu": num(stats.get("cpu")), "mem": num(stats.get("mem")), "uptime_s": num(d.get("uptime")),
                             "clients": num(d.get("num_sta"))},
                   "radios": _radios_classic(d)})
        lldp = {}                        # port -> known equipment id, or None for a neighbour the controller does not manage
        for n in d.get("lldp_table") or []:
            if isinstance(n, dict) and num(n.get("local_port_idx")) is not None:
                lldp[num(n["local_port_idx"])] = by_mac.get(norm_mac(n.get("chassis_id")))
        overrides = {num(o.get("port_idx")): o for o in d.get("port_overrides") or [] if isinstance(o, dict) and num(o.get("port_idx")) is not None}
        router = _kind_classic(d) == "router"
        for p in d.get("port_table") or []:
            if not isinstance(p, dict) or num(p.get("port_idx")) is None:
                continue
            idx = num(p["port_idx"])
            conf = port_settings(p, overrides.get(idx), profiles, nets_raw, default_net)
            prof = profiles.get(conf["portconf_id"]) if conf["portconf_id"] else None
            native = conf["native"]
            ports.append({"id": oid("p", "%s:%s" % (mac, idx)), "device": eid, "idx": idx,
                          "name": text(conf["name"], 32) or str(idx), "link": bool(p.get("up")),
                          "speed": num(p.get("speed")) if p.get("up") else None, "media": text(p.get("media"), 12),
                          "poe": {"capable": bool(p.get("port_poe")),
                                  "on": (conf["poe_mode"] != "off") if conf["poe_mode"] else bool(p.get("poe_enable")),
                                  "watts": num(p.get("poe_power")), "mode": conf["poe_mode"],
                                  "powered": bool(p.get("poe_enable"))} if p.get("port_poe") else None,
                          "profile": oid("pp", conf["portconf_id"]) if conf["portconf_id"] else "",
                          "profile_name": text((prof or {}).get("name")) if prof else None,
                          "native_vlan": vlan_of.get(native) if native else None,
                          "native_network": ids.net_id(native) if native else None,
                          "tagged_mode": conf["mode"], "tagged": [ids.net_id(k) for k in conf["tagged"]],
                          "is_uplink": bool(p.get("is_uplink")) or (umac is not None and own_up == idx),
                          "wan": router and _wan_port(p), "mode": conf["op_mode"],
                          "infra": lldp.get(idx) is not None, "lldp": idx in lldp, "lldp_neighbor": lldp.get(idx),
                          "rx_bytes": num(p.get("rx_bytes")), "tx_bytes": num(p.get("tx_bytes")),
                          "rx_errors": num(p.get("rx_errors")), "tx_errors": num(p.get("tx_errors")),
                          "rx_dropped": num(p.get("rx_dropped")), "tx_dropped": num(p.get("tx_dropped")),
                          "duplex": ("full" if p.get("full_duplex") else "half") if p.get("up") and p.get("full_duplex") is not None else None})
    return eq, ports, by_mac


RADIO_BANDS = {"ng": "2.4ghz", "na": "5ghz", "6e": "6ghz"}                  # identifier: radio codes


def _radios_classic(d) -> list:
    """Radios of an access point: band, channel, clients now (from the statistics table, else the settings table)."""
    stats = {r.get("radio"): r for r in d.get("radio_table_stats") or [] if isinstance(r, dict)}
    out = []
    for r in d.get("radio_table") or []:
        if not isinstance(r, dict) or r.get("radio") not in RADIO_BANDS:
            continue
        st = stats.get(r["radio"]) or {}
        out.append({"band": RADIO_BANDS[r["radio"]], "channel": num(st.get("channel")) or num(r.get("channel")),
                    "clients": num(st.get("num_sta")) if st.get("num_sta") is not None else num(st.get("user-num_sta"))})
    return out[:4]


def _equipment_official(raw) -> list:
    devs = items(raw, "o_devices")
    by_id = {d.get("id"): d for d in devs}
    out = []
    for d in devs:
        feats = d.get("features")
        names = list(feats) if isinstance(feats, (dict, list)) else []
        kind = "router" if "gateway" in names else "switch" if "switching" in names else "ap" if "accessPoint" in names else "other"
        up = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
        parent = by_id.get(up.get("deviceId"))
        mac = norm_mac(d.get("macAddress"))
        st = d.get("statistics") if isinstance(d.get("statistics"), dict) else {}
        out.append({"id": oid("e", mac or d.get("id")), "name": text(d.get("name")) or "?", "kind": kind,
                    "model": text(d.get("model"), 32), "firmware": text(d.get("firmwareVersion"), 32),
                    "mgmt_ip": _ip(d.get("ipAddress")), "online": str(d.get("state") or "") == "ONLINE",
                    "uplink": {"device": oid("e", norm_mac(parent.get("macAddress")) or parent.get("id")), "port": None, "medium": None}
                    if parent else None,
                    "stats": {"cpu": num(st.get("cpuUtilizationPct")), "mem": num(st.get("memoryUtilizationPct")),
                              "uptime_s": num(st.get("uptimeSec")), "clients": None}})
    return out


def _ports_official(raw) -> list:
    out = []
    for d in items(raw, "o_devices"):
        mac = norm_mac(d.get("macAddress"))
        eid = oid("e", mac or d.get("id"))
        ifs = d.get("interfaces") if isinstance(d.get("interfaces"), dict) else {}
        for p in ifs.get("ports") or []:
            if not isinstance(p, dict) or num(p.get("idx")) is None:
                continue
            poe = p.get("poe") if isinstance(p.get("poe"), dict) else None
            up = str(p.get("state") or "") == "UP"
            out.append({"id": oid("p", "%s:%s" % (mac or d.get("id"), num(p["idx"]))), "device": eid, "idx": num(p["idx"]),
                        "name": str(num(p["idx"])), "link": up, "speed": num(p.get("speedMbps")) if up else None,
                        "media": text(p.get("connector"), 12),
                        "poe": {"capable": True, "on": bool(poe.get("enabled")), "watts": None} if poe else None,
                        "profile": None, "native_vlan": None, "native_network": None, "tagged": None,
                        "is_uplink": False, "infra": False, "lldp_neighbor": None, "rx_bytes": None, "tx_bytes": None})
    return out


def _clients(raw, ids, nets) -> tuple:
    """(neutral end devices, dhcp reservations)."""
    users = {norm_mac(u.get("mac")): u for u in items(raw, "user") if norm_mac(u.get("mac"))}
    by_cidr = [(ipaddress.ip_network(n["cidr"]), n["id"]) for n in nets if n.get("cidr")]

    def net_for(ip, net_ref):
        if net_ref:
            return ids.net_id(net_ref)
        try:
            a = ipaddress.ip_address(ip)
        except (TypeError, ValueError):
            return None
        return next((nid for net, nid in by_cidr if a in net), None)

    devices = []
    sta = items(raw, "sta")
    if sta or users:
        port_count = {}
        for c in sta:
            if c.get("is_wired") and norm_mac(c.get("sw_mac")) and c.get("sw_port") is not None:
                key = (norm_mac(c["sw_mac"]), c["sw_port"])
                port_count[key] = port_count.get(key, 0) + 1
        for c in sta:
            mac = norm_mac(c.get("mac"))
            if not mac:
                continue
            u = users.get(mac, {})
            ref = c.get("virtual_network_override_id") if c.get("virtual_network_override_enabled") else c.get("network_id")
            wired = bool(c.get("is_wired"))
            parent = norm_mac(c.get("sw_mac")) if wired else norm_mac(c.get("ap_mac"))
            devices.append({"id": oid("c", mac), "name": text(c.get("name") or u.get("name") or c.get("hostname")) or text(c.get("oui"), 32) or "?",
                            "kind": "client", "network": net_for(c.get("ip"), ref), "ip": _ip(c.get("ip")),
                            "fixed": bool(c.get("use_fixedip") or u.get("use_fixedip")),
                            "fixed_ip": _ip(c.get("fixed_ip") or u.get("fixed_ip")) if (c.get("use_fixedip") or u.get("use_fixedip")) else None,
                            "wired": wired, "alone_on_port": wired and port_count.get((norm_mac(c.get("sw_mac")), c.get("sw_port"))) == 1,
                            "private_address": is_private_mac(mac), "protected": None,
                            "parent": oid("e", parent) if parent else None,
                            "port": num(c.get("sw_port")) if wired else None, "ssid": text(c.get("essid"), 32) if not wired else None,
                            "band": {"ng": "2.4ghz", "na": "5ghz", "6e": "6ghz"}.get(c.get("radio")) if not wired else None,  # identifier: radio codes
                            "blocked": bool(c.get("blocked") or u.get("blocked")), "guest": bool(c.get("is_guest")), "online": True,
                            "network_by_port": wired})
        online = {norm_mac(c.get("mac")) for c in sta}
        # known clients that are offline right now but carry settings of their own (a name, a fixed address, a network
        # override): they can still be renamed or get their fixed address, and their settings count for the plan
        for mac, u in users.items():
            if mac in online or len(devices) >= MAX_ITEMS:
                continue
            override = bool(u.get("virtual_network_override_enabled")) and bool(u.get("virtual_network_override_id"))
            fixed = _ip(u.get("fixed_ip")) if u.get("use_fixedip") else None
            if not (text(u.get("name")) or fixed or override):
                continue
            # the network it was last seen in; a released fixed address leaves its network in the record (last known)
            ref = u.get("virtual_network_override_id") if override else (
                u.get("network_id") if fixed else (u.get("last_connection_network_id") or u.get("network_id")))
            devices.append({"id": oid("c", mac), "name": text(u.get("name") or u.get("hostname")) or text(u.get("oui"), 32) or "?",
                            "kind": "client", "network": net_for(fixed, ref), "ip": fixed, "fixed": bool(fixed), "fixed_ip": fixed,
                            "wired": bool(u.get("is_wired")), "alone_on_port": None, "private_address": is_private_mac(mac),
                            "protected": None, "parent": None, "band": None, "blocked": bool(u.get("blocked")),
                            "guest": bool(u.get("is_guest")), "online": False, "network_by_port": bool(u.get("is_wired"))})
    else:
        for c in items(raw, "o_clients"):
            kind = str(c.get("type") or "")
            if kind not in ("WIRED", "WIRELESS"):
                continue
            mac = norm_mac(c.get("macAddress"))
            devices.append({"id": oid("c", mac or c.get("id")), "name": text(c.get("name")) or "?", "kind": "client",
                            "network": net_for(c.get("ipAddress"), None), "ip": _ip(c.get("ipAddress")), "fixed": False,
                            "wired": kind == "WIRED", "alone_on_port": False, "private_address": is_private_mac(mac),
                            "protected": None, "parent": None, "band": None, "blocked": False,
                            "guest": str(_d(c.get("access")).get("type") or "") == "GUEST", "online": True})
    reservations = []
    for mac, u in users.items():
        if u.get("use_fixedip") and _ip(u.get("fixed_ip")):
            reservations.append({"id": oid("d", mac), "client": oid("c", mac), "name": text(u.get("name")) or text(u.get("hostname")) or "?",
                                 "ip": _ip(u.get("fixed_ip")), "network": ids.net_id(u.get("network_id")) if u.get("network_id") else None})
    return devices, reservations


# ---------------------------------------------------------------------------- back to vendor ids (write half)
USER_FIELDS = ("name", "use_fixedip", "fixed_ip", "network_id", "virtual_network_override_enabled",
               "virtual_network_override_id")


NET_FIELDS = ("name", "purpose", "networkgroup", "ip_subnet", "vlan_enabled", "vlan", "dhcpd_enabled", "dhcpd_start",
              "dhcpd_stop", "enabled", "setting_preference", "internet_access_enabled", "ipv6_interface_type")
FORWARD_FIELDS = ("name", "enabled", "src", "dst_port", "fwd", "fwd_port", "proto", "log", "pfwd_interface")


def network_refs(raw) -> dict:
    """Neutral network id -> {"id": classic object id, "cidr", "purpose", "gateway_ip", "vlan", "record": {the network
    settings the write half may change or must put back, as stored now}} for every network of the classic API (the
    write half needs the classic id in a client's settings and for its own network changes)."""
    ids = _Ids(raw)
    out = {}
    for n in items(raw, "networkconf"):
        if not n.get("_id"):
            continue
        cidr = cidr_of(n.get("ip_subnet"))
        vlan = num(n.get("vlan")) if n.get("vlan_enabled", True) is not False else None
        out[ids.net_id(n["_id"])] = {"id": str(n["_id"]), "cidr": cidr, "purpose": str(n.get("purpose") or ""),
                                     "gateway_ip": str(ipaddress.ip_interface(n["ip_subnet"]).ip) if cidr else None,
                                     "vlan": vlan if isinstance(vlan, int) and vlan > 0 else None,
                                     "record": {f: n.get(f) for f in NET_FIELDS if f in n}}
    return out


# what the write half keeps of a wireless network for the way back (never its passwords)
WLAN_FIELDS = ("name", "networkconf_id", "wlan_bands", "security", "wpa_mode", "wpa3_support", "wpa3_transition", "hide_ssid",
               "enabled", "is_guest", "private_preshared_keys_enabled", "ap_group_ids", "ap_group_mode", "usergroup_id")


def ssid_refs(raw) -> dict:
    """Neutral SSID id -> {"id": classic object id, "record": {the wireless settings the write half may change or must
    put back, as stored now - no password, no private keys}, "has_password": bool}."""
    out = {}
    for w in items(raw, "wlanconf"):
        if not w.get("_id"):
            continue
        out[oid("s", w.get("external_id") or w["_id"])] = {
            "id": str(w["_id"]), "record": {f: copy.deepcopy(w.get(f)) for f in WLAN_FIELDS if f in w},
            "has_password": bool(w.get("x_passphrase"))}
    return out


def client_macs(raw) -> dict:
    """Neutral client id -> hardware address (the block command names a client by it; never published)."""
    out = {}
    for area in ("user", "sta"):
        for c in items(raw, area):
            mac = norm_mac(c.get("mac"))
            if mac:
                out[oid("c", mac)] = mac
    return out


def port_refs(raw) -> dict:
    """Neutral port id -> {"device": classic id of the switch, "mac", "idx", "override": the switch's override entry
    for the port (None when it has none), "overrides": the switch's whole override list as stored now}."""
    out = {}
    for d in items(raw, "device"):
        mac = norm_mac(d.get("mac"))
        if not mac or not d.get("_id"):
            continue
        ovs = [o for o in d.get("port_overrides") or [] if isinstance(o, dict)]
        by_idx = {num(o.get("port_idx")): o for o in ovs if num(o.get("port_idx")) is not None}
        # the switch keeps tagged networks per port as a mode plus an excluded list (releases since 7.x); older ones
        # know only "all / untagged only / customize"
        new_style = any("tagged_vlan_mgmt" in x for x in list(ovs) + [p for p in d.get("port_table") or [] if isinstance(p, dict)]) \
            or version_tuple(version_of(raw)) >= (7, 4)
        for p in d.get("port_table") or []:
            idx = num(p.get("port_idx")) if isinstance(p, dict) else None
            if idx is None:
                continue
            out[oid("p", "%s:%s" % (mac, idx))] = {"device": str(d["_id"]), "mac": mac, "idx": idx, "new_style": new_style,
                                                   "override": by_idx.get(idx), "overrides": ovs,
                                                   "table": {k: p.get(k) for k in PORT_KEYS + ("port_poe",) if k in p}}
    return out


def profile_refs(raw) -> dict:
    """Neutral port profile id -> classic id."""
    return {oid("pp", p["_id"]): str(p["_id"]) for p in items(raw, "portconf") if p.get("_id")}


def forward_refs(raw) -> dict:
    """Neutral port forward id -> {"id": classic object id, "record": {the settings as stored now}}."""
    out = {}
    for p in items(raw, "portforward"):
        if p.get("_id"):
            out[oid("f", p["_id"])] = {"id": str(p["_id"]), "record": {f: p.get(f) for f in FORWARD_FIELDS if f in p}}
    return out


def client_refs(raw) -> dict:
    """Neutral client id -> {"user": classic user id, "online": bool, "wired": bool, "record": {the user settings the
    write half may change, as stored now}}. Online clients without a user record of their own use the id the
    controller lists for them (``user_id``)."""
    out = {}
    for u in items(raw, "user"):
        mac = norm_mac(u.get("mac"))
        if mac and u.get("_id"):
            out[oid("c", mac)] = {"user": str(u["_id"]), "online": False, "wired": bool(u.get("is_wired")),
                                  "record": {f: u.get(f) for f in USER_FIELDS if f in u}, "reservation": oid("d", mac)}
    for c in items(raw, "sta"):
        mac = norm_mac(c.get("mac"))
        if not mac:
            continue
        cid = oid("c", mac)
        if cid in out:
            out[cid]["online"] = True
            out[cid]["wired"] = bool(c.get("is_wired"))
            continue
        uid = c.get("user_id") or c.get("_id")
        if uid:
            out[cid] = {"user": str(uid), "online": True, "wired": bool(c.get("is_wired")),
                        "record": {f: c.get(f) for f in USER_FIELDS if f in c}, "reservation": oid("d", mac)}
    return out


# ---------------------------------------------------------------------------- wireless, forwards, wans, vpn
def _ssids_classic(raw, ids) -> list:
    out = []
    for w in items(raw, "wlanconf"):
        sec = SECURITY.get(str(w.get("security") or ""), "other")
        if sec.startswith("wpa"):
            mode = "wpa3" if w.get("wpa3_support") and not w.get("wpa3_transition") else "wpa2_wpa3" if w.get("wpa3_transition") else str(w.get("wpa_mode") or "wpa2")
            sec = "%s_%s" % (mode, sec.split("_", 1)[1])
        bands = w.get("wlan_bands") or ([w["wlan_band"]] if w.get("wlan_band") and w.get("wlan_band") != "both" else ["2g", "5g"])
        out.append({"id": oid("s", w.get("external_id") or w.get("_id")), "name": text(w.get("name"), 32) or "?",
                    "network": ids.net_id(w.get("networkconf_id")) if w.get("networkconf_id") else None,
                    "bands": sorted({BANDS[b] for b in bands if b in BANDS}), "security": sec,
                    "hidden": bool(w.get("hide_ssid")), "enabled": w.get("enabled") is not False,
                    "guest": bool(w.get("is_guest")), "isolation": bool(w.get("l2_isolation"))})
    return out


def _ssids_official(raw) -> list:
    out = []
    for w in items(raw, "o_wifi"):
        netref = w.get("network") if isinstance(w.get("network"), dict) else {}
        sec = w.get("securityConfiguration") if isinstance(w.get("securityConfiguration"), dict) else {}
        freqs = w.get("broadcastingFrequenciesGHz") or []
        out.append({"id": oid("s", w.get("id")), "name": text(w.get("name"), 32) or "?",
                    "network": oid("n", netref["networkId"]) if netref.get("networkId") else None,
                    "bands": sorted({BANDS[str(f).rstrip("0").rstrip(".")] for f in freqs if str(f).rstrip("0").rstrip(".") in BANDS}),
                    "security": str(sec.get("type") or "other").lower(),
                    "hidden": bool(w.get("hideName")), "enabled": w.get("enabled") is not False,
                    "guest": bool(w.get("hotspotConfiguration")), "isolation": bool(w.get("clientIsolationEnabled"))})
    return out


def _forwards(raw) -> list:
    out = []
    for p in items(raw, "portforward"):
        out.append({"id": oid("f", p.get("_id")), "name": text(p.get("name")) or "?", "proto": text(p.get("proto"), 10),
                    "wan_port": re.sub(r"[^0-9,\-]", "", str(p.get("dst_port") or ""))[:64],
                    "dst_ip": _ip(p.get("fwd")), "dst_port": re.sub(r"[^0-9,\-]", "", str(p.get("fwd_port") or ""))[:64],
                    "enabled": bool(p.get("enabled")), "source_limited": str(p.get("src") or "any").lower() not in ("any", ""),
                    "logging": bool(p.get("log"))})
    return out


def _wans(raw, ids) -> list:
    out = {}
    order = []
    for n in items(raw, "networkconf"):
        if str(n.get("purpose") or "") == "wan":
            wid = ids.net_id(n.get("_id"))
            out[wid] = {"id": wid, "name": text(n.get("name")) or "?", "type": text(n.get("wan_type"), 16),
                        "failover": text(n.get("wan_load_balance_type"), 24), "enabled": n.get("enabled") is not False,
                        "_slot": str(n.get("attr_hidden_id") or "").upper(), "_prio": num(n.get("wan_failover_priority"))}
            order.append(wid)
    for w in items(raw, "o_wans"):
        wid = oid("n", w.get("id"))
        out.setdefault(wid, {"id": wid, "name": text(w.get("name")) or "?", "type": None, "failover": None, "enabled": True})
    _wan_states(raw, out, order)
    for w in out.values():
        w.pop("_slot", None)
        w.pop("_prio", None)
    return list(out.values())


_WAN_SLOT = {"WAN": "wan1", "WAN2": "wan2", "WAN3": "wan3"}


def _wan_states(raw, out: dict, order: list) -> None:
    """Link state, rates and the carrying line of each internet line from the gateway's own device record (read only).
    What the gateway does not tell stays absent (the control room then measures instead of believing)."""
    gw = next((d for d in items(raw, "device") if _kind_classic(d) == "router"), None)
    if gw is None:
        return
    uplink = gw.get("uplink") if isinstance(gw.get("uplink"), dict) else {}
    carrying = text(uplink.get("name") or uplink.get("ifname") or gw.get("uplink_ifname"), 16)
    for i, wid in enumerate(order):
        w = out[wid]
        key = _WAN_SLOT.get(w.get("_slot") or "") or "wan%d" % (i + 1)
        rec = gw.get(key) if isinstance(gw.get(key), dict) else None
        if rec is None:
            continue
        if isinstance(rec.get("up"), bool):
            w["up"] = rec["up"]
        for src, dst in (("rx_bytes-r", "rx_bps"), ("tx_bytes-r", "tx_bps")):
            v = num(rec.get(src))
            if v is not None and 0 <= v < 1e15:                 # no negative or endless rates (also "nan" and "inf" text)
                w[dst] = v * 8.0
        lat = num(rec.get("latency"))
        if lat is not None and 0 <= lat < 1e9:
            w["latency_ms"] = lat
        ifname = text(rec.get("ifname") or rec.get("name"), 16)
        if isinstance(rec.get("is_uplink"), bool):
            w["active"] = rec["is_uplink"]
        elif carrying and ifname:
            w["active"] = (ifname == carrying)
        low = (w.get("name") or "").lower() + " " + str(rec.get("type") or "").lower()
        w["kind"] = "cellular" if any(x in low for x in ("lte", "5g", "4g", "cellular", "mobile")) else "cable"
    if len(order) > 1:
        if out[order[0]].get("failover") == "failover-only":
            ranked = sorted(order, key=lambda wid: (out[wid].get("_prio") if out[wid].get("_prio") is not None else 99, order.index(wid)))
            for j, wid in enumerate(ranked):
                out[wid]["role"] = "primary" if j == 0 else "backup"
        else:
            for wid in order:
                out[wid]["role"] = "primary"


def _vpn(raw, ids) -> list:
    out = {}
    for n in items(raw, "networkconf"):
        if str(n.get("purpose") or "") in ("remote-user-vpn", "site-vpn", "vpn-client"):
            vid = ids.net_id(n.get("_id"))
            out[vid] = {"id": vid, "name": text(n.get("name")) or "?", "kind": text(n.get("purpose"), 20),
                        "type": text(n.get("vpn_type"), 20), "enabled": n.get("enabled") is not False}
    for v in items(raw, "o_vpn"):
        vid = oid("n", v.get("id"))
        out.setdefault(vid, {"id": vid, "name": text(v.get("name")) or "?", "kind": text(v.get("type"), 24),
                             "type": None, "enabled": v.get("enabled") is not False})
    return list(out.values())


def _raw_extra(raw) -> list:
    out = _legacy_rules(raw)
    for area, key in (("acl", "o_acl"), ("dns_policy", "o_dns")):
        for r in items(raw, key):
            out.append({"id": oid("x", area + str(r.get("id"))), "area": area, "editable": False,
                        "title": text(r.get("name") or r.get("domain"), 120) or "?", "enabled": r.get("enabled") is not False})
    return out


# ---------------------------------------------------------------------------- the whole state
def version_of(raw) -> str:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    if info.get("applicationVersion"):
        return text(info["applicationVersion"], 32)
    si = items(raw, "sysinfo")
    return text(si[0].get("version"), 32) if si else ""


def to_neutral(raw: dict) -> dict:
    """Neutral model v2 from whatever replies are present. Raises nothing for missing areas (they stay empty)."""
    raw = raw if isinstance(raw, dict) else {}
    ids = _Ids(raw)
    nets = _merge_by_id(_networks_official(raw), _networks_classic(raw, ids), fill=("cidr", "purpose", "dhcp", "zone", "gateway_ip"))
    zones = _merge_by_id(_zones_official(raw), _zones_classic(raw, ids))
    zone_of_net = {n: z["id"] for z in zones for n in z.get("networks") or []}
    for n in nets:
        if not n.get("zone"):
            n["zone"] = zone_of_net.get(n["id"])
    pols_o, pols_c = _policies_official(raw), _policies_classic(raw, ids)
    if pols_o:
        hits = {p["id"]: p for p in pols_c}
        for p in pols_o:
            c = hits.get(p["id"])
            if c:
                p["hits"], p["last_hit"] = c["hits"], c["last_hit"]
        pols = pols_o
    else:
        pols = pols_c
    eq_c, ports_c, _ = _equipment_classic(raw)
    eq = _merge_by_id(eq_c, _equipment_official(raw), fill=("firmware", "mgmt_ip", "uplink"))
    ports = ports_c or _ports_official(raw)
    _mark_management(nets, [e.get("mgmt_ip") for e in eq])
    devices, reservations = _clients(raw, ids, nets)
    ssids = _merge_by_id(_ssids_official(raw), _ssids_classic(raw, ids), fill=("network", "bands"))
    state = {"v": 2, "zones": zones, "networks": nets, "devices": devices, "equipment": eq, "ports": ports,
             "port_profiles": _profiles_classic(raw, ids),
             "policies": pols, "ssids": ssids, "dhcp_reservations": reservations, "port_forwards": _forwards(raw),
             "wans": _wans(raw, ids), "vpn": _vpn(raw, ids), "raw": _raw_extra(raw)}
    for z in state["zones"]:
        z.pop("networks", None)
    if pols:
        external = {z["id"] for z in zones if z.get("system") == "external"}
        allow = neutral.derive_allow(state, default="block")
        state["allow"] = allow
        for z in zones:
            z["internet"] = any(a == z["id"] and b in external for a, b in allow)
            z.setdefault("discovery", False)
    else:
        state["allow"] = []
        for z in zones:
            z.setdefault("internet", False)
            z.setdefault("discovery", False)
    ids_state = _ids_summary(raw)
    if ids_state:
        state["ids"] = ids_state
    state["controller"] = {"version": version_of(raw), "apis": sorted(k for k in ("official", "classic") if _has(raw, k))}
    return state


def _ids_summary(raw) -> dict:
    """Size of the intrusion detection's suppression lists (alerts and trusted addresses). Only counted, entries are
    never copied."""
    for row in items(raw, "ips"):
        sup = row.get("suppression") if isinstance(row.get("suppression"), dict) else {}
        n = sum(len(v) for v in (sup.get("alerts"), sup.get("whitelist")) if isinstance(v, list))
        return {"suppressions": n, "enabled": row.get("mode") not in (None, "disabled")}
    return {}


def _has(raw, which) -> bool:
    keys = [k for k in raw if raw.get(k) is not None]
    return any(k.startswith("o_") or k == "info" for k in keys) if which == "official" else any(not (k.startswith("o_") or k == "info") for k in keys)


def topology_event(raw: dict, t: float) -> dict:
    """The topology observation for the core: same shape as the generic controller source (so the resolver ranks
    it as ``controller`` and merges it with manual links, LLDP and address tables)."""
    raw = raw if isinstance(raw, dict) else {}
    devices = [map_device(d) for d in items(raw, "device")]
    clients = [map_client(c) for c in items(raw, "sta")]
    if not devices and items(raw, "o_devices"):
        odev = items(raw, "o_devices")
        mac_of = {d.get("id"): norm_mac(d.get("macAddress")) for d in odev}
        for d in odev:
            feats = d.get("features")
            names = list(feats) if isinstance(feats, (dict, list)) else []
            parent = mac_of.get(_d(d.get("uplink")).get("deviceId")) if isinstance(d.get("uplink"), dict) else None
            mac = mac_of.get(d.get("id"))
            if not mac:
                continue
            devices.append({"mac": mac, "ip": _ip(d.get("ipAddress")), "name": text(d.get("name"), 64),
                            "role": "router" if "gateway" in names else "switch" if "switching" in names else "ap" if "accessPoint" in names else "other",
                            "model": text(d.get("model"), 64),
                            "uplink": {"mac": parent, "port": None, "own_port": None, "medium": None} if parent else None})
        if not clients:
            for c in items(raw, "o_clients"):
                if str(c.get("type") or "") not in ("WIRED", "WIRELESS"):
                    continue
                mac, parent = norm_mac(c.get("macAddress")), mac_of.get(c.get("uplinkDeviceId"))
                if mac and parent:
                    clients.append({"mac": mac, "ip": _ip(c.get("ipAddress")), "name": text(c.get("name"), 64),
                                    "medium": "wired" if c.get("type") == "WIRED" else "radio", "parent_mac": parent,
                                    "port": None, "band": None, "ssid": None})
    ev = {"type": "topology", "source": "controller", "t": t, "devices": _unique(devices, 1000), "clients": _unique(clients, MAX_ITEMS)}
    if items(raw, "device"):                 # ports with their counters ride along every minute (link drops, exact rates)
        eq, ports, _ = _equipment_classic(raw)
        ev["ports"] = [{k: p.get(k) for k in ("id", "device", "idx", "link", "is_uplink", "rx_bytes", "tx_bytes", "rx_errors", "tx_errors")} for p in ports[:MAX_ITEMS]]
        ev["equipment"] = [{"id": e["id"], "ip": e.get("mgmt_ip"), "mac": norm_mac(d.get("mac")), "kind": e.get("kind")}
                           for e, d in zip(eq, [d for d in items(raw, "device") if norm_mac(d.get("mac"))])][:1000]
    return ev
