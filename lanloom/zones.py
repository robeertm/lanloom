"""Network zones: every IPv4 network Lanloom knows about, and how far it may look into each.

Where the networks come from
    * ``local``   - the networks of this host's own interfaces (the scanner reaches them directly);
    * ``adapter`` - the networks a controller adapter publishes (its networks and VLANs, over every VLAN of the site,
                    without a scan);
    * ``route``   - private networks behind a router of this host (listed only, from the routing table).

How far Lanloom may look (``reach``)
    * ``scan``    - on a link of this host: the scanner may sweep it (private range of at most /22);
    * ``consent`` - behind a router, but the user agreed to a scan across it (kept per range, revocable);
    * ``adapter`` - behind a router, no consent: filled **only from adapter data**; nothing is sent into it;
    * ``blocked`` - never scanned (public, carrier-grade NAT, larger than /22, ...): shown with the reason.

Pure functions, no I/O. The core passes in what it knows; ``build`` returns one entry per range, ``assign`` puts the
devices into them.
"""
from __future__ import annotations

import ipaddress

from .scan import safety

MAX_ZONES = 64
REACH = ("scan", "consent", "adapter", "blocked")


def _net(cidr):
    try:
        net = ipaddress.ip_network(str(cidr), strict=False)
    except ValueError:
        return None
    return net if net.version == 4 else None


def _entry(net) -> dict:
    return {"cidr": str(net), "name": None, "vlan": None, "purpose": None, "sources": [], "iface": None, "self_ip": None,
            "gateway": None, "dhcp": None, "isolated": None, "internet": None, "reach": "adapter", "reason": "",
            "size": net.num_addresses, "adapters": [], "devices": 0, "adapter_devices": 0, "ids": []}


def build(local_nets, adapter_states, routes=(), consent=()) -> list:
    """Zones from this host's networks, the adapters' neutral states ([{"key", "name", "state"}]) and the routes
    ([{"dest", "mask", "gateway", "flags"}]); ``consent`` lists the ranges the user allowed to be scanned across a
    router. Sorted: reachable first, then by VLAN and address."""
    zones: dict = {}

    def get(net):
        key = str(net)
        if key not in zones:
            zones[key] = _entry(net)
        return zones[key]

    link = []
    for n in local_nets or []:
        net = _net(n.get("cidr"))
        if net is None:
            continue
        e = get(net)
        e["sources"] = sorted(set(e["sources"]) | {"local"})
        e["iface"], e["self_ip"], e["gateway"] = n.get("iface"), n.get("self_ip"), n.get("gateway")
        link.append(net)
    for src in adapter_states or []:
        st = src.get("state") or {}
        for n in st.get("networks") or []:
            net = _net(n.get("cidr"))
            if net is None or len(zones) >= MAX_ZONES and str(net) not in zones:
                continue
            e = get(net)
            e["sources"] = sorted(set(e["sources"]) | {"adapter"})
            e["name"] = e["name"] or (n.get("name") if n.get("name") not in (None, "?") else None)
            e["vlan"] = e["vlan"] if e["vlan"] is not None else n.get("vlan")
            e["purpose"] = e["purpose"] or n.get("purpose")
            if e["dhcp"] is None and isinstance(n.get("dhcp"), dict):
                e["dhcp"] = bool(n["dhcp"].get("on"))
            e["isolated"] = e["isolated"] if e["isolated"] is not None else n.get("isolated")
            e["internet"] = e["internet"] if e["internet"] is not None else n.get("internet")
            e["gateway"] = e["gateway"] or n.get("gateway_ip")
            e["adapters"] = sorted(set(e["adapters"]) | {src.get("name") or src.get("key") or ""})
    for r in routes or []:
        if not r.get("flags", 0) & 2 or r.get("dest") in (None, "0.0.0.0"):
            continue
        net = _net("%s/%s" % (r["dest"], r["mask"]))
        if net is None or net.prefixlen < 16 or (str(net) not in zones and len(zones) >= MAX_ZONES):
            continue
        e = get(net)
        e["sources"] = sorted(set(e["sources"]) | {"route"})
        e["gateway"] = e["gateway"] or r.get("gateway")
    agreed = {str(_net(c)) for c in consent or [] if _net(c) is not None}
    for key, e in zones.items():
        net = ipaddress.ip_network(key)
        ok, reason = safety.check_cidr(key)
        on_link = any(net.overlaps(l) for l in link)
        if not ok:
            e["reach"], e["reason"] = "blocked", reason
        elif on_link:
            e["reach"] = "scan"
        elif key in agreed:
            e["reach"] = "consent"
        else:
            e["reach"], e["reason"] = "adapter", "zone.reason.behind_router"
    order = {"scan": 0, "consent": 1, "adapter": 2, "blocked": 3}
    return sorted(zones.values(), key=lambda z: (order[z["reach"]], z["vlan"] if isinstance(z["vlan"], int) else 1 << 20,
                                                 int(ipaddress.ip_network(z["cidr"]).network_address)))


def assign(zones: list, devices) -> list:
    """Count the devices of each zone (the smallest range that holds the address). Returns the zones with ``devices``,
    ``adapter_devices`` (devices only an adapter reported) and ``ids`` (at most 200) filled; a device without an
    address is left out. Returns ``(zones, unzoned)``: ``unzoned`` lists the ids of devices no zone holds."""
    nets = sorted(((ipaddress.ip_network(z["cidr"]), z) for z in zones), key=lambda p: -p[0].prefixlen)
    for z in zones:
        z["devices"], z["adapter_devices"], z["ids"] = 0, 0, []
    unzoned = []
    for d in devices:
        ip = d.get("ip")
        if not ip:
            continue
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        hit = next((z for net, z in nets if addr.version == net.version and addr in net), None)
        if hit is None:
            unzoned.append(d["id"])
            continue
        hit["devices"] += 1
        if (d.get("sources") or []) == ["adapter"]:
            hit["adapter_devices"] += 1
        if len(hit["ids"]) < 200:
            hit["ids"].append(d["id"])
    return zones, unzoned


def label(zone: dict) -> dict:
    """Title parts for a map zone: ``{"name", "vlan", "cidr"}`` (name may be None)."""
    return {"name": zone.get("name"), "vlan": zone.get("vlan"), "cidr": zone["cidr"]}


def scan_targets(zones: list) -> list:
    """Ranges an automatic rescan may include: on-link zones and consented ones, in the order of ``zones``. Zones that
    are filled from adapter data only are never part of it."""
    out = [z["cidr"] for z in zones if z["reach"] in ("scan", "consent")]
    return out[:safety.MAX_RANGES]
