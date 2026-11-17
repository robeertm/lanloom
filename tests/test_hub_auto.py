"""Automations tab (H3): the flow the bridge keeps (structure only, nothing private), the core views built from it
(findings, plain words, who switches what, timeline, day plan, rooms, board, health), the nightly section in both
languages, the API with its roles, and the demo. Sample data only, fixed clock."""
import json
import re
import time
from collections import deque
from pathlib import Path

import pytest

from lanloom import demo_hub, hub_privacy as hp, hubauto, hubbridge as hb, textfmt
from lanloom.nightly import render
from lanloom.sources import hub as hub_src
from lanloom.sources import hub_flow
from test_hub_bridge_core import NOW, SINCE, UNTIL, Z, add_hub, make_core

ROOT = Path(__file__).resolve().parent.parent
EN = json.loads((ROOT / "lanloom/i18n/en.json").read_text(encoding="utf-8"))
DE = json.loads((ROOT / "lanloom/i18n/de.json").read_text(encoding="utf-8"))
HOUR = 3600


# ---------------------------------------------------------------------- 1. the flow kept by the bridge
def test_flow_keeps_structure_and_drops_alias_data_templates_and_targets_of_messages():
    cfg = {"alias": "Robin's lamp", "description": "secret plan", "id": "x1",
           "triggers": [{"trigger": "state", "entity_id": ["binary_sensor.door", "person.robin"], "to": "on", "for": "0:05:00", "id": "t-robin"},
                        {"trigger": "state", "entity_id": "person.robin", "to": "home"},
                        {"trigger": "time", "at": ["06:30:00", "input_datetime.wake"], "weekday": ["mon", "fri", "xx"]},
                        {"trigger": "sun", "event": "sunset", "offset": "-00:30:00"},
                        {"trigger": "time_pattern", "minutes": "/5"},
                        {"trigger": "numeric_state", "entity_id": "sensor.temp", "above": 24, "below": "input_number.max"},
                        {"trigger": "state", "entity_id": "input_select.mode", "to": "Robin away"}],
           "conditions": [{"condition": "time", "after": "18:00", "before": "23:30:10"},
                          {"condition": "or", "conditions": [{"condition": "template", "value_template": "{{ is_state('a.b','c') }}"},
                                                              {"condition": "zone", "entity_id": "person.robin", "zone": "zone.work"}]},
                          "{{ states('sensor.x') | int > 3 }}"],
           "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.desk", "area_id": "office"}, "data": {"brightness_pct": 77}},
                       {"action": "notify.mobile_robin", "data": {"message": "Robin, the door"}},
                       {"delay": {"minutes": 2}},
                       {"choose": [{"conditions": [{"condition": "state", "entity_id": "light.desk", "state": "off"}],
                                    "sequence": [{"service": "script.wake_up"}]}],
                        "default": [{"event": "custom_robin", "event_data": {"who": "robin"}}]},
                       {"variables": {"secret": "abc"}},
                       {"repeat": {"count": 3, "sequence": [{"action": "light.toggle", "entity_id": "light.desk"}]}}]}
    f = hub_flow.flow_from_config(cfg)
    blob = json.dumps(f)
    for word in ("Robin", "robin", "secret", "brightness", "77", "t-robin", "is_state", "work", "abc", "message", "x1"):
        assert word not in blob, word
    t = f["trigger"]
    assert t[0] == {"k": "state", "e": ["binary_sensor.door"], "to": ["on"], "for": 300}
    assert t[1] == {"k": "zone"}                                          # a person arriving: kind only, no id
    assert t[2] == {"k": "time", "at": ["06:30", "input_datetime.wake"], "wd": ["mon", "fri"]}
    assert t[3] == {"k": "sun", "ev": "sunset", "off": -1800}
    assert t[4] == {"k": "pattern", "every": {"m": "/5"}}
    assert t[5] == {"k": "num", "e": ["sensor.temp"], "above": 24, "below": "input_number.max"}
    assert t[6] == {"k": "state", "e": ["input_select.mode"], "to": ["_"]}  # an option of a select: "some value"
    c = f["condition"]
    assert c[0] == {"k": "time", "after": "18:00", "before": "23:30"}
    assert c[1] == {"k": "or", "c": [{"k": "tpl", "tpl": True}, {"k": "zone"}]}
    assert c[2] == {"k": "tpl", "tpl": True}
    a = f["action"]
    assert a[0] == {"k": "call", "svc": "light.turn_on", "e": ["light.desk"], "area": ["office"]}
    assert a[1] == {"k": "call", "svc": "notify.send", "notify": True}
    assert a[2] == {"k": "delay", "for": 120}
    assert a[3]["k"] == "branch" and a[3]["opts"][0]["a"] == [{"k": "call", "svc": "script.wake_up", "e": ["script.wake_up"]}]
    assert a[3]["else"] == [{"k": "event"}]
    assert a[4] == {"k": "repeat", "a": [{"k": "call", "svc": "light.toggle", "e": ["light.desk"]}], "n": 3}
    assert len(a) == 5                                                    # variables are never kept
    assert hub_flow.entity_ids(f) == {"binary_sensor.door", "input_datetime.wake", "sensor.temp", "input_number.max",
                                      "input_select.mode", "light.desk", "script.wake_up"}


def test_flow_limits_depth_and_size_and_survives_garbage():
    deep = {"actions": [{"sequence": [{"sequence": [{"sequence": [{"sequence": [{"sequence": [{"sequence": [{"delay": 1}]}]}]}]}]}]}]}
    f = hub_flow.flow_from_config(deep)
    assert "delay" not in json.dumps(f)                                   # deeper than MAX_DEPTH: not read
    big = {"actions": [{"action": "light.turn_on", "entity_id": "light.a%d" % i} for i in range(500)]}
    f = hub_flow.flow_from_config(big)
    assert f["cut"] and len(f["action"]) == hub_flow.MAX_NODES
    for junk in (None, [], "x", {"triggers": "x", "actions": 5}, {"actions": [{"action": "not a service!"}]},
                 {"triggers": [{"trigger": "time", "at": "{{ now() }}"}]}, {"actions": [{"delay": "{{ 5 }}"}]}):
        hub_flow.flow_from_config(junk)
    assert hub_flow.flow_from_config({"actions": [{"action": "not a service!"}]})["action"] == [{"k": "call", "tpl": True}]
    assert hub_flow.seconds("1:30:00") == 5400 and hub_flow.seconds({"hours": 1, "minutes": 2}) == 3720 and hub_flow.seconds("x") is None
    assert hub_flow.seconds(True) is None and hub_flow.seconds({"minutes": "{{ x }}"}) is None


def test_bridge_keeps_sun_times_and_entry_states_only():
    assert hub_src.sun_times({"next_rising": "2026-11-17T06:40:00+00:00", "next_setting": "2026-11-17T15:50:00+00:00", "elevation": 3}) == {
        "rise": 1794897600.0, "set": 1794930600.0}
    assert hub_src.sun_times({}) is None
    rows = hub_src.entry_health([{"entry_id": "a", "domain": "weather_x", "title": "Robin's home", "state": "setup_retry"},
                                 {"entry_id": "b", "domain": "weather_x", "state": "setup_retry"},
                                 {"entry_id": "c", "domain": "cam", "state": "loaded", "disabled_by": "user"},
                                 {"entry_id": "d", "domain": "Bad Domain", "state": "loaded"}, "junk"])
    assert rows == [{"domain": "weather_x", "state": "setup_retry", "n": 2}]
    snap = hub_src.build_snapshot({}, [], [], [], entries_raw=[{"domain": "x", "state": "loaded"}])
    assert snap["entries"] == [{"domain": "x", "state": "loaded", "n": 1}]
    assert "entries" not in hub_src.build_snapshot({}, [], [], [], entries_raw=None)
    assert "sun" not in hp.outbound({"sun": {"rise": 1}, "entities": [], "devices": []})


# ---------------------------------------------------------------------- 2. the core views (fixed clock)
AREAS = [{"id": "office", "name": "Office"}, {"id": "hall", "name": "Hall"}]
CONFIGS = {
    "automation.desk_on": {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.desk_motion", "to": "on"}],
                           "actions": [{"action": "switch.turn_on", "target": {"entity_id": "switch.desk"}}]},
    "automation.desk_off": {"triggers": [{"trigger": "time", "at": "18:00:00", "weekday": ["mon", "tue", "wed", "thu", "fri"]}],
                            "conditions": [{"condition": "numeric_state", "entity_id": "sensor.desk_power", "below": 5}],
                            "actions": [{"action": "switch.turn_off", "target": {"entity_id": "switch.desk"}}]},
    "automation.hall_motion": {"mode": "single", "triggers": [{"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}],
                               "conditions": [{"condition": "sun", "after": "sunset", "before": "sunrise"}],
                               "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}}, {"delay": "0:03:00"},
                                           {"action": "light.turn_off", "target": {"entity_id": "light.hall"}}]},
    "automation.ghost": {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.old_bell", "to": "on"}],
                         "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}]},
    "automation.echo": {"triggers": [{"trigger": "state", "entity_id": "light.hall"}],
                        "actions": [{"action": "light.toggle", "target": {"entity_id": "light.hall"}}]},
    "automation.dawn": {"triggers": [{"trigger": "sun", "event": "sunrise", "offset": "00:20:00"},
                                     {"trigger": "time", "at": "input_datetime.wake"}, {"trigger": "time_pattern", "minutes": "/15"}],
                        "actions": [{"action": "cover.open_cover", "target": {"entity_id": "cover.blind"}}]},
    "script.night": {"sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.hall"}},
                                  {"action": "switch.turn_off", "target": {"entity_id": "switch.desk"}}]},
}


def ent(eid, state, did=None, area=None, **kw):
    return dict({"entity_id": eid, "device_id": did, "name": kw.pop("name", eid.split(".")[1].replace("_", " ").title()), "state": state,
                 "unit": None, "domain": eid.split(".")[0], "area": area, "dc": None, "cat": None, "platform": "sample_hub", "ls": False}, **kw)


def dev(did, name, area, **kw):
    return dict({"id": did, "name": name, "model": None, "manufacturer": None, "area": area, "via": None, "service": False, "macs": [],
                 "sw": None, "domains": [], "radio": None, "role": None, "ip_hint": None, "host_hint": None}, **kw)


def entry():
    items = [("automation.desk_on", "Desk on", "restart", "1", "on", NOW - 600), ("automation.desk_off", "Desk off", "single", "2", "on", NOW - 900),
             ("automation.hall_motion", "Hall motion", "single", "3", "on", NOW - 300), ("automation.ghost", "Ghost bell", "single", "4", "on", None),
             ("automation.echo", "Echo", "parallel", "5", "on", NOW - 10), ("automation.dawn", "Dawn", "single", "6", "off", NOW - 40 * 86400),
             ("script.night", "Night", "single", "night", "off", None)]
    ents = [ent("binary_sensor.desk_motion", "off", "d1", dc="motion"), ent("switch.desk", "on", "d2"), ent("sensor.desk_power", "3.5", "d2", unit="W"),
            ent("binary_sensor.hall_motion", "on", "d3", dc="motion"), ent("light.hall", "off", "d3"), ent("light.porch", "unavailable", "d4", area="hall"),
            ent("cover.blind", "open", "d4", area="hall"), ent("input_datetime.wake", "07:05:00"), ent("sensor.d3_battery", "40", "d3", cat="diagnostic")]
    ents += [ent(e, st, name=n) for e, n, _m, _i, st, _l in items]
    snap = {"type": "hub_snapshot", "t": NOW, "admin": True, "areas": AREAS, "trackers": [],
            "devices": [dev("d1", "Desk sensor", "office"), dev("d2", "Desk plug", "office"), dev("d3", "Hall light", "hall"), dev("d4", "Porch", "hall"),
                        dev("svc", "Some service", None, service=True)],
            "entities": ents,
            "automations": [{"entity_id": e, "id": i, "mode": m, "last": last, "state": st} for e, n, m, i, st, last in items if e.startswith("automation.")],
            "scripts": [{"entity_id": e, "id": i, "mode": m, "last": last, "state": st} for e, n, m, i, st, last in items if e.startswith("script.")],
            "entries": [{"domain": "weather_x", "state": "setup_retry", "n": 1}, {"domain": "sample_hub", "state": "loaded", "n": 3},
                        {"domain": "backup_x", "state": "not_loaded", "n": 1}],
            "sun": {"rise": NOW + 3 * HOUR, "set": NOW + 12 * HOUR}}
    e = hb.new_entry(None)
    hb.book(e, snap, NOW)
    deps = {"type": "hub_deps", "t": NOW, "cycles": [], "blocked": [],
            "automations": {k: {"trigger": [], "condition": [], "action": [], "text": [], "related": [], "flow": hub_flow.flow_from_config(v)}
                            for k, v in CONFIGS.items() if k.startswith("automation.")},
            "scripts": {k: {"trigger": [], "condition": [], "action": [], "text": [], "related": [], "flow": hub_flow.flow_from_config(v)}
                        for k, v in CONFIGS.items() if k.startswith("script.")}}
    hb.book(e, deps, NOW)
    for h in range(3):                                                   # the echo runs away within the current hour
        hb.book(e, {"type": "hub_triggers", "runs": {"automation.echo": {"n": 30, "last": NOW - 60 * (h + 1)}}}, NOW)
    hb.book(e, {"type": "hub_triggers", "runs": {"automation.desk_on": {"n": 1, "last": NOW - 600}}}, NOW)
    for t, eid, st, by in [(NOW - 3000, "switch.desk", "on", "automation.desk_on"), (NOW - 2980, "switch.desk", "off", "automation.desk_off"),
                           (NOW - 2000, "switch.desk", "on", "automation.desk_on"), (NOW - 1990, "switch.desk", "off", "user"),
                           (NOW - 1980, "switch.desk", "on", "automation.desk_on"), (NOW - 1500, "light.hall", "on", "automation.hall_motion"),
                           (NOW - 1400, "light.hall", "off", None), (NOW - 1200, "sensor.desk_power", "3.5", None),
                           (NOW - 100 * 3600, "switch.desk", "on", "automation.desk_on")]:
        ev = {"type": "hub_state", "entity_id": eid, "state": st, "t": t}
        if by:
            ev["by"] = by
        hb.book(e, ev, NOW)
    return e


def ctx():
    return hubauto.Ctx(entry(), NOW)


def codes(row):
    return [f["code"] for f in row["findings"]]


def test_findings_from_own_rules():
    rows = {r["id"]: r for r in hubauto.rows(ctx())}
    assert codes(rows["automation.ghost"]) == ["dead", "unavailable", "never"]
    assert rows["automation.ghost"]["findings"][0]["ids"] == ["binary_sensor.old_bell"]
    assert codes(rows["automation.echo"]) == ["runaway", "self_trigger"]
    assert rows["automation.echo"]["peak"] >= hb.BUSY_PER_HOUR
    assert codes(rows["automation.desk_on"]) == ["conflict"] and codes(rows["automation.desk_off"]) == ["conflict"]
    c = rows["automation.desk_on"]["findings"][0]
    assert c["other"] == "automation.desk_off" and c["n"] == 1        # the person in between is no fight between automations
    assert codes(rows["automation.hall_motion"]) == ["single_wait"]
    assert codes(rows["automation.dawn"]) == []                         # switched off: "never ran" does not apply
    assert rows["script.night"]["kind"] == "script" and codes(rows["script.night"]) == []
    assert rows["automation.desk_on"]["areas"] == ["Office"] and rows["automation.dawn"]["triggers"] == ["pattern", "sun", "time"]
    order = [r["id"] for r in hubauto.rows(ctx())]
    assert order.index("automation.ghost") < order.index("automation.dawn")   # warnings first


def test_tab_counts_totals_and_filters():
    tab = hubauto.tab(ctx())
    assert tab["totals"]["automations"] == 6 and tab["totals"]["scripts"] == 1 and tab["totals"]["on"] == 5 and tab["totals"]["off"] == 1
    assert tab["counts"]["conflict"] == 2 and tab["counts"]["dead"] == 1 and tab["totals"]["conflicts"] == 1
    assert tab["areas"] == ["Hall", "Office"] and tab["exact"] == 7 and tab["admin"]


def test_who_switches_what_anonymous_with_planned_and_conflicts():
    w = hubauto.who(ctx())
    first = w["entities"][0]
    assert first["id"] == "switch.desk" and first["conflicts"] == 1 and first["switchers"] == 3
    person = [c for c in first["causes"] if c["kind"] == "user"]
    assert len(person) == 1 and person[0]["name"] is None and person[0]["by"] == "user"
    assert sum(c["n"] for c in first["causes"] if c["kind"] == "automation") == 4
    assert {p["by"] for p in first["planned"]} == {"automation.desk_on", "automation.desk_off", "script.night"}
    assert w["conflicts"] == [dict(w["conflicts"][0], entity="switch.desk", n=1)]
    assert all(e["id"] != "sensor.desk_power" for e in w["entities"])  # a plain number without a cause is no switch
    assert "user_id" not in json.dumps(w)


def test_timeline_newest_first_with_kinds_and_runs_and_24_hours_only():
    tl = hubauto.timeline(ctx())
    assert tl == sorted(tl, key=lambda e: -e["t"])
    assert {e["kind"] for e in tl if e["type"] == "change"} == {"automation", "user", "device"}
    assert all(e["t"] >= NOW - 86400 for e in tl)
    person = [e for e in tl if e["kind"] == "user"][0]
    assert person["by"] is None and person["by_name"] is None
    runs = [e for e in tl if e["type"] == "run"]
    assert runs and runs[0]["entity"] in ("automation.echo", "automation.desk_on")


def test_day_plan_places_times_helpers_sun_windows_and_lists_rhythms():
    p = hubauto.dayplan(ctx())
    items = {i["id"]: i for i in p["items"]}
    assert items["automation.desk_off"]["marks"] == [{"kind": "time", "at": "18:00", "wd": ["mon", "tue", "wed", "thu", "fri"], "helper": None}]
    dawn = items["automation.dawn"]["marks"]
    assert {"kind": "sun", "ev": "sunrise", "off": 1200, "ts": NOW + 3 * HOUR + 1200} in dawn
    assert {"kind": "time", "at": "07:05", "wd": None, "helper": "Wake"} in dawn
    assert items["automation.hall_motion"]["windows"] == [{"kind": "sun", "from_ts": NOW + 12 * HOUR, "to_ts": NOW + 3 * HOUR,
                                                           "after": "sunset", "before": "sunrise"}]
    assert [r["id"] for r in p["rhythm"]] == ["automation.dawn"] and p["rhythm"][0]["spec"]["k"] == "auto.ex.t.every_m"
    e = entry()
    e["snapshot"].pop("sun")
    p2 = hubauto.dayplan(hubauto.Ctx(e, NOW))
    assert {"kind": "sun", "ev": "sunrise"}.items() <= p2["unplaced"][0].items()


def test_rooms_with_live_state_without_diagnostics_and_with_automations():
    rooms = {r["name"]: r for r in hubauto.rooms(ctx(), {"d3": {"fresh": "silent"}}, {"d2": "net-1"})}
    office = rooms["Office"]
    assert [d["name"] for d in office["devices"]] == ["Desk plug", "Desk sensor"]
    assert office["devices"][0]["network_device"] == "net-1" and office["on"] == 1
    assert {a["id"] for a in office["automations"]} == {"automation.desk_on", "automation.desk_off", "script.night"}
    hall = rooms["Hall"]
    assert hall["unavailable"] == 1 and [d["fresh"] for d in hall["devices"] if d["name"] == "Hall light"] == ["silent"]
    assert all(e["id"] != "sensor.d3_battery" for d in hall["devices"] for e in d["entities"])


def test_board_for_an_entity_and_for_an_automation():
    b = hubauto.board(ctx(), "switch.desk")
    left = {(p["id"], p["role"]) for p in b["left"]}
    assert ("automation.desk_on", "sets") in left and ("automation.desk_off", "sets") in left and ("script.night", "sets") in left
    assert (None, "user") in left
    assert b["right"] == []
    b = hubauto.board(ctx(), "sensor.desk_power")
    assert [(p["id"], p["role"]) for p in b["right"]] == [("automation.desk_off", "checks")]
    b = hubauto.board(ctx(), "automation.hall_motion")
    assert [(p["id"], p["role"]) for p in b["left"]] == [("binary_sensor.hall_motion", "starts")]
    assert [(p["id"], p["role"], p.get("verbs")) for p in b["right"]] == [("light.hall", "switches", ["turn_off", "turn_on"])]
    b = hubauto.board(ctx(), "automation.ghost")
    assert b["left"][0]["missing"] and b["right"][0]["unavailable"]
    assert hubauto.board(ctx(), "person.robin") is None and hubauto.board(ctx(), "light.nope") is None


def test_health_bundles_and_never_heals():
    h = hubauto.health(ctx(), {"state": {"d3": {"fresh": "silent"}}})
    assert h["integrations"]["problems"] == [{"domain": "weather_x", "state": "setup_retry", "n": 1}]
    assert h["integrations"]["loaded"] == 3 and h["integrations"]["total"] == 5
    assert h["unavailable"] == [{"domain": "light", "n": 1, "items": [{"id": "light.porch", "name": "Porch", "device": "Porch", "area": "Hall"}]}]
    assert [f["code"] for f in h["findings"]] == ["integration", "unavailable", "radio_silent"]
    e = entry()
    e["snapshot"].pop("entries")
    assert hubauto.health(hubauto.Ctx(e, NOW))["entries_known"] is False


def test_no_flow_without_admin_gives_basic_rows():
    e = entry()
    e["deps"] = None
    rows = {r["id"]: r for r in hubauto.rows(hubauto.Ctx(e, NOW))}
    assert not rows["automation.ghost"]["exact"] and codes(rows["automation.ghost"]) == ["never"]
    assert hubauto.detail(hubauto.Ctx(e, NOW), "automation.ghost")["explain"] == {"exact": False, "when": [], "only": [], "then": [], "where": []}


# ---------------------------------------------------------------------- 3. plain words: every key exists, every placeholder is filled
def specs(node):
    if isinstance(node, dict):
        if "k" in node and str(node["k"]).startswith("auto.ex."):
            yield node
        for v in node.values():
            yield from specs(v)
    elif isinstance(node, list):
        for v in node:
            yield from specs(v)


def check_spec(s):
    for lang, strings in (("en", EN), ("de", DE)):
        assert s["k"] in strings, (lang, s["k"])
        need = textfmt.placeholders(strings[s["k"]])
        assert need <= set(s["v"]), (lang, s["k"], need, s["v"])


def test_plain_words_of_the_samples_and_the_demo_use_known_keys_with_all_values():
    c = ctx()
    seen = set()
    for eid in c.items:
        for s in specs(hubauto.detail(c, eid)):
            check_spec(s)
            seen.add(s["k"])
    e = demo_entry()
    dc = hubauto.Ctx(e, NOW)
    for eid in dc.items:
        for s in specs(hubauto.detail(dc, eid)):
            check_spec(s)
            seen.add(s["k"])
    for s in specs(hubauto.dayplan(dc)):
        check_spec(s)
    assert {"auto.ex.t.motion", "auto.ex.t.time", "auto.ex.t.sunrise", "auto.ex.a.on", "auto.ex.a.branch", "auto.ex.a.notify",
            "auto.ex.c.below", "auto.ex.a.delay", "auto.ex.a.wait_for", "auto.ex.tail.wd", "auto.ex.tail.before"} <= seen


def test_every_sentence_key_the_rules_can_build_exists_in_both_languages():
    src = (ROOT / "lanloom/hubauto.py").read_text(encoding="utf-8")
    parts = set(re.findall(r'_spec\("([a-z_.]+)"', src)) | {"t." + v for v in hubauto.STATE_VERBS.values()} | {"c." + v for v in hubauto.STATE_VERBS.values()
                                                                                                          if v not in ("sunrise", "sunset")}
    parts |= {"a." + v for v in hubauto.SERVICE_VERBS.values()}
    parts |= {"t." + k for k in ("event", "mqtt", "webhook", "device", "zone", "calendar", "tag", "tpl", "sunrise", "sunset", "every_s", "every_m", "every_h")}
    parts |= {"c." + k for k in ("and", "or", "not", "tpl", "trigger", "device", "zone", "after_sunrise", "after_sunset", "before_sunrise", "before_sunset")}
    parts |= {"a." + k for k in ("event", "device")}
    many_ok = re.compile(r"^(c|a)\.")
    for p in parts:
        if p.endswith(".") or p.endswith("_"):
            continue
        for strings in (EN, DE):
            assert "auto.ex." + p in strings, p
            if many_ok.match(p) and ("auto.ex." + p + ".many") in EN:
                assert "auto.ex." + p + ".many" in strings
    for v in hubauto.SERVICE_VERBS.values():
        assert "auto.ex.a.%s.many" % v in EN and "auto.ex.a.%s.many" % v in DE


def test_finding_and_health_texts_exist_with_their_values():
    for code in hubauto.FINDINGS:
        for strings in (EN, DE):
            assert "auto.f." + code in strings and "auto.f." + code + ".text" in strings
            assert textfmt.placeholders(strings["auto.f." + code + ".text"]) <= {"n", "names", "other", "days", "last"}
    for code in ("integration", "unavailable", "radio_silent", "limited"):
        assert "auto.health.f." + code in EN and "auto.health.f." + code in DE
    for state in hubauto.PROBLEM_ENTRY_STATES + ("not_loaded", "loaded"):
        assert "report.auto.entry." + state in EN and "report.auto.entry." + state in DE


# ---------------------------------------------------------------------- 4. nightly section
def auto_sections(rep):
    return [s for s in rep.assemble(SINCE, UNTIL, None, "2026-11-16", [])["sections"] if s["id"] == "auto"]


def test_nightly_section_appears_only_with_a_source(tmp_path):
    c, rep = make_core(tmp_path)
    assert auto_sections(rep) == []
    iid = add_hub(c)
    c.hub[iid] = hb.new_entry(None)
    assert auto_sections(rep) == []                                      # no snapshot yet
    hb.book(c.hub[iid], {"type": "hub_snapshot", "t": NOW, "devices": [], "areas": [], "entities": [ent("light.a", "on")],
                         "automations": [], "scripts": []}, NOW)
    assert auto_sections(rep) == []                                      # nothing to judge: no flows, no entries, nothing unavailable
    c.hub[iid] = entry()
    (sec,) = auto_sections(rep)
    assert sec["level"] == "warn" and sec["name"] == "Sample hub"


def test_nightly_section_points_notes_and_both_languages(tmp_path):
    c, rep = make_core(tmp_path)
    c.hub[add_hub(c)] = entry()
    (sec,) = auto_sections(rep)
    keys = [i["key"] for i in sec["items"]]
    assert {"report.auto.dead", "report.auto.conflict", "report.auto.integration", "report.auto.unavailable"} <= set(keys)
    assert {"report.auto.self_trigger", "report.auto.single_wait", "report.auto.never"} <= set(keys)
    assert all(i["point"] for i in sec["items"] if i["key"] in ("report.auto.dead", "report.auto.conflict", "report.auto.integration"))
    texts = {}
    for lang in ("en", "de"):
        rr = render.Renderer(lang, Z, "2026-11-16")
        texts[lang] = [rr.line(sec["summary"])] + [rr.line(i) for i in sec["items"]]
        for line in texts[lang]:
            assert "{" not in line and "}" not in line and "report." not in line, line
    assert all(en != de for en, de in zip(texts["en"], texts["de"]))
    assert any("Desk on" in t and "Desk off" in t and "opposite" in t for t in texts["en"])
    assert any("Desk on" in t and "gegensätzlich" in t for t in texts["de"])
    assert any("keeps retrying" in t for t in texts["en"]) and any("versucht es immer wieder" in t for t in texts["de"])


def test_ai_sees_the_automations_section_only_as_its_summary():
    from lanloom.ai import features
    assert "auto" in features.PRIVATE_SECTIONS


# ---------------------------------------------------------------------- 5. live frame: runs for the board's dots
def test_live_frame_carries_automation_runs_once(tmp_path):
    c, _rep = make_core(tmp_path)
    iid = add_hub(c)
    c.hub[iid] = hb.new_entry(None)
    hb.book(c.hub[iid], {"type": "hub_triggers", "runs": {"automation.a": {"n": 2, "last": NOW}}}, NOW)
    assert c._hub_autoruns() == [[iid, "automation.a", 2]]
    assert c._hub_autoruns() == []


def test_change_ring_keeps_24_hours_and_no_private_entities():
    e = hb.new_entry(None)
    hb.book(e, {"type": "hub_state", "entity_id": "light.a", "state": "on", "t": NOW - 90000, "by": "automation.x"}, NOW)
    hb.book(e, {"type": "hub_state", "entity_id": "person.robin", "state": "home", "t": NOW}, NOW)
    hb.book(e, {"type": "hub_state", "entity_id": "light.a", "state": "off", "t": NOW, "by": "bogus by"}, NOW)
    assert list(e["changes"]) == [(NOW, "light.a", "off", None)]
    assert isinstance(e["changes"], deque)


# ---------------------------------------------------------------------- 6. API and roles (demo data)
def demo_entry():
    now = NOW
    e = hb.new_entry(None)
    from lanloom import demo
    d = demo.Demo.__new__(demo.Demo)
    snap = d._snapshot(now)
    hb.book(e, snap, now)
    import random
    for f in demo_hub.history_frames(now, random.Random(3)) + demo_hub.change_frames(now):
        hb.book(e, f, now)
    hb.book(e, demo_hub.deps_frame(now), now)
    return e


def test_demo_shows_every_part_of_the_tab():
    c = hubauto.Ctx(demo_entry(), NOW)
    tab = hubauto.tab(c)
    assert all(tab["counts"][code] for code in ("dead", "unavailable", "runaway", "conflict", "self_trigger", "loop", "single_wait", "never", "blocked"))
    assert tab["totals"]["off"] == 1 and tab["totals"]["scripts"] == 3
    plan = hubauto.dayplan(c)
    kinds = {m["kind"] for i in plan["items"] for m in i["marks"]}
    assert kinds == {"time", "sun"} and plan["rhythm"] and any(i["windows"] for i in plan["items"])
    assert hubauto.who(c)["conflicts"] and len(hubauto.timeline(c)) > 50
    assert len(hubauto.rooms(c)) >= 6
    h = hubauto.health(c)
    assert {p["state"] for p in h["integrations"]["problems"]} == {"setup_error", "setup_retry"} and h["unavailable_total"] >= 3
    blob = json.dumps(demo_hub.deps_frame(NOW))
    for word in ("Good morning", "chime", "brightness", "family_phones", "Fridge {{"):
        assert word not in blob                                         # the demo flows went through the same filter


@pytest.fixture()
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from lanloom import config as config_mod
    from lanloom.main import create_app
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "0")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    hdr = {"X-Lanloom": "1"}
    with TestClient(app, base_url="http://lanloom.test") as client:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        assert client.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=hdr).status_code == 200
        st = app.state.ll
        client.hub_id = st.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('hub',?,?,1,0)",
                                         ("Sample hub", json.dumps({"url": "http://192.0.2.5:8123", "admin_mode": True}))).lastrowid
        st.core.hub[client.hub_id] = entry()
        st.accounts.create("viewer1", "a-long-test-password", role="viewer")
        client.hdr = hdr
        yield client


def test_api_parts_for_a_viewer_and_404s(api):
    api.cookies.clear()
    assert api.post("/api/login", json={"username": "viewer1", "password": "a-long-test-password"}, headers=api.hdr).status_code == 200
    hid = api.hub_id
    r = api.get("/api/hub/%d/auto" % hid)
    assert r.status_code == 200 and r.json()["totals"]["automations"] == 6
    for part in ("who", "timeline", "plan", "rooms", "health"):
        got = api.get("/api/hub/%d/auto/%s" % (hid, part))
        assert got.status_code == 200, part
    item = api.get("/api/hub/%d/auto/item/automation.desk_off" % hid).json()
    assert item["explain"]["when"][0]["k"] == "auto.ex.t.time" and item["flow"]["action"][0]["svc"] == "switch.turn_off"
    assert api.get("/api/hub/%d/auto/board/switch.desk" % hid).json()["center"]["id"] == "switch.desk"
    assert api.get("/api/hub/%d/auto/item/automation.nope" % hid).status_code == 404
    assert api.get("/api/hub/%d/auto/item/automation.nope" % hid).json()["error"] == "auto.err.item"
    assert api.get("/api/hub/%d/auto/board/person.robin" % hid).status_code == 404
    assert api.get("/api/hub/999/auto").status_code == 404 and api.get("/api/hub/999/auto/who").status_code == 404


def test_api_needs_a_session(api):
    api.cookies.clear()
    assert api.get("/api/hub/%d/auto" % api.hub_id).status_code == 401


def test_tab_stays_quick_with_many_automations():
    e = entry()
    flows = e["deps"]["automations"]
    base = flows["automation.desk_on"]["flow"]
    for i in range(1500):
        eid = "automation.bulk_%d" % i
        flows[eid] = {"flow": base}
        e["snapshot"]["automations"].append({"entity_id": eid, "id": str(i), "mode": "single", "last": NOW - i, "state": "on"})
    for i in range(5000):
        hb.book(e, {"type": "hub_state", "entity_id": "switch.desk", "state": "on" if i % 2 else "off", "t": NOW - i * 10,
                    "by": "automation.bulk_%d" % (i % 1500)}, NOW)
    c = hubauto.Ctx(e, NOW)
    t0 = time.perf_counter()
    hubauto.tab(c)
    hubauto.who(c)
    hubauto.timeline(c)
    hubauto.dayplan(c)
    assert time.perf_counter() - t0 < 4.0                              # generous for a busy test machine
