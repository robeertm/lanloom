"""SNMP topology reading (LLDP neighbours + bridge forwarding table) - pure parsers and a fake agent on 127.0.0.1."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_sources_snmp as base  # noqa: E402
from test_sources_snmp import agents  # noqa: E402,F401  (fixture)
from lanloom.sources import _ber as ber  # noqa: E402
from lanloom.sources import snmp as m  # noqa: E402

REM = m.OID_LLDP_REM
MAN = m.OID_LLDP_MAN
LOC = m.OID_LLDP_LOC


def rem(col, port, idx, value, mark=100):
    return ("%s.%d.%d.%d.%d" % (REM, col, mark, port, idx), value)


def mac_bytes(text):
    return bytes(int(x, 16) for x in text.split(":"))


# ================================================================== parse_lldp
def test_lldp_mac_chassis_raw_and_text_forms():
    rows = [rem(4, 1, 1, 4), rem(5, 1, 1, mac_bytes("02:00:5e:10:00:01")),
            rem(6, 1, 1, 5), rem(7, 1, 1, b"ge-0/0/1"), rem(8, 1, 1, b"uplink"), rem(9, 1, 1, b"core-sw"),
            rem(4, 2, 1, 4), rem(5, 2, 1, b"02-00-5E-10-00-02"), rem(6, 2, 1, 7), rem(7, 2, 1, b"p2"),
            rem(4, 3, 1, 4), rem(5, 3, 1, b"02005e100003"),
            rem(4, 4, 1, 4), rem(5, 4, 1, b"02:00:5e:10:00:04")]
    out = m.parse_lldp(rows, [], [])
    assert [n["chassis_mac"] for n in out] == ["02:00:5e:10:00:01", "02:00:5e:10:00:02", "02:00:5e:10:00:03",
                                               "02:00:5e:10:00:04"]
    first = out[0]
    assert first["port_id"] == "ge-0/0/1" and first["port_desc"] == "uplink" and first["sys_name"] == "core-sw"
    assert first["chassis_ip"] is None and first["chassis_text"] is None and first["port_mac"] is None
    assert first["caps"] == [] and first["mgmt_ip"] is None
    assert out[1]["port_id"] == "p2"


def test_lldp_network_address_and_local_chassis_and_unknown_not_guessed():
    rows = [rem(4, 1, 1, 5), rem(5, 1, 1, bytes((1, 192, 168, 50, 7))),
            rem(4, 2, 1, 7), rem(5, 2, 1, b"local-chassis-9"),
            rem(5, 3, 1, mac_bytes("02:00:5e:10:00:09")),            # subtype missing: not guessed
            rem(4, 4, 1, 5), rem(5, 4, 1, bytes((2, 1, 2, 3, 4)))]   # not IPv4
    out = {n["local_port_num"]: n for n in m.parse_lldp(rows, [], [])}
    assert out[1]["chassis_ip"] == "192.168.50.7" and out[1]["chassis_mac"] is None
    assert out[2]["chassis_text"] == "local-chassis-9"
    assert (out[3]["chassis_mac"], out[3]["chassis_ip"], out[3]["chassis_text"]) == (None, None, None)
    assert out[4]["chassis_ip"] is None


def test_lldp_port_id_subtypes():
    rows = [rem(6, 1, 1, 3), rem(7, 1, 1, mac_bytes("02:00:5e:10:00:aa")),
            rem(6, 2, 1, 5), rem(7, 2, 1, b"eth0"),
            rem(6, 3, 1, 1), rem(7, 3, 1, b"alias-3"),
            rem(6, 4, 1, 7), rem(7, 4, 1, b"7")]
    out = {n["local_port_num"]: n for n in m.parse_lldp(rows, [], [])}
    assert out[1]["port_mac"] == "02:00:5e:10:00:aa" and out[1]["port_id"] == "02:00:5e:10:00:aa"
    assert out[2]["port_id"] == "eth0" and out[2]["port_mac"] is None
    assert out[3]["port_id"] == "alias-3" and out[4]["port_id"] == "7"


def test_lldp_capability_bits():
    assert m._caps(b"\x28\x00") == ["bridge", "router"]           # 0x28 = 0010 1000: bit 2 + bit 4
    assert m._caps(b"\x80") == ["other"] and m._caps(b"\x01") == ["station"]
    assert m._caps(b"\x10") == ["ap"] and m._caps(b"\x02") == ["docsis"] and m._caps(b"\x04") == ["phone"] and m._caps(b"\x08\xff") == ["router"]
    assert m._caps(b"") == [] and m._caps("text") is None
    rows = [rem(12, 1, 1, b"\x30\x00"), rem(11, 1, 1, b"\xff\x00"),     # enabled wins over supported
            rem(11, 2, 1, b"\x24\x00"),                                 # only supported given
            rem(12, 3, 1, b"\x00\x00")]
    out = {n["local_port_num"]: n for n in m.parse_lldp(rows, [], [])}
    assert out[1]["caps"] == ["bridge", "ap"] and out[2]["caps"] == ["bridge", "phone"] and out[3]["caps"] == []


def test_lldp_management_address_from_index():
    rows = [rem(4, 1, 2, 4), rem(5, 1, 2, mac_bytes("02:00:5e:10:00:01"))]
    man = [("%s.3.100.1.2.1.4.192.168.50.9" % MAN, 2),              # subtype 1, len 4
           ("%s.3.100.1.2.2.16.%s" % (MAN, ".".join(["32"] * 16)), 2),  # IPv6: ignored
           ("%s.3.100.7.1.1.4.192.168.50.77" % MAN, 2)]             # other neighbour, not in rem rows
    out = m.parse_lldp(rows, man, [])
    assert len(out) == 1 and out[0]["mgmt_ip"] == "192.168.50.9"


def test_lldp_local_port_label_lookup():
    rows = [rem(4, p, 1, 7) for p in (1, 2, 3, 4)]
    loc = [("%s.4.1" % LOC, b"Port One"), ("%s.3.1" % LOC, b"p1"), ("%s.3.2" % LOC, b"p2-id"),
           ("%s.4.2" % LOC, b"")]
    out = {n["local_port_num"]: n["local_port"] for n in m.parse_lldp(rows, [], loc, {3: "ifname-3"})}
    assert out == {1: "Port One", 2: "p2-id", 3: "ifname-3", 4: "4"}
    long_label = m.parse_lldp(rows[:1], [], [("%s.4.1" % LOC, b"x" * 80)])[0]["local_port"]
    assert len(long_label) == 32


def test_lldp_bound_and_ignored_rows():
    rows = [rem(4, 1, i, 7) for i in range(1, 600)] + [("1.0.8802.1.1.2.1.4.1.1.4.1", 1), ("9.9.9", 1),
                                                       rem(10, 1, 1, b"descr skipped")]
    assert len(m.parse_lldp(rows, [], [])) == m.MAX_LLDP
    assert len(m.parse_lldp(rows, [], [], limit=5)) == 5


# ================================================================== parse_fdb
def qrow(vlan, mac, port):
    return ("%s.%d.%s" % (m.OID_Q_FDB_PORT, vlan, ".".join(str(int(x, 16)) for x in mac.split(":"))), port)


def qstat(vlan, mac, st):
    return ("%s.%d.%s" % (m.OID_Q_FDB_STATUS, vlan, ".".join(str(int(x, 16)) for x in mac.split(":"))), st)


def drow(mac, port):
    return ("%s.%s" % (m.OID_D_FDB_PORT, ".".join(str(int(x, 16)) for x in mac.split(":"))), port)


def dstat(mac, st):
    return ("%s.%s" % (m.OID_D_FDB_STATUS, ".".join(str(int(x, 16)) for x in mac.split(":"))), st)


BASE = [("%s.%d" % (m.OID_BASE_PORT_IF, p), p + 100) for p in (1, 2)]
NAMES = {101: "ge-1", 102: "ge-2"}
A, B, C = "02:00:5e:00:00:01", "02:00:5e:00:00:02", "02:00:5e:00:00:03"


def test_fdb_q_bridge_status_filter_and_naming():
    q = [qrow(1, A, 1), qrow(1, B, 2), qrow(1, C, 2)]
    st = [qstat(1, A, 3), qstat(1, B, 4), qstat(1, C, 5)]     # B self, C mgmt: dropped
    assert m.parse_fdb(q, st, [], [], BASE, NAMES) == [{"port": "ge-1", "port_num": 1, "mac": A, "vlan": 1}]
    out = m.parse_fdb(q, [], [], [], BASE, NAMES)                 # missing status = learned
    assert [(e["mac"], e["port"]) for e in out] == [(A, "ge-1"), (B, "ge-2"), (C, "ge-2")]


def test_fdb_vlans_deduplicated_per_port():
    q = [qrow(10, A, 1), qrow(20, A, 1), qrow(30, A, 2), qrow(10, B, 1)]
    out = m.parse_fdb(q, [], [], [], BASE, NAMES)
    assert [(e["mac"], e["port_num"], e["vlan"]) for e in out] == [(A, 1, 10), (B, 1, 10), (A, 2, 30)]


def test_fdb_falls_back_to_dot1d_only_when_q_is_empty():
    d = [drow(A, 1), drow(B, 2)]
    ds = [dstat(A, 3), dstat(B, 4)]
    out = m.parse_fdb([], [], d, ds, BASE, NAMES)
    assert out == [{"port": "ge-1", "port_num": 1, "mac": A, "vlan": None}]
    q = [qrow(1, C, 2)]
    assert [e["mac"] for e in m.parse_fdb(q, [], d, ds, BASE, NAMES)] == [C]


def test_fdb_port_zero_unmapped_and_malformed_rows():
    q = [qrow(1, A, 0), qrow(1, B, 7), qrow(1, C, "x"), ("%s.1.1.2.3" % m.OID_Q_FDB_PORT, 1),
         ("%s.1.1.2.3.4.5.999" % m.OID_Q_FDB_PORT, 1)]
    out = m.parse_fdb(q, [], [], [], BASE, NAMES)
    assert out == [{"port": "7", "port_num": 7, "mac": B, "vlan": 1}]      # no ifIndex mapping: bridge port number
    assert m.parse_fdb([qrow(1, A, 1)], [], [], [], BASE, None)[0]["port"] == "1"


def test_fdb_bounds():
    q = [qrow(1, "02:00:5e:00:%02x:%02x" % (i >> 8, i & 255), 1) for i in range(5000)]
    assert len(m.parse_fdb(q, [], [], [], BASE, NAMES)) == m.MAX_FDB
    assert len(m.parse_fdb(q, [], [], [], BASE, NAMES, limit=3)) == 3


# ================================================================== fake agent
def _o(text):
    return tuple(int(x) for x in text.split("."))


class TopoMib(base.Mib):
    def __init__(self, with_topology=True):
        super().__init__()
        self.with_topology = with_topology

    def table(self):
        t = super().table()
        if not self.with_topology:
            return t
        oc = ber.enc_octets
        lldp = {4: ber.enc_int(4), 5: oc(mac_bytes("02:00:5e:10:00:01")), 6: ber.enc_int(5), 7: oc(b"ge-0/1"),
                8: oc(b"uplink"), 9: oc(b"core-sw"), 10: oc(b"long description"), 11: oc(b"\x28\x00"),
                12: oc(b"\x28\x00")}
        for col, v in lldp.items():
            t[_o("%s.%d.5.2.1" % (m.OID_LLDP_REM, col))] = v
        t[_o("%s.3.5.2.1.1.4.192.168.50.1" % m.OID_LLDP_MAN)] = ber.enc_int(2)
        t[_o("%s.3.2" % m.OID_LLDP_LOC)] = oc(b"p2-id")
        t[_o("%s.4.2" % m.OID_LLDP_LOC)] = oc(b"Port Two")
        t[_o(m.OID_BRIDGE_ADDR)] = oc(mac_bytes("02:00:5e:00:00:ff"))
        t[_o("%s.1" % m.OID_BASE_PORT_IF)] = ber.enc_int(1)
        t[_o("%s.2" % m.OID_BASE_PORT_IF)] = ber.enc_int(2)
        for vlan, mac, port, st in ((1, A, 1, 3), (1, B, 2, 3), (2, B, 2, 3), (1, C, 2, 4)):
            idx = ".".join(str(int(x, 16)) for x in mac.split(":"))
            t[_o("%s.%d.%s" % (m.OID_Q_FDB_PORT, vlan, idx))] = ber.enc_int(port)
            t[_o("%s.%d.%s" % (m.OID_Q_FDB_STATUS, vlan, idx))] = ber.enc_int(st)
        return t


def make_source(agents, topo=True, **kw):
    agent, _ = agents(version="2c")
    agent.mib = TopoMib(topo)
    src = m.Snmp("127.0.0.1", version="2c", community=base.COMMUNITY, port=agent.port, timeout=1.0, **kw)
    return agent, src


EXPECTED_LLDP = [{"local_port": "Port Two", "local_port_num": 2, "chassis_mac": "02:00:5e:10:00:01",
                  "chassis_ip": None, "chassis_text": None, "port_id": "ge-0/1", "port_mac": None,
                  "port_desc": "uplink", "sys_name": "core-sw", "caps": ["bridge", "router"],
                  "mgmt_ip": "192.168.50.1"}]
EXPECTED_FDB = [{"port": "port 1", "port_num": 1, "mac": A, "vlan": 1},
                {"port": "port 2", "port_num": 2, "mac": B, "vlan": 1}]


def test_read_topology_live(agents):
    agent, src = make_source(agents)
    src.poll(src._client)
    data = m.read_topology(src._client, src._if_names)
    assert data["agent_mac"] == "02:00:5e:00:00:ff"
    assert data["lldp"] == EXPECTED_LLDP
    assert [(e["port_num"], e["mac"], e["vlan"]) for e in data["fdb"]] == [(1, A, 1), (2, B, 1)]
    assert [e["port"] for e in data["fdb"]] == ["ge-1", "ge-2"]              # ifName beats ifDescr
    assert set(agent.pdu_types) <= {0xA0, 0xA1, 0xA5}
    # without a given name map the client walks the interface names itself
    assert m.read_topology(src._client)["fdb"] == data["fdb"]


def test_agent_without_the_mibs_gives_empty_lists(agents):
    agent, src = make_source(agents, topo=False)
    data = m.read_topology(src._client)
    assert data == {"agent_mac": None, "lldp": [], "fdb": []}
    ok, key, details = asyncio.run(src.test())
    assert ok and details["lldp"] == 0 and details["fdb"] == 0 and details["ifaces"] == 3


def test_test_reports_counts(agents):
    agent, src = make_source(agents)
    ok, key, details = asyncio.run(src.test())
    assert (ok, key) == (True, "source.snmp.ok")
    assert details == {"name": "switch-a", "descr": "test switch 24 port", "ifaces": 3, "lldp": 1, "fdb": 2}


def _run_for(src, wanted, seconds=8.0):
    events = []

    async def go():
        task = asyncio.create_task(src.run(events.append))
        for _ in range(int(seconds / 0.05)):
            await asyncio.sleep(0.05)
            if wanted(events):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    return events


def test_run_emits_topology_event_with_exact_shape(agents):
    agent, src = make_source(agents, interval=1.0, topology_interval=1.0)
    events = _run_for(src, lambda ev: sum(e["type"] == "topology" for e in ev) >= 2 and
                      sum(e["type"] == "snmp" for e in ev) >= 2)
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["snmp", "status"] and kinds.index("topology") == 2        # right after the first poll
    topo = [e for e in events if e["type"] == "topology"]
    assert len(topo) >= 2
    e = topo[0]
    assert set(e) == {"type", "source", "host", "t", "agent", "lldp", "fdb"}
    assert (e["source"], e["host"]) == ("snmp", "127.0.0.1") and isinstance(e["t"], float)
    assert e["agent"] == {"ip": "127.0.0.1", "mac": "02:00:5e:00:00:ff", "name": "switch-a"}
    assert e["lldp"] == EXPECTED_LLDP
    assert [(x["port"], x["port_num"], x["mac"], x["vlan"]) for x in e["fdb"]] == \
        [("ge-1", 1, A, 1), ("ge-2", 2, B, 1)]
    assert all(set(x) == {"port", "port_num", "mac", "vlan"} for x in e["fdb"])
    assert sum(1 for x in events if x["type"] == "status") == 1


def test_run_without_mibs_keeps_counter_frames_and_status(agents):
    agent, src = make_source(agents, topo=False, interval=1.0, topology_interval=1.0)
    events = _run_for(src, lambda ev: sum(e["type"] == "snmp" for e in ev) >= 3 and
                      sum(e["type"] == "topology" for e in ev) >= 2)
    topo = [e for e in events if e["type"] == "topology"]
    assert len(topo) >= 2 and all(e["lldp"] == [] and e["fdb"] == [] and e["agent"]["mac"] is None for e in topo)
    assert sum(e["type"] == "snmp" for e in events) >= 3
    assert [e["ok"] for e in events if e["type"] == "status"] == [True]


def test_topology_switch_off_and_failure_does_not_break_counters(agents, monkeypatch):
    agent, src = make_source(agents, topology=False, interval=1.0)
    events = _run_for(src, lambda ev: sum(e["type"] == "snmp" for e in ev) >= 2)
    assert not any(e["type"] == "topology" for e in events)

    agent2, src2 = make_source(agents, interval=1.0, topology_interval=1.0)

    def boom(*a, **k):
        raise m.SnmpError("no answer", "source.snmp.timeout")
    monkeypatch.setattr(m, "read_topology", boom)
    events = _run_for(src2, lambda ev: sum(e["type"] == "snmp" for e in ev) >= 3)
    assert sum(e["type"] == "snmp" for e in events) >= 3
    assert not any(e["type"] == "topology" for e in events)
    assert [e["ok"] for e in events if e["type"] == "status"] == [True]


def test_raw_octets_option_keeps_default_text():
    node = (0x04, bytes((0x02, 0x00, 0xff, 0x10)))
    assert isinstance(ber.value_of(node), str)
    assert ber.value_of(node, raw_octets=True) == bytes((0x02, 0x00, 0xff, 0x10))
    assert ber.value_of((0x02, b"\x05"), raw_octets=True) == 5
