"""Translation module of the Omada family: Open API replies -> neutral model v2 and the topology observation.

Pure functions, no I/O. Shared by the read half and (later) a write half, so both compute the same neutral state.

Input is a dict of raw replies keyed by area (whatever the read half could get; every key is optional; lists are the
``result`` lists of the API, already collected over all pages)::

    info          controller info   {controllerVer, omadacId, apiVer}
    site          the chosen site   {siteId, name, ...}
    devices       gateway, switches and access points of the site
    clients       active clients of the site
    networks      LAN networks and VLANs (lan-networks)
    profiles      LAN (port) profiles (lan-profiles)
    ssids         wireless networks of every WLAN group (each item may carry ``wlan``: the group's name)
    acl_gateway / acl_switch / acl_eap    access control rules of the three device types
    wans          internet connections of the gateway (wan-status; items may carry ``gateway``: its hardware address)
    switch_info   {mac: switch info}   port list with profile names
    switch_stat   {mac: switch statistics}   port states, uplink, neighbours

It builds::

    equipment       devices with kind, model, firmware, uplink, online flag, statistics
    ports           switch ports: link, speed, PoE, profile, native network, neighbour
    devices         end devices (clients) with network, parent device, port or SSID
    networks        LAN networks and VLANs with address range and DHCP
    ssids           wireless networks with bands, security, VLAN
    dhcp_reservations   clients that hold a fixed address
    wans            gateway ports in WAN mode with their internet state
    raw             access control rules (shown, never written)
    acl_digest      one line per rule (name, on/off, fingerprint) for the night report
    findings        plain checks: device offline, open wireless network, internet connection down, rules switched off

Never copied: the passphrase and RADIUS or PPSK settings of a wireless network, serial numbers, account data, the
client secret. A wireless network's detail reply (which carries the passphrase) is never asked for; the list reply the
adapter reads does not contain it. Neutral ids are opaque hashes; hardware addresses are only used for the topology
observation (the core needs them to match devices).
"""
import hashlib
import ipaddress
import json
import re

from ...sources._ctlmap import _unique, ip_str, name_str, norm_mac, port_str

SALT = "lanloom-omada:"
MAX_ITEMS = 5000
TEXT_MAX = 96

KINDS = {"gateway": "router", "switch": "switch", "ap": "ap"}
LINK_SPEED = {1: 10, 2: 100, 3: 1000, 4: 2500, 5: 10000, 6: 5000, 7: 25000, 8: 100000}     # Mbit/s by the API's code
RADIO_BANDS = {0: "2.4ghz", 1: "5ghz", 2: "5ghz", 3: "6ghz"}
SECURITY = {0: "open", 2: "wpa_enterprise", 3: "wpa_personal", 4: "ppsk", 5: "ppsk_radius"}
MEDIA = {1: "copper", 2: "combo", 3: "sfp"}
CONTROLLER_CLASS = "access_controller"          # marks this family's state for its night report section (no product name in data)
ONLINE = 1                       # device status: 0 disconnected, 1 connected, 2 pending, 3 heartbeat missed, 4 isolated
PENDING = 2


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
    """The list inside a collected reply: a list, or a page ``{"data": [...]}``."""
    value = raw.get(key) if isinstance(raw, dict) else None
    if isinstance(value, dict):
        value = value.get("data")
    if not isinstance(value, list):
        return []
    return [v for v in value[:MAX_ITEMS] if isinstance(v, dict)]


def _first_by_mac(rows) -> list:
    """Rows with a valid hardware address, each address once (the first row wins)."""
    seen, out = set(), []
    for r in rows:
        m = norm_mac(r.get("mac"))
        if m and m not in seen:
            seen.add(m)
            out.append(r)
    return out


def _mac_map(raw, key) -> dict:
    value = raw.get(key) if isinstance(raw, dict) else None
    return {norm_mac(k): v for k, v in value.items() if norm_mac(k) and isinstance(v, dict)} if isinstance(value, dict) else {}


def _ip(value):
    return ip_str(value if isinstance(value, str) else None)


def uptime_s(value):
    """Seconds from the API's text ``1day(s) 2h 3m 4s`` (also a plain number of seconds)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    s = str(value or "")
    parts = re.findall(r"(\d+)\s*(day|d|h|m|s)", s)
    if not parts:
        return None
    mult = {"day": 86400, "d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in parts)


def cidr_of(subnet):
    try:
        return str(ipaddress.ip_interface(str(subnet).strip()).network)
    except ValueError:
        return None


def is_private_mac(mac) -> bool:
    m = norm_mac(mac)
    return bool(m) and bool(int(m[:2], 16) & 2)


def bands_of(mask) -> list:
    """Bit mask of a wireless network: 1 = 2.4 GHz, 2 = 5 GHz, 4 = 6 GHz."""
    m = num(mask)
    if not isinstance(m, int):
        return []
    return [b for bit, b in ((1, "2.4ghz"), (2, "5ghz"), (4, "6ghz")) if m & bit]


# ---------------------------------------------------------------------------- networks
def _networks(raw) -> list:
    out = []
    for n in items(raw, "networks"):
        dh = _d(n.get("dhcpSettingsVO"))
        cidr = cidr_of(n.get("gatewaySubnet")) if n.get("gatewaySubnet") else None
        host = _ip(dh.get("hostIP"))
        if not cidr and host and isinstance(dh.get("dhcpPoolMask"), int) and 0 < dh["dhcpPoolMask"] <= 32:
            cidr = cidr_of("%s/%d" % (host, dh["dhcpPoolMask"]))
        gw = host or (str(ipaddress.ip_interface(n["gatewaySubnet"]).ip) if cidr_of(n.get("gatewaySubnet")) else None)
        purpose, vtype = n.get("purpose"), n.get("vlanType")
        vlan = num(n.get("vlan")) if purpose == 0 or (purpose == 1 and vtype == 0) else None
        dhcp = {"on": bool(dh.get("enable")), "range": [_ip(dh.get("ipaddrStart")), _ip(dh.get("ipaddrEnd"))]
                if dh.get("enable") and _ip(dh.get("ipaddrStart")) else None}
        out.append({"id": oid("n", n.get("id")), "name": text(n.get("name")) or "?", "cidr": cidr,
                    "vlan": vlan if isinstance(vlan, int) and vlan > 1 else None, "zone": None,
                    "purpose": "lan", "dhcp": dhcp, "internet": True, "enabled": True,
                    "isolated": False, "gateway_ip": gw, "management": False,
                    "primary": bool(n.get("primary"))})
    return out


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


# ---------------------------------------------------------------------------- equipment and ports
def _equipment(raw) -> tuple:
    """(equipment, mac -> equipment id)."""
    devs = _first_by_mac(items(raw, "devices"))
    by_mac = {norm_mac(d["mac"]): oid("e", norm_mac(d["mac"])) for d in devs}
    per_parent = {}
    for c in items(raw, "clients"):
        for key in ("apMac", "switchMac", "gatewayMac"):
            m = norm_mac(c.get(key))
            if m and c.get("active") is not False:
                per_parent[m] = per_parent.get(m, 0) + 1
                break
    out = []
    for d in devs:
        mac = norm_mac(d["mac"])
        umac = norm_mac(d.get("uplinkDeviceMac"))
        status = num(d.get("status"))
        port = text(d.get("uplinkDevicePort"), 32)
        out.append({"id": by_mac[mac], "name": text(d.get("name")) or text(d.get("modelName")) or text(d.get("model")) or "?",
                    "kind": KINDS.get(str(d.get("type") or "").lower(), "other"),
                    "model": text(d.get("modelName") or d.get("model"), 32), "firmware": text(d.get("firmwareVersion"), 32),
                    "mgmt_ip": _ip(d.get("ip")), "online": status == ONLINE, "pending": status == PENDING,
                    "uplink": {"device": by_mac.get(umac), "port": int(port) if port and port.isdigit() else None,
                               "medium": None} if umac and umac in by_mac else None,
                    "stats": {"cpu": num(d.get("cpuUtil")), "mem": num(d.get("memUtil")), "uptime_s": uptime_s(d.get("uptime")),
                              "clients": per_parent.get(mac)}})
    return out, by_mac


def _ports(raw, by_mac, nets) -> list:
    info, stat = _mac_map(raw, "switch_info"), _mac_map(raw, "switch_stat")
    profiles = {p.get("id"): p for p in items(raw, "profiles") if p.get("id")}
    vlan_of = {n.get("id"): num(n.get("vlan")) for n in items(raw, "networks")}
    out = []
    for mac in sorted(set(info) | set(stat)):
        if mac not in by_mac:
            continue
        eid = by_mac[mac]
        st, inf = stat.get(mac, {}), info.get(mac, {})
        plist = {num(p.get("port")): p for p in inf.get("portList") or [] if isinstance(p, dict) and num(p.get("port")) is not None}
        slist = {num(p.get("port")): p for p in st.get("ports") or [] if isinstance(p, dict) and num(p.get("port")) is not None}
        uplink = num(_d(st.get("uplink")).get("port"))
        for idx in sorted(set(plist) | set(slist)):
            p, s = plist.get(idx, {}), slist.get(idx, {})
            ps = _d(s.get("portStatus"))
            up = num(ps.get("linkStatus")) == 1 and not s.get("disable")
            prof = profiles.get(p.get("profileId"))
            native = vlan_of.get(prof.get("nativeNetworkId")) if prof else None
            down = _d(s.get("downlink"))
            neighbour = by_mac.get(norm_mac(down.get("mac")))
            poe_on = ps.get("poe") if "poe" in ps else (num(p.get("poeMode")) == 1 if p.get("poeMode") is not None else None)
            capable = bool(st.get("supportPoe")) if "supportPoe" in st else (poe_on is not None)
            out.append({"id": oid("p", "%s:%s" % (mac, idx)), "device": eid, "idx": idx,
                        "name": text(s.get("name") or p.get("name"), 32) or str(idx), "link": up,
                        "speed": LINK_SPEED.get(num(ps.get("linkSpeed"))) if up else None,
                        "media": MEDIA.get(num(s.get("type"))),
                        "poe": {"capable": capable, "on": bool(poe_on) if capable else False, "watts": None} if capable else None,
                        "profile": text(p.get("profileName")) or (text(prof.get("name")) if prof else None),
                        "native_vlan": native if isinstance(native, int) and native > 1 else None,
                        "native_network": oid("n", prof["nativeNetworkId"]) if prof and prof.get("nativeNetworkId") else None,
                        "tagged": None, "is_uplink": uplink == idx, "infra": neighbour is not None,
                        "lldp_neighbor": neighbour, "rx_bytes": None, "tx_bytes": None,
                        "disabled": bool(s.get("disable")) or num(p.get("status")) == 0})
    return out[:MAX_ITEMS]


# ---------------------------------------------------------------------------- clients
def _client_parent(c) -> tuple:
    wired = c.get("wireless") is False or c.get("connectType") == 2 or str(c.get("connectDevType") or "") in ("switch", "gateway")
    parent = norm_mac(c.get("apMac")) if not wired else (norm_mac(c.get("switchMac")) or norm_mac(c.get("gatewayMac")))
    return wired, parent


def _clients(raw, by_mac, nets) -> tuple:
    by_cidr = [(ipaddress.ip_network(n["cidr"]), n["id"]) for n in nets if n.get("cidr")]
    by_name = {n["name"]: n["id"] for n in nets}
    clients = _first_by_mac(items(raw, "clients"))
    port_count = {}
    for c in clients:
        wired, parent = _client_parent(c)
        if wired and parent and port_str(c.get("port")) is not None:
            key = (parent, port_str(c["port"]))
            port_count[key] = port_count.get(key, 0) + 1
    devices, reservations = [], []
    for c in clients[:MAX_ITEMS]:
        mac = norm_mac(c["mac"])
        wired, parent = _client_parent(c)
        ip = _ip(c.get("ip"))
        net = by_name.get(text(c.get("networkName"))) if wired else None
        if not net and ip:
            a = ipaddress.ip_address(ip)
            net = next((nid for n, nid in by_cidr if a.version == n.version and a in n), None)
        ips = _d(c.get("ipSetting"))
        fixed = bool(ips.get("useFixedAddr"))
        port = port_str(c.get("port")) if wired else None
        devices.append({"id": oid("c", mac), "name": text(c.get("name") or c.get("hostName")) or text(c.get("vendor"), 32) or "?",
                        "kind": "client", "network": net, "ip": ip, "fixed": fixed, "fixed_ip": ip if fixed else None,
                        "wired": wired, "alone_on_port": wired and parent is not None and port is not None
                        and port_count.get((parent, port)) == 1,
                        "private_address": is_private_mac(mac), "protected": None,
                        "parent": by_mac.get(parent) if parent else None,
                        "port": port, "ssid": None if wired else text(c.get("ssid"), 32),
                        "band": None if wired else RADIO_BANDS.get(num(c.get("radioId"))),
                        "blocked": bool(c.get("blocked")), "guest": bool(c.get("guest")) or c.get("connectType") == 0,
                        "online": c.get("active") is not False, "network_by_port": wired})
        if fixed and ip:
            reservations.append({"id": oid("d", mac), "client": oid("c", mac), "name": text(c.get("name") or c.get("hostName")) or "?",
                                 "ip": ip, "network": net})
    return devices, reservations


# ---------------------------------------------------------------------------- wireless, WAN, rules
def _ssids(raw, nets) -> list:
    by_vlan = {n["vlan"]: n["id"] for n in nets if n.get("vlan")}
    out, seen = [], set()
    for w in items(raw, "ssids"):
        sid = w.get("id") or w.get("ssidId")
        key = (w.get("wlan"), sid, w.get("name"))
        if key in seen:
            continue
        seen.add(key)
        vlan = num(w.get("vlanId")) if w.get("vlanEnable") else None
        out.append({"id": oid("s", "%s:%s" % (w.get("wlan") or "", sid or w.get("name"))), "name": text(w.get("name"), 32) or "?",
                    "network": by_vlan.get(vlan) if isinstance(vlan, int) else None, "vlan": vlan if isinstance(vlan, int) else None,
                    "bands": bands_of(w.get("band")), "security": SECURITY.get(num(w.get("security")), "other"),
                    "hidden": w.get("broadcast") is False, "enabled": True,
                    "guest": bool(w.get("guestNetEnable")), "isolation": False, "group": text(w.get("wlan"), 32)})
    return out[:MAX_ITEMS]


def _wans(raw, by_mac) -> list:
    out = []
    for w in items(raw, "wans"):
        if num(w.get("mode")) not in (None, 0):
            continue                                              # the gateway's LAN ports
        gw = norm_mac(w.get("gateway"))
        out.append({"id": oid("w", "%s:%s" % (gw or "", w.get("port") if w.get("port") is not None else w.get("name"))),
                    "name": text(w.get("name")) or "?", "type": text(w.get("proto"), 16), "failover": None, "enabled": True,
                    "up": num(w.get("internetState")) == 1, "link": num(w.get("status")) == 1,
                    "latency_ms": num(w.get("latency")), "loss": num(w.get("loss")),
                    "device": by_mac.get(gw) if gw else None})
    return out


ACL_SCOPES = (("acl_gateway", "gateway"), ("acl_switch", "switch"), ("acl_eap", "eap"))


def _sig(scope: str, r: dict) -> str:
    keys = ("policy", "sourceType", "sourceIds", "destinationType", "destinationIds", "protocols", "direction", "states",
            "stateMode", "bindingType", "bindingBridgeVlan", "customAclPorts", "customAclOsws", "etherType", "networkId",
            "timeRangeId")
    blob = json.dumps({k: r.get(k) for k in keys if k in r}, sort_keys=True, default=str)
    return hashlib.sha256(("%s|%s" % (scope, blob)).encode("utf-8")).hexdigest()[:12]


def _acls(raw) -> tuple:
    """(raw items, digest). Shown, never written: the rule's own order is kept, names are the rule's description."""
    shown, digest = [], []
    for key, scope in ACL_SCOPES:
        for r in items(raw, key):
            ref = oid("x", "acl:%s:%s" % (scope, r.get("id")))
            name = text(r.get("description"), 120) or "#%s" % (num(r.get("index")) if num(r.get("index")) is not None else "?")
            allow = num(r.get("policy")) == 1
            enabled = bool(r.get("status"))
            shown.append({"id": ref, "area": "acl", "editable": False, "title": name, "scope": scope,
                          "action": "allow" if allow else "block", "enabled": enabled, "order": num(r.get("index"))})
            digest.append({"ref": ref, "name": name, "scope": scope, "enabled": enabled, "allow": allow, "sig": _sig(scope, r)})
    return shown[:MAX_ITEMS], digest[:MAX_ITEMS]


# ---------------------------------------------------------------------------- checks
def findings(state: dict) -> list:
    """Plain checks of the adapter, most important first. Kinds (texts in the translations as fwr.finding.<kind>):
    ``device_offline``, ``wan_down``, ``ssid_open``, ``acls_off``."""
    out = []

    def add(kind, level, subject, **params):
        out.append({"id": oid("f", "%s:%s" % (kind, subject)), "kind": kind, "level": level, "subject": subject, "params": params})

    for w in state.get("wans") or []:
        if w.get("up") is False:
            add("wan_down", "alert", w["id"], wan=w.get("name"))
    for e in state.get("equipment") or []:
        if not e.get("online") and not e.get("pending"):
            add("device_offline", "alert" if e.get("kind") == "router" else "warn", e["id"], device=e.get("name"))
    for s in state.get("ssids") or []:
        if s.get("security") == "open" and s.get("enabled", True):
            add("ssid_open", "info" if s.get("guest") else "warn", s["id"], ssid=s.get("name"))
    off = sum(1 for r in state.get("acl_digest") or [] if not r.get("enabled"))
    if off:
        add("acls_off", "info", "acls", n=off)
    order = {"alert": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: order[f["level"]])
    return out


# ---------------------------------------------------------------------------- the whole state
def version_of(raw) -> str:
    info = _d(raw.get("info"))
    return text(info.get("controllerVer"), 32) or ""


def to_neutral(raw: dict) -> dict:
    """Neutral model v2 from whatever replies are present. Raises nothing for missing areas (they stay empty)."""
    raw = raw if isinstance(raw, dict) else {}
    nets = _networks(raw)
    eq, by_mac = _equipment(raw)
    _mark_management(nets, [e.get("mgmt_ip") for e in eq])
    devices, reservations = _clients(raw, by_mac, nets)
    acl_raw, digest = _acls(raw)
    state = {"v": 2, "zones": [], "networks": nets, "devices": devices, "equipment": eq,
             "ports": _ports(raw, by_mac, nets), "policies": [], "ssids": _ssids(raw, nets),
             "dhcp_reservations": reservations, "port_forwards": [], "wans": _wans(raw, by_mac), "vpn": [],
             "raw": acl_raw, "acl_digest": digest, "allow": []}
    for key in ("networks", "ssids", "wans", "ports", "dhcp_reservations"):      # ids must be unique (a reply may repeat an object)
        seen = set()
        state[key] = [o for o in state[key] if not (o["id"] in seen or seen.add(o["id"]))]
    state["findings"] = findings(state)
    site = _d(raw.get("site"))
    state["controller"] = {"version": version_of(raw), "class": CONTROLLER_CLASS, "apis": ["official"],
                           "site": text(site.get("name"), 48)}
    return state


def topology_event(raw: dict, t: float) -> dict:
    """The topology observation for the core: same shape as the generic controller source (so the resolver ranks
    it as ``controller`` and merges it with manual links, LLDP and address tables)."""
    raw = raw if isinstance(raw, dict) else {}
    devs = _first_by_mac(items(raw, "devices"))
    known = {norm_mac(d["mac"]) for d in devs}
    devices = []
    for d in devs:
        umac = norm_mac(d.get("uplinkDeviceMac"))
        port = text(d.get("uplinkDevicePort"), 32)
        devices.append({"mac": norm_mac(d["mac"]), "ip": _ip(d.get("ip")), "name": name_str(d.get("name")),
                        "role": KINDS.get(str(d.get("type") or "").lower(), "other"), "model": name_str(d.get("modelName"), d.get("model")),
                        "uplink": {"mac": umac, "port": port, "own_port": None, "medium": None} if umac and umac in known else None})
    clients = []
    for c in items(raw, "clients"):
        mac = norm_mac(c.get("mac"))
        wired, parent = _client_parent(c)
        if not mac or not parent:
            continue
        clients.append({"mac": mac, "ip": _ip(c.get("ip")), "name": name_str(c.get("name"), c.get("hostName")),
                        "medium": "wired" if wired else "radio", "parent_mac": parent,
                        "port": port_str(c.get("port")) if wired else None,
                        "band": None if wired else RADIO_BANDS.get(num(c.get("radioId"))),
                        "ssid": None if wired else name_str(c.get("ssid"))})
    return {"type": "topology", "source": "controller", "t": t, "devices": _unique(devices, 1000),
            "clients": _unique(clients, MAX_ITEMS)}
