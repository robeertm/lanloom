"""The access-point controller family inside the app: registry, core bookkeeping, the firewall tab's live view, the
integration API, the sample network of the demo and the night report section (fixed clock)."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import core
from lanloom import integrations
from lanloom.firewall import neutral
from lanloom.firewall.editor import Editor
from lanloom.integrations.omada import read, report
from lanloom.main import create_app
from lanloom.nightly import contrib
from lanloom.store import Store

from . import fixtures as fx
from .fake_controller import FakeController
from .test_model import raw_of

HDR = {"X-Lanloom": "1"}
USER = {"id": 1, "username": "admin"}
NOW = 1_800_000_000.0


def make_event():
    r = read.OmadaReader(url="https://192.0.2.1:8043", client_id=fx.CLIENT_ID, client_secret=fx.CLIENT_SECRET)
    return r.config_event(raw_of())


def add_integration(store, integ_id=1, name="Site controller", kind="omada"):
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


# ---------------------------------------------------------------------------- registry
def test_family_is_built_in_selectable_read_only_and_documented():
    m = integrations.get("omada")
    assert m.read and not m.write and m.capabilities["write"] == {} and m.docs == "docs/integrations/omada.md"
    assert set(m.secret_fields) == {"client_secret"} and "client_secret" not in m.config_fields
    assert {f["k"] for f in m.form} == set(m.config_fields) | set(m.secret_fields)
    pub = [f for f in integrations.public_list() if f["family"] == "omada"][0]
    assert pub["write_half"] is False and pub["capabilities"]["write"] == {}
    assert Path(__file__).resolve().parents[3].joinpath(m.docs).is_file()


def test_the_setup_assistant_recognises_the_family_by_its_gentle_signs():
    m = integrations.get("omada")
    assert {"port": 8043, "title": "omada"} in [dict(d) for d in m.detect]


def test_it_is_not_taken_for_the_sample_controller_or_firewall_of_the_demo():
    from lanloom import demo
    families = {demo._demo_family(), demo._demo_firewall(), demo._demo_family("guests"), demo._demo_family("dns")}
    assert demo._demo_apc() == "omada" and "omada" not in families and None not in families


# ---------------------------------------------------------------------------- core and editor
def test_config_event_is_booked_and_write_keys_are_stripped(booked):
    ev = make_event()
    ev["capabilities"]["write"] = {"move": "B"}                    # a (buggy) read half claims writes
    booked.on_event("int:1", 1, ev)
    live = booked.live_configs()
    assert [e["key"] for e in live] == ["int:1"] and live[0]["family"] == "omada" and live[0]["caps"]["write"] == {}
    neutral.validate(live[0]["state"])


def test_the_editor_shows_it_read_only(env, booked):
    ed = Editor(config_mod.load(), booked.store)
    ed.live = booked.live_configs
    booked.on_event("int:1", 1, make_event())
    v = ed.view(USER, "192.168.10.50", source="int:1")
    assert v["mode"] == "live" and v["capabilities"]["write"] == {} and v["live"]["family"] == "omada"
    assert v["live"]["version"] == fx.VERSION
    assert {"equipment", "ports", "ssids", "networks", "findings", "wans"} <= set(v["state"])


# ---------------------------------------------------------------------------- API
def sign_in(c, data_dir):
    code = (Path(data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200


def test_api_serves_the_live_view_and_never_the_secret(env):
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        add_integration(ll.store, integ_id=41)
        ll.core.on_event("int:41", 41, make_event())
        r = c.get("/api/firewall", params={"source": "int:41"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "live" and body["capabilities"]["write"] == {}
        assert {"Edge gateway", "Core switch", "Hall AP"} <= {e["name"] for e in body["state"]["equipment"]}
        for needle in (fx.CLIENT_SECRET, "02-00-5e", "02:00:5e", "3c:00:5e"):
            assert needle not in r.text.lower()
        fams = c.get("/api/integrations").json()
        assert "omada" in json.dumps(fams.get("families", fams) if isinstance(fams, dict) else fams)


def test_connection_through_the_app_against_the_fake_controller(env):
    srv = FakeController()
    read._reset_for_tests()
    try:
        with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
            sign_in(c, env)
            body = {"kind": "omada", "name": "Site controller",
                    "config": {"url": srv.url, "client_id": fx.CLIENT_ID, "site": "Default"},
                    "secrets": {"client_secret": fx.CLIENT_SECRET}}
            r = c.post("/api/integrations/test", json=body, headers=HDR)
            assert r.status_code == 200, r.text
            out = r.json()
            assert out.get("ok") is True, out
            assert fx.CLIENT_SECRET not in r.text
    finally:
        srv.stop()
        read._reset_for_tests()


# ---------------------------------------------------------------------------- demo
def test_demo_has_a_sample_controller_without_product_names():
    from lanloom import demo, demo_live
    fam = demo._demo_apc()
    before, after = demo_live.controller_sample(fam), demo_live.controller_sample(fam, changed=True)
    for st in (before, after):
        neutral.validate(st)
        assert st["controller"]["label"] == "Sample wireless controller" and "omada" not in json.dumps(st).lower()
    assert len(before["equipment"]) == 4 and len(before["ssids"]) == 3 and before["ports"] and before["acl_digest"]
    assert {f["kind"] for f in after["findings"]} >= {"device_offline", "ssid_open", "acls_off"}
    sec = report.night_section(contrib_state(before), after, NOW, "Sample wireless controller")
    ks = [i["key"] for i in sec["items"]]
    assert {"night.ctl.device_down", "night.ctl.acl_new", "night.ctl.firmware"} <= set(ks) and sec["summary"] == "night.ctl.summary_look"
    caps = demo_live.controller_capabilities(fam)
    assert caps["write"] == {} and caps["family"] == fam


def contrib_state(st):
    return {k: v for k, v in st.items() if k not in contrib.FAMILY_STATE_DROP}


def test_demo_seeds_the_sample_controller_into_the_reports(env, monkeypatch):
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        names = {r["name"]: r["kind"] for r in ll.store.query("SELECT name, kind FROM integrations")}
        assert names.get("Sample wireless controller") == "omada"
        lst = c.get("/api/reports").json()
        texts = [json.dumps(c.get("/api/reports/" + r["day"]).json()) for r in lst["reports"]]
        assert texts and any("night.ctl.title" in t and "night.ctl.device_down" in t for t in texts)
