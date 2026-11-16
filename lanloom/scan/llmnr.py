"""LLMNR reverse lookup (UDP 5355, unicast to the host): one PTR question per host without a name."""
from __future__ import annotations

import random

from . import _udp, dnsmsg

PORT = 5355


def reverse_name(ip: str) -> str:
    return ".".join(reversed(ip.split("."))) + ".in-addr.arpa"


def parse_answer(data: bytes, token) -> str | None:
    tid, expected = token
    if not data or len(data) > 4096:
        return None
    try:
        msg = dnsmsg.parse_message(data)
    except dnsmsg.DNSError:
        return None
    if msg["id"] != tid or not msg["response"] or msg["flags"] & 0x000F:
        return None
    for rec in msg["records"]:
        if rec["type"] == dnsmsg.T_PTR and isinstance(rec["data"], str) and rec["name"].lower() == expected:
            return _udp.clean_text(rec["data"].rstrip("."), 253)
    return None


async def reverse(ips, bucket, timeout: float = 1.0, on_sent=None) -> dict[str, str]:
    """One PTR question per host -> {ip: name}."""
    def make(ip):
        tid = random.randrange(0x10000)
        expected = reverse_name(ip)
        return (tid, expected), dnsmsg.build_query([(expected, dnsmsg.T_PTR)], unicast=False, qid=tid)

    res = await _udp.ask(ips, PORT, make, parse_answer, bucket, timeout, on_sent)
    return {ip: n for ip, n in res.items() if n}
