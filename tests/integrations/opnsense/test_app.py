"""The OPNsense family inside the app: registry, core bookkeeping, the firewall tab's live view, the integration API,
the setup assistant's suggestion and guide, the demo's sample gateway firewall and the night report section."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import core, integrations, setup_assist
from lanloom.firewall import neutral
from lanloom.firewall.editor import Editor
from lanloom.integrations.opnsense import model, read, report
from lanloom.main import create_app
from lanloom.nightly import contrib
from lanloom.scan import ports
from lanloom.store import Store

from . import fixtures as fx
from .test_model import raw_of

HDR = {"X-Lanloom": "1"}
USER = {"id": 1, "username": "admin"}
NOW = 1_800_000_000.0
ROOT = Path(__file__).resolve().parents[3]


def make_event(version="24.7"):
    r = read.OpnsenseReader(url="https://192.0.2.1", api_key=fx.API_KEY, api_secret=fx.API_SECRET)
    return r.config_event(raw_of(version))


def add_integration(store, integ_id=1, name="Edge gateway", kind="opnsense"):
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
def test_family_is_built_in_with_both_halves():
    m = integrations.get("opnsense")
    assert m.read and m.write and m.docs == "docs/integrations/opnsense.md" and (ROOT / m.docs).is_file()
    assert set(m.secret_fields) == {"api_secret"} and set(m.write_secret_fields) == {"write_api_secret"}
    assert set(m.capabilities["write"]) == {"policy.create", "policy.update", "policy.delete"}
    assert m.guide == "apikey" and m.guide in setup_assist.GUIDE_STEPS
    assert [s for s in integrations.night_sections() if s[0] == "opnsense"][0][1] is report.night_section
    pub = [f for f in integrations.public_list() if f["family"] == "opnsense"][0]
    assert pub["write_half"] is True and pub["form"][0]["k"] == "url"


def test_the_app_never_loads_the_write_half():
    with pytest.raises(PermissionError):
        integrations.load_write("opnsense", url="https://192.0.2.1", write_api_key=fx.WRITE_KEY, write_api_secret=fx.WRITE_SECRET)


def test_read_half_publishes_no_write_keys():
    ev = make_event()
    assert ev["capabilities"]["write"] == {} and ev["capabilities"]["rollback"]["device_native"] is False


# ---------------------------------------------------------------------------- core and editor
def test_config_event_is_booked_and_write_keys_are_stripped(booked):
    ev = make_event()
    ev["capabilities"]["write"] = {"policy.update": "B"}
    booked.on_event("int:1", 1, ev)
    live = booked.live_configs()
    assert [e["key"] for e in live] == ["int:1"] and live[0]["family"] == "opnsense" and live[0]["caps"]["write"] == {}
    neutral.validate(live[0]["state"])


@pytest.mark.parametrize("version", ["24.7", "26.7"])
def test_editor_shows_the_firewall_read_only(env, booked, version):
    ed = Editor(config_mod.load(), booked.store)
    ed.live = booked.live_configs
    booked.on_event("int:1", 1, make_event(version))
    v = ed.view(USER, "192.168.50.50", source="int:1")
    assert v["mode"] == "live" and v["capabilities"]["write"] == {} and v["live"]["family"] == "opnsense"
    assert {"zones", "networks", "policies", "findings", "wans", "vpn"} <= set(v["state"])


# ---------------------------------------------------------------------------- API
def sign_in(c, data_dir):
    code = (Path(data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200


def test_api_serves_the_live_view_and_never_a_secret(env):
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        add_integration(ll.store, integ_id=41)
        ll.core.on_event("int:41", 41, make_event("26.7"))
        r = c.get("/api/firewall", params={"source": "int:41"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "live" and body["capabilities"]["write"] == {}
        assert {"LAN", "WAN", "IoT", "Guests", "DMZ", "This firewall"} <= {z["name"] for z in body["state"]["zones"]}
        for bad in (fx.API_SECRET, fx.WG_PRIVATE, fx.PPPOE_SECRET, "02:4c:4c"):
            assert bad not in r.text.lower() and bad not in r.text
        fams = c.get("/api/integrations").json()
        assert "opnsense" in json.dumps(fams)


# ---------------------------------------------------------------------------- setup assistant
def test_setup_assistant_suggests_the_family_with_its_own_guide():
    assert 443 in ports.GENTLE_PORTS
    c = core.Core(SimpleNamespace(data_dir=None, demo=False, probe=False), Store(":memory:"), None)
    c.ingest({"hosts": [
        {"ip": "192.168.9.1", "mac": "02:00:00:00:00:01", "hostnames": [], "sources": ["arp"], "mdns": [], "ssdp": [],
         "is_gateway": True, "ports": [{"port": 443, "proto": "tcp", "service": "https",
                                         "banner": {"title": "Login | OPNsense", "server": None}}]},
        {"ip": "192.168.9.2", "mac": "02:00:00:00:00:02", "hostnames": [], "sources": ["arp"], "mdns": [], "ssdp": [],
         "is_gateway": False, "ports": [{"port": 443, "proto": "tcp", "service": "https", "banner": {"title": "Router", "server": None}}]},
    ]}, None)
    sg = {(s["kind"], s["ip"]): s for s in setup_assist.suggestions(c)}
    s = sg[("opnsense", "192.168.9.1")]
    assert s["certainty"] == "sure" and s["guide"] == "apikey" and s["prefill"] == {"url": "https://192.168.9.1", "allow_self_signed": True}
    assert ("opnsense", "192.168.9.2") not in sg
    texts = json.loads((ROOT / "lanloom" / "i18n" / "de.json").read_text())
    assert all("setup.guide.apikey.%s" % k in texts for k in ("1", "2", "3", "4", "title"))


# ---------------------------------------------------------------------------- demo
def test_demo_has_a_sample_gateway_firewall_without_product_names():
    from lanloom import demo, demo_live
    fam = demo._demo_gateway()
    assert fam == "opnsense"
    assert demo._demo_firewall() != fam and demo._demo_family() != fam and demo._demo_apc() != fam
    before, after = demo_live.gateway_sample(fam), demo_live.gateway_sample(fam, changed=True)
    for st in (before, after):
        neutral.validate(st)
        assert st["controller"]["label"] == "Sample gateway firewall" and "opnsense" not in json.dumps(st).lower()
    assert {f["kind"] for f in after["findings"]} >= {"admin_on_wan", "wan_inbound_open", "rules_disabled"}
    sec = report.night_section({k: v for k, v in before.items() if k not in contrib.FAMILY_STATE_DROP}, after, NOW,
                               "Sample gateway firewall")
    k = [i["key"] for i in sec["items"]]
    assert "night.fwr.rule_new" in k and "night.fwr.rule_off" in k and sec["summary"] == "night.fwr.summary_look"
    assert "opnsense" not in json.dumps(demo_live.gateway_capabilities("x")).lower()


def test_demo_seeds_the_sample_gateway_firewall_into_the_reports(env, monkeypatch):
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        names = {r["name"]: r["kind"] for r in ll.store.query("SELECT name, kind FROM integrations")}
        assert names.get("Sample gateway firewall") == "opnsense" and names.get("Sample firewall") != "opnsense"
        lst = c.get("/api/reports").json()
        texts = [json.dumps(c.get("/api/reports/" + r["day"]).json()) for r in lst["reports"]]
        assert any("Sample gateway firewall" in t and "night.fwr.rule_off" in t for t in texts)
