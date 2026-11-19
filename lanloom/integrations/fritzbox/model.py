"""Translation module of the FRITZ!Box family: TR-064 replies and the mesh topology -> neutral model v2 and the
topology observation.

Pure functions, no network access. Shared by the read half and the tests, so both compute the same neutral state.

Two kinds of input are parsed here (bytes of a reply -> plain dicts), then one dict of the collected replies is
translated. The parsers refuse any document that carries a declaration (``<!``) or a processing instruction, so no
entity can be expanded; they read only the tags named below and ignore everything else.

Collected replies (every key is optional)::

    info      {model, firmware, uptime_s}            DeviceInfo GetInfo (name of the model, software version, uptime;
                                                     the device log of that reply and the serial number are never read)
    lan       {ip, mask, dhcp, first, last}          LANHostConfigManagement GetInfo
    lan_mac   hardware address of the box' LAN side  LANEthernetInterfaceConfig GetInfo
    wans      [{kind, status, uptime_s, error}]      WANIPConnection / WANPPPConnection GetStatusInfo
    hosts     [{ip, mac, active, name, interface, port, speed, guest, model}]   the host list (every device the box knows)
    mesh      {schema_version, nodes: [...]}          the mesh topology (JSON)

It builds::

    equipment     the box and the mesh devices that are not the box (repeaters, powerline), with uplink and state
    devices       every host of the box with network, parent device, port or radio band, online flag
    networks      the home network with address range and DHCP
    wans          the internet connection and its state
    findings      plain checks: a mesh device is offline, the internet connection is down

Never copied: the serial number, the device log, passwords and keys, the external address, the guest and VPN
details. Neutral ids are opaque hashes; hardware addresses are only used for the topology observation.
"""
import hashlib
import ipaddress
import re
import xml.etree.ElementTree as ET

from ...sources._ctlmap import _unique, ip_str, name_str, norm_mac

SALT = "lanloom-fritzbox:"
MAX_ITEMS = 5000
TEXT_MAX = 96
CONTROLLER_CLASS = "home_router"          # marks this family's state for its night report section (no product name in data)
BANDS = {"2G": "2.4ghz", "5G": "5ghz", "6G": "6ghz"}
MAX_DOC = 4 * 1024 * 1024
FAULT_NOT_AUTHORIZED = 606
FAULT_INVALID_ACTION = 401


# ---------------------------------------------------------------------------- small helpers
def oid(prefix: str, vendor_id) -> str:
    """Opaque, stable neutral id for an object (never the hardware address itself)."""
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


def flag(value):
    """True/False for a 0/1 or true/false field, None when it is not one."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower() if value is not None else ""
    return True if s in ("1", "true", "yes") else False if s in ("0", "false", "no") else None


def _ip(value):
    return ip_str(value if isinstance(value, str) else None)


def _list(value) -> list:
    return [v for v in value[:MAX_ITEMS] if isinstance(v, dict)] if isinstance(value, list) else []


# ---------------------------------------------------------------------------- parsers (bytes -> dicts)
def _xml_root(data):
    if isinstance(data, bytes):
        if len(data) > MAX_DOC:
            return None
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(data, str) or len(data) > MAX_DOC or "<!" in data:
        return None
    body = re.sub(r"\A\s*<\?xml[^>]{0,200}\?>", "", data)       # the one declaration a document may start with
    if "<?" in body:
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_soap(data) -> dict:
    """Answer of one SOAP call: ``{out argument: text}``; a fault becomes ``{"_fault": code}`` (the device's UPnP
    error code, 0 when it names none); ``None`` when the reply is not a SOAP envelope."""
    root = _xml_root(data)
    if root is None or _local(root.tag) != "Envelope":
        return None
    body = next((c for c in root if _local(c.tag) == "Body"), None)
    if body is None or not len(body):
        return None
    first = body[0]
    if _local(first.tag) == "Fault":
        code = next((e.text for e in first.iter() if _local(e.tag) == "errorCode"), None)
        n = num(code)
        return {"_fault": n if isinstance(n, int) else 0}
    return {_local(c.tag): (c.text or "").strip() for c in first if not len(c)}


def parse_hostlist(data) -> list:
    """Rows of the host list document (``<List><Item>...</Item></List>``), None when it is not one."""
    root = _xml_root(data)
    if root is None or _local(root.tag) != "List":
        return None
    out = []
    for item in root:
        if _local(item.tag) != "Item" or len(out) >= MAX_ITEMS:
            continue
        f = {_local(c.tag): (c.text or "").strip() for c in item if not len(c)}
        out.append({"ip": f.get("IPAddress"), "mac": f.get("MACAddress"), "active": flag(f.get("Active")),
                    "name": f.get("HostName") or f.get("FriendlyName"), "interface": f.get("InterfaceType"),
                    "port": num(f.get("X_AVM-DE_Port")), "speed": num(f.get("X_AVM-DE_Speed")),
                    "guest": flag(f.get("X_AVM-DE_Guest")), "model": f.get("X_AVM-DE_Model")})
    return out


# ---------------------------------------------------------------------------- mesh
def _iface_port(name) -> int:
    """Port number of a mesh interface called ``LAN:2`` (0/None for anything else)."""
    m = re.fullmatch(r"LAN:(\d{1,3})", str(name or ""))
    return int(m.group(1)) if m else None


def _iface_band(name):
    m = re.fullmatch(r"(?:AP|STA|WLAN)?:?(2G|5G|6G)(?::\d+)?", str(name or ""))
    return BANDS[m.group(1)] if m else None


def mesh_graph(mesh) -> dict:
    """Parsed mesh topology: ``{"nodes": {uid: node}, "links": [...], "root": uid, "parent": {uid: link info}}``.

    A node is ``{uid, mac, macs, ip, name, model, firmware, role, meshed}``. A link carries the two node uids and the
    two interface names; it is oriented from the box (the mesh master) outwards, whatever order the document lists the
    two ends in. Links that are not connected are ignored. Empty graph when the document has no usable nodes."""
    out = {"nodes": {}, "links": [], "root": None, "parent": {}, "schema": None}
    m = _d(mesh)
    out["schema"] = text(m.get("schema_version"), 16)
    ifaces = {}                                                  # interface uid -> (node uid, interface)
    for n in _list(m.get("nodes")):
        uid = text(n.get("uid"), 64)
        if not uid or uid in out["nodes"]:
            continue
        macs = {norm_mac(n.get("device_mac_address"))} - {None}
        role = str(n.get("mesh_role") or "").lower()
        node = {"uid": uid, "mac": norm_mac(n.get("device_mac_address")), "ip": _ip(n.get("ip_address")),
                "name": text(n.get("device_name"), 64), "model": text(n.get("device_model"), 48),
                "firmware": text(n.get("device_firmware_version"), 32), "role": role,
                "meshed": bool(n.get("is_meshed")) or role in ("master", "slave"), "ifaces": [], "macs": macs}
        for i in _list(n.get("node_interfaces")):
            iu = text(i.get("uid"), 64)
            if norm_mac(i.get("mac_address")):
                macs.add(norm_mac(i["mac_address"]))
            if iu:
                ifaces[iu] = (uid, text(i.get("name"), 32))
                node["ifaces"].append(iu)
        out["nodes"][uid] = node
    seen = set()
    for n in _list(m.get("nodes")):
        for i in _list(n.get("node_interfaces")):
            for lk in _list(i.get("node_links")):
                a, b = text(lk.get("node_1_uid"), 64), text(lk.get("node_2_uid"), 64)
                ia, ib = text(lk.get("node_interface_1_uid"), 64), text(lk.get("node_interface_2_uid"), 64)
                key = text(lk.get("uid"), 64) or "%s|%s" % (ia, ib)
                if key in seen or a not in out["nodes"] or b not in out["nodes"] or a == b:
                    continue
                seen.add(key)
                if str(lk.get("state") or "CONNECTED").upper() != "CONNECTED":
                    continue
                out["links"].append({"a": a, "b": b, "ia": (ifaces.get(ia) or (None, None))[1],
                                     "ib": (ifaces.get(ib) or (None, None))[1], "type": str(lk.get("type") or "").upper()})
    masters = [u for u, n in out["nodes"].items() if n["role"] == "master"]
    out["root"] = masters[0] if masters else None
    if out["root"]:                                              # breadth first from the box: the parent is the nearer end
        frontier, reached = [out["root"]], {out["root"]}
        while frontier:
            nxt = []
            for cur in frontier:
                for lk in out["links"]:
                    for here, there, ih, it in ((lk["a"], lk["b"], lk["ia"], lk["ib"]), (lk["b"], lk["a"], lk["ib"], lk["ia"])):
                        if here == cur and there not in reached:
                            reached.add(there)
                            out["parent"][there] = {"node": here, "parent_if": ih, "own_if": it, "type": lk["type"]}
                            nxt.append(there)
            frontier = nxt
    return out


# ---------------------------------------------------------------------------- hosts, equipment
def _is_wired(host) -> bool:
    return str(host.get("interface") or "").strip().lower() in ("ethernet", "lan", "usb")


def _hosts(raw) -> list:
    """Hosts with a valid hardware address, each address once (first wins)."""
    seen, out = set(), []
    for h in _list(raw.get("hosts")):
        mac = norm_mac(h.get("mac"))
        if mac and mac not in seen:
            seen.add(mac)
            out.append(dict(h, mac=mac))
    return out


def _link_medium(link_type, parent_if, own_if) -> str:
    if link_type in ("LAN", "WIRED", "ETH") or _iface_port(parent_if) or _iface_port(own_if):
        return "wired"
    return "radio" if link_type in ("WLAN", "WIFI") or _iface_band(parent_if) or _iface_band(own_if) else None


class _Tree:
    """Who hangs where, from the mesh topology when there is one, else everything on the box."""

    def __init__(self, raw: dict):
        self.graph = mesh_graph(raw.get("mesh"))
        g = self.graph
        self.by_mac = {}                                        # hardware address -> node uid
        for uid, n in g["nodes"].items():
            for mac in n["macs"]:
                self.by_mac.setdefault(mac, uid)
        info = _d(raw.get("info"))
        self.box_ip = _ip(_d(raw.get("lan")).get("ip"))
        self.box_mac = (g["nodes"][g["root"]]["mac"] if g["root"] else None) or norm_mac(raw.get("lan_mac"))
        self.box_model = text(info.get("model"), 48) or (g["nodes"][g["root"]]["model"] if g["root"] else None)
        # meshed devices other than the box: repeaters and powerline adapters of the mesh (also when no link is up)
        self.equipment_uids = [u for u, n in g["nodes"].items() if n["meshed"] and u != g["root"] and (n["mac"] or n["macs"])]

    def mac_of(self, uid):
        n = self.graph["nodes"][uid]
        return n["mac"] or next(iter(sorted(n["macs"])), None)

    def uplink_of_mac(self, mac):
        """The uplink of the mesh node that owns this hardware address (see ``uplink_of``), None when there is none."""
        uid = self.by_mac.get(mac)
        return self.uplink_of(uid) if uid else None

    def uplink_of(self, uid):
        """(parent mac or None for the box, port on the parent, own port, medium) of a mesh node, or None."""
        p = self.graph["parent"].get(uid)
        if not p:
            return None
        parent = self.graph["nodes"][p["node"]]
        return (None if p["node"] == self.graph["root"] else parent["mac"], _iface_port(p["parent_if"]),
                _iface_port(p["own_if"]), _link_medium(p["type"], p["parent_if"], p["own_if"]), _iface_band(p["parent_if"]) or _iface_band(p["own_if"]))


def _equipment(raw, tree: _Tree, hosts: list) -> tuple:
    """(equipment, hardware address -> equipment id). The box is first."""
    info = _d(raw.get("info"))
    g = tree.graph
    box_id = oid("e", tree.box_mac or "box")
    by_mac = {tree.box_mac: box_id} if tree.box_mac else {}
    active = {h["mac"]: h.get("active") for h in hosts}
    box_name = text(info.get("model"), 48) or (g["nodes"][g["root"]]["name"] if g["root"] else None) or "?"
    out = [{"id": box_id, "name": box_name, "kind": "router", "model": tree.box_model, "firmware": text(info.get("firmware"), 32),
            "mgmt_ip": tree.box_ip, "online": True, "pending": False, "uplink": None,
            "stats": {"cpu": None, "mem": None, "uptime_s": num(info.get("uptime_s")), "clients": None}}]
    macs = {uid: tree.mac_of(uid) for uid in tree.equipment_uids}
    for uid, mac in macs.items():
        by_mac[mac] = oid("e", mac)
    for uid, mac in macs.items():
        n = g["nodes"][uid]
        up = tree.uplink_of(uid)
        parent = (by_mac.get(up[0]) if up[0] else box_id) if up else None
        seen_active = [active[m] for m in n["macs"] if m in active]
        online = bool(up) or any(seen_active)                # a live link or an active host entry: the device answers
        out.append({"id": by_mac[mac], "name": n["name"] or n["model"] or "?", "kind": "ap", "model": n["model"],
                    "firmware": n["firmware"], "mgmt_ip": n["ip"], "online": online, "pending": False,
                    "uplink": {"device": parent, "port": up[1], "medium": up[3]} if parent else None,
                    "stats": {"cpu": None, "mem": None, "uptime_s": None, "clients": None}})
    return out, by_mac


def _network(raw, box_ip):
    lan = _d(raw.get("lan"))
    ip, mask = _ip(lan.get("ip")), lan.get("mask")
    cidr = None
    if ip and isinstance(mask, str):
        try:
            cidr = str(ipaddress.ip_network("%s/%s" % (ip, mask), strict=False))
        except ValueError:
            cidr = None
    if not cidr:
        return None
    first, last = _ip(lan.get("first")), _ip(lan.get("last"))
    on = lan.get("dhcp") is True
    return {"id": oid("n", "lan"), "name": "Home network", "cidr": cidr, "vlan": None, "zone": None, "purpose": "lan",
            "dhcp": {"on": on, "range": [first, last] if on and first and last else None}, "internet": True,
            "enabled": True, "isolated": False, "gateway_ip": ip, "management": True, "primary": True}


def _devices(raw, tree: _Tree, hosts: list, by_mac: dict, net) -> list:
    box_id = by_mac.get(tree.box_mac) if tree.box_mac else oid("e", "box")
    cidr = ipaddress.ip_network(net["cidr"]) if net else None
    out, count = [], {}
    rows = []
    for h in hosts:
        if h["mac"] in by_mac or h["mac"] == tree.box_mac:
            continue                                            # a mesh device or the box itself is equipment
        wired, parent, port, band = _is_wired(h), None, None, None
        up = tree.uplink_of_mac(h["mac"])
        if up:
            parent = by_mac.get(up[0]) if up[0] else box_id
            wired = up[3] == "wired" if up[3] else wired
            port, band = (up[1] if wired else None), (None if wired else up[4])
        elif h.get("active") and not tree.graph["root"]:
            parent = box_id                                      # no mesh data: the box is the nearest known device
            port = int(h["port"]) if wired and isinstance(h.get("port"), int) and h["port"] > 0 else None
        rows.append((h, wired, parent, port, band))
        if parent and port:
            count[(parent, port)] = count.get((parent, port), 0) + 1
    for h, wired, parent, port, band in rows[:MAX_ITEMS]:
        ip = _ip(h.get("ip"))
        net_id = net["id"] if net and ip and ipaddress.ip_address(ip).version == cidr.version and ipaddress.ip_address(ip) in cidr else None
        out.append({"id": oid("c", h["mac"]), "name": text(h.get("name")) or "?", "kind": "client", "network": net_id, "ip": ip,
                    "fixed": False, "fixed_ip": None, "wired": wired,
                    "alone_on_port": bool(wired and parent and port and count.get((parent, port)) == 1),
                    "private_address": bool(int(h["mac"][:2], 16) & 2), "protected": None,
                    "parent": parent, "port": str(port) if port else None, "ssid": None, "band": band if not wired else None,
                    "blocked": False, "guest": bool(h.get("guest")), "online": bool(h.get("active")),
                    "network_by_port": False})
    return out


def _wans(raw) -> list:
    out, seen = [], set()
    for w in _list(raw.get("wans")):
        kind = text(w.get("kind"), 8) or "ip"
        status = str(w.get("status") or "").lower()
        if status in ("", "unconfigured") or kind in seen:
            continue
        seen.add(kind)
        up = status == "connected"
        out.append({"id": oid("w", kind), "name": "Internet", "type": "ppp" if kind == "ppp" else "ip", "failover": None, "enabled": True,
                    "up": up, "link": up, "latency_ms": None, "loss": None, "device": None})
    return out


# ---------------------------------------------------------------------------- checks
def findings(state: dict) -> list:
    """Plain checks, most important first. Kinds (texts in the translations as fwr.finding.<kind>):
    ``wan_down``, ``mesh_device_offline``."""
    out = []

    def add(kind, level, subject, **params):
        out.append({"id": oid("f", "%s:%s" % (kind, subject)), "kind": kind, "level": level, "subject": subject, "params": params})

    for w in state.get("wans") or []:
        if w.get("up") is False:
            add("wan_down", "alert", w["id"], wan=w.get("name"))
    for e in state.get("equipment") or []:
        if not e.get("online") and not e.get("pending") and e.get("kind") != "router":
            add("mesh_device_offline", "warn", e["id"], device=e.get("name"))
    order = {"alert": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: order[f["level"]])
    return out


# ---------------------------------------------------------------------------- the whole state
def to_neutral(raw: dict) -> dict:
    """Neutral model v2 from whatever replies are present. Raises nothing for missing areas (they stay empty)."""
    raw = raw if isinstance(raw, dict) else {}
    tree = _Tree(raw)
    hosts = _hosts(raw)
    eq, by_mac = _equipment(raw, tree, hosts)
    net = _network(raw, tree.box_ip)
    devices = _devices(raw, tree, hosts, by_mac, net)
    clients_of = {}
    for d in devices:
        if d["parent"] and d["online"]:
            clients_of[d["parent"]] = clients_of.get(d["parent"], 0) + 1
    for e in eq:
        e["stats"]["clients"] = clients_of.get(e["id"])
    state = {"v": 2, "zones": [], "networks": [net] if net else [], "devices": devices, "equipment": eq, "ports": [],
             "policies": [], "ssids": [], "dhcp_reservations": [], "port_forwards": [], "wans": _wans(raw), "vpn": [],
             "raw": [], "acl_digest": [], "allow": []}
    state["findings"] = findings(state)
    info = _d(raw.get("info"))
    state["controller"] = {"version": text(info.get("firmware"), 32) or "", "class": CONTROLLER_CLASS, "apis": ["official"],
                           "site": None, "mesh_schema": tree.graph["schema"], "mesh": bool(tree.graph["root"])}
    return state


def topology_event(raw: dict, t: float) -> dict:
    """The topology observation for the core: same shape as the generic controller source (so the resolver ranks
    it as ``controller`` and merges it with manual links, LLDP and address tables). Only devices that are active
    now appear as clients."""
    raw = raw if isinstance(raw, dict) else {}
    tree = _Tree(raw)
    hosts = _hosts(raw)
    devices, clients = [], []
    box_mac = tree.box_mac
    if box_mac:
        devices.append({"mac": box_mac, "ip": tree.box_ip, "name": name_str(_d(raw.get("info")).get("model"), tree.box_model),
                        "role": "router", "model": name_str(tree.box_model), "uplink": None})
    eq_macs = {box_mac} if box_mac else set()
    for uid in tree.equipment_uids:
        n = tree.graph["nodes"][uid]
        mac = tree.mac_of(uid)
        eq_macs.add(mac)
        up = tree.uplink_of(uid)
        parent = (up[0] or box_mac) if up else None
        devices.append({"mac": mac, "ip": n["ip"], "name": name_str(n["name"], n["model"]), "role": "ap", "model": name_str(n["model"]),
                        "uplink": {"mac": parent, "port": str(up[1]) if up[1] else None, "own_port": str(up[2]) if up[2] else None,
                                   "medium": up[3]} if parent else None})
    for h in hosts:
        mac = h["mac"]
        if mac in eq_macs or not h.get("active"):
            continue
        wired, parent, port, band = _is_wired(h), None, None, None
        up = tree.uplink_of_mac(mac)
        if up:
            parent = up[0] or box_mac
            wired = up[3] == "wired" if up[3] else wired
            port, band = (up[1] if wired else None), (None if wired else up[4])
        elif not tree.graph["root"]:
            parent = box_mac
            port = int(h["port"]) if wired and isinstance(h.get("port"), int) and h["port"] > 0 else None
        if not parent:
            continue
        clients.append({"mac": mac, "ip": _ip(h.get("ip")), "name": name_str(h.get("name")), "medium": "wired" if wired else "radio",
                        "parent_mac": parent, "port": str(port) if port else None, "band": band if not wired else None, "ssid": None})
    return {"type": "topology", "source": "controller", "t": t, "devices": _unique(devices, 1000), "clients": _unique(clients, MAX_ITEMS)}
