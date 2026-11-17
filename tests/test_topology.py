"""Physical topology: resolution of links from four sources, the core's manual editor and path booking, the
network view with cabling, and the API. Everything is synthetic (demo network, no I/O)."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import demo, mapmodel, topology
from lanloom.main import App, create_app

HDR = {"X-Lanloom": "1"}
NOW = 1_000_000.0


# ---------------------------------------------------------------------------------------------- helpers
def mac(n: int) -> str:
    return "02:00:00:00:00:%02x" % n


def vmac(n: int) -> str:
    return "52:54:00:00:00:%02x" % n


def dev(did, m=None, kind="unknown", gw=False, ip=None):
    return {"id": did, "ip": ip or "192.168.50.%d" % (sum(map(ord, did)) % 200 + 20), "mac": m, "kind": kind,
            "title": did, "hostnames": [], "is_gateway": gw}


def base():
    return [dev("gw", mac(1), "router", gw=True, ip="192.168.50.1"), dev("sw", mac(2), "switch", ip="192.168.50.2"),
            dev("h", mac(10), "server", ip="192.168.50.10")]


def ctl(devices=None, clients=None, t=NOW):
    return {"type": "topology", "source": "controller", "t": t, "devices": devices or [], "clients": clients or []}


def cdev(m, role="switch", up=None, name=None, ip=None):
    return {"mac": m, "ip": ip, "name": name, "role": role, "model": None, "uplink": up}


def up(m, port=None, own=None, medium="wired"):
    return {"mac": m, "port": port, "own_port": own, "medium": medium}


def cl(m, parent, port=None, medium="wired", band=None, ssid=None):
    return {"mac": m, "ip": None, "name": None, "medium": medium, "parent_mac": parent, "port": port, "band": band,
            "ssid": ssid}


def nb(m, local, port_id=None, caps=("bridge",), name=None):
    return {"local_port": local, "local_port_num": None, "chassis_mac": m, "chassis_ip": None, "chassis_text": None,
            "port_id": port_id, "port_mac": None, "port_desc": None, "sys_name": name, "caps": list(caps), "mgmt_ip": None}


def snmp(agent_mac, lldp=None, fdb=None, t=NOW, ip=None):
    return {"type": "topology", "source": "snmp", "host": ip, "t": t, "agent": {"ip": ip, "mac": agent_mac, "name": "agent"},
            "lldp": lldp or [], "fdb": [{"port": p, "port_num": None, "mac": m, "vlan": 1} for p, m in (fdb or [])]}


def obs(*events):
    return {"int:%d" % i: {"integ": i, "ev": ev} for i, ev in enumerate(events, 1)}


def run(devices, observations=None, manual=None, now=NOW):
    return topology.resolve(devices, manual or {}, observations or {}, now)


# ---------------------------------------------------------------------------------------------- 1 precedence
def _four_sources():
    devices = base() + [dev("swb", mac(3), "switch"), dev("x", mac(20), "switch")]
    o = obs(ctl([cdev(mac(20), up=up(mac(2), "2"))]),
            snmp(mac(1), lldp=[nb(mac(20), "lan1", "9")], ip="192.168.50.1"),
            snmp(mac(2), fdb=[("2", mac(20))], ip="192.168.50.2"))
    return devices, o


def test_manual_beats_everything_and_lists_conflicts():
    devices, o = _four_sources()
    r = run(devices, o, {"x": {"parent": "swb", "port": "1", "medium": "wired"}})
    link = r["links"]["x"]
    assert (link["source"], link["parent"], link["port"], link["age_s"]) == ("manual", "swb", "1", None)
    assert [c["source"] for c in link["conflicts"]] == ["controller", "lldp", "fdb"]
    ctl_c = link["conflicts"][0]
    assert (ctl_c["parent"], ctl_c["port"]) == ("sw", "2")
    assert link["also"] == []


def test_controller_wins_without_manual_and_agreeing_source_is_listed():
    devices, o = _four_sources()
    link = run(devices, o)["links"]["x"]
    assert (link["source"], link["parent"], link["port"]) == ("controller", "sw", "2")
    assert link["also"] == ["fdb"]
    assert [c["source"] for c in link["conflicts"]] == ["lldp"]
    assert link["conflicts"][0]["parent"] == "gw"


def test_lldp_wins_over_fdb_and_fdb_is_last_resort():
    devices, o = _four_sources()
    o.pop("int:1")
    link = run(devices, o)["links"]["x"]
    assert link["source"] == "lldp" and link["parent"] == "gw"
    assert [c["source"] for c in link["conflicts"]] == ["fdb"]
    o.pop("int:2")
    link = run(devices, o)["links"]["x"]
    assert (link["source"], link["parent"], link["port"]) == ("fdb", "sw", "2")
    assert link["conflicts"] == [] and link["also"] == []


def test_agreeing_lower_source_fills_missing_port_and_own_port():
    devices = base() + [dev("x", mac(20), "switch")]
    o = obs(ctl([cdev(mac(20), up=up(mac(1)))]), snmp(mac(1), lldp=[nb(mac(20), "lan1", "9")], ip="192.168.50.1"))
    link = run(devices, o)["links"]["x"]
    assert link["source"] == "controller" and link["also"] == ["lldp"]
    assert (link["port"], link["own_port"]) == ("lan1", "9")


def test_empty_input_resolves_to_nothing():
    r = run(base())
    assert r["links"] == {} and r["children"] == {} and r["trunks"] == [] and r["nodes"] == {} and r["unknown"] == []


# ---------------------------------------------------------------------------------------------- 2 specificity
def test_port_with_one_address_beats_port_with_many():
    devices = base() + [dev("swb", mac(3), "switch")]
    crowd = [("9", mac(10))] + [("9", mac(100 + i)) for i in range(5)]
    o = obs(snmp(mac(2), fdb=[("1", mac(10))], ip="192.168.50.2"), snmp(mac(3), fdb=crowd, ip="192.168.50.3"))
    link = run(devices, o)["links"]["h"]
    assert (link["parent"], link["port"]) == ("sw", "1")


def test_weak_uplink_inference_alone_gives_agent_to_gateway_with_own_port():
    devices = base()
    o = obs(snmp(mac(2), fdb=[("1", mac(1)), ("1", mac(10)), ("4", mac(10))], ip="192.168.50.2"))
    r = run(devices, o)
    link = r["links"]["sw"]
    assert (link["parent"], link["port"], link["own_port"], link["medium"], link["source"]) == ("gw", None, "1", "wired", "fdb")
    assert r["links"]["h"]["port"] == "4"       # nothing behind the gateway port is assigned


def test_weak_inference_loses_to_a_real_port():
    devices = base() + [dev("swb", mac(3), "switch")]
    o = obs(snmp(mac(2), fdb=[("1", mac(1))], ip="192.168.50.2"), snmp(mac(3), fdb=[("3", mac(2))], ip="192.168.50.3"))
    link = run(devices, o)["links"]["sw"]
    assert (link["parent"], link["port"]) == ("swb", "3")


# ---------------------------------------------------------------------------------------------- 3 shared ports
def test_clients_behind_an_access_point_on_one_controller_port():
    devices = base() + [dev("ap", mac(5), "ap"), dev("c1", mac(30)), dev("c2", mac(31))]
    o = obs(ctl(clients=[cl(mac(5), mac(2), "5"), cl(mac(30), mac(2), "5"), cl(mac(31), mac(2), "5")]))
    r = run(devices, o)
    assert (r["links"]["ap"]["parent"], r["links"]["ap"]["port"]) == ("sw", "5")
    for c in ("c1", "c2"):
        link = r["links"][c]
        assert (link["parent"], link["port"], link["own_port"], link["medium"]) == ("ap", None, None, "radio")
        assert link["reason"] == "topo.reason.behind_ap" and link["vars"] == {"via": "ap", "port": "5"}
    assert sorted(r["children"]["ap"]) == ["c1", "c2"]


def test_virtual_guests_share_the_port_of_their_host():
    devices = base() + [dev("g1", vmac(1)), dev("g2", vmac(2))]
    o = obs(ctl(clients=[cl(mac(10), mac(2), "6"), cl(vmac(1), mac(2), "6"), cl(vmac(2), mac(2), "6")]))
    r = run(devices, o)
    assert (r["links"]["h"]["parent"], r["links"]["h"]["port"], r["links"]["h"]["reason"]) == ("sw", "6", None)
    for g in ("g1", "g2"):
        link = r["links"][g]
        assert (link["parent"], link["medium"], link["port"]) == ("h", "virtual", None)
        assert link["reason"] == "topo.reason.guest" and link["vars"]["via"] == "h"


def test_forwarding_database_trunk_assigns_nothing():
    n = topology.TRUNK_MACS
    devices = base() + [dev("m%d" % i, mac(100 + i)) for i in range(n)]
    o = obs(snmp(mac(2), fdb=[("8", mac(100 + i)) for i in range(n)], ip="192.168.50.2"))
    r = run(devices, o)
    assert not any(k.startswith("m") for k in r["links"])
    assert r["trunks"] == [{"parent": "sw", "port": "8", "macs": n}]


def test_trunk_with_a_switch_on_it_is_not_a_trunk_but_a_chain():
    n = topology.TRUNK_MACS
    devices = base() + [dev("swb", mac(3), "switch")] + [dev("m%d" % i, mac(100 + i)) for i in range(n)]
    rows = [("8", mac(3))] + [("8", mac(100 + i)) for i in range(n)]
    r = run(devices, obs(snmp(mac(2), fdb=rows, ip="192.168.50.2")))
    assert r["links"]["swb"]["parent"] == "sw"
    assert r["links"]["m0"]["parent"] == "swb" and r["links"]["m0"]["reason"] == "topo.reason.behind"


def test_small_shared_port_is_marked_shared_with_count():
    devices = base() + [dev("a", mac(30)), dev("b", mac(31)), dev("c", mac(32))]
    r = run(devices, obs(snmp(mac(2), fdb=[("4", mac(30)), ("4", mac(31)), ("4", mac(32))], ip="192.168.50.2")))
    assert r["trunks"] == []
    for k in "abc":
        link = r["links"][k]
        assert (link["parent"], link["port"]) == ("sw", "4")
        assert link["reason"] == "topo.reason.shared" and link["vars"] == {"n": 2, "port": "4"}


# ---------------------------------------------------------------------------------------------- 4 LLDP orientation
def test_router_sees_switch_switch_is_the_child_with_both_ports():
    o = obs(snmp(mac(1), lldp=[nb(mac(2), "lan2", "1")], ip="192.168.50.1"))
    link = run(base(), o)["links"]["sw"]
    assert (link["parent"], link["port"], link["own_port"], link["source"], link["medium"]) == ("gw", "lan2", "1", "lldp", "wired")


def test_switch_sees_access_point_ap_is_the_child():
    devices = base() + [dev("ap", mac(5), "ap")]
    o = obs(snmp(mac(2), lldp=[nb(mac(5), "3", "eth0", caps=("ap",))], ip="192.168.50.2"))
    link = run(devices, o)["links"]["ap"]
    assert (link["parent"], link["port"], link["own_port"]) == ("sw", "3", "eth0")


@pytest.mark.parametrize("swap", [False, True])
def test_both_sides_reported_make_one_link_with_both_ports(swap):
    router_side = snmp(mac(1), lldp=[nb(mac(2), "lan2")], ip="192.168.50.1")        # remote port unknown
    switch_side = snmp(mac(2), lldp=[nb(mac(1), "1", caps=("router",))], ip="192.168.50.2")
    o = obs(switch_side, router_side) if swap else obs(router_side, switch_side)
    r = run(base(), o)
    assert list(r["links"]) == ["sw"]
    link = r["links"]["sw"]
    assert (link["parent"], link["port"], link["own_port"]) == ("gw", "lan2", "1")


def test_orientation_follows_hop_distance_not_the_reporting_agent():
    devices = base() + [dev("swb", mac(3), "switch")]
    o = obs(ctl([cdev(mac(2), up=up(mac(1)))]), snmp(mac(3), lldp=[nb(mac(2), "1", "7")], ip="192.168.50.3"))
    link = run(devices, o)["links"]["swb"]
    assert (link["parent"], link["port"], link["own_port"]) == ("sw", "7", "1")


def test_equal_rank_without_gateway_makes_the_reporting_agent_the_parent():
    devices = [dev("s1", mac(2), "switch"), dev("s2", mac(3), "switch")]
    link = run(devices, obs(snmp(mac(2), lldp=[nb(mac(3), "1", "2")], ip="192.168.50.2")))["links"]["s2"]
    assert link["parent"] == "s1"


def test_unknown_lldp_switch_becomes_a_virtual_node_and_unknown_plain_neighbour_is_ignored():
    o = obs(snmp(mac(2), lldp=[nb(mac(77), "5", "1", caps=("bridge",), name="spare"), nb(mac(78), "6", "1", caps=())],
                 ip="192.168.50.2"))
    r = run(base(), o)
    vid = "lldp:" + mac(77).replace(":", "")
    assert r["links"][vid]["parent"] == "sw"
    assert vid in r["nodes"] and r["nodes"][vid]["kind"] == "switch" and r["nodes"][vid]["title"] == "spare"
    assert not any(mac(78).replace(":", "") in k for k in list(r["links"]) + list(r["nodes"]))


# ---------------------------------------------------------------------------------------------- 5 forwarding database
def test_own_address_in_forwarding_database_is_skipped():
    o = obs(snmp(mac(2), fdb=[("1", mac(2)), ("2", mac(10))], ip="192.168.50.2"))
    r = run(base(), o)
    assert "sw" not in r["links"]
    assert (r["links"]["h"]["parent"], r["links"]["h"]["port"]) == ("sw", "2")


def test_gateway_port_gives_no_neighbours_behind_it():
    o = obs(snmp(mac(2), fdb=[("1", mac(1)), ("1", mac(10))], ip="192.168.50.2"))
    r = run(base(), o)
    assert "h" not in r["links"] and r["links"]["sw"]["own_port"] == "1"


def test_lldp_neighbour_port_is_skipped_in_the_forwarding_database():
    devices = base() + [dev("swb", mac(3), "switch")]
    o = obs(snmp(mac(2), lldp=[nb(mac(3), "3", "1")], fdb=[("3", mac(3)), ("3", mac(10))], ip="192.168.50.2"))
    r = run(devices, o)
    assert r["links"]["swb"]["source"] == "lldp"
    assert "h" not in r["links"]


def test_unknown_addresses_are_counted_per_port():
    o = obs(snmp(mac(2), fdb=[("5", mac(10)), ("5", mac(200)), ("5", mac(201))], ip="192.168.50.2"))
    r = run(base(), o)
    assert r["unknown"] == [{"parent": "sw", "port": "5", "count": 2}]
    assert r["links"]["h"]["port"] == "5"


def test_agent_outside_the_inventory_becomes_a_snmp_node():
    o = {"int:7": {"integ": 7, "ev": snmp(mac(90), fdb=[("2", mac(10))], ip="192.168.50.90")}}
    r = run(base(), o)
    assert r["links"]["h"]["parent"] == "snmp:7"
    assert r["nodes"]["snmp:7"]["kind"] == "switch" and r["nodes"]["snmp:7"]["ip"] == "192.168.50.90"
    assert "snmp:7" in r["infra"]


# ---------------------------------------------------------------------------------------------- 6 controller
def test_router_role_maps_to_gateway_and_its_uplink_is_ignored():
    o = obs(ctl([cdev(mac(60), role="router", up=up(mac(2), "9"), ip="192.168.50.60")],
                [cl(mac(10), mac(60), "4")]))
    r = run(base(), o)
    assert "gw" not in r["links"]
    assert (r["links"]["h"]["parent"], r["links"]["h"]["port"]) == ("gw", "4")


def test_infrastructure_outside_the_inventory_gets_a_ctl_node():
    spare = mac(77)
    o = obs(ctl([cdev(spare, up=up(mac(1), "3", "1"), name="Spare switch")], [cl(mac(10), spare, "2")]))
    r = run(base(), o)
    vid = "ctl:" + spare.replace(":", "")
    assert r["nodes"][vid]["kind"] == "switch" and r["nodes"][vid]["title"] == "Spare switch"
    assert (r["links"][vid]["parent"], r["links"][vid]["port"], r["links"][vid]["own_port"]) == ("gw", "3", "1")
    assert (r["links"]["h"]["parent"], r["links"]["h"]["port"]) == (vid, "2")
    assert vid in r["infra"]


def test_radio_client_keeps_band_and_ssid_and_bad_band_is_dropped():
    devices = base() + [dev("ap", mac(5), "ap"), dev("p1", mac(30)), dev("p2", mac(31))]
    o = obs(ctl([cdev(mac(5), "ap", up=up(mac(2), "2"))],
                [cl(mac(30), mac(5), medium="radio", band="5ghz", ssid="Home"),
                 cl(mac(31), mac(5), medium="radio", band="7ghz", ssid="Home")]))
    r = run(devices, o)
    assert (r["links"]["p1"]["medium"], r["links"]["p1"]["band"], r["links"]["p1"]["ssid"], r["links"]["p1"]["port"]) == ("radio", "5ghz", "Home", None)
    assert r["links"]["p2"]["band"] is None and r["links"]["p2"]["medium"] == "radio"
    assert r["links"]["ap"]["parent"] == "sw"


# ---------------------------------------------------------------------------------------------- 7 loops
def test_loop_is_skipped_and_reported():
    devices = base() + [dev("swb", mac(3), "switch")]
    o = obs(ctl([cdev(mac(3), up=up(mac(2), "1"))]), snmp(mac(1), fdb=[("2", mac(3))], ip="192.168.50.1"))
    r = run(devices, o, {"sw": {"parent": "swb", "port": "4", "medium": "wired"}})
    assert r["links"]["sw"]["parent"] == "swb" and r["links"]["sw"]["source"] == "manual"
    link = r["links"]["swb"]
    assert (link["parent"], link["source"]) == ("gw", "fdb")
    assert [(c["source"], c["parent"]) for c in link["loop"]] == [("controller", "sw")]


# ---------------------------------------------------------------------------------------------- 8 helpers
def test_stale_observation_is_ignored():
    ev = ctl(clients=[cl(mac(10), mac(2), "3")], t=NOW - topology.STALE_S - 1)
    assert run(base(), obs(ev))["links"] == {}
    ev["t"] = NOW - topology.STALE_S
    assert run(base(), obs(ev))["links"]["h"]["port"] == "3"


def test_port_key_sorts_naturally():
    ports = ["eth0", "10", "2", None, "Port 2", "Port 10", "1"]
    got = sorted(ports, key=topology.port_key)
    assert got[:3] == ["1", "2", "10"] and got[-1] is None
    assert got.index("Port 2") < got.index("Port 10") and got.index("eth0") < got.index("Port 2")


def test_mac_helpers():
    assert topology.norm_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert topology.norm_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    assert topology.norm_mac("00:00:00:00:00:00") == "" and topology.norm_mac("nonsense") == "" and topology.norm_mac(None) == ""
    assert topology.is_virtual_mac("52:54:00:aa:bb:cc") and topology.is_virtual_mac("52-54-00-AA-BB-CC")
    assert topology.is_virtual_mac("02:42:ac:11:00:02")
    assert not topology.is_virtual_mac("02:00:00:00:00:01") and not topology.is_virtual_mac("") and not topology.is_virtual_mac("zz")


def test_subtree():
    children = {"a": ["b", "c"], "b": ["d"]}
    assert topology.subtree(children, "a") == {"b", "c", "d"}
    assert topology.subtree(children, "d") == set()
    assert topology.subtree({"a": ["b"], "b": ["a"]}, "a") == {"a", "b"}


# ---------------------------------------------------------------------------------------------- core fixtures
def did(n: int, virtual=False) -> str:
    return "d-" + (demo._vmac(n) if virtual else demo._mac(n)).replace(":", "")


SWITCH, AP1, STORAGE, SELF, GATEWAY, PRINTER = did(2), did(3), did(10), did(11), did(1), did(40)


@pytest.fixture()
def core(tmp_path, monkeypatch):
    for k, v in (("LANLOOM_DATA", str(tmp_path)), ("LANLOOM_DEMO", "1"), ("LANLOOM_DEMO_SEED", "1"), ("LANLOOM_UPDATE_CHECK", "0")):
        monkeypatch.setenv(k, v)
    return App(config_mod.load()).core


def feed(core):
    now = time.time()
    core.on_event("int:4", 4, demo.controller_event(now))
    core.on_event("int:3", 3, demo.router_topology_event(now))


# ---------------------------------------------------------------------------------------------- 9 core
def test_set_manual_link_happy_path_cleans_port_and_bumps_version(core):
    v = core.structure_version
    core.set_manual_link(STORAGE, {"parent": SWITCH, "port": "  5  ", "medium": "wired"})
    assert core.structure_version > v
    row = core.manual_links()[STORAGE]
    assert (row["parent"], row["port"], row["medium"]) == (SWITCH, "5", "wired")
    link = core.topology()["links"][STORAGE]
    assert (link["source"], link["parent"], link["port"]) == ("manual", SWITCH, "5")


def test_virtual_manual_link_drops_the_port(core):
    core.set_manual_link(did(13, True), {"parent": did(15), "port": "7", "medium": "virtual"})
    row = core.manual_links()[did(13, True)]
    assert row["port"] is None and row["medium"] == "virtual"


def test_manual_link_can_be_removed_with_none(core):
    core.set_manual_link(STORAGE, {"parent": SWITCH})
    assert STORAGE in core.manual_links()
    core.set_manual_link(STORAGE, None)
    assert STORAGE not in core.manual_links() and STORAGE not in core.topology()["links"]


@pytest.mark.parametrize("link", [{"parent": ""}, {"parent": "self"}, {"parent": "d-nope"}, {"parent": "internet"}])
def test_manual_link_rejects_bad_parents(core, link):
    link = dict(link, parent=STORAGE if link["parent"] == "self" else link["parent"])
    with pytest.raises(ValueError) as e:
        core.set_manual_link(STORAGE, link)
    assert str(e.value) == "topo.err.parent"
    assert STORAGE not in core.manual_links()


def test_manual_link_rejects_a_loop_and_bad_input(core):
    feed(core)
    with pytest.raises(ValueError) as e:
        core.set_manual_link(SWITCH, {"parent": AP1})     # the access point hangs below the switch
    assert str(e.value) == "topo.err.loop"
    for bad in ({"parent": SWITCH, "medium": "plasma"}, "text", ["x"]):
        with pytest.raises(ValueError) as e:
            core.set_manual_link(STORAGE, bad)
        assert str(e.value) == "topo.err.invalid"


def test_ingest_renames_ip_ids_and_moves_manual_rows(core):
    def host(n, with_mac):
        h = demo.scan_host({"ip": "192.168.50.%d" % n, "n": n, "names": [], "ports": []})
        if not with_mac:
            h["mac"] = None
        return h
    core.ingest({"hosts": [host(77, False), host(78, False)]}, None)
    a, b = "d-ip-192-168-50-77", "d-ip-192-168-50-78"
    core.set_manual_link(a, {"parent": SWITCH, "port": "3"})
    core.set_manual_link(STORAGE, {"parent": b, "port": "4"})
    core.ingest({"hosts": [host(77, True), host(78, True)]}, None)
    new_a, new_b = did(77), did(78)
    rows = core.manual_links()
    assert a not in rows and rows[new_a]["parent"] == SWITCH and rows[new_a]["port"] == "3"
    assert rows[STORAGE]["parent"] == new_b


def test_forget_device_drops_rows_of_child_and_parent(core):
    assert PRINTER in core.manual_links()          # seeded manual entry
    assert core.forget_device(PRINTER)
    assert PRINTER not in core.manual_links()
    core.set_manual_link(STORAGE, {"parent": did(15)})
    assert core.forget_device(did(15))
    assert STORAGE not in core.manual_links()


def test_topology_event_bumps_structure_once_and_not_for_identical_event(core):
    v0 = core.structure_version
    ev = demo.controller_event(time.time())
    core.on_event("int:4", 4, ev)
    v1 = core.structure_version
    assert v1 >= v0 + 1                  # the topology changed; devices only the controller knows are booked as well
    core.on_event("int:4", 4, dict(ev, t=time.time() + 5))
    assert core.structure_version == v1
    changed = demo.controller_event(time.time())
    next(c for c in changed["clients"] if c["mac"] == demo._hmac(10))["port"] = "9"
    core.on_event("int:4", 4, changed)
    assert core.structure_version == v1 + 1


# ---------------------------------------------------------------------------------------------- 10 paths
def test_no_topology_no_path(core):
    assert core.phys_path(SELF, STORAGE) is None
    assert core.phys_path(SELF, SELF) is None


def test_path_goes_up_to_the_switch_and_down_with_signs(core):
    feed(core)
    assert core.phys_path(SELF, STORAGE) == [("p:%s:%s" % (SWITCH, SELF), -1), ("p:%s:%s" % (SWITCH, STORAGE), +1)]
    assert core.phys_path(STORAGE, SELF) == [("p:%s:%s" % (SWITCH, STORAGE), -1), ("p:%s:%s" % (SWITCH, SELF), +1)]


def test_path_to_the_router_ends_at_the_gateway(core):
    feed(core)
    path = core.phys_path(SELF, GATEWAY)
    assert path == [("p:%s:%s" % (SWITCH, SELF), -1), ("p:%s:%s" % (GATEWAY, SWITCH), -1)]


def test_book_path_adds_bytes_with_the_right_direction(core):
    feed(core)
    core.pending_bytes = {}
    core._book_path(SELF, STORAGE, 100, 7)       # this host sent 100, received 7
    # direction 0 = parent -> child: the way up carries what the host sent against the trace, so received is first
    assert core.pending_bytes["p:%s:%s" % (SWITCH, SELF)] == [7, 100]
    assert core.pending_bytes["p:%s:%s" % (SWITCH, STORAGE)] == [100, 7]


def test_flow_event_books_the_cabling_too(core):
    feed(core)
    core.pending_bytes = {}
    core.on_event("int:1", None, {"type": "bytes", "flows": [{"peer_ip": "192.168.50.10", "tx": 100, "rx": 7}]})
    assert core.pending_bytes["p:%s:%s" % (SWITCH, STORAGE)] == [100, 7]
    assert core.pending_bytes["f:%s:%s" % (SELF, STORAGE)] == [100, 7]


# ---------------------------------------------------------------------------------------------- 11 network view
def _no_physical(core):
    core.store.execute("DELETE FROM phys_manual")
    core._manual_ver += 1
    core._topo_cache = None


def test_network_view_without_topology_is_unchanged(core):
    _no_physical(core)
    view = mapmodel.build(core, "network")
    zones = {z["id"] for z in view["zones"]}
    assert "cabling" not in zones and not any(z.startswith("guests:") for z in zones)
    # devices only the controller knows (no network adapter here) sit under "other addresses"
    assert zones <= {"edge", "net:192.168.50.0/24", "net:other"} and "net:192.168.50.0/24" in zones
    kinds = {t["kind"] for t in view["traces"]}
    assert not kinds & {"cable", "radio", "virtual"} and "lan" in kinds
    assert not any(t["id"].startswith("p:") for t in view["traces"])


def test_network_view_with_cabling(core):
    feed(core)
    core.on_event("int:1", None, {"type": "bytes", "flows": [{"peer_ip": "192.168.50.10", "tx": 5, "rx": 5},
                                                              {"peer_ip": "192.168.50.99", "tx": 5, "rx": 5}]})
    view = mapmodel.build(core, "network")
    zones = [z["id"] for z in view["zones"]]
    assert "cabling" in zones and [z for z in zones if z.startswith("guests:")] == ["guests:" + did(15)]
    guest_zone = next(z for z in view["zones"] if z["id"].startswith("guests:"))
    assert guest_zone["vars"]["host"]
    kinds = {t["kind"] for t in view["traces"]}
    assert {"cable", "radio", "virtual"} <= kinds
    ids = {c["id"] for c in view["components"]}
    assert all(t["a"] in ids and t["b"] in ids for t in view["traces"])
    cable = next(t for t in view["traces"] if t["id"] == "p:%s:%s" % (SWITCH, STORAGE))
    assert cable["kind"] == "cable" and cable["phys"]["port"] == "3" and cable["phys"]["source"] == "controller"
    # the host's flow to a cabled device is carried by the cabling; the one to an unknown host is not
    assert not any(t["id"] == "f:%s:%s" % (SELF, STORAGE) for t in view["traces"])
    unknown = did(99)
    assert any(t["b"] == unknown and t["kind"] == "lan" for t in view["traces"])
    assert not any(t["b"] == unknown and t["id"].startswith("p:") for t in view["traces"])
    assert any(t["id"] == "f:%s:%s" % (SELF, unknown) for t in view["traces"])


def test_lan_trace_only_for_devices_without_a_physical_link(core):
    feed(core)
    view = mapmodel.build(core, "network")
    lan_targets = {t["b"] for t in view["traces"] if t["kind"] == "lan"}
    linked = {t["b"] for t in view["traces"] if t["id"].startswith("p:")}
    assert not lan_targets & linked - {SELF}
    assert STORAGE not in lan_targets and STORAGE in linked


def test_component_detail_physical_section(core):
    feed(core)
    phys = mapmodel.component_detail(core, STORAGE)["physical"]
    assert phys["link"]["parent"] == SWITCH and phys["link"]["parent_ref"]["id"] == SWITCH and phys["link"]["port"] == "3"
    assert STORAGE not in {c["id"] for c in phys["choices"]} and phys["manual"] is None
    printer = mapmodel.component_detail(core, PRINTER)["physical"]
    assert printer["link"]["source"] == "manual" and printer["manual"]["port"] == "9"
    assert printer["link"]["conflicts"][0]["source"] == "controller" and printer["link"]["conflicts"][0]["parent_ref"]["id"] == SWITCH
    sw = mapmodel.component_detail(core, SWITCH)["physical"]
    assert STORAGE in {c["id"] for c in sw["children"]}
    below = {c["id"] for c in sw["choices"]}
    assert AP1 not in below      # a device cannot hang below its own descendants


# ---------------------------------------------------------------------------------------------- 12 API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    for k, v in (("LANLOOM_DATA", str(tmp_path)), ("LANLOOM_DEMO", "1"), ("LANLOOM_DEMO_SEED", "1"), ("LANLOOM_UPDATE_CHECK", "0")):
        monkeypatch.setenv(k, v)
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.data_dir = tmp_path
        yield c


def _login(c):
    code = (Path(c.data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200, r.text


def test_topology_endpoint_needs_a_session(client):
    assert client.get("/api/topology").status_code in (401, 403)


def test_patch_link_and_read_it_back(client):
    _login(client)
    r = client.patch("/api/devices/" + STORAGE, json={"link": {"parent": SWITCH, "port": "5", "medium": "wired"}}, headers=HDR)
    assert r.status_code == 200, r.text
    topo = client.get("/api/topology")
    assert topo.status_code == 200
    link = topo.json()["links"][STORAGE]
    assert (link["source"], link["parent"], link["port"], link["medium"]) == ("manual", SWITCH, "5", "wired")
    assert STORAGE in topo.json()["children"][SWITCH]


def test_patch_link_with_bad_parent_is_a_400(client):
    _login(client)
    r = client.patch("/api/devices/" + STORAGE, json={"link": {"parent": "d-nope", "port": "5"}}, headers=HDR)
    assert r.status_code == 400 and r.json() == {"error": "topo.err.parent"}
    r = client.patch("/api/devices/" + STORAGE, json={"link": {"parent": SWITCH, "medium": "plasma"}}, headers=HDR)
    assert r.status_code == 400 and r.json() == {"error": "topo.err.invalid"}


def test_manual_access_point_on_a_switch_port_pulls_the_port_clients_behind_it():
    """A consumer access point set by hand on switch port 3: the clients the controller reports on port 3 hang behind it."""
    devs = [
        {"id": "gw", "ip": "192.168.50.1", "mac": "02:00:00:00:00:01", "kind": "router", "is_gateway": True},
        {"id": "sw", "ip": "192.168.50.2", "mac": "02:00:00:00:00:02", "kind": "switch"},
        {"id": "ap", "ip": "192.168.50.3", "mac": "02:00:00:00:00:03", "kind": "unknown"},
        {"id": "c1", "ip": "192.168.50.31", "mac": "02:00:00:00:00:31", "kind": "phone"},
        {"id": "c2", "ip": "192.168.50.32", "mac": "02:00:00:00:00:32", "kind": "computer"},
    ]
    ev = {"type": "topology", "source": "controller", "t": 1000.0, "devices": [], "clients": [
        {"mac": "02:00:00:00:00:%s" % n, "ip": None, "name": None, "medium": "wired", "parent_mac": "02:00:00:00:00:02",
         "port": "3", "band": None, "ssid": None} for n in ("31", "32")]}
    manual = {"ap": {"parent": "sw", "port": "3", "medium": "wired", "t": 900.0}}
    out = topology.resolve(devs, manual, {"int:1": {"integ": 1, "ev": ev}}, now=1010.0)
    links = out["links"]
    assert links["ap"]["parent"] == "sw" and links["ap"]["port"] == "3" and links["ap"]["source"] == "manual"
    for c in ("c1", "c2"):
        assert links[c]["parent"] == "ap" and links[c]["port"] is None
        assert links[c]["reason"] == "topo.reason.behind" and links[c]["vars"]["via"] == "ap"


def test_manual_host_keeps_its_virtual_guests_as_guests():
    devs = [
        {"id": "gw", "ip": "192.168.50.1", "mac": "02:00:00:00:00:01", "kind": "router", "is_gateway": True},
        {"id": "sw", "ip": "192.168.50.2", "mac": "02:00:00:00:00:02", "kind": "switch"},
        {"id": "host", "ip": "192.168.50.15", "mac": "02:00:00:00:00:15", "kind": "server"},
        {"id": "vm1", "ip": "192.168.50.16", "mac": "52:54:00:00:00:16", "kind": "server"},
    ]
    ev = {"type": "topology", "source": "controller", "t": 1000.0, "devices": [], "clients": [
        {"mac": "52:54:00:00:00:16", "ip": None, "name": None, "medium": "wired", "parent_mac": "02:00:00:00:00:02",
         "port": "6", "band": None, "ssid": None}]}
    manual = {"host": {"parent": "sw", "port": "6", "medium": "wired", "t": 900.0}}
    links = topology.resolve(devs, manual, {"int:1": {"integ": 1, "ev": ev}}, now=1010.0)["links"]
    assert links["vm1"]["parent"] == "host" and links["vm1"]["medium"] == "virtual"
    assert links["vm1"]["reason"] == "topo.reason.guest"


# ---------------------------------------------------------------------------------------------- wireless by default
def _behind_ap(medium=None, child_kind="phone", ap_kind="ap"):
    devices = base() + [dev("ap", mac(5), ap_kind), dev("c1", mac(30), child_kind), dev("c2", mac(31))]
    o = obs(ctl(clients=[cl(mac(5), mac(2), "5"), cl(mac(30), mac(2), "5", medium=medium or "wired"), cl(mac(31), mac(2), "5")]))
    return run(devices, o)


def test_clients_behind_an_access_point_default_to_wireless():
    r = _behind_ap()
    for c in ("c1", "c2"):
        assert r["links"][c]["medium"] == "radio" and r["links"][c]["reason"] == "topo.reason.behind_ap"
    assert r["links"]["ap"]["medium"] == "wired"          # the access point itself still sits on a cable


def test_a_device_behind_something_that_is_not_an_access_point_stays_unknown():
    r = _behind_ap(ap_kind="unknown")
    assert r["links"]["c1"]["medium"] != "radio" and r["links"]["c1"]["reason"] != "topo.reason.behind_ap"


def test_a_switch_next_to_an_access_point_is_not_a_wireless_client():
    devices = base() + [dev("ap", mac(5), "ap"), dev("sw2", mac(6), "switch")]
    o = obs(ctl(clients=[cl(mac(5), mac(2), "5"), cl(mac(6), mac(2), "5")]))
    assert run(devices, o)["links"]["sw2"]["medium"] != "radio"


def test_manual_entry_beats_the_wireless_default():
    devices = base() + [dev("ap", mac(5), "ap"), dev("c1", mac(30))]
    o = obs(ctl(clients=[cl(mac(5), mac(2), "5"), cl(mac(30), mac(2), "5")]))
    r = run(devices, o, manual={"c1": {"parent": "ap", "port": None, "medium": "wired", "t": NOW}})
    assert r["links"]["c1"]["medium"] == "wired" and r["links"]["c1"]["source"] == "manual"


def test_reported_link_type_beats_the_wireless_default():
    devices = base() + [dev("ap", mac(5), "ap"), dev("c1", mac(30))]
    o = obs(ctl(devices=[cdev(mac(5), "ap", up=up(mac(2), "5"))], clients=[cl(mac(30), mac(5), None, medium="wired")]))
    assert run(devices, o)["links"]["c1"]["medium"] == "wired"


# ---------------------------------------------------------------------------------------------- traffic unit setting
def test_rate_text_in_bits_and_bytes():
    from lanloom import live
    assert live.human_rate(41e6) == "41 MB/s"
    assert live.human_rate(41e6, "bits") == "328 Mbit/s"
    assert live.human_rate(125, "bits") == "1.0 kbit/s"
    assert live.human_rate(0, "bits") == "0 bit/s"
    assert live.human_rate(2.5e8, "bits") == "2.0 Gbit/s"
    assert live.human_rate(41e6, "nonsense") == "41 MB/s"


def test_rate_unit_setting_is_validated_and_shown_in_state(client):
    _login(client)
    assert client.get("/api/settings").json()["rate_unit"] == "bytes"
    assert client.put("/api/settings", json={"rate_unit": "bits"}, headers=HDR).json()["rate_unit"] == "bits"
    assert client.get("/api/state").json()["rate_unit"] == "bits"
    assert client.put("/api/settings", json={"rate_unit": "parsecs"}, headers=HDR).json()["rate_unit"] == "bits"
    assert client.put("/api/settings", json={"rate_unit": "bytes"}, headers=HDR).json()["rate_unit"] == "bytes"


# ---------------------------------------------------------------------------------------------- hidden and forgotten devices
def test_card_of_a_hidden_device_opens_and_is_gone_after_forget(core):
    feed(core)
    assert mapmodel.component_detail(core, PRINTER) is not None
    core.patch_device(PRINTER, {"hidden": True})
    d = mapmodel.component_detail(core, PRINTER)
    assert d is not None, "a hidden device must still open its card"
    assert d["device"]["hidden"] is True and d["traces"] == [] and d["component"]["id"] == PRINTER
    assert core.forget_device(PRINTER) is True
    assert mapmodel.component_detail(core, PRINTER) is None
    assert mapmodel.component_detail(core, "d-000000000000") is None


def test_forget_route_removes_the_device_for_an_operator(client):
    _login(client)
    assert client.get("/api/component/" + PRINTER).status_code == 200
    client.patch("/api/devices/" + PRINTER, json={"hidden": True}, headers=HDR)
    assert client.get("/api/component/" + PRINTER).status_code == 200
    r = client.delete("/api/devices/" + PRINTER, headers=HDR)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/component/" + PRINTER).status_code == 404
    assert PRINTER not in {d["id"] for d in client.get("/api/devices").json()["devices"]}


def test_forget_needs_operator_and_the_card_has_a_forget_button():
    from lanloom import access
    rule = access.RULES[("DELETE", "/api/devices/{device_id}")]
    assert rule.role == "operator" and rule.audit == "device.forget"
    src = (Path(__file__).resolve().parent.parent / "lanloom" / "web" / "js" / "views.js").read_text()
    assert "method: 'DELETE'" in src and "t('device.forget')" in src and "device.forget_confirm" in src
