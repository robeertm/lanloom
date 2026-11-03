"""SNMP reader and source - no network beyond 127.0.0.1.

* AES-128 (FIPS-197 C.1), AES-128-CFB128 (NIST SP 800-38A F.3.13), key localisation (RFC 3414 A.3.1 / A.3.2);
* BER round trips and clean exceptions for broken input;
* a fake agent on 127.0.0.1 (v2c and v3 with every supported auth protocol and security level): discovery,
  time resync, encrypted Get/GetBulk, walk end, dropped foreign/broken replies, reports as clear errors;
* the source: system group, interfaces, octet deltas incl. 32-bit wrap and HC preference;
* secrets never in exceptions or repr; only read PDUs can be built.
"""
import ast
import asyncio
import hashlib
import os
import socket
import threading
import time
import traceback

import pytest

from lanloom.sources import _aes as aes
from lanloom.sources import _ber as ber
from lanloom.sources import snmp as m

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lanloom", "sources", "snmp.py")

USER = "map-reader"
AUTH_PW = "auth-test-phrase-7319"
PRIV_PW = "priv-test-phrase-4826"
COMMUNITY = "community-test-5531"
ENGINE = bytes.fromhex("80001f8880aabbccddeeff00112233")


# ================================================================== test vectors
def test_aes128_fips197_c1():
    key = bytes(range(16))
    clear = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert aes.encrypt_block(key, clear).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"


def test_aes128_cfb128_nist_f3_13():
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    clear = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a" "ae2d8a571e03ac9c9eb76fac45af8e51"
                          "30c81c46a35ce411e5fbc1191a0a52ef" "f69f2445df4f9b17ad2b417be66c3710")
    secret = bytes.fromhex("3b3fd92eb72dad20333449f8e83cfb4a" "c8a64537a0b3a93fcde3cdad9f1ce58b"
                           "26751f67a3cbb140b1808cf187a4f4df" "c04b05357c5d1c0eeac4c66f9ff7f2e6")
    assert aes.cfb(key, iv, clear, False) == secret
    assert aes.cfb(key, iv, secret, True) == clear
    assert aes.cfb(key, iv, clear[:37], False) == secret[:37]       # no padding: partial last block
    assert aes.cfb(key, iv, secret[:37], True) == clear[:37]


def test_key_localization_rfc3414_a31_md5():
    ku = m.password_to_key("maplesyrup", "md5")
    assert ku.hex() == "9faf3283884e92834ebc9847d8edd963"
    kul = m.localize_key(ku, bytes.fromhex("000000000000000000000002"), "md5")
    assert kul.hex() == "526f5eed9fcce26f8964c2930787d82b"


def test_key_localization_rfc3414_a32_sha():
    ku = m.password_to_key("maplesyrup", "sha")
    assert ku.hex() == "9fb5cc0381497b3793528939ff788d5d79145211"
    kul = m.localize_key(ku, bytes.fromhex("000000000000000000000002"), "sha")
    assert kul.hex() == "6695febc9288e36282235fc7151f128497b38f3f"


def test_key_localization_sha256_matches_rfc7860_construction():
    engine = bytes.fromhex("000000000000000000000002")
    data = (b"maplesyrup" * (1048576 // 10 + 1))[:1048576]
    ku = hashlib.sha256(data).digest()
    assert m.password_to_key("maplesyrup", "sha256") == ku
    assert m.localize_key(ku, engine, "sha256") == hashlib.sha256(ku + engine + ku).digest()
    assert len(m.mac(b"k" * 32, b"msg", "sha256")) == 24


def test_aes_tables_complete():
    assert len(aes.SBOX) == 256 and len(set(aes.SBOX)) == 256


# ================================================================== BER
@pytest.mark.parametrize("n", [0, 1, -1, 127, 128, -128, -129, 255, 256, 2 ** 31 - 1, -2 ** 31, 2 ** 63 - 1, -2 ** 63])
def test_ber_integer_roundtrip(n):
    tag, w = ber.decode(ber.enc_int(n))
    assert tag == 0x02 and ber.value_of((tag, w)) == n


@pytest.mark.parametrize("n", [0, 1, 127, 128, 255, 2 ** 32 - 1, 2 ** 32, 2 ** 63, 2 ** 64 - 1])
def test_ber_counter64_roundtrip(n):
    assert ber.value_of(ber.decode(ber.enc_uint(n, 0x46))) == n


def test_ber_counter32_without_leading_zero_and_too_large():
    assert ber.value_of((0x41, b"\xff\xff\xff\xff")) == 2 ** 32 - 1
    with pytest.raises(ber.BerError):
        ber.value_of((0x41, b"\x01\x00\x00\x00\x00"))
    with pytest.raises(ber.BerError):
        ber.value_of((0x46, b"\x01" + bytes(8)))


@pytest.mark.parametrize("oid", ["1.3.6.1.2.1.1.1.0", "0.0", "2.999.4294967295.1",
                                 "1.3.6.1.4.1.4294967295.268435456.128.16383", ".1.3.6.1.6.3.15.1.1.4.0"])
def test_ber_oid_roundtrip(oid):
    tag, w = ber.decode(ber.enc_oid(oid))
    assert tag == 0x06 and ber.value_of((tag, w)) == oid.lstrip(".")


@pytest.mark.parametrize("length", [0, 1, 127, 128, 255, 256, 65535, 65536, 70000])
def test_ber_lengths(length):
    content = bytes((i * 7) & 0xFF for i in range(length))
    enc = ber.enc_octets(content)
    assert ber.decode(enc) == (0x04, content)


def test_ber_nested_values():
    seq = ber.tlv(0x30, ber.enc_int(-5) + ber.tlv(0x40, bytes((192, 0, 2, 1))) + ber.tlv(0x05, b"")
                  + ber.tlv(0x81, b"") + ber.enc_uint(123456, 0x43) + ber.enc_octets("port-1".encode()))
    tag, children = ber.decode(seq)
    assert [ber.value_of(k) for k in children] == [-5, "192.0.2.1", None, None, 123456, "port-1"]


@pytest.mark.parametrize("raw", [
    b"", b"\x02", b"\x02\x05\x01", b"\x04\x81", b"\x04\x82\x01", b"\x04\x85\x00\x00\x00\x00\x01",
    b"\x04\x80\x00\x00", b"\x04\x84\xff\xff\xff\xff", b"\x30\x05\x02\x01\x01\x02\x05", b"\x1f\x01\x00",
    b"\x02\x01\x01\x00", b"\x30\x00\x00",
])
def test_ber_broken(raw):
    with pytest.raises(ber.BerError):
        ber.decode(raw)


def test_ber_depth_limited():
    deep = b""
    for _ in range(50):
        deep = ber.tlv(0x30, deep)
    with pytest.raises(ber.BerError):
        ber.decode(deep)


@pytest.mark.parametrize("raw", [b"", b"\x2b\x86", b"\x80\x01", b"\x2b\x8f\xff\xff\xff\xff\xff\x7f", b"\x2b" + b"\x01" * 200])
def test_ber_oid_broken(raw):
    with pytest.raises(ber.BerError):
        ber.value_of((0x06, raw))


def test_decode_v3_random_garbage_only_ber_errors():
    pdu = m.build_pdu(m.PDU_GET, 7, ["1.3.6.1.2.1.1.1.0"])
    real = m.encode_v3(1, 7, ENGINE, 1, 2, b"u", bytes(12), bytes(8), ber.enc_octets(pdu))
    assert m.decode_v3(real)["engine_id"] == ENGINE
    for i in range(len(real)):
        with pytest.raises(ber.BerError):
            m.decode_v3(real[:i])
    rnd = __import__("random").Random(5)
    for _ in range(2000):
        raw = bytes(rnd.randrange(256) for _ in range(rnd.randrange(120)))
        for fn in (m.decode_v3, m.decode_v2c):
            try:
                fn(raw)
            except ber.BerError:
                pass


def test_v2c_codec_roundtrip():
    pdu = m.build_pdu(m.PDU_GETBULK, 99, ["1.3.6.1.2.1.2.2.1.2"], 0, 10)
    version, community, node = m.decode_v2c(m.encode_v2c(b"c1", pdu))
    assert version == 1 and community == b"c1"
    ptag, rid, a, b, vbs = m.parse_pdu(node)
    assert (ptag, rid, a, b) == (0xA5, 99, 0, 10) and vbs[0][0] == (1, 3, 6, 1, 2, 1, 2, 2, 1, 2)


@pytest.mark.parametrize("tag", [0xA3, 0xA4, 0xA6, 0xA7, 0xA2, 0xA8])
def test_only_read_pdus_can_be_built(tag):
    with pytest.raises(m.SnmpError):
        m.build_pdu(tag, 1, ["1.3.6.1.2.1.1.1.0"])


def test_source_builds_no_write_pdu_and_starts_no_process():
    text = open(SRC, encoding="utf-8").read()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            assert node.value not in (0xA3, 0xA4, 0xA6, 0xA7), "write/trap PDU constant in source"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + ([node.module] if getattr(node, "module", None) else [])
            assert not any(n in ("subprocess", "ctypes") for n in names)
    assert m._READ_PDUS == frozenset({0xA0, 0xA1, 0xA5})


# ================================================================== counters
def test_counter_tracker_hc_and_32bit_wrap():
    c = m.CounterTracker()
    assert c.delta(1, "in", 1000, 64, mono=0) is None             # baseline
    assert c.delta(1, "in", 1500, 64, mono=10) == 500
    assert c.delta(1, "in", 100, 64, mono=20) is None              # HC decrease = reset
    assert c.delta(1, "in", 400, 64, mono=30) == 300
    assert c.delta(2, "in", 2 ** 32 - 100, 32, 1_000_000_000, mono=0) is None
    assert c.delta(2, "in", 50, 32, 1_000_000_000, mono=10) == 150  # wrapped once
    # an impossible "wrap" for a slow link is a reset, not traffic
    assert c.delta(3, "in", 4_000_000_000, 32, 10_000_000, mono=0) is None
    assert c.delta(3, "in", 10, 32, 10_000_000, mono=10) is None
    # width changed (HC appeared): new baseline
    assert c.delta(2, "in", 99999, 64, mono=20) is None
    c.forget_missing({1})
    assert set(k[0] for k in c.last) == {1}


def test_counter_jump_beyond_link_speed_is_discarded_and_counted():
    c = m.CounterTracker()
    # 1 Gbit/s link, 10 s between polls: at most ~1.25 GB can pass; a 64-bit counter that jumps by 1 TB is a
    # discontinuity (interface re-initialised, agent glitch), not traffic
    assert c.delta(1, "in", 1_000, 64, 1_000_000_000, mono=0) is None
    assert c.delta(1, "in", 1_000 + 10 ** 12, 64, 1_000_000_000, mono=10) is None
    assert c.discarded == 1
    # the jump is the new baseline: normal traffic right after it counts again
    assert c.delta(1, "in", 1_000 + 10 ** 12 + 500_000_000, 64, 1_000_000_000, mono=20) == 500_000_000
    # same for 32-bit counters without a wrap
    assert c.delta(2, "out", 0, 32, 10_000_000, mono=0) is None
    assert c.delta(2, "out", 100_000_000, 32, 10_000_000, mono=10) is None
    assert c.discarded == 2
    # unknown link speed: nothing to compare against, the delta is kept
    assert c.delta(3, "in", 0, 64, None, mono=0) is None
    assert c.delta(3, "in", 10 ** 12, 64, None, mono=10) == 10 ** 12
    # full line rate (with some timing tolerance) is real traffic
    assert c.delta(4, "in", 0, 64, 1_000_000_000, mono=0) is None
    assert c.delta(4, "in", 1_400_000_000, 64, 1_000_000_000, mono=10) == 1_400_000_000
    assert c.discarded == 2


# ================================================================== fake agent
IF_DESCR = (1, 3, 6, 1, 2, 1, 2, 2, 1, 2)
IF_SPEED = (1, 3, 6, 1, 2, 1, 2, 2, 1, 5)
IF_OPER = (1, 3, 6, 1, 2, 1, 2, 2, 1, 8)
IF_IN = (1, 3, 6, 1, 2, 1, 2, 2, 1, 10)
IF_OUT = (1, 3, 6, 1, 2, 1, 2, 2, 1, 16)
IFX_NAME = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 1)
IFX_HC_IN = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 6)
IFX_HC_OUT = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 10)
IFX_HIGH = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 15)


class Mib:
    """Small switch: 3 interfaces, index 3 has no HC counters (32-bit fallback)."""

    def __init__(self):
        self.uptime = 987654
        self.hc = {1: [10 ** 12, 5 * 10 ** 11], 2: [42, 7]}
        self.lo = {1: [123, 456], 2: [42, 7], 3: [2 ** 32 - 1000, 10]}

    def table(self):
        t = {(1, 3, 6, 1, 2, 1, 1, 1, 0): ber.enc_octets(b"test switch 24 port"),
             (1, 3, 6, 1, 2, 1, 1, 3, 0): ber.enc_uint(self.uptime, 0x43),
             (1, 3, 6, 1, 2, 1, 1, 5, 0): ber.enc_octets(b"switch-a")}
        for i in (1, 2, 3):
            t[IF_DESCR + (i,)] = ber.enc_octets(b"port %d" % i)
            t[IF_SPEED + (i,)] = ber.enc_uint(100_000_000, 0x42)
            t[IF_OPER + (i,)] = ber.enc_int(1 if i != 2 else 2)
            t[IF_IN + (i,)] = ber.enc_uint(self.lo[i][0] % 2 ** 32, 0x41)
            t[IF_OUT + (i,)] = ber.enc_uint(self.lo[i][1] % 2 ** 32, 0x41)
        for i in (1, 2):
            t[IFX_NAME + (i,)] = ber.enc_octets(b"ge-%d" % i)
            t[IFX_HC_IN + (i,)] = ber.enc_uint(self.hc[i][0], 0x46)
            t[IFX_HC_OUT + (i,)] = ber.enc_uint(self.hc[i][1], 0x46)
            t[IFX_HIGH + (i,)] = ber.enc_uint(1000, 0x42)
        t[IFX_NAME + (3,)] = ber.enc_octets(b"mgmt")
        t[IFX_HIGH + (3,)] = ber.enc_uint(0, 0x42)
        return t


def _vb(oid, value_tlv):
    return ber.tlv(0x30, ber.enc_oid(oid) + value_tlv)


class FakeAgent(threading.Thread):
    """Plays an SNMP agent (v2c, or v3 USM with md5/sha/sha256 + AES); builds replies by hand."""

    def __init__(self, version="3", mode="", auth_proto="sha", level="authPriv"):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.version, self.mode, self.auth_proto, self.level = version, mode, auth_proto, level
        self.mib = Mib()
        self.k_auth = m.localize_key(m.password_to_key(AUTH_PW, auth_proto), ENGINE, auth_proto)
        self.k_priv = m.localize_key(m.password_to_key(PRIV_PW, auth_proto), ENGINE, auth_proto)[:16]
        self.boots, self.time0, self.start_mono = 5, 1000, time.monotonic()
        self.halt = threading.Event()
        self.discoveries = self.time_reports = 0
        self.pdu_types, self.msg_ids, self.salts, self.errors = [], [], [], []

    def etime(self):
        return self.time0 + int(time.monotonic() - self.start_mono)

    def run(self):
        while not self.halt.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                if self.version == "2c":
                    self.handle_v2c(data, addr)
                else:
                    self.handle_v3(data, addr)
            except Exception as exc:  # pragma: no cover
                self.errors.append(repr(exc))

    def stop(self):
        self.halt.set()
        self.join(2)
        self.sock.close()

    def answer(self, ptag, a, b, vbs):
        table = self.mib.table()
        if ptag == 0xA0:
            return [_vb(oid, table.get(oid, ber.tlv(0x81, b""))) for oid, _ in vbs]
        if ptag == 0xA5:
            assert a == 0 and b > 0 and len(vbs) == 1
            following = sorted(o for o in table if o > vbs[0][0])[:b]
            out = [_vb(o, table[o]) for o in following]
            out += [_vb(following[-1] if following else vbs[0][0], ber.tlv(0x82, b""))] * (b - len(following))
            return out
        self.errors.append("unexpected PDU 0x%02x" % ptag)
        return None

    # ------------------------------------------------------------ v2c
    def handle_v2c(self, data, addr):
        version, community, node = m.decode_v2c(data)
        if community != COMMUNITY.encode():
            return                                           # real agents stay silent
        ptag, rid, a, b, vbs = m.parse_pdu(node)
        self.pdu_types.append(ptag)
        out = self.answer(ptag, a, b, vbs)
        if out is None:
            return
        if self.mode == "foreign":
            self.sock.sendto(b"\x30\x03\x02\x01", addr)
            wrong = [_vb(oid, ber.enc_octets(b"WRONG")) for oid, _ in vbs]
            pdu = ber.tlv(0xA2, ber.enc_int(rid + 1) + ber.enc_int(0) + ber.enc_int(0) + ber.tlv(0x30, b"".join(wrong)))
            self.sock.sendto(m.encode_v2c(community, pdu), addr)                       # foreign request id
            pdu = ber.tlv(0xA2, ber.enc_int(rid) + ber.enc_int(0) + ber.enc_int(0) + ber.tlv(0x30, b"".join(wrong)))
            self.sock.sendto(m.encode_v2c(b"other", pdu), addr)                         # other community
        status = 6 if self.mode == "noaccess" else 0
        pdu = ber.tlv(0xA2, ber.enc_int(rid) + ber.enc_int(status) + ber.enc_int(0) + ber.tlv(0x30, b"".join(out)))
        self.sock.sendto(m.encode_v2c(community, pdu), addr)

    # ------------------------------------------------------------ v3
    def send_v3(self, addr, msg_id, ptag, rid, vbs, level, user=USER.encode(), bad_mac=False):
        pdu = ber.tlv(ptag, ber.enc_int(rid) + ber.enc_int(0) + ber.enc_int(0) + ber.tlv(0x30, b"".join(vbs)))
        scoped = m.scoped_pdu(ENGINE, pdu)
        boots, etime = self.boots, self.etime()
        salt = b""
        if level == "authPriv":
            salt = os.urandom(8)
            data = ber.enc_octets(aes.cfb(self.k_priv, boots.to_bytes(4, "big") + etime.to_bytes(4, "big") + salt,
                                          scoped, False))
            flags = 3
        elif level == "authNoPriv":
            data, flags = scoped, 1
        else:
            data, flags = scoped, 0
        mac_len = 24 if self.auth_proto == "sha256" else 12
        auth = bytes(mac_len) if flags else b""
        raw = m.encode_v3(msg_id, flags, ENGINE, boots, etime, user, auth, salt, data)
        if flags:
            sig = m.mac(self.k_auth, raw, self.auth_proto)
            if bad_mac:
                sig = bytes((sig[0] ^ 1,)) + sig[1:]
            raw = m.encode_v3(msg_id, flags, ENGINE, boots, etime, user, sig, salt, data)
        self.sock.sendto(raw, addr)

    def report(self, addr, msg_id, rid, oid, level="noAuthNoPriv"):
        self.send_v3(addr, msg_id, 0xA8, rid, [_vb(oid, ber.enc_uint(1, 0x41))], level)

    def handle_v3(self, data, addr):
        n = m.decode_v3(data)
        self.msg_ids.append(n["msg_id"])
        if n["engine_id"] == b"":
            assert n["flags"] == 4
            _, rid, _, _, _ = m.parse_scoped(n["data"])
            self.discoveries += 1
            self.report(addr, n["msg_id"], rid, m._OID_UNKNOWN_ENGINE)
            if self.mode == "restart" and self.discoveries == 1:
                self.boots += 1      # agent restarts: next access lies outside the time window
            return
        expected_flags = {"authPriv": 7, "authNoPriv": 5, "noAuthNoPriv": 4}[self.level]
        assert n["flags"] == expected_flags and n["engine_id"] == ENGINE
        if n["user"] != USER.encode():
            self.report(addr, n["msg_id"], 0, m._OID_UNKNOWN_USER)
            return
        if n["flags"] & 1:
            if not m.verify_mac(self.k_auth, data, *n["auth_span"], proto=self.auth_proto):
                self.report(addr, n["msg_id"], 0, m._OID_WRONG_DIGEST)
                return
            if self.mode == "always_time" or n["boots"] != self.boots or abs(n["time"] - self.etime()) > 150:
                self.time_reports += 1
                self.boots += self.mode == "always_time"
                self.report(addr, n["msg_id"], 0, m._OID_NOT_IN_TIME, "authNoPriv")
                return
        if n["flags"] & 2:
            self.salts.append(n["priv"])
            iv = n["boots"].to_bytes(4, "big") + n["time"].to_bytes(4, "big") + n["priv"]
            clear = aes.cfb(self.k_priv, iv, n["data"], True)
            try:
                scoped, _ = ber.read_node(clear, 0, len(clear))
                ptag, rid, a, b, vbs = m.parse_scoped(scoped)
            except ber.BerError:
                self.report(addr, n["msg_id"], 0, m._OID_DECRYPTION)
                return
        else:
            ptag, rid, a, b, vbs = m.parse_scoped(n["data"])
        self.pdu_types.append(ptag)
        out = self.answer(ptag, a, b, vbs)
        if out is None:
            return
        if self.mode == "foreign":
            self.sock.sendto(b"\x30\x03\x02\x01", addr)
            self.sock.sendto(os.urandom(80), addr)
            wrong = [_vb(oid, ber.enc_octets(b"WRONG")) for oid, _ in vbs]
            self.send_v3(addr, (n["msg_id"] + 1) & 0x7FFFFFFF, 0xA2, rid, wrong, self.level)
            self.send_v3(addr, n["msg_id"], 0xA2, rid + 1, wrong, self.level)
            if self.level == "authPriv":
                self.send_v3(addr, n["msg_id"], 0xA2, rid, wrong, "authNoPriv")         # downgraded
            self.send_v3(addr, n["msg_id"], 0xA2, rid, wrong, "noAuthNoPriv")
            self.send_v3(addr, n["msg_id"], 0xA2, rid, wrong, self.level, bad_mac=True)
        self.send_v3(addr, n["msg_id"], 0xA2, rid, out, self.level, bad_mac=self.mode == "bad_mac")


@pytest.fixture
def agents():
    made = []

    def make(version="3", mode="", auth_proto="sha", level="authPriv", **client_kw):
        a = FakeAgent(version, mode, auth_proto, level)
        a.start()
        kw = dict(port=a.port, timeout=1.0, retries=2)
        if version == "2c":
            kw.update(version="2c", community=client_kw.pop("community", COMMUNITY))
        else:
            kw.update(version="3", user=client_kw.pop("user", USER), auth_proto=auth_proto,
                      auth_key=None if level == "noAuthNoPriv" else client_kw.pop("auth_key", AUTH_PW),
                      priv_key=client_kw.pop("priv_key", PRIV_PW) if level == "authPriv" else None)
        kw.update(client_kw)
        c = m.SnmpClient("127.0.0.1", **kw)
        made.append((a, c))
        return a, c

    yield make
    for a, c in made:
        c.close()
        a.stop()
        assert a.errors == []


@pytest.mark.parametrize("auth_proto", ["md5", "sha", "sha256"])
@pytest.mark.parametrize("level", ["authPriv", "authNoPriv", "noAuthNoPriv"])
def test_v3_get_and_walk_all_protocols(agents, auth_proto, level):
    agent, client = agents(auth_proto=auth_proto, level=level)
    assert client.level == level
    vals = client.get(["1.3.6.1.2.1.1.5.0", ".1.3.6.1.2.1.1.3.0", "1.3.6.1.2.1.1.9.0"])
    assert vals == {"1.3.6.1.2.1.1.5.0": "switch-a", "1.3.6.1.2.1.1.3.0": 987654, "1.3.6.1.2.1.1.9.0": None}
    rows = client.walk("1.3.6.1.2.1.31.1.1.1.6", repetitions=1)
    assert rows == [("1.3.6.1.2.1.31.1.1.1.6.1", 10 ** 12), ("1.3.6.1.2.1.31.1.1.1.6.2", 42)]
    assert client.walk("1.3.6.1.2.1.31.1.1.1.8") == []
    assert set(agent.pdu_types) == {0xA0, 0xA5}
    assert agent.discoveries == 1


def test_v3_discovery_time_resync_and_salts(agents):
    agent, client = agents(mode="restart")
    assert client.get("1.3.6.1.2.1.1.5.0") == {"1.3.6.1.2.1.1.5.0": "switch-a"}
    assert agent.discoveries == 1 and agent.time_reports == 1 and client._boots == 6
    client.walk("1.3.6.1.2.1.2.2.1.2", repetitions=2)
    assert len(set(agent.msg_ids)) == len(agent.msg_ids)
    salts = [int.from_bytes(s, "big") for s in agent.salts]
    assert all(b == a + 1 for a, b in zip(salts, salts[1:])), "salt increments per message"
    assert list(client._keys) == [ENGINE]


def test_v3_foreign_and_broken_replies_dropped(agents):
    agent, client = agents(mode="foreign")
    assert client.get("1.3.6.1.2.1.1.5.0") == {"1.3.6.1.2.1.1.5.0": "switch-a"}
    assert client.dropped >= 5


def test_v3_foreign_replies_dropped_auth_no_priv(agents):
    agent, client = agents(mode="foreign", level="authNoPriv")
    assert client.get("1.3.6.1.2.1.1.5.0") == {"1.3.6.1.2.1.1.5.0": "switch-a"}
    assert client.dropped >= 4


def test_v3_bad_mac_gives_auth_error(agents):
    agent, client = agents(mode="bad_mac", timeout=0.3, retries=1)
    with pytest.raises(m.SnmpError) as e:
        client.get("1.3.6.1.2.1.1.5.0")
    assert e.value.key == "source.snmp.auth_failed"


def test_v3_wrong_passphrase_reports_and_no_secret_in_errors(agents):
    agent, client = agents(auth_key="wrong-auth-phrase-1111", timeout=0.5, retries=1)
    with pytest.raises(m.SnmpError) as e:
        client.get("1.3.6.1.2.1.1.5.0")
    assert e.value.key == "source.snmp.auth_failed"
    text = str(e.value) + repr(e.value) + "".join(traceback.format_exception(e.value)) + repr(client) + str(client)
    for secret in ("wrong-auth-phrase-1111", PRIV_PW, AUTH_PW):
        assert secret not in text
    assert not hasattr(client, "auth_key") and not hasattr(client, "priv_key")
    assert all(not isinstance(v, str) or v not in (AUTH_PW, PRIV_PW) for v in vars(client).values())


def test_v3_unknown_user(agents):
    agent, client = agents(user="nobody", timeout=0.5, retries=1)
    with pytest.raises(m.SnmpError) as e:
        client.get("1.3.6.1.2.1.1.5.0")
    assert e.value.key == "source.snmp.auth_failed" and "unknown" in str(e.value)


def test_v3_resync_only_once(agents):
    agent, client = agents(mode="always_time", timeout=0.5, retries=1)
    with pytest.raises(m.SnmpError):
        client.get("1.3.6.1.2.1.1.5.0")
    assert agent.time_reports == 2


def test_v2c_get_walk_and_foreign(agents):
    agent, client = agents(version="2c", mode="foreign")
    assert client.get("1.3.6.1.2.1.1.5.0") == {"1.3.6.1.2.1.1.5.0": "switch-a"}
    names = client.walk("1.3.6.1.2.1.31.1.1.1.1", repetitions=2)
    assert [v for _, v in names] == ["ge-1", "ge-2", "mgmt"]
    assert client.dropped >= 2


def test_v2c_wrong_community_times_out_without_leaking(agents):
    agent, client = agents(version="2c", community="wrong-community-77", timeout=0.3, retries=1)
    with pytest.raises(m.SnmpError) as e:
        client.get("1.3.6.1.2.1.1.5.0")
    assert e.value.key == "source.snmp.timeout"
    text = str(e.value) + "".join(traceback.format_exception(e.value)) + repr(client)
    assert "wrong-community-77" not in text and COMMUNITY not in text


def test_v2c_error_status(agents):
    agent, client = agents(version="2c", mode="noaccess")
    with pytest.raises(m.SnmpError) as e:
        client.get("1.3.6.1.2.1.1.5.0")
    assert e.value.key == "source.snmp.auth_failed"


@pytest.mark.parametrize("kw", [
    dict(version="1", community="x"), dict(version="2c"), dict(version="3"), dict(version="3", user="u" * 40),
    dict(version="3", user="u", priv_key="p"), dict(version="3", user="u", auth_key="a", auth_proto="sha512"),
    dict(version="3", user="u", auth_key="a", priv_key="p", priv_proto="des"), dict(version="2c", community="x", port=0),
])
def test_config_validation(kw):
    with pytest.raises(m.SnmpError) as e:
        m.SnmpClient("127.0.0.1", **kw)
    assert e.value.key == "source.snmp.config"


# ================================================================== source
def test_source_poll_deltas_and_fallback(agents):
    agent, _ = agents(version="2c")
    src = m.Snmp("127.0.0.1", version="2c", community=COMMUNITY, port=agent.port, timeout=1.0)
    assert COMMUNITY not in repr(src)
    f1 = src.poll(src._client)
    assert f1["type"] == "snmp" and f1["host"] == "127.0.0.1"
    assert f1["sys"] == {"name": "switch-a", "descr": "test switch 24 port", "uptime": 9876.54}
    by = {i["index"]: i for i in f1["ifaces"]}
    assert by[1]["name"] == "ge-1" and by[1]["oper"] == "up" and by[1]["speed"] == 1_000_000_000
    assert by[2]["oper"] == "down"
    assert by[3]["name"] == "mgmt" and by[3]["speed"] == 100_000_000      # ifHighSpeed 0 -> ifSpeed
    assert all(i["in"] is None and i["out"] is None for i in f1["ifaces"])
    agent.mib.uptime += 1000
    agent.mib.hc[1][0] += 5000
    agent.mib.hc[1][1] += 70
    agent.mib.lo[1][0] += 999999                 # 32-bit counters must be ignored when HC exist
    agent.mib.lo[3][0] += 3000                   # wraps the 32-bit counter
    agent.mib.lo[3][1] += 5
    f2 = src.poll(src._client)
    by = {i["index"]: i for i in f2["ifaces"]}
    assert (by[1]["in"], by[1]["out"]) == (5000, 70)
    assert (by[2]["in"], by[2]["out"]) == (0, 0)
    assert (by[3]["in"], by[3]["out"]) == (3000, 5)
    agent.mib.uptime = 10                        # agent restarted -> baselines dropped
    agent.mib.hc[1][0] += 1
    f3 = src.poll(src._client)
    assert all(i["in"] is None for i in f3["ifaces"])


def test_source_run_emits_frames_and_status(agents):
    agent, _ = agents(version="3", auth_proto="sha256")
    src = m.Snmp("127.0.0.1", version="3", user=USER, auth_key=AUTH_PW, priv_key=PRIV_PW, auth_proto="sha256",
                 port=agent.port, interval=1.0, timeout=1.0)
    events = []

    async def go():
        task = asyncio.create_task(src.run(events.append))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if sum(e["type"] == "snmp" for e in events) >= 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert events[1] == {"type": "status", "source": "snmp", "ok": True, "key": "source.snmp.ok", "t": events[1]["t"]}
    snmp_frames = [e for e in events if e["type"] == "snmp"]
    assert len(snmp_frames) >= 2 and len(snmp_frames[-1]["ifaces"]) == 3
    blob = repr(events)
    assert AUTH_PW not in blob and PRIV_PW not in blob


def test_source_run_unreachable_never_raises():
    # a closed local UDP port: no reply -> status timeout, backoff, keeps running until cancelled
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    src = m.Snmp("127.0.0.1", version="2c", community=COMMUNITY, port=port, timeout=0.2, retries=1)
    events = []

    async def go():
        task = asyncio.create_task(src.run(events.append))
        await asyncio.sleep(1.0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert events and events[0]["ok"] is False and events[0]["key"] == "source.snmp.timeout"


def test_source_test_details(agents):
    agent, _ = agents(version="2c")
    src = m.Snmp("127.0.0.1", version="2c", community=COMMUNITY, port=agent.port, timeout=1.0)
    ok, key, details = asyncio.run(src.test())
    assert (ok, key) == (True, "source.snmp.ok")
    assert details == {"name": "switch-a", "descr": "test switch 24 port", "ifaces": 3}
    bad = m.Snmp("127.0.0.1", version="2c", community="nope", port=agent.port, timeout=0.2, retries=1)
    assert asyncio.run(bad.test()) == (False, "source.snmp.timeout", {})
