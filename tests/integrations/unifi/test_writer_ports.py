"""The write half of the UniFi family, stage B switch ports: name, profile, untagged and tagged networks, power over
Ethernet and power cycling - against the writable fake (console and classic style), alone and inside the executor
pipeline. The override list goes back exactly as read except for the planned port, the allow-list below the adapter,
infrastructure ports stay untouched, power changes are confirmed, a failure mid-plan puts every port back."""
import copy

import pytest

from lanloom.firewall import model as fwmodel
from lanloom.firewall import portconf
from lanloom.firewall.runner import Runner
from lanloom.integrations import transport
from lanloom.integrations.credentials import Secret
from lanloom.integrations.unifi import model
from lanloom.integrations.unifi import write as wmod
from lanloom.integrations.unifi.write import UnifiWriter, WriteError

from . import fixtures as fx
from .fake_writer import D_SW, PW, USER, WritableFake

LAN = model.oid("n", fx.O_NET_LAN)
LAB = model.oid("n", fx.C_NET_LAB)
GUEST = model.oid("n", fx.O_NET_GUEST)
WAN = model.oid("n", fx.O_NET_WAN)
PROFILE = model.oid("pp", fx.C_PROFILE)


def port_id(idx):
    return model.oid("p", "%s:%s" % (fx.MAC_SW, idx))


@pytest.fixture(params=["console", "classic"])
def fake(request):
    f = WritableFake(style=request.param)
    yield f
    f.stop()


def writer(fake, tmp_path, **kw):
    return UnifiWriter(url=fake.url, write_username=USER, write_password=Secret(PW), settle_s=0,
                       state_dir=str(tmp_path), sleep=lambda s: None, **kw)


def runner(w, tmp_path, **kw):
    return Runner(w, tmp_path / "ex", target="controller", require_signature=False, watch_s=0, check_s=0,
                  sleep=lambda s: None, **kw)


def preview(w, ops):
    st = w.read()
    return st, portconf.preview(st, portconf.clean_ops(st, ops), lockout=lambda s: w.lockout(st, s))


def plan_from(st, steps, confirmed=()):
    base = fwmodel.preview(st, {})
    base.update(steps=steps, warnings=[], costs={"objects": len(steps)})
    return fwmodel.build_plan(st, base, target="controller", user="admin", confirmed=list(confirmed))


def switch(fake):
    return fake.device(D_SW)


def override(fake, idx):
    return next((o for o in switch(fake).get("port_overrides") or [] if o.get("port_idx") == idx), None)


# ---------------------------------------------------------------------------- reading
def test_ports_read_with_profiles_networks_and_power(fake, tmp_path):
    st = writer(fake, tmp_path).read()
    ports = {p["idx"]: p for p in st["ports"]}
    assert ports[2]["profile"] == PROFILE and ports[2]["profile_name"] == "Office ports"
    assert ports[2]["native_network"] == LAN and ports[2]["tagged_mode"] == "all"          # from the profile
    assert ports[4]["name"] == "Lab desk" and ports[4]["native_network"] == LAB and ports[4]["tagged_mode"] == "none"
    assert ports[3]["infra"] is False and portconf.infra_reason(st, ports[3]) == "pc.block.infra"   # the access point's uplink
    assert ports[5]["poe"]["on"] is False and ports[5]["poe"]["mode"] == "off"
    assert ports[6]["poe"]["mode"] == "pasv24"
    assert ports[8]["is_uplink"] and ports[8]["lldp"]
    assert [p["name"] for p in st["port_profiles"]] == ["Office ports"]
    assert st["port_profiles"][0]["native_network"] == LAN and st["port_profiles"][0]["tagged_mode"] == "all"
    assert "port.set.native_network" in w_caps() and w_caps()["port.set.poe"] == "B*" and w_caps()["port.cycle"] == "B*"


def w_caps():
    from lanloom.integrations.unifi import WRITE_CAPABILITIES
    return WRITE_CAPABILITIES["write"]


# ---------------------------------------------------------------------------- writing
def test_name_and_untagged_network_through_the_runner(fake, tmp_path):
    w = writer(fake, tmp_path)
    keep3, orig4 = copy.deepcopy(override(fake, 3)), copy.deepcopy(override(fake, 4))
    st, pv = preview(w, [{"kind": "port.set", "object": port_id(4), "after": {"name": "Lab bench", "native_network": LAN}}])
    assert not pv["blocks"], pv["blocks"]
    assert any(x[0] == "pc.warn.native" and x[1]["n"] == 2 for x in pv["warnings"])     # two devices share the port
    assert any(x[0] == "pc.warn.shared" for x in pv["warnings"])
    # the default network holds the gateway and the switches: whatever is plugged in reaches their admin pages
    assert pv["confirm"] == ["confirm:port.set:%s" % port_id(4)] and any(x[0] == "pc.warn.to_management" for x in pv["warnings"])
    res = runner(w, tmp_path).run(plan_from(st, pv["steps"], confirmed=pv["confirm"]))
    assert res["status"] == "done", res
    o4 = override(fake, 4)
    assert o4["name"] == "Lab bench" and o4["native_networkconf_id"] == fx.C_NET_LAN and o4["forward"] == "native"
    assert {k: v for k, v in o4.items() if k not in ("name", "native_networkconf_id", "forward")} == \
        {k: v for k, v in orig4.items() if k not in ("name", "native_networkconf_id", "forward")}
    assert override(fake, 3) == keep3                                   # the other port goes back exactly as read
    assert all(set(body) == {"port_overrides"} for _, body in fake.device_puts)
    assert res["inverse"][0]["kind"] == "port.set" and res["inverse"][0]["after"]["native_network"] == LAB


def test_a_choice_of_tagged_networks_writes_the_excluded_list(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [{"kind": "port.set", "object": port_id(5), "after": {"tagged": [LAB]}}])
    assert not pv["blocks"], pv["blocks"]
    assert pv["steps"][0]["after"] == {"tagged_mode": "custom", "tagged": [LAB]}
    assert runner(w, tmp_path).run(plan_from(st, pv["steps"]))["status"] == "done"
    o5 = override(fake, 5)                                              # a port without an entry gets one
    assert o5["tagged_vlan_mgmt"] == "custom" and o5["excluded_networkconf_ids"] == [fx.C_NET_GUEST]
    port = {p["idx"]: p for p in w.read()["ports"]}[5]
    assert port["tagged_mode"] == "custom" and port["tagged"] == [LAB]


def test_a_profile_decides_the_networks(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [{"kind": "port.set", "object": port_id(4), "after": {"profile": PROFILE, "native_network": LAN}}])
    assert pv["steps"][0]["after"] == {"profile": PROFILE}               # the network field of the same draft is dropped
    assert any(x[0] == "pc.warn.profile" and x[1]["profile"] == "Office ports" for x in pv["warnings"])
    assert any(x[0] == "pc.warn.native" for x in pv["warnings"])          # the profile moves the port to the default network
    assert runner(w, tmp_path).run(plan_from(st, pv["steps"], confirmed=pv["confirm"]))["status"] == "done"
    assert override(fake, 4)["portconf_id"] == fx.C_PROFILE
    back = runner(w, tmp_path / "2").run(plan_from(w.read(), [{"kind": "port.set", "object": port_id(4),
                                                                "before": {"profile": PROFILE}, "after": {"profile": ""}}]))
    assert back["status"] == "done" and "portconf_id" not in override(fake, 4)


def test_power_off_is_confirmed_and_a_failure_puts_every_port_back(fake, tmp_path):
    w = writer(fake, tmp_path)
    before = copy.deepcopy(switch(fake)["port_overrides"])
    st, pv = preview(w, [{"kind": "port.set", "object": port_id(2), "after": {"poe": False}},
                         {"kind": "port.set", "object": port_id(4), "after": {"name": "Lab bench"}}])
    token = "confirm:port.set:%s" % port_id(2)
    assert pv["confirm"] == [token]
    assert any(x[0] == "pc.warn.poe_off" and "Office PC" in x[1]["devices"] for x in pv["warnings"])
    refused = runner(w, tmp_path).run(plan_from(st, pv["steps"]))
    assert refused["status"] == "refused" and refused["reason"] == "fw.block.confirm"
    fake.fail_device = 2                                                # the second switch write fails
    res = runner(w, tmp_path / "2").run(plan_from(w.read(), pv["steps"], confirmed=[token]))
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert switch(fake)["port_overrides"] == before                     # port 2 lost its new entry again


def test_a_repeated_step_writes_nothing(fake, tmp_path):
    w = writer(fake, tmp_path)
    step = {"kind": "port.set", "object": port_id(5), "before": {"poe": False}, "after": {"poe": True}}
    w.read()
    w.apply(step)
    assert override(fake, 5)["poe_mode"] == "auto"
    n = len(fake.device_puts)
    w.read()
    w.apply(step)
    assert len(fake.device_puts) == n


def test_power_cycle_is_one_command_without_a_way_back(fake, tmp_path):
    w = writer(fake, tmp_path)
    st = w.read()
    step = {"kind": "port.cycle", "object": port_id(5), "before": {}, "after": {"cycle": True}}
    assert runner(w, tmp_path).run(plan_from(st, [step]))["reason"] == "fw.block.confirm"
    res = runner(w, tmp_path / "2").run(plan_from(w.read(), [step], confirmed=["confirm:port.cycle:%s" % port_id(5)]))
    assert res["status"] == "done" and res["inverse"] == []
    assert fake.cycles == [(fx.MAC_SW, 5)] and not fake.device_puts


@pytest.mark.parametrize("idx,after,why", [
    (8, {"native_network": LAB}, "fw.block.uplink"),
    (3, {"poe": False}, "pc.block.infra"),                    # the access point hangs here
    (3, {"name": "AP hall"}, None),                           # a name always
    (4, {"poe": True}, "pc.block.no_poe"),
    (6, {"poe": True}, "pc.block.poe_mode"),                  # passive power is left alone
    (5, {"native_network": WAN}, "fw.block.network_kind"),
    (5, {"profile": model.oid("pp", "5f0000000000000000000299")}, "fw.block.unknown"),
    (5, {"tagged": [GUEST]}, None),                           # a guest network with a VLAN may be carried
])
def test_family_and_neutral_lockout(fake, tmp_path, idx, after, why):
    w = writer(fake, tmp_path)
    st = w.read()
    port = next(p for p in st["ports"] if p["idx"] == idx)
    step = {"kind": "port.set", "object": port["id"], "before": {f: portconf._cur(port, f) for f in after}, "after": after}
    if "tagged" in after:
        step["before"]["tagged_mode"], step["after"]["tagged_mode"] = port["tagged_mode"], "custom"
    assert (fwmodel.lockout_reason(st, step) or w.lockout(st, step)) == why


def test_cycle_of_a_port_without_power_is_refused(fake, tmp_path):
    w = writer(fake, tmp_path)
    st = w.read()
    assert w.lockout(st, {"kind": "port.cycle", "object": port_id(4), "before": {}, "after": {"cycle": True}}) == "pc.block.no_poe"


# ---------------------------------------------------------------------------- below the adapter
def test_allow_list_takes_only_the_override_list():
    good = [{"port_idx": 3, "name": "Port 3", "poe_mode": "auto", "port_security_mac_address": [], "stormctrl_bcast_rate": 100}]
    assert wmod._overrides(good)
    for bad in ([{"port_idx": 3}, {"port_idx": 3}],                       # one entry per port
                [{"port_idx": 0}], [{"port_idx": True}],
                [{"port_idx": 3, "poe_mode": "on"}],
                [{"port_idx": 3, "native_networkconf_id": "../x"}],
                [{"port_idx": 3, "Name": "x"}],
                [{"port_idx": 3, "name": "x\n"}],
                [{"port_idx": 3, "deep": {"a": {"b": 1}}}],
                [{"port_idx": i} for i in range(1, 66)]):
        assert not wmod._overrides(bad), bad
    def allows(method, path, body):
        try:
            wmod.ALLOW.check(method, path, body)
            return True
        except transport.Forbidden:
            return False
    assert allows("PUT", "/proxy/network/api/s/default/rest/device/%s" % D_SW, {"port_overrides": good})
    assert not allows("PUT", "/proxy/network/api/s/default/rest/device/%s" % D_SW, {"port_overrides": good, "name": "x"})
    assert not allows("POST", "/api/s/default/cmd/devmgr", {"cmd": "restart", "mac": fx.MAC_SW, "port_idx": 3})
    assert allows("POST", "/api/s/default/cmd/devmgr", {"cmd": "power-cycle", "mac": fx.MAC_SW, "port_idx": 3})


def test_body_check_refuses_anything_but_the_port_settings(fake, tmp_path):
    w = writer(fake, tmp_path)
    w.read()
    ref = w._ports[port_id(4)]
    entry = dict(copy.deepcopy(ref["override"]), isolation=True)          # not a field this half may change
    with pytest.raises(WriteError) as e:
        w._overrides_with(ref, {4: entry})
    assert e.value.key == "unifi.body_check"
    ok = w._overrides_with(ref, {4: dict(copy.deepcopy(ref["override"]), name="Bench")})
    assert [o["port_idx"] for o in ok] == [3, 4]


def test_the_executor_keeps_its_own_port(fake, tmp_path, monkeypatch):
    w = writer(fake, tmp_path)
    st = w.read()
    monkeypatch.setattr(w, "self_address", lambda: "192.168.50.20")      # the executor runs on the office PC
    step = {"kind": "port.set", "object": port_id(2), "before": {"poe": True}, "after": {"poe": False}}
    assert w.lockout(st, step) == "pc.block.self_path"


def test_transport_refuses_a_body_the_builder_never_makes(fake, tmp_path):
    w = writer(fake, tmp_path)
    w.read()
    with pytest.raises(WriteError) as e:
        w._put_overrides(D_SW, [{"port_idx": 3, "poe_mode": "on"}])
    assert e.value.key == "unifi.refused_by_allow_list"
