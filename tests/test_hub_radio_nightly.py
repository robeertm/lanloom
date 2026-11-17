"""Nightly report section "radio and batteries" of the hub bridge: when it appears, what it counts and flags, that it
renders in both languages without leftovers, and that freshness is judged at the end of the report window."""
import datetime

from lanloom import hubbridge as hb
from lanloom.nightly import render
from test_hub_bridge_core import NOW, SINCE, UNTIL, Z, add_hub, make_core

DAY = "2026-11-16"


def iso(age_s):
    return datetime.datetime.fromtimestamp(NOW - age_s, datetime.timezone.utc).isoformat(timespec="seconds")


def hdev(did, name, **kw):
    return dict({"id": did, "name": name, "model": None, "manufacturer": None, "area": None, "via": None, "service": False,
                 "macs": [], "sw": None, "domains": [], "radio": None, "role": None, "ip_hint": None, "host_hint": None}, **kw)


def hent(eid, did, state, **kw):
    return dict({"entity_id": eid, "device_id": did, "name": eid, "state": state, "unit": None, "domain": eid.split(".", 1)[0],
                 "area": None, "dc": None, "cat": None, "platform": "sample_hub", "ls": False}, **kw)


def snap(devices, entities):
    return {"type": "hub_snapshot", "t": NOW, "devices": devices, "entities": entities, "areas": [], "trackers": [],
            "automations": [], "scripts": []}


def full_snapshot():
    devices = [hdev("c1", "Mesh stick", radio="mesh154", role="coordinator"),
               hdev("gone", "Silent plug", radio="mesh154", via="c1"),
               hdev("weak", "Weak sensor", radio="mesh154", via="c1"),
               hdev("low", "Low flag sensor", radio="mesh154", via="c1"),
               hdev("soon", "Aging sensor", radio="mesh154", via="c1"),
               hdev("px", "Quiet proxy", radio="ble", role="receiver")]
    ents = [hent("sensor.stick", "c1", "online"),
            hent("light.gone", "gone", "unavailable"),
            hent("sensor.weak_b", "weak", "7", unit="%", dc="battery", domain="sensor"), hent("sensor.weak_x", "weak", "1"),
            hent("binary_sensor.low_b", "low", "on", dc="battery"),
            hent("sensor.soon_b", "soon", "18", unit="%", dc="battery", domain="sensor"),
            hent("sensor.px_x", "px", "unavailable")]
    return snap(devices, ents)


def sections(rep):
    return [s for s in rep.assemble(SINCE, UNTIL, None, DAY, [])["sections"] if s["id"] == "hub_radio"]


def filled(tmp_path, snapshot=None, **kw):
    c, rep = make_core(tmp_path)
    iid = add_hub(c, **kw)
    entry = c.hub[iid] = hb.new_entry(None)
    hb.book(entry, snapshot or full_snapshot(), NOW)
    return c, rep, iid


def test_section_absent_without_hub_snapshot_radio_or_when_disabled(tmp_path):
    c, rep = make_core(tmp_path)
    assert sections(rep) == []
    iid = add_hub(c)
    assert sections(rep) == []                                                       # a connection without a snapshot
    c.hub[iid] = hb.new_entry(None)
    hb.book(c.hub[iid], snap([hdev("w", "Wlan plug")], [hent("light.w", "w", "on"), hent("sensor.w_t", "w", "3", unit="W", domain="sensor")]), NOW)
    assert sections(rep) == []                                                       # no radio device and no battery
    hb.book(c.hub[iid], full_snapshot(), NOW)
    assert len(sections(rep)) == 1
    c.store.execute("UPDATE integrations SET enabled=0 WHERE id=?", (iid,))
    assert sections(rep) == []


def test_section_appears_for_batteries_alone(tmp_path):
    s = snap([hdev("w", "Wlan remote")], [hent("sensor.w_b", "w", "5", unit="%", dc="battery", domain="sensor")])
    c, rep, iid = filled(tmp_path, s)
    (sec,) = sections(rep)
    assert [i["key"] for i in sec["items"] if i["point"]] == ["report.radio.battery_now"]
    assert sec["summary"]["vars"] == {"n": 0, "nets": 0}


def test_section_points_levels_and_summary(tmp_path):
    c, rep, iid = filled(tmp_path)
    (sec,) = sections(rep)
    assert sec["title"] == "report.sec.hub_radio" and sec["name"] == "Sample hub"
    by = {}
    for i in sec["items"]:
        by.setdefault(i["key"], []).append(i)
    assert sorted(by) == ["report.radio.anchor_silent", "report.radio.battery_low", "report.radio.battery_now", "report.radio.battery_soon",
                          "report.radio.net_bad_plain", "report.radio.silent"]
    assert by["report.radio.silent"][0]["vars"]["name"] == "Silent plug" and by["report.radio.silent"][0]["point"] is True
    assert by["report.radio.battery_now"][0]["vars"] == {"name": "Weak sensor", "pct": 7} and by["report.radio.battery_now"][0]["point"]
    assert by["report.radio.battery_low"][0]["vars"] == {"name": "Low flag sensor", "pct": 0} and by["report.radio.battery_low"][0]["point"]
    assert by["report.radio.battery_soon"][0]["vars"] == {"name": "Aging sensor", "pct": 18} and by["report.radio.battery_soon"][0]["point"] is False
    assert by["report.radio.anchor_silent"][0]["vars"]["name"] == "Quiet proxy" and by["report.radio.anchor_silent"][0]["point"]
    assert by["report.radio.net_bad_plain"][0]["level"] == "danger" and by["report.radio.net_bad_plain"][0]["point"]
    assert sum(1 for i in sec["items"] if i["point"]) == 5
    assert sec["level"] == "danger"
    assert sec["summary"]["key"] == "report.radio.summary" and sec["summary"]["vars"] == {"n": 4, "nets": 2}
    assert [(p["key"], p["vars"]) for p in sec["summary"]["parts"]] == [
        ("report.radio.part.silent", {"n": 1}), ("report.radio.part.now", {"n": 2}), ("report.radio.part.soon", {"n": 1})]


def test_a_quiet_network_has_no_points(tmp_path):
    s = snap([hdev("c1", "Mesh stick", radio="mesh154", role="coordinator"), hdev("a", "Lamp", radio="mesh154", via="c1")],
             [hent("sensor.stick", "c1", "online"), hent("light.a", "a", "on"), hent("sensor.a_b", "a", "90", unit="%", dc="battery", domain="sensor")])
    c, rep, iid = filled(tmp_path, s)
    (sec,) = sections(rep)
    assert not [i for i in sec["items"] if i["point"]] and sec["level"] == "ok"
    assert not sec["summary"].get("parts") and sec["summary"]["vars"] == {"n": 1, "nets": 1}


def test_a_bad_network_with_a_coordinator_names_it(tmp_path):
    s = snap([hdev("c1", "Mesh stick", radio="mesh154", role="coordinator"), hdev("a", "Lamp", radio="mesh154", via="c1")],
             [hent("sensor.stick", "c1", "unavailable"), hent("light.a", "a", "on")])
    c, rep, iid = filled(tmp_path, s)
    (sec,) = sections(rep)
    (bad,) = [i for i in sec["items"] if i["key"] == "report.radio.net_bad"]
    assert bad["vars"] == {"name": "Mesh stick", "silent": 0, "n": 1} and bad["point"] is True


def test_long_lists_end_with_and_n_more(tmp_path):
    devices = [hdev("c1", "Mesh stick", radio="mesh154", role="coordinator")] + [hdev("d%d" % i, "Plug %02d" % i, radio="mesh154", via="c1") for i in range(12)]
    ents = [hent("sensor.stick", "c1", "online")] + [hent("light.d%d" % i, "d%d" % i, "unavailable") for i in range(12)]
    c, rep, iid = filled(tmp_path, snap(devices, ents))
    (sec,) = sections(rep)
    assert sum(1 for i in sec["items"] if i["key"] == "report.radio.silent") == 8
    (more,) = [i for i in sec["items"] if i["key"] == "report.radio.more"]
    assert more["vars"] == {"n": 4} and more["point"] is False


def test_merged_devices_are_counted_in_the_summary_parts(tmp_path):
    c, rep, iid = filled(tmp_path)
    c.store.upsert_device({"id": "d-test-1", "ip": "192.168.50.1", "mac": "aa:bb:cc:00:00:99", "data": {}, "kind": "computer", "confidence": 0.9,
                           "title": "Radio bridge"}, None)
    s = full_snapshot()
    s["devices"][0]["macs"] = ["aa:bb:cc:00:00:99"]
    s["t"] = NOW + 1
    hb.book(c.hub[iid], s, NOW)
    (sec,) = sections(rep)
    assert ("report.radio.part.merged", {"n": 1}) in [(p["key"], p["vars"]) for p in sec["summary"]["parts"]]


def test_section_renders_in_both_languages_without_placeholders(tmp_path):
    c, rep, iid = filled(tmp_path)
    (sec,) = sections(rep)
    texts = {}
    for lang in ("en", "de"):
        rr = render.Renderer(lang, Z, DAY)
        texts[lang] = [rr.line(sec["summary"])] + [rr.line(i) for i in sec["items"]]
        for line in texts[lang]:
            assert "{" not in line and "}" not in line and "report." not in line, line
    assert len(texts["en"]) == len(texts["de"]) >= 7
    assert all(en != de for en, de in zip(texts["en"], texts["de"]))
    assert any("Silent plug" in t for t in texts["en"]) and any("Weak sensor" in t and "7" in t for t in texts["de"])


def test_freshness_is_judged_at_the_end_of_the_report_window(tmp_path):
    # "last seen" two days before the report's end: silent at report time. Read with today's wall clock the stamp would lie
    # in the future (the fixed report time is later), and the device would look fresh.
    devices = [hdev("c1", "Mesh stick", radio="mesh154", role="coordinator"), hdev("a", "Old lamp", radio="mesh154", via="c1"),
               hdev("b", "New lamp", radio="mesh154", via="c1")]
    ents = [hent("sensor.stick", "c1", "online"), hent("light.a", "a", "on"), hent("sensor.a_ls", "a", iso(2 * 86400), ls=True),
            hent("light.b", "b", "on"), hent("sensor.b_ls", "b", iso(600), ls=True)]
    c, rep, iid = filled(tmp_path, snap(devices, ents))
    (sec,) = sections(rep)
    silent = [i for i in sec["items"] if i["key"] == "report.radio.silent"]
    assert [i["vars"]["name"] for i in silent] == ["Old lamp"]
