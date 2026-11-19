"""Night report section of the OpenWrt family: findings, rule changes, port forwards and wireless networks, in i18n
keys of both app languages; nothing without a state."""
import copy
import json
from pathlib import Path

from lanloom.integrations.openwrt import model, report

from .test_model import raw_of

ROOT = Path(__file__).resolve().parents[3]
NOW = 1_800_000_000.0


def state(**edit):
    return model.to_neutral(raw_of(**edit))


def test_no_state_no_section():
    assert report.night_section(None, None, NOW) is None
    assert report.night_section(None, {"rule_digest": []}, NOW) is None          # not a router state (another family)


def test_first_report_lists_the_findings_only():
    sec = report.night_section(None, state(), NOW, "Edge router")
    assert sec["family"] == "openwrt" and sec["title"] == "night.wrt.title" and sec["name"] == "Edge router"
    keys = [i["key"] for i in sec["items"]]
    assert keys[0] == "fwr.finding.admin_on_wan" and "fwr.finding.ssid_open" in keys
    assert sec["params"]["changes"] == 0 and sec["params"]["ssids"] == 4 and sec["summary"] == "night.wrt.summary_look"


def test_changes_since_the_last_report():
    before = state()
    raw = raw_of()
    fw = raw["firewall"]
    next(s for s in fw.values() if s.get("name") == "Old test")["enabled"] = "1"
    next(s for s in fw.values() if s.get("name") == "Guest DNS")["dest_port"] = "53 853"
    del fw[next(n for n, s in fw.items() if s.get("name") == "IoT DNS")]
    fw["new"] = {".type": "rule", ".name": "new", ".anonymous": False, ".index": 90, "name": "Camera from outside",
                 "src": "wan", "dest": "lan", "proto": "tcp", "dest_port": "554", "target": "ACCEPT"}
    del fw[next(n for n, s in fw.items() if s.get(".type") == "redirect")]
    wl = raw["wireless"]
    next(s for s in wl.values() if s.get("ssid") == "Things")["disabled"] = "1"
    next(s for s in wl.values() if s.get("ssid") == "Guests")["encryption"] = "psk2"
    wl["x"] = {".type": "wifi-iface", ".name": "x", ".anonymous": False, ".index": 50, "device": "radio1", "mode": "ap",
               "ssid": "Office", "encryption": "sae", "network": "lan"}
    sec = report.night_section(copy.deepcopy(before), model.to_neutral(raw), NOW)
    got = {(i["key"], json.dumps(i["params"], sort_keys=True)) for i in sec["items"]}
    want = {("night.fwr.rule_on", '{"rule": "Old test"}'), ("night.fwr.rule_changed", '{"rule": "Guest DNS"}'),
            ("night.fwr.rule_gone", '{"rule": "IoT DNS"}'), ("night.fwr.rule_new", '{"rule": "Camera from outside"}'),
            ("night.fwr.forward_gone", '{"rule": "Web server"}'), ("fwr.finding.wan_inbound_open", None)}
    assert {w for w in want if w[1]} <= got
    assert any(k == "fwr.finding.wan_inbound_open" for k, _ in got) is False   # a port-limited rule is no open door
    assert [i["level"] for i in sec["items"]] == sorted((i["level"] for i in sec["items"]), key=report.LEVELS.get)
    assert sec["params"]["changes"] == 5
    assert not any(k.startswith("night.wrt.ssid") for k, _ in got)          # wireless changes: the shared section


def test_every_key_exists_in_every_app_language():
    keys = {"night.wrt.title", "night.wrt.summary_look", "night.wrt.summary_calm", "fwr.finding.forward_accept",
            "fwr.finding.admin_on_wan", "fwr.finding.ssid_open", "fwr.finding.wan_down", "fwr.finding.rules_disabled",
            "fwr.finding.wan_inbound_open", "fwr.finding.any_any", "night.fwr.rule_new", "night.fwr.forward_new"}
    for lang in ("en", "de"):
        texts = json.loads((ROOT / "lanloom" / "i18n" / ("%s.json" % lang)).read_text())
        assert keys <= set(texts), keys - set(texts)
