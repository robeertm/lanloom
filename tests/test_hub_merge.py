"""Hub devices merged with network devices (hubmerge.match) and the radio networks of a hub (radio_view, onsite,
health). Pure functions with a fixed clock; names are sample data."""
import datetime

import pytest

from lanloom import hubmerge as hm

NOW = 1_800_000_000.0


def iso(age_s):
    return datetime.datetime.fromtimestamp(NOW - age_s, datetime.timezone.utc).isoformat(timespec="seconds")


def net(did, title="", mac=None, ip=None, **kw):
    return dict({"id": did, "title": title, "mac": mac, "ip": ip, "aliases": [], "hostnames": []}, **kw)


def hubdev(did, name=None, **kw):
    return dict({"id": did, "name": name or did, "service": False, "macs": [], "ip_hint": None, "host_hint": None,
                 "radio": None, "role": None, "via": None, "area": None}, **kw)


def ent(eid, did, state="on", **kw):
    return dict({"entity_id": eid, "device_id": did, "state": state, "domain": eid.split(".", 1)[0], "dc": None, "ls": False}, **kw)


def snap(devices=(), entities=(), trackers=(), areas=()):
    return {"devices": list(devices), "entities": list(entities), "trackers": list(trackers), "areas": list(areas)}


# ---------------------------------------------------------------------- match
def one(devs, hub_snap, rules=None, integ=1):
    return hm.match(devs, {integ: hub_snap}, rules)


def test_mac_match_has_confidence_095():
    res = one([net("n1", "Sample TV", mac="aa:bb:cc:00:00:01")], snap([hubdev("h1", macs=["AA:BB:CC:00:00:01"])]))
    (m,) = res["by_net"]["n1"]
    assert (m["how"], m["confidence"], m["value"], m["manual"], m["integ"], m["device_id"]) == ("mac", 0.95, "AA:BB:CC:00:00:01", False, 1, "h1")
    assert res["by_hub"] == {"1:h1": "n1"} and res["suggest"] == {}


def test_mac_match_through_an_alias_interface_maps_to_the_primary_device():
    devs = [net("n1", "Sample NAS", mac="aa:bb:cc:00:00:01", aliases=[{"mac": "aa:bb:cc:00:00:02"}]),
            net("n2", "Sample NAS wlan", mac="aa:bb:cc:00:00:03", merged_into={"id": "n1"})]
    res = one(devs, snap([hubdev("h1", macs=["aa:bb:cc:00:00:02"]), hubdev("h2", macs=["aa:bb:cc:00:00:03"])]))
    assert res["by_hub"] == {"1:h1": "n1", "1:h2": "n1"}
    assert sorted(m["device_id"] for m in res["by_net"]["n1"]) == ["h1", "h2"]


def test_tracker_match_ip_hint_plus_tracker_mac():
    s = snap([hubdev("h1", ip_hint="192.168.1.50")], trackers=[{"ip": "192.168.1.50", "mac": "AA:BB:CC:00:00:09"}])
    res = one([net("n1", "Sample phone", mac="aa:bb:cc:00:00:09", ip="192.168.1.77")], s)
    (m,) = res["by_net"]["n1"]
    assert (m["how"], m["confidence"], m["value"]) == ("tracker", 0.85, "192.168.1.50")


def test_ip_match_has_confidence_07():
    res = one([net("n1", "Sample printer", ip="192.168.1.60")], snap([hubdev("h1", ip_hint="192.168.1.60")]))
    (m,) = res["by_net"]["n1"]
    assert (m["how"], m["confidence"]) == ("ip", 0.7)


def test_mac_beats_tracker_beats_ip():
    s = snap([hubdev("h1", macs=["aa:bb:cc:00:00:01"], ip_hint="192.168.1.60")], trackers=[{"ip": "192.168.1.60", "mac": "aa:bb:cc:00:00:02"}])
    devs = [net("n1", "One", mac="aa:bb:cc:00:00:01"), net("n2", "Two", mac="aa:bb:cc:00:00:02", ip="192.168.1.60")]
    assert one(devs, s)["by_hub"] == {"1:h1": "n1"}
    s["devices"][0]["macs"] = []
    assert one(devs, s)["by_hub"] == {"1:h1": "n2"}


def test_name_match_is_only_a_suggestion():
    res = one([net("n1", "Garage Door", ip="192.168.1.5")], snap([hubdev("h1", name="garage-door")]))
    assert res["by_net"] == {} and res["by_hub"] == {}
    (m,) = res["suggest"]["n1"]
    assert (m["how"], m["confidence"]) == ("name", 0.45)


def test_name_match_by_host_name_label():
    res = one([net("n1", "x", hostnames=["plug-kitchen.lan"])], snap([hubdev("h1", name="Other", host_hint="plug-kitchen.local")]))
    assert res["suggest"]["n1"][0]["how"] == "name"


def test_name_match_needs_uniqueness_on_both_sides():
    s = snap([hubdev("h1", name="Garage Door")])
    twins = [net("n1", "Garage Door"), net("n2", "garage door")]
    assert one(twins, s)["suggest"] == {}                                           # two network devices with the name
    two_hub = snap([hubdev("h1", name="Garage Door"), hubdev("h2", name="Garage Door")])
    assert one([net("n1", "Garage Door")], two_hub)["suggest"] == {}               # two hub devices with the name
    assert one([net("n1", "Garage Door", hidden=True), net("n2", "Garage Door")], s)["suggest"]["n2"]   # a hidden twin does not count


def test_short_names_never_match():
    assert one([net("n1", "Tv")], snap([hubdev("h1", name="Tv")]))["suggest"] == {}


def test_name_match_not_for_plain_radio_end_devices():
    plain = hubdev("h1", name="Garage Door", radio="mesh154", role="device")
    assert one([net("n1", "Garage Door")], snap([plain]))["suggest"] == {}
    for role in ("coordinator", "receiver", "border_router"):
        anchor = hubdev("h1", name="Garage Door", radio="mesh154", role=role)
        assert one([net("n1", "Garage Door")], snap([anchor]))["suggest"]["n1"][0]["how"] == "name", role


def test_service_devices_are_ignored():
    res = one([net("n1", "Sample TV", mac="aa:bb:cc:00:00:01")], snap([hubdev("h1", macs=["aa:bb:cc:00:00:01"], service=True)]))
    assert res == {"by_net": {}, "by_hub": {}, "suggest": {}}


def test_split_rule_removes_a_match():
    s = snap([hubdev("h1", macs=["aa:bb:cc:00:00:01"])])
    devs = [net("n1", "Sample TV", mac="aa:bb:cc:00:00:01")]
    assert one(devs, s)["by_hub"]
    res = one(devs, s, {"split": [["n1", "1:h1"]], "join": []})
    assert res == {"by_net": {}, "by_hub": {}, "suggest": {}}
    # the rule names an alias interface: the primary device is the one that counts
    devs = [net("n1", "Sample TV"), net("n2", "Sample TV wlan", mac="aa:bb:cc:00:00:01", merged_into={"id": "n1"})]
    assert one(devs, s, {"split": [["n1", "1:h1"]], "join": []})["by_hub"] == {}


def test_join_rule_adds_a_manual_match_without_evidence():
    res = one([net("n1", "Sample TV")], snap([hubdev("h1", name="Unrelated")]), {"split": [], "join": [["n1", "1:h1"]]})
    (m,) = res["by_net"]["n1"]
    assert (m["how"], m["confidence"], m["manual"], m["value"]) == ("manual", 1.0, True, None)
    assert res["by_hub"] == {"1:h1": "n1"}


def test_join_confirms_a_suggestion_and_ignores_unknown_devices_and_garbage():
    s = snap([hubdev("h1", name="Garage Door")])
    res = one([net("n1", "Garage Door")], s, {"split": [], "join": [["n1", "1:h1"], ["nope", "1:h1"], ["n1", "x:h1"], ["n1", "1:"], [1, 2]]})
    assert [m["how"] for m in res["by_net"]["n1"]] == ["manual"] and res["suggest"] == {}


def test_matches_sorted_and_hub_key_helper():
    s = snap([hubdev("h2", macs=["aa:bb:cc:00:00:01"]), hubdev("h1", ip_hint="192.168.1.5")])
    res = one([net("n1", "Sample", mac="aa:bb:cc:00:00:01", ip="192.168.1.5")], s)
    assert [(m["device_id"], m["how"]) for m in res["by_net"]["n1"]] == [("h2", "mac"), ("h1", "ip")]
    assert hm.hub_key(3, "abc") == "3:abc"


def test_match_without_hubs_or_snapshots():
    assert hm.match([net("n1", "x")], {}) == {"by_net": {}, "by_hub": {}, "suggest": {}}
    assert hm.match([net("n1", "x")], {1: None}) == {"by_net": {}, "by_hub": {}, "suggest": {}}


# ---------------------------------------------------------------------- radio_view: networks
def mesh_snap():
    devices = [hubdev("c1", "Stick", radio="mesh154", role="coordinator"),
               hubdev("a", "Lamp A", radio="mesh154", role="device", via="c1"),
               hubdev("b", "Lamp B", radio="mesh154", role="device"),           # no path: the only coordinator takes it
               hubdev("w", "Wlan plug")]
    return snap(devices, [ent("light.a", "a"), ent("light.b", "b")])


def test_devices_group_under_their_coordinator_and_a_lone_coordinator_adopts_strays():
    v = hm.radio_view(mesh_snap(), {}, NOW)
    (n,) = v["networks"]
    assert n["key"] == "mesh154:c1" and n["coordinator"] == "c1" and n["coordinator_name"] == "Stick"
    assert n["members"] == ["a", "b"] and n["devices"] == 2
    assert v["dev_net"] == {"c1": "mesh154:c1", "a": "mesh154:c1", "b": "mesh154:c1"}
    assert "w" not in v["dev_net"] and "w" not in v["state"]


def test_two_coordinators_leave_strays_in_the_underscore_network():
    s = mesh_snap()
    s["devices"].append(hubdev("c2", "Second stick", radio="mesh154", role="coordinator"))
    v = hm.radio_view(s, {}, NOW)
    assert sorted(n["key"] for n in v["networks"]) == ["mesh154:_", "mesh154:c1", "mesh154:c2"]
    assert v["dev_net"]["b"] == "mesh154:_" and v["dev_net"]["a"] == "mesh154:c1"
    under = next(n for n in v["networks"] if n["key"] == "mesh154:_")
    assert under["coordinator"] is None and under["members"] == ["b"]


def test_via_chain_through_a_router_device_reaches_the_coordinator():
    devices = [hubdev("c1", "Stick", radio="subghz", role="coordinator"),
               hubdev("r", "Router node", radio="subghz", role="device", via="c1"),
               hubdev("e", "End node", radio="subghz", role="device", via="r")]
    v = hm.radio_view(snap(devices), {}, NOW)
    assert v["dev_net"] == {"c1": "subghz:c1", "r": "subghz:c1", "e": "subghz:c1"}


def test_via_loop_does_not_hang():
    devices = [hubdev("x", radio="mesh154", role="device", via="y"), hubdev("y", radio="mesh154", role="device", via="x")]
    v = hm.radio_view(snap(devices), {}, NOW)
    assert set(v["dev_net"]) == {"x", "y"}


@pytest.mark.parametrize("kind", ["ble", "thread", "ipstd"])
def test_receiver_kinds_always_form_one_underscore_network(kind):
    anchor_role = {"ble": "receiver", "thread": "border_router", "ipstd": "device"}[kind]
    devices = [hubdev("p1", "Proxy 1", radio=kind, role=anchor_role), hubdev("p2", "Proxy 2", radio=kind, role=anchor_role),
               hubdev("d1", "Thing", radio=kind, role="device", via="p1")]
    ents = [ent("sensor.t", "d1", "5"), ent("sensor.p1", "p1"), ent("sensor.p2", "p2")]
    (n,) = hm.radio_view(snap(devices, ents), {}, NOW)["networks"]
    assert n["key"] == kind + ":_" and n["coordinator"] is None and n["coordinator_fresh"] is None
    assert sorted(n["members"]) == ["d1", "p1", "p2"]
    if anchor_role != "device":
        assert n["anchors"] == 2 and n["devices"] == 1


def test_receivers_and_border_routers_count_as_anchors_not_devices():
    devices = [hubdev("p1", "Proxy", radio="ble", role="receiver"), hubdev("d1", "Tag", radio="ble", role="device")]
    ents = [ent("sensor.tag", "d1", "1"), ent("sensor.proxy", "p1", "unavailable")]
    (n,) = hm.radio_view(snap(devices, ents), {}, NOW)["networks"]
    assert (n["anchors"], n["anchors_silent"], n["devices"], n["silent"]) == (1, 1, 1, 0)
    assert n["health"] == "bad"                                    # every anchor is silent


def test_unknown_radio_kind_is_skipped_and_service_devices_too():
    devices = [hubdev("x", radio="weird", role="device"), hubdev("s", radio="ble", role="device", service=True)]
    v = hm.radio_view(snap(devices), {}, NOW)
    assert v["networks"] == [] and v["dev_net"] == {}


def test_networks_are_ordered_by_kind():
    devices = [hubdev("m", radio="ipstd", role="device"), hubdev("c", radio="mesh154", role="coordinator"),
               hubdev("b", radio="ble", role="receiver")]
    assert [n["kind"] for n in hm.radio_view(snap(devices), {}, NOW)["networks"]] == ["mesh154", "ble", "ipstd"]


# ---------------------------------------------------------------------- freshness
def view_of(entities, states=None, now=NOW):
    s = snap([hubdev("c1", "Stick", radio="mesh154", role="coordinator"), hubdev("a", "Lamp A", radio="mesh154", via="c1", role="device")],
             entities)
    return hm.radio_view(s, states or {}, now)["state"]["a"]


def test_all_entities_unavailable_is_silent():
    st = view_of([ent("light.a", "a", "unavailable"), ent("sensor.a_x", "a", "unavailable")])
    assert (st["fresh"], st["available"], st["grade"]) == ("silent", False, "interrupted")


def test_last_seen_older_than_a_day_is_silent_even_when_entities_answer():
    st = view_of([ent("light.a", "a", "on"), ent("sensor.a_ls", "a", iso(hm.SILENT_S + 60), ls=True)])
    assert st["fresh"] == "silent" and st["last_seen_s"] == hm.SILENT_S + 60 and st["grade"] == "interrupted"


def test_recent_last_seen_is_ok_with_grade_near():
    st = view_of([ent("light.a", "a", "on"), ent("sensor.a_ls", "a", iso(120), ls=True)])
    assert (st["fresh"], st["grade"], st["last_seen_s"], st["available"]) == ("ok", "near", 120, True)


@pytest.mark.parametrize("age, grade", [(3599, "near"), (3600, "periodic"), (6 * 3600 - 1, "periodic"), (6 * 3600, "sluggish"),
                                        (hm.SILENT_S, "sluggish")])
def test_grade_by_last_seen_age(age, grade):
    st = view_of([ent("sensor.a_ls", "a", iso(age), ls=True)])
    assert st["grade"] == grade and st["fresh"] == "ok"


def test_no_last_seen_is_periodic_and_no_entities_is_unknown():
    st = view_of([ent("light.a", "a", "on")])
    assert (st["fresh"], st["grade"], st["last_seen_s"]) == ("ok", "periodic", None)
    st = view_of([])
    assert (st["fresh"], st["grade"], st["available"]) == ("unknown", "unknown", None)


def test_unreadable_last_seen_values_are_ignored():
    for bad in ("unknown", "unavailable", "", "not a time", "x" * 50):
        st = view_of([ent("light.a", "a", "on"), ent("sensor.a_ls", "a", bad, ls=True)])
        assert st["fresh"] == "ok" and st["last_seen_s"] is None, bad


def test_live_states_override_the_snapshot():
    ents = [ent("light.a", "a", "unavailable")]
    assert view_of(ents)["fresh"] == "silent"
    assert view_of(ents, {"light.a": ("on", NOW - 5)})["fresh"] == "ok"
    ents = [ent("light.a", "a", "on"), ent("sensor.a_ls", "a", iso(10 * 3600), ls=True)]
    assert view_of(ents)["fresh"] == "ok"
    assert view_of(ents, {"sensor.a_ls": (iso(hm.SILENT_S + 5), NOW)})["fresh"] == "silent"


def test_age_of_the_last_update_is_never_used():
    st = view_of([ent("light.a", "a", "on")], {"light.a": ("on", NOW - 10 * hm.SILENT_S)})
    assert st["fresh"] == "ok"


def test_last_seen_without_zone_is_read_as_utc_and_future_times_count_as_now():
    naive = datetime.datetime.fromtimestamp(NOW - 300, datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    assert view_of([ent("sensor.a_ls", "a", naive, ls=True)])["last_seen_s"] == 300
    assert view_of([ent("sensor.a_ls", "a", iso(-500), ls=True)])["last_seen_s"] == 0


def test_the_newest_of_several_last_seen_entities_counts():
    st = view_of([ent("sensor.a_ls", "a", iso(500), ls=True), ent("sensor.a_ls2", "a", iso(9000), ls=True)])
    assert st["last_seen_s"] == 500


# ---------------------------------------------------------------------- health of a network
def health_of(stick_state, lamp_states):
    devices = [hubdev("c1", "Stick", radio="mesh154", role="coordinator")]
    ents = [ent("sensor.stick", "c1", stick_state)]
    for i, state in enumerate(lamp_states):
        devices.append(hubdev("l%d" % i, "Lamp %d" % i, radio="mesh154", role="device", via="c1"))
        ents.append(ent("light.l%d" % i, "l%d" % i, state))
    (n,) = hm.radio_view(snap(devices, ents), {}, NOW)["networks"]
    return n


def test_health_ok_warn_bad():
    assert health_of("online", ["on", "on", "on", "on"])["health"] == "ok"
    n = health_of("online", ["on", "on", "on", "unavailable"])
    assert (n["health"], n["silent"], n["devices"]) == ("warn", 1, 4)
    assert health_of("online", ["on", "unavailable"])["health"] == "bad"                 # half silent
    assert health_of("online", ["unavailable", "unavailable", "on"])["health"] == "bad"
    n = health_of("unavailable", ["on", "on"])
    assert (n["health"], n["coordinator_fresh"]) == ("bad", "silent")                     # coordinator silent


def test_battery_count_of_a_network():
    devices = [hubdev("c1", radio="mesh154", role="coordinator"), hubdev("l", "Lamp", radio="mesh154", via="c1")]
    ents = [ent("sensor.l_b", "l", "7", dc="battery", domain="sensor")]
    (n,) = hm.radio_view(snap(devices, ents), {}, NOW)["networks"]
    assert n["battery"] == 1


# ---------------------------------------------------------------------- onsite
def onsite_snap():
    devices = [hubdev("c1", "Stick", radio="mesh154", role="coordinator"),
               hubdev("crit", "Door sensor", radio="mesh154", via="c1", area="hall"),
               hubdev("low", "Binary low", radio="mesh154", via="c1"),
               hubdev("soon", "Window sensor", radio="mesh154", via="c1"),
               hubdev("gone", "Silent plug", radio="mesh154", via="c1"),
               hubdev("both", "Silent and weak", radio="mesh154", via="c1"),
               hubdev("fine", "Healthy lamp", radio="mesh154", via="c1"),
               hubdev("wlan", "Wlan clock"),
               hubdev("wbat", "Wlan remote"),
               hubdev("rx", "Proxy", radio="ble", role="receiver")]
    ents = [ent("sensor.crit_b", "crit", "7", dc="battery"), ent("sensor.crit_x", "crit", "1"),
            ent("binary_sensor.low_b", "low", "on", dc="battery"),
            ent("sensor.soon_b", "soon", "18", dc="battery"),
            ent("light.gone", "gone", "unavailable"),
            ent("sensor.both_b", "both", "unavailable", dc="battery"), ent("light.both", "both", "unavailable"),
            ent("light.fine", "fine", "on"), ent("sensor.fine_b", "fine", "90", dc="battery"),
            ent("sensor.wlan_x", "wlan", "1"),
            ent("sensor.wbat_b", "wbat", "22", dc="battery"),
            ent("sensor.rx_x", "rx", "unavailable"), ent("sensor.c1_x", "c1", "online")]
    return snap(devices, ents, areas=[{"id": "hall", "name": "Hall"}])


def onsite_of(s, states=None):
    states = states or {}
    return hm.onsite(s, states, hm.radio_view(s, states, NOW), NOW)


def test_onsite_categories_and_sort_order():
    res = onsite_of(onsite_snap())
    cats = {i["device_id"]: i["cat"] for i in res["items"]}
    assert cats == {"crit": "now", "low": "now", "soon": "soon", "wbat": "soon", "gone": "silent", "both": "silent", "fine": "ok"}
    # no radio and no battery, or an anchor/coordinator without battery: not on the list
    assert "wlan" not in cats and "rx" not in cats and "c1" not in cats
    order = [hm.SEVERITY[i["cat"]] for i in res["items"]]
    assert order == sorted(order) and hm.SEVERITY["now"] < hm.SEVERITY["silent"] < hm.SEVERITY["soon"] < hm.SEVERITY["ok"]
    assert res["counts"] == {"now": 2, "soon": 2, "silent": 2, "ok": 1}


def test_a_silent_device_with_a_weak_battery_is_urgent():
    s = onsite_snap()
    # the plug's "last seen" is two days old and it reports a battery of 20 %
    s["entities"].append(ent("sensor.gone_b", "gone", "20", dc="battery"))
    s["entities"].append(ent("sensor.gone_ls", "gone", iso(2 * hm.SILENT_S), ls=True))
    gone = next(i for i in onsite_of(s)["items"] if i["device_id"] == "gone")
    assert gone["cat"] == "now" and "silent" in gone["reasons"] and "battery" in gone["reasons"] and gone["battery"] == 20
    # a silent device without any battery value stays "silent"
    both = next(i for i in onsite_of(onsite_snap())["items"] if i["device_id"] == "both")
    assert both["cat"] == "silent" and both["battery"] is None


def test_battery_thresholds_inclusive():
    for pct, cat in ((10, "now"), (11, "soon"), (25, "soon"), (26, "ok")):
        s = snap([hubdev("c1", radio="mesh154", role="coordinator"), hubdev("d", "Dev", radio="mesh154", via="c1")],
                 [ent("sensor.d_b", "d", str(pct), dc="battery")])
        (item,) = onsite_of(s)["items"]
        assert item["cat"] == cat and item["battery"] == pct, pct


def test_battery_values_outside_0_to_100_and_non_numbers_are_ignored():
    for bad in ("120", "-3", "abc", "nan", "inf"):
        s = snap([hubdev("c1", radio="mesh154", role="coordinator"), hubdev("d", "Dev", radio="mesh154", via="c1")],
                 [ent("sensor.d_b", "d", bad, dc="battery"), ent("light.d", "d", "on")])
        (item,) = onsite_of(s)["items"]
        assert item["battery"] is None and item["cat"] == "ok", bad


def test_lowest_battery_wins_and_binary_low_sets_the_flag():
    s = onsite_snap()
    s["entities"].append(ent("sensor.crit_b2", "crit", "40", dc="battery"))
    crit = next(i for i in onsite_of(s)["items"] if i["device_id"] == "crit")
    assert crit["battery"] == 7
    low = next(i for i in onsite_of(s)["items"] if i["device_id"] == "low")
    assert low["battery_low"] is True and low["battery"] is None and low["reasons"] == ["low"]


def test_onsite_item_fields_and_area_name_and_live_state():
    s = onsite_snap()
    res = onsite_of(s)
    crit = next(i for i in res["items"] if i["device_id"] == "crit")
    assert (crit["name"], crit["area"], crit["radio"], crit["fresh"]) == ("Door sensor", "Hall", "mesh154", "ok")
    assert res["thresholds"] == {"now": 10, "soon": 25, "silent_h": 24}
    res = onsite_of(s, {"sensor.crit_b": ("60", NOW)})                          # a live value replaces the snapshot value
    assert next(i for i in res["items"] if i["device_id"] == "crit")["cat"] == "ok"


def test_onsite_sort_by_battery_then_name():
    devices = [hubdev("c1", radio="mesh154", role="coordinator")] + [hubdev(n, n.upper(), radio="mesh154", via="c1") for n in "abc"]
    ents = [ent("sensor.a_b", "a", "9", dc="battery"), ent("sensor.b_b", "b", "3", dc="battery"), ent("sensor.c_b", "c", "9", dc="battery")]
    assert [i["device_id"] for i in onsite_of(snap(devices, ents))["items"]] == ["b", "a", "c"]


def test_onsite_without_radio_or_snapshot():
    assert onsite_of(None)["items"] == [] and onsite_of({})["counts"] == {"now": 0, "soon": 0, "silent": 0, "ok": 0}


# ---------------------------------------------------------------------- health()
def test_health_counts_receivers_border_routers_and_ip_standard():
    devices = [hubdev("p1", "Proxy B", radio="ble", role="receiver", sw="1.0", model="P"),
               hubdev("p2", "Proxy A", radio="ble", role="receiver"),
               hubdev("t", "Tag", radio="ble", role="device"),
               hubdev("br", "Border", radio="thread", role="border_router"),
               hubdev("i1", "Std 1", radio="ipstd", role="device"), hubdev("i2", "Std 2", radio="ipstd", role="device"),
               hubdev("i3", "Std 3", radio="ipstd", role="device")]
    ents = [ent("sensor.p1", "p1", "on"), ent("sensor.p2", "p2", "unavailable"), ent("sensor.br", "br", "on"),
            ent("sensor.i1", "i1", "unavailable"), ent("sensor.i2", "i2", "on")]
    s = snap(devices, ents)
    h = hm.health(s, hm.radio_view(s, {}, NOW))
    assert [r["name"] for r in h["receivers"]] == ["Proxy A", "Proxy B"]
    assert [r["fresh"] for r in h["receivers"]] == ["silent", "ok"]
    assert [r["name"] for r in h["border_routers"]] == ["Border"]
    assert h["ble_devices"] == 1
    assert h["ipstd"] == {"devices": 3, "silent": 1}
    assert next(r for r in h["receivers"] if r["device_id"] == "p1")["sw"] == "1.0"


def test_health_of_nothing():
    assert hm.health(None, None) == {"receivers": [], "ble_devices": 0, "border_routers": [], "ipstd": {"devices": 0, "silent": 0}}
