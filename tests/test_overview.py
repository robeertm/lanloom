"""Overview (start page) and the "Right now" rail: key figures, calm source states, noteworthy list, roles, demo data."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod, live, overview
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.data_dir = tmp_path
        c.app_ref = app
        yield c


def _setup_and_scan(c):
    code = (Path(c.data_dir) / "setup-code.txt").read_text().strip()
    assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
    nets = c.get("/api/networks").json()
    assert c.post("/api/scan", json={"cidrs": [nets["networks"][0]["cidr"]]}, headers=HDR).status_code == 200
    for _ in range(80):
        if c.get("/api/scan").json()["current"]["state"] != "running":
            break
        time.sleep(0.25)


def test_overview_needs_sign_in(client):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview/now").status_code == 401


def test_overview_summary_in_demo(client):
    _setup_and_scan(client)
    s = client.get("/api/overview").json()
    for key in ("devices", "sources", "wan", "paths", "report", "health", "notes", "new", "unsure", "source_list"):
        assert key in s, key
    d = s["devices"]
    assert d["total"] > 20 and 0 <= d["online"] <= d["total"]
    assert s["health"]["level"] in overview.LEVELS
    # demo: a day of health history so the look back is never empty
    assert len(s["health"]["history"]) == overview.HISTORY_LEN
    assert all(len(p) == 4 for p in s["health"]["history"])
    for src in s["source_list"]:
        assert src["state"] in ("fresh", "stale", "waiting", "nodata", "failed", "off")
    for n in s["notes"]:
        assert n["level"] in ("info", "warn", "danger") and n["key"].startswith("ov.note.") and n["link"].startswith("#/")


def test_right_now_in_demo(client):
    _setup_and_scan(client)
    time.sleep(2.5)                                         # a few live frames
    r = client.get("/api/overview/now").json()
    assert set(r) >= {"talks", "changes", "new", "notes", "t"}
    assert r["changes"], "the demo always has a few recent changes"
    kinds = {c["kind"] for c in r["changes"]}
    assert kinds <= set(overview.CHANGE_KINDS)
    for tk in r["talks"]:
        assert tk["r0"] + tk["r1"] >= overview.TALK_FROM and tk["a"]["id"] and tk["b"]["id"]


def test_viewer_may_read_overview():
    from lanloom import access
    assert access.RULES[("GET", "/api/overview")].role == "viewer"
    assert access.RULES[("GET", "/api/overview/now")].role == "viewer"


# ---------------------------------------------------------------------- calm source states (no error text for "nothing yet")
class _Core:
    def __init__(self):
        self.source_status, self.cadence = {}, {}

    def _source_grade(self, key):
        cad = self.cadence.get(key)
        return cad.grade() if cad else "unknown"


def test_source_waiting_nodata_stale_failed_fresh_off():
    core, now = _Core(), time.time()
    assert overview.source_state(core, "int:1", now)["state"] == "waiting"
    core.source_status["int:1"] = {"ok": True, "t": now - 10}
    assert overview.source_state(core, "int:1", now)["state"] == "waiting"      # just connected
    core.source_status["int:1"] = {"ok": True, "t": now - 600}
    assert overview.source_state(core, "int:1", now)["state"] == "nodata"
    cad = core.cadence["int:1"] = live.Cadence()
    for i in range(5):
        cad.tick(now - 50 + i * 10)
    assert overview.source_state(core, "int:1", now)["state"] == "fresh"
    cad2 = core.cadence["int:2"] = live.Cadence()
    for i in range(5):
        cad2.tick(now - 900 + i * 10)
    assert overview.source_state(core, "int:2", now)["state"] == "stale"
    core.source_status["int:2"] = {"ok": False, "key": "source.mqtt.timeout", "t": now}
    st = overview.source_state(core, "int:2", now)
    assert st["state"] == "failed" and st["key"] == "source.mqtt.timeout"
    assert overview.source_state(core, "int:2", now, enabled=False)["state"] == "off"


def test_new_devices_skip_first_inventory():
    ov = overview.Overview(SimpleNamespace())
    now = time.time()
    devs = [{"id": "a", "first_seen": now - 3 * 3600, "is_self": False, "hidden": False},
            {"id": "b", "first_seen": now - 3 * 3600 + 60, "is_self": False, "hidden": False},
            {"id": "c", "first_seen": now - 600, "is_self": False, "hidden": False},
            {"id": "s", "first_seen": now - 300, "is_self": True, "hidden": False}]
    assert [d["id"] for d in ov._new(devs, now)] == ["c"]


def test_registered_notes_are_merged_and_sorted():
    calls = []

    def extra(core, now):
        calls.append(now)
        return [{"id": "incident:1", "level": "danger", "key": "ov.note.wan_down", "vars": {}, "link": "#/overview"}]

    overview.register_note(extra)
    try:
        ov = overview.Overview(SimpleNamespace())
        notes = ov.notes(time.time(), devs=[], sources=[], wan={"state": "up"}, report={}, new=[])
        assert notes[0]["id"] == "incident:1" and calls
    finally:
        overview.NOTES.remove(extra)


def test_an_offline_device_without_a_known_start_has_no_time_in_its_line():
    # a controller can report a device as disconnected before Lanloom saw it go: no start, so no "{age}" left in the text
    now = time.time()
    dev = {"id": "d1", "title": "Access point garden", "ip": "192.168.50.4", "kind": "ap", "hidden": False, "status": "down",
           "was_up": True, "is_gateway": False}
    ov = overview.Overview(SimpleNamespace())
    unknown = ov.notes(now, devs=[dict(dev, down_since=None)], sources=[], wan={"state": "up"}, report={}, new=[])
    known = ov.notes(now, devs=[dict(dev, down_since=now - 600)], sources=[], wan={"state": "up"}, report={}, new=[])
    note = next(n for n in unknown if n["id"] == "dev:d1")
    assert note["sub"] == "ov.note.dev_offline.sub_unknown" and note["vars"]["age_dur"] is None
    note = next(n for n in known if n["id"] == "dev:d1")
    assert note["sub"] == "ov.note.dev_offline.sub" and note["vars"]["age_dur"] == 600
    import json
    for lang in ("en", "de"):
        texts = json.loads((Path(overview.__file__).parent / "i18n" / ("%s.json" % lang)).read_text(encoding="utf-8"))
        assert "{age}" not in texts["ov.note.dev_offline.sub_unknown"] and "{kind}" in texts["ov.note.dev_offline.sub_unknown"]
