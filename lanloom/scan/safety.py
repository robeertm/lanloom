"""Scan safety rules.

The scanner only ever touches private address space that the user explicitly
chose. Everything else is refused before a single packet is sent.

Allowed:
  * RFC 1918 IPv4 ranges (10/8, 172.16/12, 192.168/16)
  * IPv4 link-local (169.254/16), subject to the size limits below

Refused:
  * public / reserved / unspecified / broadcast addresses
  * carrier-grade NAT (100.64/10), loopback, multicast
  * any IPv6 range (an IPv6 subnet is far too large to sweep)
  * ranges larger than /22 (1024 addresses) in gentle mode or /24 in deep mode
  * deep mode without explicit consent

Every refusal carries a reason key (an i18n key) instead of a free text.
"""
from __future__ import annotations

import ipaddress

OK = "scan.ok"
R_INVALID = "scan.unsafe.invalid"
R_EMPTY = "scan.unsafe.empty"
R_IPV6 = "scan.unsafe.ipv6"
R_PUBLIC = "scan.unsafe.public"
R_CGNAT = "scan.unsafe.cgnat"
R_LOOPBACK = "scan.unsafe.loopback"
R_MULTICAST = "scan.unsafe.multicast"
R_TOO_LARGE = "scan.unsafe.too_large"
R_TOO_MANY = "scan.unsafe.too_many"
R_CONSENT = "scan.unsafe.consent"

PRIVATE_V4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
MULTICAST_V4 = ipaddress.ip_network("224.0.0.0/4")

MAX_ADDRESSES_GENTLE = 1024   # /22
MAX_ADDRESSES_DEEP = 256      # /24
MAX_TOTAL_GENTLE = 4096       # sum over all ranges of one plan
MAX_TOTAL_DEEP = 1024
MAX_RANGES = 8


def parse_cidr(cidr) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """Parse a CIDR or single address leniently (host bits are cleared). None if invalid."""
    if not isinstance(cidr, str):
        return None
    text = cidr.strip()
    if not text or len(text) > 64:
        return None
    try:
        return ipaddress.ip_network(text, strict=False)
    except (ValueError, TypeError):
        return None


def _within(net, parent) -> bool:
    return net.version == parent.version and net.subnet_of(parent)


def _overlaps(net, other) -> bool:
    return net.version == other.version and net.overlaps(other)


def check_cidr(cidr, deep: bool = False) -> tuple[bool, str]:
    """Return (ok, reason_key) for one range."""
    net = parse_cidr(cidr)
    if net is None:
        return False, R_INVALID
    if net.version != 4:
        return False, R_IPV6
    if _overlaps(net, LOOPBACK_V4):
        return False, R_LOOPBACK
    if _overlaps(net, MULTICAST_V4):
        return False, R_MULTICAST
    if _overlaps(net, CGNAT_V4):
        return False, R_CGNAT
    allowed = any(_within(net, p) for p in PRIVATE_V4) or _within(net, LINK_LOCAL_V4)
    if not allowed:
        return False, R_PUBLIC
    limit = MAX_ADDRESSES_DEEP if deep else MAX_ADDRESSES_GENTLE
    if net.num_addresses > limit:
        return False, R_TOO_LARGE
    return True, OK


def is_scannable_ip(ip, networks) -> bool:
    """True when `ip` is a valid IPv4 address inside one of the (already checked) networks."""
    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return False
    return any(addr.version == n.version and addr in n for n in networks)


def check_plan_ranges(cidrs, deep: bool) -> tuple[bool, str, list]:
    """Validate a list of ranges together. Returns (ok, reason_key, normalized networks)."""
    if not isinstance(cidrs, (list, tuple)) or not cidrs:
        return False, R_EMPTY, []
    if len(cidrs) > MAX_RANGES:
        return False, R_TOO_MANY, []
    nets: list = []
    for c in cidrs:
        ok, key = check_cidr(c, deep=deep)
        if not ok:
            return False, key, []
        net = parse_cidr(c)
        if any(_within(net, n) for n in nets):
            continue
        nets = [n for n in nets if not _within(n, net)]
        nets.append(net)
    total = sum(n.num_addresses for n in nets)
    if total > (MAX_TOTAL_DEEP if deep else MAX_TOTAL_GENTLE):
        return False, R_TOO_LARGE, []
    return True, OK, sorted(nets)


def host_addresses(net) -> list[str]:
    """Addresses worth probing in a range (network/broadcast excluded for prefixes shorter than /31)."""
    if net.prefixlen >= 31:
        return [str(a) for a in net]
    return [str(a) for a in net.hosts()]
