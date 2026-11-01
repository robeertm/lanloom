"""Multicast socket helpers shared by mDNS and SSDP.

Queries go out once per local interface address of the scanned ranges
(IP_MULTICAST_IF), so they leave on the right interface even when the
routing table has no route for 224.0.0.0/4 (e.g. a network without a
default gateway). Without interface addresses the system default is used.
"""
from __future__ import annotations

import socket


def query_sockets(iface_ips=(), ttl: int = 255) -> list[socket.socket]:
    """UDP sockets bound to 0.0.0.0:<ephemeral>, one per interface address (or one default socket)."""
    socks: list[socket.socket] = []
    for ip in list(iface_ips) or [None]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setblocking(False)
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            except OSError:
                pass
            if ip:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.bind(("0.0.0.0", 0))
            socks.append(s)
        except OSError:
            s.close()
    if not socks and iface_ips:
        return query_sockets((), ttl)
    return socks


def listen_socket(group: str, port: int, iface_ips=()) -> socket.socket | None:
    """Socket on `port` (shared via SO_REUSEADDR/SO_REUSEPORT) joined to `group`; None on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", port))
        joined = 0
        for ip in list(iface_ips) or ["0.0.0.0"]:
            try:
                mreq = socket.inet_aton(group) + socket.inet_aton(ip)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined += 1
            except OSError:
                continue
        if not joined:
            s.close()
            return None
        s.setblocking(False)
        return s
    except OSError:
        s.close()
        return None
