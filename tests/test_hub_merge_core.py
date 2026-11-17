"""Hub devices merged with network devices in the core, the map model and the component cards: radio lanes in both
views, the hub side of a merged device, match rules, the sensors view, the sensor page history and the live rings.
Sample data only; the hub is filled through the same event path the live source uses."""
import asyncio
import time

import pytest

from lanloom import mapmodel
from test_hub_bridge_core import add_hub, make_core

MAC_TV = "aa:bb:cc:00:00:10"
MAC_STICK = "aa:bb:cc:00:00:20"


def hdev(did, name, **kw):
    return dict({"id": did, "name": name, "model": None, "manufacturer": None, "area": None, "via": None, "service": False,
                 "macs": [], "sw": None, "domains": [], "radio": None, "role": None, "ip_hint": None, "host_hint": None}, **kw)


def hent(eid, did, state, **kw):
    return dict({"entity_id": eid, "device_id": did, "name": eid.split(".", 1)[1].replace("_", " ").title(), "state": state, "unit": None,
                 "domain": eid.split(".", 1)[0], "area": None, "dc": None, "cat": None, "platform": "sample_hub", "ls": False}, **kw)


def snapshot(t=1.0):
    devices = [
        hdev("c1", "Mesh stick", radio="mesh154", role="coordinator", macs=[MAC_STICK], model="Stick"),
        hdev("a", "Lamp A", radio="mesh154", role="device", via="c1", area="porch"),
        hdev("b", "Lamp B", radio="mesh154", role="device", via="c1"),
        hdev("px", "Proxy", radio="ble", role="receiver"),
        hdev("tag", "Tag", radio="ble", role="device"),
        hdev("tv", "Living TV", macs=[MAC_TV]),
    ]
    entities = [hent("light.a", "a", "unavailable", platform="sample_mesh"), hent("light.b", "b", "on", platform="sample_mesh"),
                hent("sensor.b_battery", "b", "8", unit="%", dc="battery", domain="sensor", platform="sample_mesh", cat="diagnostic"),
                hent("sensor.stick_status", "c1", "online"), hent("sensor.proxy_x", "px", "on"), hent("sensor.tag_x", "tag", "4", unit="W"),
                hent("sensor.tv_x", "tv", "1"), hent("light.porch", None, "on", area="porch", platform="sample_area"),
                hent("sensor.free_temp", None, "21.5", unit="C", domain="sensor")]
    return {"type": "hub_snapshot", "t": t, "devices": devices, "entities": entities, "areas": [{"id": "porch", "name": "Porch"}],
            "trackers": [], "automations": [], "scripts": []}


def add_net_device(c, n, title, mac):
    dev_id = "d-test-%d" % n
    c.store.upsert_device({"id": dev_id, "ip": "192.168.50.%d" % n, "mac": mac, "data": {}, "kind": "computer", "confidence": 0.9,
                           "title": title}, None)
    return dev_id


@pytest.fixture()
def world(tmp_path):
    c, _rep = make_core(tmp_path)
    iid = add_hub(c)
    tv = add_net_device(c, 1, "Living TV", MAC_TV)
    stick = add_net_device(c, 2, "Radio bridge", MAC_STICK)
    plain = add_net_device(c, 3, "Printer", "aa:bb:cc:00:00:30")
    c.on_event("int:%d" % iid, iid, snapshot())
    c.structure_version += 1
    return c, iid, {"tv": tv, "stick": stick, "plain": plain}


def comp(view, cid):
    return next((x for x in view["components"] if x["id"] == cid), None)


def trace(view, tid):
    return next((x for x in view["traces"] if x["id"] == tid), None)


# ---------------------------------------------------------------------- merge
def test_hub_merge_matches_by_mac(world):
    c, iid, ids = world
    res = c.hub_merge()
    assert res["by_hub"] == {"%d:tv" % iid: ids["tv"], "%d:c1" % iid: ids["stick"]}
    assert [m["how"] for m in res["by_net"][ids["tv"]]] == ["mac"]
    assert c.hub_merge() is res                                     # cached until something changes


def test_hub_merge_without_hubs_is_empty(tmp_path):
    c, _ = make_core(tmp_path)
    add_net_device(c, 1, "Living TV", MAC_TV)
    assert c.hub_merge() == {"by_net": {}, "by_hub": {}, "suggest": {}}


# ---------------------------------------------------------------------- overview
def test_overview_has_a_radio_zone_with_networks_and_mesh_traces(world):
    c, iid, ids = world
    v = mapmodel.build(c, "overview")
    assert "radios:%d" % iid in {z["id"] for z in v["zones"]}
    nets = [x for x in v["components"] if x["id"].startswith("rn:%d:" % iid)]
    assert sorted(x["id"] for x in nets) == ["rn:%d:ble:_" % iid, "rn:%d:mesh154:c1" % iid]
    assert all(x["zone"] == "radios:%d" % iid and x["radio"] in ("ble", "mesh154") for x in nets)
    for key, kind in (("mesh154:c1", "mesh154"), ("ble:_", "ble")):
        t = trace(v, "m:%d:%s" % (iid, key))
        assert t is not None and t["kind"] == "radio" and t["phys"]["mesh"] == kind and t["phys"]["medium"] == "radio"
        assert t["a"] == "hub:%d" % iid and t["b"] == "rn:%d:%s" % (iid, key)
    # areas of the hub still appear
    assert comp(v, "area:%d:porch" % iid) and trace(v, "h:%d:porch" % iid)


def test_merged_network_devices_carry_the_hub_badge(world):
    c, iid, ids = world
    for view in ("overview", "network"):
        v = mapmodel.build(c, view)
        tv = comp(v, ids["tv"])
        assert tv["hub"] is True and tv["badges"][0] == "HUB", view
        plain = comp(v, ids["plain"])
        assert "hub" not in plain and "HUB" not in plain["badges"], view


# ---------------------------------------------------------------------- network view
def test_network_view_lanes_radio_devices_and_one_lane_mark_per_network(world):
    c, iid, ids = world
    v = mapmodel.build(c, "network")
    zones = {z["id"] for z in v["zones"]}
    assert {"radio:%d:mesh154:c1" % iid, "radio:%d:ble:_" % iid} <= zones
    # a silent radio device is down, a healthy one up; both are drawn as "rd:" tiles
    a, b = comp(v, "rd:%d:a" % iid), comp(v, "rd:%d:b" % iid)
    assert a["status"] == "down" and b["status"] == "up" and a["zone"] == "radio:%d:mesh154:c1" % iid
    assert comp(v, "rd:%d:tag" % iid)["status"] == "up" and comp(v, "rd:%d:px" % iid)["kind"] == "ap"
    # the coordinator is merged with its network device, so it is not drawn a second time
    assert comp(v, "rd:%d:c1" % iid) is None and comp(v, ids["stick"]) is not None
    by_net = {}
    for t in v["traces"]:
        if t["kind"] == "radio" and (t.get("phys") or {}).get("mesh"):
            by_net.setdefault(t["phys"]["mesh"], []).append(t["id"])
    assert {k: len(x) for k, x in by_net.items()} == {"mesh154": 1, "ble": 1}
    # device traces (physical links) never carry the mark
    assert not [t for t in v["traces"] if t["kind"] != "radio" and (t.get("phys") or {}).get("mesh")]
    # the lanes of the members start at the merged coordinator; the first one (the silent device) carries the mark
    assert trace(v, "r:%d:a" % iid)["a"] == ids["stick"] and trace(v, "r:%d:a" % iid)["phys"]["mesh"] == "mesh154"
    assert trace(v, "r:%d:b" % iid)["a"] == ids["stick"] and "mesh" not in trace(v, "r:%d:b" % iid)["phys"]
    # the hub itself is drawn in the radio zone when no network device stands for it
    assert comp(v, "hub:%d" % iid)["kind"] == "hub"


def test_network_view_with_an_unmerged_coordinator_marks_its_own_lane(world):
    c, iid, ids = world
    c.set_hub_match(ids["stick"], "%d:c1" % iid, "split")
    v = mapmodel.build(c, "network")
    coord = comp(v, "rd:%d:c1" % iid)
    assert coord is not None and coord["title"] == "Mesh stick" and coord["radio"] == "mesh154"
    t = trace(v, "r:%d:c1" % iid)
    assert t["a"] == "hub:%d" % iid and t["phys"]["mesh"] == "mesh154"
    members = [trace(v, "r:%d:%s" % (iid, m)) for m in ("a", "b")]
    assert all(x["a"] == "rd:%d:c1" % iid and "mesh" not in x["phys"] for x in members)
    assert sum(1 for x in v["traces"] if (x.get("phys") or {}).get("mesh") == "mesh154") == 1


def test_network_view_network_without_coordinator_uses_the_network_tile(world):
    c, iid, ids = world
    v = mapmodel.build(c, "network")
    tile = comp(v, "rn:%d:ble:_" % iid)
    assert tile["title_key"] == "radio.kind.ble" and tile["vars"] == {"devices": 2, "silent": 0}
    assert trace(v, "m:%d:ble:_" % iid)["phys"]["mesh"] == "ble"
    assert "mesh" not in trace(v, "r:%d:tag" % iid)["phys"]


# ---------------------------------------------------------------------- component cards
def test_component_detail_of_a_merged_device_names_the_evidence(world):
    c, iid, ids = world
    d = mapmodel.component_detail(c, ids["tv"])
    (side,) = d["hub_side"]
    assert side["integ"] == iid and side["device_id"] == "tv" and side["name"] == "Living TV"
    assert side["match"]["how"] == "mac" and side["match"]["confidence"] == 0.95 and side["match"]["manual"] is False
    assert [e["id"] for e in side["entities"]] == ["sensor.tv_x"]
    assert "hub_side" not in mapmodel.component_detail(c, ids["plain"])


def test_component_detail_of_a_merged_coordinator_carries_the_network(world):
    c, iid, ids = world
    d = mapmodel.component_detail(c, ids["stick"])
    assert d["hub_side"][0]["role"] == "coordinator"
    net = d["radio_net"]
    assert net["key"] == "mesh154:c1" and net["integ"] == iid and net["devices"] == 2 and net["silent"] == 1
    assert [m["device_id"] for m in net["members"]] == ["a", "b"]            # the silent one first
    assert next(m for m in net["members"] if m["device_id"] == "b")["battery"] == 8


def test_component_detail_of_radio_devices_and_networks(world):
    c, iid, ids = world
    d = mapmodel.component_detail(c, "rd:%d:a" % iid)
    assert d["hub_device"]["device_id"] == "a" and d["hub_device"]["fresh"]["fresh"] == "silent" and "radio_net" not in d
    assert d["hub_device"]["network"]["key"] == "mesh154:c1" and d["hub_device"]["network"]["coordinator"] == "Mesh stick"
    d = mapmodel.component_detail(c, "rn:%d:ble:_" % iid)
    assert d["radio_net"]["kind"] == "ble" and d["radio_net"]["anchors"] == 1 and d["radio_net"]["coordinator"] is None
    assert mapmodel.component_detail(c, "rd:%d:nope" % iid) is None


# ---------------------------------------------------------------------- rules
def test_split_removes_the_hub_side_and_reset_restores_it(world):
    c, iid, ids = world
    key = "%d:tv" % iid
    assert c.set_hub_match(ids["tv"], key, "split") == {"ok": True, "action": "split"}
    assert key not in c.hub_merge()["by_hub"]
    assert "hub_side" not in mapmodel.component_detail(c, ids["tv"])
    assert "hub" not in comp(mapmodel.build(c, "overview"), ids["tv"])
    assert c.hub_match_rules() == {"split": [[ids["tv"], key]], "join": []}
    c.set_hub_match(ids["tv"], key, "reset")
    assert c.hub_match_rules() == {"split": [], "join": []}
    assert mapmodel.component_detail(c, ids["tv"])["hub_side"][0]["match"]["how"] == "mac"


def test_join_makes_a_manual_match(world):
    c, iid, ids = world
    key = "%d:tag" % iid
    assert c.set_hub_match(ids["plain"], key, "join")["action"] == "join"
    assert c.hub_merge()["by_hub"][key] == ids["plain"]
    d = mapmodel.component_detail(c, ids["plain"])
    assert d["hub_side"][0]["match"] == {"integ": iid, "device_id": "tag", "how": "manual", "value": None, "confidence": 1.0, "manual": True}
    # joining the same hub device elsewhere moves it
    c.set_hub_match(ids["tv"], key, "join")
    assert c.hub_merge()["by_hub"][key] == ids["tv"] and "hub_side" not in mapmodel.component_detail(c, ids["plain"])


def test_invalid_match_input_raises(world):
    c, iid, ids = world
    bad = [(ids["tv"], "%d:tv" % iid, "bogus"), (ids["tv"], "x:tv", "split"), (ids["tv"], "%d:" % iid, "split"),
           (ids["tv"], "999:tv", "split"), (ids["tv"], "%d:nope" % iid, "split"), (ids["tv"], "%d:%s" % (iid, "x" * 70), "split"),
           (ids["tv"], 5, "split"), (7, "%d:tv" % iid, "split"), (ids["tv"], "nocolon", "split")]
    for net_id, key, action in bad:
        with pytest.raises(ValueError, match="hub.match.invalid"):
            c.set_hub_match(net_id, key, action)
    with pytest.raises(ValueError, match="device.err.missing"):
        c.set_hub_match("no-such-device", "%d:tv" % iid, "split")
    assert c.hub_match_rules() == {"split": [], "join": []}


# ---------------------------------------------------------------------- sensors view
def test_hub_sensors_view(world):
    c, iid, ids = world
    assert c.hub_sensors(999) is None
    now = time.time()
    c.on_event("int:%d" % iid, iid, {"type": "hub_state", "entity_id": "sensor.tag_x", "state": "5", "t": now})
    v = c.hub_sensors(iid)
    assert {"integration", "name", "connected", "t", "limited", "areas", "devices", "entities", "onsite", "radio", "health", "events"} <= set(v)
    assert v["integration"] == iid and v["name"] == "Sample hub" and v["connected"] is True and v["limited"] is False
    assert {e["id"] for e in v["entities"]} >= {"light.a", "sensor.tag_x", "sensor.free_temp"}
    assert next(e for e in v["entities"] if e["id"] == "sensor.tag_x")["state"] == "5"
    by = {d["id"]: d for d in v["devices"]}
    assert by["tv"]["network_device"] == ids["tv"] and by["tv"]["radio"] is None and by["tv"]["net"] is None
    assert by["a"]["fresh"] == "silent" and by["a"]["net"] == "mesh154:c1" and by["b"]["network_device"] is None
    cats = {i["device_id"]: i["cat"] for i in v["onsite"]["items"]}
    assert cats["b"] == "now" and cats["a"] == "silent"
    assert sorted(n["key"] for n in v["radio"]) == ["ble:_", "mesh154:c1"]
    assert [r["name"] for r in v["health"]["receivers"]] == ["Proxy"] and v["health"]["ble_devices"] == 1
    assert v["events"] == [{"platform": "sample_hub", "per_min": 0.1}]


def test_hub_sensors_without_a_snapshot(tmp_path):
    c, _ = make_core(tmp_path)
    iid = add_hub(c)
    v = c.hub_sensors(iid)
    assert v["connected"] is False and v["entities"] == [] and v["radio"] == [] and v["onsite"]["items"] == []


# ---------------------------------------------------------------------- history
def test_hub_history_1h_comes_from_the_ring(world):
    c, iid, ids = world
    now = time.time()
    for i, val in enumerate(("4", "6", "8")):
        c.on_event("int:%d" % iid, iid, {"type": "hub_state", "entity_id": "sensor.tag_x", "state": val, "t": now - 300 + i * 100})
    out = asyncio.run(c.hub_history(iid, "sensor.tag_x", "1h"))
    assert out["kind"] == "numeric" and out["source"] == "live" and out["range"] == "1h"
    assert [p[2] for p in out["points"]] == [4.0, 6.0, 8.0]
    assert out["entity"]["id"] == "sensor.tag_x" and out["entity"]["device"] == "Tag" and out["entity"]["radio"] == "ble"
    assert out["entity"]["fresh"] == "ok" and out["entity"]["state"] == "8"
    lamp = asyncio.run(c.hub_history(iid, "light.b", "1h"))
    assert lamp["kind"] == "state" and lamp["segments"][-1][2] == "on" and lamp["entity"]["area"] is None
    assert asyncio.run(c.hub_history(iid, "light.porch", "1h"))["entity"]["area"] == "Porch"


def test_hub_history_long_range_needs_a_running_source(world):
    c, iid, ids = world
    for rng in ("24h", "7d", "30d"):
        with pytest.raises(ValueError, match="hub.err.offline"):
            asyncio.run(c.hub_history(iid, "sensor.tag_x", rng))


def test_hub_history_rejects_bad_input(world):
    c, iid, ids = world
    for args, err in (((999, "sensor.tag_x", "1h"), "hub.err.missing"), ((iid, "sensor.tag_x", "5y"), "hub.err.range"),
                      ((iid, "sensor.nothing", "1h"), "hub.err.entity"), ((iid, "person.sam", "1h"), "hub.err.entity"),
                      ((iid, "not an id", "1h"), "hub.err.entity")):
        with pytest.raises(ValueError, match=err):
            asyncio.run(c.hub_history(*args))


def test_hub_history_maps_source_errors(world):
    from lanloom.sources._common import SourceError
    c, iid, ids = world

    class Src:
        def __init__(self, key):
            self.key = key

        async def history(self, *a, **kw):
            raise SourceError(self.key, "x")
    for key, want in (("source.hub.busy", "hub.err.busy"), ("source.hub.offline", "hub.err.offline"),
                      ("source.hub.history_item", "hub.err.entity"), ("source.hub.timeout", "hub.err.history")):
        c._hub_sources[iid] = Src(key)
        with pytest.raises(ValueError, match=want):
            asyncio.run(c.hub_history(iid, "sensor.tag_x", "7d"))


# ---------------------------------------------------------------------- live rings
def test_tick_rings_for_radio_devices_and_areas(world):
    c, iid, ids = world
    now = time.time()
    assert "rings" not in c.tick(now)                                             # nothing happened yet
    ev = lambda eid, st: c.on_event("int:%d" % iid, iid, {"type": "hub_state", "entity_id": eid, "state": st, "t": now})
    ev("light.b", "off")
    ev("light.b", "on")
    ev("sensor.tag_x", "7")
    ev("light.porch", "off")
    ev("sensor.unknown_thing", "1")                                               # unknown entity: no lane
    frame = c.tick(now + 1)
    rings = dict((t, n) for t, n in frame["rings"])
    assert rings == {"m:%d:mesh154:c1" % iid: 2, "r:%d:b" % iid: 2, "m:%d:ble:_" % iid: 1, "r:%d:tag" % iid: 1,
                     "h:%d:porch" % iid: 1}
    assert [n for _t, n in frame["rings"]] == sorted((n for _t, n in frame["rings"]), reverse=True)
    assert "rings" not in c.tick(now + 2)                                         # the pending counts are consumed


def test_tick_rings_are_capped_per_lane(world):
    c, iid, ids = world
    now = time.time()
    for _ in range(50):
        c.on_event("int:%d" % iid, iid, {"type": "hub_state", "entity_id": "light.b", "state": "on", "t": now})
    assert dict(c.tick(now)["rings"])["r:%d:b" % iid] == 20
