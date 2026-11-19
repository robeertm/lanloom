"""The MikroTik family inside the app: registry, core bookkeeping, the live view, the integration API, the setup
assistant's suggestion and a connection test against the stand-in router. (Sample network of the demo and the
translations: see the notes at the end.)"""
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import core, integrations, setup_assist
from lanloom.firewall import neutral
from lanloom.firewall.editor import Editor
from lanloom.integrations.mikrotik import read
from lanloom.main import create_app
from lanloom.store import Store

from . import fixtures as fx
from .fake_router import FakeRouter
from .test_model import raw_of

ROOT = Path(__file__).resolve().parents[3]
HDR = {"X-Lanloom": "1"}
USER = {"id": 1, "username": "admin"}


def make_event():
    r = read.MikrotikReader(url="https://192.0.2.1", username=fx.USER, password=fx.PASSWORD)
    return r.config_event(raw_of())


def add_integration(store, integ_id=1, name="Edge router", kind="mikrotik"):
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
def live_env(monkeypatch, env):
    """The app without the demo switch: a connection test really talks to the stand-in router."""
    monkeypatch.setenv("LANLOOM_DEMO", "0")
    return env


@pytest.fixture()
def booked(env):
    store = Store(":memory:")
    c = core.Core(config_mod.load(), store, None)
    add_integration(store)
    return c


# ---------------------------------------------------------------------------- registry
def test_family_is_built_in_selectable_read_only_and_documented():
    m = integrations.get("mikrotik")
    assert m.read and not m.write and m.capabilities["write"] == {} and m.docs == "docs/integrations/mikrotik.md"
    assert set(m.secret_fields) == {"password"} and "password" not in m.config_fields
    assert {f["k"] for f in m.form} == set(m.config_fields) | set(m.secret_fields)
    assert m.report == "lanloom.integrations.mikrotik.report:night_section"
    pub = [f for f in integrations.public_list() if f["family"] == "mikrotik"][0]
    assert pub["write_half"] is False and pub["capabilities"]["write"] == {}
    assert "mikrotik" in pub["label"].lower()


def test_the_family_has_a_read_half_but_no_write_half_on_disk():
    folder = ROOT / "lanloom" / "integrations" / "mikrotik"
    assert (folder / "read.py").is_file() and not (folder / "write.py").exists()


def test_the_product_name_stays_in_the_adapter_and_its_allowed_places():
    for path in (ROOT / "lanloom").rglob("*"):
        if path.suffix in (".py", ".js", ".html", ".css") and "integrations/mikrotik" not in path.as_posix():
            assert "mikrotik" not in path.read_text(encoding="utf-8").lower(), path
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "mikrotik" not in readme and "routeros" not in readme


def test_no_other_product_name_in_the_adapter_code():
    for path in (ROOT / "lanloom" / "integrations" / "mikrotik").glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert not re.search("wi-?" + "fi" + r"\b", text), path.name                  # the brand list's own pattern
        # other product names are put together here so that this file itself carries none of them
        for word in ("win" + "box", "wire" + "guard", "blue" + "tooth", "uni" + "fi", "fri" + "tz", "open" + "wrt",
                     "opn" + "sense", "om" + "ada", "prox" + "mox", "soph" + "os", "home " + "assistant"):
            assert word not in text, (path.name, word)


# ---------------------------------------------------------------------------- setup assistant
def test_the_setup_assistant_knows_the_family_by_its_gentle_signs_and_guide():
    m = integrations.get("mikrotik")
    signs = [dict(d) for d in m.detect]
    assert {"port": 80, "title": "routeros"} in signs and {"port": 443, "title": "routeros"} in signs
    assert {"title": "mikrotik"} in signs and {"ssdp": "mikrotik"} in signs
    assert m.guide == "rest" and setup_assist.GUIDE_STEPS.get("rest") == 4 and m.prefill_scheme == "https"


def test_setup_assistant_suggests_the_family_from_stored_scan_data():
    c = core.Core(SimpleNamespace(data_dir=None, demo=False, probe=False), Store(":memory:"), None)
    c.ingest({"hosts": [
        {"ip": "192.0.2.1", "mac": "02:00:00:00:00:01", "hostnames": [], "sources": ["arp"], "mdns": [], "ssdp": [],
         "is_gateway": True, "ports": [{"port": 443, "proto": "tcp", "service": "https",
                                         "banner": {"title": "RouterOS v7.16.1 > Login", "server": None}}]},
        {"ip": "192.0.2.2", "mac": "02:00:00:00:00:02", "hostnames": [], "sources": ["arp"], "mdns": [], "ssdp": [],
         "is_gateway": False, "ports": [{"port": 80, "proto": "tcp", "service": "http",
                                          "banner": {"title": "Router", "server": None}}]},
    ]}, None)
    sg = {(s["kind"], s["ip"]): s for s in setup_assist.suggestions(c)}
    s = sg[("mikrotik", "192.0.2.1")]
    assert s["guide"] == "rest" and s["prefill"]["url"] == "https://192.0.2.1"
    assert ("mikrotik", "192.0.2.2") not in sg


def test_a_router_with_only_the_plain_web_service_gets_an_http_address_prefilled():
    c = core.Core(SimpleNamespace(data_dir=None, demo=False, probe=False), Store(":memory:"), None)
    c.ingest({"hosts": [
        {"ip": "192.0.2.3", "mac": "02:00:00:00:00:03", "hostnames": [], "sources": ["arp"], "mdns": [], "ssdp": [],
         "is_gateway": True, "ports": [{"port": 80, "proto": "tcp", "service": "http",
                                         "banner": {"title": "RouterOS router configuration page", "server": None}}]},
    ]}, None)
    sg = {(s["kind"], s["ip"]): s for s in setup_assist.suggestions(c)}
    assert sg[("mikrotik", "192.0.2.3")]["prefill"]["url"].startswith("http")


# ---------------------------------------------------------------------------- core and editor
def test_config_event_is_booked_and_write_keys_are_stripped(booked):
    ev = make_event()
    ev["capabilities"]["write"] = {"move": "B"}                    # a (buggy) read half claims writes
    booked.on_event("int:1", 1, ev)
    live = booked.live_configs()
    assert [e["key"] for e in live] == ["int:1"] and live[0]["family"] == "mikrotik" and live[0]["caps"]["write"] == {}
    neutral.validate(live[0]["state"])


def test_the_topology_event_is_taken_by_the_core(booked):
    ev = read.model.topology_event(raw_of(), time.time())
    booked.on_event("int:1", 1, ev)                                # must not raise


def test_the_editor_shows_it_read_only(env, booked):
    ed = Editor(config_mod.load(), booked.store)
    ed.live = booked.live_configs
    booked.on_event("int:1", 1, make_event())
    v = ed.view(USER, "10.77.0.50", source="int:1")
    assert v["mode"] == "live" and v["capabilities"]["write"] == {} and v["live"]["family"] == "mikrotik"
    assert v["live"]["version"] == fx.VERSION
    assert {"equipment", "ports", "networks", "findings", "wans", "port_forwards"} <= set(v["state"])


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
        assert {"edge-router", "core-switch", "hall-ap"} <= {e["name"] for e in body["state"]["equipment"]}
        for needle in (fx.PASSWORD, "02:00:5e", "02-00-5e", "cc:2d:e0", "pppoe-secret-pw", "abc123456"):
            assert needle not in r.text.lower()
        assert "mikrotik" in json.dumps(c.get("/api/integrations").json())


def test_connection_through_the_app_against_the_stand_in_router(live_env):
    srv = FakeRouter()
    read._PAUSED.clear()
    try:
        with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
            sign_in(c, live_env)
            body = {"kind": "mikrotik", "name": "Edge router", "config": {"url": srv.url, "username": fx.USER},
                    "secrets": {"password": fx.PASSWORD}}
            r = c.post("/api/integrations/test", json=body, headers=HDR)
            assert r.status_code == 200, r.text
            out = r.json()
            assert out.get("ok") is True, out
            assert fx.PASSWORD not in r.text
            assert {x["method"] for x in srv.requests} == {"GET"}
    finally:
        srv.stop()
        read._PAUSED.clear()


def test_a_user_whose_group_can_write_is_refused_by_the_app(live_env):
    srv = FakeRouter(group="full")
    read._PAUSED.clear()
    try:
        with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
            sign_in(c, live_env)
            body = {"kind": "mikrotik", "name": "Edge router", "config": {"url": srv.url, "username": fx.USER},
                    "secrets": {"password": fx.PASSWORD}}
            out = c.post("/api/integrations/test", json=body, headers=HDR).json()
            assert out.get("ok") is False and out.get("key") == "integration.err.can_write", out
            assert {x["method"] for x in srv.requests} == {"GET"}
    finally:
        srv.stop()
        read._PAUSED.clear()


def test_a_wrong_password_through_the_app_has_its_own_reason_and_a_corrected_one_works_at_once(live_env):
    srv = FakeRouter()
    read._PAUSED.clear()
    try:
        with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
            sign_in(c, live_env)
            body = {"kind": "mikrotik", "name": "Edge router", "config": {"url": srv.url, "username": fx.USER},
                    "secrets": {"password": "wrong-password"}}
            out = c.post("/api/integrations/test", json=body, headers=HDR).json()
            assert out["ok"] is False and out["key"] == "source.family.auth_failed"
            body["secrets"]["password"] = fx.PASSWORD
            assert c.post("/api/integrations/test", json=body, headers=HDR).json()["ok"] is True
    finally:
        srv.stop()
        read._PAUSED.clear()


# ---------------------------------------------------------------------------- translations
def test_the_guide_and_the_night_report_have_all_their_texts_in_every_language():
    base = ROOT / "lanloom" / "i18n"
    for lang in ("en", "de", "fr", "es", "nl", "it"):
        d = json.loads((base / ("%s.json" % lang)).read_text(encoding="utf-8"))
        for n in range(1, 5):
            assert d["setup.guide.rest.%d" % n].strip(), (lang, n)
        assert d["setup.guide.rest.title"].strip() and d["integrations.family.mikrotik"].startswith("MikroTik")
        for key in ("night.rtr.title", "night.rtr.summary_calm", "night.rtr.summary_look", "night.rtr.router_restarted",
                    "night.rtr.firmware", "night.rtr.neighbour_new", "night.rtr.neighbour_gone", "night.rtr.wan_down",
                    "night.rtr.wan_up", "night.rtr.rule_new", "night.rtr.rule_gone", "night.rtr.rule_changed",
                    "night.rtr.rule_on", "night.rtr.rule_off", "night.rtr.forward_new", "night.rtr.forward_gone",
                    "night.rtr.hosts_new", "fwr.finding.input_open", "fwr.finding.wan_down", "fwr.finding.rules_disabled"):
            assert d[key].strip(), (lang, key)


# ---------------------------------------------------------------------------- demo
def contrib_state(st):
    from lanloom.nightly import contrib
    return {k: v for k, v in st.items() if k not in contrib.FAMILY_STATE_DROP}


def test_it_is_not_taken_for_a_sample_of_another_kind_in_the_demo():
    from lanloom import demo
    others = {demo._demo_family(), demo._demo_firewall(), demo._demo_family("guests"), demo._demo_family("dns"),
              demo._demo_apc(), demo._demo_router(), demo._demo_gateway(), demo._demo_home()}
    assert demo._demo_ros() == "mikrotik" and "mikrotik" not in others


def test_demo_has_a_sample_router_without_product_names():
    from lanloom import demo, demo_live
    from lanloom.integrations.mikrotik import report
    fam = demo._demo_ros()
    before, after = demo_live.ros_sample(fam), demo_live.ros_sample(fam, changed=True)
    for st in (before, after):
        neutral.validate(st)
        assert st["controller"]["label"] == "Sample switching router" and "mikrotik" not in json.dumps(st).lower()
    sec = report.night_section(contrib_state(before), after, 1_800_000_000.0, "Sample switching router")
    assert sec and sec["items"] and all(i["key"].startswith(("night.rtr.", "fwr.finding.")) for i in sec["items"])
    caps = demo_live.ros_capabilities(fam)
    assert caps["write"] == {} and caps["family"] == fam


def test_demo_seeds_the_sample_router_into_the_reports(env, monkeypatch):
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        names = {r["name"]: r["kind"] for r in ll.store.query("SELECT name, kind FROM integrations")}
        assert names.get("Sample switching router") == "mikrotik"
        lst = c.get("/api/reports").json()
        texts = [json.dumps(c.get("/api/reports/" + r["day"]).json()) for r in lst["reports"]]
        assert texts and any("night.rtr.title" in t for t in texts)
