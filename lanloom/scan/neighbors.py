"""Neighbour discovery without raw sockets.

1. Read the kernel neighbour table (/proc/net/arp).
2. "Nudge": send one empty UDP datagram to port 9 (discard) of every address.
   The kernel has to resolve the MAC address first, which fills the ARP table
   for every host that is on-link and alive. No ICMP, no privileges.
3. Read the table again.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket

ARP_PATH = "/proc/net/arp"
NUDGE_PORT = 9          # discard service
NUDGE_RATE = 100.0      # datagrams per second
SETTLE_S = 1.5          # time for ARP resolution after the last nudge
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
ATF_COM = 0x2           # entry complete


def parse_arp(text: str) -> dict[str, dict]:
    """Parse /proc/net/arp content -> {ip: {"mac", "iface"}} for complete entries only."""
    out: dict[str, dict] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        ip, _hw, flags, mac, _mask, iface = parts[:6]
        try:
            ipaddress.IPv4Address(ip)
            fl = int(flags, 16)
        except ValueError:
            continue
        mac = mac.lower()
        if not fl & ATF_COM or not _MAC_RE.match(mac) or mac == "00:00:00:00:00:00":
            continue
        out[ip] = {"mac": mac, "iface": iface}
    return out


def read_arp(path: str = ARP_PATH) -> dict[str, dict] | None:
    """Return the parsed table, or None when the table is not available (no /proc)."""
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return parse_arp(fh.read(1 << 20))
    except OSError:
        return None


async def nudge(addresses, bucket, port: int = NUDGE_PORT) -> int:
    """Send one empty UDP datagram to each address. Returns the number of datagrams sent."""
    sent = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        for ip in addresses:
            await bucket.acquire()
            try:
                sock.sendto(b"", (ip, port))
                sent += 1
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except OSError:
                # EHOSTUNREACH / ECONNREFUSED (from an earlier ICMP error) are expected for absent hosts
                pass
    finally:
        sock.close()
    return sent
