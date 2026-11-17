"""API of the hub merge feature: sensors list, sensor page history, match rules (roles and audit), the hub list of
/api/state, and the demo's radio networks and history. Sample data only; the app runs in demo mode on a temp dir."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod, demo_hub, hubbridge as hb, hubmerge
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
MAC_TV = "aa:bb:cc:00:00:10"


def hdev(did, name, **kw):
    return dict({"id": did, "name": name, "model": None, "manufacturer": None, "area": None, "via": None, "service": False,
                 "macs": [], "sw": None, "domains": [], "radio": None, "role": None, "ip_hint": None, "host_hint": None}, **kw)


def hent(eid, did, state, **kw):
    return dict({"entity_id": eid, "device_id": did, "name": eid, "state": state, "unit": None, "domain": eid.split(".", 1)[0],
                 "area": None, "dc": None, "cat": None, "platform": "sample_hub", "ls": False}, **kw)


def snapshot(now):
    return {"type": "hub_snapshot", "t": now, "areas": [], "trackers": [], "automations": [], "scripts": [],
            "devices": [hdev("c1", "Mesh stick", radio="mesh154", role="coordinator"),
                        hdev("a", "Lamp A", radio="mesh154", via="c1"), hdev("tv", "Living TV", macs=[MAC_TV])],
            "entities": [hent("sensor.stick", "c1", "online"), hent("light.a", "a", "on"),
                         hent("sensor.a_power", "a", "12.5", unit="W", domain="sensor"), hent("sensor.tv_x", "tv", "1")]}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "0")             # the demo loop would feed its own snapshot into the last hub connection
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as client:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        assert client.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        st = app.state.ll
        client.st = st
        client.hub_id = st.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('hub',?,?,1,0)",
                                         ("Sample hub", json.dumps({"url": "http://192.0.2.5:8123"}))).lastrowid
        now = time.time()
        st.core.hub[client.hub_id] = hb.new_entry(None)
        hb.book(st.core.hub[client.hub_id], snapshot(now), now)
        st.core.hub[client.hub_id]["states"]["sensor.a_power"] = ("13.5", now)
        client.net_id = "d-test-1"
        st.core.store.upsert_device({"id": client.net_id, "ip": "192.168.50.1", "mac": MAC_TV, "data": {}, "kind": "computer",
                                     "confidence": 0.9, "title": "Living TV"}, None)
        for role in ("viewer", "operator"):
            st.accounts.create(role + "1", PW, role=role)
        yield client


def login(c, user):
    c.cookies.clear()
    r = c.post("/api/login", json={"username": user, "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------- sensors
def test_sensors_as_viewer(api):
    login(api, "viewer1")
    r = api.get("/api/hub/%d/sensors" % api.hub_id)
    assert r.status_code == 200
    body = r.json()
    assert body["integration"] == api.hub_id and body["connected"] is True
    assert {"areas", "devices", "entities", "onsite", "radio", "health", "events"} <= set(body)
    assert next(e for e in body["entities"] if e["id"] == "sensor.a_power")["state"] == "13.5"
    assert [n["key"] for n in body["radio"]] == ["mesh154:c1"]
    assert next(d for d in body["devices"] if d["id"] == "tv")["network_device"] == api.net_id


def test_sensors_unknown_id_is_404_and_signed_out_is_refused(api):
    login(api, "viewer1")
    r = api.get("/api/hub/9999/sensors")
    assert r.status_code == 404 and r.json()["error"] == "hub.err.missing"
    api.cookies.clear()
    assert api.get("/api/hub/%d/sensors" % api.hub_id).status_code == 401


# ---------------------------------------------------------------------- entity history
def test_entity_range_1h_as_viewer(api):
    login(api, "viewer1")
    r = api.get("/api/hub/%d/entity/sensor.a_power?range=1h" % api.hub_id)
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "numeric" and body["source"] == "live" and body["range"] == "1h"
    assert body["entity"]["id"] == "sensor.a_power" and body["entity"]["device"] == "Lamp A"
    assert body["points"] and body["points"][-1][1] == 13.5 and body["summary"]["last"] == 13.5
    assert api.get("/api/hub/%d/entity/light.a" % api.hub_id).json()["kind"] == "state"           # the default range is one hour


def test_entity_range_7d_with_the_demo_uses_the_demo_history(api, monkeypatch):
    from lanloom import demo
    monkeypatch.setattr(api.st.core, "demo", demo.Demo())
    login(api, "viewer1")
    r = api.get("/api/hub/%d/entity/sensor.a_power?range=7d" % api.hub_id)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "statistics" and body["kind"] == "numeric" and 0 < len(body["points"]) <= 240


def test_entity_errors(api):
    login(api, "viewer1")
    base = "/api/hub/%d/entity/" % api.hub_id
    r = api.get(base + "sensor.a_power?range=5y")
    assert r.status_code == 409 and r.json()["error"] == "hub.err.range"
    r = api.get(base + "sensor.nothing?range=1h")
    assert r.status_code == 404 and r.json()["error"] == "hub.err.entity"
    r = api.get(base + "person.sam?range=1h")
    assert r.status_code == 404 and r.json()["error"] == "hub.err.entity"
    r = api.get("/api/hub/9999/entity/sensor.a_power?range=1h")
    assert r.status_code == 404 and r.json()["error"] == "hub.err.missing"


# ---------------------------------------------------------------------- match
def post_match(c, action="split", **over):
    body = dict({"device": c.net_id, "hub_device": "tv", "action": action}, **over)
    return c.post("/api/hub/%d/match" % c.hub_id, json=body, headers=HDR)


def audit(c, action):
    return [e for e in c.st.audit.query({}, 200) if e["action"] == action]


def test_match_as_viewer_is_refused_and_audited(api):
    login(api, "viewer1")
    r = post_match(api)
    assert r.status_code == 403 and r.json()["error"] == "auth.err.role"
    assert api.st.core.hub_match_rules() == {"split": [], "join": []}
    rows = audit(api, "hub.match")
    assert len(rows) == 1 and rows[0]["outcome"] == "denied" and rows[0]["actor"] == "viewer1" and rows[0]["detail"] == {"need": "operator"}


def test_match_as_operator_works_and_is_audited(api):
    login(api, "operator1")
    r = post_match(api)
    assert r.status_code == 200 and r.json() == {"ok": True, "action": "split"}
    assert api.st.core.hub_match_rules() == {"split": [[api.net_id, "%d:tv" % api.hub_id]], "join": []}
    rows = [e for e in audit(api, "hub.match") if e["outcome"] != "denied"]
    assert len(rows) == 1 and rows[0]["actor"] == "operator1"
    assert api.net_id in rows[0]["target"] and ("%d:tv" % api.hub_id) in rows[0]["target"]
    assert rows[0]["detail"] == {"action": "split"}
    assert post_match(api, "reset").json() == {"ok": True, "action": "reset"}
    assert api.st.audit.verify()["ok"]


def test_match_errors(api):
    login(api, "operator1")
    r = post_match(api, "bogus")
    assert r.status_code == 400 and r.json()["error"] == "hub.match.invalid"
    r = post_match(api, hub_device=5)
    assert r.status_code == 400 and r.json()["error"] == "hub.match.invalid"
    r = post_match(api, device="no-such-device")
    assert r.status_code == 404 and r.json()["error"] == "device.err.missing"
    r = post_match(api, hub_device="nope")
    assert r.status_code == 400
    assert api.post("/api/hub/9999/match", json={"device": api.net_id, "hub_device": "tv", "action": "split"}, headers=HDR).status_code == 400
    denied_detail = [e["detail"] for e in audit(api, "hub.match")]
    assert {"action": "?"} in denied_detail                                       # an unknown action is audited as "?"
    assert api.st.core.hub_match_rules() == {"split": [], "join": []}


# ---------------------------------------------------------------------- /api/state
def test_state_lists_the_hubs_for_signed_in_users_only(api):
    login(api, "viewer1")
    assert api.get("/api/state").json()["hubs"] == [{"id": api.hub_id, "name": "Sample hub"}]
    api.cookies.clear()
    st = api.get("/api/state").json()
    assert st["hubs"] == [] and st["authenticated"] is False


# ---------------------------------------------------------------------- demo module
def demo_snapshot(now):
    devices, entities, trackers = demo_hub.radio_snapshot(now)
    return {"devices": devices, "entities": entities, "trackers": trackers, "areas": []}, entities


def test_demo_radio_snapshot_has_five_networks():
    now = 1_800_000_000.0
    snap, _ents = demo_snapshot(now)
    view = hubmerge.radio_view(snap, {}, now)
    assert sorted(n["kind"] for n in view["networks"]) == sorted(["mesh154", "thread", "subghz", "ble", "ipstd"])
    assert len(view["networks"]) == 5
    assert all(n["members"] for n in view["networks"])
    assert any(s["fresh"] == "silent" for s in view["state"].values()) and any(s["fresh"] == "ok" for s in view["state"].values())
    site = hubmerge.onsite(snap, {}, view, now)
    assert site["counts"]["now"] and site["counts"]["silent"]


def test_demo_radio_snapshot_has_hardware_addresses_for_matching():
    snap, _ = demo_snapshot(1_800_000_000.0)
    assert any(d["macs"] for d in snap["devices"]) and snap["trackers"]


def test_demo_history_numeric_7d_is_statistics_otherwise_states():
    now = 1_800_000_000.0
    num = {"entity_id": "sensor.sample_temp", "state": "21.0", "domain": "sensor", "unit": "C"}
    res = demo_hub.history(num, "7d", True, now)
    assert res["stats"] and not res["states"] and res["stats"][0][1] <= res["stats"][0][2] <= res["stats"][0][3]
    assert res["stats"][0][0] >= now - 7 * 86400 - 1 and res["stats"][-1][0] <= now
    assert len(demo_hub.history(num, "30d", True, now)["stats"]) > len(res["stats"])
    day = demo_hub.history(num, "24h", True, now)
    assert day["states"] and not day["stats"]
    lamp = {"entity_id": "light.sample_lamp", "state": "off", "domain": "light", "unit": None}
    res = demo_hub.history(lamp, "7d", False, now)
    assert res["states"] and not res["stats"] and {s[1] for s in res["states"]} <= {"on", "off"}
    assert demo_hub.history(num, "7d", True, now) == demo_hub.history(num, "7d", True, now)       # reproducible per entity
