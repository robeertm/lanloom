"""SNMPv2c GET for sysDescr, sysObjectID and sysName (UDP 161, read-only, default community).

This phase is off unless the user switches it on in the scan plan.
"""
from __future__ import annotations

import random

from ..sources import _ber as ber
from . import _udp
from .scrub import scrub

PORT = 161
COMMUNITY = b"public"
OID_DESCR = "1.3.6.1.2.1.1.1.0"
OID_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
OID_NAME = "1.3.6.1.2.1.1.5.0"
_ENTERPRISES = (1, 3, 6, 1, 4, 1)
PDU_GET = 0xA0
PDU_RESPONSE = 0xA2
MAX_DATAGRAM = 4096


def build_get_request(request_id: int) -> bytes:
    vbs = b"".join(ber.tlv(ber.T_SEQUENCE, ber.enc_oid(o) + ber.tlv(ber.T_NULL, b""))
                   for o in (OID_DESCR, OID_OBJECT_ID, OID_NAME))
    pdu = ber.tlv(PDU_GET, ber.enc_int(request_id) + ber.enc_int(0) + ber.enc_int(0)
                  + ber.tlv(ber.T_SEQUENCE, vbs))
    return ber.tlv(ber.T_SEQUENCE, ber.enc_int(1) + ber.enc_octets(COMMUNITY) + pdu)


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return scrub(_udp.clean_text(value, limit)) or None


def parse_get_response(data: bytes, request_id: int) -> dict | None:
    """Parse a v2c response; None when malformed, not ours (request id) or an error/empty answer."""
    if not data or len(data) > MAX_DATAGRAM:
        return None
    try:
        tag, kids = ber.decode(bytes(data))
        if tag != ber.T_SEQUENCE or len(kids) != 3 or ber.int_from(kids[0][1]) not in (0, 1) \
                or kids[1][0] != ber.T_OCTETS:
            return None
        ptag, pk = kids[2]
        if ptag != PDU_RESPONSE or len(pk) != 4 or pk[3][0] != ber.T_SEQUENCE:
            return None
        if ber.int_from(pk[0][1]) != request_id or ber.int_from(pk[1][1]) != 0:
            return None
        values: dict[str, object] = {}
        for vb in pk[3][1]:
            if vb[0] != ber.T_SEQUENCE or len(vb[1]) != 2 or vb[1][0][0] != ber.T_OID:
                return None
            oid = ber.oid_text(ber.oid_from(vb[1][0][1]))
            values[oid] = ber.value_of(vb[1][1])
    except (ber.BerError, TypeError, ValueError, IndexError):
        return None
    object_id = values.get(OID_OBJECT_ID)
    object_id = object_id if isinstance(object_id, str) and object_id.count(".") >= 1 else None
    enterprise = None
    if object_id:
        try:
            parts = tuple(int(x) for x in object_id.split("."))
        except ValueError:
            parts = ()
            object_id = None
        if parts[:6] == _ENTERPRISES and len(parts) > 6:
            enterprise = parts[6]
    out = {"descr": _text(values.get(OID_DESCR), 255), "object_id": object_id,
           "name": _text(values.get(OID_NAME), 64), "enterprise": enterprise}
    if not (out["descr"] or out["object_id"] or out["name"]):
        return None
    return out


async def query(ips, bucket, timeout: float = 1.0, on_sent=None) -> dict[str, dict]:
    """One GET per host -> {ip: parse result}."""
    def make(_ip):
        rid = random.randrange(1, 0x7FFFFFFF)
        return rid, build_get_request(rid)

    return await _udp.ask(ips, PORT, make, parse_get_response, bucket, timeout, on_sent)
