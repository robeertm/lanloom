"""Firewall editor: model, gate, signatures, the executor pipeline against an in-memory stand-in, and the API.

Nothing here touches a network: the controller is the built-in simulation or a stand-in adapter."""
import hashlib
import json
import os
import stat
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import executor as executor_mod
from lanloom.firewall import adapter, canon, model, runner, signature
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
RP, ORIGIN = "lanloom.test", "http://lanloom.test"


# ---------------------------------------------------------------------------- helpers
def state():
    return adapter.sample_state()


def plan_for(st, moves, confirmed=None, target="simulation", requester_ip=None, now=None):
    pv = model.preview(st, moves, requester_ip=requester_ip)
    assert pv["steps"], pv
    return model.build_plan(st, pv, target=target, user="admin", confirmed=confirmed if confirmed is not None else pv["confirm"], now=now)


class Key:
    def __init__(self, cid=b"credential-1"):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        n = self.priv.public_key().public_numbers()
        self.x, self.y = n.x.to_bytes(32, "big"), n.y.to_bytes(32, "big")
        self.cid = cid
        self.count = 0

    def pinned(self, rp=RP):
        return {"id": signature.b64u(self.cid), "rp_id": rp, "x": self.x.hex(), "y": self.y.hex(),
                "fingerprint": signature.key_fingerprint(self.cid)}

    def cose(self):
        return bytes([0xa5, 0x01, 0x02, 0x03, 0x26, 0x20, 0x01, 0x21, 0x58, 0x20]) + self.x + bytes([0x22, 0x58, 0x20]) + self.y

    def sign(self, challenge, rp=RP, origin=ORIGIN, flags=0x05, ctype="webauthn.get"):
        self.count += 1
        cd = json.dumps({"type": ctype, "challenge": signature.b64u(challenge), "origin": origin}).encode()
        ad = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big")
        sig = self.priv.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"credential_id": signature.b64u(self.cid), "authenticator_data": signature.b64u(ad),
                "client_data": signature.b64u(cd), "signature": signature.b64u(sig)}


def make_runner(tmp_path, sim=None, **kw):
    events = []
    kw.setdefault("require_signature", False)
    r = runner.Runner(sim or adapter.SimulatedController(settle_s=0), tmp_path / "state", target=kw.pop("target", "simulation"),
                      watch_s=kw.pop("watch_s", 0), check_s=0, report=events.append, sleep=lambda s: None, **kw)
    return r, events


# ---------------------------------------------------------------------------- model and preview
def test_canonical_hash_is_stable_and_rejects_floats():
    a = {"b": 1, "a": [True, None, "x"]}
    assert canon.canonical(a) == b'{"a":[true,null,"x"],"b":1}'
    assert canon.digest_hex(a) == canon.digest_hex(json.loads(json.dumps(a)))
    with pytest.raises(canon.NotCanonical):
        canon.canonical({"x": 1.5})


def test_preview_explains_a_move_in_plain_words():
    pv = model.preview(state(), {"d10": "n_work"})
    assert pv["steps"] == [{"kind": "move", "device": "d10", "before": {"network": "n_main", "ip": "192.168.50.22"},
                            "after": {"network": "n_work", "ip": "192.168.40.22"}}]
    keys = [w[0] for w in pv["warnings"]]
    assert keys[0] == "fw.warn.reprovision"       # wireless move: every access point reconfigured
    assert {"fw.warn.new_ip", "fw.warn.private_address", "fw.warn.discovery", "fw.warn.dependency"} <= set(keys)
    assert pv["confirm"] == ["private_address:d10"] and not pv["blocks"]
    ch = pv["changes"][0]
    assert ["fw.can.internet", {}] in ch["may"] and ["fw.can.discovery", {}] in ch["may_not"]


def test_preview_blocks_protected_management_and_shared_port():
    pv = model.preview(state(), {"d05": "n_work", "d08": "n_mgmt", "d09": "n_work"})
    assert sorted(b[0] for b in pv["blocks"]) == ["fw.block.management", "fw.block.protected", "fw.block.shared_port"]
    assert not pv["steps"]


def test_address_keeps_last_number_or_takes_next_free():
    st = state()
    pv = model.preview(st, {"d11": "n_work"})     # .23 is free in Work
    assert pv["steps"][0]["after"]["ip"] == "192.168.40.23"
    pv = model.preview(st, {"d16": "n_main"})     # .24 free in Main
    assert pv["steps"][0]["after"]["ip"] == "192.168.50.24"
    pv = model.preview(st, {"d27": "n_main"})     # .23 is taken by the tablet -> first free from .20
    ip = pv["steps"][0]["after"]["ip"]
    assert ip != "192.168.50.23" and ip.startswith("192.168.50.")


def test_preview_self_device_and_lockout():
    pv = model.preview(state(), {"d08": "n_cams"}, requester_ip="192.168.50.20")
    assert "self" in pv["confirm"] and ["fw.block.lockout", {"name": "Office laptop"}] in pv["blocks"]


def test_too_many_changes_are_blocked():
    st = state()
    movable = [d["id"] for d in st["devices"] if all(v is None for k, v in model.drop_targets(st, d).items() if k == "n_guest")]
    pv = model.preview(st, {d: "n_guest" for d in movable})
    assert len(movable) > model.MAX_OBJECTS and pv["blocks"][-1][0] == "fw.block.too_many"


# ---------------------------------------------------------------------------- gate
@pytest.mark.parametrize("mutate,key", [
    (lambda p, s: p.update(target="controller"), "fw.block.target"),
    (lambda p, s: p.update(expires=p["created"] - 1), "fw.block.expired"),
    (lambda p, s: p.update(confirmed=[]), "fw.block.confirm"),
    (lambda p, s: s["devices"][9].update(ip="192.168.50.99"), "fw.block.conflict"),
    (lambda p, s: p["steps"][0]["after"].update(ip="10.9.9.9"), "fw.block.format"),
    (lambda p, s: p["steps"][0].update(extra=1), "fw.block.format"),
])
def test_gate_refuses(mutate, key):
    st = state()
    plan = plan_for(st, {"d10": "n_work"})
    mutate(plan, st)
    with pytest.raises(model.Refused) as exc:
        model.gate(st, plan, target="simulation", now=time.time(), quiet=False, busy=False, healthy=(True, ""))
    assert exc.value.key == key


def test_gate_refuses_moving_a_protected_device_even_in_a_crafted_plan():
    st = state()
    plan = plan_for(st, {"d10": "n_work"})
    plan["steps"] = [{"kind": "move", "device": "d01", "before": {"network": "n_mgmt", "ip": "192.168.10.1"},
                      "after": {"network": "n_work", "ip": "192.168.40.1"}}]
    plan["base"] = canon.fingerprint(st)
    with pytest.raises(model.Refused) as exc:
        model.gate(st, plan, target="simulation", now=time.time(), quiet=False, busy=False, healthy=(True, ""))
    assert exc.value.key == "fw.block.protected"


def test_gate_busy_health_and_quiet_hours():
    st = state()
    plan = plan_for(st, {"d19": "n_cams"})
    for kw, key in ((dict(busy=True), "fw.block.busy"), (dict(healthy=(False, "memory")), "fw.block.health"), (dict(quiet=True), "fw.block.quiet")):
        args = dict(target="simulation", now=time.time(), quiet=False, busy=False, healthy=(True, ""))
        args.update(kw)
        with pytest.raises(model.Refused) as exc:
            model.gate(st, plan, **args)
        assert exc.value.key == key
    assert model.in_quiet_hours("21-7", time.mktime((2026, 1, 1, 23, 0, 0, 0, 0, -1)))
    assert not model.in_quiet_hours("21-7", time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1)))
    assert not model.in_quiet_hours("", time.time())


# ---------------------------------------------------------------------------- signatures
def test_signature_checks():
    key = Key()
    challenge = os.urandom(32)
    pinned = [key.pinned()]
    assert signature.verify_assertion(key.sign(challenge), challenge, pinned, [ORIGIN])[0] == pinned[0]["id"]
    cases = [
        (key.sign(os.urandom(32)), "another plan"),
        (key.sign(challenge, flags=0x01), "not verified"),
        (key.sign(challenge, origin="http://evil.example"), "origin"),
        (key.sign(challenge, rp="evil.example"), "rp id"),
        (key.sign(challenge, ctype="webauthn.create"), "ceremony"),
        (Key(b"credential-1").sign(challenge), "bad signature"),       # same id, other key
        (Key(b"other").sign(challenge), "unknown key"),
    ]
    for a, why in cases:
        with pytest.raises(signature.Invalid) as exc:
            signature.verify_assertion(a, challenge, pinned, [ORIGIN])
        assert why in str(exc.value)
    a = key.sign(challenge)
    with pytest.raises(signature.Invalid, match="counter"):
        signature.verify_assertion(a, challenge, pinned, [ORIGIN], counters={pinned[0]["id"]: key.count})


def test_cose_key_roundtrip():
    key = Key()
    assert signature.cose_to_xy(key.cose()) == (key.x.hex(), key.y.hex())
    with pytest.raises(signature.Invalid):
        signature.cose_to_xy(bytes([0xa1, 0x01, 0x01]))      # not an EC2 key


# ---------------------------------------------------------------------------- the pipeline
def test_simulation_run_backs_up_watches_and_finishes(tmp_path):
    sim = adapter.SimulatedController(settle_s=0)
    r, events = make_runner(tmp_path, sim)
    st = sim.read()
    plan = plan_for(st, {"d19": "n_cams", "d10": "n_work"})
    res = r.run(plan)
    assert res["status"] == "done" and [e["stage"] for e in events] == ["verify", "gate", "backup", "apply", "settle", "watch", "done"]
    after = {d["id"]: d for d in sim.read()["devices"]}
    assert after["d19"]["network"] == "n_cams" and after["d10"]["ip"] == "192.168.40.22"
    backup = tmp_path / "state" / "backup" / plan["id"] / "before.json"
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    assert {d["id"] for d in json.loads(backup.read_text())["snapshot"]["devices"]} == {"d19", "d10"}
    assert res["inverse"][0]["after"]["network"] in ("n_main", "n_smart")
    assert r.run(plan)["reason"] == "fw.block.nonce"          # replay
    line = json.loads((tmp_path / "state" / "protocol.jsonl").read_text().splitlines()[0])
    assert line["status"] == "done" and "192.168" not in json.dumps(line)


def test_device_that_does_not_come_back_is_rolled_back_alone(tmp_path):
    sim = adapter.SimulatedController(settle_s=0, fail_devices={"d19"})
    clock = [time.time()]
    r, _ = make_runner(tmp_path, sim, watch_s=300, clock=lambda: clock[0])
    r.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
    r.check_s = 60
    res = r.run(plan_for(sim.read(), {"d19": "n_cams", "d20": "n_cams"}, now=clock[0]))
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.rolled_back_device"
    after = {d["id"]: d["network"] for d in sim.read()["devices"]}
    assert after["d19"] == "n_smart" and after["d20"] == "n_cams"


class BrokenAfterChange(adapter.SimulatedController):
    def probe(self, steps):
        return {"global_ok": False, "missing": []}


class FailsOnWrite(adapter.SimulatedController):
    def apply(self, step):
        super().apply(step)
        raise RuntimeError("controller said no")


@pytest.mark.parametrize("cls,reason", [(BrokenAfterChange, "fw.res.rolled_back_global"), (FailsOnWrite, "fw.res.apply_failed")])
def test_everything_goes_back_when_the_network_breaks(tmp_path, cls, reason):
    sim = cls(settle_s=0)
    before = sim.read()
    r, _ = make_runner(tmp_path, sim)
    res = r.run(plan_for(before, {"d19": "n_cams", "d20": "n_cams"}))
    assert res["status"] == "rolled_back" and res["reason"] == reason
    assert sim.read()["devices"] == before["devices"]
    assert not (tmp_path / "state" / "active.json").exists()


def test_crash_recovery_restores_the_backup(tmp_path):
    sim = adapter.SimulatedController(settle_s=0)
    before = sim.read()
    r, _ = make_runner(tmp_path, sim)
    plan = plan_for(before, {"d19": "n_cams"})
    snap = sim.snapshot(["d19"])
    (tmp_path / "state" / "backup" / plan["id"]).mkdir()
    runner.write_private(tmp_path / "state" / "backup" / plan["id"] / "before.json", {"snapshot": snap})
    sim.apply(plan["steps"][0])
    runner.write_private(tmp_path / "state" / "active.json", {"plan": plan["id"], "applied": True})
    assert r.busy()
    assert r.recover()["status"] == "rolled_back"
    assert sim.read()["devices"] == before["devices"] and not r.busy()


def test_backup_never_follows_a_planted_link(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("keep")
    link = tmp_path / "before.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        runner.write_private(link, {"x": 1}, exclusive=True)
    assert target.read_text() == "keep"


def test_signed_run_with_pinned_key(tmp_path):
    key = Key()
    sim = adapter.SimulatedController(settle_s=0)
    r, _ = make_runner(tmp_path, sim, require_signature=True, keys=[key.pinned()], origins=[ORIGIN])
    plan = plan_for(sim.read(), {"d19": "n_cams"})
    assert r.run(plan, None)["reason"] == "fw.block.signature"
    plan = plan_for(sim.read(), {"d19": "n_cams"})
    other = plan_for(sim.read(), {"d20": "n_cams"})
    assert r.run(plan, key.sign(canon.digest(other)))["reason"] == "fw.block.signature"     # signature of another plan
    plan = plan_for(sim.read(), {"d19": "n_cams"})
    assert r.run(plan, key.sign(canon.digest(plan)))["status"] == "done"


def test_executor_is_not_armed_without_phrase_and_file(tmp_path):
    env = {"LANLOOM_EXECUTOR_STATE": str(tmp_path), "LANLOOM_EXECUTOR_TARGET": "controller", "LANLOOM_EXECUTOR_ARM": "I-UNDERSTAND"}
    assert not executor_mod.Settings(env).armed()
    (tmp_path / "armed").write_text("")
    assert executor_mod.Settings(env).armed()
    assert not executor_mod.Settings(dict(env, LANLOOM_EXECUTOR_ARM="yes")).armed()
    with pytest.raises(SystemExit):
        executor_mod.build_adapter(executor_mod.Settings(dict(env, LANLOOM_EXECUTOR_ARM="")))
    assert isinstance(executor_mod.build_adapter(executor_mod.Settings({"LANLOOM_EXECUTOR_STATE": str(tmp_path)})), adapter.SimulatedController)


def test_trust_key_pins_and_lists(tmp_path, capsys):
    s = executor_mod.Settings({"LANLOOM_EXECUTOR_STATE": str(tmp_path)})
    key = Key()
    assert executor_mod.trust_key(s, json.dumps(key.pinned())) == 0
    assert key.pinned()["fingerprint"] in capsys.readouterr().out
    assert runner.read_private(tmp_path / "signing-keys.json", [])[0]["x"] == key.x.hex()
    assert executor_mod.trust_key(s, "{}") == 2


def test_adapter_loader_requires_the_base_class():
    with pytest.raises(TypeError):
        adapter.load_adapter("json:JSONDecoder", {})
    assert isinstance(adapter.load_adapter("lanloom.firewall.adapter:SimulatedController", {"settle_s": 0}), adapter.ControllerAdapter)


# ---------------------------------------------------------------------------- API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_WATCH_S", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_SETTLE_S", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url=ORIGIN) as c:
        c.data_dir = tmp_path
        c.ll = app.state.ll
        yield c


def sign_in(c):
    code = (Path(c.data_dir) / "setup-code.txt").read_text().strip()
    assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200


def wait_done(c):
    for _ in range(200):
        v = c.get("/api/firewall").json()
        if not v["running"]:
            return v
        time.sleep(0.05)
    raise AssertionError("simulation did not finish")


def test_firewall_needs_sign_in_and_executor_routes_need_the_token(client):
    for method, path in (("GET", "/api/firewall"), ("POST", "/api/firewall/preview"), ("PUT", "/api/firewall/draft")):
        assert client.request(method, path, headers=HDR, json={}).status_code == 401
    sign_in(client)
    for method, path in (("GET", "/api/executor/plans"), ("POST", "/api/executor/state"), ("POST", "/api/executor/report")):
        assert client.request(method, path, headers=HDR, json={}).status_code == 401
        assert client.request(method, path, headers=dict(HDR, Authorization="Bearer " + "x" * 40), json={}).status_code == 401


def test_simulation_path_draft_preview_apply_undo(client):
    sign_in(client)
    v = client.get("/api/firewall").json()
    assert v["mode"] == "simulation" and v["state"]["controller"]["kind"] == "simulation"
    assert v["targets"]["d01"]["n_work"] == "fw.why.protected"
    assert client.put("/api/firewall/draft", json={"moves": {"d19": "n_cams", "nope": "n_work"}}, headers=HDR).json()["moves"] == {"d19": "n_cams"}
    pv = client.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert pv["changes"][0]["new_ip"] == "192.168.70.60"
    p = client.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).json()
    assert p["sign"] is None and len(p["hash"]) == 64
    assert client.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(client)
    job = v["history"][0]
    assert job["status"] == "done" and job["summary"] == [{"name": "Washer plug", "from": "Smart home", "to": "Cameras"}]
    assert {d["id"]: d["network"] for d in v["state"]["devices"]}["d19"] == "n_cams"
    assert v["draft"]["moves"] == {}
    d = client.post("/api/firewall/undo", json={"plan": job["plan"]}, headers=HDR).json()
    assert d["moves"] == {"d19": "n_smart"} and d["undo_of"] == job["plan"]
    p = client.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).json()
    assert p["plan"]["undo_of"] == job["plan"] and p["plan"]["steps"][0]["after"]["ip"] == "192.168.60.60"
    client.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR)
    v = wait_done(client)
    assert {d["id"]: d["network"] for d in v["state"]["devices"]}["d19"] == "n_smart"


def test_plan_needs_confirmations_and_no_blocks(client):
    sign_in(client)
    client.put("/api/firewall/draft", json={"moves": {"d10": "n_work"}}, headers=HDR)
    r = client.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR)
    assert r.status_code == 409 and r.json()["error"] == "fw.block.confirm"
    assert client.post("/api/firewall/plan", json={"confirmed": ["private_address:d10"]}, headers=HDR).status_code == 200
    client.put("/api/firewall/draft", json={"moves": {"d09": "n_work"}}, headers=HDR)
    r = client.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR)
    assert r.json()["error"] == "fw.block.shared_port"
    r = client.post("/api/firewall/approve", json={"plan": "p_unknown"}, headers=HDR)
    assert r.json()["error"] == "fw.block.expired"


def test_external_mode_needs_a_passkey_signature_and_hands_plans_to_the_executor(tmp_path, monkeypatch):
    token = "t" * 40
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_FW_EXECUTOR", "external")
    monkeypatch.setenv("LANLOOM_EXECUTOR_TOKEN", token)
    app = create_app(config_mod.load())
    auth = dict(HDR, Authorization="Bearer " + token)
    with TestClient(app, base_url=ORIGIN) as c:
        c.data_dir = tmp_path
        sign_in(c)
        assert c.get("/api/firewall").json()["state"] is None
        assert c.post("/api/executor/state", json={"state": state(), "executor": {"target": "controller", "armed": True}}, headers=auth).status_code == 200
        v = c.get("/api/firewall").json()
        assert v["executor"]["connected"] and v["executor"]["armed"] and v["state"]["devices"]
        key = Key()
        user = app.state.ll.store.one("SELECT id FROM users")
        app.state.ll.store.execute("INSERT INTO passkeys(user_id, credential_id, public_key, sign_count, name, created) VALUES(?,?,?,?,?,?)",
                                   (user["id"], key.cid, key.cose(), 0, "Test key", time.time()))
        assert c.get("/api/firewall/keys").json()["keys"][0]["x"] == key.x.hex()
        c.put("/api/firewall/draft", json={"moves": {"d19": "n_cams"}}, headers=HDR)
        p = c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).json()
        assert p["plan"]["target"] == "controller" and p["sign"]["allow"] == [signature.b64u(key.cid)]
        challenge = signature.b64u_dec(p["sign"]["challenge"])
        assert challenge == canon.digest(p["plan"])
        r = c.post("/api/firewall/approve", json={"plan": p["plan"]["id"], "assertion": key.sign(os.urandom(32))}, headers=HDR)
        assert r.status_code == 409 and r.json()["error"] == "fw.block.signature"
        a = key.sign(challenge)
        assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"], "assertion": a}, headers=HDR).json()["ok"]
        plans = c.get("/api/executor/plans?target=controller", headers=auth).json()["plans"]
        assert len(plans) == 1 and plans[0]["assertion"] == a
        assert c.get("/api/executor/plans?target=controller", headers=auth).json()["plans"] == []
        # the executor verifies again with its own pinned key and reports back
        r, _ = make_runner(tmp_path / "ex", adapter.SimulatedController(settle_s=0), target="controller",
                           require_signature=True, keys=[key.pinned()], origins=[ORIGIN])
        r.report = lambda ev: c.post("/api/executor/report", json=ev, headers=auth)
        assert r.run(plans[0]["plan"], plans[0]["assertion"])["status"] == "done"
        assert c.get("/api/firewall").json()["history"][0]["status"] == "done"
