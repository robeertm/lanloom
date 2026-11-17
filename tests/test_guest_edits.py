"""Guest changes in the firewall editor (hypervisor stage B): network card bridge/VLAN/firewall flag, the guest's own
firewall and its rules, start and shutdown - drafts, preview with lockout rules and confirmations, plan, gate,
simulation, undo, roles and the audit log. The rehearsal target is the sample hypervisor next to the sample network."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom.firewall import adapter, guests, model, steps as stepkinds
from lanloom.firewall.runner import Runner
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
ORIGIN = "http://lanloom.test"
_S = adapter.with_sample_hypervisor(adapter.sample_state(), now=1_800_000_000)
_NAMES = {"broker": "broker", "automation": "automation", "filter": "name-filter", "test": "test-box", "template": "base-template"}
G = {k: next(g["id"] for g in _S["guests"] if g["name"] == name) for k, name in _NAMES.items()}
NIC_TEST = next(n["id"] for n in _S["guest_nics"] if n["guest"] == G["test"] and n["name"] == "net0")
NIC_BROKER = next(n["id"] for n in _S["guest_nics"] if n["guest"] == G["broker"] and n["name"] == "net0")
RULE_BROKER = next(r["id"] for r in _S["guest_rules"] if r["owner"] == G["broker"] and r["pos"] == 0)
CRIT = {"192.168.50.53": "hv.why.dns", "192.168.50.13": "hv.why.hub"}


def st():
    return adapter.with_sample_hypervisor(adapter.sample_state(), now=1_800_000_000)


def crit(s):
    return guests.critical_guests(s, CRIT)


def pv_of(s, ops):
    return guests.preview(s, guests.clean_ops(s, ops), crit(s))


# ---------------------------------------------------------------------------- drafts and preview (pure)
def test_sample_hypervisor_sits_next_to_the_sample_network():
    s = st()
    assert s["devices"] and s["networks"] and len(s["guests"]) == 5 and s["bridges"]
    assert set(crit(s)) == {G["broker"], G["filter"]}
    assert crit(s)[G["filter"]] == "hv.why.dns"


def test_clean_ops_keeps_real_changes_with_their_before():
    s = st()
    ops = guests.clean_ops(s, [{"kind": "guest.nic", "object": NIC_TEST, "after": {"vlan": 30, "bridge": "vmbr0"}},
                               {"kind": "guest.nic", "object": "nope", "after": {"vlan": 30}},
                               {"kind": "guest.power", "object": G["test"], "after": {"status": "stopped"}},   # no change
                               {"kind": "guest.fw_rule", "object": RULE_BROKER, "after": {"evil": 1}},
                               {"kind": "port.set", "object": "x", "after": {"poe": False}}])
    assert list(ops.values()) == [{"kind": "guest.nic", "object": NIC_TEST, "before": {"vlan": 70}, "after": {"vlan": 30}}]


def test_new_rule_gets_a_new_id_and_defaults():
    s = st()
    ops = guests.clean_ops(s, [{"kind": "guest.fw_rule_create", "after": {"guest": G["test"], "direction": "in", "action": "allow",
                                                                         "proto": "tcp", "ports": "22", "name": "SSH from admins"}}])
    (step,) = ops.values()
    assert stepkinds.NEW_ID.match(step["object"]) and step["after"]["enabled"] is True and step["after"]["source"] == ""
    stepkinds.check_step(step)


def test_preview_shows_changes_warnings_and_the_confirmation_of_a_stop():
    s = st()
    s["guests"][3]["status"] = "running"          # test-box running
    pv = pv_of(s, [{"kind": "guest.power", "object": G["test"], "after": {"status": "stopped"}},
                   {"kind": "guest.nic", "object": NIC_TEST, "after": {"vlan": 30}}])
    assert not pv["blocks"] and len(pv["steps"]) == 2
    token = "confirm:guest.power:%s" % G["test"]
    assert pv["confirm"] == [token]
    assert ["fw.warn.hv_stop", {"name": "test-box", "confirm": token}] in pv["warnings"]
    assert any(w[0] == "fw.warn.hv_nic" for w in pv["warnings"])
    nic = next(c for c in pv["changes"] if c["kind"] == "guest.nic")
    assert nic["name"] == "test-box · net0" and nic["edit"] == [{"field": "vlan", "from": 70, "to": 30}]


@pytest.mark.parametrize("op,why", [
    ({"kind": "guest.power", "object": G["filter"], "after": {"status": "stopped"}}, "fw.block.hv_critical"),     # name server
    ({"kind": "guest.nic", "object": NIC_BROKER, "after": {"vlan": 40}}, "fw.block.hv_critical"),               # hub's broker
    ({"kind": "guest.fw_rule", "object": RULE_BROKER, "after": {"enabled": False}}, "fw.block.hv_critical"),
    ({"kind": "guest.firewall", "object": G["filter"], "after": {"firewall": True}}, "fw.block.hv_critical"),
    ({"kind": "guest.power", "object": G["automation"], "after": {"status": "stopped"}}, "fw.block.protected"),   # protection flag
    ({"kind": "guest.nic", "object": NIC_TEST, "after": {"bridge": "vmbr9"}}, "fw.block.hv_bridge_unknown"),
])
def test_lockout_rules_block_in_the_preview(op, why):
    pv = pv_of(st(), [op])
    assert not pv["steps"] and pv["blocks"][0][0] == why


def test_critical_guest_may_still_be_started_and_its_firewall_switched_off():
    s = st()
    s["guests"][0]["status"] = "stopped"
    pv = pv_of(s, [{"kind": "guest.power", "object": G["broker"], "after": {"status": "running"}}])
    assert not pv["blocks"] and pv["steps"]
    pv = pv_of(st(), [{"kind": "guest.firewall", "object": G["broker"], "after": {"firewall": False}}])
    assert not pv["blocks"] and pv["warnings"][0][0] == "fw.warn.hv_fw_off"


def test_vlan_outside_the_bridge_list_and_on_a_plain_vnet_is_refused():
    s = st()
    vmbr0 = next(b for b in s["bridges"] if b["name"] == "vmbr0")
    vmbr0["vids"] = "2-100"
    assert pv_of(s, [{"kind": "guest.nic", "object": NIC_TEST, "after": {"vlan": 200}}])["blocks"][0][0] == "fw.block.hv_vlan_not_allowed"
    assert pv_of(s, [{"kind": "guest.nic", "object": NIC_TEST, "after": {"bridge": "things"}}])["blocks"][0][0] == "fw.block.hv_vlan_vnet"
    ok = pv_of(s, [{"kind": "guest.nic", "object": NIC_TEST, "after": {"bridge": "things", "vlan": None}}])
    assert not ok["blocks"]


def test_tag_on_a_bridge_that_is_not_vlan_aware_gives_a_warning():
    pv = pv_of(st(), [{"kind": "guest.nic", "object": NIC_TEST, "after": {"bridge": "vmbr1"}}])
    assert not pv["blocks"] and any(w[0] == "fw.warn.hv_vlan_classic" for w in pv["warnings"])


def test_ports_need_tcp_or_udp_and_foreign_rules_cannot_be_removed():
    s = st()
    pv = pv_of(s, [{"kind": "guest.fw_rule", "object": RULE_BROKER, "after": {"proto": "icmp"}}])
    assert pv["blocks"]          # broker is critical anyway; the check below uses an ordinary guest
    s["guest_rules"].append({"id": "rx", "scope": "guest", "owner": G["test"], "guest": G["test"], "pos": 0, "direction": "in",
                             "action": "allow", "enabled": True, "proto": "tcp", "ports": "80", "dport": "80", "source": None,
                             "name": "web", "own": False, "plain": True})
    pv = pv_of(s, [{"kind": "guest.fw_rule", "object": "rx", "after": {"proto": "icmp"}}])
    assert pv["blocks"][0][0] == "fw.block.hv_rule_ports"
    assert guests.clean_ops(s, [{"kind": "guest.fw_rule_delete", "object": "rx"}]) == {}       # not Lanloom's own


def test_new_rule_with_another_rule_change_on_the_same_guest_is_refused():
    s = st()
    s["guest_rules"].append({"id": "rx", "scope": "guest", "owner": G["test"], "guest": G["test"], "pos": 0, "direction": "in",
                             "action": "allow", "enabled": True, "proto": "tcp", "ports": "80", "dport": "80", "source": None,
                             "name": "web", "own": False, "plain": True})
    pv = pv_of(s, [{"kind": "guest.fw_rule", "object": "rx", "after": {"enabled": False}},
                   {"kind": "guest.fw_rule_create", "after": {"guest": G["test"], "direction": "in", "action": "block"}}])
    assert pv["blocks"][-1][0] == "fw.block.hv_rule_mix"


def test_complex_rule_can_only_be_switched():
    s = st()
    group_rule = next(r for r in s["guest_rules"] if r["owner"] == G["automation"] and r["action"] == "group")
    assert guests.step_lockout(s, {"kind": "guest.fw_rule", "object": group_rule["id"], "before": {"enabled": True},
                                   "after": {"enabled": False}}, {}) is None
    macro = next(r for r in s["guest_rules"] if r["owner"] == G["automation"] and r.get("macro"))
    assert guests.step_lockout(s, {"kind": "guest.fw_rule", "object": macro["id"], "before": {"source": "+admins"},
                                   "after": {"source": "192.168.50.0/24"}}, {}) == "fw.block.hv_rule_complex"


# ---------------------------------------------------------------------------- simulation through the runner
def _plan(s, ops, confirmed=()):
    pv = pv_of(s, ops)
    assert not pv["blocks"], pv["blocks"]
    pv.update(base=model.fingerprint(s), costs={"objects": len(pv["steps"])}, requester=None)
    return model.build_plan(s, pv, target="simulation", user="admin", confirmed=list(confirmed) + pv["confirm"])


def test_simulation_applies_guest_steps_and_rolls_them_back(tmp_path):
    sim = adapter.SimulatedController(settle_s=0, hypervisor=True, fail_after_writes=3)
    s = sim.read()
    plan = _plan(s, [{"kind": "guest.nic", "object": NIC_TEST, "after": {"vlan": 30, "firewall": True}},
                     {"kind": "guest.power", "object": G["test"], "after": {"status": "running"}},
                     {"kind": "guest.fw_rule_create", "after": {"guest": G["test"], "direction": "in", "action": "allow",
                                                                "proto": "tcp", "ports": "22", "name": "SSH"}}])
    r = Runner(sim, tmp_path, target="simulation", require_signature=False, watch_s=0, check_s=0, sleep=lambda x: None)
    assert r.run(plan)["status"] == "rolled_back"
    now = sim.read()
    nic = next(n for n in now["guest_nics"] if n["id"] == NIC_TEST)
    assert nic["vlan"] == 70 and nic["firewall"] is False
    assert next(g for g in now["guests"] if g["id"] == G["test"])["status"] == "stopped"
    sim.fail_after_writes = 0
    plan = _plan(sim.read(), [{"kind": "guest.nic", "object": NIC_TEST, "after": {"vlan": 30}},
                              {"kind": "guest.power", "object": G["test"], "after": {"status": "running"}},
                              {"kind": "guest.fw_rule_create", "after": {"guest": G["test"], "direction": "in", "action": "allow",
                                                                         "proto": "tcp", "ports": "22", "name": "SSH"}}])
    res = Runner(sim, tmp_path / "2", target="simulation", require_signature=False, watch_s=0, check_s=0, sleep=lambda x: None).run(plan)
    assert res["status"] == "done"
    now = sim.read()
    assert next(n for n in now["guest_nics"] if n["id"] == NIC_TEST)["vlan"] == 30
    new_rule = next(x for x in now["guest_rules"] if x.get("own"))
    assert new_rule["comment"] == "lanloom: SSH" and new_rule["dport"] == "22" and new_rule["owner"] == G["test"]
    kinds = sorted(x["kind"] for x in res["inverse"])
    assert kinds == ["guest.fw_rule_delete", "guest.nic", "guest.power"]


def test_runner_refuses_a_stop_of_a_critical_guest_even_with_a_forged_plan(tmp_path):
    sim = adapter.SimulatedController(settle_s=0, hypervisor=True)
    s = sim.read()
    step = {"kind": "guest.power", "object": G["filter"], "before": {"status": "running"}, "after": {"status": "stopped"}}
    plan = model.build_plan(s, {"steps": [step], "base": model.fingerprint(s), "warnings": [], "costs": {"objects": 1}},
                            target="simulation", user="admin", confirmed=[stepkinds.confirm_token(step)])
    res = Runner(sim, tmp_path, target="simulation", require_signature=False, watch_s=0, check_s=0, sleep=lambda x: None).run(plan)
    assert res["status"] == "refused" and res["reason"] == "fw.block.hv_critical"
    assert next(g for g in sim.read()["guests"] if g["id"] == G["filter"])["status"] == "running"


def test_stop_without_its_confirmation_is_refused_by_the_gate(tmp_path):
    sim = adapter.SimulatedController(settle_s=0, hypervisor=True)
    s = sim.read()
    step = {"kind": "guest.power", "object": G["test"], "before": {"status": "stopped"}, "after": {"status": "running"}}
    plan = model.build_plan(s, {"steps": [step], "base": model.fingerprint(s), "warnings": [], "costs": {"objects": 1}},
                            target="simulation", user="admin", confirmed=[])
    res = Runner(sim, tmp_path, target="simulation", require_signature=False, watch_s=0, check_s=0, sleep=lambda x: None).run(plan)
    assert res["status"] == "refused" and res["reason"] == "fw.block.confirm"


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
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
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


def test_editor_offers_guest_changes_with_critical_guests_marked(c):
    v = c.get("/api/firewall?source=editor").json()
    assert v["mode"] == "simulation" and v["state"]["guests"]
    assert {"guest.nic", "guest.power", "guest.fw_rule_create"} <= set(v["capabilities"]["write"])
    assert "guest.power" in v["capabilities"]["confirm"]
    assert v["guests"]["critical"][G["filter"]] == "hv.why.dns"
    assert "vmbr0" in v["guests"]["bridges"][G["test"]] and "things" in v["guests"]["bridges"][G["test"]]


def test_draft_preview_apply_and_undo_of_guest_changes(c):
    d = c.put("/api/firewall/draft", json={"ops": [{"kind": "guest.nic", "object": NIC_TEST, "after": {"vlan": 30}},
                                                    {"kind": "guest.power", "object": G["test"], "after": {"status": "running"}}]},
              headers=HDR).json()
    assert len(d["ops"]) == 2
    # a client of an earlier release sends moves only: the guest changes stay
    d = c.put("/api/firewall/draft", json={"moves": {}}, headers=HDR).json()
    assert len(d["ops"]) == 2
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    token = "confirm:guest.power:%s" % G["test"]
    assert not pv["blocks"] and pv["confirm"] == [token]
    r = c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR)
    assert r.status_code == 409 and r.json()["error"] == "fw.block.confirm"
    p = c.post("/api/firewall/plan", json={"confirmed": [token]}, headers=HDR).json()
    assert p["plan"]["v"] == 2
    assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(c)
    job = v["history"][0]
    assert job["status"] == "done", job
    assert {x["kind"] for x in job["summary"]} == {"guest.nic", "guest.power"}
    assert next(x for x in job["summary"] if x["kind"] == "guest.power")["status"] == "running"
    assert next(n for n in v["state"]["guest_nics"] if n["id"] == NIC_TEST)["vlan"] == 30, (job, v["history"][:3])
    assert next(g for g in v["state"]["guests"] if g["id"] == G["test"])["status"] == "running"
    d = c.post("/api/firewall/undo", json={"plan": job["plan"]}, headers=HDR).json()
    afters = sorted((o["kind"], o["after"]) for o in d["ops"].values())
    assert afters == [("guest.nic", {"vlan": 70}), ("guest.power", {"status": "stopped"})]
    entries = c.get("/api/audit?limit=30").json()["entries"]
    drafts = [e for e in entries if e["action"] == "firewall.draft"]
    assert drafts[-1]["detail"]["guest_changes"] == 2


def test_draft_of_a_critical_guest_is_kept_but_blocked_in_the_preview(c):
    c.put("/api/firewall/draft", json={"ops": [{"kind": "guest.power", "object": G["filter"], "after": {"status": "stopped"}}]}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert pv["blocks"] == [["fw.block.hv_critical", {"name": "name-filter", "reason": "hv.why.dns"}]]
    assert c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).status_code == 409


def test_viewer_cannot_draft_guest_changes_and_operator_cannot_approve(c):
    c.ll.accounts.create("vic", PW, role="viewer")
    c.ll.accounts.create("op", PW, role="operator")
    op = [{"kind": "guest.nic", "object": NIC_TEST, "after": {"vlan": 30}}]
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "vic", "password": PW}, headers=HDR).status_code == 200
    assert c.put("/api/firewall/draft", json={"ops": op}, headers=HDR).status_code == 403
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "op", "password": PW}, headers=HDR).status_code == 200
    assert c.put("/api/firewall/draft", json={"ops": op}, headers=HDR).status_code == 200
    assert c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).status_code == 403
