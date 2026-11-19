"""Night report section of the OPNsense family (pure; fixed clock; every text an i18n key in every language)."""
import copy
import json
from pathlib import Path

import pytest

from lanloom.integrations.opnsense import model, report
from lanloom.nightly.contrib import FAMILY_STATE_DROP

from . import fixtures as fx
from .test_model import raw_of

NOW = 1_800_000_000.0
I18N = Path(__file__).resolve().parents[3] / "lanloom" / "i18n"


def stored(state):
    return {k: v for k, v in state.items() if k not in FAMILY_STATE_DROP}


def keys(sec):
    return [i["key"] for i in sec["items"]]


@pytest.fixture(scope="module")
def state():
    return model.to_neutral(raw_of())


@pytest.mark.parametrize("st", [None, {}, [], "x", {"hosts": [{"name": "h"}]}, {"zones": []}])
def test_no_section_without_a_firewall_state(st):
    assert report.night_section(None, st, NOW, "x") is None


def test_first_report_lists_findings_only(state):
    sec = report.night_section(None, state, NOW, "gw")
    assert sec["family"] == "opnsense" and sec["title"] == "night.fwr.title" and sec["name"] == "gw"
    assert set(keys(sec)) == {"fwr.finding.admin_on_wan", "fwr.finding.rules_disabled"}
    assert sec["params"]["changes"] == 0 and sec["params"]["zones"] == 7 and sec["look"] == 1


def test_changes_since_last_night(state):
    raw = raw_of()
    rules = raw["filter"]["rules"]["rule"]
    rules[fx.U["iot_lan"]]["enabled"] = "0"                                            # switched off
    rules[fx.U["iot_web"]]["destination_port"] = "80"                                 # acts differently
    del rules[fx.U["old_test"]]                                                       # removed
    rules["a00000ff-0000-4000-8000-0000000000ff"] = fx.rule("Lab open from outside", "wan", dst="opt3", seq=99)
    now = model.to_neutral(raw)
    sec = report.night_section(stored(state), now, NOW, "gw")
    k = keys(sec)
    for want in ("night.fwr.rule_off", "night.fwr.rule_changed", "night.fwr.rule_gone", "night.fwr.rule_new",
                 "fwr.finding.wan_inbound_open"):
        assert want in k, k
    assert sec["params"]["changes"] == 4
    levels = [i["level"] for i in sec["items"]]
    assert levels == sorted(levels, key=lambda l: report.LEVELS[l])
    assert "a00000ff" not in json.dumps(sec) and fx.U["iot_lan"] not in json.dumps(sec)    # no internal ids


def test_forwarding_added_and_removed():
    before = model.to_neutral(raw_of("26.7"))
    raw = raw_of("26.7")
    raw["dnat"]["rows"][0]["descr"] = "Camera stream (new)"
    sec = report.night_section(stored(before), model.to_neutral(raw), NOW, "gw")
    assert "night.fwr.forward_new" in keys(sec) and "night.fwr.forward_gone" in keys(sec)


def test_every_key_exists_in_every_language(state):
    raw = raw_of()
    raw["gateways"]["items"][0]["status"] = "down"
    raw["filter"]["rules"]["rule"][fx.U["floating"]]["enabled"] = "1"
    sec = report.night_section(stored(state), model.to_neutral(raw), NOW, "gw")
    used = set(keys(sec)) | {sec["title"], sec["summary"], "night.fwr.summary_calm", "night.fwr.summary_look"}
    assert "fwr.finding.wan_down" in used and "fwr.finding.any_any" in used
    for lang in ("en", "de"):
        texts = json.loads((I18N / ("%s.json" % lang)).read_text())
        assert not [k for k in used if k not in texts], lang
