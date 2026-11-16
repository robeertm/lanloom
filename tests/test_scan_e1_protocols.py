"""Identification protocols (NDP, WS-Discovery, NetBIOS, LLMNR, SNMP): parsers, plan defaults, runner phases."""
import asyncio
import socket
import struct
import uuid

import pytest

from lanloom.scan import _udp, dnsmsg, llmnr, make_plan, nbns, ndp, run, runner, snmpprobe, wsd
from lanloom.sources import _ber as ber

from test_scan_runner import ALIVE, NET, fake_net  # noqa: F401  (fixture reuse)


# ---------------------------------------------------------------- NDP
def _neigh(family=socket.AF_INET6, ip="2001:db8::5", mac=bytes.fromhex("020000000005"), state=0x02, ifindex=2,
           mtype=28):
    attrs = b""
    if ip:
        dst = socket.inet_pton(socket.AF_INET6, ip)
        attrs += struct.pack("=HH", 4 + len(dst), 1) + dst
    if mac:
        attrs += struct.pack("=HH", 4 + len(mac), 2) + mac + b"\x00\x00"
    body = struct.pack("=BBHiHBB", family, 0, 0, ifindex, state, 0, 0) + attrs
    return struct.pack("=IHHII", 16 + len(body), mtype, 2, 1, 0) + body


def test_parse_neigh_dump():
    done = struct.pack("=IHHII", 20, 3, 2, 1, 0) + b"\x00" * 4
    data = (_neigh() + _neigh(ip="fe80::1", state=0x04) + _neigh(state=0x20) + _neigh(state=0x01)
            + _neigh(state=0x40) + _neigh(ip="ff02::1") + _neigh(mac=bytes(6)) + _neigh(mac=None)
            + _neigh(family=socket.AF_INET) + done + _neigh(ip="2001:db8::9"))
    got = ndp.parse_neigh_dump(data)
    assert [(e["ip"], e["mac"], e["state"]) for e in got] == [
        ("2001:db8::5", "02:00:00:00:00:05", "reachable"), ("fe80::1", "02:00:00:00:00:05", "stale")]
    assert got[0]["iface_index"] == 2


def test_parse_neigh_dump_garbage():
    assert ndp.parse_neigh_dump(b"") == []
    assert ndp.parse_neigh_dump(b"\xff" * 7) == []
    assert ndp.parse_neigh_dump(_neigh()[:-5]) == []
    assert ndp.parse_neigh_dump(struct.pack("=IHHII", 4, 28, 0, 0, 0)) == []


def test_group_by_mac_orders_link_local_last_and_caps():
    entries = [{"ip": "fe80::1", "mac": "aa"}] + [{"ip": "2001:db8::%x" % i, "mac": "aa"} for i in range(1, 12)]
    grouped = ndp.group_by_mac(entries)
    assert len(grouped["aa"]) == 8 and "fe80::1" not in grouped["aa"]
    assert ndp.group_by_mac(entries[:2])["aa"][-1] == "fe80::1"


def test_read_ndp_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no netlink")
    monkeypatch.setattr(socket, "socket", boom)
    assert ndp.read_ndp() is None


# ---------------------------------------------------------------- WS-Discovery
_MATCH = (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
          b' xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
          b' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
          b' xmlns:i="http://example.org/pnpx/2005/10"><s:Body><d:ProbeMatches><d:ProbeMatch>'
          b'<a:EndpointReference><a:Address>urn:uuid:11111111-2222-3333-4444-555555555555</a:Address>'
          b'</a:EndpointReference><d:Types>i:NetworkVideoTransmitter wsdp:Device</d:Types>'
          b'<d:XAddrs>http://192.168.77.12:80/onvif/device_service ftp://x/y</d:XAddrs>'
          b'</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>')


def test_probe_match_parsed():
    r = wsd.parse_probe_match(_MATCH)
    assert r == {"types": ["NetworkVideoTransmitter", "Device"],
                 "xaddrs": ["http://192.168.77.12:80/onvif/device_service"],
                 "endpoint": "11111111-2222-3333-4444-555555555555"}


def test_probe_match_limits_and_rejections():
    many = b"".join(b" t:T%d" % i for i in range(20))
    r = wsd.parse_probe_match(_MATCH.replace(b"i:NetworkVideoTransmitter wsdp:Device", many))
    assert len(r["types"]) == 8
    long_type = b"t:" + b"x" * 300
    r = wsd.parse_probe_match(_MATCH.replace(b"i:NetworkVideoTransmitter", long_type))
    assert all(len(t) <= 120 for t in r["types"])
    assert wsd.parse_probe_match(b"") is None and wsd.parse_probe_match(b"<a><b></a>") is None
    assert wsd.parse_probe_match(_MATCH + b" " * (64 * 1024)) is None
    assert wsd.parse_probe_match(b"<root/>") is None
    bad = _MATCH.replace(b"<s:Envelope", b'<!DOCTYPE x [<!ENTITY e "boom">]><s:Envelope', 1)
    assert wsd.parse_probe_match(bad) is None
    bad = _MATCH.replace(b"<?xml version=\"1.0\"?>", b'<?xml version="1.0"?><!ENTITY e "x">')
    assert wsd.parse_probe_match(bad) is None


def test_probe_message_is_well_formed():
    pkt = wsd.build_probe()
    root = wsd.ET.fromstring(pkt)
    assert any(wsd._local(e.tag) == "Probe" for e in root.iter())
    text = pkt.decode()
    mid = text.split("urn:uuid:")[1].split("<")[0]
    uuid.UUID(mid)


# ---------------------------------------------------------------- NetBIOS
def _nbstat_answer(tid, names, mac=bytes.fromhex("0a0b0c0d0e0f"), rcode=0):
    rr = b"".join(n.ljust(15).encode() + bytes((suffix,)) + struct.pack("!H", 0x8000 if group else 0x0400)
                  for n, suffix, group in names)
    rdata = bytes((len(names),)) + rr + mac + b"\x00" * 40
    return (struct.pack("!HHHHHH", tid, 0x8400 | rcode, 0, 1, 0, 0) + b"\xc0\x0c"[:0]
            + b"\x20" + nbns._encode_star() + b"\x00" + struct.pack("!HHIH", 0x21, 1, 0, len(rdata)) + rdata)


def test_nbstat_query_bytes():
    q = nbns.build_nbstat_query(0x1234)
    assert q[:4] == b"\x12\x34\x00\x00" and q[4:6] == b"\x00\x01"
    assert q[12] == 0x20 and q[13:15] == b"CK" and q[15:45] == b"A" * 30 and q[45] == 0
    assert q[46:] == b"\x00\x21\x00\x01" and len(q) == 50


def test_nbstat_response():
    data = _nbstat_answer(7, [("PRINTBOX", 0, False), ("WORKGROUP", 0, True), ("PRINTBOX", 0x20, False)])
    r = nbns.parse_nbstat_response(data, 7)
    assert r["computer"] == "PRINTBOX" and r["workgroup"] == "WORKGROUP" and r["mac"] == "0a:0b:0c:0d:0e:0f"
    assert r["names"][1] == {"name": "WORKGROUP", "suffix": 0, "group": True}
    assert nbns.parse_nbstat_response(data, 8) is None            # wrong transaction id
    assert nbns.parse_nbstat_response(data) is not None
    assert nbns.parse_nbstat_response(_nbstat_answer(7, [("A", 0, False)], mac=bytes(6)), 7)["mac"] is None
    assert nbns.parse_nbstat_response(_nbstat_answer(7, [("A", 0, False)], rcode=3), 7) is None


def test_nbstat_limits_and_garbage():
    many = [("N%d" % i, 0x20, False) for i in range(40)]
    r = nbns.parse_nbstat_response(_nbstat_answer(1, many), 1)
    assert r is None or len(r["names"]) <= 16
    r = nbns.parse_nbstat_response(_nbstat_answer(1, [("A\x01B", 0, False)]), 1)
    assert r["computer"] == "A B"
    for junk in (b"", b"\x00" * 11, b"\xff" * 300, _nbstat_answer(1, [("A", 0, False)])[:-60]):
        assert nbns.parse_nbstat_response(junk, 1) is None


# ---------------------------------------------------------------- LLMNR
def test_llmnr_answer():
    exp = llmnr.reverse_name("192.168.77.5")
    assert exp == "5.77.168.192.in-addr.arpa"
    ans = dnsmsg.build_record(exp, dnsmsg.T_PTR, dnsmsg.rdata_ptr("laptop"))
    data = dnsmsg.build_response([ans], qid=0x4242)
    assert llmnr.parse_answer(data, (0x4242, exp)) == "laptop"
    assert llmnr.parse_answer(data, (0x4243, exp)) is None
    assert llmnr.parse_answer(data, (0x4242, "6.77.168.192.in-addr.arpa")) is None
    assert llmnr.parse_answer(b"\x00\x01", (1, exp)) is None
    dotted = dnsmsg.build_response([dnsmsg.build_record(exp, dnsmsg.T_PTR, dnsmsg.rdata_ptr("host.lan."))], qid=1)
    assert llmnr.parse_answer(dotted, (1, exp)) == "host.lan"
    long = dnsmsg.build_response([dnsmsg.build_record(exp, dnsmsg.T_PTR, dnsmsg.rdata_ptr("a" * 60 + ".b"))], qid=1)
    assert len(llmnr.parse_answer(long, (1, exp))) <= 253


# ---------------------------------------------------------------- SNMP
def _snmp_response(rid, vbs, status=0):
    body = b"".join(ber.tlv(ber.T_SEQUENCE, ber.enc_oid(o) + v) for o, v in vbs)
    pdu = ber.tlv(0xA2, ber.enc_int(rid) + ber.enc_int(status) + ber.enc_int(0) + ber.tlv(ber.T_SEQUENCE, body))
    return ber.tlv(ber.T_SEQUENCE, ber.enc_int(1) + ber.enc_octets(b"public") + pdu)


def test_snmp_request_and_response():
    req = ber.decode(snmpprobe.build_get_request(77))
    assert req[1][2][0] == 0xA0
    resp = _snmp_response(77, [
        (snmpprobe.OID_DESCR, ber.enc_octets(b"Example switch 1.0\x00")),
        (snmpprobe.OID_OBJECT_ID, ber.enc_oid("1.3.6.1.4.1.4526.100.1")),
        (snmpprobe.OID_NAME, ber.enc_octets(b"sw-" + b"x" * 100))])
    r = snmpprobe.parse_get_response(resp, 77)
    assert r["descr"] == "Example switch 1.0" and r["object_id"] == "1.3.6.1.4.1.4526.100.1"
    assert r["enterprise"] == 4526 and len(r["name"]) == 64
    assert snmpprobe.parse_get_response(resp, 78) is None
    assert snmpprobe.parse_get_response(_snmp_response(77, [], status=2), 77) is None
    assert snmpprobe.parse_get_response(resp[:-4], 77) is None
    assert snmpprobe.parse_get_response(b"", 77) is None


def test_snmp_missing_values_and_non_enterprise_oid():
    resp = _snmp_response(5, [(snmpprobe.OID_DESCR, ber.tlv(ber.T_NO_SUCH_OBJECT, b"")),
                              (snmpprobe.OID_OBJECT_ID, ber.enc_oid("1.3.6.1.2.1.1.9"))])
    r = snmpprobe.parse_get_response(resp, 5)
    assert r["descr"] is None and r["enterprise"] is None and r["object_id"] == "1.3.6.1.2.1.1.9"
    empty = _snmp_response(5, [(snmpprobe.OID_DESCR, ber.tlv(ber.T_NO_SUCH_OBJECT, b""))])
    assert snmpprobe.parse_get_response(empty, 5) is None


# ---------------------------------------------------------------- shared UDP helper (loopback, no external traffic)
def test_ask_accepts_only_asked_host_and_port(monkeypatch):
    async def go():
        loop = asyncio.get_running_loop()
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        srv.setblocking(False)
        port = srv.getsockname()[1]

        def echo():
            data, addr = srv.recvfrom(100)
            srv.sendto(b"ok:" + data, addr)
        loop.add_reader(srv, echo)

        class B:
            async def acquire(self):
                pass
        try:
            res = await _udp.ask(["127.0.0.1", "127.0.0.1", "::1", "nonsense"], port,
                                 lambda ip: (1, b"x"), lambda d, t: d, B(), 1.0)
            assert res == {"127.0.0.1": b"ok:x"}
            res = await _udp.ask(["127.0.0.1"], port + 1 if port < 65535 else port - 1,
                                 lambda ip: (1, b"x"), lambda d, t: d, B(), 0.2)
            assert res == {}
        finally:
            loop.remove_reader(srv)
            srv.close()
    asyncio.run(go())


# ---------------------------------------------------------------- plan
def test_plan_defaults():
    for deep in (False, True):
        p = make_plan(["192.168.77.0/24"], deep=deep, consent=deep)
        assert p["snmp_public"] is False
        assert all(p[k] is True for k in ("ndp", "wsd", "netbios", "llmnr"))
        assert p["wsd_s"] == 3.0 and p["udp_timeout"] == 1.0
        runner.validate_plan(p)
    p = make_plan(["192.168.77.0/24"], snmp_public=True, wsd_s=0, udp_timeout=0.2)
    assert p["snmp_public"] is True
    for bad in ({"snmp_public": 1}, {"wsd_s": 11}, {"udp_timeout": 0.1}, {"netbios": "yes"}):
        with pytest.raises(ValueError):
            make_plan(["192.168.77.0/24"], **bad)


def test_old_plan_without_new_keys_is_valid():
    p = make_plan(["192.168.77.0/24"])
    for k in runner.NEW_OPTIONS + runner.NEW_SWITCHES:
        del p[k]
    runner.validate_plan(p)
    full = runner._with_defaults(p)
    assert full["wsd_s"] == 3.0 and full["snmp_public"] is False and full["ndp"] is True
    p["ndp"] = "x"
    with pytest.raises(ValueError):
        runner.validate_plan(p)


# ---------------------------------------------------------------- runner phases
@pytest.fixture
def identify(monkeypatch, fake_net):
    seen = {}
    mac = lambda last: "02:00:00:00:00:%02x" % last

    async def probe(duration, networks=(), iface_ips=(), progress=None):
        return ({"192.168.77.12": [{"types": ["Device"], "xaddrs": [], "endpoint": None}],
                 "10.1.1.1": [{"types": ["Device"], "xaddrs": [], "endpoint": None}]}, 2)

    def read_ndp(timeout=1.0):
        return {mac(10): ["2001:db8::10", "fe80::10"], mac(250): ["2001:db8::fa"]}

    async def nb(ips, bucket, timeout, on_sent=None):
        seen["netbios"] = list(ips)
        for _ in ips:
            on_sent()
        return {"192.168.77.11": {"names": [], "computer": "PRINTER11", "workgroup": "HOME", "mac": "0a:00:00:00:00:01"},
                "192.168.77.77": {"computer": "GHOST", "workgroup": None, "mac": None}}

    async def ll(ips, bucket, timeout, on_sent=None):
        seen["llmnr"] = list(ips)
        return {ip: "llmnr-" + ip.rsplit(".", 1)[1] for ip in ips}

    async def sn(ips, bucket, timeout, on_sent=None):
        seen["snmp"] = list(ips)
        return {"192.168.77.1": {"descr": "Router", "object_id": "1.3.6.1.4.1.9.1", "name": "gw",
                                 "enterprise": 9, "extra": "dropped"}}

    monkeypatch.setattr(wsd, "probe", probe)
    monkeypatch.setattr(ndp, "read_ndp", read_ndp)
    monkeypatch.setattr(nbns, "query", nb)
    monkeypatch.setattr(llmnr, "reverse", ll)
    monkeypatch.setattr(snmpprobe, "query", sn)
    return seen


def _plan(**kw):
    return make_plan([NET], settle_s=0.0, rate=100, **kw)


def test_runner_identify_phases(identify):
    events = []
    res = asyncio.run(run(_plan(snmp_public=True), progress=events.append))
    hosts = {h["ip"]: h for h in res["hosts"]}
    assert hosts["192.168.77.12"]["wsd"] == [{"types": ["Device"], "xaddrs": [], "endpoint": None}]
    assert "wsd" in hosts["192.168.77.12"]["sources"] and "10.1.1.1" not in hosts
    assert hosts["192.168.77.10"]["ipv6"] == ["2001:db8::10", "fe80::10"] and "ndp" in hosts["192.168.77.10"]["sources"]
    assert not any(a for h in res["hosts"] for a in h["ipv6"] if a.endswith("::fa"))      # no host created by NDP
    assert "192.168.77.250" not in hosts
    nb = hosts["192.168.77.11"]
    assert nb["netbios"] == {"computer": "PRINTER11", "workgroup": "HOME", "mac": "0a:00:00:00:00:01"}
    assert "PRINTER11" in nb["hostnames"] and "192.168.77.77" not in hosts
    assert "192.168.77.50" not in identify["netbios"] and "192.168.77.50" not in identify["snmp"]
    # LLMNR only for hosts that still have no name
    assert hosts["192.168.77.13"]["hostnames"] == ["llmnr-13"] and "llmnr" in hosts["192.168.77.13"]["sources"]
    assert "192.168.77.10" not in identify["llmnr"] and "192.168.77.11" not in identify["llmnr"]
    assert hosts["192.168.77.1"]["snmp"] == {"descr": "Router", "object_id": "1.3.6.1.4.1.9.1", "name": "gw",
                                             "enterprise": 9}
    assert hosts["192.168.77.13"]["snmp"] is None and hosts["192.168.77.13"]["netbios"] is None
    ident = [e for e in events if e["phase"] == "identify"]
    assert ident[0]["done"] == 0 and ident[-1]["done"] == ident[-1]["total"] > 0
    phases = [e["phase"] for e in events]
    assert phases.index("identify") > phases.index("banners")


def test_runner_snmp_off_by_default_and_switches(identify):
    res = asyncio.run(run(_plan()))
    assert "snmp" not in identify and all(h["snmp"] is None for h in res["hosts"])
    identify.clear()
    res = asyncio.run(run(_plan(ndp=False, wsd=False, netbios=False, llmnr=False)))
    assert not identify
    assert all(not h["wsd"] and not h["ipv6"] and h["netbios"] is None for h in res["hosts"])


def test_runner_counts_datagrams_and_survives_errors(identify, monkeypatch):
    base = asyncio.run(run(_plan(netbios=False, llmnr=False, wsd=False, ndp=False)))["stats"]["probes"]
    full = asyncio.run(run(_plan()))["stats"]["probes"]
    assert full > base

    async def boom(*a, **kw):
        raise RuntimeError("x")
    for mod, fn in ((wsd, "probe"), (nbns, "query"), (llmnr, "reverse"), (snmpprobe, "query")):
        monkeypatch.setattr(mod, fn, boom)
    monkeypatch.setattr(ndp, "read_ndp", lambda timeout=1.0: 1 / 0)
    res = asyncio.run(run(_plan(snmp_public=True)))
    assert len(res["hosts"]) >= 5


def test_runner_old_plan_runs(identify):
    p = _plan()
    for k in runner.NEW_OPTIONS + runner.NEW_SWITCHES:
        del p[k]
    res = asyncio.run(run(p))
    assert res["hosts"] and "snmp" not in identify
