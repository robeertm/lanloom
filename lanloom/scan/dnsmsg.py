"""Minimal DNS message builder/parser for mDNS / DNS-SD (RFC 1035, RFC 6762, RFC 6763).

Only what the scanner needs: queries for PTR/SRV/TXT/A/AAAA and parsing of answers.
The parser is defensive: every read is bounds-checked, compression pointers may
only point backwards and are limited in number, names are limited to 255 bytes.
Any malformed input raises `DNSError` (a ValueError) and nothing else.
"""
from __future__ import annotations

import ipaddress
import struct

T_A = 1
T_PTR = 12
T_TXT = 16
T_AAAA = 28
T_SRV = 33
T_ANY = 255
CLASS_IN = 1
QU_BIT = 0x8000          # "unicast response requested" in the question class (mDNS)
CACHE_FLUSH = 0x8000     # cache-flush bit in the record class (mDNS)

MAX_NAME = 255
MAX_LABEL = 63
MAX_POINTERS = 32
MAX_RECORDS = 512
MAX_TXT_ITEMS = 64
MAX_TXT_VALUE = 255


class DNSError(ValueError):
    pass


def encode_name(name: str) -> bytes:
    """Encode a dotted name into wire format (no compression)."""
    if not isinstance(name, str):
        raise DNSError("name must be str")
    name = name.rstrip(".")
    out = bytearray()
    if name:
        for label in name.split("."):
            raw = label.encode("utf-8")
            if not raw or len(raw) > MAX_LABEL:
                raise DNSError("bad label")
            out.append(len(raw))
            out += raw
    out.append(0)
    if len(out) > MAX_NAME:
        raise DNSError("name too long")
    return bytes(out)


def build_query(questions, unicast: bool = True, qid: int = 0) -> bytes:
    """Build a query. `questions` is a list of (name, qtype)."""
    qs = list(questions)
    if not qs or len(qs) > 64:
        raise DNSError("bad question count")
    header = struct.pack("!HHHHHH", qid & 0xFFFF, 0, len(qs), 0, 0, 0)
    body = bytearray()
    qclass = CLASS_IN | (QU_BIT if unicast else 0)
    for name, qtype in qs:
        body += encode_name(name) + struct.pack("!HH", int(qtype) & 0xFFFF, qclass)
    return header + bytes(body)


def read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Read a (possibly compressed) name at `offset`. Returns (name, offset after the name)."""
    labels: list[str] = []
    length_total = 0
    jumps = 0
    pos = offset
    end_after: int | None = None
    n = len(data)
    lowest = offset
    while True:
        if pos >= n:
            raise DNSError("name out of bounds")
        ln = data[pos]
        if ln & 0xC0 == 0xC0:
            if pos + 1 >= n:
                raise DNSError("pointer out of bounds")
            target = ((ln & 0x3F) << 8) | data[pos + 1]
            if end_after is None:
                end_after = pos + 2
            jumps += 1
            # pointers must go strictly backwards: rules out loops
            if jumps > MAX_POINTERS or target >= lowest:
                raise DNSError("bad compression pointer")
            lowest = target
            pos = target
            continue
        if ln & 0xC0:
            raise DNSError("reserved label type")
        if ln == 0:
            pos += 1
            break
        if pos + 1 + ln > n:
            raise DNSError("label out of bounds")
        raw = data[pos + 1:pos + 1 + ln]
        length_total += ln + 1
        if length_total > MAX_NAME:
            raise DNSError("name too long")
        labels.append(raw.decode("utf-8", errors="replace"))
        pos += 1 + ln
    return ".".join(labels), (end_after if end_after is not None else pos)


def _need(data: bytes, pos: int, size: int) -> None:
    if pos < 0 or pos + size > len(data):
        raise DNSError("truncated")


def parse_txt(rdata: bytes) -> dict:
    out: dict[str, str] = {}
    pos = 0
    items = 0
    while pos < len(rdata) and items < MAX_TXT_ITEMS:
        ln = rdata[pos]
        pos += 1
        if pos + ln > len(rdata):
            raise DNSError("txt out of bounds")
        item = rdata[pos:pos + ln]
        pos += ln
        items += 1
        if not item:
            continue
        key, sep, val = item.partition(b"=")
        k = key.decode("utf-8", errors="replace").strip().lower()[:64]
        if not k or k in out:
            continue
        out[k] = _clean(val.decode("utf-8", errors="replace"))[:MAX_TXT_VALUE] if sep else ""
    return out


def _clean(text: str) -> str:
    return "".join(ch for ch in text if ch.isprintable())


def _parse_rdata(data: bytes, rtype: int, start: int, rdlen: int):
    end = start + rdlen
    if rtype == T_A:
        if rdlen != 4:
            raise DNSError("bad A")
        return str(ipaddress.IPv4Address(data[start:end]))
    if rtype == T_AAAA:
        if rdlen != 16:
            raise DNSError("bad AAAA")
        return str(ipaddress.IPv6Address(data[start:end]))
    if rtype == T_PTR:
        name, after = read_name(data, start)
        if after > end:
            raise DNSError("PTR overruns rdata")
        return name
    if rtype == T_SRV:
        if rdlen < 7:
            raise DNSError("bad SRV")
        prio, weight, port = struct.unpack_from("!HHH", data, start)
        target, after = read_name(data, start + 6)
        if after > end:
            raise DNSError("SRV overruns rdata")
        return {"priority": prio, "weight": weight, "port": port, "target": target}
    if rtype == T_TXT:
        return parse_txt(data[start:end])
    return None


def parse_message(data: bytes) -> dict:
    """Parse a DNS message. Unknown record types are kept with data None.

    Raises DNSError for anything malformed.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise DNSError("not bytes")
    data = bytes(data)
    _need(data, 0, 12)
    qid, flags, qd, an, ns, ar = struct.unpack_from("!HHHHHH", data, 0)
    if qd + an + ns + ar > MAX_RECORDS:
        raise DNSError("too many records")
    pos = 12
    questions = []
    for _ in range(qd):
        name, pos = read_name(data, pos)
        _need(data, pos, 4)
        qtype, qclass = struct.unpack_from("!HH", data, pos)
        pos += 4
        questions.append({"name": name, "type": qtype, "unicast": bool(qclass & QU_BIT),
                          "class": qclass & 0x7FFF})
    records = []
    for section, count in (("an", an), ("ns", ns), ("ar", ar)):
        for _ in range(count):
            name, pos = read_name(data, pos)
            _need(data, pos, 10)
            rtype, rclass, ttl, rdlen = struct.unpack_from("!HHIH", data, pos)
            pos += 10
            _need(data, pos, rdlen)
            rdata = _parse_rdata(data, rtype, pos, rdlen)
            pos += rdlen
            records.append({"name": name, "type": rtype, "class": rclass & 0x7FFF, "ttl": ttl,
                            "data": rdata, "section": section})
    return {"id": qid, "flags": flags, "response": bool(flags & 0x8000),
            "questions": questions, "records": records}


# ---------------------------------------------------------------- response building (tests / fakes)

def build_record(name: str, rtype: int, rdata: bytes, ttl: int = 120, flush: bool = False) -> bytes:
    rclass = CLASS_IN | (CACHE_FLUSH if flush else 0)
    return encode_name(name) + struct.pack("!HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def rdata_ptr(target: str) -> bytes:
    return encode_name(target)


def rdata_srv(port: int, target: str, priority: int = 0, weight: int = 0) -> bytes:
    return struct.pack("!HHH", priority, weight, port) + encode_name(target)


def rdata_txt(items: dict) -> bytes:
    out = bytearray()
    for k, v in items.items():
        raw = (f"{k}={v}" if v is not None else str(k)).encode("utf-8")[:255]
        out.append(len(raw))
        out += raw
    return bytes(out) or b"\x00"


def rdata_a(ip: str) -> bytes:
    return ipaddress.IPv4Address(ip).packed


def build_response(answers: list[bytes], additionals: list[bytes] = (), qid: int = 0) -> bytes:
    header = struct.pack("!HHHHHH", qid & 0xFFFF, 0x8400, 0, len(answers), 0, len(additionals))
    return header + b"".join(answers) + b"".join(additionals)
