"""Zones: every network Lanloom knows and how far it may look into each (pure functions and the core's zone list)."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import core, demo, zones
from lanloom.demo import controller_event, router_topology_event
from lanloom.demo_live import live_capabilities, live_sample
from lanloom.store import Store

LOCAL = {"cidr": "192.168.50.0/24", "iface": "eth0", "self_ip": "192.168.50.11", "gateway": "192.168.50.1"}


def net(cidr, name=None, vlan=None, **kw):
    return dict({"cidr": cidr, "name": name, "vlan": vlan}, **kw)


def adapter(*nets, key="int:1", name="Controller"):
    return {"key": key, "name": name, "state": {"networks": list(nets)}}


def by_cidr(zs):
    return {z["cidr"]: z for z in zs}


# ---------------------------------------------------------------------- build
def test_local_network_is_reachable_by_scan():
    z = zones.build([LOCAL], [])
    assert len(z) == 1 and z[0]["reach"] == "scan" and z[0]["sources"] == ["local"] and z[0]["reason"] == ""
    assert z[0]["iface"] == "eth0" and z[0]["self_ip"] == "192.168.50.11" and z[0]["gateway"] == "192.168.50.1"
    assert z[0]["size"] == 256


def test_adapter_network_behind_a_router_is_adapter_only_until_consent():
    st = adapter(net("192.168.60.0/24", "Smart home", 60, purpose="corporate", dhcp={"on": True}, isolated=True,
                     internet=True, gateway_ip="192.168.60.1"))
    z = by_cidr(zones.build([LOCAL], [st]))["192.168.60.0/24"]
    assert z["reach"] == "adapter" and z["reason"] == "zone.reason.behind_router"
    assert (z["name"], z["vlan"], z["purpose"], z["dhcp"], z["isolated"], z["internet"], z["gateway"]) == \
        ("Smart home", 60, "corporate", True, True, True, "192.168.60.1")
    assert z["adapters"] == ["Controller"] and z["sources"] == ["adapter"]
    z = by_cidr(zones.build([LOCAL], [st], consent=["192.168.60.0/24"]))["192.168.60.0/24"]
    assert z["reach"] == "consent" and z["reason"] == ""


def test_consent_is_matched_by_the_range_not_the_spelling():
    st = adapter(net("192.168.60.0/24", vlan=60))
    z = by_cidr(zones.build([LOCAL], [st], consent=["192.168.60.7/24", "junk", None]))["192.168.60.0/24"]
    assert z["reach"] == "consent"


def test_local_network_is_scan_even_when_an_adapter_names_it():
    st = adapter(net("192.168.50.0/24", "Main", 1))
    z = zones.build([LOCAL], [st])
    assert len(z) == 1 and z[0]["reach"] == "scan" and z[0]["sources"] == ["adapter", "local"]
    assert z[0]["name"] == "Main" and z[0]["vlan"] == 1


def test_public_and_oversized_networks_are_blocked_with_a_reason():
    st = adapter(net("8.8.8.0/24", "Outside", 9), net("10.0.0.0/21", "Big", 5), net("100.64.0.0/24", "Carrier", 6),
                 net("192.168.68.0/22", "Edge", 7), net("127.0.0.0/24"), net("224.0.0.0/24"))
    z = by_cidr(zones.build([LOCAL], [st], consent=["8.8.8.0/24", "10.0.0.0/21"]))      # consent cannot unlock them
    assert z["8.8.8.0/24"]["reach"] == "blocked" and z["8.8.8.0/24"]["reason"] == "scan.unsafe.public"
    assert z["10.0.0.0/21"]["reach"] == "blocked" and z["10.0.0.0/21"]["reason"] == "scan.unsafe.too_large"
    assert z["100.64.0.0/24"]["reason"] == "scan.unsafe.cgnat"
    assert z["192.168.68.0/22"]["reach"] == "adapter"                                   # a /22 is the limit, still allowed
    assert z["127.0.0.0/24"]["reason"] == "scan.unsafe.loopback" and z["224.0.0.0/24"]["reason"] == "scan.unsafe.multicast"


def test_ipv6_and_invalid_networks_are_left_out():
    st = adapter(net("fd00::/64"), net("nonsense"), net(None), net("192.168.60.0/24", vlan=60))
    z = zones.build([{"cidr": "fe80::/64"}, {"cidr": "x"}, LOCAL], [st])
    assert sorted(e["cidr"] for e in z) == ["192.168.50.0/24", "192.168.60.0/24"]


def test_name_and_vlan_come_from_the_first_adapter_that_has_them():
    first = adapter(net("192.168.60.0/24", "?", None), key="int:1", name="One")
    second = adapter(net("192.168.60.0/24", "Smart home", 60), key="int:2", name="Two")
    z = zones.build([LOCAL], [first, second])[1]
    assert z["name"] == "Smart home" and z["vlan"] == 60 and z["adapters"] == ["One", "Two"]


def test_routes_only_count_with_the_gateway_flag():
    routes = [{"dest": "192.168.90.0", "mask": "255.255.255.0", "gateway": "192.168.50.2", "flags": 3},      # UG
              {"dest": "192.168.91.0", "mask": "255.255.255.0", "gateway": "0.0.0.0", "flags": 1},           # U only
              {"dest": "0.0.0.0", "mask": "0.0.0.0", "gateway": "192.168.50.1", "flags": 3},                 # default
              {"dest": "10.0.0.0", "mask": "255.0.0.0", "gateway": "192.168.50.2", "flags": 3},              # too wide
              {"dest": "junk", "mask": "255.255.255.0", "gateway": "1.1.1.1", "flags": 3}]
    z = by_cidr(zones.build([LOCAL], [], routes))
    assert set(z) == {"192.168.50.0/24", "192.168.90.0/24"}
    assert z["192.168.90.0/24"]["sources"] == ["route"] and z["192.168.90.0/24"]["gateway"] == "192.168.50.2"
    assert z["192.168.90.0/24"]["reach"] == "adapter"


def test_order_is_reachable_first_then_vlan_then_address():
    st = adapter(net("192.168.80.0/24", vlan=80), net("192.168.60.0/24", vlan=60), net("8.8.8.0/24", vlan=1),
                 net("192.168.70.0/24"), net("192.168.20.0/24", vlan=20))
    z = zones.build([LOCAL], [st], consent=["192.168.80.0/24"])
    assert [e["cidr"] for e in z] == ["192.168.50.0/24", "192.168.80.0/24", "192.168.20.0/24", "192.168.60.0/24",
                                      "192.168.70.0/24", "8.8.8.0/24"]
    assert [e["reach"] for e in z] == ["scan", "consent", "adapter", "adapter", "adapter", "blocked"]


def test_number_of_zones_is_limited():
    st = adapter(*[net("10.%d.%d.0/24" % (i // 200, i % 200)) for i in range(zones.MAX_ZONES + 20)])
    assert len(zones.build([LOCAL], [st])) == zones.MAX_ZONES


def test_scan_targets_never_include_adapter_or_blocked_zones():
    st = adapter(net("192.168.60.0/24", vlan=60), net("192.168.80.0/24", vlan=80), net("8.8.8.0/24"))
    z = zones.build([LOCAL], [st], consent=["192.168.80.0/24"])
    assert zones.scan_targets(z) == ["192.168.50.0/24", "192.168.80.0/24"]
    assert zones.scan_targets([e for e in z if e["reach"] in ("adapter", "blocked")]) == []
    many = [{"cidr": "10.%d.0.0/24" % i, "reach": "scan"} for i in range(30)]
    assert len(zones.scan_targets(many)) == 8


# ---------------------------------------------------------------------- assign
def _dev(i, ip, sources=("tcp",)):
    return {"id": i, "ip": ip, "sources": list(sources)}


def test_assign_counts_into_the_smallest_matching_network():
    z = zones.build([LOCAL], [adapter(net("192.168.50.128/25", "Back", 5), net("192.168.60.0/24", "Smart", 60))])
    devices = [_dev("a", "192.168.50.5"), _dev("b", "192.168.50.200"), _dev("c", "192.168.60.7", ["adapter"]),
               _dev("d", "192.168.60.8", ["adapter", "tcp"]), _dev("e", "172.16.0.1"), _dev("f", ""),
               _dev("g", "not-an-ip"), {"id": "h", "ip": None}]
    out, unzoned = zones.assign(z, devices)
    got = by_cidr(out)
    assert got["192.168.50.0/24"]["devices"] == 1 and got["192.168.50.0/24"]["ids"] == ["a"]
    assert got["192.168.50.128/25"]["devices"] == 1 and got["192.168.50.128/25"]["ids"] == ["b"]
    assert got["192.168.60.0/24"]["devices"] == 2 and got["192.168.60.0/24"]["adapter_devices"] == 1
    assert got["192.168.50.0/24"]["adapter_devices"] == 0
    assert unzoned == ["e"]                       # no address -> left out of both the zones and the unzoned list


def test_assign_resets_counts_and_caps_the_id_list():
    z = zones.build([LOCAL], [])
    devices = [_dev("d%d" % i, "192.168.50.%d" % (i % 250 + 1)) for i in range(260)]
    out, _ = zones.assign(z, devices)
    assert out[0]["devices"] == 260 and len(out[0]["ids"]) == 200
    out, _ = zones.assign(out, [])
    assert out[0]["devices"] == 0 and out[0]["ids"] == [] and out[0]["adapter_devices"] == 0


def test_label_has_the_title_parts():
    assert zones.label({"cidr": "192.168.60.0/24", "name": "Smart home", "vlan": 60, "extra": 1}) == \
        {"name": "Smart home", "vlan": 60, "cidr": "192.168.60.0/24"}
    assert zones.label({"cidr": "10.0.0.0/24"}) == {"name": None, "vlan": None, "cidr": "10.0.0.0/24"}


# ---------------------------------------------------------------------- core
@pytest.fixture()
def demo_core():
    st = Store(":memory:")
    c = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=True), st, None)
    c.demo = demo.Demo()
    c.demo.seed(c)
    return c


@pytest.fixture()
def live_core(demo_core):
    """The demo with the controller, the router and a family read half delivering their events."""
    c, now = demo_core, time.time()
    for kind, name in (("controller", "Network controller"), ("simulation", "Sample controller")):
        c.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,'{}',1,?)", (kind, name, now))
    ids = {r["kind"]: r["id"] for r in c.store.query("SELECT id, kind FROM integrations")}
    c.on_event("int:%d" % ids["controller"], ids["controller"], controller_event(now))
    c.on_event("int:99", 99, router_topology_event(now))
    c.on_event("int:%d" % ids["simulation"], ids["simulation"],
               {"type": "config", "source": "family", "family": "simulation", "t": now, "state": live_sample(),
                "capabilities": live_capabilities("simulation")})
    return c


def _zones(c):
    return by_cidr(c.zone_list()["zones"])


def test_without_a_configuration_only_the_local_network_is_known(demo_core):
    res = demo_core.zone_list()
    assert [z["cidr"] for z in res["zones"]] == ["192.168.50.0/24"] and res["zones"][0]["reach"] == "scan"
    assert res["adapter_inventory"] is True and res["no_address"] == 2        # two hardware addresses without an IP address
    assert res["unzoned"] >= 6                                                  # the adapter-only devices have no zone yet


def test_configuration_event_adds_the_vlans_with_their_adapter_only_devices(live_core):
    z = _zones(live_core)
    home = z["192.168.60.0/24"]
    assert home["name"] == "Smart home" and home["vlan"] == 60 and home["reach"] == "adapter"
    assert home["devices"] == 4 and home["adapter_devices"] == 4 and len(home["ids"]) == 4
    assert z["192.168.70.0/24"]["adapter_devices"] == 2 and z["192.168.80.0/24"]["adapter_devices"] == 1
    assert z["192.168.50.0/24"]["reach"] == "scan" and z["192.168.50.0/24"]["adapter_devices"] == 0
    assert z["192.168.50.0/24"]["name"] == "Main" and z["192.168.50.0/24"]["vlan"] == 1
    assert live_core.zone_list()["unzoned"] == 0


def test_zone_list_sends_nothing_and_keeps_adapter_zones_out_of_the_scan_targets(live_core):
    zs = live_core.zone_list()["zones"]
    assert zones.scan_targets(zs) == ["192.168.50.0/24"]
    assert live_core.zone_consent() == []


def test_consent_is_given_and_withdrawn(live_core):
    res = live_core.set_zone_consent("192.168.60.0/24", True)
    assert by_cidr(res["zones"])["192.168.60.0/24"]["reach"] == "consent"
    assert live_core.zone_consent() == ["192.168.60.0/24"]
    assert zones.scan_targets(live_core.zone_list()["zones"]) == ["192.168.50.0/24", "192.168.60.0/24"]
    live_core.set_zone_consent("192.168.60.0/24", True)                         # twice is still one entry
    assert live_core.zone_consent() == ["192.168.60.0/24"]
    res = live_core.set_zone_consent("192.168.60.9/24", False)                  # any spelling of the range
    assert by_cidr(res["zones"])["192.168.60.0/24"]["reach"] == "adapter" and live_core.zone_consent() == []
    assert zones.scan_targets(res["zones"]) == ["192.168.50.0/24"]


def test_consent_refuses_unknown_public_and_invalid_ranges(live_core):
    with pytest.raises(ValueError) as e:
        live_core.set_zone_consent("192.168.99.0/24", True)
    assert str(e.value) == "zone.err.unknown"
    with pytest.raises(ValueError) as e:
        live_core.set_zone_consent("8.8.8.0/24", True)
    assert str(e.value) == "scan.unsafe.public"
    with pytest.raises(ValueError) as e:
        live_core.set_zone_consent("not a range", True)
    assert str(e.value) == "scan.unsafe.invalid"
    with pytest.raises(ValueError) as e:
        live_core.set_zone_consent("10.0.0.0/21", True)
    assert str(e.value) == "scan.unsafe.too_large"
    assert live_core.zone_consent() == []


def test_consent_never_makes_the_local_network_or_a_blocked_one_different(live_core):
    live_core.set_zone_consent("192.168.50.0/24", True)                         # allowed (known), reach stays scan
    assert _zones(live_core)["192.168.50.0/24"]["reach"] == "scan"


def test_rescan_includes_consented_ranges_only(live_core):
    live_core.set_zone_consent("192.168.70.0/24", True)
    live_core.store.set_setting("rescan_cidrs", ["192.168.50.0/24"])
    cidrs = list(live_core.store.get_setting("rescan_cidrs", []) or [])
    cidrs += [c for c in live_core.zone_consent() if c not in cidrs]            # the same step as core._rescan_loop
    assert cidrs == ["192.168.50.0/24", "192.168.70.0/24"]
    assert "192.168.60.0/24" not in cidrs and "192.168.80.0/24" not in cidrs


def test_consent_list_ignores_junk_in_the_setting(live_core):
    live_core.store.set_setting("zone_consent", ["192.168.60.0/24", 5, None, {"a": 1}])
    assert live_core.zone_consent() == ["192.168.60.0/24"]
    assert _zones(live_core)["192.168.60.0/24"]["reach"] == "consent"
