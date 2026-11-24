"""Resilience (S13): the seven axes as pure functions, risk, levels, score and grade, own fields per part, the power cut with the
UPS slider, internet down and single parts as scenarios, the layers, the risk lens, the routes with their role rules and audit
entries, the nightly report section and the sample data of the demo."""
import time

import pytest
from fastapi.testclient import TestClient

from lanloom import auth, config as config_mod, resilience as rs
from lanloom.main import create_app
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}
NOW = 1_800_000_000.0


# ------------------------------------------------------------------ pure parts
def test_levels_score_and_grade():
    assert [rs.level_of(x) for x in (0, 14.9, 15, 29.9, 30, 44.9, 45, 59.9, 60, 100)] == [
        "low", "low", "moderate", "moderate", "elevated", "elevated", "high", "high", "critical", "critical"]
    assert rs.level_of(None) is None
    assert rs.score_of([]) is None
    sc = rs.score_of([(10.0, 0.2), (12.0, 0.1), (8.0, 0.0)])
    assert sc["score"] >= 85 and sc["grade"] == "A"
    bad = rs.score_of([(70.0, 1.0), (65.0, 1.0), (60.0, 0.9), (10.0, 0.0)])
    assert bad["grade"] in ("E", "F") and bad["score"] < 40
    assert [rs.grade_of(x) for x in (100, 85, 84, 72, 60, 48, 36, 35, 0)] == ["A", "A", "B", "B", "C", "D", "E", "F", "F"]


def test_impact_grows_with_what_hangs_on_it():
    none = rs.axis_impact({"sure": {}, "likely": 0})
    assert none["weak"] == 0 and none["value"] == "none"
    few = rs.axis_impact({"sure": {"device": 3}, "likely": 1})
    many = rs.axis_impact({"sure": {"device": 40, "automation": 20, "entity": 400}, "likely": 0})
    assert 0 < few["weak"] < many["weak"] <= 1.0
    assert rs.axis_impact({"sure": {"entity": 20}, "likely": 0})["weak"] < rs.axis_impact({"sure": {"device": 20}, "likely": 0})["weak"]
    assert few["why"] == [{"key": "impact_devices", "vars": {"n": 3}}, {"key": "impact_likely", "vars": {"n": 1}}]


def test_impact_total_equals_its_breakdown():
    sure = {"device": 1, "hub_device": 1, "entity": 11, "connection": 2, "hub": 1, "automation": 3, "script": 1, "node": 2}
    ax = rs.axis_impact({"sure": sure, "likely": 0})
    head, also = ax["why"]
    assert head == {"key": "impact_devices", "vars": {"n": 3}}
    assert head["vars"]["n"] + sum(also["vars"].values()) == sum(sure.values()) == 22      # #15: the parts add up to the total
    assert also["vars"] == {"hub_devices": 1, "automations": 4, "entities": 11, "connections": 3}
    assert (ax["devices"], ax["autos"], ax["entities"], ax["links"]) == (3, 4, 11, 3)


def test_impact_leads_with_the_devices_and_lists_the_rest():
    """#20: the headline is about the devices, the other groups go on their own line (the browser drops the zeros)."""
    big = rs.axis_impact({"sure": {"device": 29, "hub_device": 41, "entity": 457, "connection": 4}, "likely": 0})
    assert big["why"][0] == {"key": "impact_devices", "vars": {"n": 29}}
    also = big["why"][1]
    assert also == {"key": "impact_also", "vars": {"hub_devices": 41, "automations": 0, "entities": 457, "connections": 4}}
    assert 29 + sum(also["vars"].values()) == 531
    small = rs.axis_impact({"sure": {"hub_device": 1, "entity": 3}, "likely": 2})
    assert small["why"][0] == {"key": "impact_no_devices", "vars": {}}
    assert small["why"][2] == {"key": "impact_likely", "vars": {"n": 2}}
    assert rs.axis_impact({"sure": {"device": 2}, "likely": 0})["why"] == [{"key": "impact_devices", "vars": {"n": 2}}]
    assert all(r["key"] != "impact_sure" for r in big["why"] + small["why"])


def test_risk_counts_unknown_as_half_and_skips_what_does_not_count():
    axes = {k: {"weak": 0.0} for k in rs.WEIGHT}
    assert rs.risk_of(axes, 1.0)[0] == 0
    unknown = {k: {"weak": None} for k in rs.WEIGHT}
    r, weak = rs.risk_of(unknown, 1.0)
    assert weak == pytest.approx(0.5) and r == pytest.approx(50.0)
    axes["backup"] = {"weak": None, "counts": False}
    axes["power"] = {"weak": 1.0}
    with_skip, _ = rs.risk_of(axes, 1.0)
    axes["backup"] = {"weak": 0.0}
    without, _ = rs.risk_of(axes, 1.0)
    assert with_skip > without                       # the part without backup data is not made better by a 0
    assert rs.risk_of({k: {"weak": 1.0} for k in rs.WEIGHT}, 0.0)[0] == pytest.approx(30.0)   # nothing hangs on it: at most 30


def test_spare_backup_power_currency_watch_health():
    assert rs.axis_spare(None, False)["value"] == "not_needed"
    assert rs.axis_spare(None, True)["weak"] is None
    assert rs.axis_spare("cold", True)["weak"] == 0.35 and rs.axis_spare("none", True)["weak"] == 1.0
    assert rs.axis_spare(None, True, reserve_line="Mobile")["weak"] == 0.0
    assert rs.axis_backup([], True, False, NOW)["weak"] == 1.0
    assert rs.axis_backup([], False, True, NOW)["weak"] is None
    assert rs.axis_backup([], False, False, NOW)["counts"] is False
    ok = rs.axis_backup([{"name": "Nightly", "status": "ok", "restore_t": NOW - 10 * 86400}], True, False, NOW)
    assert ok["weak"] == 0.0 and ok["value"] == "checked"
    old = rs.axis_backup([{"name": "Nightly", "status": "ok", "restore_t": NOW - 200 * 86400}], True, False, NOW)
    assert old["weak"] == 0.25 and old["why"][-1]["key"] == "backup_no_restore"
    assert rs.axis_backup([{"name": "N", "status": "failed"}, {"name": "M", "status": "late"}], True, False, NOW)["weak"] == 0.5
    assert rs.axis_power(None, "none")["weak"] is None
    assert rs.axis_power("mains", "own")["weak"] == 1.0
    assert rs.axis_power("ups", "own", ups={"state": "replace", "name": "UPS"})["weak"] == 0.35
    assert rs.axis_power("ups", "via", "Core switch")["why"][0] == {"key": "power_ups_via", "vars": {"via": "Core switch"}}
    assert rs.axis_power("ups", "derived", ups={"name": "Rack"})["why"][0]["key"] == "power_derived"
    cur = rs.axis_currency([{"id": "CVE-1", "severity": "high", "exploited": True}], {"state": "past", "until": "2025-01-01"},
                           [{"name": "web:443", "tier": "warn", "days": 9}])
    assert cur["weak"] == 1.0 and cur["value"] == "vulnerable"
    assert rs.axis_currency([], {"state": "soon", "until": "2027-01-01"}, [])["weak"] == 0.3
    assert rs.axis_currency([], None, [])["weak"] == 0.0
    assert rs.axis_watch(True, "agent")["weak"] == 0.0 and rs.axis_watch(True, None)["weak"] == 0.5 and rs.axis_watch(False, None)["weak"] == 1.0
    assert rs.axis_health(None, [])["weak"] is None
    assert rs.axis_health({"share35": 0.9995, "incidents7": 0}, [])["weak"] == 0.0
    assert rs.axis_health({"share35": 0.985, "incidents7": 4}, [])["weak"] == pytest.approx(0.65)
    assert rs.axis_health({"share35": 1.0, "incidents7": 0}, [{"metric": "disk", "eta_h": 30}])["weak"] == 0.6


def test_recommendation_follows_the_biggest_share():
    axes = {"impact": {"weak": 1.0}, "spare": {"weak": 1.0, "value": "none"}, "backup": {"weak": 0.0}, "power": {"weak": 0.1, "value": "ups"},
            "currency": {"weak": 0.0}, "watch": {"weak": 0.0}, "health": {"weak": 0.0}}
    assert rs.recommendation(axes, True)["key"] == "spare"
    assert rs.recommendation(axes, False)["key"] == "spare_device"
    axes["impact"] = {"weak": 0.1}                    # a spare is only named for central parts
    axes["power"] = {"weak": 1.0, "value": "mains"}
    assert rs.recommendation(axes, True) == {"key": "power", "effort": "small", "axis": "power"}
    axes["power"] = {"weak": None, "value": "unknown"}
    assert rs.recommendation(axes, True)["key"] == "power_unknown"
    calm = {k: {"weak": 0.0} for k in rs.AXES}
    assert rs.recommendation(calm, True) is None


def test_own_fields_and_settings_are_cleaned():
    store = Store(":memory:")
    assert rs.set_part(store, "d-1", {"power": "ups", "spare": "cold", "note": "  on\x00 the shelf  "}, NOW) == {
        "power": "ups", "spare": "cold", "note": "on the shelf", "t": int(NOW)}
    with pytest.raises(ValueError, match="rs.err.power"):
        rs.clean_part({"power": "solar"})
    with pytest.raises(ValueError, match="rs.err.spare"):
        rs.clean_part({"spare": "maybe"})
    assert rs.clean_part({"note": "x" * 500})["note"] == "x" * rs.NOTE_MAX
    assert rs.set_part(store, "d-1", {"power": "", "spare": None}) is None and "d-1" not in rs.parts_of(store)
    assert rs.settings_of(store) == {"runtime_min": None, "reserve_min": 0}
    assert rs.clean_settings({"runtime_min": 45, "reserve_min": 5}) == {"runtime_min": 45, "reserve_min": 5}
    assert rs.clean_settings({"runtime_min": None}) == {"runtime_min": None}
    for bad in ({"runtime_min": 0}, {"runtime_min": True}, {"reserve_min": 500}, {}):
        with pytest.raises(ValueError):
            rs.clean_settings(bad)


def test_ups_cutoff_takes_the_live_runtime_and_the_reserve():
    assert rs.Resilience.ups_cutoff({"runtime_s": 1800}, {"reserve_min": 5}) == (1500, "live")
    assert rs.Resilience.ups_cutoff({"runtime_s": None}, {"runtime_min": 20, "reserve_min": 0}) == (1200, "setting")
    assert rs.Resilience.ups_cutoff(None, {"runtime_min": None, "reserve_min": 0}) == (None, None)
    assert rs.Resilience.ups_cutoff({"runtime_s": 120}, {"reserve_min": 5}) == (0, "live")


# ------------------------------------------------------------------ the demo: whole computation, routes, roles, audit
@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("LANLOOM_DATA", str(tmp_path_factory.mktemp("resilience")))
    mp.setenv("LANLOOM_DEMO", "1")
    mp.setenv("LANLOOM_DEMO_SEED", "1")
    mp.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        ll = app.state.ll
        users = {role: ll.accounts.create(role + "1", "a-long-test-password", role=role) for role in ("viewer", "operator")}
        cookies = {role: ll.sessions.create(uid, ip="127.0.0.1", agent="test") for role, uid in users.items()}

        def as_(role):
            c.cookies.set(auth.SESSION_COOKIE, cookies[role])
            return c
        as_("viewer")
        deadline = time.time() + 40
        while time.time() < deadline:      # the topology, the UPS reading and the sample fields arrive with the first rounds of the demo
            if ll.core.store.get_setting("demo_resilience_seeded", False) and ll.core.controlroom.ups.units:
                r = c.get("/api/failure/d-024c4c000002?dir=down").json()
                if r.get("counts", {}).get("automations"):
                    break
            time.sleep(0.5)
        ll.core.resilience.invalidate()
        yield app, as_
    mp.undo()


def ip(app, n):
    return app.state.ll.core.ip_to_device["192.168.50.%d" % n]


def test_demo_has_a_score_weak_spots_and_reasons(demo):
    app, as_ = demo
    d = as_("viewer").get("/api/resilience").json()
    sc = d["score"]
    assert sc and 0 <= sc["score"] <= 100 and sc["grade"] in "ABCDEF" and sc["n"] >= 5
    assert sum(sc["levels"].values()) == sc["n"]
    assert 3 <= len(d["weak_spots"]) <= rs.TOP
    risks = [w["risk"] for w in d["weak_spots"]]
    assert risks == sorted(risks, reverse=True)
    assert all(w["rec"] is None or w["rec"]["key"] in {v[0] for v in rs.REC_AXIS.values()} for w in d["weak_spots"])
    assert "_rated" not in d and "_lens" not in d
    assert d["history"] and d["history"][-1][1] == sc["score"]
    # the virtualization host is on the mains and carries the hub: it is a top weak spot with a reason
    top = {w["id"] for w in d["weak_spots"][:4]}
    assert ip(app, 15) in top


def test_power_follows_poe_and_guests(demo):
    app, as_ = demo
    c = as_("viewer")
    ap = c.get("/api/resilience/part/" + ip(app, 3)).json()           # PoE on the core switch, which is on the UPS
    power = next(a for a in ap["axes"] if a["axis"] == "power")
    assert power["value"] == "ups" and power["source"] == "via" and power["why"][0]["key"] == "power_ups_via"
    storage = c.get("/api/resilience/part/" + ip(app, 10)).json()     # the UPS reports through the storage box
    p2 = next(a for a in storage["axes"] if a["axis"] == "power")
    assert p2["source"] == "derived" and p2["weak"] == 0.1
    hub = c.get("/api/resilience/part/" + ip(app, 14)).json()         # a guest of the virtualization host (mains)
    p3 = next(a for a in hub["axes"] if a["axis"] == "power")
    assert p3["value"] == "mains" and p3["source"] == "via"
    assert {a["axis"] for a in hub["axes"]} == set(rs.AXES)
    assert c.get("/api/resilience/part/nonsense").status_code == 404


def test_power_cut_slider(demo):
    app, as_ = demo
    c = as_("viewer")
    d = c.get("/api/resilience").json()
    power = next(s for s in d["scenarios"] if s["id"] == "power")
    assert power["basis"] == "live" and power["cut_s"] is not None and power["reserve_min"] == 5
    assert power["hub_off_min"] == 0                                  # the hub runs on a host without UPS: off at once
    early = c.get("/api/resilience/scenario/power?minute=1").json()
    late = c.get("/api/resilience/scenario/power?minute=200").json()
    assert early["running"] and not late["running"]
    assert late["counts"]["total"] > early["counts"]["total"] > 0
    assert {r["id"] for r in early["roots"]} == {g["id"] for grp in power["groups"] if grp["id"] == "mains" for g in grp["items"]}
    assert ip(app, 2) in {r["id"] for r in late["roots"]}             # the switch on the UPS is off once the battery is empty
    assert early["title"]["key"] == "rs.sc.power.title" and early["dir"] == "down"
    assert all("stage" in it and "why" in it for it in late["items"])
    assert c.get("/api/resilience/scenario/power?minute=-5").status_code == 400
    assert c.get("/api/resilience/scenario/nope").status_code == 404


def test_internet_and_node_scenarios_and_layers(demo):
    app, as_ = demo
    c = as_("viewer")
    d = c.get("/api/resilience").json()
    net = next(s for s in d["scenarios"] if s["id"] == "internet")
    assert net["reserve"] == "Mobile backup" and net["counts"]["likely"] >= 1 and net["manual_n"] >= 1
    gw = next(s for s in d["scenarios"] if s["kind"] == "node" and s["role"] == "gateway")
    assert gw["counts"]["devices"] > 10 and gw["hub_off"] is True
    sc = c.get("/api/resilience/scenario/" + gw["id"]).json()
    assert sc["root"]["id"] == gw["node"] and sc["counts"]["total"] >= gw["counts"]["sure"]
    layers = {l["id"]: l for l in d["layers"]}
    assert {"power", "gateway", "network", "servers"} <= set(layers)
    assert layers["gateway"]["breaks"].get("network", 0) >= 1 and layers["gateway"]["level"] in rs.LEVELS


def test_lens_tiles_and_view_check(demo):
    app, as_ = demo
    c = as_("viewer")
    lens = c.get("/api/resilience/lens?view=network").json()
    assert lens["tiles"] and all(v["l"] in rs.LEVELS and 0 <= v["r"] <= 100 for v in lens["tiles"].values())
    assert ip(app, 1) in lens["tiles"]
    assert c.get("/api/resilience/lens?view=overview").status_code == 200
    assert c.get("/api/resilience/lens?view=nonsense").status_code == 400


def test_fields_need_an_operator_and_land_in_the_audit_log(demo):
    app, as_ = demo
    node = ip(app, 21)
    assert as_("viewer").put("/api/resilience/part/" + node, json={"power": "ups"}, headers=HDR).status_code == 403
    assert as_("viewer").put("/api/resilience/settings", json={"reserve_min": 2}, headers=HDR).status_code == 403
    c = as_("operator")
    r = c.put("/api/resilience/part/" + node, json={"power": "mains", "spare": "none", "note": "desk"}, headers=HDR)
    assert r.status_code == 200 and r.json()["fields"]["power"] == "mains"
    part = c.get("/api/resilience/part/" + node).json()
    assert part["fields"]["note"] == "desk" and next(a for a in part["axes"] if a["axis"] == "power")["value"] == "mains"
    assert c.put("/api/resilience/part/" + node, json={"power": "solar"}, headers=HDR).json()["error"] == "rs.err.power"
    assert c.put("/api/resilience/part/" + node, json={"power": None, "spare": None}, headers=HDR).json()["fields"] == {}
    assert c.put("/api/resilience/settings", json={"runtime_min": 25, "reserve_min": 5}, headers=HDR).json() == {"runtime_min": 25, "reserve_min": 5}
    assert c.put("/api/resilience/settings", json={"reserve_min": 999}, headers=HDR).status_code == 400
    acts = {(e["action"], e["actor"]) for e in app.state.ll.audit.query({}, 50)}
    assert ("resilience.part", "operator1") in acts and ("resilience.settings", "operator1") in acts


def test_nightly_section_only_with_something_to_tell(demo):
    app, as_ = demo
    from types import SimpleNamespace
    from lanloom import resilience_report as rr
    core = app.state.ll.core
    now = time.time()
    ctx = SimpleNamespace(core=core, since=now - 86400, until=now)
    core.store.set_setting(rs.HISTORY, [["2000-01-01", 99, "A"]])     # yesterday's score was much better
    core.resilience.invalidate()
    assert rr._available(ctx)
    sec = rr._build(ctx)
    assert sec["id"] == "resilience" and any(i["key"] == "rs.report.score_down" and i["point"] for i in sec["items"])
    hist = core.resilience.history()
    core.store.set_setting(rs.HISTORY, [["2000-01-01", hist[-1][1], hist[-1][2]]] + [h for h in hist if h[0] != "2000-01-01"])
    core.resilience.invalidate()
    crit = [w for w in core.resilience.result(now)["weak_spots"] if w["level"] == "critical"]
    assert rr._available(ctx) == bool(crit)


def test_cache_and_budget(demo):
    app, _as = demo
    r = app.state.ll.core.resilience
    t0 = time.perf_counter()
    r.result(fresh=True)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    r.result()
    warm = time.perf_counter() - t0
    assert warm < 0.05 and cold < 5.0
