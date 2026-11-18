"""S4: the data of the switch front view - the controller read half's ports, radios and wireless names, the port watch (link drops,
error episodes, rates), roaming, the exact counters of a cable device, and the faceplates themselves."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import core, portfront
from lanloom.demo_live import live_sample
from lanloom.store import Store



# ---------------------------------------------------------------------- the port watch
def ports(link=True, err=0, tx=0, rx=0):
    return [{"id": "p1", "device": "e1", "idx": 1, "link": link, "rx_errors": err, "tx_errors": 0, "tx_bytes": tx, "rx_bytes": rx}]


def test_levels_follow_the_agreed_thresholds():
    assert [portfront.level_for(n) for n in (0, 1, 2, 3, 9)] == ["ok", "warn", "warn", "bad", "bad"]


def test_a_dropped_link_and_error_episodes_are_counted_for_24_hours():
    w = portfront.PortWatch()
    t = 1_800_000_000
    w.feed(ports(True, 0), t)
    w.feed(ports(False, 0), t + 60)                 # link dropped
    w.feed(ports(True, 0), t + 120)
    w.feed(ports(True, 5), t + 180)                 # errors went up: one episode
    w.feed(ports(True, 5), t + 240)                 # no change: nothing new
    s = w.stats("p1", t + 300)
    assert (s["flaps"], s["errors"], s["events"], s["level"]) == (1, 1, 2, "warn")
    w.feed(ports(False), t + 360); w.feed(ports(True), t + 420)
    assert w.stats("p1", t + 500)["level"] == "bad"
    assert w.stats("p1", t + 25 * 3600)["events"] == 0                      # a day later the events are gone


def test_a_port_that_was_never_up_is_no_flap_and_counters_starting_over_are_no_error():
    w = portfront.PortWatch()
    w.feed(ports(False, 7), 100.0); w.feed(ports(False, 7), 160.0)
    w.feed(ports(True, 2), 220.0)                                          # counter went down: the device restarted
    assert w.stats("p1", 300.0)["events"] == 0


def test_byte_counters_become_deltas_and_rates_and_a_restart_gives_nothing():
    w = portfront.PortWatch()
    assert w.feed(ports(tx=1000, rx=500), 100.0) == []
    assert w.feed(ports(tx=7000, rx=1500), 160.0) == [("p1", 6000, 1000)]
    assert w.stats("p1", 160.0)["rate"] == {"tx": 100, "rx": 17}
    assert w.feed(ports(tx=10, rx=10), 220.0) == []                          # counters started over
    assert w.stats("p1", 700.0)["rate"] is None                              # an old rate is not shown as live


def test_events_survive_a_restart():
    store = Store(":memory:")
    w = portfront.PortWatch(store)
    t = time.time()
    w.feed(ports(True), t - 1000); w.feed(ports(False), t - 940)
    w._dirty, w._saved = True, 0
    w._save(t)
    again = portfront.PortWatch(store)
    assert again.stats("p1", t)["flaps"] == 1
    assert again.problems(t) == [("p1", 1, 0)]


def test_garbage_in_a_reading_is_ignored():
    w = portfront.PortWatch()
    w.feed([{"id": 5}, {"id": "p9", "link": "yes", "rx_bytes": "7", "tx_bytes": True}, "x", {}], 1.0)
    w.feed([{"id": "p9", "link": 1, "rx_bytes": None}], 2.0)
    assert w.stats("p9", 3.0)["events"] == 0


# ---------------------------------------------------------------------- roaming
def test_a_client_that_moves_between_access_points_counts_once_for_each_side():
    r = portfront.RoamWatch()
    t = 1_800_000_000
    r.feed([{"id": "c1", "ap": "A", "band": "5ghz"}, {"id": "c2", "ap": "A", "band": "2.4ghz"}], t)
    assert r.bands("A") == {"5ghz": 1, "2.4ghz": 1}
    r.feed([{"id": "c1", "ap": "B", "band": "5ghz"}, {"id": "c2", "ap": "A", "band": "2.4ghz"}], t + 60)
    assert r.counts("A", t + 70) == {"in": 0, "out": 1} and r.counts("B", t + 70) == {"in": 1, "out": 0}
    assert r.total(t + 70) == 1 and r.bands("B") == {"5ghz": 1}
    r.feed([], t + 120)
    r.feed([{"id": "c1", "ap": "A", "band": "5ghz"}], t + 180)           # gone for a poll, back elsewhere: a new start, no roam
    assert r.total(t + 200) == 1
    assert r.total(t + 25 * 3600) == 0


# ---------------------------------------------------------------------- faceplates
def test_the_faceplate_of_a_switch_lists_what_sits_behind_each_port():
    st = live_sample(1_800_000_000)
    w, r = portfront.PortWatch(), portfront.RoamWatch()
    fronts = portfront.fronts(st, w, r, 1_800_000_000)
    sw = next(f for f in fronts if f["name"] == "Switch hall")
    p9 = next(p for p in sw["ports"] if p["idx"] == 9)
    assert [d["name"] for d in p9["devices"]] == ["Storage"] and p9["link"] and p9["speed"] == 1000
    assert next(p for p in sw["ports"] if p["idx"] == 15)["sfp"] is True
    assert next(p for p in sw["ports"] if p["idx"] == 3)["neighbor"] == "Access point living room"
    assert next(p for p in sw["ports"] if p["idx"] == 5)["poe"]["on"] is True
    assert [f["kind"] for f in fronts] == sorted((f["kind"] for f in fronts), key={"router": 0, "switch": 1, "ap": 2}.get)


def test_an_access_point_shows_live_clients_per_band_and_its_roams():
    st = live_sample(1_800_000_000)
    w, r = portfront.PortWatch(), portfront.RoamWatch()
    r.feed([{"id": "c%d" % i, "ap": "MAC-AP1", "band": "5ghz"} for i in range(4)], 100.0)
    r.feed([{"id": "c%d" % i, "ap": "MAC-AP1" if i else "MAC-AP2", "band": "5ghz"} for i in range(4)], 160.0)
    ap = next(f for f in portfront.fronts(st, w, r, 200.0, ap_keys={"e_ap1": "MAC-AP1", "e_ap2": "MAC-AP2"}) if f["name"] == "Access point living room")
    assert ap["roams"] == {"in": 0, "out": 1}
    assert {x["band"]: x["clients"] for x in ap["radios"]} == {"5ghz": 3, "2.4ghz": 5}      # live count for 5 GHz, stored one for 2.4


def test_port_health_shows_on_the_faceplate_and_the_worst_port_sets_the_level():
    st = live_sample(1_800_000_000)
    w, r = portfront.PortWatch(), portfront.RoamWatch()
    pid = next(p["id"] for p in st["ports"] if p["device"] == "e_sw1" and p["idx"] == 12)
    w.events[pid] = [[1_800_000_000 - 60, "flap"]] * 3
    sw = next(f for f in portfront.fronts(st, w, r, 1_800_000_000) if f["name"] == "Switch hall")
    assert sw["level"] == "bad" and next(p for p in sw["ports"] if p["idx"] == 12)["flaps"] == 3
    assert all(p["level"] == "ok" for p in sw["ports"] if p["idx"] != 12)


def test_nothing_private_is_in_a_faceplate():
    st = live_sample(1_800_000_000)
    text = json.dumps(portfront.fronts(st, portfront.PortWatch(), portfront.RoamWatch(), 1_800_000_000))
    assert "mac" not in text.lower().replace("machine", "")


# ---------------------------------------------------------------------- the core
@pytest.fixture()
def bare_core(monkeypatch):
    c = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), Store(":memory:"), None)
    topo = {"nodes": {}, "links": {"d-office": {"parent": "d-sw", "child": "d-office", "port": "2", "own_port": None, "medium": "wired", "band": None, "ssid": None, "source": "controller"},
                                   "d-lab1": {"parent": "d-sw", "child": "d-lab1", "port": "4", "own_port": None, "medium": "wired", "band": None, "ssid": None, "source": "controller"},
                                   "d-lab2": {"parent": "d-sw", "child": "d-lab2", "port": "4", "own_port": None, "medium": "wired", "band": None, "ssid": None, "source": "controller"}}}
    monkeypatch.setattr(c, "topology", lambda: topo)
    c.ip_to_device = {"192.168.50.2": "d-sw"}
    return c


def test_a_cable_device_alone_on_its_port_gets_its_exact_bytes_from_the_port_counters(bare_core):
    eq = [{"id": "e1", "ip": "192.168.50.2", "mac": "02:00:00:00:00:01"}]
    base = [{"id": "pa", "device": "e1", "idx": 2, "link": True, "tx_bytes": 1000, "rx_bytes": 100},
            {"id": "pb", "device": "e1", "idx": 4, "link": True, "tx_bytes": 5000, "rx_bytes": 500}]
    bare_core.book_ports(base, eq, 100.0)
    later = [dict(base[0], tx_bytes=7000, rx_bytes=1100), dict(base[1], tx_bytes=9000, rx_bytes=900)]
    bare_core.book_ports(later, eq, 160.0)
    assert bare_core.spread["p:d-sw:d-office"] == [6000, 1000, 60]            # a = switch: what the switch sent is a -> b
    assert "p:d-sw:d-lab1" not in bare_core.spread                             # two devices share port 4: not exact, no booking
    assert "p:d-sw:d-office" in bare_core.port_exact


def test_host_flows_do_not_count_twice_on_a_link_the_port_counters_cover(bare_core, monkeypatch):
    monkeypatch.setattr(bare_core, "phys_path", lambda a, b: [("p:d-sw:d-office", 1)])
    bare_core.port_exact["p:d-sw:d-office"] = time.time()
    bare_core._book_path("self", "d-office", 100, 50)
    assert bare_core.pending_bytes == {}
    bare_core.port_exact["p:d-sw:d-office"] = time.time() - 600                  # the counters went quiet: the host's flows count again
    bare_core._book_path("self", "d-office", 100, 50)
    assert bare_core.pending_bytes == {"p:d-sw:d-office": [100, 50]}


def test_a_full_poll_feeds_the_watch_only_when_no_light_poll_carries_ports(bare_core):
    st = live_sample(time.time())
    bare_core.hitlog.feed("k", [], 1.0)
    bare_core.live_cfg  # noqa: B018 - the booking path is exercised through book_ports directly
    bare_core.book_ports(st["ports"], [], time.time())
    assert bare_core.portwatch.last
    n = len(bare_core.portwatch.last)
    bare_core.book_ports(st["ports"], [], time.time() + 1)
    assert len(bare_core.portwatch.last) == n


def test_the_roaming_booking_ignores_wired_clients(bare_core):
    bare_core.book_roaming({"clients": [{"mac": "m1", "medium": "wired", "parent_mac": "sw"}, {"mac": "m2", "medium": "radio", "parent_mac": "ap1", "band": "5ghz"}]}, 10.0)
    assert bare_core.roamwatch.bands("ap1") == {"5ghz": 1} and bare_core.roamwatch.bands("sw") == {}
    bare_core.book_roaming({"clients": [{"mac": "m2", "medium": "radio", "parent_mac": "ap2", "band": "5ghz"}]}, 70.0)
    assert bare_core.roamwatch.total(80.0) == 1


# ---------------------------------------------------------------------- the network view: noise, tags, quiet devices
def test_the_common_floor_of_a_network_is_taken_out_when_asked(bare_core):
    bare_core.devices = lambda: [{"id": "d-office", "ip": "192.168.50.20"}, {"id": "d-b", "ip": "192.168.50.21"}, {"id": "d-c", "ip": "192.168.50.22"},
                                 {"id": "d-lone", "ip": "192.168.70.5"}]
    bare_core.live_configs = lambda: [{"state": {"networks": [{"id": "n1", "cidr": "192.168.50.0/24"}, {"id": "n2", "cidr": "192.168.70.0/24"}]}}]
    exact = [("sw", "d-office", 1000, 10), ("sw", "d-b", 400, 10), ("sw", "d-c", 700, 10), ("sw", "d-lone", 50, 10)]
    out = bare_core._without_noise(exact)
    assert [o[2] for o in out] == [600, 0, 300, 50]                # n1 has three ports: floor 400 comes off; n2 has one port: untouched
    assert [o[3] for o in out] == [10, 10, 10, 10]                 # what the devices send is never reduced
    assert bare_core._without_noise(exact[:2]) == exact[:2]        # two ports are no floor


def test_noise_is_off_by_default_and_can_be_switched_on_in_the_settings(bare_core):
    assert bare_core.settings()["port_noise"] is False
    assert bare_core.update_settings({"port_noise": True})["port_noise"] is True
    assert bare_core.update_settings({"port_noise": "yes"})["port_noise"] is True        # a wrong type changes nothing


def test_device_tags_name_the_network_and_the_wireless_network(bare_core, monkeypatch):
    bare_core.devices = lambda: [{"id": "d1", "ip": "192.168.50.20"}, {"id": "d2", "ip": "192.168.70.9"}, {"id": "d3", "ip": "10.9.9.9"}]
    bare_core.live_configs = lambda: [{"state": {"networks": [{"id": "n1", "name": "Main", "vlan": 1, "cidr": "192.168.50.0/24"},
                                                              {"id": "n2", "name": "Cameras", "vlan": 70, "cidr": "192.168.70.0/24"}]}}]
    bare_core.topo_obs = {"x": {}}
    monkeypatch.setattr(bare_core, "topology", lambda: {"links": {"d1": {"medium": "radio", "ssid": "Home", "band": "5ghz"}, "d2": {"medium": "wired"}}})
    tags = bare_core.net_tags()
    assert tags["d1"] == {"vlan": 1, "net": "Main", "ssid": "Home", "band": "5ghz"}
    assert tags["d2"] == {"vlan": 70, "net": "Cameras", "ssid": None, "band": None}
    assert "d3" not in tags


def comp(i, kind="iot", zone="net:192.168.50.0/24", **kw):
    return dict({"id": "d%d" % i, "zone": zone, "kind": kind, "status": "up", "grade": "unknown"}, **kw)


def test_a_large_network_folds_its_quiet_devices_into_one_tile():
    from lanloom import mapmodel
    comps = [comp(i, "computer") for i in range(8)] + [comp(10 + i, "iot") for i in range(10)]
    mapmodel.collapse_quiet(comps)
    folded = [c for c in comps if c.get("fold")]
    more = next(c for c in comps if c["id"].startswith("qm:"))
    assert len(comps) - len(folded) <= mapmodel.MAX_ZONE + 1                      # what stays visible, plus the tile itself
    assert all(c["kind"] == "iot" for c in folded) and {c["fold"] for c in folded} == {more["fold_key"]} == {"quiet:net:192.168.50.0/24"}
    assert more["vars"]["n"] == len(folded) and more["title_key"] == "comp.quiet.more" and more["quiet_more"] == more["zone"]


def test_nothing_folds_in_a_small_network_or_for_busy_kinds_and_a_device_with_a_problem_stays():
    from lanloom import mapmodel
    small = [comp(i) for i in range(10)]
    mapmodel.collapse_quiet(small)
    assert len(small) == 10
    busy = [comp(i, "computer") for i in range(16)]
    mapmodel.collapse_quiet(busy)
    assert len(busy) == 16
    mixed = [comp(i, "computer") for i in range(12)] + [comp(30, "iot", status="down"), comp(31, "iot", grade="interrupted"), comp(32, "iot"), comp(33, "iot"), comp(34, "printer")]
    mapmodel.collapse_quiet(mixed)
    assert {c["id"] for c in mixed if c.get("fold")} == {"d32", "d33", "d34"}        # a device that is down or interrupted stays on the board
