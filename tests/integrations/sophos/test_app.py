"""The zone firewall family inside the app: core bookkeeping, the firewall tab's live view, the integration API, the
demo's sample firewall and the night report section (fixed clock)."""
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
from lanloom.integrations.sophos import model, read, report
from lanloom.main import create_app
from lanloom.nightly import contrib
from lanloom.store import Store

from . import fixtures as fx

HDR = {"X-Lanloom": "1"}
USER = {"id": 1, "username": "admin"}
NOW = 1_800_000_000.0
PASSWORD = "s3cret&<pass>"


def raw_of(reply=fx.REPLY) -> dict:
    meta, ents = model.parse_reply(reply)
    return {"api_version": meta["api_version"], "entities": ents}


def make_event(raw=None):
    r = read.SophosReader(url="https://192.0.2.1:4444", username="lanloom-read", password=PASSWORD)
    return r.config_event(raw_of() if raw is None else raw)


def add_integration(store, integ_id=1, name="Edge firewall", kind="sophos"):
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
def test_family_is_built_in_selectable_and_read_only():
    m = integrations.get("sophos")
    assert m.read and not m.write and m.capabilities["write"] == {} and m.docs == "docs/integrations/sophos.md"
    assert set(m.secret_fields) == {"password"} and "password" not in m.config_fields
    pub = [f for f in integrations.public_list() if f["family"] == "sophos"][0]
    assert pub["write_half"] is False and pub["capabilities"]["write"] == {}
    assert [s for s in integrations.night_sections() if s[0] == "sophos"][0][1] is report.night_section


def test_read_allow_list_holds_only_the_checked_query():
    rules = read.ALLOW.rules
    assert len(rules) == 1 and rules[0].method == "POST" and rules[0].query and rules[0].form
    assert read.ALLOW.write is False


# ---------------------------------------------------------------------------- core and editor
def test_config_event_is_booked_and_write_keys_are_stripped(booked):
    ev = make_event()
    ev["capabilities"]["write"] = {"move": "B"}                    # a (buggy) read half claims writes
    booked.on_event("int:1", 1, ev)
    live = booked.live_configs()
    assert [e["key"] for e in live] == ["int:1"] and live[0]["family"] == "sophos" and live[0]["caps"]["write"] == {}
    neutral.validate(live[0]["state"])


def test_editor_shows_the_firewall_read_only(env, booked):
    ed = Editor(config_mod.load(), booked.store)
    ed.live = booked.live_configs
    booked.on_event("int:1", 1, make_event())
    v = ed.view(USER, "192.168.10.50", source="int:1")
    assert v["mode"] == "live" and v["capabilities"]["write"] == {} and v["live"]["family"] == "sophos"
    assert v["live"]["version"] == fx.API_VERSION
    assert {"zones", "networks", "policies", "port_forwards", "findings", "wans", "vpn"} <= set(v["state"])


# ---------------------------------------------------------------------------- API
def sign_in(c, data_dir):
    code = (Path(data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200


def test_api_serves_the_live_view_and_never_the_password(env):
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        add_integration(ll.store, integ_id=41)
        ll.core.on_event("int:41", 41, make_event())
        r = c.get("/api/firewall", params={"source": "int:41"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "live" and body["capabilities"]["write"] == {}
        assert {"LAN", "WAN", "DMZ", "VPN", "IoT"} <= {z["name"] for z in body["state"]["zones"]}
        assert PASSWORD not in r.text and "02:00:5e:10:00:01" not in r.text.lower()
        fams = c.get("/api/integrations").json()
        listed = fams.get("families", fams) if isinstance(fams, dict) else fams
        assert "sophos" in json.dumps(listed)


# ---------------------------------------------------------------------------- demo
def test_demo_has_a_sample_firewall_without_product_names():
    from lanloom import demo, demo_live
    fam = demo._demo_firewall()
    assert fam == "sophos"
    assert demo._demo_family() != fam and demo._demo_family("guests") != fam      # controller and hypervisor unchanged
    before, after = demo_live.firewall_sample(fam), demo_live.firewall_sample(fam, extra_rule=True)
    for st in (before, after):
        neutral.validate(st)
        assert st["controller"]["label"] == "Sample firewall" and "sophos" not in json.dumps(st).lower()
    assert len(after["port_forwards"]) == len(before["port_forwards"]) + 1
    assert {f["kind"] for f in after["findings"]} >= {"admin_on_wan", "wan_inbound_open", "rules_disabled"}
    sec = report.night_section({k: v for k, v in before.items() if k not in contrib.FAMILY_STATE_DROP}, after, NOW, "Sample firewall")
    keys = [i["key"] for i in sec["items"]]
    assert "night.fwr.rule_new" in keys and "night.fwr.forward_new" in keys and sec["summary"] == "night.fwr.summary_look"


def test_demo_seeds_the_sample_firewall_into_the_reports(env, monkeypatch):
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        ll = c.app.state.ll
        names = {r["name"]: r["kind"] for r in ll.store.query("SELECT name, kind FROM integrations")}
        assert names.get("Sample firewall") == "sophos"
        lst = c.get("/api/reports").json()
        texts = [json.dumps(c.get("/api/reports/" + r["day"]).json()) for r in lst["reports"]]
        assert texts and any("night.fwr.title" in t and "night.fwr.rule_new" in t for t in texts)
