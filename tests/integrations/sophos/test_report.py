"""Night report section of the firewall family (pure; fixed clock)."""
import copy
import json
from pathlib import Path

import pytest

from lanloom.integrations.sophos import model, report
from lanloom.nightly.contrib import FAMILY_STATE_DROP

from . import fixtures as fx

NOW = 1_800_000_000.0
ORDER = {"alert": 0, "warn": 1, "info": 2}


def base_raw():
    meta, ents = model.parse_reply(fx.REPLY)
    return {"api_version": meta["api_version"], "entities": ents}


def mutate(fn):
    raw = copy.deepcopy(base_raw())
    fn(raw["entities"])
    return model.to_neutral(raw)


def stored(state):
    """A state the way the night report keeps it between two nights."""
    return {k: v for k, v in state.items() if k not in FAMILY_STATE_DROP}


def rule(ents, name):
    return next(r for r in ents["FirewallRule"] if r["Name"] == name)


def keys(sec, key):
    return [i for i in sec["items"] if i["key"] == key]


@pytest.fixture(scope="module")
def state():
    return model.to_neutral(base_raw())


# ---------------------------------------------------------------------------- not a firewall
@pytest.mark.parametrize("st", [None, {}, [], "x", {"hosts": [{"name": "h"}], "guests": []}, {"zones": []}])
def test_no_section_without_a_firewall_state(st):
    assert report.night_section(None, st, NOW, "x") is None
    assert report.night_section({"rule_digest": []}, st, NOW, "x") is None


# ---------------------------------------------------------------------------- first report
def test_first_report_lists_findings_only(state):
    sec = report.night_section(None, state, NOW, "fw")
    assert sec["family"] == "sophos" and sec["title"] == "night.fwr.title" and sec["name"] == "fw"
    assert {i["key"] for i in sec["items"]} == {"fwr.finding.admin_on_wan", "fwr.finding.wan_inbound_open", "fwr.finding.rules_disabled"}
    assert sec["look"] == 2 and sec["summary"] == "night.fwr.summary_look"
    assert sec["params"] == {"n": 2, "rules": 8, "changes": 0, "zones": 5}
    levels = [i["level"] for i in sec["items"]]
    assert levels == sorted(levels, key=ORDER.get)


def test_calm_when_nothing_to_look_at():
    st = mutate(lambda e: (e.update(Zone=[z for z in e["Zone"] if z["Name"] != "WAN"][:1] + [
        {"Name": "WAN", "Type": "WAN", "ApplianceAccess": {}}]),
        e.update(FirewallRule=[r for r in e["FirewallRule"] if r["Name"] != "Remote desk from outside"])))
    sec = report.night_section(None, st, NOW, None)
    assert sec["look"] == 0 and sec["summary"] == "night.fwr.summary_calm" and sec["params"]["n"] == 0
    assert [i["key"] for i in sec["items"]] == ["fwr.finding.rules_disabled"]


def test_previous_of_another_kind_counts_as_no_previous(state):
    sec = report.night_section({"hosts": [1], "guests": []}, state, NOW, "fw")
    assert sec["params"]["changes"] == 0


# ---------------------------------------------------------------------------- second report
def _changed():
    def f(e):
        extra = copy.deepcopy(rule(e, "LAN to internet"))
        extra["Name"] = "Brand new"
        e["FirewallRule"].append(extra)                                           # added (enabled allow -> warn)
        e["FirewallRule"] = [r for r in e["FirewallRule"] if r["Name"] != "Staff browsing"]            # removed
        rule(e, "Inside to DMZ")["Status"] = "Disable"                            # switched off
        rule(e, "Drop things to DMZ")["NetworkPolicy"]["Services"] = {"Service": "HTTP"}               # changed
        rule(e, "Old test rule")["Status"] = "Enable"                             # switched on
        rule(e, "Old test rule")["NetworkPolicy"]["Action"] = "Drop"
    return f


def _pair():
    prev_state = model.to_neutral(base_raw())
    raw = copy.deepcopy(base_raw())
    _changed()(raw["entities"])
    raw["entities"]["NATRule"] = raw["entities"]["NATRule"] + [{
        "Name": "Camera", "Status": "Enable", "TranslatedDestination": "nas", "TranslatedService": "Original",
        "OriginalServices": {"Service": "HTTP"}, "OriginalDestinationNetworks": {"Network": "#Port2"}}]
    return prev_state, model.to_neutral(raw)


@pytest.mark.parametrize("keep_all", [True, False])
def test_second_report_names_every_change(keep_all):
    prev, now = _pair()
    sec = report.night_section(prev if keep_all else stored(prev), now, NOW, "fw")
    assert [i["params"]["rule"] for i in keys(sec, "night.fwr.rule_new")] == ["Brand new"]
    assert [i["params"]["rule"] for i in keys(sec, "night.fwr.rule_gone")] == ["Staff browsing"]
    assert [i["params"]["rule"] for i in keys(sec, "night.fwr.rule_off")] == ["Inside to DMZ"]
    assert [i["params"]["rule"] for i in keys(sec, "night.fwr.rule_on")] == ["Old test rule"]
    assert [i["params"]["rule"] for i in keys(sec, "night.fwr.rule_changed")] == ["Drop things to DMZ"]
    assert [i["params"]["rule"] for i in keys(sec, "night.fwr.forward_new")] == ["Camera"]
    assert not keys(sec, "night.fwr.forward_gone")
    levels = {i["key"]: i["level"] for i in sec["items"]}
    assert levels["night.fwr.rule_new"] == "warn" and levels["night.fwr.rule_changed"] == "warn"
    assert levels["night.fwr.rule_gone"] == "info" and levels["night.fwr.rule_off"] == "info" and levels["night.fwr.forward_new"] == "warn"
    assert sec["params"]["changes"] == 6 and sec["params"]["rules"] == 8
    got = [i["level"] for i in sec["items"]]
    assert got == sorted(got, key=ORDER.get)
    assert sec["summary"] == "night.fwr.summary_look" and sec["look"] == sum(1 for i in sec["items"] if i["level"] != "info")


def test_stored_previous_has_no_policy_list_but_still_works():
    prev, now = _pair()
    s = stored(prev)
    assert "policies" not in s and "raw" not in s and s["rule_digest"]
    assert keys(report.night_section(s, now, NOW, None), "night.fwr.rule_changed")


def test_forward_gone_and_new_disabled_rule_is_only_info():
    _, prev = _pair()                                  # the state with the extra forward is the earlier one
    now = model.to_neutral(base_raw())
    sec = report.night_section(stored(prev), now, NOW, None)
    assert [i["params"]["rule"] for i in keys(sec, "night.fwr.forward_gone")] == ["Camera"]
    assert keys(sec, "night.fwr.forward_gone")[0]["level"] == "info"
    quiet = mutate(lambda e: e["FirewallRule"].append(dict(copy.deepcopy(rule(e, "LAN to internet")), Name="Parked", Status="Disable")))
    sec = report.night_section(model.to_neutral(base_raw()), quiet, NOW, None)
    assert keys(sec, "night.fwr.rule_new")[0]["level"] == "info"


def test_unchanged_state_has_no_change_items(state):
    sec = report.night_section(stored(state), state, NOW, "fw")
    assert sec["params"]["changes"] == 0 and not [i for i in sec["items"] if i["key"].startswith("night.")]


def test_items_are_capped_and_section_is_json_clean():
    prev = model.to_neutral({"entities": {}})
    ents = {"FirewallRule": [{"Name": "r%d" % i, "Status": "Enable", "NetworkPolicy": {"Action": "Accept"}} for i in range(100)]}
    sec = report.night_section(stored(prev), model.to_neutral({"entities": ents}), NOW, None)
    assert len(sec["items"]) == report.MAX_ITEMS and sec["params"]["changes"] == 100
    json.dumps(sec)


def test_report_does_not_read_the_clock(state, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: (_ for _ in ()).throw(AssertionError("clock used")))
    report.night_section(None, state, NOW, None)


# ---------------------------------------------------------------------------- i18n
REPO = Path(__file__).resolve().parents[3]
KEYS = ["night.fwr.title", "night.fwr.summary_look", "night.fwr.summary_calm", "night.fwr.rule_new", "night.fwr.rule_gone",
        "night.fwr.rule_on", "night.fwr.rule_off", "night.fwr.rule_changed", "night.fwr.forward_new", "night.fwr.forward_gone",
        "fwr.finding.admin_on_wan", "fwr.finding.wan_inbound_open", "fwr.finding.any_any", "fwr.finding.rules_disabled",
        "source.family.api_ip", "source.family.api_off"]


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_key_exists_in_both_languages(lang):
    table = json.loads((REPO / "lanloom" / "i18n" / ("%s.json" % lang)).read_text(encoding="utf-8"))
    missing = [k for k in KEYS if k not in table]
    assert not missing, "missing in %s.json: %s" % (lang, missing)
    assert all(isinstance(table[k], str) and table[k].strip() for k in KEYS if k in table)


def test_every_key_the_code_can_produce_is_in_the_list(state):
    prev, now = _pair()
    produced = {i["key"] for i in report.night_section(prev, now, NOW, None)["items"]}
    produced |= {"night.fwr.rule_gone", "night.fwr.forward_gone", "night.fwr.rule_on", "night.fwr.rule_off"}
    produced |= {"fwr.finding.%s" % f["kind"] for f in now["findings"]}
    assert produced <= set(KEYS)
