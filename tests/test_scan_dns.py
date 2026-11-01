import random
import struct

import pytest

from lanloom.scan import dnsmsg
from lanloom.scan.dnsmsg import DNSError, T_A, T_PTR, T_SRV, T_TXT
from lanloom.scan.mdns import MdnsCollector, SERVICES_META


def _sample_response():
    inst = "Kitchen display._http._tcp.local"
    return dnsmsg.build_response(
        [dnsmsg.build_record("_http._tcp.local", T_PTR, dnsmsg.rdata_ptr(inst))],
        [dnsmsg.build_record(inst, T_SRV, dnsmsg.rdata_srv(8080, "display-1.local"), flush=True),
         dnsmsg.build_record(inst, T_TXT, dnsmsg.rdata_txt({"path": "/ui", "ver": "2"})),
         dnsmsg.build_record("display-1.local", T_A, dnsmsg.rdata_a("192.168.1.20"))])


def test_query_roundtrip():
    q = dnsmsg.build_query([(SERVICES_META, T_PTR), ("_ipp._tcp.local", T_PTR)], unicast=True)
    msg = dnsmsg.parse_message(q)
    assert not msg["response"]
    assert [x["name"] for x in msg["questions"]] == [SERVICES_META, "_ipp._tcp.local"]
    assert all(x["unicast"] and x["type"] == T_PTR for x in msg["questions"])


def test_response_parse():
    msg = dnsmsg.parse_message(_sample_response())
    assert msg["response"]
    recs = {(r["type"], r["name"]): r["data"] for r in msg["records"]}
    assert recs[(T_PTR, "_http._tcp.local")] == "Kitchen display._http._tcp.local"
    assert recs[(T_SRV, "Kitchen display._http._tcp.local")]["port"] == 8080
    assert recs[(T_TXT, "Kitchen display._http._tcp.local")] == {"path": "/ui", "ver": "2"}
    assert recs[(T_A, "display-1.local")] == "192.168.1.20"


def test_name_compression():
    # header + question "a.local" at 12, answer name = pointer to 12, PTR rdata = "x" + pointer to 12
    header = struct.pack("!HHHHHH", 0, 0x8400, 1, 1, 0, 0)
    q = dnsmsg.encode_name("a.local") + struct.pack("!HH", T_PTR, 1)
    rdata = b"\x01x\xc0\x0c"
    ans = b"\xc0\x0c" + struct.pack("!HHIH", T_PTR, 1, 120, len(rdata)) + rdata
    msg = dnsmsg.parse_message(header + q + ans)
    assert msg["records"][0]["name"] == "a.local"
    assert msg["records"][0]["data"] == "x.a.local"


def test_pointer_loop_rejected():
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    # name at offset 12 is a pointer to itself
    with pytest.raises(DNSError):
        dnsmsg.parse_message(header + b"\xc0\x0c" + b"\x00" * 10)
    # forward pointer
    with pytest.raises(DNSError):
        dnsmsg.parse_message(header + b"\xc0\x20" + b"\x00" * 40)
    # two names pointing at each other: 12 -> 14 is forward, refused
    with pytest.raises(DNSError):
        dnsmsg.parse_message(header + b"\xc0\x0e\xc0\x0c" + b"\x00" * 10)


@pytest.mark.parametrize("data", [
    b"", b"\x00" * 11, struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0),           # truncated
    struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\x3f" + b"a" * 10,        # label overruns
    struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\x80abc",                  # reserved label type
    struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\x00" + struct.pack("!HHIH", T_A, 1, 1, 5) + b"\x01" * 5,
    struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\x00" + struct.pack("!HHIH", T_SRV, 1, 1, 3) + b"\x01" * 3,
    struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\x00" + struct.pack("!HHIH", T_TXT, 1, 1, 2) + b"\x09a",
    struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\x00" + struct.pack("!HHIH", T_A, 1, 1, 400),
    struct.pack("!HHHHHH", 0, 0x8400, 0xFFFF, 0xFFFF, 0, 0),                      # absurd counts
    struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + (b"\x3f" + b"a" * 63) * 5 + b"\x00",  # name > 255
])
def test_malformed_raises_dnserror(data):
    with pytest.raises(DNSError):
        dnsmsg.parse_message(data)


def test_ptr_rdata_overrun_rejected():
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    rdata = dnsmsg.encode_name("long.name.local")
    rr = b"\x00" + struct.pack("!HHIH", T_PTR, 1, 1, 3) + rdata
    with pytest.raises(DNSError):
        dnsmsg.parse_message(header + rr)


def test_fuzz_never_raises_other_errors():
    rng = random.Random(1234)
    base = _sample_response()
    for _ in range(3000):
        data = bytearray(base)
        for _ in range(rng.randint(1, 8)):
            op = rng.random()
            if op < 0.5 and data:
                data[rng.randrange(len(data))] = rng.randrange(256)
            elif op < 0.75 and data:
                del data[rng.randrange(len(data)):]
            else:
                data.insert(rng.randrange(len(data) + 1), rng.randrange(256))
        try:
            dnsmsg.parse_message(bytes(data))
        except DNSError:
            pass
    for _ in range(2000):
        blob = bytes(rng.randrange(256) for _ in range(rng.randint(0, 200)))
        try:
            dnsmsg.parse_message(blob)
        except DNSError:
            pass


def test_fuzz_collector_never_raises():
    rng = random.Random(99)
    col = MdnsCollector()
    base = _sample_response()
    for _ in range(2000):
        data = bytearray(base)
        for _ in range(rng.randint(1, 6)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        col.feed(bytes(data), ("192.168.1.20", 5353))
    col.next_questions(repeat_base=True)
    col.results(None)


def test_encode_name_limits():
    with pytest.raises(DNSError):
        dnsmsg.encode_name("a" * 64 + ".local")
    with pytest.raises(DNSError):
        dnsmsg.encode_name("a..b")
    with pytest.raises(DNSError):
        dnsmsg.encode_name(".".join(["abcdefgh"] * 40))
    assert dnsmsg.encode_name("") == b"\x00"


def test_txt_parsing_edge_cases():
    assert dnsmsg.parse_txt(b"\x00") == {}
    assert dnsmsg.parse_txt(b"\x04flag") == {"flag": ""}
    assert dnsmsg.parse_txt(b"\x03a=1\x03A=2") == {"a": "1"}          # first wins, keys case-insensitive
    assert dnsmsg.parse_txt(b"\x05k=\x01\x02z") == {"k": "z"}          # control characters dropped
    with pytest.raises(DNSError):
        dnsmsg.parse_txt(b"\x05ab")
