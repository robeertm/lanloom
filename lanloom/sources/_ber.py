"""Minimal, strict BER encoder/decoder for the SNMP reader (definite lengths, single-byte tags only).

Decoding never reads past a buffer limit, nesting depth is bounded and every step consumes at least two bytes,
so malformed or hostile datagrams can only raise ``BerError``.
"""
from __future__ import annotations


class BerError(ValueError):
    """Malformed or unsupported BER encoding."""


MAX_DEPTH = 10
MAX_OID_PARTS = 128

T_INTEGER = 0x02
T_OCTETS = 0x04
T_NULL = 0x05
T_OID = 0x06
T_SEQUENCE = 0x30
T_IPADDRESS = 0x40
T_COUNTER32 = 0x41
T_GAUGE32 = 0x42
T_TIMETICKS = 0x43
T_OPAQUE = 0x44
T_COUNTER64 = 0x46
T_NO_SUCH_OBJECT = 0x80
T_NO_SUCH_INSTANCE = 0x81
T_END_OF_MIB = 0x82


# ------------------------------------------------------------------ encode
def encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes((n,))
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(b),)) + b


def tlv(tag: int, content: bytes) -> bytes:
    return bytes((tag,)) + encode_length(len(content)) + content


def enc_int(n: int, tag: int = T_INTEGER) -> bytes:
    """Signed integer, minimal two's complement."""
    length = (n + (n < 0)).bit_length() // 8 + 1
    return tlv(tag, n.to_bytes(length, "big", signed=True))


def enc_uint(n: int, tag: int) -> bytes:
    """Unsigned integer (Counter32/Gauge32/TimeTicks/Counter64)."""
    if n < 0:
        raise BerError("negative value for unsigned type")
    return tlv(tag, n.to_bytes(n.bit_length() // 8 + 1, "big"))


def enc_octets(b: bytes) -> bytes:
    return tlv(T_OCTETS, bytes(b))


def oid_tuple(oid) -> tuple[int, ...]:
    """'1.3.6.1...' / '.1.3...' / tuple -> validated tuple of ints."""
    if isinstance(oid, str):
        text = oid.strip().lstrip(".")
        try:
            parts = tuple(int(t) for t in text.split("."))
        except ValueError:
            raise BerError("invalid OID") from None
    else:
        parts = tuple(int(t) for t in oid)
    if len(parts) < 2 or len(parts) > MAX_OID_PARTS or any(t < 0 or t > 0xFFFFFFFF for t in parts) \
            or parts[0] > 2 or (parts[0] < 2 and parts[1] > 39):
        raise BerError("invalid OID")
    return parts


def oid_text(parts) -> str:
    return ".".join(str(t) for t in parts)


def enc_oid(oid) -> bytes:
    t = oid_tuple(oid)
    out = bytearray()
    for z in (t[0] * 40 + t[1],) + t[2:]:
        chunk = [z & 0x7F]
        z >>= 7
        while z:
            chunk.append(0x80 | (z & 0x7F))
            z >>= 7
        out.extend(reversed(chunk))
    return tlv(T_OID, bytes(out))


# ------------------------------------------------------------------ decode
def read_header(data: bytes, pos: int, end: int) -> tuple[int, int, int]:
    """TLV header -> (tag, content_start, content_end). Strict: nothing beyond ``end``."""
    if pos + 2 > end:
        raise BerError("truncated")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise BerError("multi-byte tag not supported")
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or n > 4:
            raise BerError("invalid length form")
        if pos + n > end:
            raise BerError("truncated")
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    if length > end - pos:
        raise BerError("length beyond buffer")
    return tag, pos, pos + length


def read_node(data: bytes, pos: int, end: int, depth: int = 0):
    """One TLV -> ((tag, value), new_pos). Constructed: value = list of nodes, else bytes."""
    tag, start, stop = read_header(data, pos, end)
    if tag & 0x20:
        if depth >= MAX_DEPTH:
            raise BerError("nested too deeply")
        children = []
        p = start
        while p < stop:
            node, p = read_node(data, p, stop, depth + 1)
            children.append(node)
        return (tag, children), stop
    return (tag, bytes(data[start:stop])), stop


def decode(data: bytes):
    """Exactly one TLV filling the whole buffer."""
    node, end = read_node(data, 0, len(data))
    if end != len(data):
        raise BerError("trailing bytes")
    return node


def int_from(b: bytes) -> int:
    if not isinstance(b, bytes) or not b or len(b) > 9:
        raise BerError("invalid INTEGER length")
    return int.from_bytes(b, "big", signed=True)


def uint_from(b: bytes, bits: int) -> int:
    # read unsigned: some agents encode 0xFFFFFFFF without a leading zero byte
    if not isinstance(b, bytes) or not b or len(b) > bits // 8 + 1:
        raise BerError("invalid counter length")
    n = int.from_bytes(b, "big")
    if n >> bits:
        raise BerError("counter too large")
    return n


def oid_from(b: bytes) -> tuple[int, ...]:
    if not isinstance(b, bytes) or not b or len(b) > 1024:
        raise BerError("invalid OID length")
    parts = []
    z = 0
    running = False
    for byte in b:
        if not running and byte == 0x80:
            raise BerError("OID not minimally encoded")
        z = (z << 7) | (byte & 0x7F)
        if z.bit_length() > 35:
            raise BerError("OID part too large")
        if byte & 0x80:
            running = True
        else:
            parts.append(z)
            z = 0
            running = False
    if running:
        raise BerError("OID truncated")
    if len(parts) >= MAX_OID_PARTS:
        raise BerError("OID too long")
    first = parts[0]
    if first < 40:
        head = (0, first)
    elif first < 80:
        head = (1, first - 40)
    else:
        head = (2, first - 80)
    return head + tuple(parts[1:])


def value_of(node):
    """Varbind value node -> Python value (None for NULL / exceptions, hex for opaque/unknown)."""
    tag, w = node
    if tag & 0x20 or not isinstance(w, bytes):
        raise BerError("constructed value in varbind")
    if tag == T_INTEGER:
        return int_from(w)
    if tag in (T_COUNTER32, T_GAUGE32, T_TIMETICKS):
        return uint_from(w, 32)
    if tag == T_COUNTER64:
        return uint_from(w, 64)
    if tag == T_OCTETS:
        return w.decode("utf-8", errors="replace")
    if tag == T_OID:
        return oid_text(oid_from(w))
    if tag == T_IPADDRESS:
        if len(w) != 4:
            raise BerError("invalid IpAddress length")
        return "%d.%d.%d.%d" % tuple(w)
    if tag in (T_NULL, T_NO_SUCH_OBJECT, T_NO_SUCH_INSTANCE, T_END_OF_MIB):
        return None
    return w.hex()
