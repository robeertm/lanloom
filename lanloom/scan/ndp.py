"""IPv6 neighbour table, read passively over netlink (no packets are sent, no privileges needed).

Only a dump request for the kernel's neighbour cache is written to the kernel socket; the answer lists the
IPv6 addresses that were already learned for each link-layer address.
"""
from __future__ import annotations

import ipaddress
import socket
import struct

NETLINK_ROUTE = 0
RTM_NEWNEIGH = 28
RTM_GETNEIGH = 30
NLMSG_ERROR = 2
NLMSG_DONE = 3
NLM_F_REQUEST = 0x1
NLM_F_DUMP = 0x300
NDA_DST = 1
NDA_LLADDR = 2
NUD_REACHABLE, NUD_STALE, NUD_DELAY, NUD_PROBE, NUD_PERMANENT = 0x02, 0x04, 0x08, 0x10, 0x80
_STATES = {NUD_REACHABLE: "reachable", NUD_STALE: "stale", NUD_DELAY: "delay", NUD_PROBE: "probe",
           NUD_PERMANENT: "permanent"}
MAX_BUFFER = 1 << 20
MAX_PER_MAC = 8
_HDR = struct.Struct("=IHHII")
_NDMSG = struct.Struct("=BBHiHBB")


def _state_name(bits: int) -> str | None:
    for flag in (NUD_PERMANENT, NUD_REACHABLE, NUD_DELAY, NUD_PROBE, NUD_STALE):
        if bits & flag:
            return _STATES[flag]
    return None


def parse_neigh_dump(data: bytes) -> list[dict]:
    """Parse netlink RTM_NEWNEIGH messages -> [{"ip","mac","iface_index","state"}] (IPv6 only)."""
    out: list[dict] = []
    if not isinstance(data, (bytes, bytearray)):
        return out
    data = bytes(data[:MAX_BUFFER])
    pos = 0
    while pos + _HDR.size <= len(data):
        length, mtype, _flags, _seq, _pid = _HDR.unpack_from(data, pos)
        if length < _HDR.size or pos + length > len(data):
            break
        end = pos + length
        if mtype == NLMSG_DONE:
            break
        if mtype == RTM_NEWNEIGH and length >= _HDR.size + _NDMSG.size:
            family, _p1, _p2, ifindex, state, _nflags, _ntype = _NDMSG.unpack_from(data, pos + _HDR.size)
            entry = _entry(data, pos + _HDR.size + _NDMSG.size, end, family, ifindex, state)
            if entry:
                out.append(entry)
        pos += (length + 3) & ~3
    return out


def _entry(data: bytes, pos: int, end: int, family: int, ifindex: int, state: int) -> dict | None:
    if family != socket.AF_INET6:
        return None
    name = _state_name(state)
    if name is None:
        return None
    dst = lladdr = None
    while pos + 4 <= end:
        alen, atype = struct.unpack_from("=HH", data, pos)
        if alen < 4 or pos + alen > end:
            break
        payload = data[pos + 4:pos + alen]
        if atype == NDA_DST and len(payload) == 16:
            dst = payload
        elif atype == NDA_LLADDR and len(payload) == 6:
            lladdr = payload
        pos += (alen + 3) & ~3
    if dst is None or lladdr is None:
        return None
    addr = ipaddress.IPv6Address(dst)
    if addr.is_multicast or addr.is_unspecified or not any(lladdr):
        return None
    mac = ":".join("%02x" % b for b in lladdr)
    return {"ip": str(addr), "mac": mac, "iface_index": ifindex, "state": name}


def group_by_mac(entries) -> dict[str, list[str]]:
    """{mac: [ipv6]} with at most MAX_PER_MAC addresses, link-local ones last."""
    per: dict[str, list[str]] = {}
    for e in entries:
        lst = per.setdefault(e["mac"], [])
        if e["ip"] not in lst:
            lst.append(e["ip"])
    return {mac: sorted(ips, key=lambda a: (ipaddress.IPv6Address(a).is_link_local, a))[:MAX_PER_MAC]
            for mac, ips in per.items()}


def read_ndp(timeout: float = 1.0) -> dict[str, list[str]] | None:
    """Return {mac: [ipv6, ...]} or None when netlink is unavailable. Never raises."""
    try:
        family = getattr(socket, "AF_NETLINK", None)
        if family is None:
            return None
        sock = socket.socket(family, socket.SOCK_RAW, NETLINK_ROUTE)   # netlink socket type, not a raw IP socket: no privileges
    except (OSError, ValueError, AttributeError):
        return None
    try:
        sock.settimeout(max(0.05, float(timeout)))
        msg = _NDMSG.pack(socket.AF_INET6, 0, 0, 0, 0, 0, 0)
        sock.send(_HDR.pack(_HDR.size + len(msg), RTM_GETNEIGH, NLM_F_REQUEST | NLM_F_DUMP, 1, 0) + msg)
        entries: list[dict] = []
        total = 0
        done = False
        while not done and total < MAX_BUFFER:
            chunk = sock.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            entries.extend(parse_neigh_dump(chunk))
            pos = 0
            while pos + _HDR.size <= len(chunk):
                length, mtype = struct.unpack_from("=IH", chunk, pos)
                if length < _HDR.size:
                    break
                if mtype in (NLMSG_DONE, NLMSG_ERROR):
                    done = True
                    break
                pos += (length + 3) & ~3
        return group_by_mac(entries)
    except Exception:
        return None
    finally:
        sock.close()
