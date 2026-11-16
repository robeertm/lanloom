"""NetBIOS node-status query (UDP 137): one question per known host, answer must come from that host."""
from __future__ import annotations

import random
import struct

from . import _udp

PORT = 137
MAX_NAMES = 16
NBSTAT = 0x21


def _encode_star() -> bytes:
    raw = b"*" + b"\x00" * 15
    return b"".join(bytes((0x41 + (b >> 4), 0x41 + (b & 15))) for b in raw)


def build_nbstat_query(tid: int) -> bytes:
    return struct.pack("!HHHHHH", tid & 0xFFFF, 0, 1, 0, 0, 0) + b"\x20" + _encode_star() + b"\x00" \
        + struct.pack("!HH", NBSTAT, 1)


def _clean(raw: bytes) -> str:
    text = raw.decode("latin-1")
    return "".join(ch if ch.isprintable() else " " for ch in text).strip()


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        if pos >= len(data):
            raise ValueError("truncated")
        ln = data[pos]
        if ln & 0xC0 == 0xC0:
            return pos + 2
        if ln & 0xC0:
            raise ValueError("bad label")
        pos += 1 + ln
        if ln == 0:
            return pos


def parse_nbstat_response(data: bytes, tid: int | None = None) -> dict | None:
    """Parse a node-status answer. With `tid` the transaction id must match."""
    try:
        if not data or len(data) > 4096 or len(data) < 12:
            return None
        rid, flags, qd, an = struct.unpack_from("!HHHH", data, 0)
        if tid is not None and rid != (tid & 0xFFFF):
            return None
        if not flags & 0x8000 or flags & 0x000F or an < 1 or qd > 1:
            return None
        pos = 12
        for _ in range(qd):
            pos = _skip_name(data, pos) + 4
        pos = _skip_name(data, pos)
        rtype, _rclass, _ttl, rdlen = struct.unpack_from("!HHIH", data, pos)
        pos += 10
        if rtype != NBSTAT or pos + rdlen > len(data) or rdlen < 1:
            return None
        count = data[pos]
        pos += 1
        names = []
        computer = workgroup = None
        for i in range(count):
            if pos + 18 > len(data):
                return None
            name = _clean(data[pos:pos + 15])
            suffix = data[pos + 15]
            group = bool(struct.unpack_from("!H", data, pos + 16)[0] & 0x8000)
            pos += 18
            if not name:
                continue
            if len(names) < MAX_NAMES:
                names.append({"name": name, "suffix": suffix, "group": group})
            if suffix == 0:
                if not group and computer is None:
                    computer = name
                elif group and workgroup is None:
                    workgroup = name
        mac = None
        if pos + 6 <= len(data) and any(data[pos:pos + 6]):
            mac = ":".join("%02x" % b for b in data[pos:pos + 6])
        if not names:
            return None
        return {"names": names, "computer": computer, "workgroup": workgroup, "mac": mac}
    except (struct.error, ValueError):
        return None


async def query(ips, limiter_or_bucket, timeout: float = 1.0, on_sent=None) -> dict[str, dict]:
    """One node-status question per host -> {ip: parse result}."""
    used: set[int] = set()

    def make(_ip):
        tid = random.randrange(0x10000)
        while tid in used and len(used) < 0x10000:
            tid = random.randrange(0x10000)
        used.add(tid)
        return tid, build_nbstat_query(tid)

    return await _udp.ask(ips, PORT, make, parse_nbstat_response, limiter_or_bucket, timeout, on_sent)
