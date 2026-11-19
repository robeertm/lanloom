"""The night report section (fixed clock): nothing without a state, changes since the last report, checks."""
import copy

from lanloom.integrations.omada import model, report
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


def test_no_state_no_section_and_other_families_are_ignored():
    assert report.night_section(None, None, NOW) is None
    assert report.night_section(None, {"hosts": [], "controller": {}}, NOW) is None
    assert report.night_section(None, {"rule_digest": [], "controller": {"version": "1"}}, NOW) is None
    assert report.night_section(None, {"controller": {"version": "1", "class": "something_else"}}, NOW) is None


def test_first_night_lists_the_checks_only():
    sec = report.night_section(None, state(), NOW, "Site controller")
    assert sec["family"] == "omada" and sec["title"] == "night.ctl.title" and sec["name"] == "Site controller"
    assert keys(sec) == ["fwr.finding.device_offline", "fwr.finding.ssid_open", "fwr.finding.acls_off"]
    assert sec["summary"] == "night.ctl.summary_look" and sec["look"] == 1
    assert sec["params"] == {"n": 1, "devices": 5, "online": 3, "ssids": 4, "rules": 3, "changes": 0}


def test_calm_night_has_no_changes_and_a_calm_summary():
    raw = raw_of()
    raw["devices"][2]["status"] = 1                                                   # the desk switch is back
    raw["ssids"][2]["security"] = 3
    raw["acl_gateway"][1]["status"] = True
    st = state(raw)
    sec = report.night_section(stored(st), st, NOW)
    assert sec["items"] == [] and sec["summary"] == "night.ctl.summary_calm" and sec["params"]["changes"] == 0 and sec["look"] == 0


def test_device_changes():
    before = state()
    raw = raw_of()
    raw["devices"][1]["status"] = 0                                                   # the core switch went down
    raw["devices"][2]["status"] = 1                                                   # the desk switch came back
    raw["devices"][3]["firmwareVersion"] = "1.4.0"
    raw["devices"].pop(4)                                                             # the pending AP was forgotten
    raw["devices"].append({"mac": "02-00-5E-20-00-09", "name": "Shed AP", "type": "ap", "status": 1, "ip": "192.168.10.9"})
    sec = report.night_section(stored(before), state(raw), NOW)
    got = {(i["key"], tuple(sorted(i["params"].items()))) for i in sec["items"]}
    assert ("night.ctl.device_down", (("device", "Core switch"),)) in got
    assert ("night.ctl.device_up", (("device", "Desk switch"),)) in got
    assert ("night.ctl.firmware", (("before", "1.3.1"), ("device", "Hall AP"), ("now", "1.4.0"))) in got
    assert ("night.ctl.device_gone", (("device", "New AP"),)) in got
    assert ("night.ctl.device_new", (("device", "Shed AP"),)) in got
    assert sec["params"]["changes"] == 5


def test_a_device_that_went_down_is_said_once_the_check_stays_for_one_that_was_already_down():
    before = state()
    raw = raw_of()
    raw["devices"][1]["status"] = 0
    sec = report.night_section(stored(before), state(raw), NOW)
    offline = [i["params"]["device"] for i in sec["items"] if i["key"] == "fwr.finding.device_offline"]
    assert offline == ["Desk switch"]                                                  # it was down before: still a check
    assert [i["params"]["device"] for i in sec["items"] if i["key"] == "night.ctl.device_down"] == ["Core switch"]


def test_a_device_that_is_only_waiting_to_be_adopted_is_not_reported_down():
    before_raw = raw_of()
    before_raw["devices"][4]["status"] = 1
    sec = report.night_section(stored(state(before_raw)), state(), NOW)
    assert not any(i["key"] == "night.ctl.device_down" and i["params"].get("device") == "New AP" for i in sec["items"])


def test_wireless_network_changes():
    before = state()
    raw = raw_of()
    raw["ssids"][0]["security"] = 0                                                   # Home lost its encryption
    raw["ssids"].pop(3)                                                               # Lab is gone
    raw["ssids"].append({"id": "ss-new", "name": "Pop-up", "band": 1, "broadcast": True, "security": 0, "vlanEnable": False, "wlan": "Default"})
    sec = report.night_section(stored(before), state(raw), NOW)
    by = {(i["key"], i["params"].get("ssid")): i["level"] for i in sec["items"]}
    assert by[("night.ctl.ssid_changed", "Home")] == "warn"
    assert by[("night.ctl.ssid_gone", "Lab")] == "info"
    assert by[("night.ctl.ssid_new", "Pop-up")] == "warn"                             # a new open network deserves a look


def test_access_rule_changes():
    before = state()
    raw = raw_of()
    raw["acl_gateway"][0]["destinationIds"] = [fx.NET_GUEST]                          # acts differently
    raw["acl_gateway"][1]["status"] = True                                            # switched on
    raw["acl_switch"][0]["status"] = False                                            # switched off
    raw["acl_gateway"].append({"id": "acl-g9", "index": 9, "description": "Let everything in", "policy": 1, "status": True,
                               "sourceType": 0, "sourceIds": [], "destinationType": 0, "destinationIds": [], "protocols": [256]})
    sec = report.night_section(stored(before), state(raw), NOW)
    by = {(i["key"], i["params"].get("rule")): i["level"] for i in sec["items"]}
    assert by[("night.ctl.acl_changed", "Block IoT to LAN")] == "warn"
    assert by[("night.ctl.acl_on", "Allow all")] == "info" and by[("night.ctl.acl_off", "#1")] == "info"
    assert by[("night.ctl.acl_new", "Let everything in")] == "warn"                   # an enabled rule that allows
    removed = raw_of()
    removed["acl_gateway"].pop(0)
    gone = report.night_section(stored(before), state(removed), NOW)
    assert ("night.ctl.acl_gone", "Block IoT to LAN") in {(i["key"], i["params"].get("rule")) for i in gone["items"]}


def test_items_are_ordered_by_importance_and_capped():
    raw = raw_of()
    raw["devices"] = [{"mac": "02-00-5E-20-01-%02X" % n, "name": "AP %d" % n, "type": "ap", "status": 0, "ip": "192.168.10.%d" % (100 + n)}
                      for n in range(60)]
    sec = report.night_section(None, state(raw), NOW)
    assert len(sec["items"]) == report.MAX_ITEMS
    order = [report.LEVELS[i["level"]] for i in sec["items"]]
    assert order == sorted(order)


def test_the_section_is_registered_for_the_night_report():
    from lanloom import integrations
    assert [fn for fam, fn in integrations.night_sections() if fam == "omada"] == [report.night_section]


def test_section_texts_exist_in_both_languages():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[3] / "lanloom" / "i18n"
    sec = report.night_section(stored(state()), state(copy.deepcopy(raw_of())), NOW)
    wanted = {"night.ctl.title", sec["summary"]} | {i["key"] for i in sec["items"]}
    wanted |= {"night.ctl.%s" % k for k in ("summary_calm", "summary_look", "device_down", "device_up", "device_new", "device_gone", "firmware",
                                             "ssid_new", "ssid_gone", "ssid_changed", "acl_new", "acl_gone", "acl_changed", "acl_on", "acl_off")}
    wanted |= {"fwr.finding.%s" % k for k in ("device_offline", "wan_down", "ssid_open", "acls_off")}
    for lang in ("en", "de"):
        d = json.loads((root / ("%s.json" % lang)).read_text(encoding="utf-8"))
        assert not [k for k in wanted if k not in d], lang
