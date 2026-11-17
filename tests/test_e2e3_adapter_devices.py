"""Devices only an adapter knows: the records, their booking into the inventory, and the identity stage that folds
address-only devices into the device they belong to."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import adapter_inventory as inv
from lanloom import core, demo, identity
from lanloom.store import Store

MAC_A, MAC_B = "02:aa:bb:cc:dd:01", "02:aa:bb:cc:dd:02"


def obs(**ev):
    return {"int:1": {"integ": 1, "ev": dict({"type": "topology", "source": "controller"}, **ev)}}


# ---------------------------------------------------------------------- records
def test_controller_clients_become_records_with_their_place():
    recs = inv.records(obs(clients=[{"mac": "02:AA:BB:CC:DD:01", "ip": "192.168.60.5", "name": "  Window   sensor ", "port": "7",
                                     "parent_mac": "02:aa:bb:cc:dd:ff", "medium": "wired", "vlan": 60}]))
    assert recs == [{"mac": MAC_A, "ip": "192.168.60.5", "name": "Window sensor", "role": None, "sources": ["controller"],
                     "port": "7", "parent_mac": "02:aa:bb:cc:dd:ff", "medium": "wired", "vlan": 60, "agent": None}]


def test_controller_devices_carry_their_role_and_uplink():
    recs = inv.records(obs(devices=[
        {"mac": MAC_A, "ip": "192.168.1.2", "name": "Switch", "role": "switch", "uplink": {"mac": MAC_B, "port": "1"}},
        {"mac": MAC_B, "ip": "192.168.1.3", "name": "Thing", "role": "toaster"}]))
    sw, thing = recs
    assert sw["role"] == "switch" and sw["parent_mac"] == MAC_B and sw["port"] == "1"
    assert thing["role"] is None                                       # only router, switch, ap are roles
    assert inv.ROLE_KINDS == {"router": "router", "switch": "switch", "ap": "ap"}


def test_lldp_neighbours_are_records_with_a_role_from_their_capabilities():
    recs = {r["mac"]: r for r in inv.records(obs(source="snmp", lldp=[
        {"chassis_mac": MAC_A, "mgmt_ip": "192.168.1.9", "sys_name": "bridge-1", "caps": ["bridge"]},
        {"chassis_mac": MAC_B, "chassis_ip": "192.168.1.10", "caps": ["router"]},
        {"chassis_mac": "02:aa:bb:cc:dd:03", "caps": ["ap", "bridge"]},
        {"chassis_mac": "02:aa:bb:cc:dd:04", "caps": ["router", "bridge"]},
        {"chassis_mac": "02:aa:bb:cc:dd:05", "caps": ["telephone"]}]))}
    assert recs[MAC_A]["role"] == "switch" and recs[MAC_A]["ip"] == "192.168.1.9" and recs[MAC_A]["name"] == "bridge-1"
    assert recs[MAC_B]["role"] == "router" and recs[MAC_B]["ip"] == "192.168.1.10"
    assert recs["02:aa:bb:cc:dd:03"]["role"] == "ap" and recs["02:aa:bb:cc:dd:04"]["role"] == "switch"
    assert recs["02:aa:bb:cc:dd:05"]["role"] is None
    assert all(r["sources"] == ["lldp"] for r in recs.values())


def test_forwarding_table_rows_only_from_snmp_agents():
    ev = {"source": "snmp", "agent": {"mac": MAC_B}, "fdb": [{"mac": MAC_A, "port": "lan2"}]}
    (rec,) = inv.records(obs(**ev))
    assert rec["sources"] == ["fdb"] and rec["port"] == "lan2" and rec["parent_mac"] == MAC_B
    assert rec["medium"] == "wired" and rec["agent"] == MAC_B and rec["ip"] is None
    assert inv.records(obs(source="controller", agent={"mac": MAC_B}, fdb=[{"mac": MAC_A, "port": "1"}])) == []


def test_richest_source_first_and_sources_are_merged():
    topo = {"a": {"ev": {"source": "controller", "clients": [{"mac": MAC_A, "ip": "192.168.60.5", "name": "Sensor"}]}},
            "b": {"ev": {"source": "snmp", "agent": {"mac": MAC_B}, "fdb": [{"mac": MAC_A, "port": "3"}],
                         "lldp": [{"chassis_mac": MAC_A, "mgmt_ip": "192.168.99.9", "sys_name": "other"}]}}}
    (rec,) = inv.records(topo)
    assert rec["sources"] == ["controller", "lldp", "fdb"]
    assert rec["ip"] == "192.168.60.5" and rec["name"] == "Sensor" and rec["port"] == "3"


def test_group_null_and_own_addresses_are_not_devices():
    own = "02:aa:bb:cc:dd:77"
    rows = [{"mac": m, "ip": "192.168.60.5"} for m in
            ("01:00:5e:00:00:fb", "33:33:00:00:00:01", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00", "", None, "junk", own.upper(), MAC_A)]
    recs = inv.records(obs(clients=rows), own_macs=[own])
    assert [r["mac"] for r in recs] == [MAC_A]


def test_address_is_validated():
    def ip(value):
        return inv.records(obs(clients=[{"mac": MAC_A, "ip": value}]))[0]["ip"]
    assert ip("192.168.60.5") == "192.168.60.5" and ip(" 192.168.60.5 ") == "192.168.60.5"
    for bad in ("300.1.1.1", "0.0.0.0", "224.0.0.1", "127.0.0.1", "fe80::1", "fd00::5", "junk", "", None, 5, "1.2.3"):
        assert ip(bad) is None, bad


def test_vlan_medium_and_text_are_cleaned():
    (rec,) = inv.records(obs(clients=[{"mac": MAC_A, "vlan": True, "medium": "carrier-pigeon", "name": "x" * 200, "port": "p" * 99}]))
    assert rec["vlan"] is None and rec["medium"] is None and len(rec["name"]) == 64 and len(rec["port"]) == 24
    for v, ok in ((0, None), (4095, None), (1, 1), (4094, 4094), ("60", None)):
        assert inv.records(obs(clients=[{"mac": MAC_A, "vlan": v}]))[0]["vlan"] == ok, v


def test_record_count_is_limited():
    rows = [{"mac": "02:00:00:00:%02x:%02x" % (i // 256, i % 256)} for i in range(inv.MAX_RECORDS + 50)]
    recs = inv.records(obs(clients=rows))
    assert len(recs) == inv.MAX_RECORDS
    again = inv.records(obs(clients=rows[:5] + rows))                    # a known address is still merged past the limit
    assert len(again) == inv.MAX_RECORDS


def test_broken_observations_are_skipped():
    assert inv.records({}) == [] and inv.records(None) == []
    assert inv.records({"x": None, "y": {}, "z": {"ev": None}, "w": {"ev": {"clients": None, "devices": None}}}) == []


def test_host_of_looks_like_a_scan_host_marked_adapter():
    (rec,) = inv.records(obs(clients=[{"mac": MAC_A, "name": "Sensor", "vlan": 60, "port": "7"}]))
    host = inv.host_of(rec)
    assert host["ip"] == "" and host["mac"] == MAC_A and host["hostnames"] == ["Sensor"] and host["sources"] == ["adapter"]
    assert host["ports"] == [] and host["is_self"] is False and host["adapter"]["vlan"] == 60 and host["adapter"]["port"] == "7"
    assert inv.is_adapter_only(host) and not inv.is_adapter_only({"sources": ["tcp", "adapter"]}) and not inv.is_adapter_only({})
    assert inv.host_of({"mac": MAC_A, "sources": ["lldp"]})["hostnames"] == []


# ---------------------------------------------------------------------- booking into the inventory
@pytest.fixture()
def bare_core():
    st = Store(":memory:")
    return core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), st, None)


def _row(c, device_id):
    row = c.store.one("SELECT * FROM devices WHERE id=?", (device_id,))
    return None if row is None else dict(row, data=json.loads(row["data"]))


def _recs(*clients, **ev):
    return inv.records(obs(clients=list(clients), **ev))


def test_new_device_is_created_under_its_hardware_address(bare_core):
    c = bare_core
    n = c.ingest_adapter(_recs({"mac": MAC_A, "ip": "192.168.60.5", "name": "Sensor"}, {"mac": MAC_B}))
    assert n == 2
    a, b = _row(c, "d-02aabbccdd01"), _row(c, "d-02aabbccdd02")
    assert a["ip"] == "192.168.60.5" and a["data"]["sources"] == ["adapter"] and a["data"]["adapter"]["sources"] == ["controller"]
    assert b["ip"] == "" and b["data"]["sources"] == ["adapter"] and b["mac"] == MAC_B
    views = {d["id"]: d for d in c.devices()}
    assert views["d-02aabbccdd02"]["adapter_only"] and views["d-02aabbccdd02"]["ip"] == ""
    assert views["d-02aabbccdd01"]["title"]
    assert c.ingest_adapter(_recs({"mac": MAC_A, "ip": "192.168.60.5", "name": "Sensor"}, {"mac": MAC_B})) == 0   # not new again


def test_controller_roles_give_infrastructure_its_kind(bare_core):
    c = bare_core
    recs = inv.records(obs(devices=[{"mac": MAC_A, "ip": "192.168.1.2", "name": "Core", "role": "switch"},
                                    {"mac": MAC_B, "ip": "192.168.1.3", "name": "Upstairs access point", "role": "ap"}]))
    c.ingest_adapter(recs)
    assert _row(c, "d-02aabbccdd01")["kind"] == "switch" and _row(c, "d-02aabbccdd02")["kind"] == "ap"


def test_scanned_device_keeps_its_scan_data_and_only_gains_the_adapter_and_a_missing_address(bare_core):
    c = bare_core
    scanned = {"ip": "192.168.50.7", "mac": MAC_A, "hostnames": ["x.lan"], "sources": ["tcp", "arp"],
               "ports": [{"port": 22, "proto": "tcp", "service": "ssh", "banner": None}], "mdns": [], "ssdp": []}
    c.ingest({"hosts": [scanned, {"ip": "", "mac": MAC_B, "hostnames": [], "sources": ["arp"], "ports": [], "mdns": [], "ssdp": []}]}, None)
    n = c.ingest_adapter(_recs({"mac": MAC_A, "ip": "192.168.99.99", "name": "Sensor", "vlan": 5},
                               {"mac": MAC_B, "ip": "192.168.50.8"}))
    assert n == 0
    a = _row(c, "d-02aabbccdd01")
    assert a["ip"] == "192.168.50.7"                                       # a known address is never replaced
    assert a["data"]["sources"] == ["tcp", "arp"] and [p["port"] for p in a["data"]["ports"]] == [22]
    assert a["data"]["adapter"]["vlan"] == 5 and a["data"]["hostnames"] == ["x.lan", "Sensor"]
    b = _row(c, "d-02aabbccdd02")
    assert b["ip"] == "192.168.50.8" and b["data"]["ip"] == "192.168.50.8" and b["data"]["sources"] == ["arp"]
    assert not {d["id"]: d for d in c.devices()}["d-02aabbccdd01"]["adapter_only"]


def test_a_later_scan_keeps_the_adapter_facts(bare_core):
    c = bare_core
    c.ingest_adapter(_recs({"mac": MAC_A, "ip": "192.168.60.5", "vlan": 60}))
    before = _row(c, "d-02aabbccdd01")["data"]["adapter"]
    assert before["vlan"] == 60
    c.ingest({"hosts": [{"ip": "192.168.60.5", "mac": MAC_A, "hostnames": [], "sources": ["tcp"], "ports": [], "mdns": [], "ssdp": []}]}, None)
    after = _row(c, "d-02aabbccdd01")
    assert after["data"]["adapter"] == before and after["data"]["sources"] == ["tcp"]
    assert not {d["id"]: d for d in c.devices()}["d-02aabbccdd01"]["adapter_only"]


def test_setting_off_books_nothing(bare_core):
    c = bare_core
    c.topo_obs = obs(clients=[{"mac": MAC_A, "ip": "192.168.60.5"}])
    c.store.set_setting("adapter_inventory", False)
    assert c.book_adapter_devices(force=True) == 0 and c.store.devices() == []
    assert c.zone_list()["adapter_inventory"] is False
    c.store.set_setting("adapter_inventory", True)
    assert c.book_adapter_devices(force=True) == 1 and len(c.store.devices()) == 1


def test_booking_is_throttled_and_skips_this_host(bare_core):
    c = bare_core
    c.ingest({"hosts": [{"ip": "192.168.50.2", "mac": MAC_B, "hostnames": [], "sources": ["tcp"], "ports": [], "mdns": [],
                         "ssdp": [], "is_self": True}]}, None)
    c.topo_obs = obs(clients=[{"mac": MAC_A, "ip": "192.168.60.5"}, {"mac": MAC_B, "ip": "192.168.50.2"}])
    t = time.time()
    assert c.book_adapter_devices(now=t) == 1
    c.topo_obs = obs(clients=[{"mac": MAC_A}, {"mac": "02:aa:bb:cc:dd:09"}])
    assert c.book_adapter_devices(now=t + 5) == 0                           # within 30 s: nothing
    assert c.book_adapter_devices(now=t + 31) == 1
    assert "adapter" not in _row(c, "d-02aabbccdd02")["data"]                # this host is not an adapter find


def test_first_booking_is_remembered(bare_core):
    c = bare_core
    assert c.store.get_setting("adapter_seeded", False) is False
    c.ingest_adapter(_recs({"mac": MAC_A, "ip": "192.168.60.5"}))
    assert c.store.get_setting("adapter_seeded") is True


def test_first_booking_marks_devices_as_known_so_no_new_device_event(bare_core):
    c = bare_core
    w = c.reports.watch
    w.seeded = True
    c.ingest_adapter(_recs({"mac": MAC_A, "ip": "192.168.60.5"}, {"mac": MAC_B}))
    assert {"d-02aabbccdd01", "d-02aabbccdd02"} <= w.known
    n0 = len(c.store.query("SELECT * FROM events WHERE kind='dev_new'"))
    w.observe(time.time())
    assert len(c.store.query("SELECT * FROM events WHERE kind='dev_new'")) == n0
    # a later device is new as usual
    c.ingest_adapter(_recs({"mac": "02:aa:bb:cc:dd:03", "ip": "192.168.60.6"}))
    assert "d-02aabbccdd03" not in w.known


# ---------------------------------------------------------------------- demo: identity of address-only devices
@pytest.fixture()
def demo_core():
    st = Store(":memory:")
    c = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=True), st, None)
    c.demo = demo.Demo()
    c.demo.seed(c)
    return c


def test_demo_second_network_card_is_folded_into_the_studio_desktop(demo_core):
    views = {d["ip"]: d for d in demo_core.devices() if d["ip"]}
    studio = views["192.168.50.21"]
    assert [a["id"] for a in studio["aliases"]] == ["d-00005e00539a"]
    alias = studio["aliases"][0]
    assert alias["mac"] == "00:00:5e:00:53:9a" and alias["ip"] == "" and alias["reasons"] == ["same_block_port_time"]
    assert 0.6 <= alias["confidence"] < 1 and alias["manual"] is False
    second = next(d for d in demo_core.devices() if d["id"] == "d-00005e00539a")
    assert second["merged_into"]["id"] == studio["id"] and second["adapter_only"]


def test_demo_random_address_is_never_folded_and_other_adapter_devices_stay_apart(demo_core):
    devices = {d["id"]: d for d in demo_core.devices()}
    phone = devices["d-daa1197e41c3"]
    assert phone["ip"] == "192.168.80.31" and phone["merged_into"] is None and phone["aliases"] == [] and phone["adapter_only"]
    assert phone["origin"] == "random"
    merged = {d["id"] for d in devices.values() if d["merged_into"]}
    assert merged.isdisjoint({"d-daa1197e41c3", "d-00005e006010", "d-00005e007010", "d-00005e009011"})
    # an address the router's table learned and nothing else known stays a device of its own
    assert devices["d-00005e009011"]["ip"] == "" and devices["d-00005e009011"]["merged_into"] is None


def test_demo_adapter_devices_book_into_the_journal_as_known(demo_core):
    assert demo_core.store.get_setting("adapter_seeded") is True
    assert sum(1 for d in demo_core.devices() if d["adapter_only"]) == 9


# ---------------------------------------------------------------------- identity: block + port + time, announced links
def dev(i, mac, ip="192.168.1.5", first=1000.0, port=None, **data):
    return {"id": i, "ip": ip, "mac": mac, "first_seen": first, "last_scan": 1, "link_port": port, "data": dict(data, mac=mac)}


def test_same_block_port_and_time_folds_an_address_only_device_in():
    a = dev("d-a", "00:00:5e:00:53:44", port="d-sw#7")
    b = dev("d-b", "00:00:5e:00:53:9a", ip="", first=1300.0, port="d-sw#7")
    g = identity.groups([a, b])
    assert list(g["members"]) == ["d-b"] and g["members"]["d-b"]["primary"] == "d-a"
    assert g["members"]["d-b"]["reasons"] == ["same_block_port_time"] and g["members"]["d-b"]["confidence"] == 0.65


@pytest.mark.parametrize("change", [
    {"b_first": 1000.0 + 11 * 60},                       # seen too far apart
    {"b_port": "d-sw#8"},                                # another port
    {"b_port": None},                                    # no port known
    {"b_ip": "192.168.1.6"},                             # both have an address: not this rule
    {"b_mac": "da:00:5e:00:53:9a"},                      # random hardware address
    {"b_mac": "00:00:5f:00:53:9a"},                      # another block
    {"a_ip": ""},                                        # both without an address
])
def test_block_port_time_needs_every_part(change):
    a = dev("d-a", change.get("a_mac", "00:00:5e:00:53:44"), ip=change.get("a_ip", "192.168.1.5"), port="d-sw#7")
    b = dev("d-b", change.get("b_mac", "00:00:5e:00:53:9a"), ip=change.get("b_ip", ""), first=change.get("b_first", 1200.0),
            port=change["b_port"] if "b_port" in change else "d-sw#7")
    g = identity.groups([a, b])
    assert g["members"] == {} and not any("same_block_port_time" in s["reasons"] for s in g["suggestions"])


def test_adjacent_addresses_use_the_old_rule_not_the_new_one():
    a = dev("d-a", "00:00:5e:00:53:44", port="d-sw#7")
    b = dev("d-b", "00:00:5e:00:53:45", ip="", port="d-sw#7")
    g = identity.groups([a, b])
    reasons = g["members"]["d-b" if "d-b" in g["members"] else "d-a"]["reasons"]
    assert "adjacent_mac" in reasons and "same_block_port_time" not in reasons


def test_announced_lldp_chassis_folds_two_devices_into_one():
    a, b = dev("d-a", "02:00:00:00:00:01"), dev("d-b", "02:00:00:00:00:99", ip="192.168.1.9")
    links = [{"macs": ["02:00:00:00:00:01", "02:00:00:00:00:99"], "why": "same_lldp_chassis", "conf": 0.85}]
    assert identity.groups([a, b])["members"] == {}
    g = identity.groups([a, b], links=links)
    assert len(g["members"]) == 1 and list(g["members"].values())[0]["reasons"] == ["same_lldp_chassis"]
    assert list(g["members"].values())[0]["confidence"] == 0.85 and not list(g["members"].values())[0]["manual"]


def test_announced_hub_device_and_address_links_fold_too():
    a, b, c = dev("d-a", "02:00:00:00:00:01"), dev("d-b", "02:00:00:00:00:02", ip="192.168.1.6"), dev("d-c", None, ip="192.168.1.7")
    g = identity.groups([a, b], links=[{"macs": ["02:00:00:00:00:01", "02:00:00:00:00:02"], "why": "same_hub_device", "conf": 0.8}])
    assert list(g["members"].values())[0]["reasons"] == ["same_hub_device"]
    g = identity.groups([a, c], links=[{"macs": ["02:00:00:00:00:01"], "ips": ["192.168.1.7"], "why": "same_lldp_chassis", "conf": 0.85}])
    assert len(g["members"]) == 1


def test_weak_or_oversized_or_lonely_links_do_nothing():
    a, b = dev("d-a", "02:00:00:00:00:01"), dev("d-b", "02:00:00:00:00:02", ip="192.168.1.6")
    macs = ["02:00:00:00:00:01", "02:00:00:00:00:02"]
    assert identity.groups([a, b], links=[{"macs": macs, "why": "same_hub_device", "conf": 0.4}])["members"] == {}
    assert identity.groups([a, b], links=[{"macs": macs[:1], "why": "same_hub_device", "conf": 0.9}])["members"] == {}
    crowd = [dev("d-%d" % i, "02:00:00:00:01:%02x" % i, ip="192.168.1.%d" % (10 + i)) for i in range(9)]
    links = [{"macs": [d["mac"] for d in crowd], "why": "same_hub_device", "conf": 0.9}]
    assert identity.groups(crowd, links=links)["members"] == {}


def test_user_split_beats_an_announced_link_and_join_beats_nothing():
    a, b = dev("d-a", "02:00:00:00:00:01"), dev("d-b", "02:00:00:00:00:99", ip="192.168.1.9")
    links = [{"macs": ["02:00:00:00:00:01", "02:00:00:00:00:99"], "why": "same_lldp_chassis", "conf": 0.85}]
    assert identity.groups([a, b], splits=[["d-a", "d-b"]], links=links)["members"] == {}
    g = identity.groups([a, b], joins=[["d-a", "d-b"]])
    assert list(g["members"].values())[0]["manual"] is True


def test_core_collects_links_from_lldp_and_hub_registries(bare_core):
    c = bare_core
    c.topo_obs = obs(source="snmp", lldp=[{"chassis_mac": MAC_A, "port_mac": MAC_B, "sys_name": "x", "caps": ["bridge"]},
                                          {"chassis_mac": MAC_A, "caps": []}])
    c.hub = {1: {"snapshot": {"devices": [{"macs": [MAC_A, "02:aa:bb:cc:dd:03"]}, {"macs": [MAC_B]}]}}}
    links = c._identity_links()
    assert {"macs": [MAC_A, MAC_B], "ips": [], "why": "same_lldp_chassis", "conf": 0.85} in links
    assert {"macs": [MAC_A, "02:aa:bb:cc:dd:03"], "ips": [], "why": "same_hub_device", "conf": 0.8} in links
    assert len(links) == 2
