"""Failure chain (S5/H4): the dependency graph over network and hub, both directions with stages and certainties, manual
dependencies (hard/soft/none), the outage automatic, the routes with their role rules and audit entries, the nightly report
section, the sentence in the offline notification, and a controller that lists a disconnected device (no sign of life)."""
import time

import pytest
from fastapi.testclient import TestClient

from lanloom import adapter_inventory, auth, config as config_mod, failure
from lanloom.main import create_app
from lanloom.sources import _ctlmap
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}
SWITCH, AP_UP, AP_GARDEN, HV_HOST, BACKUP, STORAGE, VACUUM = ("d-024c4c000002", "d-024c4c000003", "d-024c4c000004", "d-024c4c00000f",
                                                              "d-024c4c00000c", "d-024c4c00000a", "d-024c4c000041")


# ------------------------------------------------------------------ the search on a hand-built graph
def graph(edges):
    g = failure.Graph()
    for a, b, *_ in edges:
        g.node(a, kind="device", title=a.upper(), tiles=[a])
        g.node(b, kind="device", title=b.upper(), tiles=[b])
    for a, b, cert in edges:
        g.edge(a, b, "port", cert)
    g.index()
    return g


def test_sure_continues_and_likely_ends_the_chain():
    # c and d hang on b (sure), b on a (sure); e needs d only a little (likely), f hangs on e (sure)
    g = graph([("b", "a", "sure"), ("c", "b", "sure"), ("d", "b", "sure"), ("e", "d", "likely"), ("f", "e", "sure")])
    found = failure.Failure.search(g, ["a"], "down")
    assert {n: (r[0], r[1]) for n, r in found.items()} == {"b": (1, "sure"), "c": (2, "sure"), "d": (2, "sure"), "e": (3, "likely")}
    assert "f" not in found                                   # "likely" ends: f is not dragged in through an impaired part
    up = failure.Failure.search(g, ["f"], "up")
    assert {n: r[1] for n, r in up.items()} == {"e": "sure", "d": "likely"}


def test_depth_limit_and_loops_are_safe():
    g = graph([("b", "a", "sure"), ("c", "b", "sure"), ("a", "c", "sure"), ("d", "c", "sure")])
    found = failure.Failure.search(g, ["a"], "down", depth=2)
    assert set(found) == {"b", "c"}                           # stage 3 (d) is beyond the depth
    assert set(failure.Failure.search(g, ["a"], "down", depth=99)) == {"b", "c", "d"}   # the loop back to a is not followed
    assert failure.Failure.search(g, ["a"], "down", depth=0)  # at least one stage


def test_a_sure_way_wins_over_a_likely_one():
    g = graph([("x", "a", "likely"), ("b", "a", "sure"), ("x", "b", "sure")])
    assert failure.Failure.search(g, ["a"], "down")["x"][:2] == (2, "sure")


# ------------------------------------------------------------------ manual entries
def test_manual_entries_are_checked_and_cleaned():
    store = Store(":memory:")
    failure.set_manual(store, "a", "b", "hard", "  keeps\x00 its data there  ")
    assert failure.manual_entries(store)["a"]["b"]["k"] == "hard"
    assert failure.manual_entries(store)["a"]["b"]["note"] == "keeps its data there"
    with pytest.raises(ValueError, match="fail.err.kind"):
        failure.set_manual(store, "a", "c", "maybe")
    for i in range(failure.MAX_MANUAL_PER_NODE - 1):
        failure.set_manual(store, "a", "t%d" % i, "soft")
    with pytest.raises(ValueError, match="fail.err.too_many"):
        failure.set_manual(store, "a", "one-more", "soft")
    failure.set_manual(store, "a", "b", None)
    assert "b" not in failure.manual_entries(store)["a"]
    assert len(failure.set_manual(store, "a", "x" * 3, "none", "n" * 500)["a"]["xxx"]["note"]) == failure.NOTE_MAX


# ------------------------------------------------------------------ the demo: network, hub, manual entries, outage
@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("LANLOOM_DATA", str(tmp_path_factory.mktemp("failure")))
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
        deadline = time.time() + 30
        while time.time() < deadline:      # the topology of the demo arrives with its first controller and counter readings
            r = c.get("/api/failure/%s?dir=down" % SWITCH).json()
            if r.get("counts", {}).get("automations"):
                break
            time.sleep(0.5)
        yield app, as_
    mp.undo()


def test_switch_failure_reaches_devices_hub_and_automations(demo):
    _app, as_ = demo
    r = as_("viewer").get("/api/failure/%s?dir=down" % SWITCH).json()
    c = r["counts"]
    # the hub runs as a guest on a host behind the switch: its devices, entities and automations hang on the switch too
    assert c["devices"] >= 20 and c["hub_devices"] >= 20 and c["automations"] >= 10 and c["entities"] >= 50
    assert r["root"]["id"] == SWITCH and r["dir"] == "down"
    by_id = {i["id"]: i for i in r["items"]}
    assert by_id[AP_UP]["stage"] == 1 and by_id[AP_UP]["why"]["key"] == "port" and by_id[AP_UP]["why"]["vars"]["port"] == "2"
    assert by_id[AP_GARDEN]["stage"] == 2 and by_id[AP_GARDEN]["why"]["key"] == "radio"
    hub = next(i for i in r["items"] if i["kind"] == "hub")
    assert hub["path"][0] == SWITCH and hub["path"][-1] == hub["id"] and len(hub["path_names"]) == len(hub["path"])
    auto = next(i for i in r["items"] if i["kind"] == "automation")
    assert auto["why"]["key"] == "auto_hub" and auto["tiles"] and auto["own"] == 0      # drawn in the hub tile, which is not its own
    assert [s["stage"] for s in r["stages"]] == sorted(s["stage"] for s in r["stages"])


def test_automation_chain_up_names_trigger_condition_and_targets(demo):
    _app, as_ = demo
    r = as_("viewer").get("/api/failure/he:1:automation.hallway_light_on_motion?dir=up").json()
    why = {i["entity_id"]: (i["why"]["key"], i["cert"]) for i in r["items"] if i.get("entity_id")}
    assert why["binary_sensor.hallway_4"] == ("trigger", "sure")        # never starts without what starts it
    assert why["sensor.hallway_1"] == ("condition", "likely")
    assert why["light.hallway_2"] == ("acts", "likely")                  # the light it switches
    kinds = {i["kind"] for i in r["items"]}
    assert {"hub", "device", "hub_device", "entity"} <= kinds            # down to the host of the hub and the switch
    # the light is not switched automatically any more when the automation fails
    down = as_("viewer").get("/api/failure/he:1:automation.hallway_light_on_motion?dir=down").json()
    assert any(i.get("entity_id") == "light.hallway_2" and i["why"]["key"] == "switched" for i in down["items"])


def test_map_ids_resolve_and_unknown_ids_are_404(demo):
    _app, as_ = demo
    c = as_("viewer")
    assert c.get("/api/failure/rd:1:mesh-motion-hall?dir=up").json()["root"]["id"] == "hd:1:mesh-motion-hall"
    assert c.get("/api/failure/hub:1").status_code in (200, 404)
    assert c.get("/api/failure/d-nope").status_code == 404
    assert c.get("/api/failure/d-nope").json()["error"] == "fail.err.unknown"
    assert c.get("/api/failure/%s?dir=sideways" % SWITCH).status_code == 400


def test_hub_side_of_a_merged_device_is_marked(demo):
    _app, as_ = demo
    r = as_("viewer").get("/api/failure/%s?dir=down" % HV_HOST).json()
    vac = next(i for i in r["items"] if i["kind"] == "hub_device" and i["tiles"][0] == VACUUM)
    assert vac["side"] == "hub"                                          # the board marks the vacuum tile impaired, not failed
    assert VACUUM not in {i["id"] for i in r["items"]}                   # the network device itself runs on


def test_manual_dependency_decides_the_pair_and_lands_in_the_audit_log(demo):
    app, as_ = demo
    c = as_("viewer")
    deps = c.get("/api/failure/deps/%s" % BACKUP).json()
    assert any(m["target"] == STORAGE and m["k"] == "hard" for m in deps["manual"])           # from the demo
    assert any(u["id"] == STORAGE and u["why"]["key"] == "manual_hard" for u in deps["up"])
    assert any(i["id"] == BACKUP for i in c.get("/api/failure/%s" % STORAGE).json()["items"])
    # a viewer reads, never writes
    assert c.put("/api/failure/deps/%s" % BACKUP, json={"target": STORAGE, "kind": None}, headers=HDR).status_code == 403
    c = as_("operator")
    # "none" removes the automatic edge between the pair: the backup server no longer hangs on the switch
    r = c.put("/api/failure/deps/%s" % BACKUP, json={"target": SWITCH, "kind": "none", "note": "own uplink"}, headers=HDR)
    assert r.status_code == 200 and r.json()["k"] == "none"
    via = {i["id"]: i for i in c.get("/api/failure/%s?dir=down" % SWITCH).json()["items"]}[BACKUP]
    assert via["why"]["key"] == "manual_hard" and via["path"] == [SWITCH, STORAGE, BACKUP]     # only through the storage it writes to
    acts = [(e["action"], e["actor"]) for e in app.state.ll.audit.query({}, 20)]
    assert ("failure.dependency", "operator1") in acts
    assert c.put("/api/failure/deps/%s" % BACKUP, json={"target": SWITCH, "kind": None}, headers=HDR).json()["k"] is None
    back = {i["id"]: i for i in c.get("/api/failure/%s?dir=down" % SWITCH).json()["items"]}[BACKUP]
    assert back["stage"] == 1 and back["why"]["key"] == "port"
    # bad requests
    assert c.put("/api/failure/deps/%s" % BACKUP, json={"target": BACKUP, "kind": "hard"}, headers=HDR).json()["error"] == "fail.err.self"
    assert c.put("/api/failure/deps/%s" % BACKUP, json={"target": "d-nope", "kind": "hard"}, headers=HDR).json()["error"] == "fail.err.unknown"
    assert c.put("/api/failure/deps/%s" % BACKUP, json={"target": STORAGE, "kind": "maybe"}, headers=HDR).status_code == 400
    assert c.put("/api/failure/deps/he:1:automation.night_mode", json={"target": STORAGE, "kind": "hard"}, headers=HDR).json()["error"] == "fail.err.kind"


def test_outage_automatic_marks_what_hangs_on_the_garden_access_point(demo):
    _app, as_ = demo
    o = as_("viewer").get("/api/failure/outages").json()
    assert [x["id"] for x in o["outages"]] == [AP_GARDEN]                # the demo outage; phones and computers never start it
    root = o["outages"][0]
    assert root["counts"]["total"] == o["counts"]["total"] > 5
    ids = {i["id"]: i for i in o["items"]}
    assert VACUUM in ids and ids[VACUUM]["causes"] == [AP_GARDEN]
    auto = [i for i in o["items"] if i["kind"] == "automation"]
    assert [a["entity_id"] for a in auto] == ["automation.vacuum_on_weekdays"] and auto[0]["cert"] == "likely"   # dashed in the tab


def test_candidates_and_board_extra_edges(demo):
    _app, as_ = demo
    c = as_("viewer")
    cands = c.get("/api/failure/candidates").json()["items"]
    assert {x["kind"] for x in cands} <= {"device", "node", "container", "connection", "hub", "internet"} and any(x["id"] == STORAGE for x in cands)
    assert any(x["id"] == "internet" for x in cands)        # "needs the internet" for the emergency scenario (S13)
    extra = c.get("/api/failure/board?view=network").json()["extra"]
    assert [BACKUP, STORAGE, "sure"] in extra and [HV_HOST, STORAGE, "likely"] in extra
    assert c.get("/api/failure/board?view=nonsense").status_code == 400


def test_offline_notification_names_what_depends_on_the_device(demo):
    app, _as = demo
    core = app.state.ll.core
    extra = core.notify._chain_of(SWITCH)
    assert extra["affected"] > 20 and extra["autos"] >= 10
    assert core.notify._chain_of("d-nope") == {}
    from lanloom.notify.events import get
    msg = core.notify.message(get("dev_offline"), dict({"title": "Core switch"}, **extra), "warning", {"lang": "en"})
    assert "Core switch is offline." in msg["text"] and "probably depend on it, among them" in msg["text"]
    plain = core.notify.message(get("dev_offline"), {"title": "Phone"}, "info", {"lang": "en"})
    assert plain["text"] == "Phone is offline."


def test_nightly_section_lists_the_outage_of_the_night(demo):
    app, _as = demo
    core = app.state.ll.core
    from lanloom import failure_report
    from lanloom.nightly.contrib import Ctx
    now = time.time()
    core.reports.journal.add("dev_offline", {"id": AP_GARDEN, "title": "Access point garden", "kind": "ap"}, now - 600)
    ctx = Ctx(core=core, journal=core.reports.journal, since=now - 3600, until=now + 1, prev=None)
    assert failure_report._available(ctx)
    sec = failure_report._build(ctx)
    item = next(i for i in sec["items"] if i["id"].startswith("fc:" + AP_GARDEN))
    assert item["key"] == "fail.report.down_now" and item["point"] and item["vars"]["autos"] == 1
    assert sec["summary"]["key"] == "fail.report.summary"


# ------------------------------------------------------------------ a controller lists disconnected devices too: no sign of life
def test_disconnected_controller_device_is_no_sign_of_life():
    assert _ctlmap.map_device({"mac": "02:aa:bb:cc:dd:01", "type": "uap", "state": 0})["online"] is False
    assert _ctlmap.map_device({"mac": "02:aa:bb:cc:dd:01", "type": "uap", "state": 6})["online"] is False
    assert "online" not in _ctlmap.map_device({"mac": "02:aa:bb:cc:dd:01", "type": "uap", "state": 1})
    assert "online" not in _ctlmap.map_device({"mac": "02:aa:bb:cc:dd:01", "type": "uap", "state": False})
    obs = {"int:1": {"integ": 1, "ev": {"type": "topology", "source": "controller",
                                         "devices": [{"mac": "02:aa:bb:cc:dd:01", "role": "ap", "online": False},
                                                     {"mac": "02:aa:bb:cc:dd:02", "role": "ap"}],
                                         "clients": [{"mac": "02:aa:bb:cc:dd:03", "parent_mac": "02:aa:bb:cc:dd:01", "medium": "radio"}]}}}
    recs = {r["mac"]: r for r in adapter_inventory.records(obs)}
    assert recs["02:aa:bb:cc:dd:01"].get("offline") is True
    assert "offline" not in recs["02:aa:bb:cc:dd:02"] and "offline" not in recs["02:aa:bb:cc:dd:03"]
    # seen as a client somewhere else at the same time: it answers after all
    obs["int:2"] = {"integ": 2, "ev": {"type": "topology", "source": "controller", "clients": [{"mac": "02:aa:bb:cc:dd:01"}]}}
    assert "offline" not in {r["mac"]: r for r in adapter_inventory.records(obs)}["02:aa:bb:cc:dd:01"]
