"""NetFlow / IPFIX receiver: parsers, template handling, direction, exporter filter, hostile input, settings helpers."""
import asyncio
import ipaddress
import logging
import random
import socket
import struct

import pytest

from lanloom import netflow
from lanloom.netflow import Collector, UNKNOWN_DEVICE, parse_exporters, parse_port

T0 = 1_790_000_000.0
EXP = "192.0.2.1"
LAN = [ipaddress.ip_network("192.0.2.0/24"), ipaddress.ip_network("2001:db8:1::/48")]
OUT_NETS = [ipaddress.ip_network(n) for n in ("203.0.113.0/24", "198.51.100.0/24", "2001:db8:ffff::/48")]


def is_out(ip):
    return any(ip.version == n.version and ip in n for n in OUT_NETS)


class FakePaths:
    def __init__(self):
        self.calls = []

    def book(self, device, peer, tx, rx, now):
        self.calls.append((device, peer, tx, rx, now))
        return True


def make(devices=None, skip=(), exporters=(EXP,)):
    paths = FakePaths()
    devices = devices or {}
    col = Collector(paths, devices.get, lambda: LAN, skip_ips=lambda: set(skip), clock=lambda: T0, outside=is_out)
    col.configure(list(exporters), 2055)
    return col, paths


def ip4(text):
    return ipaddress.IPv4Address(text).packed


def ip6(text):
    return ipaddress.IPv6Address(text).packed


# ---------------------------------------------------------------------- packet builders
def v5_packet(records):
    out = struct.pack(">HHIIIIBBH", 5, len(records), 0, 0, 0, 0, 0, 0, 0)
    for src, dst, octets in records:
        out += ip4(src) + ip4(dst) + ip4("0.0.0.0") + struct.pack(">HHII", 1, 2, 3, octets) + bytes(24)
    return out


def v9_template(tid, fields):
    body = struct.pack(">HH", tid, len(fields)) + b"".join(struct.pack(">HH", t, ln) for t, ln in fields)
    return struct.pack(">HH", 0, 4 + len(body)) + body


def v9_packet(sets, source=1):
    return struct.pack(">HHIIII", 9, len(sets), 0, 0, 0, source) + b"".join(sets)


def data_set(sid, records, pad=0):
    body = b"".join(records) + bytes(pad)
    return struct.pack(">HH", sid, 4 + len(body)) + body


def ipfix_packet(sets, domain=1):
    body = b"".join(sets)
    return struct.pack(">HHIII", 10, 16 + len(body), 0, 0, domain) + body


def ipfix_template(tid, fields):
    """fields: (ie, length) or (ie, length, enterprise number)."""
    body = struct.pack(">HH", tid, len(fields))
    for f in fields:
        if len(f) == 3:
            body += struct.pack(">HHI", f[0] | 0x8000, f[1], f[2])
        else:
            body += struct.pack(">HH", f[0], f[1])
    return struct.pack(">HH", 2, 4 + len(body)) + body


V4_FIELDS = [(8, 4), (12, 4), (1, 4)]


def v4_record(src, dst, octets):
    return ip4(src) + ip4(dst) + struct.pack(">I", octets)


# ---------------------------------------------------------------------- v5
def test_v5_bookings():
    col, paths = make({"192.0.2.10": "dev-a"})
    pkt = v5_packet([("192.0.2.10", "203.0.113.5", 1000),       # upload of a known device
                     ("198.51.100.7", "192.0.2.10", 2000),      # download
                     ("192.0.2.77", "203.0.113.6", 300)])       # unknown LAN device
    assert col.handle(pkt, EXP) == 3
    assert paths.calls == [("dev-a", "203.0.113.5", 1000, 0, T0), ("dev-a", "198.51.100.7", 0, 2000, T0),
                           (UNKNOWN_DEVICE, "203.0.113.6", 300, 0, T0)]
    st = col.stats()
    assert (st["packets"], st["flows"], st["booked"], st["bytes"]) == (1, 3, 3, 3300)
    assert st["exporters"][EXP]["version"] == 5 and st["exporters"][EXP]["packets"] == 1
    assert st["last_packet"] == T0 and st["last_flow"] == T0


def test_v5_count_mismatch_is_malformed():
    col, paths = make()
    pkt = v5_packet([("192.0.2.10", "203.0.113.5", 10)])
    bad = pkt[:2] + struct.pack(">H", 5) + pkt[4:]
    assert col.handle(bad, EXP) == 0 and col.stats()["malformed"] == 1 and not paths.calls


# ---------------------------------------------------------------------- v9
def test_v9_template_then_data():
    col, paths = make({"192.0.2.10": "dev-a"})
    assert col.handle(v9_packet([v9_template(256, V4_FIELDS)]), EXP) == 0
    pkt = v9_packet([data_set(256, [v4_record("192.0.2.10", "203.0.113.5", 500), v4_record("198.51.100.2", "192.0.2.10", 700)])])
    assert col.handle(pkt, EXP) == 2
    assert [c[:4] for c in paths.calls] == [("dev-a", "203.0.113.5", 500, 0), ("dev-a", "198.51.100.2", 0, 700)]
    assert col.stats()["exporters"][EXP]["version"] == 9


def test_v9_data_before_template_waits():
    col, paths = make()
    data = v9_packet([data_set(256, [v4_record("192.0.2.10", "203.0.113.5", 500)])])
    assert col.handle(data, EXP) == 0
    st = col.stats()
    assert st["waiting"] == 1 and st["malformed"] == 0 and st["booked"] == 0 and not paths.calls
    col.handle(v9_packet([v9_template(256, V4_FIELDS)]), EXP)
    assert col.handle(data, EXP) == 1
    assert paths.calls[0][:4] == (UNKNOWN_DEVICE, "203.0.113.5", 500, 0)


def test_v9_options_template_skipped_and_its_data_ignored():
    col, paths = make()
    opt_body = struct.pack(">HHH", 300, 4, 4) + struct.pack(">HHHH", 1, 4, 34, 4)        # scope + option field
    opt = struct.pack(">HH", 1, 4 + len(opt_body) + 2) + opt_body + bytes(2)
    pkt = v9_packet([opt, v9_template(256, V4_FIELDS), data_set(300, [bytes(8)]),
                     data_set(256, [v4_record("192.0.2.10", "203.0.113.5", 9)])])
    assert col.handle(pkt, EXP) == 1
    st = col.stats()
    assert st["waiting"] == 0 and st["malformed"] == 0


def test_v9_padding_ignored():
    col, paths = make()
    col.handle(v9_packet([v9_template(256, V4_FIELDS)]), EXP)
    pkt = v9_packet([data_set(256, [v4_record("192.0.2.10", "203.0.113.5", 11)], pad=3)])
    assert col.handle(pkt, EXP) == 1 and len(paths.calls) == 1


def test_v9_out_bytes_fallback_and_wide_counters():
    col, paths = make()
    fields = [(8, 4), (12, 4), (23, 8), (2, 2)]
    col.handle(v9_packet([v9_template(256, fields)]), EXP)
    rec = ip4("192.0.2.10") + ip4("203.0.113.5") + struct.pack(">Q", 5_000_000_000) + b"\x00\x01"
    assert col.handle(v9_packet([data_set(256, [rec])]), EXP) == 1
    assert paths.calls[0][2] == 5_000_000_000


def test_v9_in_bytes_wins_and_odd_lengths():
    col, paths = make()
    fields = [(1, 3), (23, 4), (8, 4), (12, 4)]
    col.handle(v9_packet([v9_template(256, fields)]), EXP)
    rec = b"\x00\x01\x00" + struct.pack(">I", 99) + ip4("192.0.2.10") + ip4("203.0.113.5")
    col.handle(v9_packet([data_set(256, [rec])]), EXP)
    assert paths.calls[0][2] == 256


def test_templates_do_not_mix():
    other = "192.0.2.2"
    col, paths = make(exporters=(EXP, other))
    col.handle(v9_packet([v9_template(256, V4_FIELDS)], source=1), EXP)
    data = lambda src: v9_packet([data_set(256, [v4_record("192.0.2.10", "203.0.113.5", 5)])], source=src)
    assert col.handle(data(2), EXP) == 0           # other source id
    assert col.handle(data(1), other) == 0         # other exporter
    assert col.stats()["waiting"] == 2
    assert col.handle(data(1), EXP) == 1
    # same ids, different layout per exporter
    col.handle(v9_packet([v9_template(256, [(12, 4), (8, 4), (1, 4)])], source=1), other)
    assert col.handle(data(1), other) == 1
    assert paths.calls[-1][:4] == (UNKNOWN_DEVICE, "203.0.113.5", 0, 5)      # fields swapped: a download


def test_versions_do_not_share_templates():
    col, _ = make()
    col.handle(v9_packet([v9_template(256, V4_FIELDS)]), EXP)
    col.handle(ipfix_packet([data_set(256, [v4_record("192.0.2.10", "203.0.113.5", 5)])]), EXP)
    assert col.stats()["waiting"] == 1


# ---------------------------------------------------------------------- IPFIX
def test_ipfix_ipv6_flow():
    col, paths = make({"2001:db8:1::5": "dev-v6"})
    col.handle(ipfix_packet([ipfix_template(400, [(27, 16), (28, 16), (1, 8)])]), EXP)
    rec = ip6("2001:db8:1::5") + ip6("2001:db8:ffff::9") + struct.pack(">Q", 4242)
    assert col.handle(ipfix_packet([data_set(400, [rec])]), EXP) == 1
    assert paths.calls[0][:4] == ("dev-v6", "2001:db8:ffff::9", 4242, 0)
    assert col.stats()["exporters"][EXP]["version"] == 10


def test_ipfix_ipv4_mapped_is_ipv4():
    col, paths = make({"192.0.2.10": "dev-a"})
    col.handle(ipfix_packet([ipfix_template(400, [(27, 16), (28, 16), (1, 4)])]), EXP)
    rec = ip6("::ffff:198.51.100.3") + ip6("::ffff:192.0.2.10") + struct.pack(">I", 8)
    assert col.handle(ipfix_packet([data_set(400, [rec])]), EXP) == 1
    assert paths.calls[0][:4] == ("dev-a", "198.51.100.3", 0, 8)


def test_ipfix_variable_and_enterprise_fields_skipped():
    col, paths = make()
    fields = [(8, 4), (99, 0xFFFF), (12, 4), (7, 6, 12345), (1, 4), (98, 0xFFFF)]
    col.handle(ipfix_packet([ipfix_template(500, fields)]), EXP)
    short = b"\x03abc"
    long = b"\xff" + struct.pack(">H", 300) + bytes(300)
    r1 = ip4("192.0.2.10") + short + ip4("203.0.113.5") + bytes(6) + struct.pack(">I", 10) + b"\x00"
    r2 = ip4("198.51.100.4") + long + ip4("192.0.2.10") + bytes(6) + struct.pack(">I", 20) + b"\x02xy"
    assert col.handle(ipfix_packet([data_set(500, [r1, r2])]), EXP) == 2
    assert [c[2:4] for c in paths.calls] == [(10, 0), (0, 20)]
    assert col.stats()["malformed"] == 0


def test_ipfix_octet_delta_preferred_over_post_octet_and_total_ignored():
    col, paths = make()
    col.handle(ipfix_packet([ipfix_template(500, [(8, 4), (12, 4), (85, 8), (23, 4), (1, 4)])]), EXP)
    rec = ip4("192.0.2.10") + ip4("203.0.113.5") + struct.pack(">Q", 777) + struct.pack(">II", 5, 6)
    col.handle(ipfix_packet([data_set(500, [rec])]), EXP)
    assert paths.calls[0][2] == 6


def test_ipfix_template_withdrawal():
    col, paths = make()
    col.handle(ipfix_packet([ipfix_template(400, V4_FIELDS)]), EXP)
    data = ipfix_packet([data_set(400, [v4_record("192.0.2.10", "203.0.113.5", 5)])])
    assert col.handle(data, EXP) == 1
    withdraw = struct.pack(">HHHH", 2, 8, 400, 0)
    col.handle(ipfix_packet([withdraw]), EXP)
    assert col.handle(data, EXP) == 0 and col.stats()["waiting"] == 1


def test_ipfix_options_template_skipped():
    col, _ = make()
    body = struct.pack(">HHH", 600, 2, 1) + struct.pack(">HH", 144, 4) + struct.pack(">HH", 41, 8)
    opt = struct.pack(">HH", 3, 4 + len(body)) + body
    col.handle(ipfix_packet([opt, data_set(600, [bytes(12)])]), EXP)
    st = col.stats()
    assert st["waiting"] == 0 and st["malformed"] == 0


# ---------------------------------------------------------------------- exporter filter
def test_foreign_sender_dropped_and_log_throttled(caplog):
    col, paths = make()
    pkt = v5_packet([("192.0.2.10", "203.0.113.5", 10)])
    with caplog.at_level(logging.INFO, logger="lanloom.netflow"):
        for _ in range(50):
            assert col.handle(pkt, "198.51.100.99") == 0
    assert not paths.calls
    st = col.stats()
    assert st["dropped_foreign"] == 50 and st["packets"] == 0 and "198.51.100.99" not in st["exporters"]
    assert len([r for r in caplog.records if r.name == "lanloom.netflow"]) == 1


def test_drop_summary_after_ten_minutes(caplog):
    now = [T0]
    col = Collector(FakePaths(), lambda ip: None, lambda: LAN, clock=lambda: now[0], outside=is_out)
    pkt = v5_packet([("192.0.2.10", "203.0.113.5", 10)])
    with caplog.at_level(logging.INFO, logger="lanloom.netflow"):
        for _ in range(5):
            col.handle(pkt, "198.51.100.99")
        now[0] += 601
        col.handle(pkt, "198.51.100.99")
        col.handle(pkt, "198.51.100.99")
    assert len(caplog.records) == 2


def test_empty_allow_list_drops_everything():
    col, paths = make(exporters=())
    assert col.handle(v5_packet([("192.0.2.10", "203.0.113.5", 10)]), EXP) == 0
    col2 = Collector(paths, lambda ip: None, lambda: LAN, clock=lambda: T0, outside=is_out)      # never configured
    assert col2.handle(v5_packet([("192.0.2.10", "203.0.113.5", 10)]), EXP) == 0
    assert col.stats()["dropped_foreign"] == 1 and col2.stats()["dropped_foreign"] == 1 and not paths.calls


def test_sender_forms_are_normalised():
    col, paths = make()
    assert col.handle(v5_packet([("192.0.2.10", "203.0.113.5", 10)]), "::ffff:192.0.2.1") == 1
    assert col.handle(b"x", "not an address") == 0 and col.stats()["dropped_foreign"] == 1


# ---------------------------------------------------------------------- direction
def test_skip_self_and_local():
    col, paths = make(skip=["192.0.2.50"])
    pkt = v5_packet([("192.0.2.50", "203.0.113.5", 10),          # the Lanloom host itself
                     ("203.0.113.5", "192.0.2.50", 10),
                     ("192.0.2.10", "192.0.2.20", 10),            # LAN <-> LAN
                     ("203.0.113.5", "198.51.100.5", 10),         # transit
                     ("192.0.2.10", "203.0.113.5", 0),            # empty
                     ("192.0.2.10", "203.0.113.5", 10)])
    assert col.handle(pkt, EXP) == 1
    st = col.stats()
    assert (st["skipped_self"], st["skipped_local"], st["booked"], st["flows"]) == (2, 3, 1, 6)
    assert len(paths.calls) == 1


def test_private_address_is_lan_without_configured_network():
    paths = FakePaths()
    col = Collector(paths, lambda ip: None, lambda: [], clock=lambda: T0, outside=is_out)
    col.configure([EXP], 2055)
    assert col.handle(v5_packet([("192.0.2.10", "203.0.113.5", 10)]), EXP) == 1
    assert paths.calls[0][0] == UNKNOWN_DEVICE


def test_default_outside_rule():
    paths = FakePaths()
    col = Collector(paths, lambda ip: None, lambda: [], clock=lambda: T0)
    col.configure([EXP], 2055)
    overlay = str(netflow._CGNAT[1])                            # first host of the overlay VPN range (no literal address)
    pkt = v5_packet([("10.1.2.3", "8.8.8.8", 10),               # global address
                     ("10.1.2.3", overlay, 10),                 # overlay VPN range counts as outside
                     ("10.1.2.3", "224.0.0.251", 10),           # multicast
                     ("10.1.2.3", "169.254.1.1", 10),
                     ("10.1.2.3", "127.0.0.1", 10)])
    assert col.handle(pkt, EXP) == 2
    assert {c[1] for c in paths.calls} == {"8.8.8.8", overlay}


def test_book_failure_and_callback_errors_do_not_raise():
    class Boom(FakePaths):
        def book(self, *a):
            raise RuntimeError("x")

    def bad_device(ip):
        raise RuntimeError("x")

    col = Collector(Boom(), bad_device, lambda: 1 / 0, clock=lambda: T0, outside=is_out)
    col.configure([EXP], 2055)
    assert col.handle(v5_packet([("192.0.2.10", "203.0.113.5", 10)]), EXP) == 0
    assert col.stats()["flows"] == 1 and col.stats()["booked"] == 0


# ---------------------------------------------------------------------- hostile input
def sample_packets():
    v9 = [v9_packet([v9_template(256, V4_FIELDS), data_set(256, [v4_record("192.0.2.10", "203.0.113.5", 5)], pad=2)])]
    ipf = [ipfix_packet([ipfix_template(400, [(8, 4), (99, 0xFFFF), (12, 4), (7, 6, 1), (1, 4)]),
                         data_set(400, [ip4("192.0.2.10") + b"\x02ab" + ip4("203.0.113.5") + bytes(6) + bytes(4)])])]
    return [v5_packet([("192.0.2.10", "203.0.113.5", 10), ("198.51.100.1", "192.0.2.10", 20)])] + v9 + ipf


def test_truncated_packets_never_raise():
    for pkt in sample_packets():
        col, _ = make()
        for n in range(len(pkt) + 1):
            col.handle(pkt[:n], EXP)
        st = col.stats()
        assert st["packets"] == len(pkt) + 1
        assert st["booked"] <= st["flows"] and st["bytes"] >= 0 and st["malformed"] >= 1


def test_random_bytes_never_raise():
    rng = random.Random(1234)
    col, _ = make()
    for i in range(3000):
        data = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 200)))
        if i % 3 == 0 and len(data) >= 2:
            data = bytes([0, rng.choice((5, 9, 10))]) + data[2:]
        col.handle(data, EXP)
    st = col.stats()
    assert st["packets"] == 3000 and st["booked"] <= st["flows"]
    assert st["malformed"] + st["waiting"] + st["refused"] >= 1


def test_bit_flips_never_raise():
    rng = random.Random(99)
    for pkt in sample_packets():
        col, _ = make()
        for _ in range(500):
            b = bytearray(pkt)
            for _ in range(rng.randint(1, 4)):
                b[rng.randrange(len(b))] = rng.getrandbits(8)
            col.handle(bytes(b), EXP)
        assert col.stats()["packets"] == 500


def test_huge_counter_is_clamped():
    col, paths = make()
    col.handle(v9_packet([v9_template(256, [(8, 4), (12, 4), (1, 8)])]), EXP)
    rec = ip4("192.0.2.10") + ip4("203.0.113.5") + b"\xff" * 8
    col.handle(v9_packet([data_set(256, [rec])]), EXP)
    assert paths.calls[0][2] == netflow.MAX_FLOW_BYTES


# ---------------------------------------------------------------------- limits
def test_more_than_128_fields_refused():
    col, _ = make()
    col.handle(v9_packet([v9_template(256, [(8, 4)] * 129)]), EXP)
    col.handle(v9_packet([data_set(256, [bytes(4 * 129)])]), EXP)
    st = col.stats()
    assert st["refused"] == 1 and st["waiting"] == 1
    col.handle(v9_packet([v9_template(257, [(8, 1)] * 128)]), EXP)
    assert col.stats()["refused"] == 1


def test_more_than_512_templates_refused():
    col, _ = make()
    for start in range(256, 256 + 520, 20):
        sets = [v9_template(t, V4_FIELDS) for t in range(start, min(start + 20, 256 + 520))]
        col.handle(v9_packet(sets), EXP)
    st = col.stats()
    assert st["refused"] == 520 - 512
    # an existing template can still be replaced when the table is full
    col.handle(v9_packet([v9_template(256, [(12, 4), (8, 4), (1, 4)])]), EXP)
    assert col.stats()["refused"] == 8


def test_zero_length_record_refused():
    col, _ = make()
    col.handle(v9_packet([v9_template(256, [(8, 0)])]), EXP)
    assert col.stats()["refused"] == 1


def test_configure_forgets_removed_exporters():
    col, _ = make()
    col.handle(v9_packet([v9_template(256, V4_FIELDS)]), EXP)
    col.configure(["192.0.2.2"], 2055)
    col.configure([EXP], 2055)
    col.handle(v9_packet([data_set(256, [v4_record("192.0.2.10", "203.0.113.5", 5)])]), EXP)
    assert col.stats()["waiting"] == 1


# ---------------------------------------------------------------------- settings helpers
def test_parse_exporters():
    assert parse_exporters(["192.0.2.1", "2001:DB8::1", " 192.0.2.1 "]) == ["192.0.2.1", "2001:db8::1"]
    assert parse_exporters(["::ffff:192.0.2.1"]) == ["192.0.2.1"]
    for bad in ([], None, "192.0.2.1", ["192.0.2.0/24"], ["0.0.0.0"], ["::"], ["224.0.0.1"], ["ff02::1"], ["nonsense"],
                [""], [5], ["fe80::1%eth0"], ["192.0.2.%d" % i for i in range(1, 10)]):
        assert parse_exporters(bad) is None, bad
    assert len(parse_exporters(["192.0.2.%d" % i for i in range(1, 9)])) == 8


def test_parse_port():
    assert netflow.DEFAULT_PORT == 2055 and parse_port(2055) == 2055
    assert parse_port("9995") == 9995 and parse_port(65535) == 65535 and parse_port(1024) == 1024
    for bad in (0, 80, 1023, 65536, -1, "x", None, True, 2055.0, "99999999"):
        assert parse_port(bad) is None, bad


# ---------------------------------------------------------------------- socket
def test_udp_endpoint_books_and_stops():
    async def run():
        col, paths = make(exporters=("127.0.0.1",))
        await col.start("127.0.0.1", 0)
        st = col.stats()
        assert st["listening"] and st["error"] is None and st["port"] > 0
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(v5_packet([("192.0.2.10", "203.0.113.5", 321)]), ("127.0.0.1", st["port"]))
        finally:
            sock.close()
        for _ in range(200):
            if paths.calls:
                break
            await asyncio.sleep(0.01)
        col.stop()
        return col, paths

    col, paths = asyncio.run(run())
    assert paths.calls and paths.calls[0][2] == 321
    assert not col.stats()["listening"]


def test_udp_bind_error_is_reported_not_raised():
    async def run():
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", 0))
        try:
            col, _ = make()
            await col.start("127.0.0.1", blocker.getsockname()[1])
            return col.stats()
        finally:
            blocker.close()

    st = asyncio.run(run())
    assert st["listening"] is False and st["error"] == "settings.netflow.bind_failed"
