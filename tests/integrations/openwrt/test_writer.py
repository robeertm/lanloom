"""Write half of the OpenWrt family against a stateful fake of rpcd's ubus API: field-exact allow-list of JSON-RPC
calls, changes staged in the session, the router's own way back (``uci apply`` with rollback + timeout, ``uci
confirm``, ``uci rollback``), Lanloom's own way back, stable references across commits, lockout rules, and the
executor pipeline end to end."""
import ast
import json
from pathlib import Path

import pytest

from lanloom.firewall import model as fwmodel
from lanloom.firewall import steps as fwsteps
from lanloom.firewall.runner import Runner
from lanloom.integrations.openwrt import model
from lanloom.integrations.openwrt import write as wmod
from lanloom.integrations.openwrt.write import OpenwrtWriter, WriteError

from . import fixtures as fx
from .fake_ubus import FakeUbus

SID = "a" * 32


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture
def clk():
    return Clock()


@pytest.fixture(params=["21.02", "23.05"])
def fake(request):
    f = FakeUbus(request.param)
    yield f
    f.close()


@pytest.fixture
def fw4():
    f = FakeUbus("23.05")
    yield f
    f.close()


def writer(fake, clk, **kw):
    kw.setdefault("settle_s", 0)
    return OpenwrtWriter(url=fake.url, write_username=fx.WRITE_USER, write_password=fx.WRITE_PASSWORD, clock=clk.time,
                         sleep=clk.sleep, **kw)


def z(name):
    return model.zone_id(name)


def pid(ref):
    return model.policy_id(ref)


_nonce = iter(range(1, 10_000))


def make_plan(w, clk, steps):
    st = w.read()
    now = int(clk.now)
    conf = {fwsteps.confirm_token(s) for s in steps if fwsteps.needs_confirmation(s)}
    return {"v": 2, "id": "p_%d_%04d" % (now, next(_nonce)), "target": "controller", "base": fwmodel.fingerprint(st),
            "created": now, "expires": now + 300, "nonce": "%032x" % next(_nonce), "requester": {"user": "admin", "device": None},
            "steps": steps, "warnings": [], "confirmed": sorted(conf), "costs": {"objects": len(steps)}, "undo_of": None}


def run(fake, clk, tmp_path, steps, *, w=None, watch_s=0):
    w = w or writer(fake, clk)
    plan = make_plan(w, clk, steps)
    r = Runner(w, tmp_path / "ex", target="controller", require_signature=False, clock=clk.time, sleep=clk.sleep,
               watch_s=watch_s, check_s=1)
    return r.run(plan), w, r


GUESTS = "a:Guests may not reach home#1"
OLD = "a:Old test#1"
OWN = "s:" + fx.OWN


def toggle(ref=GUESTS, on=False):
    return {"kind": "policy.update", "object": pid(ref), "before": {"enabled": not on}, "after": {"enabled": on}}


def create(src="guest", dst="iot", **after):
    a = dict({"name": "Guests may not reach things", "src": {"zone": z(src)}, "dst": {"zone": z(dst) if dst != "*" else "*"},
              "action": "block"}, **after)
    return {"kind": "policy.create", "object": "new:0a1b2c3d4e5f", "before": {}, "after": a}


def body(obj, method, args, sid=SID):
    return model.call_body(model.ZERO_SID if (obj, method) == ("session", "login") else sid, obj, method, args, 7)


# ---------------------------------------------------------------------------- the allow-list below the adapter
GOOD = {"name": "[lanloom] x", "src": "guest", "dest": "iot", "proto": "tcp", "dest_port": "8000-8080", "target": "DROP",
        "enabled": "1"}


@pytest.mark.parametrize("obj,method,args", [
    ("session", "login", {"username": "lanloom-write", "password": "x", "timeout": 900}),
    ("uci", "get", {"config": "firewall"}),
    ("uci", "get", {"config": "network"}),
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee", "values": GOOD}),
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee",
                    "values": {"name": "[lanloom] to the router", "src": "iot", "proto": "all", "target": "REJECT", "enabled": "1"}}),
    ("uci", "set", {"config": "firewall", "section": "cfg0a1b2c", "values": {"enabled": "0"}}),
    ("uci", "set", {"config": "firewall", "section": "cfg0a1b2c", "values": {"name": "any text", "dest": "*", "target": "ACCEPT"}}),
    ("uci", "delete", {"config": "firewall", "section": "lanloom_00aabbccddee"}),
    ("uci", "delete", {"config": "firewall", "section": "cfg0a1b2c", "option": "dest_port"}),
    ("uci", "delete", {"config": "firewall", "section": "cfg0a1b2c", "option": "dest"}),
    ("uci", "changes", {"config": "firewall"}),
    ("uci", "revert", {"config": "firewall"}),
    ("uci", "apply", {"rollback": True, "timeout": 90}),
    ("uci", "apply", {"rollback": False}),
    ("uci", "confirm", {}),
    ("uci", "rollback", {}),
    ("system", "board", {}),
    ("network.interface", "dump", {}),
])
def test_allow_list_lets_the_planned_calls_through(obj, method, args):
    wmod.ALLOW.check("POST", "/ubus", body(obj, method, args))


@pytest.mark.parametrize("obj,method,args", [
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee", "values": dict(GOOD, name="no mark")}),
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "my_rule", "values": GOOD}),           # own sections only
    ("uci", "add", {"config": "firewall", "type": "rule", "values": GOOD}),                              # anonymous: never
    ("uci", "add", {"config": "firewall", "type": "zone", "name": "lanloom_00aabbccddee", "values": GOOD}),
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee",
                    "values": {k: v for k, v in GOOD.items() if k != "target"}}),                        # incomplete
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee", "values": dict(GOOD, src_ip="10.0.0.1")}),
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee", "values": dict(GOOD, src="*")}),
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee", "values": dict(GOOD, dest_port="80 443")}),
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee", "values": dict(GOOD, target="MARK")}),
    ("uci", "add", {"config": "firewall", "type": "rule", "name": "lanloom_00aabbccddee", "values": dict(GOOD, name="[lanloom] a\nb")}),
    ("uci", "add", {"config": "network", "type": "rule", "name": "lanloom_00aabbccddee", "values": GOOD}),
    ("uci", "set", {"config": "firewall", "section": "cfg0a1b2c", "values": {}}),
    ("uci", "set", {"config": "firewall", "section": "cfg0a1b2c", "values": {"masq": "1"}}),
    ("uci", "set", {"config": "firewall", "section": "cfg0a1b2c", "values": {"network": ["lan"]}}),
    ("uci", "set", {"config": "network", "section": "lan", "values": {"enabled": "0"}}),
    ("uci", "set", {"config": "wireless", "section": "default_radio0", "values": {"enabled": "0"}}),
    ("uci", "set", {"config": "firewall", "section": "bad name!", "values": {"enabled": "0"}}),
    ("uci", "delete", {"config": "firewall", "section": "cfg0a1b2c"}),                                    # a foreign section: never
    ("uci", "delete", {"config": "firewall", "section": "cfg0a1b2c", "option": "src"}),
    ("uci", "apply", {"rollback": True, "timeout": 30}),                                                 # shorter than the web interface
    ("uci", "apply", {"rollback": True}),
    ("uci", "apply", {}),
    ("uci", "get", {"config": "wireless"}),
    ("uci", "commit", {"config": "firewall"}),
    ("uci", "rename", {"config": "firewall", "section": "lanloom_00aabbccddee", "name": "x"}),
    ("uci", "order", {"config": "firewall", "sections": ["a"]}),
    ("file", "write", {"path": "/etc/config/firewall", "data": "x"}),
    ("file", "exec", {"command": "/sbin/reboot"}),
    ("system", "reboot", {}),
    ("rc", "init", {"name": "firewall", "action": "stop"}),
    ("session", "login", {"username": "lanloom-write", "password": "x"}),
])
def test_allow_list_refuses_everything_else(obj, method, args):
    with pytest.raises(Exception):
        wmod.ALLOW.check("POST", "/ubus", body(obj, method, args))


def test_allow_list_checks_the_envelope_and_the_session():
    good = body("uci", "confirm", {})
    wmod.ALLOW.check("POST", "/ubus", good)
    for bad in (dict(good, method="list"), dict(good, jsonrpc="1.0"), dict(good, extra=1),
                dict(good, params=[model.ZERO_SID, "uci", "confirm", {}]),             # a change without a session
                dict(good, params=["xyz", "uci", "confirm", {}])):
        with pytest.raises(Exception):
            wmod.ALLOW.check("POST", "/ubus", bad)
    with pytest.raises(Exception):
        wmod.ALLOW.check("POST", "/cgi-bin/luci", good)
    with pytest.raises(Exception):
        wmod.ALLOW.check("POST", "/ubus", body("session", "login", {"username": "u", "password": "p", "timeout": 900}, sid=SID)
                         | {"params": [SID, "session", "login", {"username": "u", "password": "p", "timeout": 900}]})


def test_the_module_has_exactly_one_send_path():
    src = Path(wmod.__file__).read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "request"]
    assert len(calls) == 1
    assert "http.client" not in src and "urllib.request" not in src and "socket" not in src.replace("connguard", "")


def test_a_refused_call_never_leaves_the_process(fw4, clk):
    w = writer(fw4, clk)
    w.read()
    n = len(fw4.requests)
    with pytest.raises(WriteError) as e:
        w._call("uci", "set", {"config": "network", "section": "lan", "values": {"proto": "dhcp"}})
    assert e.value.key == "openwrt.refused_by_allow_list" and len(fw4.requests) == n


def test_options_are_checked(fw4, clk):
    for bad in ({"url": "ftp://x"}, {"write_username": "bad user"}, {"write_password": ""}, {"nope": 1}):
        kw = dict(url=fw4.url, write_username=fx.WRITE_USER, write_password=fx.WRITE_PASSWORD)
        kw.update(bad)
        with pytest.raises(ValueError):
            OpenwrtWriter(**kw)
    w = writer(fw4, clk)
    assert fx.WRITE_PASSWORD not in repr(w) and "127.0.0.1" in repr(w)
    assert writer(fw4, clk, apply_timeout=5).apply_timeout == 90


def test_a_refused_password_makes_the_half_unhealthy(fw4, clk):
    w = OpenwrtWriter(url=fw4.url, write_username=fx.WRITE_USER, write_password="wrong", clock=clk.time, sleep=clk.sleep)
    assert w.health()[0] is False


def test_the_read_login_cannot_write(fw4, clk, tmp_path):
    """The router itself refuses changes of a login whose access list only reads (here: the read-only login used by
    mistake in the executor) - nothing is staged, nothing goes live."""
    w = OpenwrtWriter(url=fw4.url, write_username=fx.READ_USER, write_password=fx.READ_PASSWORD, clock=clk.time,
                      sleep=clk.sleep, settle_s=0)
    with pytest.raises(WriteError) as e:
        run(fw4, clk, tmp_path, [toggle()], w=w)                       # refused before the backup is complete
    assert e.value.key == "openwrt.forbidden"
    assert fw4.rule("Guests may not reach home").get("enabled") is None and fw4.applies == []


# ---------------------------------------------------------------------------- one change, both releases
def test_switching_a_rule_off_with_the_routers_way_back(fake, clk, tmp_path):
    res, w, _ = run(fake, clk, tmp_path, [toggle()])
    assert res["status"] == "done", res
    assert fake.changes_made() == ["set", "apply", "confirm"]
    assert fake.applies == [(True, 90)] and fake.confirms == 1 and fake.pending is None
    assert fake.rule("Guests may not reach home")["enabled"] == "0"
    fake.expire()
    assert fake.rule("Guests may not reach home")["enabled"] == "0"   # nothing to revert after the confirmation
    assert w.capabilities()["rollback"]["device_native"] is True


def test_staged_changes_are_not_live_until_apply(fw4, clk):
    w = writer(fw4, clk)
    st = w.read()
    step = toggle()
    w.snapshot([step["object"]])
    w.apply(step)
    assert fw4.rule("Guests may not reach home").get("enabled") is None   # staged in the session only
    other = writer(fw4, clk)
    p = next(x for x in other.read()["policies"] if x["id"] == step["object"])
    assert p["enabled"] is True                                        # another session does not see it
    w.settle([step])
    assert fw4.rule("Guests may not reach home")["enabled"] == "0"
    assert st


def test_the_router_reverts_on_its_own_when_the_executor_never_confirms(fw4, clk, tmp_path):
    """The executor dies right after the apply: the router's 90 second timer puts the configuration back, and the
    executor's recovery afterwards finds nothing left to write."""
    w = writer(fw4, clk)
    w.read()
    step = toggle()
    snap = w.snapshot([step["object"]])
    assert snap["device_native"]
    w.apply(step)

    class Died(Exception):
        pass

    def die():
        raise Died()
    w.read = die
    with pytest.raises(Died):
        w.settle([step])
    assert fw4.pending and fw4.rule("Guests may not reach home")["enabled"] == "0"   # live, timer runs
    fw4.expire()                                                       # 90 s without confirmation
    assert fw4.rule("Guests may not reach home").get("enabled") is None
    fresh = writer(fw4, clk)
    fw4.requests.clear()
    fresh.restore(snap)
    assert fw4.changes_made() == []                                    # nothing differs any more


def test_a_cut_off_executor_is_reverted_by_the_router(fw4, clk, tmp_path):
    """The change cuts the executor off right after the apply (every request fails): the half cannot confirm, the
    run is rolled back, and the router's own timer brings the configuration back without any help."""
    fw4.drop_after_apply = True
    res, w, _ = run(fw4, clk, tmp_path, [toggle()])
    assert res["status"] != "done"
    assert fw4.pending is not None and fw4.confirms == 0
    fw4.expire()
    fw4.restore_link()
    assert fw4.rule("Guests may not reach home").get("enabled") is None


def test_a_check_that_fails_after_apply_rolls_back_at_once(fw4, clk, tmp_path):
    w = writer(fw4, clk)
    orig = w._missing
    w._missing = lambda steps: ["x"]                                   # the read-back does not show the change
    res, w, _ = run(fw4, clk, tmp_path, [toggle()], w=w)
    w._missing = orig
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed", res
    assert fw4.rollbacks == 1 and fw4.confirms == 0 and fw4.pending is None
    assert fw4.rule("Guests may not reach home").get("enabled") is None


def test_a_slow_check_is_not_confirmed_late(fw4, clk, tmp_path):
    w = writer(fw4, clk, settle_s=60)                                  # the read-back ends after 60 % of the timeout
    res, w, _ = run(fw4, clk, tmp_path, [toggle()], w=w)
    assert res["status"] == "rolled_back", res
    assert fw4.confirms == 0 and fw4.rollbacks == 1
    assert fw4.rule("Guests may not reach home").get("enabled") is None


def test_another_apply_that_waits_refuses_ours(fw4, clk, tmp_path):
    fw4.pending = {"sid": "b" * 32, "copy": json.loads(json.dumps(fw4.stored)), "timeout": 90}
    res, w, _ = run(fw4, clk, tmp_path, [toggle()])
    assert res["status"] != "done"
    assert fw4.rule("Guests may not reach home").get("enabled") is None
    assert fw4.calls("revert")                                         # the staged change was discarded


def test_an_old_rpcd_without_rollback_still_has_lanlooms_way_back(fw4, clk, tmp_path):
    fw4.no_rollback = True
    w = writer(fw4, clk)
    w._native = False                                                  # the version probe said: no own way back
    res, w, _ = run(fw4, clk, tmp_path, [toggle()], w=w)
    assert res["status"] == "done", res
    assert fw4.applies == [(False, None)] and fw4.calls("confirm") == []
    assert w.capabilities()["rollback"]["device_native"] is False


# ---------------------------------------------------------------------------- create, change, delete
def test_create_adds_a_named_marked_rule_and_returns_its_id(fake, clk, tmp_path):
    res, w, _ = run(fake, clk, tmp_path, [create(ports="8000-8080", proto="tcp")])
    assert res["status"] == "done", res
    new = [s for n, s in fake.committed().items() if n.startswith("lanloom_") and n != fx.OWN]
    assert len(new) == 1
    r = new[0]
    assert r["name"] == "[lanloom] Guests may not reach things" and r["src"] == "guest" and r["dest"] == "iot"
    assert r["target"] == "DROP" and r["proto"] == "tcp" and r["dest_port"] == "8000-8080" and r["enabled"] == "1"
    assert list(fake.committed())[-1] == r[".name"]                    # appended after every existing rule
    st = w.read()
    p = next(x for x in st["policies"] if x["vendor_ref"] == "s:" + r[".name"])
    assert p["own"] and p["editable"] and p["name"] == "Guests may not reach things" and p["ports"] == "8000-8080"
    assert res["inverse"][0]["kind"] == "policy.delete" and res["inverse"][0]["object"] == p["id"]


def test_create_to_the_router_itself(fw4, clk, tmp_path):
    res, w, _ = run(fw4, clk, tmp_path, [create(dst="self", action="allow", proto="udp", ports="53", name="Guest DNS too")])
    assert res["status"] == "done", res
    r = next(s for n, s in fw4.committed().items() if s.get("name") == "[lanloom] Guest DNS too")
    assert "dest" not in r and r["target"] == "ACCEPT" and r["dest_port"] == "53"


def test_change_action_target_and_ports_of_a_foreign_rule(fw4, clk, tmp_path):
    ref = "a:Guest DNS#1"
    st = writer(fw4, clk).read()
    p = next(x for x in st["policies"] if x["vendor_ref"] == ref)
    step = {"kind": "policy.update", "object": p["id"], "before": {"action": "allow", "ports": "53", "dst": p["dst"]},
            "after": {"action": "reject", "ports": "", "dst": {"zone": z("lan")}}}
    res, w, _ = run(fw4, clk, tmp_path, [step])
    assert res["status"] == "done", res
    r = fw4.rule("Guest DNS")
    assert r["target"] == "REJECT" and r["dest"] == "lan" and "dest_port" not in r
    st = w.read()
    assert any(x["id"] == p["id"] and x["action"] == "reject" for x in st["policies"])   # same id after the commit


def test_a_foreign_unnamed_rule_reference_survives_a_commit_that_renames_its_section(fw4, clk, tmp_path):
    before = fw4.rule("Guests may not reach home")[".name"]
    res, w, _ = run(fw4, clk, tmp_path, [toggle()])
    assert res["status"] == "done"
    assert fw4.rule("Guests may not reach home")[".name"] == before     # a changed value keeps the name
    fw4.stored["firewall"].insert(1, fx.sec("rule", anon=True, name="Added on the router", src="lan", target="ACCEPT"))
    after = fw4.rule("Guests may not reach home")[".name"]
    assert before != after                                             # a section added before it renames it
    st = w.read()
    assert next(x for x in st["policies"] if x["id"] == pid(GUESTS))["enabled"] is False
    res, w, _ = run(fw4, clk, tmp_path, [toggle(on=True)])            # and the way back finds it again
    assert res["status"] == "done", res
    assert fw4.rule("Guests may not reach home")["enabled"] == "1"


def test_a_foreign_unnamed_rule_keeps_its_name(fw4, clk, tmp_path):
    step = {"kind": "policy.update", "object": pid(GUESTS), "before": {"name": "Guests may not reach home"},
            "after": {"name": "Something else"}}
    w = writer(fw4, clk)
    st = w.read()
    assert w.lockout(st, step) == "fw.block.capability"
    own = {"kind": "policy.update", "object": pid(OWN), "before": {"name": "IoT may not reach guests"},
           "after": {"name": "IoT never reaches guests"}}
    assert w.lockout(st, own) is None
    res, w, _ = run(fw4, clk, tmp_path, [own])
    assert res["status"] == "done", res
    assert fw4.committed()[fx.OWN]["name"] == "[lanloom] IoT never reaches guests"


def test_delete_an_own_rule_and_undo_brings_it_back(fake, clk, tmp_path):
    w = writer(fake, clk)
    st = w.read()
    p = next(x for x in st["policies"] if x["vendor_ref"] == OWN)
    step = {"kind": "policy.delete", "object": p["id"], "after": {},
            "before": {f: p[f] for f in ("name", "src", "dst", "action", "proto", "ports", "enabled")}}
    res, w, _ = run(fake, clk, tmp_path, [step], w=w)
    assert res["status"] == "done", res
    assert fx.OWN not in fake.committed()
    inv = res["inverse"]
    assert inv and inv[0]["kind"] == "policy.create"
    res2, w, _ = run(fake, clk, tmp_path, inv)
    assert res2["status"] == "done", res2
    back = [s for n, s in fake.committed().items() if s.get("name") == "[lanloom] IoT may not reach guests"]
    assert len(back) == 1 and back[0]["target"] == "DROP"


def test_steps_are_idempotent(fw4, clk):
    w = writer(fw4, clk)
    w.read()
    step = toggle()
    w.snapshot([step["object"]])
    w.apply(step)
    n = len(fw4.calls("set"))
    w.apply(step)                                                      # already as planned: nothing written
    assert len(fw4.calls("set")) == n
    w._delete("not-there", None)                                       # already gone: fine


def test_a_foreign_rule_is_never_deleted(fw4, clk, tmp_path):
    w = writer(fw4, clk)
    st = w.read()
    step = {"kind": "policy.delete", "object": pid(GUESTS), "before": {}, "after": {}}
    assert w.lockout(st, step) == "fw.block.not_own"
    with pytest.raises(WriteError) as e:
        w._delete(pid(GUESTS), None)
    assert e.value.key == "openwrt.not_own"
    assert fw4.rule("Guests may not reach home") is not None


@pytest.mark.parametrize("step,reason", [
    ({"kind": "policy.update", "object": pid("a:Allow-Ping#1"), "before": {}, "after": {"enabled": False}}, "fw.block.builtin"),
    ({"kind": "policy.update", "object": pid("a:IoT to the broker#1"), "before": {}, "after": {"enabled": False}}, "fw.block.builtin"),
    ({"kind": "policy.update", "object": pid("a:?iot>guest#1"), "before": {}, "after": {"enabled": False}}, "fw.block.builtin"),
    ({"kind": "policy.update", "object": pid("input:wan"), "before": {}, "after": {"action": "allow"}}, "fw.block.builtin"),
    ({"kind": "policy.update", "object": pid("fwd:a:lan>wan#1"), "before": {}, "after": {"enabled": False}}, "fw.block.builtin"),
    ({"kind": "policy.create", "object": "new:1", "before": {}, "after": {"name": "x", "src": {"zone": "*"},
                                                                         "dst": {"zone": z("lan")}, "action": "block"}},
     "fw.block.capability"),
    ({"kind": "policy.create", "object": "new:1", "before": {}, "after": {"name": "x", "src": {"zone": z("self")},
                                                                         "dst": {"zone": z("lan")}, "action": "block"}},
     "fw.block.capability"),
    ({"kind": "policy.create", "object": "new:1", "before": {}, "after": {"name": "x", "src": {"zone": z("guest")},
                                                                         "dst": {"zone": z("lan"), "match": {"kind": "ip", "items": ["192.168.1.2"]}},
                                                                         "action": "block"}}, "fw.block.capability"),
    ({"kind": "policy.create", "object": "new:1", "before": {}, "after": {"name": "x", "src": {"zone": z("guest")},
                                                                         "dst": {"zone": z("lan")}, "action": "block",
                                                                         "ports": "80,443", "proto": "tcp"}}, "fw.block.capability"),
    ({"kind": "policy.create", "object": "new:1", "before": {}, "after": {"name": "x", "src": {"zone": z("guest")},
                                                                         "dst": {"zone": z("lan")}, "action": "block",
                                                                         "ports": "80", "proto": "any"}}, "fw.block.capability"),
    ({"kind": "zone.create", "object": "new:1", "before": {}, "after": {"name": "x"}}, "fw.block.capability"),
    ({"kind": "network.update", "object": "x", "before": {}, "after": {"name": "x"}}, "fw.block.capability"),
])
def test_lockout_rules(fw4, clk, step, reason):
    w = writer(fw4, clk)
    assert w.lockout(w.read(), step) == reason


def test_a_change_on_the_router_in_between_is_refused_and_nothing_goes_live(fake, clk, tmp_path):
    w = writer(fake, clk)
    second = pid(OWN)
    plan = make_plan(w, clk, [toggle(), {"kind": "policy.update", "object": second,
                                         "before": {"name": "IoT may not reach guests"},
                                         "after": {"name": "IoT never reaches guests"}}])
    orig = w._update

    def update(step):
        if step["object"] == second:                                   # someone renames the rule on the router meanwhile
            for x in fake.stored["firewall"]:
                if x.get(".name") == fx.OWN:
                    x["name"] = "[lanloom] renamed meanwhile"
        return orig(step)
    w._update = update
    r = Runner(w, tmp_path / "ex", target="controller", require_signature=False, clock=clk.time, sleep=clk.sleep, watch_s=0)
    res = r.run(plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed", res
    assert fake.rule("Guests may not reach home").get("enabled") is None   # the first step never went live
    assert fake.applies == [] and fake.pending is None                    # nothing applied half-way
    assert fake.calls("revert")
    assert fake.committed()[fx.OWN]["name"] == "[lanloom] renamed meanwhile"   # the router's own change stays


def test_a_lost_answer_on_create_leaves_no_stray_rule(fake, clk, tmp_path):
    fake.lose_answer = True
    res, w, _ = run(fake, clk, tmp_path, [create()])
    assert res["status"] != "done"
    assert not [n for n in fake.committed() if n.startswith("lanloom_") and n != fx.OWN]
    assert fake.pending is None


def test_watch_puts_back_a_rule_someone_flipped_after_the_change(fw4, clk, tmp_path):
    w = writer(fw4, clk)
    calls = {"n": 0}
    orig = w.probe

    def probe(steps):
        calls["n"] += 1
        if calls["n"] == 1:
            fw4.edit("Guests may not reach home", enabled="1")
        return orig(steps)
    w.probe = probe
    res, w, _ = run(fw4, clk, tmp_path, [toggle()], w=w, watch_s=5)
    assert res["status"] in ("rolled_back", "partial", "done")
    assert calls["n"] >= 1


def test_two_failed_reads_while_watching_count_as_global(fw4, clk):
    w = writer(fw4, clk)
    w.read()
    w._read_raw = lambda: (_ for _ in ()).throw(WriteError("openwrt.read_failed"))
    assert w.probe([toggle()])["global_ok"] is True
    assert w.probe([toggle()])["global_ok"] is False


def test_restore_after_a_crash_writes_every_saved_option_back(fw4, clk):
    w = writer(fw4, clk)
    w.read()
    snap = w.snapshot([pid(GUESTS)])
    fw4.edit("Guests may not reach home", target="ACCEPT", enabled="0")   # what a half-done crashed plan left live
    fresh = writer(fw4, clk)
    fresh.restore(snap)
    r = fw4.rule("Guests may not reach home")
    assert r["target"] == "REJECT" and r.get("enabled") is None
    assert fw4.applies[-1] == (False, None)                            # a restore is applied without a timer


def test_left_overs_of_an_earlier_session_never_go_live(fw4, clk, tmp_path):
    w = writer(fw4, clk)
    w.read()
    w._call("uci", "set", {"config": "firewall", "section": w._sections["a:Old test#1"], "values": {"target": "DROP"}})
    res, w, _ = run(fw4, clk, tmp_path, [toggle()], w=w)
    assert res["status"] == "done", res
    assert fw4.rule("Old test")["target"] == "ACCEPT"


def test_the_password_and_session_never_show(fw4, clk, tmp_path, caplog):
    res, w, _ = run(fw4, clk, tmp_path, [toggle()])
    assert res["status"] == "done"
    text = repr(w) + str(w.__dict__.get("writes")) + caplog.text + json.dumps(res, default=str)
    assert fx.WRITE_PASSWORD not in text and (w._sid or "x" * 32) not in json.dumps(res, default=str)
    with pytest.raises(WriteError) as e:
        OpenwrtWriter(url=fw4.url, write_username=fx.WRITE_USER, write_password="nope", clock=clk.time)._read_raw()
    assert "nope" not in str(e.value) and "nope" not in repr(e.value)


def test_login_limit_per_hour(fw4, clk):
    w = writer(fw4, clk)
    for _ in range(4):
        w._sid = None
        w._login()
    w._sid = None
    with pytest.raises(WriteError) as e:
        w._login()
    assert e.value.key == "openwrt.login_limit"
    clk.now += 3601
    w._login()
