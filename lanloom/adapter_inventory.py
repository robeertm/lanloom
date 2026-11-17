"""Devices that only an adapter knows: clients of a controller in other VLANs, hardware addresses learned by a switch
(forwarding table), neighbours announced over LLDP.

No packet is sent for any of them; the records come from the topology observations the live sources already deliver
(``core.topo_obs``). A record has a hardware address and, when the source knows it, an IP address, a name and where it
hangs (parent hardware address, port, medium). Devices without an IP address are first-class: they are kept under
their hardware address and the identity stage (``identity.groups``) folds them into the device they belong to - a
second port of a router, a wireless and a wired card of one laptop - whenever the evidence is strong enough.

Pure functions, no I/O.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

from . import topology

MAX_RECORDS = 3000
ROLE_KINDS = {"router": "router", "switch": "switch", "ap": "ap"}


def _ip(value):
    try:
        a = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None
    return str(a) if a.version == 4 and not (a.is_unspecified or a.is_multicast or a.is_loopback) else None


def _usable_mac(mac: str) -> bool:
    """A unicast hardware address (broadcast, multicast and all-zero addresses are not devices)."""
    mac = topology.norm_mac(mac)
    return bool(mac) and not int(mac[:2], 16) & 1 and mac != "ff:ff:ff:ff:ff:ff"


def _name(value) -> str:
    return " ".join(str(value or "").split())[:64]


def records(topo_obs: dict, own_macs: Iterable = ()) -> list:
    """One record per hardware address from all topology observations, richest source first.

    ``own_macs``: hardware addresses of this host (never reported as a device found by an adapter)."""
    own = {topology.norm_mac(m) for m in own_macs}
    out: dict = {}

    def add(mac, source, ip=None, name=None, role=None, port=None, parent=None, medium=None, vlan=None, agent=None):
        mac = topology.norm_mac(mac)
        if not _usable_mac(mac) or mac in own or len(out) >= MAX_RECORDS and mac not in out:
            return
        rec = out.setdefault(mac, {"mac": mac, "ip": None, "name": "", "role": None, "sources": [], "port": None,
                                   "parent_mac": None, "medium": None, "vlan": None, "agent": None})
        if source not in rec["sources"]:
            rec["sources"].append(source)
        rec["ip"] = rec["ip"] or _ip(ip)
        rec["name"] = rec["name"] or _name(name)
        rec["role"] = rec["role"] or (role if role in ROLE_KINDS else None)
        rec["port"] = rec["port"] or topology.clean_port(port)
        rec["parent_mac"] = rec["parent_mac"] or (topology.norm_mac(parent) or None)
        rec["medium"] = rec["medium"] or (medium if medium in topology.MEDIA else None)
        if isinstance(vlan, int) and not isinstance(vlan, bool) and 0 < vlan < 4095:
            rec["vlan"] = rec["vlan"] or vlan
        rec["agent"] = rec["agent"] or agent

    for key, entry in sorted((topo_obs or {}).items()):
        ev = (entry or {}).get("ev") or {}
        src = ev.get("source")
        for d in ev.get("devices") or []:
            add(d.get("mac"), "controller", ip=d.get("ip"), name=d.get("name"), role=d.get("role"),
                parent=(d.get("uplink") or {}).get("mac"), port=(d.get("uplink") or {}).get("port"))
        for c in ev.get("clients") or []:
            add(c.get("mac"), "controller", ip=c.get("ip"), name=c.get("name"), port=c.get("port"),
                parent=c.get("parent_mac"), medium=c.get("medium"), vlan=c.get("vlan"))
        for n in ev.get("lldp") or []:
            add(n.get("chassis_mac"), "lldp", ip=n.get("mgmt_ip") or n.get("chassis_ip"), name=n.get("sys_name"),
                role=_lldp_role(n))
        agent_mac = topology.norm_mac((ev.get("agent") or {}).get("mac"))
        if src == "snmp":
            for row in ev.get("fdb") or []:
                add(row.get("mac"), "fdb", port=row.get("port"), parent=agent_mac, medium="wired",
                    agent=agent_mac or None)
    return sorted(out.values(), key=lambda r: r["mac"])


def _lldp_role(n: dict):
    caps = set(n.get("caps") or [])
    if "ap" in caps:
        return "ap"
    if "router" in caps and "bridge" not in caps:
        return "router"
    if caps & {"bridge", "repeater"}:
        return "switch"
    return None


def host_of(rec: dict) -> dict:
    """A scan-result style host for the classifier and the store. Marked with the source ``adapter``."""
    return {"ip": rec.get("ip") or "", "mac": rec["mac"], "hostnames": [rec["name"]] if rec.get("name") else [],
            "sources": ["adapter"], "ports": [], "mdns": [], "ssdp": [], "ipv6": [], "wsd": [], "netbios": None,
            "snmp": None, "is_gateway": False, "is_self": False, "rtt_ms": None,
            "adapter": {"sources": list(rec["sources"]), "port": rec.get("port"), "parent_mac": rec.get("parent_mac"),
                        "medium": rec.get("medium"), "vlan": rec.get("vlan"), "agent": rec.get("agent"),
                        "role": rec.get("role")}}


def is_adapter_only(data: dict) -> bool:
    return (data.get("sources") or []) == ["adapter"]
