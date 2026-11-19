"""Switch ports in the firewall editor (stage B, U5): name, port profile, untagged and tagged networks, power over Ethernet
and power cycling - the pure rules of ``firewall.portconf`` on the sample switch, the gate, the simulation through the API
(draft, preview, confirmation, plan, approval, undo), roles and the nightly section "Port settings" with a fixed clock
in every language of the app. Nothing here touches a network."""
import asyncio
import copy
import re
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod, core as core_mod
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.firewall import adapter, canon, model, netconf, portconf, steps
from lanloom.firewall import report as _fw_report  # noqa: F401 - registers the nightly sections
from lanloom.integrations import capabilities as capmod
from lanloom.integrations.simulation import CAPABILITIES as SIM_CAPS
from lanloom.main import create_app
from lanloom.nightly import Reports, render
from lanloom.store import Store
from lanloom.textfmt import languages

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
ORIGIN = "http://lanloom.test"
NOW = 1_800_000_000


def sample():
    return adapter.with_sample_gateway(adapter.with_sample_hypervisor(adapter.sample_state(), now=NOW))


def port(s, idx):
    return next(p for p in s["ports"] if p["idx"] == idx)


def step(s, idx, **after):
    p = port(s, idx)
    return {"kind": "port.set", "object": p["id"], "before": {f: portconf._cur(p, f) for f in after}, "after": after}


def pv_of(s, ops, **kw):
    return portconf.preview(s, portconf.clean_ops(s, ops), **kw)


# ---------------------------------------------------------------------------- the sample switch
def test_sample_switch_ports_profiles_and_devices():
    s = sample()
    assert len(s["ports"]) == 17 and {p["id"] for p in s["port_profiles"]} == {"pp_trunk", "pp_cams", "pp_smart", "pp_work"}
    assert port(s, 13)["profile"] == "pp_cams" and port(s, 13)["profile_name"] == "Cameras"
    assert [d["name"] for d in portconf.devices_on(s, port(s, 7))] == ["Office laptop"]
    assert [e["name"] for e in portconf.equipment_on(s, port(s, 2))] == ["Access point upstairs"]
    info = portconf.info(s, {"addresses": sorted(adapter.SAMPLE_CRITICAL)})
    assert info["locked"] == {"p_sw_1": "fw.block.uplink", "p_sw_2": "pc.block.infra", "p_sw_3": "pc.block.infra",
                              "p_sw_4": "pc.block.protected", "p_sw_5": "pc.block.protected", "p_sw_10": "pc.block.protected",
                              "p_sw_11": "pc.block.protected"}
    assert info["devices"]["p_sw_13"] == [{"id": "d25", "name": "Front door camera", "kind": "camera"}]
    assert [p["name"] for p in info["profiles"]] == ["Access point trunk", "Cameras", "Smart home", "Work desk"]


def test_effective_settings_follow_the_profile():
    s = sample()
    eff = portconf.effective(s, port(s, 13))
    assert eff["native_network"] == "n_cams" and eff["native_vlan"] == 70 and eff["tagged_mode"] == "none"
    eff = portconf.effective(s, port(s, 7), {"profile": "pp_work"})
    assert eff["native_network"] == "n_work" and eff["profile"] == "pp_work"
    assert portconf.effective(s, port(s, 14), {"tagged_mode": "custom", "tagged": ["n_media"]})["tagged"] == ["n_media"]
    assert portconf.effective(s, port(s, 14), {"tagged_mode": "all"})["tagged"] == []


# ---------------------------------------------------------------------------- drafts
def test_clean_ops_keeps_only_real_valid_changes():
    s = sample()
    ops = [{"kind": "port.set", "object": "p_sw_7", "after": {"name": " Desk ", "native_network": "n_main"}},       # name only
           {"kind": "port.set", "object": "p_sw_8", "after": {"profile": "pp_work", "native_network": "n_media"}},   # profile wins
           {"kind": "port.set", "object": "p_sw_14", "after": {"tagged": ["n_media", "n_main", "n_media"]}},       # native dropped
           {"kind": "port.set", "object": "p_sw_12", "after": {"speed": 10}},                                       # not writable
           {"kind": "port.set", "object": "p_nope", "after": {"name": "x"}},
           {"kind": "port.cycle", "object": "p_sw_13", "after": {}}]
    out = portconf.clean_ops(s, ops)
    assert out["port.set|p_sw_7"]["after"] == {"name": "Desk"} and out["port.set|p_sw_7"]["before"] == {"name": "Port 7"}
    assert out["port.set|p_sw_8"]["after"] == {"profile": "pp_work"}
    assert out["port.set|p_sw_14"]["after"] == {"tagged": ["n_media"], "tagged_mode": "custom"}
    assert out["port.cycle|p_sw_13"] == {"kind": "port.cycle", "object": "p_sw_13", "before": {}, "after": {"cycle": True}}
    assert set(out) == {"port.set|p_sw_7", "port.set|p_sw_8", "port.set|p_sw_14", "port.cycle|p_sw_13"}


@pytest.mark.parametrize("idx,after,why", [
    (1, {"native_network": "n_media"}, "fw.block.uplink"),
    (1, {"name": "To the gateway"}, None),                                  # a name always
    (2, {"poe": False}, "pc.block.infra"),                                  # the access point hangs here
    (4, {"native_network": "n_media"}, "pc.block.protected"),                # the storage
    (10, {"poe": True}, "pc.block.no_poe"),
    (15, {"poe": True}, "pc.block.no_poe"),
    (7, {"native_network": "n_gone"}, "fw.block.unknown"),
    (7, {"profile": "pp_gone"}, "fw.block.unknown"),
    (7, {"native_network": "n_work"}, None),
    (7, {"tagged_mode": "custom", "tagged": ["n_main"]}, "pc.block.tagged_native"),
    (13, {"poe": False}, "pc.block.profile_poe"),                            # the cameras profile switches power on
    (14, {"poe": False}, None),
])
def test_lockout_rules(idx, after, why):
    s = sample()
    assert (model.lockout_reason(s, step(s, idx, **after)) or portconf.step_lockout(s, step(s, idx, **after))) == why


def test_profile_and_networks_in_one_step_and_odd_power_modes():
    s = sample()
    st = {"kind": "port.set", "object": "p_sw_7", "before": {"profile": "", "native_network": "n_main"},
          "after": {"profile": "pp_work", "native_network": "n_media"}}
    assert portconf.step_lockout(s, st) == "pc.block.profile_and_networks"
    port(s, 14)["poe"]["mode"] = "pasv24"
    assert portconf.step_lockout(s, step(s, 14, poe=False)) == "pc.block.poe_mode"
    port(s, 6)["mode"] = "mirror"
    assert portconf.step_lockout(s, step(s, 6, native_network="n_work")) == "pc.block.port_mode"
    port(s, 6)["mode"], port(s, 6)["lldp"] = "switch", True
    assert portconf.step_lockout(s, step(s, 6, native_network="n_work")) == "pc.block.lldp"


def test_the_own_path_keeps_its_port():
    s = sample()
    laptop = next(d for d in s["devices"] if d["id"] == "d08")
    st = step(s, 7, poe=False)
    assert portconf.step_lockout(s, st, {"addresses": [laptop["ip"]]}) == "pc.block.self_path"
    assert portconf.step_lockout(s, st, {"devices": ["d08"]}) == "pc.block.self_path"
    assert portconf.step_lockout(s, step(s, 7, name="Mine"), {"devices": ["d08"]}) is None
    assert portconf.block_vars(s, st) == {"name": "Core switch · Port 7", "device": "Office laptop", "reason": ""}


def test_preview_warnings_and_confirmations():
    s = sample()
    pv = pv_of(s, [{"kind": "port.set", "object": "p_sw_7", "after": {"native_network": "n_work"}},
                   {"kind": "port.set", "object": "p_sw_14", "after": {"poe": False}},
                   {"kind": "port.cycle", "object": "p_sw_13", "after": {}},
                   {"kind": "port.set", "object": "p_sw_16", "after": {"native_network": "n_mgmt"}}])
    assert not pv["blocks"], pv["blocks"]
    keys = [w[0] for w in pv["warnings"]]
    assert ["pc.warn.native", {"name": "Core switch · Port 7", "net": "Work", "devices": "Office laptop", "n": 1}] in pv["warnings"]
    assert "pc.warn.fixed_ip" in keys                                       # the laptop keeps its old fixed address
    assert "pc.warn.poe_off_empty" in keys and "pc.warn.cycle" in keys and "pc.warn.native_empty" in keys
    assert "pc.warn.to_management" in keys
    assert sorted(pv["confirm"]) == ["confirm:port.cycle:p_sw_13", "confirm:port.set:p_sw_14", "confirm:port.set:p_sw_16"]
    assert {c["what"] for c in pv["changes"]} == {"pc.change.port", "pc.change.cycle"}
    blocked = pv_of(s, [{"kind": "port.set", "object": "p_sw_4", "after": {"native_network": "n_media"}}])
    assert blocked["blocks"] == [["pc.block.protected", {"name": "Core switch · Port 4", "device": "Storage", "reason": ""}]]


def test_what_uses_a_device_by_address_warns_when_its_port_changes_network():
    s = sample()
    pv = pv_of(s, [{"kind": "port.set", "object": "p_sw_12", "after": {"native_network": "n_main"}}],
               deps={"d24": [{"via": "integration", "by": "Solar inverter (inverter)"}]})
    assert ["fw.warn.addr_integration", {"name": "Solar inverter", "by": "Solar inverter (inverter)"}] in pv["warnings"]


# ---------------------------------------------------------------------------- steps, fingerprint, capabilities
def test_power_cycle_step_shape_and_no_way_back():
    good = {"kind": "port.cycle", "object": "p_sw_13", "before": {}, "after": {"cycle": True}}
    steps.check_step(good)
    for bad in (dict(good, before={"cycle": True}), dict(good, after={"cycle": False}), dict(good, after={})):
        with pytest.raises(steps.FieldError):
            steps.check_step(bad)
    assert steps.inverse_step(good) is None and steps.needs_confirmation(good)
    assert model.inverse([good, {"kind": "port.set", "object": "p_sw_7", "before": {"name": "Port 7"}, "after": {"name": "D"}}]) == \
        [{"kind": "port.set", "object": "p_sw_7", "before": {"name": "D"}, "after": {"name": "Port 7"}}]
    caps = capmod.validate(SIM_CAPS)
    assert capmod.stage_for(caps, good) == "B*"
    assert capmod.stage_for(caps, {"kind": "port.set", "object": "p", "before": {"poe": True}, "after": {"poe": False}}) == "B*"
    assert capmod.stage_for(caps, {"kind": "port.set", "object": "p", "before": {"name": "1"}, "after": {"name": "x"}}) == "B"


def test_fingerprint_sees_power_on_off_but_not_the_draw():
    s = sample()
    fp = canon.fingerprint(s)
    port(s, 13)["poe"]["watts"] = 5.9
    assert canon.fingerprint(s) == fp
    port(s, 13)["poe"]["on"] = False
    assert canon.fingerprint(s) != fp


def test_networks_count_the_ports_that_carry_them():
    s = sample()
    assert netconf.references(s, "n_media")["ports"] == 1                  # port 16 carries it untagged
    port(s, 14).update(tagged_mode="custom", tagged=["n_media"])
    assert netconf.references(s, "n_media")["ports"] == 2


def test_gate_refuses_a_port_change_without_its_confirmation_and_two_changes_of_one_port():
    s = sample()
    st = step(s, 14, poe=False)
    pv = {"base": canon.fingerprint(s), "steps": [st], "warnings": [], "costs": {"objects": 1}}
    p = model.build_plan(s, pv, target="simulation", user="admin", confirmed=[], now=NOW)
    with pytest.raises(model.Refused) as e:
        model.gate(s, p, target="simulation", now=NOW, quiet=False, busy=False, healthy=(True, ""), caps=SIM_CAPS)
    assert e.value.key == "fw.block.confirm"
    assert portconf.plan_lockout(s, [st, {"kind": "port.cycle", "object": st["object"], "before": {}, "after": {"cycle": True}}]) == \
        "pc.block.one_per_port"


def test_simulation_applies_and_restores_a_port(tmp_path):
    from lanloom.firewall.runner import Runner
    sim = adapter.SimulatedController(state=sample(), settle_s=0)
    s = sim.read()
    st = step(s, 7, native_network="n_work", name="Desk")
    pv = {"base": canon.fingerprint(s), "steps": [st], "warnings": [], "costs": {"objects": 1}}
    r = Runner(sim, tmp_path / "x", target="simulation", require_signature=False, watch_s=0, check_s=0, sleep=lambda x: None)
    res = r.run(model.build_plan(s, pv, target="simulation", user="admin", confirmed=[]))
    assert res["status"] == "done", res
    p = port(sim.read(), 7)
    assert p["native_network"] == "n_work" and p["native_vlan"] == 40 and p["name"] == "Desk"
    sim.restore({"ports": [port(s, 7)]})
    assert port(sim.read(), 7)["native_network"] == "n_main"


# ---------------------------------------------------------------------------- through the API
@pytest.fixture()
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_WATCH_S", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_SETTLE_S", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url=ORIGIN) as client:
        code = (tmp_path / "setup-code.txt").read_text().strip()
        assert client.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        client.ll = app.state.ll
        yield client


def wait_done(c):
    for _ in range(600):
        v = c.get("/api/firewall?source=editor").json()
        if not v["running"]:
            return v
        time.sleep(0.05)
    raise AssertionError("simulation did not finish")


def test_editor_offers_ports_with_locks_and_profiles(c):
    v = c.get("/api/firewall?source=editor").json()
    assert {"port.set.native_network", "port.set.poe", "port.cycle"} <= set(v["capabilities"]["write"])
    pc = v["portconf"]
    assert pc["locked"]["p_sw_1"] == "fw.block.uplink" and "p_sw_7" not in pc["locked"]
    assert pc["devices"]["p_sw_7"][0]["name"] == "Office laptop" and len(pc["profiles"]) == 4
    assert len(v["state"]["ports"]) == 17 and v["state"]["port_profiles"]


def test_change_a_port_through_the_api_then_undo(c):
    op = {"kind": "port.set", "object": "p_sw_7", "after": {"native_network": "n_work", "name": "Desk"}}
    d = c.put("/api/firewall/draft", json={"ops": [op]}, headers=HDR).json()
    assert list(d["ops"]) == ["port.set|p_sw_7"]
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert not pv["blocks"] and pv["changes"][0]["what"] == "pc.change.port" and not pv["confirm"]
    p = c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).json()
    assert p["plan"]["v"] == 2
    assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(c)
    job = v["history"][0]
    assert job["status"] == "done", job
    assert job["summary"] == [{"name": "Core switch · Port 7", "kind": "port.set", "fields": ["name", "native_network"]}]
    p7 = next(x for x in v["state"]["ports"] if x["id"] == "p_sw_7")
    assert p7["native_network"] == "n_work" and p7["name"] == "Desk"
    d = c.post("/api/firewall/undo", json={"plan": job["plan"]}, headers=HDR).json()
    assert d["ops"]["port.set|p_sw_7"]["after"] == {"name": "Port 7", "native_network": "n_main"}
    entries = c.get("/api/audit?limit=30").json()["entries"]
    assert any(e["action"].startswith("firewall.") for e in entries)


def test_power_off_and_power_cycle_need_their_confirmation(c):
    ops = [{"kind": "port.set", "object": "p_sw_14", "after": {"poe": False}}, {"kind": "port.cycle", "object": "p_sw_7", "after": {}}]
    c.put("/api/firewall/draft", json={"ops": ops}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert not pv["blocks"] and sorted(pv["confirm"]) == ["confirm:port.cycle:p_sw_7", "confirm:port.set:p_sw_14"]
    r = c.post("/api/firewall/plan", json={"confirmed": pv["confirm"][:1]}, headers=HDR)
    assert r.status_code == 409 and r.json()["error"] == "fw.block.confirm"
    p = c.post("/api/firewall/plan", json={"confirmed": pv["confirm"]}, headers=HDR).json()
    assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(c)
    assert v["history"][0]["status"] == "done"
    ports = {x["id"]: x for x in v["state"]["ports"]}
    assert ports["p_sw_14"]["poe"]["on"] is False and ports["p_sw_7"]["cycles"] == 1


def test_a_protected_port_is_refused_and_viewers_cannot_draft(c):
    c.put("/api/firewall/draft", json={"ops": [{"kind": "port.set", "object": "p_sw_5", "after": {"native_network": "n_work"}}]}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert pv["blocks"][0][0] == "pc.block.protected" and pv["blocks"][0][1]["device"] == "Home server"
    c.ll.accounts.create("vic", PW, role="viewer")
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "vic", "password": PW}, headers=HDR).status_code == 200
    r = c.put("/api/firewall/draft", json={"ops": [{"kind": "port.set", "object": "p_sw_7", "after": {"name": "x"}}]}, headers=HDR)
    assert r.status_code == 403


# ---------------------------------------------------------------------------- nightly section "Port settings"
TZ = "Europe/Amsterdam"
Z = ZoneInfo(TZ)


def at(y, mo, d, hh=0, mm=0):
    return datetime(y, mo, d, hh, mm, tzinfo=Z).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, clock, states):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    core = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    core.live_configs = lambda: [{"key": "int:1", "name": "Controller", "family": "sample", "state": st} for st in states()]
    rep = Reports(core, Path(tmp_path), clock=clock)
    rep.cert_check = lambda: []
    core.reports = rep
    store.set_setting("report_cfg", {"time": "06:30", "tz": TZ, "keep_days": 30, "link": ""})
    return core, rep


def section(report, sid):
    return next((s for s in report["sections"] if s["id"] == sid), None)


def texts(report, sec):
    out = {}
    for lang in languages():
        r = render.Renderer(lang, Z, report["day"])
        lines = [r.tr(sec["summary"]["key"], **r.values(sec["summary"]["vars"])), r.tr(sec["title"])] + [r.line(i) for i in sec["items"]]
        for line in lines:
            assert not re.search(r"\{|\}|report\.", line), (lang, line)
        out[lang] = lines
    return out


def test_nightly_port_settings_baseline_changes_and_calm(tmp_path):
    cur = {"st": copy.deepcopy(sample())}
    clock = Clock(at(2026, 11, 19, 6, 30))
    core, rep = make(tmp_path, clock, lambda: [cur["st"]])
    r1 = asyncio.run(rep.run_due(clock.t))
    s1 = section(r1, "portconf")
    assert s1["summary"]["key"] == "report.pc.baseline" and s1["summary"]["vars"]["n"] == 17 and not s1["items"]
    t1 = texts(r1, s1)
    assert "17 ports" in t1["en"][0] and "17 Ports" in t1["de"][0]
    st = copy.deepcopy(cur["st"])
    portconf.apply_to(st, port(st, 7), {"native_network": "n_work"})
    portconf.apply_to(st, port(st, 14), {"poe": False})
    portconf.apply_to(st, port(st, 8), {"profile": "pp_work"})
    cur["st"] = st
    clock.t = at(2026, 11, 20, 6, 30)
    r2 = asyncio.run(rep.run_due(clock.t))
    s2 = section(r2, "portconf")
    keys = sorted(i["key"] for i in s2["items"])
    assert keys == ["report.pc.native", "report.pc.native", "report.pc.poe_off", "report.pc.profile"]
    assert s2["summary"]["vars"] == {"n": 3} and s2["level"] == "warn"
    t2 = texts(r2, s2)
    assert any("Core switch · Port 7 carries Work untagged now (before: Main)" in x for x in t2["en"])
    assert any("Core switch · Port 14 liefert keinen Strom mehr" in x for x in t2["de"])
    clock.t = at(2026, 11, 21, 6, 30)
    r3 = asyncio.run(rep.run_due(clock.t))
    s3 = section(r3, "portconf")
    assert s3["summary"]["key"] == "report.pc.calm" and not s3["items"]
    assert texts(r3, s3)["en"][0] == "No port changed its profile, networks or power (17 ports)."


def test_nightly_section_only_with_port_settings(tmp_path):
    clock = Clock(at(2026, 11, 19, 6, 30))
    st = sample()
    for p in st["ports"]:
        p.update(native_network=None, tagged_mode=None, profile="")
    core, rep = make(tmp_path, clock, lambda: [st])
    assert section(asyncio.run(rep.run_due(clock.t)), "portconf") is None
