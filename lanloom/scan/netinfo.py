"""Detect the local IPv4 networks (read-only, no privileges).

Sources: /proc/net/route (routes + default gateway) and interface addresses
via `socket.if_nameindex()` + SIOCGIFADDR / SIOCGIFNETMASK ioctls.
Only implemented for the /proc-based platform; elsewhere `detect_networks()`
returns an empty list.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import struct
import sys

from . import safety

ROUTE_PATH = "/proc/net/route"
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
RTF_UP = 0x1
RTF_GATEWAY = 0x2

# virtual / container interfaces that are skipped unless nothing else exists
VIRTUAL_PREFIXES = ("docker", "veth", "br-", "virbr", "cni", "vxlan", "tun", "tap", "wg", "lo")  # identifier: interface name prefixes
CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")  # identifier: container runtime marker files
DEFAULT_BRIDGE = ipaddress.ip_network("172.16.0.0/12")


def _hex_ip(h: str) -> str | None:
    try:
        return socket.inet_ntoa(struct.pack("<I", int(h, 16)))
    except (ValueError, struct.error, OSError):
        return None


def parse_routes(text: str) -> list[dict]:
    """Parse /proc/net/route -> [{"iface","dest","gateway","mask","flags","metric"}]."""
    routes = []
    for line in text.splitlines()[1:]:
        p = line.split()
        if len(p) < 8:
            continue
        try:
            flags = int(p[3], 16)
            metric = int(p[6])
        except ValueError:
            continue
        dest, gw, mask = _hex_ip(p[1]), _hex_ip(p[2]), _hex_ip(p[7])
        if dest is None or gw is None or mask is None:
            continue
        routes.append({"iface": p[0], "dest": dest, "gateway": gw, "mask": mask,
                       "flags": flags, "metric": metric})
    return routes


def is_virtual(name: str) -> bool:
    return name.startswith(VIRTUAL_PREFIXES)


def _iface_addrs() -> list[tuple[str, str, str]]:
    """[(iface, ip, netmask)] via ioctl. Empty on failure."""
    try:
        import fcntl
    except ImportError:
        return []
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _idx, name in socket.if_nameindex():
            req = struct.pack("256s", name.encode("utf-8", errors="replace")[:15])
            try:
                ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, req)[20:24])
            except OSError:
                continue
            out.append((name, ip, mask))
    except OSError:
        return out
    finally:
        s.close()
    return out


def _in_container() -> bool:
    return any(os.path.exists(p) for p in CONTAINER_MARKERS)


def build_networks(addrs, routes, in_container: bool) -> list[dict]:
    """Pure part of detect_networks (testable)."""
    gw_by_iface: dict[str, str] = {}
    for r in sorted(routes, key=lambda r: r["metric"]):
        if r["dest"] == "0.0.0.0" and r["mask"] == "0.0.0.0" and r["flags"] & RTF_GATEWAY and r["flags"] & RTF_UP:
            gw_by_iface.setdefault(r["iface"], r["gateway"])
    items = []
    for name, ip, mask in addrs:
        try:
            iface = ipaddress.IPv4Interface(f"{ip}/{mask}")
        except ValueError:
            continue
        if iface.ip.is_loopback or iface.ip.is_unspecified:
            continue
        net = iface.network
        private = any(net.subnet_of(p) for p in safety.PRIVATE_V4) or net.subnet_of(safety.LINK_LOCAL_V4)
        items.append({"cidr": str(net), "iface": name, "self_ip": str(iface.ip),
                      "gateway": gw_by_iface.get(name), "private": bool(private),
                      "size": net.num_addresses, "host_mode": False, "_virtual": is_virtual(name)})
    physical = [i for i in items if not i["_virtual"]]
    any_virtual = any(is_virtual(a[0]) and a[0] != "lo" for a in addrs)
    # "bridge container": inside a container, a single eth with an address from the container
    # bridge pool and no bridges/veths of its own
    bridge_container = (in_container and not any_virtual and len(physical) == 1
                        and ipaddress.ip_network(physical[0]["cidr"]).subnet_of(DEFAULT_BRIDGE))
    host_mode = bool(physical) and not bridge_container
    chosen = physical if physical else items
    for i in chosen:
        i["host_mode"] = host_mode if physical else False
        del i["_virtual"]
    # default-route interface first, then private ranges, then by size
    chosen.sort(key=lambda i: (i["gateway"] is None, not i["private"], i["size"], i["cidr"]))
    return chosen


def detect_networks() -> list[dict]:
    if not sys.platform.startswith("linux") or not os.path.exists(ROUTE_PATH):  # identifier: platform check
        return []
    try:
        with open(ROUTE_PATH, encoding="ascii", errors="replace") as fh:
            routes = parse_routes(fh.read(1 << 20))
    except OSError:
        routes = []
    try:
        return build_networks(_iface_addrs(), routes, _in_container())
    except (OSError, ValueError):
        return []
