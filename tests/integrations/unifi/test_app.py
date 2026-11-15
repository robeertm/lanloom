"""The family's live configuration inside the app: core bookkeeping, the firewall editor's live view and the API."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import core
from lanloom.firewall.editor import Editor
from lanloom.integrations.unifi import read
from lanloom.main import create_app
from lanloom.store import Store

from . import fixtures as fx

HDR = {"X-Lanloom": "1"}
USER = {"id": 1, "username": "admin"}


def make_event(raw=None):
    """The event a real read half publishes (built by the reader itself, from fixtures)."""
    r = read.UnifiReader(url="https://192.0.2.1", api_key="k")
    return r.config_event(fx.both() if raw is None else raw)


def add_integration(store, integ_id=1, name="Hall console", kind="unifi"):
    store.execute("INSERT INTO integrations(id, kind, name, config, enabled, created) VALUES(?,?,?,?,1,?)",
                  (integ_id, kind, name, "{}", time.time()))


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_WATCH_S", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_SETTLE_S", "0")
    return tmp_path


@pytest.fixture()
def booked(env):
    store = Store(":memory:")
    c = core.Core(config_mod.load(), store, None)
    add_integration(store)
    return c


# ---------------------------------------------------------------------------- core
def test_config_event_is_booked_and_listed(booked):
    ev = make_event()
    booked.on_event("int:1", 1, ev)
    live = booked.live_configs()
    assert [e["key"] for e in live] == ["int:1"] and live[0]["name"] == "Hall console" and live[0]["family"] == "unifi"
    assert live[0]["state"] == ev["state"] and live[0]["caps"]["api"]["kind"] == "official-local" and live[0]["age_s"] <= 1
    assert booked.live_configs(max_age_s=-1) == []                 # stale entries are not offered
    booked.live_cfg[1]["t"] -= 5000
    assert booked.live_configs() == []


def test_write_capabilities_are_always_stripped(booked):
    ev = make_event()
    assert ev["capabilities"]["write"] == {}
    ev["capabilities"]["write"] = {"move": "B", "port.set.poe": "B*"}              # a (buggy) read half claims writes
    booked.on_event("int:1", 1, ev)
    assert booked.live_configs()[0]["caps"]["write"] == {}
    assert ev["capabilities"]["write"] == {"move": "B", "port.set.poe": "B*"}


def test_invalid_state_or_capabilities_are_refused(booked):
    booked.on_event("int:1", 1, make_event())
    before = booked.live_configs()[0]["state"]
    bad_state = make_event()
    bad_state["state"]["devices"] = "not a list"
    booked.on_event("int:1", 1, bad_state)
    assert booked.live_configs()[0]["state"] == before             # the earlier good state stays
    assert booked.source_status["int:1"]["ok"] is False and booked.source_status["int:1"]["key"] == "source.family.protocol"
    for change in ({"capabilities": {"family": "unifi", "api": {"kind": "cloud"}}}, {"capabilities": None}, {"state": None},
                   {"family": "other"}):
        ev = make_event()
        ev.update(change)
        booked.on_event("int:1", 1, ev)
        assert booked.live_configs()[0]["state"] == before
    booked.on_event("int:7", 7, make_event())                       # no integration row: never listed
    assert [e["key"] for e in booked.live_configs()] == ["int:1"]


def test_topology_event_of_the_family_feeds_the_core(booked):
    from lanloom.integrations.unifi import model
    ev = model.topology_event(fx.both(), time.time())
    booked.on_event("int:1", 1, ev)
    assert booked.topo_obs["int:1"]["ev"]["source"] == "controller" and booked.topo_obs["int:1"]["integ"] == 1
    assert isinstance(booked.topology()["links"], dict)


# ---------------------------------------------------------------------------- editor
def test_editor_shows_the_live_configuration_read_only(env, booked):
    ed = Editor(config_mod.load(), booked.store)
    ed.live = booked.live_configs
    booked.on_event("int:1", 1, make_event())
    v = ed.view(USER, "192.168.50.20", source="int:1")
    assert v["mode"] == "live" and v["source"] == "int:1" and v["draft"] == {"moves": {}, "t": 0, "undo_of": None}
    assert v["capabilities"]["write"] == {} and v["capabilities"]["confirm"] == [] and "ports" in v["capabilities"]["read"]
    assert v["live"]["name"] == "Hall console" and v["live"]["family"] == "unifi" and v["live"]["version"] == "11.0.1"
    assert v["live"]["apis"] == ["classic", "official"] and v["targets"] == {} and v["history"] == [] and v["running"] is None
    assert {"zones", "networks", "policies", "equipment", "ports"} <= set(v["state"])
    assert v["requester"] == next(d["id"] for d in v["state"]["devices"] if d["ip"] == "192.168.50.20")
    assert [s["key"] for s in v["sources"]] == ["editor", "int:1"] and v["sources"][1]["kind"] == "live"


def test_editor_defaults_to_the_live_source_only_in_simulation(env, booked, monkeypatch):
    ed = Editor(config_mod.load(), booked.store)
    ed.live = booked.live_configs
    assert ed.view(USER, "192.168.50.20")["mode"] == ed.view(USER, "192.168.50.20", "editor")["mode"] == "simulation"   # nothing live yet
    booked.on_event("int:1", 1, make_event())
    assert ed.mode == "simulation"
    assert ed.view(USER, "192.168.50.20")["source"] == "int:1"
    assert ed.view(USER, "192.168.50.20", source="int:99")["source"] == "int:1"    # unknown source: the default
    sim = ed.view(USER, "192.168.50.20", source="editor")
    assert sim["mode"] == "simulation" and sim["source"] == "editor" and sim["state"] is not None
    assert sim["capabilities"]["write"] != {}                                       # the simulation keeps its moves
    monkeypatch.setenv("LANLOOM_FW_EXECUTOR", "external")
    monkeypatch.setenv("LANLOOM_EXECUTOR_TOKEN", "t" * 40)
    ext = Editor(config_mod.load(), booked.store)
    ext.live = booked.live_configs
    assert ext.mode == "external" and ext.view(USER, "192.168.50.20")["source"] == "editor"
    assert ext.view(USER, "192.168.50.20", source="int:1")["mode"] == "live"


def test_editor_survives_a_failing_live_source(env, booked):
    ed = Editor(config_mod.load(), booked.store)

    def boom():
        raise RuntimeError("live source failed")

    ed.live = boom
    assert ed.view(USER, "192.168.50.20")["source"] == "editor"


# ---------------------------------------------------------------------------- API
def sign_in(c, data_dir):
    code = (Path(data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200


def test_api_serves_the_live_view_per_source(env):
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        add_integration(ll.store)
        ll.core.on_event("int:1", 1, make_event())
        live = c.get("/api/firewall", params={"source": "int:1"})
        assert live.status_code == 200
        body = live.json()
        assert body["mode"] == "live" and body["source"] == "int:1" and body["capabilities"]["write"] == {}
        assert {z["name"] for z in body["state"]["zones"]} == {"Internal", "External", "Guest"}
        assert fx.PASS not in live.text and fx.PSK not in live.text
        assert c.get("/api/firewall").json()["source"] == "int:1"                  # default while the editor simulates
        sim = c.get("/api/firewall", params={"source": "editor"}).json()
        assert sim["mode"] == "simulation" and sim["source"] == "editor"
        assert [s["key"] for s in sim["sources"]] == ["editor", "int:1"]
        assert c.get("/api/firewall", params={"source": "int:99"}).json()["source"] == "int:1"
        assert c.get("/api/firewall", params={"source": "x" * 500}).status_code == 200
        assert body["draft"]["moves"] == {} and body["targets"] == {}
