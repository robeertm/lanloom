"""The home router family inside the app: registry, core bookkeeping, the live view, the integration API, the setup
assistant, the sample router of the demo and the night report section (fixed clock)."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import core, integrations, setup_assist
from lanloom.firewall import neutral
from lanloom.integrations.fritzbox import read, report
from lanloom.main import create_app
from lanloom.nightly import contrib
from lanloom.store import Store

from . import fixtures as fx
from .fake_box import FakeBox
from .test_model import raw_of

HDR = {"X-Lanloom": "1"}
NOW = 1_800_000_000.0


def make_event():
    r = read.FritzboxReader(url="http://192.0.2.1", username=fx.USER, password=fx.PASSWORD)
    return r.config_event(raw_of())


def add_integration(store, integ_id=1, name="Home router", kind="fritzbox"):
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
    m = integrations.get("fritzbox")
    assert m.read and not m.write and m.capabilities["write"] == {} and m.docs == "docs/integrations/fritzbox.md"
    assert set(m.secret_fields) == {"password"} and "password" not in m.config_fields
    assert {f["k"] for f in m.form} == set(m.config_fields) | set(m.secret_fields)
    pub = [f for f in integrations.public_list() if f["family"] == "fritzbox"][0]
    assert pub["write_half"] is False and pub["capabilities"]["write"] == {}
    guide = Path(__file__).resolve().parents[3].joinpath(m.docs).read_text(encoding="utf-8")
    assert "## Troubleshooting" in guide and "No affiliation or endorsement" in guide


def test_the_product_name_stays_in_the_adapter_not_in_the_core():
    root = Path(__file__).resolve().parents[3]
    for path in (root / "lanloom").rglob("*"):
        if path.suffix in (".py", ".js", ".html", ".css") and "integrations/fritzbox" not in path.as_posix():
            assert "fritz" not in path.read_text(encoding="utf-8").lower(), path
    readme = (root / "README.md").read_text(encoding="utf-8").lower()
    assert "fritz" not in readme and "avm" not in readme.split()


# ---------------------------------------------------------------------------- setup assistant
def test_the_setup_assistant_recognises_the_family_by_its_gentle_signs():
    m = integrations.get("fritzbox")
    assert {"title": "fritz!box"} in [dict(d) for d in m.detect] and {"ssdp": "fritz!box"} in [dict(d) for d in m.detect]
    assert m.guide == "tr064" and setup_assist.GUIDE_STEPS["tr064"] == 4 and m.prefill_scheme == "http"


def test_the_guide_has_all_its_texts_in_every_language():
    base = Path(__file__).resolve().parents[3] / "lanloom" / "i18n"
    for lang in ("en", "de", "fr", "es", "nl", "it"):
        d = json.loads((base / ("%s.json" % lang)).read_text(encoding="utf-8"))
        for n in range(1, 5):
            assert d["setup.guide.tr064.%d" % n].strip()
        assert d["setup.guide.tr064.title"].strip() and d["integrations.family.fritzbox"].startswith("FRITZ!Box")
        for key in ("night.home.title", "night.home.summary_calm", "night.home.summary_look", "night.home.device_down",
                    "night.home.device_up", "night.home.device_new", "night.home.device_gone", "night.home.firmware",
                    "night.home.hosts_new", "fwr.finding.mesh_device_offline"):
            assert d[key].strip(), (lang, key)


def test_it_is_not_taken_for_a_sample_of_another_kind_in_the_demo():
    from lanloom import demo
    others = {demo._demo_family(), demo._demo_firewall(), demo._demo_family("guests"), demo._demo_family("dns"), demo._demo_apc(),
              demo._demo_router(), demo._demo_gateway()}
    assert demo._demo_home() == "fritzbox" and "fritzbox" not in others


# ---------------------------------------------------------------------------- core and editor
def test_config_event_is_booked_and_write_keys_are_stripped(booked):
    ev = make_event()
    ev["capabilities"]["write"] = {"move": "B"}                    # a (buggy) read half claims writes
    booked.on_event("int:1", 1, ev)
    live = booked.live_configs()
    assert [e["key"] for e in live] == ["int:1"] and live[0]["family"] == "fritzbox" and live[0]["caps"]["write"] == {}
    neutral.validate(live[0]["state"])


def test_the_topology_event_is_taken_by_the_core(booked):
    ev = read.model.topology_event(raw_of(), time.time())
    booked.on_event("int:1", 1, ev)                                # must not raise


# ---------------------------------------------------------------------------- API
def sign_in(c, data_dir):
    code = (Path(data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200


def test_api_serves_the_live_view_and_never_hardware_addresses_or_the_password(env):
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        add_integration(ll.store, integ_id=41)
        ll.core.on_event("int:41", 41, make_event())
        r = c.get("/api/firewall", params={"source": "int:41"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "live" and body["capabilities"]["write"] == {}
        assert {"FRITZ!Box 7590", "Repeater upstairs"} <= {e["name"] for e in body["state"]["equipment"]}
        for needle in (fx.PASSWORD, "02:4c:4c", "02-4c-4c", "serial-do-not-keep", "log line"):
            assert needle not in r.text.lower()
        assert "fritzbox" in json.dumps(c.get("/api/integrations").json())


def test_connection_through_the_app_against_the_fake_box(env):
    srv = FakeBox()
    read._reset_for_tests()
    try:
        with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
            sign_in(c, env)
            body = {"kind": "fritzbox", "name": "Home router", "config": {"url": srv.url, "username": fx.USER},
                    "secrets": {"password": fx.PASSWORD}}
            r = c.post("/api/integrations/test", json=body, headers=HDR)
            assert r.status_code == 200, r.text
            out = r.json()
            assert out.get("ok") is True, out
            assert fx.PASSWORD not in r.text
    finally:
        srv.stop()
        read._reset_for_tests()


# ---------------------------------------------------------------------------- demo
def contrib_state(st):
    return {k: v for k, v in st.items() if k not in contrib.FAMILY_STATE_DROP}


def test_demo_has_a_sample_router_without_product_names():
    from lanloom import demo, demo_live
    fam = demo._demo_home()
    before, after = demo_live.home_sample(fam), demo_live.home_sample(fam, changed=True)
    for st in (before, after):
        neutral.validate(st)
        assert st["controller"]["label"] == "Sample home router" and "fritz" not in json.dumps(st).lower()
    assert len(before["equipment"]) == 2 and len(before["devices"]) == 4 and len(after["devices"]) == 5
    sec = report.night_section(contrib_state(before), after, NOW, "Sample home router")
    ks = [i["key"] for i in sec["items"]]
    assert ks == ["night.home.firmware", "night.home.hosts_new"] and sec["summary"] == "night.home.summary_calm"
    caps = demo_live.home_capabilities(fam)
    assert caps["write"] == {} and caps["family"] == fam


def test_demo_seeds_the_sample_router_into_the_reports(env, monkeypatch):
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        names = {r["name"]: r["kind"] for r in ll.store.query("SELECT name, kind FROM integrations")}
        assert names.get("Sample home router") == "fritzbox"
        lst = c.get("/api/reports").json()
        texts = [json.dumps(c.get("/api/reports/" + r["day"]).json()) for r in lst["reports"]]
        assert texts and any("night.home.title" in t and "night.home.hosts_new" in t for t in texts)
