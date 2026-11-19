"""Write half of the OPNsense family against a stateful fake of the REST API: field-exact allow-list, Lanloom's own
way back, the firewall's own way back on the releases that have it (savepoint + 60 second timer + confirmation),
none on the releases that dropped it, lockout rules, and the executor pipeline end to end."""
import ast
import copy
import json
from pathlib import Path

import pytest

from lanloom.firewall import model as fwmodel
from lanloom.firewall import steps as fwsteps
from lanloom.firewall.runner import Runner
from lanloom.integrations import transport
from lanloom.integrations.opnsense import model
from lanloom.integrations.opnsense import write as wmod
from lanloom.integrations.opnsense.write import OpnsenseWriter, WriteError

from . import fixtures as fx
from .fake_opn import FakeOpn

REV = "1763500007.1234"
U = fx.U


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


@pytest.fixture(params=["24.7", "26.7"])
def fake(request):
    f = FakeOpn(request.param)
    yield f
    f.close()


@pytest.fixture
def native():
    f = FakeOpn("24.7")
    yield f
    f.close()


@pytest.fixture
def plain():
    f = FakeOpn("26.7")
    yield f
    f.close()


def writer(fake, clk, **kw):
    kw.setdefault("settle_s", 0)
    return OpnsenseWriter(url=fake.url, write_api_key=fx.WRITE_KEY, write_api_secret=fx.WRITE_SECRET, clock=clk.time,
                          sleep=clk.sleep, **kw)


def z(ident):
    return model.zone_id(ident)


def pid(uuid, src, dst):
    return model.policy_id(uuid, z(src) if src != "*" else "*", z(dst) if dst != "*" else "*")


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


def toggle(uuid=U["iot_lan"], src="opt1", dst="lan", on=False):
    return {"kind": "policy.update", "object": pid(uuid, src, dst), "before": {"enabled": not on}, "after": {"enabled": on}}


def create(src="opt2", dst="opt3", **after):
    a = dict({"name": "Guests may not reach IoT", "src": {"zone": z(src)}, "dst": {"zone": z(dst) if dst != "*" else "*"},
              "action": "block"}, **after)
    return {"kind": "policy.create", "object": "new:0a1b2c3d4e5f", "before": {}, "after": a}


def posts(fake):
    return [p for m, p in fake.writes()]


# ---------------------------------------------------------------------------- the allow-list below the adapter
GOOD_RULE = {"enabled": "1", "action": "block", "quick": "1", "interface": "opt2", "direction": "in", "ipprotocol": "inet",
             "protocol": "TCP", "source_net": "opt2", "source_not": "0", "destination_net": "opt3", "destination_not": "0",
             "destination_port": "8000:8080", "description": "[lanloom] x", "sequence": "91"}
F = "/api/firewall/filter"


@pytest.mark.parametrize("method,path,body", [
    ("POST", F + "/addRule", {"rule": GOOD_RULE}),
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, destination_net="(self)", destination_port="", protocol="any")}),
    ("POST", F + "/setRule/" + U["iot_lan"], {"rule": {"enabled": "0"}}),
    ("POST", F + "/setRule/" + U["iot_lan"], {"rule": {"description": "any text", "action": "pass", "destination_net": "any"}}),
    ("POST", F + "/delRule/" + U["own_block"], {}),
    ("POST", F + "/savepoint", {}),
    ("POST", F + "/apply", {}),
    ("POST", F + "/apply/" + REV, {}),
    ("POST", F + "/cancelRollback/" + REV, {}),
    ("GET", F + "/get", None),
    ("GET", F + "/getRule/" + U["lan_any"], None),
    ("GET", "/api/interfaces/overview/interfacesInfo", None),
])
def test_allow_list_lets_the_planned_requests_through(method, path, body):
    wmod.ALLOW.check(method, path, body)


@pytest.mark.parametrize("method,path,body", [
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, description="no mark")}),           # a new rule must carry the mark
    ("POST", F + "/addRule", {"rule": {k: v for k, v in GOOD_RULE.items() if k != "sequence"}}),   # incomplete
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, log="1")}),                           # a field outside the list
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, direction="out")}),
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, destination_net="10.0.0.0/8")}),      # addresses/aliases: never
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, destination_net="lan,opt1")}),
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, destination_port="80,443")}),
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, interface="lan,opt1")}),
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, source_not="1")}),
    ("POST", F + "/addRule", {"rule": dict(GOOD_RULE, description="[lanloom] a\nb")}),
    ("POST", F + "/addRule", {"rule": GOOD_RULE, "extra": 1}),
    ("POST", F + "/setRule/" + U["iot_lan"], {"rule": {}}),
    ("POST", F + "/setRule/" + U["iot_lan"], {"rule": {"gateway": "WAN_DHCP"}}),
    ("POST", F + "/setRule/" + U["iot_lan"], {"rule": {"direction": "out"}}),
    ("POST", F + "/setRule/not-a-uuid", {"rule": {"enabled": "0"}}),
    ("POST", F + "/delRule/" + U["own_block"], {"x": 1}),
    ("POST", F + "/toggleRule/" + U["iot_lan"], {}),
    ("POST", F + "/revert/" + REV, {}),
    ("POST", F + "/apply/../x", {}),
    ("POST", "/api/core/firmware/update", {}),
    ("POST", "/api/firewall/alias/addItem", {"alias": {}}),
    ("POST", "/api/firewall/source_nat/addRule", {"rule": GOOD_RULE}),
    ("GET", "/api/core/system/reboot", None),
    ("PUT", F + "/setRule/" + U["iot_lan"], {"rule": {"enabled": "0"}}),
])
def test_allow_list_refuses_everything_else(method, path, body):
    with pytest.raises(transport.Forbidden):
        wmod.ALLOW.check(method, path, body)


def test_the_module_has_exactly_one_send_path():
    src = Path(wmod.__file__).read_text()
    tree = ast.parse(src)
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert not mods & {"socket", "http", "urllib", "ssl", "requests", "aiohttp", "subprocess"}
    for word in ("HTTPConnection", "HTTPSConnection", "urlopen", "create_connection", "eval(", "exec(", "__import__"):
        assert word not in src


def test_a_refused_request_never_leaves_the_process(native, clk):
    w = writer(native, clk)
    with pytest.raises(WriteError) as e:
        w._post(F + "/addRule", {"rule": dict(GOOD_RULE, description="no mark")})
    assert e.value.key == "opnsense.refused_by_allow_list" and not native.writes()


def test_options_are_checked(native, clk):
    with pytest.raises(ValueError):
        OpnsenseWriter(url=native.url, write_api_key=fx.WRITE_KEY, write_api_secret=fx.WRITE_SECRET, nope=1)
    with pytest.raises(ValueError):
        OpnsenseWriter(url=native.url, write_api_key="short", write_api_secret=fx.WRITE_SECRET)
    with pytest.raises(ValueError):
        OpnsenseWriter(url="ftp://x", write_api_key=fx.WRITE_KEY, write_api_secret=fx.WRITE_SECRET)
    w = writer(native, clk)
    assert fx.WRITE_SECRET not in repr(w) and fx.WRITE_SECRET not in str(w) and w.endpoints() == ["127.0.0.1"]


def test_read_is_get_only_and_capabilities_follow_the_release(fake, clk):
    w = writer(fake, clk)
    st = w.read()
    assert not fake.writes() and st["controller"]["kind"] == "firewall" and st["policies"]
    caps = w.capabilities()
    assert set(caps["write"]) == {"policy.create", "policy.update", "policy.delete"}
    assert caps["rollback"]["device_native"] is (fake.version == "24.7") and caps["api"]["version"]
    assert w.health() == (True, "")


def test_a_refused_write_key_makes_the_half_unhealthy(native, clk):
    w = OpnsenseWriter(url=native.url, write_api_key=fx.WRITE_KEY, write_api_secret=fx.API_SECRET, clock=clk.time, sleep=clk.sleep)
    assert w.health()[0] is False


# ---------------------------------------------------------------------------- one change, both releases
def test_switching_a_rule_off_with_the_firewalls_way_back(native, clk, tmp_path):
    res, w, _ = run(native, clk, tmp_path, [toggle()])
    assert res["status"] == "done", res
    assert posts(native) == [F + "/savepoint", F + "/setRule/<uuid>", F + "/apply/<rev>", F + "/cancelRollback/<rev>"]
    assert native.config[U["iot_lan"]]["enabled"] == "0" and native.live[U["iot_lan"]]["enabled"] == "0"
    assert native.armed is None                                       # confirmed: the firewall's timer is stopped
    native.expire()
    assert native.config[U["iot_lan"]]["enabled"] == "0"             # nothing to revert after the confirmation


def test_switching_a_rule_off_on_a_release_without_savepoint(plain, clk, tmp_path):
    res, w, _ = run(plain, clk, tmp_path, [toggle()])
    assert res["status"] == "done", res
    assert posts(plain) == [F + "/setRule/<uuid>", F + "/apply"]     # no savepoint asked: the release has none
    assert plain.live[U["iot_lan"]]["enabled"] == "0"
    assert w.capabilities()["rollback"]["device_native"] is False


@pytest.mark.parametrize("version,native_way", [("24.7", True), ("26.7", False)])
def test_unknown_release_asks_for_a_savepoint_and_takes_the_answer(version, native_way, clk, tmp_path):
    f = FakeOpn(version)
    try:
        for p in ("/api/diagnostics/system/systemInformation", "/api/core/firmware/status"):
            f.replies.pop(p)
        res, w, _ = run(f, clk, tmp_path, [toggle()])
        assert res["status"] == "done", res
        assert (F + "/savepoint") in posts(f)
        assert (F + "/apply/<rev>" in posts(f)) is native_way and (F + "/apply" in posts(f)) is not native_way
        assert w.capabilities()["rollback"]["device_native"] is native_way
    finally:
        f.close()


def test_the_firewall_reverts_on_its_own_when_the_executor_never_confirms(native, clk, tmp_path):
    """The executor dies right after apply/<revision>: the firewall's 60 second timer puts the rules back, and the
    executor's recovery afterwards finds nothing left to write."""
    w = writer(native, clk)
    w.read()
    step = toggle()
    snap = w.snapshot([step["object"]])
    assert snap["device_native"] and snap["revision"]
    w.apply(step)
    assert native.live[U["iot_lan"]]["enabled"] == "1"                # staged in the configuration, not live yet

    class Died(Exception):
        pass

    def die():
        raise Died()
    w.read = die
    with pytest.raises(Died):
        w.settle([step])
    assert native.armed == snap["revision"] and native.live[U["iot_lan"]]["enabled"] == "0"
    native.expire()                                                   # 60 s without confirmation
    assert native.config[U["iot_lan"]]["enabled"] == "1" and native.live[U["iot_lan"]]["enabled"] == "1"
    fresh = writer(native, clk)
    native.requests.clear()
    fresh.restore(snap)
    assert posts(native) == [F + "/apply"]                            # nothing differs any more: only the apply


def test_a_check_that_fails_after_apply_stops_the_timer_and_writes_back(native, clk, tmp_path):
    w = writer(native, clk)
    orig = w._missing
    w._missing = lambda steps: ["x"]                                   # the read-back does not show the change
    res, w, _ = run(native, clk, tmp_path, [toggle()], w=w)
    w._missing = orig
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed", res
    p = posts(native)
    assert p.index(F + "/apply/<rev>") < p.index(F + "/cancelRollback/<rev>") < len(p) - 1 and p[-1] == F + "/apply"
    assert native.armed is None and native.config[U["iot_lan"]]["enabled"] == "1" and native.live[U["iot_lan"]]["enabled"] == "1"


# ---------------------------------------------------------------------------- create, change, delete
def test_create_appends_a_marked_rule_and_returns_its_id(fake, clk, tmp_path):
    res, w, _ = run(fake, clk, tmp_path, [create()])
    assert res["status"] == "done", res
    new = [u for u in fake.config if u not in fx.rules()]
    assert len(new) == 1
    r = fake.config[new[0]]
    assert r["description"] == "[lanloom] Guests may not reach IoT" and r["sequence"] == "91"
    assert model.values(r["interface"]) == ["opt2"] and model.values(r["destination_net"]) == ["opt3"]
    assert model.values(r["source_net"]) == ["opt2"] and model.value(r["action"]) == "block"
    st = w.read()
    p = next(x for x in st["policies"] if x["vendor_ref"] == new[0])
    assert p["own"] and p["editable"] and p["name"] == "Guests may not reach IoT"
    assert res["inverse"][0]["kind"] == "policy.delete" and res["inverse"][0]["object"] == p["id"]


def test_create_with_port_range_to_the_firewall_itself(native, clk, tmp_path):
    st = create(src="opt1", dst="self", name="IoT may ask for names", action="allow", proto="tcp_udp", ports="53")
    res, w, _ = run(native, clk, tmp_path, [st])
    assert res["status"] == "done", res
    new = next(u for u in native.config if u not in fx.rules())
    r = native.config[new]
    assert model.values(r["destination_net"]) == ["(self)"] and r["destination_port"] == "53"
    assert model.value(r["protocol"]) == "TCP/UDP"


def test_change_name_action_and_target_of_a_foreign_simple_rule(native, clk, tmp_path):
    step = {"kind": "policy.update", "object": pid(U["iot_lan"], "opt1", "lan"),
            "before": {"name": "IoT may not reach LAN", "action": "block"},
            "after": {"name": "IoT may not reach LAN (reject)", "action": "reject"}}
    res, w, _ = run(native, clk, tmp_path, [step])
    assert res["status"] == "done", res
    r = native.config[U["iot_lan"]]
    assert r["description"] == "IoT may not reach LAN (reject)" and model.value(r["action"]) == "reject"
    assert native.requests[[q["path"] for q in native.requests].index(F + "/setRule/" + U["iot_lan"])]["body"] == \
        {"rule": {"description": "IoT may not reach LAN (reject)", "action": "reject"}}


def test_delete_an_own_rule_and_undo_brings_it_back(fake, clk, tmp_path):
    oid = pid(U["own_block"], "opt2", "opt3")
    w = writer(fake, clk)
    before = {k: v for k, v in next(p for p in w.read()["policies"] if p["id"] == oid).items()
              if k in ("name", "src", "dst", "action", "proto", "ports", "enabled")}
    res, w, r = run(fake, clk, tmp_path, [{"kind": "policy.delete", "object": oid, "before": before, "after": {}}], w=w)
    assert res["status"] == "done", res and U["own_block"] not in fake.config
    undo = res["inverse"]
    assert undo[0]["kind"] == "policy.create"
    res2 = r.run(make_plan(w, clk, undo))
    assert res2["status"] == "done", res2
    back = [x for x in fake.config.values() if x["description"] == "[lanloom] Guests may not reach the DMZ"]
    assert len(back) == 1 and model.value(back[0]["action"]) == "block"


def test_steps_are_idempotent(native, clk):
    w = writer(native, clk)
    w.read()
    native.config[U["iot_lan"]]["enabled"] = "0"
    assert w.apply(toggle()) is None and not native.writes()


# ---------------------------------------------------------------------------- refusals
def test_a_foreign_rule_is_never_deleted(native, clk, tmp_path):
    oid = pid(U["iot_lan"], "opt1", "lan")
    res, _, _ = run(native, clk, tmp_path, [{"kind": "policy.delete", "object": oid,
                                             "before": {"name": "IoT may not reach LAN", "action": "block"}, "after": {}}])
    assert res["status"] == "refused" and res["reason"] == "fw.block.not_own", res
    assert not native.writes()


@pytest.mark.parametrize("step,reason", [
    (create(src="homenets"), "fw.block.capability"),                       # a group is no place for a rule
    (create(dst="wan"), "fw.block.capability"),                            # a target on the internet side
    (create(dst="*"), "fw.block.format"),                                  # "any" is no zone a plan can name
    (create(ports="80,443", proto="tcp"), "fw.block.capability"),          # one port or one range only
    (create(src="lan", dst="self", name="x"), "zf.block.mgmt_rule"),       # never block the firewall itself
    ({"kind": "policy.update", "object": pid(U["cam_in"], "wan", "opt3"), "before": {"enabled": True},
      "after": {"enabled": False}}, "fw.block.builtin"),                    # not a simple rule (alias, address)
    ({"kind": "port.set", "object": "x1", "before": {"poe": True}, "after": {"poe": False}}, "fw.block.capability"),
])
def test_lockout_rules(native, clk, tmp_path, step, reason):
    res, _, _ = run(native, clk, tmp_path, [step])
    assert res["status"] == "refused" and res["reason"] == reason, res
    assert not native.writes()


def test_classic_rules_are_never_written(plain, clk, tmp_path):
    from lanloom.integrations.opnsense.read import OpnsenseReader
    r = OpnsenseReader(url=plain.url, api_key=fx.API_KEY, api_secret=fx.API_SECRET)
    classic = [p for p in r.config_event(r.collect())["state"]["policies"] if p["builtin"]]
    w = writer(plain, clk)
    w.read()
    assert classic and all(p["id"] not in w._refs for p in classic) and w._refs
    step = {"kind": "policy.update", "object": classic[0]["id"], "before": {"enabled": True}, "after": {"enabled": False}}
    res, _, _ = run(plain, clk, tmp_path, [step], w=w)
    assert res["status"] == "refused" and res["reason"] == "fw.block.unknown" and not plain.writes()


# ---------------------------------------------------------------------------- the way back
def test_a_change_on_the_firewall_in_between_is_refused_and_nothing_goes_live(fake, clk, tmp_path):
    def other_admin(f):
        f.config[U["iot_lan"]]["enabled"] = "0"
    fake.on_write = None
    w = writer(fake, clk)
    second = pid(U["own_block"], "opt2", "opt3")
    plan = make_plan(w, clk, [toggle(on=False), {"kind": "policy.update", "object": second,
                                                 "before": {"name": "Guests may not reach the DMZ"},
                                                 "after": {"name": "Guests never reach the DMZ"}}])
    orig = w._update

    def update(step):
        if step["object"] == second:
            fake.config[U["own_block"]]["description"] = "[lanloom] renamed meanwhile"
        return orig(step)
    w._update = update
    r = Runner(w, tmp_path / "ex", target="controller", require_signature=False, clock=clk.time, sleep=clk.sleep, watch_s=0)
    res = r.run(plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed", res
    assert fake.config[U["iot_lan"]]["enabled"] == "1"                     # the first step was written back
    assert F + "/apply/<rev>" not in posts(fake)                           # nothing went live half-way
    assert fake.armed is None


def test_a_lost_answer_on_create_leaves_no_stray_rule(fake, clk, tmp_path):
    fake.lose_answer = True
    res, w, _ = run(fake, clk, tmp_path, [create()])
    assert res["status"] == "rolled_back", res
    assert not [u for u in fake.config if u not in fx.rules()]
    assert fake.live == fake.config


def test_watch_puts_back_a_rule_someone_flipped_after_the_change(native, clk, tmp_path):
    w = writer(native, clk)
    calls = {"n": 0}
    orig = w.probe

    def probe(steps):
        calls["n"] += 1
        if calls["n"] == 1:
            native.config[U["iot_lan"]]["enabled"] = "1"
        return orig(steps)
    w.probe = probe
    res, w, _ = run(native, clk, tmp_path, [toggle()], w=w, watch_s=10)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.rolled_back_device", res
    assert native.config[U["iot_lan"]]["enabled"] == "1" and posts(native)[-1] == F + "/apply"


def test_two_failed_reads_while_watching_roll_everything_back(native, clk, tmp_path):
    w = writer(native, clk)
    orig = w.probe
    state = {"n": 0}

    def probe(steps):
        state["n"] += 1
        native.fail_next = {"/api/firewall/filter/get": 500}
        return orig(steps)
    w.probe = probe
    res, w, _ = run(native, clk, tmp_path, [toggle()], w=w, watch_s=10)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.rolled_back_global", res
    assert native.config[U["iot_lan"]]["enabled"] == "1"


def test_restore_after_a_crash_writes_every_saved_field_back(native, clk):
    w = writer(native, clk)
    w.read()
    snap = w.snapshot([pid(U["iot_lan"], "opt1", "lan")])
    native.config[U["iot_lan"]]["enabled"] = "0"
    native.config[U["iot_lan"]]["description"] = "half written"
    fresh = writer(native, clk)
    fresh.restore(json.loads(json.dumps(snap)))
    r = native.config[U["iot_lan"]]
    assert r["enabled"] == "1" and r["description"] == "IoT may not reach LAN"


def test_writes_are_json_and_carry_only_the_write_key(native, clk, tmp_path):
    run(native, clk, tmp_path, [toggle()])
    w_reqs = [q for q in native.requests if q["method"] == "POST"]
    assert w_reqs and all(q["key"] == fx.WRITE_KEY and q["content_type"] == "application/json" for q in w_reqs)
    assert fx.API_KEY not in json.dumps(native.requests)
