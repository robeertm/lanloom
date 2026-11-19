"""The night report section (fixed clock): nothing without a state, changes since the last report, checks."""
import copy

from lanloom.integrations.mikrotik import model, report
from lanloom.nightly import contrib

from . import fixtures as fx
from .test_model import raw_of

NOW = 1_800_000_000.0


def state(raw=None):
    return model.to_neutral(raw or raw_of())


def stored(st):
    """What the night report keeps of a state for the next night."""
    return {k: v for k, v in st.items() if k not in contrib.FAMILY_STATE_DROP}


def keys(sec):
    return [i["key"] for i in sec["items"]]


def section(raw_now, raw_before=None):
    return report.night_section(stored(state(raw_before)) if raw_before is not False else None, state(raw_now), NOW, "Edge router")


def calm_raw():
    raw = raw_of()
    raw["filter"][2]["disabled"] = "false"                         # no rule switched off
    raw["pppoe_clients"][0]["running"] = "true"                    # the standby line is connected
    return raw


# ---------------------------------------------------------------------------- nothing
def test_no_state_no_section_and_other_families_are_ignored():
    assert report.night_section(None, None, NOW) is None
    assert report.night_section(None, {"hosts": [], "controller": {}}, NOW) is None
    assert report.night_section(None, {"controller": {"version": "1", "class": "access_controller"}}, NOW) is None
    assert report.night_section(None, {"controller": {"version": "1", "class": "home_router"}}, NOW) is None
    assert report.night_section(None, {"rule_digest": []}, NOW) is None


def test_first_night_lists_the_checks_only():
    sec = report.night_section(None, state(), NOW, "Edge router")
    assert sec["family"] == "mikrotik" and sec["title"] == "night.rtr.title" and sec["name"] == "Edge router"
    assert keys(sec) == ["fwr.finding.wan_down", "fwr.finding.rules_disabled"]
    assert sec["summary"] == "night.rtr.summary_look" and sec["look"] == 1
    assert sec["params"] == {"n": 1, "clients": 6, "wans": 2, "wans_up": 1, "rules": 4, "forwards": 2, "changes": 0}


def test_calm_night_has_no_changes_and_a_calm_summary():
    raw = calm_raw()
    sec = section(raw, raw)
    assert sec["items"] == [] and sec["summary"] == "night.rtr.summary_calm" and sec["look"] == 0 and sec["params"]["changes"] == 0


def test_the_section_is_the_same_for_a_stored_state_and_a_fresh_one():
    raw = calm_raw()
    st = state(raw)
    assert report.night_section(stored(st), st, NOW)["items"] == report.night_section(st, st, NOW)["items"] == []


# ---------------------------------------------------------------------------- the router and its lines
def test_restart_and_firmware_change():
    raw = calm_raw()
    now = copy.deepcopy(raw)
    now["resource"]["uptime"] = "2h"
    now["resource"]["version"] = "7.17 (stable)"
    sec = section(now, raw)
    got = {(i["key"], tuple(sorted(i["params"].items()))) for i in sec["items"]}
    assert ("night.rtr.router_restarted", (("device", "edge-router"),)) in got
    assert ("night.rtr.firmware", (("before", fx.VERSION), ("device", "edge-router"), ("now", "7.17"))) in got
    assert sec["params"]["changes"] == 2 and sec["look"] == 1


def test_a_longer_uptime_or_a_small_difference_is_no_restart():
    raw = calm_raw()
    now = copy.deepcopy(raw)
    now["resource"]["uptime"] = "2w"
    assert section(now, raw)["items"] == []
    now["resource"]["uptime"] = "1w2d3h3m50s"                       # ten seconds less: clock noise, not a restart
    assert section(now, raw)["items"] == []


def test_a_line_that_went_down_is_said_once_as_a_change_not_again_as_a_check():
    raw = calm_raw()
    now = copy.deepcopy(raw)
    now["routes"][0]["active"] = "false"
    now["routes"][0]["inactive"] = "true"
    now["dhcp_clients"][0]["status"] = "searching..."
    sec = section(now, raw)
    assert [i["key"] for i in sec["items"] if i["key"].endswith("wan_down")] == ["night.rtr.wan_down"]
    down = by_key(sec, "night.rtr.wan_down")
    assert down["level"] == "alert" and down["params"] == {"wan": "ether1"}
    assert sec["look"] >= 1 and sec["summary"] == "night.rtr.summary_look"


def test_a_line_that_stays_down_stays_a_check():
    raw = raw_of()                                                      # the standby line is down in both nights
    sec = section(raw, raw)
    assert "fwr.finding.wan_down" in keys(sec) and "night.rtr.wan_down" not in keys(sec)


def test_a_line_that_came_back():
    before = raw_of()
    now = copy.deepcopy(before)
    now["pppoe_clients"][0]["running"] = "true"
    sec = section(now, before)
    assert by_key(sec, "night.rtr.wan_up")["params"] == {"wan": "pppoe-backup"} and by_key(sec, "night.rtr.wan_up")["level"] == "info"
    assert "fwr.finding.wan_down" not in keys(sec)


def test_neighbours_appearing_and_vanishing():
    before = calm_raw()
    now = copy.deepcopy(before)
    now["neighbors"] = [n for n in now["neighbors"] if n["identity"] != "hall-ap"]
    now["neighbors"].append({"interface": "ether3", "address4": "10.77.0.8", "mac-address": "CC:2D:E0:00:00:08", "identity": "shed-switch",
                             "platform": "MikroTik", "board": "CSS610", "version": "2.0"})
    sec = section(now, before)
    assert by_key(sec, "night.rtr.neighbour_gone")["params"] == {"device": "hall-ap"} and by_key(sec, "night.rtr.neighbour_gone")["level"] == "warn"
    assert by_key(sec, "night.rtr.neighbour_new")["params"] == {"device": "shed-switch"}
    assert sec["params"]["changes"] == 2


# ---------------------------------------------------------------------------- rules and forwards
def test_rule_changes_added_removed_changed_switched():
    before = calm_raw()
    now = copy.deepcopy(before)
    now["filter"][0]["disabled"] = "true"                           # established: off
    now["filter"][1]["in-interface"] = "ether5"                     # drop from wan: acts differently
    now["filter"].pop(3)                                            # the invalid-state drop is gone
    now["filter"].append({".id": "*B9", "chain": "input", "action": "accept", "dst-port": "22", "comment": "ssh open"})
    sec = section(now, before)
    got = {(i["key"], i["params"]["rule"]) for i in sec["items"] if "rule" in i["params"]}
    assert ("night.rtr.rule_off", "established") in got
    assert ("night.rtr.rule_changed", "drop from wan") in got
    assert ("night.rtr.rule_gone", "#4 forward drop") in got
    assert ("night.rtr.rule_new", "ssh open") in got
    assert by_key(sec, "night.rtr.rule_new")["level"] == "warn" and by_key(sec, "night.rtr.rule_changed")["level"] == "warn"
    assert by_key(sec, "night.rtr.rule_off")["level"] == "info" and by_key(sec, "night.rtr.rule_gone")["level"] == "info"
    assert sec["params"]["changes"] == 4


def test_a_rule_switched_on_again_and_a_new_blocking_rule_is_only_a_note():
    before = raw_of()                                               # "temporary open" is off
    now = copy.deepcopy(before)
    now["filter"][2]["disabled"] = "false"
    now["filter"].append({".id": "*B8", "chain": "forward", "action": "drop", "src-address": "10.77.0.77", "comment": "block one"})
    sec = section(now, before)
    assert by_key(sec, "night.rtr.rule_on")["params"] == {"rule": "temporary open"}
    assert by_key(sec, "night.rtr.rule_new")["level"] == "info"


def test_renaming_a_rule_is_no_change():
    before = calm_raw()
    now = copy.deepcopy(before)
    now["filter"][1]["comment"] = "another name"
    sec = section(now, before)
    assert not [k for k in keys(sec) if k.startswith("night.rtr.rule")] and sec["params"]["changes"] == 0


def test_port_forwarding_added_and_removed():
    before = calm_raw()
    now = copy.deepcopy(before)
    now["nat"].pop(1)                                               # "nas web" removed
    now["nat"].append({".id": "*C9", "chain": "dstnat", "action": "dst-nat", "protocol": "tcp", "dst-port": "2222",
                       "to-addresses": "10.77.0.60", "to-ports": "22", "comment": "ssh in"})
    sec = section(now, before)
    assert by_key(sec, "night.rtr.forward_new")["params"] == {"rule": "ssh in"} and by_key(sec, "night.rtr.forward_new")["level"] == "warn"
    assert by_key(sec, "night.rtr.forward_gone")["params"] == {"rule": "nas web"}


def test_a_new_forward_that_is_switched_off_is_not_reported():
    before = calm_raw()
    now = copy.deepcopy(before)
    now["nat"].append({".id": "*C9", "chain": "dstnat", "action": "dst-nat", "protocol": "tcp", "dst-port": "2222",
                       "to-addresses": "10.77.0.60", "disabled": "true"})
    assert "night.rtr.forward_new" not in keys(section(now, before))


# ---------------------------------------------------------------------------- devices
def test_new_devices_are_a_note_with_the_first_names():
    before = calm_raw()
    now = copy.deepcopy(before)
    for n in range(5):
        mac = "02:00:5E:40:00:%02X" % n
        now["leases"].append({"mac-address": mac, "address": "10.77.0.%d" % (150 + n), "host-name": "guest-%d" % n, "dynamic": "true",
                              "status": "bound"})
    sec = section(now, before)
    item = by_key(sec, "night.rtr.hosts_new")
    assert item["level"] == "info" and item["params"]["n"] == 5
    assert item["params"]["names"].count(",") == report.NAMES_SHOWN - 1
    assert sec["look"] == 0 and sec["summary"] == "night.rtr.summary_calm"


def test_no_baseline_no_new_devices_and_unnamed_devices_have_no_names():
    raw = calm_raw()
    assert "night.rtr.hosts_new" not in keys(report.night_section(None, state(raw), NOW))
    prev = stored(state(raw))
    prev.pop("known_hosts")
    assert "night.rtr.hosts_new" not in keys(report.night_section(prev, state(raw), NOW))
    now = copy.deepcopy(raw)
    now["arp"].append({"address": "10.77.0.140", "mac-address": "02:00:5E:40:00:77", "interface": "bridge", "status": "reachable", "complete": "true"})
    item = by_key(section(now, raw), "night.rtr.hosts_new")
    assert item["params"] == {"n": 1}


def test_the_previous_state_of_another_family_counts_as_no_previous_state():
    st = state(calm_raw())
    sec = report.night_section({"controller": {"class": "home_router"}, "wans": []}, st, NOW)
    assert sec["params"]["changes"] == 0


def test_items_are_sorted_most_important_first_and_capped():
    raw = calm_raw()
    now = copy.deepcopy(raw)
    for n in range(60):
        now["filter"].append({".id": "*Z%d" % n, "chain": "input", "action": "accept", "dst-port": str(1000 + n)})
    sec = section(now, raw)
    levels = [report.LEVELS[i["level"]] for i in sec["items"]]
    assert levels == sorted(levels) and len(sec["items"]) == report.MAX_ITEMS
    assert sec["params"]["changes"] == 60


def by_key(sec, key):
    return next(i for i in sec["items"] if i["key"] == key)
