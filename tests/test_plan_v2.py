"""Plan steps v2: shapes, capabilities, the gate, the executor pipeline with creates/updates/deletes, and that the
v1 path (moves, simulation) stays byte-identical to earlier releases. Nothing here touches a network."""
import copy
import json

import pytest

from lanloom.firewall import adapter, canon, model, neutral, runner, steps
from lanloom.integrations import capabilities as capmod

ALL_WRITE = {"move": "B", "policy.create": "B", "policy.update": "B", "policy.delete": "B", "port.set": "B",
             "port.set.poe": "B*", "network.update": "B", "network.delete": "B", "client.block": "B*",
             "host.bridge": "C", "ssid.update": "B"}
CAPS = {"family": "sample", "api": {"kind": "simulation", "version": "1"}, "read": ["networks", "zones", "ports", "policies"],
        "write": ALL_WRITE}


def v2_state():
    st = adapter.sample_state()
    st["ports"] = [{"id": "p_sw_1", "device": "d02", "idx": 1, "name": "Uplink", "is_uplink": True, "profile": "all",
                    "native_vlan": 1, "tagged": [10, 30], "poe": False, "link": True, "speed": 1000},
                   {"id": "p_sw_7", "device": "d02", "idx": 7, "name": "Garage", "is_uplink": False, "profile": "all",
                    "native_vlan": 1, "tagged": [], "poe": True, "link": True, "speed": 100}]
    st["policies"] = [{"id": "pol_1", "name": "Cameras stay inside", "src": {"zone": "z_cameras"}, "dst": {"zone": "z_internal"},
                       "action": "block", "proto": "any", "ports": "", "return_traffic": True, "enabled": True, "order": 10,
                       "hits": 5},
                      {"id": "pol_own", "name": "Lanloom rule", "src": {"zone": "z_work"}, "dst": {"zone": "z_devices"},
                       "action": "allow", "proto": "tcp", "ports": "443", "return_traffic": True, "enabled": True,
                       "order": 20, "own": True},
                      {"id": "pol_builtin", "name": "Default", "src": {"zone": "z_guests"}, "dst": {"zone": "z_internal"},
                       "action": "block", "proto": "any", "ports": "", "return_traffic": False, "enabled": True,
                       "order": 999, "builtin": True}]
    return st


def plan(st, step_list, confirmed=(), target="simulation", now=1_000_000):
    pv = {"base": canon.fingerprint(st), "steps": step_list, "warnings": [], "costs": {"objects": len(step_list)}}
    return model.build_plan(st, pv, target=target, user="admin", confirmed=list(confirmed), now=now)


def gate(st, p, caps=CAPS, now=1_000_000):
    model.gate(st, p, target="simulation", now=now, quiet=False, busy=False, healthy=(True, ""), caps=caps)


def port_step(field="native_vlan", before=1, after=30, obj="p_sw_7"):
    return {"kind": "port.set", "object": obj, "before": {field: before}, "after": {field: after}}


# ---------------------------------------------------------------------------- v1 stays byte-identical
def test_sample_network_fingerprint_and_v1_plan_unchanged():
    st = adapter.sample_state()
    assert canon.fingerprint(st) == "fp:0941f8f5056289b07ae1"      # the value of release 1.3.0
    pv = model.preview(st, {"d08": "n_work"})
    p = model.build_plan(st, pv, target="simulation", user="admin", confirmed=[], now=5)
    assert p["v"] == 1 and set(p["steps"][0]) == {"kind", "device", "before", "after"}
    assert model.inverse(pv["steps"]) == [{"kind": "move", "device": "d08", "before": pv["steps"][0]["after"],
                                           "after": pv["steps"][0]["before"]}]
    assert adapter.SimulatedController(settle_s=0).snapshot(["d08"]).keys() == {"devices"}


def test_fingerprint_ignores_counters_but_sees_writable_fields():
    st = v2_state()
    fp = canon.fingerprint(st)
    st["policies"][0]["hits"] = 999
    st["ports"][1]["speed"] = 10
    assert canon.fingerprint(st) == fp
    st["ports"][1]["native_vlan"] = 70
    assert canon.fingerprint(st) != fp


# ---------------------------------------------------------------------------- shapes
@pytest.mark.parametrize("step", [
    {"kind": "port.set", "object": "p1", "before": {"native_vlan": 1}, "after": {"native_vlan": 1}},          # unchanged
    {"kind": "port.set", "object": "p1", "before": {"native_vlan": 1}, "after": {"profile": "x"}},            # fields differ
    {"kind": "port.set", "object": "p1", "before": {"speed": 1}, "after": {"speed": 2}},                      # not writable
    {"kind": "port.set", "object": "p1", "before": {"native_vlan": 1}, "after": {"native_vlan": 5000}},       # range
    {"kind": "port.set", "device": "p1", "before": {"native_vlan": 1}, "after": {"native_vlan": 2}},          # key name
    {"kind": "policy.create", "object": "pol_9", "before": {}, "after": {"name": "x", "src": {"zone": "a"}, "dst": {"zone": "b"}, "action": "allow"}},  # not a new id
    {"kind": "policy.create", "object": "new:0123abcd", "before": {}, "after": {"name": "x"}},                 # required missing
    {"kind": "policy.update", "object": "p", "before": {"src": {"zone": "a"}}, "after": {"src": {"zone": "a", "ip": "1.2.3.4"}}},
    {"kind": "policy.delete", "object": "p", "before": {"name": "x"}, "after": {"name": "y"}},
    {"kind": "forward.update", "object": "f", "before": {"wan_port": "80"}, "after": {"wan_port": "80-70000"}},
    {"kind": "ssid.update", "object": "s", "before": {"bands": ["5ghz"]}, "after": {"bands": ["60ghz"]}},
    {"kind": "client.set", "object": "d1", "before": {"name": "a"}, "after": {"name": "b\n"}},
])
def test_bad_steps_are_refused(step):
    with pytest.raises(steps.FieldError):
        steps.check_step(step)


def test_unknown_kind_and_higher_version_are_refused_as_such():
    st = v2_state()
    p = plan(st, [port_step()])
    assert p["v"] == 2
    bad = dict(p, steps=[{"kind": "device.firmware", "object": "d02", "before": {"v": "1"}, "after": {"v": "2"}}])
    with pytest.raises(model.Refused) as e:
        model.validate_plan(bad)
    assert e.value.key == "fw.block.kind"
    with pytest.raises(model.Refused) as e:
        model.validate_plan(dict(p, v=3))
    assert e.value.key == "fw.block.version" and e.value.vars == {"v": 3, "max": 2}
    with pytest.raises(model.Refused):
        model.validate_plan(dict(p, v=True))
    moves_only = model.preview(st, {"d08": "n_work"})["steps"]
    with pytest.raises(model.Refused) as e:                     # a moves-only plan must stay v1
        model.validate_plan(dict(p, steps=moves_only))
    assert e.value.key == "fw.block.format"


def test_no_kind_for_firmware_accounts_vpn_or_mirroring():
    names = " ".join(steps.KINDS)
    for word in ("firmware", "restart", "reboot", "account", "user", "key", "vpn", "wan", "mirror", "factory", "remote"):
        assert word not in names


# ---------------------------------------------------------------------------- the gate
def test_gate_accepts_a_port_change_within_capabilities():
    st = v2_state()
    gate(st, plan(st, [port_step()]))


@pytest.mark.parametrize("caps,key", [
    (dict(CAPS, write={"move": "B"}), "fw.block.capability"),
    (dict(CAPS, write={"port.set.name": "B"}), "fw.block.capability"),   # field not covered
    (dict(CAPS, limits={"objects_per_plan": 0}), None),
])
def test_gate_capabilities(caps, key):
    st = v2_state()
    p = plan(st, [port_step()])
    if key is None:
        gate(st, p, caps=caps)          # 0 = no extra limit beyond the hard one
        return
    with pytest.raises(model.Refused) as e:
        gate(st, p, caps=caps)
    assert e.value.key == key


def test_field_level_capability_covers_only_that_field():
    caps = dict(CAPS, write={"port.set.native_vlan": "B"})
    st = v2_state()
    gate(st, plan(st, [port_step()]), caps=caps)


def test_stage_c_is_refused_whatever_the_adapter_declares():
    st = v2_state()
    st["bridges"] = [{"id": "br0", "host": "h1", "name": "br0", "vlan_aware": False, "ports": []}]
    p = plan(st, [{"kind": "host.bridge", "object": "br0", "before": {"vlan_aware": False}, "after": {"vlan_aware": True}}])
    with pytest.raises(model.Refused) as e:
        gate(st, p)
    assert e.value.key == "fw.block.stage"


@pytest.mark.parametrize("step,key", [
    (port_step(obj="p_sw_1"), "fw.block.uplink"),
    (port_step(before=5), "fw.block.conflict"),                                    # before differs from the fresh read
    (port_step(obj="p_missing"), "fw.block.unknown"),
    ({"kind": "policy.delete", "object": "pol_1", "before": {"name": "Cameras stay inside"}, "after": {}}, "fw.block.not_own"),
    ({"kind": "policy.update", "object": "pol_builtin", "before": {"enabled": True}, "after": {"enabled": False}}, "fw.block.builtin"),
    ({"kind": "policy.update", "object": "pol_own", "before": {"dst": {"zone": "z_devices"}, "action": "allow"},
      "after": {"dst": {"zone": "z_mgmt"}, "action": "block"}}, "zf.block.mgmt_rule"),
    ({"kind": "network.update", "object": "n_mgmt", "before": {"vlan": 10}, "after": {"vlan": 11}}, "fw.block.management"),
    ({"kind": "network.delete", "object": "n_cams", "before": {"name": "Cameras"}, "after": {}}, "fw.block.not_own"),
])
def test_gate_lockout_and_conflicts(step, key):
    st = v2_state()
    with pytest.raises(model.Refused) as e:
        gate(st, plan(st, [step]))
    assert e.value.key == key


def test_confirmation_needed_for_poe_and_blocking():
    st = v2_state()
    poe = port_step("poe", True, False)
    with pytest.raises(model.Refused) as e:
        gate(st, plan(st, [poe]))
    assert e.value.key == "fw.block.confirm"
    gate(st, plan(st, [poe], confirmed=[steps.confirm_token(poe)]))
    block = {"kind": "client.block", "object": "d11", "before": {"blocked": None}, "after": {"blocked": True}}
    with pytest.raises(steps.FieldError):
        steps.check_step(block)             # blocked must be a boolean on both sides
    st["devices"][10]["blocked"] = False
    block["before"]["blocked"] = False
    with pytest.raises(model.Refused) as e:
        gate(st, plan(st, [block]))
    assert e.value.key == "fw.block.confirm"


def test_one_step_per_object_and_hard_limit():
    st = v2_state()
    with pytest.raises(model.Refused) as e:
        gate(st, plan(st, [port_step(), port_step("name", "Garage", "Cam")]))
    assert e.value.key == "fw.block.unknown"
    st["ports"] += [dict(st["ports"][1], id="px%d" % i) for i in range(11)]
    p = plan(st, [port_step(obj="px%d" % i) for i in range(11)])
    with pytest.raises(model.Refused) as e:
        gate(st, p)
    assert e.value.key == "fw.block.too_many"


def test_create_must_not_exist_yet():
    st = v2_state()
    create = {"kind": "policy.create", "object": "new:00aa11bb", "before": {},
              "after": {"name": "Work to printer", "src": {"zone": "z_work"}, "dst": {"zone": "z_internal"}, "action": "allow"}}
    gate(st, plan(st, [create]))
    st["policies"].append({"id": "new:00aa11bb", "name": "x"})
    with pytest.raises(model.Refused):
        gate(st, plan(st, [create]))


# ---------------------------------------------------------------------------- the executor pipeline
def make(tmp_path, st=None, caps=CAPS, **kw):
    sim = adapter.SimulatedController(state=st or v2_state(), settle_s=0, capabilities=caps, **kw)
    events = []
    r = runner.Runner(sim, tmp_path / "state", target="simulation", require_signature=False, watch_s=0, check_s=0,
                      report=events.append, sleep=lambda s: None, clock=lambda: 1_000_000)
    return sim, r, events


def test_run_v2_create_update_delete_and_inverse(tmp_path):
    sim, r, _ = make(tmp_path)
    st = sim.read()
    create = {"kind": "policy.create", "object": "new:00aa11bb", "before": {},
              "after": {"name": "Work to printer", "src": {"zone": "z_work"}, "dst": {"zone": "z_internal"}, "action": "allow"}}
    delete = {"kind": "policy.delete", "object": "pol_own", "before": {"name": "Lanloom rule", "order": 20}, "after": {}}
    with pytest.raises(steps.FieldError):
        steps.check_step(delete)            # order is not a policy field of delete/update (only policy.reorder)
    delete["before"] = {"name": "Lanloom rule", "action": "allow"}
    res = r.run(plan(st, [port_step(), create, delete]))
    assert res["status"] == "done", res
    after = sim.read()
    assert neutral.objects(after, "ports")["p_sw_7"]["native_vlan"] == 30
    assert "pol_own" not in neutral.objects(after, "policies")
    new = [p for p in after["policies"] if p["name"] == "Work to printer"][0]
    inv = res["inverse"]
    assert [s["kind"] for s in inv] == ["policy.create", "policy.delete", "port.set"]
    assert inv[1]["object"] == new["id"]                    # the inverse deletes the id the controller gave it
    assert inv[0]["object"].startswith("new:")


def test_failure_mid_plan_rolls_back_everything_including_created(tmp_path):
    sim, r, _ = make(tmp_path, fail_after_writes=3)
    st = sim.read()
    create = {"kind": "policy.create", "object": "new:00aa11bb", "before": {},
              "after": {"name": "Temp", "src": {"zone": "z_work"}, "dst": {"zone": "z_internal"}, "action": "allow"}}
    delete = {"kind": "policy.delete", "object": "pol_own", "before": {"name": "Lanloom rule"}, "after": {}}
    res = r.run(plan(st, [port_step(), create, delete]))
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    back = sim.read()
    assert neutral.objects(back, "ports")["p_sw_7"]["native_vlan"] == 1
    assert not [p for p in back["policies"] if p["name"] == "Temp"]
    assert "pol_own" in neutral.objects(back, "policies")


def test_crash_after_create_is_recovered_on_next_start(tmp_path):
    sim, r, _ = make(tmp_path)
    st = sim.read()
    create = {"kind": "policy.create", "object": "new:00aa11bb", "before": {},
              "after": {"name": "Temp", "src": {"zone": "z_work"}, "dst": {"zone": "z_internal"}, "action": "allow"}}
    p = plan(st, [create])


    def die(steps_):
        raise SystemExit("power cut")          # not an Exception: the run dies like a killed process
    sim.settle = die
    with pytest.raises(SystemExit):
        r.run(p)
    active = runner.read_private(tmp_path / "state" / "active.json", None)
    assert active["created"]                                   # the new id was recorded before anything else
    sim.settle = lambda s: None
    r2 = runner.Runner(sim, tmp_path / "state", target="simulation", require_signature=False)
    assert r2.recover()["status"] == "rolled_back"
    assert not [x for x in sim.read()["policies"] if x["name"] == "Temp"]


def test_partial_rollback_of_one_object(tmp_path):
    st = v2_state()
    st["ssids"] = [{"id": "s1", "name": "Home", "network": "n_main", "bands": ["5ghz"], "security": "wpa2", "hidden": False,
                    "enabled": True}]
    sim = adapter.SimulatedController(state=st, settle_s=0, capabilities=CAPS, fail_devices={"p_sw_7"})
    r = runner.Runner(sim, tmp_path / "s", target="simulation", require_signature=False, watch_s=10, check_s=0,
                      sleep=lambda s: None, clock=FakeClock())
    ssid = {"kind": "ssid.update", "object": "s1", "before": {"hidden": False}, "after": {"hidden": True}}
    res = r.run(plan(st, [port_step(), ssid]))
    assert res["status"] == "rolled_back" and res["rolled_back"] == ["p_sw_7"]
    now = sim.read()
    assert neutral.objects(now, "ports")["p_sw_7"]["native_vlan"] == 1          # back
    assert neutral.objects(now, "ssids")["s1"]["hidden"] is True                 # kept


class FakeClock:
    """1_000_000 and one second more on every call (the watch loop needs time to pass)."""
    def __init__(self):
        self.t = 1_000_000

    def __call__(self):
        self.t += 1
        return self.t


def test_simulation_capabilities_refuse_v2_steps(tmp_path):
    sim = adapter.SimulatedController(state=v2_state(), settle_s=0)
    r = runner.Runner(sim, tmp_path / "s", target="simulation", require_signature=False, clock=lambda: 1_000_000)
    res = r.run(plan(sim.read(), [port_step()]))
    assert res["status"] == "refused" and res["reason"] == "fw.block.capability"
    assert sim.writes == []


def test_adapter_lockout_hook_adds_rules(tmp_path):
    class Picky(adapter.SimulatedController):
        def lockout(self, state, step):
            return "fw.block.protected" if steps.step_object(step) == "p_sw_7" else None
    sim = Picky(state=v2_state(), settle_s=0, capabilities=CAPS)
    r = runner.Runner(sim, tmp_path / "s", target="simulation", require_signature=False, clock=lambda: 1_000_000)
    res = r.run(plan(sim.read(), [port_step()]))
    assert res["reason"] == "fw.block.protected" and sim.writes == []


# ---------------------------------------------------------------------------- neutral model
def test_neutral_validate():
    st = v2_state()
    neutral.validate(st)
    for bad in ({"devices": []}, dict(st, ports=[{"id": "aa:bb:cc:dd:ee:ff"}]), dict(st, ports=[{"id": "a"}, {"id": "a"}]),
                dict(st, raw=[{"id": "r", "editable": True}]), dict(st, allow=[["a"]])):
        with pytest.raises(neutral.ModelError):
            neutral.validate(bad)


def test_derive_allow_from_policies():
    st = {"zones": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
          "policies": [{"id": "1", "src": {"zone": "a"}, "dst": {"zone": "b"}, "action": "allow", "order": 1},
                       {"id": "2", "src": {"zone": "a"}, "dst": {"zone": "*"}, "action": "block", "order": 2},
                       {"id": "3", "src": {"zone": "b"}, "dst": {"zone": "c"}, "action": "allow", "proto": "tcp", "ports": "22", "order": 0},
                       {"id": "4", "src": {"zone": "c"}, "dst": {"zone": "a"}, "action": "allow", "order": 3, "enabled": False}]}
    assert neutral.derive_allow(st) == [["a", "b"]]
    assert ["c", "b"] in neutral.derive_allow(st, default="allow") and ["a", "c"] not in neutral.derive_allow(st, default="allow")


def test_capability_refine_only_narrows_and_public_hides_stage_c():
    caps = capmod.validate(CAPS)
    narrow = capmod.refine(caps, version="2.0", read_missing=["ports"], write_missing=["port.set"])
    assert "ports" not in narrow["read"] and not any(k.startswith("port.set") for k in narrow["write"])
    assert narrow["api"]["version"] == "2.0"
    pub = capmod.public(caps)
    assert "host.bridge" not in pub["write"] and pub["confirm"] == ["client.block", "port.set.poe"]
    for bad in (dict(CAPS, write={"device.firmware": "B"}), dict(CAPS, write={"move": "A"}), dict(CAPS, read=["everything"]),
                dict(CAPS, limits={"objects_per_plan": 50}), dict(CAPS, extra=1), dict(CAPS, write={"port.set.speed": "B"})):
        with pytest.raises(capmod.CapabilityError):
            capmod.validate(bad)


def test_plan_json_roundtrip_is_canonical():
    st = v2_state()
    p = plan(st, [port_step()])
    assert canon.digest_hex(json.loads(json.dumps(p))) == model.plan_hash(p)
    assert copy.deepcopy(p) == p
