"""Recorded NetFlow v5 / v9 / IPFIX sample packets for the receiver tests (W58, W61, W68).

The packets are made up here, byte by byte, from the RFCs (3954 for v9, 7011 for IPFIX); they contain no captured traffic.
Addresses: local devices in 192.168.50.0/24 and 2001:db8:50::/48, internet peers are public resolver addresses, exporters
192.0.2.1-3 (a v9 router, an IPFIX firewall, an old v5 box) and one sender that is not allowed (192.0.2.99).

    python3 -I tests/netflow_samples.py --write     # rewrite tests/fixtures/netflow/ (the test checks the files match)

The recording (``recording.json``) lists the packets in the order and at the second they were "received", the sender, and
what the receiver must show afterwards per exporter. ``tests/test_netflow_samples.py`` replays it into the collector and
through a real UDP socket into the core.
"""
from __future__ import annotations

import ipaddress
import json
import struct
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent / "fixtures" / "netflow"

ROUTER, FIREWALL, OLDBOX, STRANGER = "192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.99"
LAN = "192.168.50.0/24"
TV, LAPTOP, PHONE, PRINTER, SELF = "192.168.50.10", "192.168.50.11", "192.168.50.12", "192.168.50.13", "192.168.50.2"
LAPTOP6 = "2001:db8:50::11"
DEVICES = {TV: "dev-tv", LAPTOP: "dev-laptop", PHONE: "dev-phone", PRINTER: "dev-printer", LAPTOP6: "dev-laptop"}
PEER_A, PEER_B, PEER_6 = "8.8.8.8", "9.9.9.9", "2620:fe::fe"


def _ip(text: str) -> bytes:
    return ipaddress.ip_address(text).packed


# ---------------------------------------------------------------------- NetFlow v9
def v9(sets: list, uptime_ms: int, seq: int, domain: int = 1, secs: int = 1_790_000_000) -> bytes:
    return struct.pack(">HHIIII", 9, len(sets), uptime_ms, secs, seq, domain) + b"".join(sets)


def v9_template_set(templates: list) -> bytes:
    body = b""
    for tid, fields in templates:
        body += struct.pack(">HH", tid, len(fields)) + b"".join(struct.pack(">HH", ie, ln) for ie, ln in fields)
    return struct.pack(">HH", 0, 4 + len(body)) + body


def v9_options_template_set(tid: int, scope: list, options: list) -> bytes:
    sc = b"".join(struct.pack(">HH", t, ln) for t, ln in scope)
    op = b"".join(struct.pack(">HH", t, ln) for t, ln in options)
    body = struct.pack(">HHH", tid, len(sc), len(op)) + sc + op
    body += bytes(-len(body) % 4)                                   # padding to 32 bits
    return struct.pack(">HH", 1, 4 + len(body)) + body


def data_set(sid: int, records: list, pad: int = 0) -> bytes:
    body = b"".join(records) + bytes(pad)
    return struct.pack(">HH", sid, 4 + len(body)) + body


# v9 router: IPv4 template with ports, protocol and interfaces (read past, not kept), IPv6 template with 8-byte counters
V9_T4 = [(8, 4), (12, 4), (7, 2), (11, 2), (4, 1), (10, 2), (14, 2), (2, 4), (1, 4)]
V9_T6 = [(27, 16), (28, 16), (7, 2), (11, 2), (4, 1), (1, 8)]


def v9_rec4(src, dst, octets, sport=50000, dport=443, proto=6):
    return _ip(src) + _ip(dst) + struct.pack(">HHBHHII", sport, dport, proto, 3, 1, max(1, octets // 1200), octets)


def v9_rec6(src, dst, octets, sport=50001, dport=443, proto=6):
    return _ip(src) + _ip(dst) + struct.pack(">HHBQ", sport, dport, proto, octets)


# ---------------------------------------------------------------------- IPFIX
def ipfix(sets: list, seq: int, domain: int = 7, secs: int = 1_790_000_000) -> bytes:
    body = b"".join(sets)
    return struct.pack(">HHIII", 10, 16 + len(body), secs, seq, domain) + body


def ipfix_template_set(templates: list) -> bytes:
    """templates: [(tid, [(ie, length) | (ie, length, enterprise)])]; length 0xFFFF = variable."""
    body = b""
    for tid, fields in templates:
        body += struct.pack(">HH", tid, len(fields))
        for f in fields:
            body += struct.pack(">HHI", f[0] | 0x8000, f[1], f[2]) if len(f) == 3 else struct.pack(">HH", f[0], f[1])
    return struct.pack(">HH", 2, 4 + len(body)) + body


def ipfix_withdrawal(tid: int) -> bytes:
    return struct.pack(">HHHH", 2, 8, tid, 0)


def ipfix_options_template_set(tid: int, fields: list, scope_count: int) -> bytes:
    body = struct.pack(">HHH", tid, len(fields), scope_count) + b"".join(struct.pack(">HH", t, ln) for t, ln in fields)
    body += bytes(-len(body) % 4)
    return struct.pack(">HH", 3, 4 + len(body)) + body


def varlen(raw: bytes) -> bytes:
    return (bytes([len(raw)]) + raw) if len(raw) < 255 else (b"\xff" + struct.pack(">H", len(raw)) + raw)


# IPFIX firewall: interface name (variable length), a vendor field (enterprise number 32473 = documentation), addresses,
# 8-byte octetDeltaCount, and a second template that is withdrawn later
FW_T = [(82, 0xFFFF), (1000, 2, 32473), (8, 4), (12, 4), (7, 2), (11, 2), (1, 8)]
FW_T2 = [(8, 4), (12, 4), (1, 4)]


def fw_rec(name: bytes, src, dst, octets, sport=40000, dport=443):
    return varlen(name) + b"\x00\x01" + _ip(src) + _ip(dst) + struct.pack(">HHQ", sport, dport, octets)


# ---------------------------------------------------------------------- NetFlow v5
def v5(records: list, seq: int) -> bytes:
    out = struct.pack(">HHIIIIBBH", 5, len(records), 900_000, 1_790_000_000, 0, seq, 0, 0, 0)
    for src, dst, octets in records:
        out += _ip(src) + _ip(dst) + _ip("0.0.0.0") + struct.pack(">HHII", 1, 2, max(1, octets // 1000), octets)
        out += struct.pack(">IIHHBBBBHHBBH", 0, 0, 50002, 443, 0, 0, 6, 0, 0, 0, 24, 24, 0)
    return out


# ---------------------------------------------------------------------- the recording
def packets() -> list:
    """[(file name, second, sender, bytes)] in the order they arrive."""
    up = 86_400_000                                                 # the router has been up for a day
    rec = []
    # router (v9): data before its template, then templates, flows, options with a sampling rate, flows, restart
    rec.append(("01-v9-data-before-template.bin", 0.0, ROUTER, v9([data_set(256, [v9_rec4(TV, PEER_A, 9_000)])], up, 1)))
    rec.append(("02-v9-templates-and-data.bin", 1.0, ROUTER, v9([
        v9_template_set([(256, V9_T4), (257, V9_T6)]),
        data_set(256, [v9_rec4(TV, PEER_A, 120_000), v9_rec4(PEER_B, PHONE, 30_000, 53, 51000, 17),
                       v9_rec4(LAPTOP, PRINTER, 4_000, 50010, 631), v9_rec4(SELF, PEER_A, 777)], pad=3),
        data_set(257, [v9_rec6(LAPTOP6, PEER_6, 2_500_000), v9_rec6(PEER_6, LAPTOP6, 6_000_000, 443, 50001)]),
    ], up + 1_000, 2)))
    rec.append(("03-v9-options-sampling.bin", 2.0, ROUTER, v9([
        v9_options_template_set(300, [(1, 4)], [(34, 4), (35, 1)]),           # scope "system"; sampling interval 10
        data_set(300, [struct.pack(">IIB", 1, 10, 2)], pad=3),
    ], up + 2_000, 3)))
    rec.append(("04-v9-sampled-data.bin", 3.0, ROUTER, v9([data_set(256, [v9_rec4(PHONE, PEER_B, 5_000)])], up + 3_000, 4)))
    rec.append(("05-v9-restart-old-template.bin", 4.0, ROUTER, v9([data_set(256, [v9_rec4(TV, PEER_A, 8_000)])], 1_500, 1)))
    rec.append(("06-v9-restart-new-template.bin", 5.0, ROUTER, v9([
        v9_template_set([(256, V9_T4)]), data_set(256, [v9_rec4(TV, PEER_A, 11_000)])], 2_500, 2)))
    rec.append(("07-v9-truncated.bin", 6.0, ROUTER, v9([v9_template_set([(258, V9_T4)])], 3_500, 3)[:-6]))
    # firewall (IPFIX): templates with a variable-length and a vendor field, options (skipped), a withdrawn template
    rec.append(("11-ipfix-templates-and-data.bin", 0.5, FIREWALL, ipfix([
        ipfix_template_set([(256, FW_T), (257, FW_T2)]),
        data_set(256, [fw_rec(b"lan0", LAPTOP, PEER_B, 64_000), fw_rec(b"x" * 300, PEER_A, TV, 900_000, 443, 40001)], pad=2),
        data_set(257, [_ip(PRINTER) + _ip(PEER_A) + struct.pack(">I", 1_500)]),
    ], 1)))
    rec.append(("12-ipfix-options.bin", 1.5, FIREWALL, ipfix([
        ipfix_options_template_set(400, [(144, 4), (41, 8), (42, 8)], 1),   # exporting process: message and flow totals
        data_set(400, [struct.pack(">IQQ", 1, 120, 3)]),
    ], 3)))
    rec.append(("13-ipfix-withdrawal.bin", 2.5, FIREWALL, ipfix([
        ipfix_withdrawal(257), data_set(257, [_ip(PRINTER) + _ip(PEER_A) + struct.pack(">I", 2_000)]),
    ], 4)))
    # old box (v5) and a sender that is not allowed
    rec.append(("21-v5.bin", 1.2, OLDBOX, v5([(PRINTER, PEER_B, 2_000), (PHONE, TV, 300)], 1)))
    rec.append(("31-v9-not-allowed.bin", 2.2, STRANGER, v9([v9_template_set([(256, V9_T4)])], 5_000, 1)))
    return sorted(rec, key=lambda r: r[1])


# What the receiver shows after the replay (worked out by hand from the packets above, not computed from them).
EXPECT = {
    "exporters": {
        ROUTER: {"version": 9, "packets": 7,
                 # 02: TV->A 120000 up, B->PHONE 30000 down, LAPTOP6->6 up, 6->LAPTOP6 down (SELF skipped, LAN pair not booked)
                 # 04: 5000 x 10 (sampling from 03); 06: 11000 after the restart, no sampling any more
                 "booked": 6, "bytes": 120_000 + 30_000 + 2_500_000 + 6_000_000 + 50_000 + 11_000,
                 "flows": 4 + 2 + 1 + 1, "waiting": 2, "restarts": 1, "templates": 1, "malformed": 1},
        FIREWALL: {"version": 10, "packets": 3, "booked": 3, "bytes": 64_000 + 900_000 + 1_500, "flows": 3,
                   "waiting": 1, "restarts": 0, "templates": 2, "malformed": 0},
        OLDBOX: {"version": 5, "packets": 1, "booked": 1, "bytes": 2_000, "flows": 2, "waiting": 0, "restarts": 0,
                 "templates": 0, "malformed": 0},
    },
    "dropped_foreign": 1,
    "refused_senders": [STRANGER],
    "skipped_self": 1,
    "devices": {"dev-tv": {"tx": 120_000 + 11_000, "rx": 900_000}, "dev-phone": {"tx": 50_000, "rx": 30_000},
                "dev-laptop": {"tx": 2_500_000 + 64_000, "rx": 6_000_000}, "dev-printer": {"tx": 1_500 + 2_000, "rx": 0}},
}


def recording() -> dict:
    return {
        "about": "Made-up NetFlow v5/v9/IPFIX packets for the receiver tests; see tests/netflow_samples.py.",
        "lan": LAN, "devices": DEVICES, "self": SELF,
        "packets": [{"file": f, "second": t, "sender": s, "size": len(b)} for f, t, s, b in packets()],
        "expect": EXPECT,
    }


def write(directory: Path = DIR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for f, _t, _s, b in packets():
        (directory / f).write_bytes(b)
    (directory / "recording.json").write_text(json.dumps(recording(), indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        sys.exit(__doc__)
    write()
    print("wrote", DIR)
