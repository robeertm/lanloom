"""The write half of the Proxmox VE family (stage B, guests) against the stateful fake, alone and inside the executor
pipeline: the allow-list below the adapter, every step kind end to end, idempotence, digest conflicts, tasks, pending
changes, partial failures and the way back, crash recovery, lockout rules, the account check and secrets."""
import ast
import json
import logging
from pathlib import Path

import pytest

from lanloom.firewall import model as fwmodel
from lanloom.firewall import steps as fwsteps
from lanloom.firewall.runner import Runner, write_private
from lanloom.integrations import transport
from lanloom.integrations.proxmox import model
from lanloom.integrations.proxmox import write as wmod
from lanloom.integrations.proxmox.write import ProxmoxWriter, WriteError

from . import fake_writable as fw
from .fake_writable import FakeWritable, SECRET, TOKEN_ID

DIGEST = "ab" * 20
UPID = "UPID%3Anode-a%3A0000ABCD%3A00001234%3A6553F00D%3Aqmstart%3A101%3Alanloom%40pve%21write%3A"
NIC0_101 = "virtio=02:4C:4C:00:0A:01,bridge=vmbr0,firewall=1,tag=20"


class Clock:
    """A fake clock: ``sleep`` only moves it on, so waiting loops run at once."""

    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture
def fake():
    f = FakeWritable()
    yield f
    f.stop()


@pytest.fixture
def clk():
    return Clock()


def writer(fake, clk, **kw):
    kw.setdefault("settle_s", 0)
    return ProxmoxWriter(url=fake.url, write_token_id=TOKEN_ID, write_token_secret=SECRET, clock=clk.time, sleep=clk.sleep, **kw)


def gid(key):
    return model.oid("g", key)


def nic_id(key, slot="net0"):
    return model.oid("nic", "%s/%s" % (gid(key), slot))


def rid(key, pos):
    return model.rule_id("guest", gid(key), pos)


# ---- plan steps
def nic_step(key, before, after, slot="net0"):
    return {"kind": "guest.nic", "object": nic_id(key, slot), "before": before, "after": after}


def power_step(key, before, after):
    return {"kind": "guest.power", "object": gid(key), "before": {"status": before}, "after": {"status": after}}


def fw_step(key, before, after):
    return {"kind": "guest.firewall", "object": gid(key), "before": {"firewall": before}, "after": {"firewall": after}}


def rule_step(key, pos, before, after):
    return {"kind": "guest.fw_rule", "object": rid(key, pos), "before": before, "after": after}


def create_step(key, **after):
    after = dict({"direction": "in", "action": "allow", "enabled": True}, **after, guest=gid(key))
    return {"kind": "guest.fw_rule_create", "object": "new:" + "0a1b2c3d4e5f", "before": {}, "after": after}


def delete_step(key, pos, before):
    return {"kind": "guest.fw_rule_delete", "object": rid(key, pos), "before": dict(before, guest=gid(key)), "after": {}}


OWN_RULE = {"name": "test", "direction": "in", "action": "block", "proto": "tcp", "ports": "8080", "source": "192.168.50.0/24",
            "enabled": True}
FOREIGN_RULE = {"name": "https", "direction": "in", "action": "allow", "proto": "tcp", "ports": "443", "source": "", "enabled": True}

_nonce = iter(range(1, 10_000))


def make_plan(w, clk, steps, confirmed=()):
    st = w.read()
    now = int(clk.now)
    conf = set(confirmed) | {fwsteps.confirm_token(s) for s in steps if fwsteps.needs_confirmation(s)}
    return {"v": 2, "id": "p_%d_%04d" % (now, next(_nonce)), "target": "hypervisor", "base": fwmodel.fingerprint(st), "created": now,
            "expires": now + 300, "nonce": "%032x" % next(_nonce), "requester": {"user": "admin", "device": None},
            "steps": steps, "warnings": [], "confirmed": sorted(conf), "costs": {"objects": len(steps)}, "undo_of": None}


def make_runner(w, clk, tmp_path, **kw):
    kw.setdefault("watch_s", 0)
    kw.setdefault("check_s", 0)
    return Runner(w, tmp_path / "ex", target="hypervisor", require_signature=False, clock=clk.time, sleep=clk.sleep, **kw)


def run(fake, clk, tmp_path, steps, *, w=None, confirmed=(), runner_kw=None, **wkw):
    w = w or writer(fake, clk, **wkw)
    plan = make_plan(w, clk, steps, confirmed)
    r = make_runner(w, clk, tmp_path, **(runner_kw or {}))
    return r.run(plan), w, r


def refused(res, reason):
    assert res["status"] == "refused" and res["reason"] == reason, res


# ---------------------------------------------------------------------------- the allow-list below the adapter
G = "/api2/json/nodes/node-a/qemu/101"


@pytest.mark.parametrize("method,path,body", [
    ("PUT", G + "/config", {"net0": "virtio=02:4C:4C:00:0A:01,bridge=vmbr0,tag=30", "digest": DIGEST}),
    ("PUT", G + "/config", {"net1": "e1000=02:4C:4C:00:0A:11,bridge=vmbr1"}),
    ("PUT", "/api2/json/nodes/node-b/lxc/201/config", {"net0": "name=eth0,bridge=vmbr0,hwaddr=02:4C:4C:00:12:01,ip=dhcp,type=veth"}),
    ("PUT", G + "/config", {"revert": "net0", "digest": DIGEST}),
    ("POST", G + "/status/start", {}),
    ("POST", G + "/status/shutdown", {"timeout": 60, "forceStop": 0}),
    ("PUT", G + "/firewall/options", {"enable": 1, "digest": DIGEST}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "enable": 1, "proto": "tcp", "dport": "80,443", "source": "192.168.50.0/24",
                                     "comment": "lanloom: x", "pos": 3, "digest": DIGEST}),
    ("POST", G + "/firewall/rules", {"type": "out", "action": "REJECT", "enable": 0, "comment": "lanloom:"}),
    ("PUT", G + "/firewall/rules/1", {"enable": 0, "digest": DIGEST}),
    ("PUT", G + "/firewall/rules/1", {"dport": "22", "delete": "source,proto", "comment": "any text"}),
    ("DELETE", G + "/firewall/rules/1", None),
    ("DELETE", G + "/firewall/rules/1?digest=" + DIGEST, None),
    ("GET", G + "/config?current=1", None),
    ("GET", G + "/pending", None),
    ("GET", G + "/status/current", None),
    ("GET", G + "/firewall/rules", None),
    ("GET", "/api2/json/nodes/node-a/tasks/" + UPID + "/status", None),
    ("GET", "/api2/json/access/permissions", None),
])
def test_allow_list_lets_the_planned_writes_and_reads_through(method, path, body):
    assert wmod.ALLOW.check(method, path, body).method == method


@pytest.mark.parametrize("method,path,body", [
    # config: card values only
    ("PUT", G + "/config", {"memory": "99999"}),
    ("PUT", G + "/config", {"net0": "virtio=02:4C:4C:00:0A:01,bridge=vmbr0", "memory": "1"}),
    ("PUT", G + "/config", {"net0": "virtio=02:4C:4C:00:0A:01,bridge=vmbr0\nmemory=1"}),
    ("PUT", G + "/config", {"net0": "virtio=02:4C:4C:00:0A:01,bridge=vmbr0 tag=1"}),
    ("PUT", G + "/config", {"net0": "bogus=1,bridge=vmbr0"}),
    ("PUT", G + "/config", {"net32": "virtio=02:4C:4C:00:0A:01,bridge=vmbr0"}),
    ("PUT", G + "/config", {"net0": "virtio=02:4C:4C:00:0A:01", "digest": "not-hex"}),
    ("PUT", G + "/config", {"revert": "memory"}),
    ("PUT", G + "/config", {"delete": "net0"}),
    ("PUT", G + "/config", {}),
    ("PUT", G + "/config", None),
    ("PUT", G + "/config?x=1", {"net0": "virtio=02:4C:4C:00:0A:01,bridge=vmbr0"}),
    ("GET", G + "/config", None),
    # power: start, and shutdown that is never forced
    ("POST", G + "/status/stop", {}),
    ("POST", G + "/status/reset", {}),
    ("POST", G + "/status/suspend", {}),
    ("POST", G + "/status/start", {"timeout": 60}),
    ("POST", G + "/status/shutdown", {"timeout": 60, "forceStop": 1}),
    ("POST", G + "/status/shutdown", {"timeout": 60, "forceStop": True}),
    ("POST", G + "/status/shutdown", {"timeout": 5, "forceStop": 0}),
    ("POST", G + "/status/shutdown", {"timeout": 9000, "forceStop": 0}),
    ("POST", G + "/status/shutdown", {"timeout": 60, "skiplock": 1}),
    # firewall: the switch only, rules with plain fields and the mark
    ("PUT", G + "/firewall/options", {"policy_in": "ACCEPT"}),
    ("PUT", G + "/firewall/options", {"enable": 2}),
    ("PUT", G + "/firewall/options", {"enable": True}),
    ("PUT", G + "/firewall/options", {}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": "no mark"}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP"}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": ""}),
    ("POST", G + "/firewall/rules", {}),
    ("POST", G + "/firewall/rules", {"type": "group", "action": "DROP", "comment": "lanloom: x"}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "ACCEPT", "macro": "SSH", "comment": "lanloom: x"}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": "lanloom: x\nINJECT"}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": "lanloom: " + "x" * 100}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": "lanloom: x", "dport": "80;rm"}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": "lanloom: x", "source": "evil host"}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": "lanloom: x", "dport": "99999"}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": "lanloom: x", "enable": True}),
    ("POST", G + "/firewall/rules", {"type": "in", "action": "DROP", "comment": "lanloom: x", "pos": -1}),
    ("PUT", G + "/firewall/rules/1", {"pos": 0, "enable": 1}),
    ("PUT", G + "/firewall/rules/1", {"delete": "type"}),
    ("PUT", G + "/firewall/rules/1", {"delete": "action,comment"}),
    ("PUT", G + "/firewall/rules/1", {"macro": "SSH"}),
    ("PUT", G + "/firewall/rules/1", {}),
    ("PUT", G + "/firewall/rules/x", {"enable": 1}),
    ("DELETE", G + "/firewall/rules/1?digest=zz", None),
    ("DELETE", G + "/firewall/rules/1", {"digest": DIGEST}),
    # nothing else on the host
    ("DELETE", "/api2/json/nodes/node-a/firewall/rules/0", None),
    ("POST", "/api2/json/nodes/node-a/firewall/rules", {"type": "in", "action": "DROP", "comment": "lanloom: x"}),
    ("PUT", "/api2/json/nodes/node-a/firewall/options", {"enable": 0}),
    ("DELETE", "/api2/json/cluster/firewall/rules/0", None),
    ("PUT", "/api2/json/cluster/firewall/options", {"enable": 0}),
    ("POST", "/api2/json/nodes/x/network", {"iface": "vmbr9", "type": "bridge"}),
    ("PUT", "/api2/json/nodes/x/network", {}),
    ("DELETE", "/api2/json/nodes/x/network/vmbr0", None),
    ("POST", "/api2/json/nodes/node-a/qemu", {"vmid": 500}),
    ("DELETE", G, None),
    ("POST", G + "/snapshot", {"snapname": "x"}),
    ("POST", G + "/migrate", {"target": "node-b"}),
    ("POST", G + "/clone", {"newid": 500}),
    ("PUT", G + "/resize", {"disk": "scsi0", "size": "+1G"}),
    ("POST", "/api2/json/nodes/node-a/qemu/101/../102/status/stop", {}),
    ("PUT", "/api2/json/access/users/root@pam", {"enable": 0}),
    ("POST", "/api2/json/access/ticket", {"username": "a", "password": "b"}),
    ("POST", "/api2/json/nodes/node-a/status", {"command": "shutdown"}),
    ("GET", "/api2/json/nodes/node-a/tasks/UPID:node-a:0:0:0:x:1:u@pam!t:/status", None),
    ("GET", "/api2/json/storage", None),
    ("PATCH", G + "/config", {"net0": "virtio=02:4C:4C:00:0A:01,bridge=vmbr0"}),
    ("GET", G + "/config?current=1", {"x": 1}),
])
def test_allow_list_refuses_everything_else(method, path, body):
    with pytest.raises(transport.Forbidden):
        wmod.ALLOW.check(method, path, body)


def test_the_module_has_exactly_one_send_path():
    """No network module is imported: every byte goes through ``transport.Transport``."""
    path = Path(wmod.__file__)
    mods = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            mods.add(node.module)
    assert not mods & {"socket", "http", "http.client", "ssl", "urllib.request", "urllib3", "requests", "subprocess", "asyncio",
                       "ftplib", "smtplib", "os", "shutil"}, mods
    assert "urllib.parse" in mods
    text = path.read_text(encoding="utf-8")
    for word in ("HTTPConnection", "HTTPSConnection", "urlopen", "create_connection", "eval(", "exec(", "__import__"):
        assert word not in text, word


def test_a_refused_request_never_leaves_the_process(fake, clk):
    w = writer(fake, clk)
    w.read()
    n = len(fake.requests)
    for method, rel, body in (("PUT", "/nodes/node-a/qemu/101/config", {"memory": "1"}),
                              ("POST", "/nodes/node-a/qemu/101/status/stop", {}),
                              ("POST", "/nodes/node-a/qemu/101/status/shutdown", {"timeout": 60, "forceStop": 1}),
                              ("DELETE", "/nodes/node-a/firewall/rules/0", None)):
        with pytest.raises(WriteError) as exc:
            w._send(method, rel, body)
        assert exc.value.key == "proxmox.refused_by_allow_list"
    assert len(fake.requests) == n and w._t.refused == 4 and not fake.writes


def test_writes_are_form_encoded_and_carry_only_the_write_token(fake, clk, tmp_path):
    res, w, _ = run(fake, clk, tmp_path, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])
    assert res["status"] == "done"
    assert fake.requests and all(r["headers"]["authorization"] == "PVEAPIToken=%s=%s" % (TOKEN_ID, SECRET) for r in fake.requests)
    assert all(r["headers"]["content-type"] == "application/x-www-form-urlencoded" for r in fake.writes)
    assert fake.methods() <= {"GET", "PUT"} and w.endpoints() == ["127.0.0.1"]


def test_options_unknown_and_missing_values_are_refused(fake):
    base = dict(url=fake.url, write_token_id=TOKEN_ID, write_token_secret=SECRET)
    with pytest.raises(ValueError, match="unknown option"):
        ProxmoxWriter(**base, tokn="typo")
    for bad in (dict(base, url="ftp://x"), dict(base, url=None), dict(base, write_token_id="nobang"),
                dict(base, write_token_secret=None), dict(base, write_token_secret="short"), dict(base, protect_addresses=["not-an-ip"])):
        with pytest.raises(ValueError):
            ProxmoxWriter(**bad)
    caps = ProxmoxWriter(**base).capabilities()
    assert set(caps["write"]) == {"guest.nic", "guest.fw_rule", "guest.fw_rule_create", "guest.fw_rule_delete", "guest.firewall",
                                  "guest.power"}


def test_unknown_step_kinds_are_not_supported(fake, clk):
    w = writer(fake, clk)
    st = w.read()
    assert w.lockout(st, {"kind": "move", "device": "x", "after": {}}) == "fw.block.capability"
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "host.bridge", "object": "x", "before": {}, "after": {}})
    assert exc.value.key == "proxmox.kind_not_supported" and not fake.writes


# ---------------------------------------------------------------------------- reading
def test_read_gives_the_neutral_state_and_never_writes(fake, clk):
    w = writer(fake, clk)
    st = w.read()
    assert st["controller"]["kind"] == "hypervisor" and st["side_effects"] == {}
    assert {g["vmid"] for g in st["guests"]} == {101, 102, 103, 104, 105, 106, 107, 108, 201}
    assert fake.methods() == {"GET"}
    assert w.health() == (True, "")
    assert all(r["path"].startswith("/api2/json/") for r in fake.requests)


# ---------------------------------------------------------------------------- guest.nic
def test_nic_vlan_change_keeps_every_other_key_and_sends_the_digest(fake, clk, tmp_path):
    d0 = fake.digest("qemu/101")
    other = fake.net("qemu/101", "net1")
    res, w, _ = run(fake, clk, tmp_path, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])
    assert res["status"] == "done" and res["reason"] == "fw.res.done"
    assert fake.net("qemu/101") == "virtio=02:4C:4C:00:0A:01,bridge=vmbr0,firewall=1,tag=30"
    assert fake.net("qemu/101", "net1") == other and fake.cfg["qemu/101"]["memory"] == "4096"
    puts = [r for r in fake.writes if r["method"] == "PUT"]
    assert len(puts) == 1 and puts[0]["path"].endswith("/qemu/101/config")
    assert puts[0]["body"] == {"net0": fake.net("qemu/101"), "digest": d0} and fake.digest("qemu/101") != d0
    assert w.writes == [("PUT", "/<qemu>/config")]
    assert [h["status"] for h in make_runner(w, clk, tmp_path).history()] == ["done"]


def test_nic_vlan_removed_and_bridge_changed_to_a_virtual_network(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [nic_step("qemu/101", {"bridge": "vmbr0", "vlan": 20}, {"bridge": "cams", "vlan": None})])
    assert res["status"] == "done"
    assert fake.net("qemu/101") == "virtio=02:4C:4C:00:0A:01,bridge=cams,firewall=1"


def test_nic_vlan_added_to_a_card_without_tag_goes_right_after_the_bridge(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [nic_step("qemu/101", {"vlan": None}, {"vlan": 40}, slot="net1")])
    assert res["status"] == "done"                      # vmbr1 has no VLAN list of its own to hold the tag back
    assert fake.net("qemu/101", "net1") == "e1000=02:4C:4C:00:0A:11,bridge=vmbr1,tag=40"


def test_nic_vlan_added_on_a_vlan_aware_bridge(fake, clk, tmp_path):
    fake.cfg["qemu/102"]["net0"] = "virtio=02:4C:4C:00:0B:01,bridge=vmbr0"
    res, _, _ = run(fake, clk, tmp_path, [nic_step("qemu/102", {"vlan": None}, {"vlan": 40})])
    assert res["status"] == "done" and fake.net("qemu/102") == "virtio=02:4C:4C:00:0B:01,bridge=vmbr0,tag=40"


def test_nic_firewall_flag_on_and_off(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [nic_step("qemu/108", {"firewall": False}, {"firewall": True})])
    assert res["status"] == "done" and fake.net("qemu/108") == "virtio=02:4C:4C:00:11:01,bridge=vmbr0,firewall=1,tag=20"
    res, _, _ = run(fake, clk, tmp_path / "b", [nic_step("qemu/101", {"firewall": True}, {"firewall": False})])
    assert res["status"] == "done" and fake.net("qemu/101") == "virtio=02:4C:4C:00:0A:01,bridge=vmbr0,tag=20"


def test_a_container_card_keeps_its_address_and_type(fake, clk, tmp_path):
    before = fake.net("lxc/201")
    res, _, _ = run(fake, clk, tmp_path, [nic_step("lxc/201", {"firewall": False}, {"firewall": True})])
    assert res["status"] == "done"
    assert fake.net("lxc/201") == before.replace("bridge=vmbr0", "bridge=vmbr0,firewall=1")


def test_nic_step_is_idempotent(fake, clk):
    w = writer(fake, clk)
    w.read()
    step = nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})
    w.apply(step)
    assert len(fake.writes) == 1
    w.apply(step)                                         # already as planned: nothing written
    assert len(fake.writes) == 1 and "tag=30" in fake.net("qemu/101")


def test_nic_before_mismatch_is_refused_below_the_adapter(fake, clk):
    w = writer(fake, clk)
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply(nic_step("qemu/101", {"vlan": 99}, {"vlan": 30}))
    assert exc.value.key == "proxmox.changed" and not fake.writes


def test_nic_on_a_locked_guest_is_refused_below_the_adapter(fake, clk):
    w = writer(fake, clk)
    w.read()
    fake.cfg["qemu/101"]["lock"] = "backup"
    with pytest.raises(WriteError) as exc:
        w.apply(nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}))
    assert exc.value.key == "proxmox.locked" and not fake.writes


def test_an_unknown_object_is_refused_below_the_adapter(fake, clk):
    w = writer(fake, clk)
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply(nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}, slot="net7"))
    assert exc.value.key == "proxmox.object_unknown" and not fake.writes


def test_a_write_that_did_not_take_is_an_error(fake, clk):
    w = writer(fake, clk)
    w.read()
    fake.pending_slots = set()
    orig = fake._put_config

    def ignore(key, body):
        fake.cfg_ver[key] += 1
        return 200, None
    fake._put_config = ignore
    with pytest.raises(WriteError) as exc:
        w.apply(nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}))
    fake._put_config = orig
    assert exc.value.key == "proxmox.not_applied"


# ---------------------------------------------------------------------------- guest.firewall
def test_guest_firewall_on_and_off(fake, clk, tmp_path):
    d0 = fake.fw_digest("qemu/108")
    res, _, _ = run(fake, clk, tmp_path, [fw_step("qemu/108", False, True)])
    assert res["status"] == "done" and fake.fw_opts["qemu/108"]["enable"] == 1
    put = fake.writes[0]
    assert put["method"] == "PUT" and put["path"].endswith("/qemu/108/firewall/options") and put["body"] == {"enable": "1", "digest": d0}
    res, _, _ = run(fake, clk, tmp_path / "b", [fw_step("qemu/101", True, False)])
    assert res["status"] == "done" and fake.fw_opts["qemu/101"]["enable"] == 0


def test_guest_firewall_is_idempotent_and_checks_before(fake, clk):
    w = writer(fake, clk)
    w.read()
    w.apply(fw_step("qemu/108", False, True))
    w.apply(fw_step("qemu/108", False, True))
    assert len(fake.writes) == 1
    with pytest.raises(WriteError) as exc:
        w.apply(fw_step("qemu/108", False, False))
    assert exc.value.key == "proxmox.changed" and len(fake.writes) == 1


# ---------------------------------------------------------------------------- guest.fw_rule
def test_rule_disable_and_enable(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [rule_step("qemu/101", 0, {"enabled": True}, {"enabled": False})])
    assert res["status"] == "done" and fake.rule_list("qemu/101")[0]["enable"] == 0
    assert fake.rule_list("qemu/101")[0]["comment"] == "https"
    body = fake.writes[0]["body"]
    assert fake.writes[0]["path"].endswith("/qemu/101/firewall/rules/0") and set(body) == {"enable", "digest"} and body["enable"] == "0"
    res, _, _ = run(fake, clk, tmp_path / "b", [rule_step("qemu/101", 0, {"enabled": False}, {"enabled": True})])
    assert res["status"] == "done" and fake.rule_list("qemu/101")[0]["enable"] == 1


def test_rule_of_a_complex_kind_may_only_be_switched(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [rule_step("qemu/101", 2, {"enabled": True}, {"enabled": False})])
    assert res["status"] == "done" and fake.rule_list("qemu/101")[2]["enable"] == 0 and fake.rule_list("qemu/101")[2]["macro"] == "SSH"
    res, _, _ = run(fake, clk, tmp_path / "b", [rule_step("qemu/101", 2, {"action": "allow"}, {"action": "block"})])
    refused(res, "fw.block.hv_rule_complex")


def test_rule_field_change_and_clearing_fields(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [rule_step("qemu/101", 1, {"ports": "8080", "source": "192.168.50.0/24"},
                                                    {"ports": "9090", "source": ""})])
    assert res["status"] == "done"
    rule = fake.rule_list("qemu/101")[1]
    assert rule["dport"] == "9090" and "source" not in rule and rule["comment"] == "lanloom: test" and rule["proto"] == "tcp"
    body = fake.writes[0]["body"]
    assert body["dport"] == "9090" and body["delete"] == "source" and "source" not in body
    # clearing ports and the protocol together
    res, _, _ = run(fake, clk, tmp_path / "b", [rule_step("qemu/101", 1, {"ports": "9090", "proto": "tcp"}, {"ports": "", "proto": "any"})])
    assert res["status"] == "done"
    rule = fake.rule_list("qemu/101")[1]
    assert "dport" not in rule and "proto" not in rule
    assert fake.writes[-1]["body"]["delete"] == "dport,proto"


def test_rule_action_direction_source_and_name_of_an_own_rule(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [rule_step("qemu/101", 1, {"action": "block", "direction": "in", "source": "192.168.50.0/24",
                                                                    "name": "test", "proto": "tcp"},
                                                    {"action": "reject", "direction": "out", "source": "+office", "name": "renamed",
                                                     "proto": "udp"})])
    assert res["status"] == "done"
    rule = fake.rule_list("qemu/101")[1]
    assert (rule["action"], rule["type"], rule["source"], rule["proto"]) == ("REJECT", "out", "+office", "udp")
    assert rule["comment"] == "lanloom: renamed"             # the mark stays on a rule of ours


def test_rule_name_of_a_foreign_rule_is_written_as_is(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [rule_step("qemu/101", 0, {"name": "https"}, {"name": "web"})])
    assert res["status"] == "done" and fake.rule_list("qemu/101")[0]["comment"] == "web"


def test_rule_ports_on_a_protocol_without_ports_are_refused(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [rule_step("qemu/101", 1, {"proto": "tcp"}, {"proto": "icmp"})])
    refused(res, "fw.block.hv_rule_ports")
    assert not fake.writes


def test_rule_step_is_idempotent_and_checks_before(fake, clk):
    w = writer(fake, clk)
    w.read()
    step = rule_step("qemu/101", 0, {"enabled": True}, {"enabled": False})
    w.apply(step)
    w.apply(step)
    assert len(fake.writes) == 1
    with pytest.raises(WriteError) as exc:
        w.apply(rule_step("qemu/101", 0, {"enabled": True}, {"ports": "1"}))
    assert exc.value.key == "proxmox.changed" and len(fake.writes) == 1


def test_rule_that_vanished_is_an_unknown_object(fake, clk):
    w = writer(fake, clk)
    w.read()
    fake.rules["qemu/101"].pop()
    fake.rules["qemu/101"].pop()
    with pytest.raises(WriteError) as exc:
        w.apply(rule_step("qemu/101", 1, {"enabled": True}, {"enabled": False}))
    assert exc.value.key == "proxmox.object_unknown"


# ---------------------------------------------------------------------------- guest.fw_rule_create / delete
def test_rule_create_is_appended_marked_and_returns_its_id(fake, clk, tmp_path):
    d0 = fake.fw_digest("qemu/101")
    step = create_step("qemu/101", proto="tcp", ports="22", source="192.168.50.5", name="ssh admin")
    res, w, _ = run(fake, clk, tmp_path, [step])
    assert res["status"] == "done"
    rules = fake.rule_list("qemu/101")
    assert len(rules) == 4 and rules[3] == {"type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": "22",
                                           "source": "192.168.50.5", "comment": "lanloom: ssh admin"}
    post = fake.writes[0]
    assert post["method"] == "POST" and post["path"].endswith("/qemu/101/firewall/rules")
    assert post["body"]["pos"] == "3" and post["body"]["digest"] == d0 and post["body"]["comment"].startswith("lanloom:")
    assert w.writes == [("POST", "/<qemu>/firewall/rules")]
    # the plan's way back removes it under the id the host gave it
    assert res["inverse"][0]["kind"] == "guest.fw_rule_delete" and res["inverse"][0]["object"] == rid("qemu/101", 3)


def test_rule_create_on_a_guest_without_rules_and_with_minimum_fields(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [create_step("qemu/102", action="block", enabled=False)])
    assert res["status"] == "done"
    assert fake.rule_list("qemu/102") == [{"type": "in", "action": "DROP", "enable": 0, "comment": "lanloom:"}]
    assert fake.writes[0]["body"]["pos"] == "0"


def test_rule_create_returns_the_id_of_the_new_rule(fake, clk):
    w = writer(fake, clk)
    w.read()
    out = w.apply(create_step("qemu/101", proto="tcp", ports="22", name="x"))
    assert out == {"id": rid("qemu/101", 3)}


def test_delete_own_rule_with_digest(fake, clk, tmp_path):
    d0 = fake.fw_digest("qemu/101")
    res, w, _ = run(fake, clk, tmp_path, [delete_step("qemu/101", 1, OWN_RULE)])
    assert res["status"] == "done"
    assert [r.get("comment") for r in fake.rule_list("qemu/101")] == ["https", None]
    d = fake.writes[0]
    assert d["method"] == "DELETE" and d["path"].endswith("/qemu/101/firewall/rules/1") and d["query"] == "digest=" + d0
    assert w.writes == [("DELETE", "/<qemu>/firewall/rules/1")]


def test_delete_of_a_foreign_rule_is_refused_by_the_lockout_and_below_it(fake, clk, tmp_path):
    res, w, _ = run(fake, clk, tmp_path, [delete_step("qemu/101", 0, FOREIGN_RULE)])
    refused(res, "fw.block.not_own")
    assert not fake.writes
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply(delete_step("qemu/101", 0, FOREIGN_RULE))
    assert exc.value.key == "proxmox.not_own" and not fake.writes and len(fake.rule_list("qemu/101")) == 3


def test_delete_is_idempotent_when_the_rule_is_gone(fake, clk):
    w = writer(fake, clk)
    w.read()
    step = delete_step("qemu/101", 1, OWN_RULE)
    w.apply(step)
    # another rule is at that place now: it is not ours, so it is never removed by position
    with pytest.raises(WriteError) as exc:
        w.apply(step)
    assert exc.value.key == "proxmox.not_own" and len(fake.writes) == 1
    fake.rules["qemu/101"].clear()
    w.apply(step)                                          # nothing there any more
    assert len(fake.writes) == 1


def test_delete_checks_the_rule_against_before(fake, clk):
    w = writer(fake, clk)
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply(delete_step("qemu/101", 1, dict(OWN_RULE, ports="1234")))
    assert exc.value.key == "proxmox.changed" and not fake.writes


# ---------------------------------------------------------------------------- guest.power
def test_start_follows_the_task_to_its_end(fake, clk, tmp_path):
    fake.task_mode, fake.task_polls = "slow", 3
    res, w, _ = run(fake, clk, tmp_path, [power_step("qemu/102", "stopped", "running")])
    assert res["status"] == "done" and fake.status("qemu/102") == "running"
    post = fake.writes[0]
    assert post["method"] == "POST" and post["path"].endswith("/qemu/102/status/start") and post["body"] == {}
    assert fake.count(r"/tasks/UPID:node-a:0000ABCD:00001234:6553F00D:qmstart:102:lanloom@pve!write:/status", "GET") == 4
    assert w.writes == [("POST", "/<qemu>/status/start")]


def test_shutdown_is_clean_and_never_forced(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [power_step("qemu/101", "running", "stopped")], shutdown_timeout_s=90)
    assert res["status"] == "done" and fake.status("qemu/101") == "stopped"
    assert fake.writes[0]["path"].endswith("/qemu/101/status/shutdown")
    assert fake.writes[0]["body"] == {"timeout": "90", "forceStop": "0"}
    assert not any(w["path"].endswith("/status/stop") for w in fake.writes)


def test_start_and_shutdown_of_a_container(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [power_step("lxc/201", "running", "stopped")])
    assert res["status"] == "done" and fake.status("lxc/201") == "stopped" and fake.writes[0]["path"].endswith("/lxc/201/status/shutdown")
    res, _, _ = run(fake, clk, tmp_path / "b", [power_step("lxc/201", "stopped", "running")])
    assert res["status"] == "done" and fake.status("lxc/201") == "running"


def test_power_is_idempotent_and_checks_before(fake, clk):
    w = writer(fake, clk)
    w.read()
    w.apply(power_step("qemu/102", "stopped", "running"))
    w.apply(power_step("qemu/102", "stopped", "running"))
    assert len(fake.writes) == 1
    with pytest.raises(WriteError) as exc:
        w.apply(power_step("qemu/101", "stopped", "stopped"))
    assert exc.value.key == "proxmox.changed"
    assert len(fake.writes) == 1


def test_power_on_a_ha_managed_or_locked_guest_is_refused_below_the_adapter(fake, clk):
    w = writer(fake, clk)
    w.read()
    fake.res["qemu/102"]["hastate"] = "started"
    with pytest.raises(WriteError) as exc:
        w.apply(power_step("qemu/102", "stopped", "running"))
    assert exc.value.key == "proxmox.ha_managed"
    del fake.res["qemu/102"]["hastate"]
    fake.res["qemu/102"]["lock"] = "backup"
    with pytest.raises(WriteError) as exc:
        w.apply(power_step("qemu/102", "stopped", "running"))
    assert exc.value.key == "proxmox.locked" and not fake.writes


def test_a_failed_task_stops_the_plan_and_rolls_back(fake, clk, tmp_path):
    fake.task_mode = "failed"
    res, _, _ = run(fake, clk, tmp_path, [nic_step("qemu/108", {"vlan": 20}, {"vlan": 30}), power_step("qemu/102", "stopped", "running")])
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert fake.status("qemu/102") == "stopped" and fake.net("qemu/108").endswith("tag=20")


def test_a_task_that_never_ends_times_out_and_rolls_back(fake, clk, tmp_path):
    fake.task_mode = "never"
    t0 = clk.now
    res, w, _ = run(fake, clk, tmp_path, [power_step("qemu/101", "running", "stopped")], shutdown_timeout_s=10, task_timeout_s=20)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert fake.status("qemu/101") == "running" and 20 <= clk.now - t0 < 60
    assert [x for x in fake.writes if x["path"].endswith("/status/shutdown")]


def test_task_errors_carry_the_host_message_only_in_the_detail(fake, clk):
    w = writer(fake, clk)
    w.read()
    fake.task_mode = "failed"
    with pytest.raises(WriteError) as exc:
        w.apply(power_step("qemu/102", "stopped", "running"))
    assert exc.value.key == "proxmox.task_failed" and "exit code" in exc.value.detail and str(exc.value) == "proxmox.task_failed"


def test_task_timeout_key(fake, clk):
    fake.task_mode = "never"
    w = writer(fake, clk, shutdown_timeout_s=10, task_timeout_s=10)
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply(power_step("qemu/101", "running", "stopped"))
    assert exc.value.key == "proxmox.task_timeout"


def test_a_task_that_is_ok_but_changed_nothing_is_not_applied(fake, clk):
    fake.task_mode = "noeffect"
    w = writer(fake, clk)
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply(power_step("qemu/101", "running", "stopped"))
    assert exc.value.key == "proxmox.not_applied"


def test_an_unusable_task_id_is_refused(fake, clk):
    w = writer(fake, clk)
    for bad in (None, 5, "", "UPID:x", "UPID:node-a:0000ABCD:00001234:6553F00D:qmstart:101:../../x:"):
        with pytest.raises(WriteError) as exc:
            w._wait_task("node-a", bad)
        assert exc.value.key == "proxmox.task_unknown"
    assert not fake.requests


# ---------------------------------------------------------------------------- conflicts, partial failures, the way back
def test_before_mismatch_at_the_gate_is_refused_as_conflict(fake, clk, tmp_path):
    step = nic_step("qemu/101", {"vlan": 99}, {"vlan": 30})
    res, _, _ = run(fake, clk, tmp_path, [step])
    refused(res, "fw.block.conflict")
    assert not fake.writes


def test_the_host_changing_between_plan_and_write_is_a_conflict(fake, clk, tmp_path):
    w = writer(fake, clk)
    plan = make_plan(w, clk, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])
    fake.external_config_change("qemu/101", net0="virtio=02:4C:4C:00:0A:01,bridge=vmbr0,firewall=1,tag=77")
    res = make_runner(w, clk, tmp_path).run(plan)
    refused(res, "fw.block.conflict")
    assert not fake.writes


def test_a_change_between_the_checks_and_the_write_fails_on_the_digest_and_rolls_back(fake, clk, tmp_path):
    fake.on_write = lambda: fake.external_config_change("qemu/101", memory="8192")      # someone else, a moment before our write
    res, _, _ = run(fake, clk, tmp_path, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert fake.net("qemu/101") == NIC0_101 and fake.cfg["qemu/101"]["memory"] == "8192"      # their change is left alone
    assert [w["method"] for w in fake.writes] == ["PUT"]


def test_a_digest_conflict_on_a_rule_list_is_refused(fake, clk, tmp_path):
    fake.on_write = lambda: fake.external_rule_change("qemu/101")
    res, w, _ = run(fake, clk, tmp_path, [rule_step("qemu/101", 0, {"enabled": True}, {"enabled": False})])
    assert res["status"] == "rolled_back" and fake.rule_list("qemu/101")[0]["enable"] == 1
    fake.on_write = lambda: fake.external_rule_change("qemu/101")
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply(create_step("qemu/101", name="x"))
    assert exc.value.key == "proxmox.write_refused" and exc.value.detail == "status 500 digest" and len(fake.rule_list("qemu/101")) == 3


def test_the_host_refusing_a_write_is_passed_on_with_its_reason_key_only(fake, clk):
    w = writer(fake, clk)
    w.read()
    fake.fail_next("PUT", "/config$", 500)
    with pytest.raises(WriteError) as exc:
        w.apply(nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}))
    assert exc.value.key == "proxmox.write_refused" and exc.value.detail.startswith("status 500")
    fake.fail_next("PUT", "/config$", 403)
    with pytest.raises(WriteError) as exc:
        w.apply(nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}))
    assert exc.value.key == "proxmox.forbidden"


def test_partial_failure_in_a_three_step_plan_restores_the_first_steps(fake, clk, tmp_path):
    fake.fail_next("PUT", r"/qemu/108/firewall/options$", 500)
    steps = [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}), power_step("qemu/102", "stopped", "running"), fw_step("qemu/108", False, True)]
    res, _, _ = run(fake, clk, tmp_path, steps)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed" and res["rolled_back"] == "all"
    assert fake.net("qemu/101") == NIC0_101
    assert fake.status("qemu/102") == "stopped" and fake.fw_opts["qemu/108"]["enable"] == 0
    shut = [w for w in fake.writes if w["path"].endswith("/qemu/102/status/shutdown")]
    assert len(shut) == 1 and shut[0]["body"]["forceStop"] == "0"


def test_a_rule_created_by_a_failed_plan_is_removed_again(fake, clk, tmp_path):
    fake.fail_next("PUT", r"/qemu/108/config$", 500)
    steps = [create_step("qemu/101", proto="tcp", ports="22", name="ssh"), nic_step("qemu/108", {"vlan": 20}, {"vlan": 30})]
    res, _, _ = run(fake, clk, tmp_path, steps)
    assert res["status"] == "rolled_back"
    assert [r.get("comment") for r in fake.rule_list("qemu/101")] == ["https", "lanloom: test", None]
    assert [x["method"] for x in fake.writes if "/firewall/rules" in x["path"]] == ["POST", "DELETE"]


def test_a_rule_deleted_by_a_failed_plan_comes_back_at_its_place(fake, clk, tmp_path):
    before = json.loads(json.dumps(fake.rule_list("qemu/101")))
    fake.fail_next("PUT", r"/qemu/108/config$", 500)
    steps = [delete_step("qemu/101", 1, OWN_RULE), nic_step("qemu/108", {"vlan": 20}, {"vlan": 30})]
    res, _, _ = run(fake, clk, tmp_path, steps)
    assert res["status"] == "rolled_back"
    assert fake.rule_list("qemu/101") == before


def test_a_changed_rule_and_firewall_switch_are_restored(fake, clk, tmp_path):
    before = json.loads(json.dumps(fake.rule_list("qemu/101")))
    fake.fail_next("PUT", r"/qemu/108/config$", 500)
    steps = [rule_step("qemu/101", 1, {"ports": "8080", "source": "192.168.50.0/24"}, {"ports": "", "source": ""}),
             fw_step("qemu/102", False, True), nic_step("qemu/108", {"vlan": 20}, {"vlan": 30})]
    res, _, _ = run(fake, clk, tmp_path, steps)
    assert res["status"] == "rolled_back"
    assert fake.rule_list("qemu/101") == before and fake.fw_opts["qemu/102"]["enable"] == 0


def test_a_step_that_no_longer_matches_at_apply_time_rolls_the_earlier_ones_back(fake, clk, tmp_path):
    # the second guest changes right when the first write happens (after the gate, before the second step)
    fake.on_write = lambda: fake.external_config_change("qemu/108", net0="virtio=02:4C:4C:00:11:01,bridge=vmbr0,tag=55")
    steps = [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}), nic_step("qemu/108", {"vlan": 20}, {"vlan": 30})]
    res, _, _ = run(fake, clk, tmp_path, steps)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert fake.net("qemu/101") == NIC0_101                  # the earlier step is back; the host's own change is no step of ours


def test_a_pending_card_change_is_reverted_and_the_plan_rolled_back(fake, clk, tmp_path):
    fake.pending_slots = {"net0"}
    res, w, _ = run(fake, clk, tmp_path, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert fake.net("qemu/101") == NIC0_101 and fake.pend["qemu/101"] == {}
    puts = [x["body"] for x in fake.writes if x["method"] == "PUT"]
    assert "revert" in puts[1] and puts[1]["revert"] == "net0" and "digest" in puts[1]       # reverted, with the digest
    assert len(puts) == 2


def test_a_pending_card_change_reports_needs_restart(fake, clk):
    fake.pending_slots = {"net0"}
    w = writer(fake, clk)
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply(nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}))
    assert exc.value.key == "proxmox.needs_restart" and fake.pend["qemu/101"] == {}


def test_probe_notices_a_card_that_moved_back_and_the_runner_rolls_the_device_back(fake, clk, tmp_path):
    def interfere(s):
        clk.now += s
        if fake.net("qemu/101").endswith("tag=30") and fake.res["qemu/101"].get("_touched") is None:
            fake.res["qemu/101"]["_touched"] = 1
            fake.external_config_change("qemu/101", net0="virtio=02:4C:4C:00:0A:01,bridge=vmbr0,firewall=1,tag=40")
    w = ProxmoxWriter(url=fake.url, write_token_id=TOKEN_ID, write_token_secret=SECRET, clock=clk.time, sleep=clk.sleep, settle_s=0)
    plan = make_plan(w, clk, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])
    r = Runner(w, tmp_path / "ex", target="hypervisor", require_signature=False, clock=clk.time, sleep=interfere, watch_s=6, check_s=2)
    res = r.run(plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.rolled_back_device"
    assert fake.net("qemu/101") == NIC0_101


def test_two_failed_reads_while_watching_roll_the_plan_back(fake, clk, tmp_path):
    state = {"n": 0}

    def sleeper(s):
        clk.now += s
        if not state["n"]:
            state["n"] = 1
            fake.fail_next("GET", r"/qemu/101/config$", 500, times=2)
    w = ProxmoxWriter(url=fake.url, write_token_id=TOKEN_ID, write_token_secret=SECRET, clock=clk.time, sleep=sleeper, settle_s=0)
    plan = make_plan(w, clk, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])
    r = Runner(w, tmp_path / "ex", target="hypervisor", require_signature=False, clock=clk.time, sleep=clk.sleep, watch_s=10, check_s=2)
    res = r.run(plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.rolled_back_global"
    assert fake.net("qemu/101") == NIC0_101


def test_probe_checks_every_kind_and_skips_creates_and_deletes(fake, clk):
    w = writer(fake, clk)
    steps = [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}), power_step("qemu/102", "stopped", "running"),
             fw_step("qemu/108", False, True), rule_step("qemu/101", 0, {"enabled": True}, {"enabled": False}),
             create_step("qemu/101", name="x"), delete_step("qemu/101", 1, OWN_RULE)]
    w.read()
    pr = w.probe(steps)
    ids = {nic_id("qemu/101"), gid("qemu/102"), gid("qemu/108"), rid("qemu/101", 0)}
    assert pr == {"global_ok": True, "missing": pr["missing"]} and set(pr["missing"]) == ids
    for s in steps[:4]:
        w.apply(s)
    assert w.probe(steps) == {"global_ok": True, "missing": []}


# ---------------------------------------------------------------------------- crash recovery
def test_recover_puts_everything_back_after_a_crash(fake, clk, tmp_path):
    w = writer(fake, clk)
    w.read()
    ids = [nic_id("qemu/101"), gid("qemu/102"), rid("qemu/101", 0)]
    snap = w.snapshot(ids)
    rules_before = json.loads(json.dumps(fake.rule_list("qemu/101")))
    # the crashed run had done all of this
    w.apply(nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}))
    w.apply(power_step("qemu/102", "stopped", "running"))
    w.apply(rule_step("qemu/101", 0, {"enabled": True}, {"enabled": False}))
    new = w.apply(create_step("qemu/101", name="late"))["id"]
    r = make_runner(w, clk, tmp_path)
    (r.dir / "backup" / "p_crash").mkdir(parents=True, exist_ok=True)
    write_private(r.dir / "backup" / "p_crash" / "before.json", {"plan": "p_crash", "t": 1, "snapshot": snap})
    write_private(r.dir / "active.json", {"plan": "p_crash", "applied": True, "t": 1, "created": [new]})
    assert r.busy()
    out = r.recover()
    assert out["status"] == "rolled_back" and out["reason"] == "fw.res.recovered" and not r.busy()
    assert fake.net("qemu/101") == NIC0_101 and fake.status("qemu/102") == "stopped"
    assert fake.rule_list("qemu/101") == rules_before


def test_recover_does_nothing_when_nothing_was_applied(fake, clk, tmp_path):
    w = writer(fake, clk)
    w.read()
    snap = w.snapshot([nic_id("qemu/101")])
    fake.external_config_change("qemu/101", net0="virtio=02:4C:4C:00:0A:01,bridge=vmbr0,tag=66")
    r = make_runner(w, clk, tmp_path)
    (r.dir / "backup" / "p_x").mkdir(parents=True)
    write_private(r.dir / "backup" / "p_x" / "before.json", {"plan": "p_x", "t": 1, "snapshot": snap})
    write_private(r.dir / "active.json", {"plan": "p_x", "applied": False, "t": 1})
    assert r.recover()["status"] == "rolled_back"
    assert fake.net("qemu/101").endswith("tag=66") and not fake.writes


def test_snapshot_holds_what_the_way_back_writes_and_nothing_secret(fake, clk):
    w = writer(fake, clk)
    w.read()
    snap = w.snapshot([nic_id("qemu/101"), gid("qemu/101"), rid("qemu/101", 1), "new:abc"])
    assert snap["guest_nics"][0]["value"] == NIC0_101 and snap["guest_nics"][0]["slot"] == "net0"
    assert snap["guests"][0]["status"] == "running" and snap["guests"][0]["firewall"] is True
    assert snap["guest_rules"][0]["pos"] == 1 and snap["guest_rules"][0]["count"] == 3
    assert snap["guest_rules"][0]["rule"]["comment"] == "lanloom: test" and "digest" not in snap["guest_rules"][0]["rule"]
    assert SECRET not in json.dumps(snap)
    with pytest.raises(WriteError):
        w.snapshot(["nic000000000000"])


# ---------------------------------------------------------------------------- lockout
SELF_ADDR = lambda host: ({"192.168.50.13": "hv.why.self"}, {})        # noqa: E731


def test_a_guest_with_the_executors_address_cannot_be_stopped_moved_or_firewalled(fake, clk, tmp_path):
    w = writer(fake, clk)
    w.use_identity(SELF_ADDR)
    fake.fw_opts["lxc/103"]["enable"] = 0
    for i, step in enumerate([power_step("lxc/103", "running", "stopped"),
                              nic_step("lxc/103", {"vlan": None}, {"vlan": 20}),
                              nic_step("lxc/103", {"bridge": "vmbr0"}, {"bridge": "vmbr1"}),
                              nic_step("lxc/103", {"firewall": False}, {"firewall": True}),
                              fw_step("lxc/103", False, True),
                              create_step("lxc/103", name="x")]):
        res, _, _ = run(fake, clk, tmp_path / str(i), [step], w=w)
        refused(res, "fw.block.hv_critical")
    assert not fake.writes


def test_a_critical_guest_can_be_started_and_have_its_firewall_switched_off(fake, clk, tmp_path):
    w = writer(fake, clk)
    w.use_identity(SELF_ADDR)
    fake.res["lxc/103"]["status"] = "stopped"
    res, _, _ = run(fake, clk, tmp_path, [power_step("lxc/103", "stopped", "running")], w=w)
    assert res["status"] == "done" and fake.status("lxc/103") == "running"
    fake.fw_opts["lxc/103"]["enable"] = 1
    res, _, _ = run(fake, clk, tmp_path / "b", [fw_step("lxc/103", True, False)], w=w)
    assert res["status"] == "done" and fake.fw_opts["lxc/103"]["enable"] == 0


def test_a_guest_with_the_executors_card_is_critical_without_a_guest_agent(fake, clk, tmp_path):
    w = writer(fake, clk)
    w.use_identity(lambda host: ({}, {model.mac_hash("02:4c:4c:00:0a:01"): "hv.why.executor"}))
    res, _, _ = run(fake, clk, tmp_path, [power_step("qemu/101", "running", "stopped")], w=w)
    refused(res, "fw.block.hv_critical")
    res, _, _ = run(fake, clk, tmp_path / "b", [rule_step("qemu/101", 0, {"enabled": True}, {"enabled": False})], w=w)
    refused(res, "fw.block.hv_critical")
    res, _, _ = run(fake, clk, tmp_path / "c", [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})], w=w)
    refused(res, "fw.block.hv_critical")
    assert not fake.writes


def test_the_tag_and_the_list_protect_guests(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [power_step("qemu/107", "running", "stopped")])
    refused(res, "fw.block.hv_critical")
    res, _, _ = run(fake, clk, tmp_path / "b", [power_step("qemu/108", "running", "stopped")], protect_guests=[108])
    refused(res, "fw.block.hv_critical")
    res, _, _ = run(fake, clk, tmp_path / "c", [nic_step("qemu/108", {"vlan": 20}, {"vlan": 30})], protect_guests=["108"])
    refused(res, "fw.block.hv_critical")
    assert not fake.writes
    res, _, _ = run(fake, clk, tmp_path / "d", [power_step("qemu/108", "running", "stopped")])        # not listed: allowed
    assert res["status"] == "done" and fake.status("qemu/108") == "stopped"


def test_protected_addresses_and_the_hosts_own_address(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [power_step("lxc/103", "running", "stopped")], protect_addresses=["192.168.50.13"])
    refused(res, "fw.block.hv_critical")
    # the API address of the host itself is not a guest: it never makes a guest critical
    w = writer(fake, clk)
    w.use_identity(lambda host: ({host: "hv.why.self"}, {}))
    res, _, _ = run(fake, clk, tmp_path / "b", [power_step("lxc/103", "running", "stopped")], w=w)
    assert res["status"] == "done"


def test_template_locked_and_ha_guests_are_refused(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [power_step("qemu/104", "stopped", "running")])
    refused(res, "fw.block.hv_template")
    res, _, _ = run(fake, clk, tmp_path / "b", [nic_step("qemu/105", {"vlan": None}, {"vlan": 20})])
    refused(res, "fw.block.hv_locked")
    res, _, _ = run(fake, clk, tmp_path / "c", [power_step("qemu/106", "running", "stopped")])
    refused(res, "fw.block.hv_ha")
    assert not fake.writes
    res, _, _ = run(fake, clk, tmp_path / "d", [nic_step("qemu/106", {"vlan": None}, {"vlan": 20})])
    assert res["status"] == "done"                                # HA only governs start and stop
    assert not any("/status/" in w["path"] for w in fake.writes)


def test_a_card_to_a_bridge_the_guests_node_does_not_have_is_refused(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [nic_step("lxc/201", {"bridge": "vmbr0"}, {"bridge": "vmbr1"})])      # vmbr1 exists on node-a only
    refused(res, "fw.block.hv_bridge_unknown")
    res, _, _ = run(fake, clk, tmp_path / "b", [nic_step("qemu/101", {"bridge": "vmbr0"}, {"bridge": "vmbr77"})])
    refused(res, "fw.block.hv_bridge_unknown")
    assert not fake.writes


def test_a_vlan_outside_the_bridges_list_or_on_a_plain_bridge_is_refused(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 150})])             # vmbr0 allows 2-100
    refused(res, "fw.block.hv_vlan_not_allowed")
    res, _, _ = run(fake, clk, tmp_path / "b", [nic_step("qemu/101", {"vlan": 20}, {"vlan": 101})])
    refused(res, "fw.block.hv_vlan_not_allowed")
    res, _, _ = run(fake, clk, tmp_path / "c", [nic_step("qemu/101", {"bridge": "vmbr0"}, {"bridge": "cams"})])  # keeps tag 20 on a virtual net
    refused(res, "fw.block.hv_vlan_vnet")
    assert not fake.writes
    res, _, _ = run(fake, clk, tmp_path / "d", [nic_step("qemu/101", {"vlan": 20}, {"vlan": 100})])       # the edge is inside
    assert res["status"] == "done"


def test_a_plan_with_a_rule_create_and_another_rule_step_on_one_guest_is_refused(fake, clk, tmp_path):
    steps = [create_step("qemu/101", name="x"), rule_step("qemu/101", 0, {"enabled": True}, {"enabled": False})]
    res, _, _ = run(fake, clk, tmp_path, steps)
    refused(res, "fw.block.hv_rule_mix")
    steps = [delete_step("qemu/101", 1, OWN_RULE), rule_step("qemu/101", 0, {"enabled": True}, {"enabled": False})]
    res, _, _ = run(fake, clk, tmp_path / "b", steps)
    refused(res, "fw.block.hv_rule_mix")
    steps = [create_step("qemu/101", name="x"), create_step("qemu/101", name="y") | {"object": "new:" + "ffeeddccbbaa"}]
    res, _, _ = run(fake, clk, tmp_path / "c", steps)
    refused(res, "fw.block.hv_rule_mix")
    assert not fake.writes
    # different guests, or several plain rule changes of one guest, are fine
    steps = [create_step("qemu/101", name="x"), create_step("qemu/102", name="y") | {"object": "new:" + "ffeeddccbbaa"}]
    res, _, _ = run(fake, clk, tmp_path / "d", steps)
    assert res["status"] == "done"


def test_two_power_or_firewall_steps_on_one_guest_are_refused(fake, clk, tmp_path):
    steps = [power_step("qemu/102", "stopped", "running"), fw_step("qemu/102", False, True)]
    res, w, _ = run(fake, clk, tmp_path, steps)
    refused(res, "fw.block.unknown")                          # the gate sees one object twice before the family rule does
    assert not fake.writes
    assert w.plan_lockout(w.read(), steps) == "fw.block.hv_one_per_guest"
    assert w.plan_lockout(w.read(), steps[:1]) is None


def test_a_plan_for_an_unknown_guest_is_refused(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [{"kind": "guest.power", "object": "g000000000000", "before": {"status": "stopped"},
                                           "after": {"status": "running"}}])
    refused(res, "fw.block.unknown")
    assert not fake.writes


def test_a_power_step_whose_before_is_not_the_hosts_state_is_refused_at_the_gate(fake, clk, tmp_path):
    res, _, _ = run(fake, clk, tmp_path, [power_step("qemu/101", "stopped", "running")])      # it is running
    refused(res, "fw.block.conflict")
    assert not fake.writes


# ---------------------------------------------------------------------------- the account
def test_a_token_with_only_audit_rights_cannot_write(fake, clk):
    fake.permissions = fw.PERMISSIONS_AUDIT
    w = writer(fake, clk)
    assert w.account() == (False, ["cannot_write"])
    assert w.health() == (False, "token cannot write")
    assert w.health() == (False, "token cannot write")           # cached, still refused


def test_a_gate_with_an_audit_only_token_refuses_the_plan(fake, clk, tmp_path):
    fake.permissions = fw.PERMISSIONS_AUDIT
    res, _, _ = run(fake, clk, tmp_path, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])
    refused(res, "fw.block.health")
    assert not fake.writes


def test_a_token_with_system_rights_works_but_is_warned_about(fake, clk, caplog):
    fake.permissions = fw.PERMISSIONS_BROAD
    w = writer(fake, clk)
    assert w.account() == (True, ["owner_role"])
    with caplog.at_level(logging.WARNING, logger="lanloom.executor"):
        assert w.health() == (True, "")
    assert "could change much more than guests" in caplog.text and SECRET not in caplog.text


def test_a_proper_token_has_no_warning(fake, clk, caplog):
    w = writer(fake, clk)
    with caplog.at_level(logging.DEBUG):
        assert w.account() == (True, []) and w.health() == (True, "")
    assert "much more" not in caplog.text


def test_the_superuser_token_is_refused_at_construction():
    for tid in ("root@pam!x", "root@pam!lanloom", "ROOT@PAM!x"):
        with pytest.raises(ValueError, match="superuser"):
            ProxmoxWriter(url="https://192.0.2.10:8006", write_token_id=tid, write_token_secret=SECRET)
    ProxmoxWriter(url="https://192.0.2.10:8006", write_token_id="rootless@pve!x", write_token_secret=SECRET)


def test_a_refused_token_marks_the_writer_dead(fake, clk):
    w = ProxmoxWriter(url=fake.url, write_token_id=TOKEN_ID, write_token_secret="00000000-0000-0000-0000-000000000000", clock=clk.time,
                      sleep=clk.sleep)
    assert w.health() == (False, "proxmox.token_refused")
    assert w.health() == (False, "token refused")
    with pytest.raises(WriteError) as exc:
        w.read()
    assert exc.value.key == "proxmox.token_refused" and not fake.writes


def test_an_unreachable_host_is_a_health_failure_not_a_crash(clk):
    f = FakeWritable()
    url = f.url
    f.stop()
    w = ProxmoxWriter(url=url, write_token_id=TOKEN_ID, write_token_secret=SECRET, clock=clk.time, sleep=clk.sleep)
    ok, why = w.health()
    assert ok is False and why == "integration.err.unreachable"


# ---------------------------------------------------------------------------- secrets
def test_the_write_token_secret_never_shows(fake, clk, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = writer(fake, clk)
    texts = [repr(w), str(w), repr(w._t), str(w._t), repr(w._secret), str(w._secret), "%s" % w._secret, "{}".format(w._secret)]
    assert w.read() and w.health()
    snap = w.snapshot([nic_id("qemu/101"), gid("qemu/101"), rid("qemu/101", 0)])
    texts.append(json.dumps(snap))
    # one run that works, one that fails on the host, one that is refused: nothing of it may carry the secret
    r = make_runner(w, clk, tmp_path)
    plans = [make_plan(w, clk, [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30})])]
    res_ok = r.run(plans[0])
    fake.fail_next("PUT", r"/config$", 500)
    res_bad = r.run(make_plan(w, clk, [nic_step("qemu/108", {"vlan": 20}, {"vlan": 30})]))
    res_ref = r.run(make_plan(w, clk, [power_step("qemu/104", "stopped", "running")]))
    assert (res_ok["status"], res_bad["status"], res_ref["status"]) == ("done", "rolled_back", "refused")
    texts += [json.dumps(x, default=str) for x in (res_ok, res_bad, res_ref, r.history(50))]
    for err in (lambda: w._send("PUT", "/nodes/node-a/qemu/101/config", {"memory": "1"}),
                lambda: w.apply(nic_step("qemu/101", {"vlan": 99}, {"vlan": 40}))):
        with pytest.raises(WriteError) as exc:
            err()
        texts += [str(exc.value), repr(exc.value), exc.value.key, exc.value.detail]
    for p in (tmp_path / "ex").rglob("*"):
        if p.is_file():
            texts.append(p.read_text(encoding="utf-8"))
    texts.append(caplog.text)
    texts += [str(m) for m in caplog.messages]
    for t in texts:
        assert SECRET not in t and "7f3a9c2e" not in t
    with pytest.raises(TypeError):
        json.dumps({"s": w._secret})
    assert w._auth()["Authorization"].endswith(SECRET) and w._t._auth is not None            # only the one send path can ask for it


def test_the_way_back_leaves_objects_no_step_reached_alone(fake, clk, tmp_path):
    # the first step fails at once; the second card was never touched by the plan but changed on the host meanwhile -
    # the rollback must not put that card back to the backup (it would undo someone else's change)
    fake.fail_next("PUT", r"/qemu/101/config$", 500)
    fake.on_write = lambda *a: fake.external_config_change("qemu/108", net0=fake.net("qemu/108").replace("tag=20", "tag=40"))
    steps = [nic_step("qemu/101", {"vlan": 20}, {"vlan": 30}), nic_step("qemu/108", {"vlan": 20}, {"vlan": 30})]
    res, _, _ = run(fake, clk, tmp_path, steps)
    assert res["status"] == "rolled_back"
    assert "tag=40" in fake.net("qemu/108")
