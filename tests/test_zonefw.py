"""Zone firewall (stage B): the pure logic of ``firewall.zonefw`` (may A reach B, rule order, the state after a draft, the
refusal rules and the reachability guard), the sample network that carries zone rules, the executor pipeline against
the simulation, and the editor / API ("may A reach B?" before and after a draft).

Nothing here touches a network: the controller is the built-in simulation or a hand-made neutral state."""
import copy
import time

import pytest
from fastapi.testclient import TestClient

from lanloom import access, config as config_mod
from lanloom.firewall import adapter, model as fwmodel, neutral, netconf, runner as fwrunner, steps as stepkinds, zonefw
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
ORIGIN = "http://lanloom.test"


# ---------------------------------------------------------------------------- helpers
def sample():
    return adapter.SimulatedController(hypervisor=True, settle_s=0).read()


def zone(zid, name, **kw):
    return dict({"id": zid, "name": name, "builtin": False, "internet": False, "discovery": False}, **kw)


def pol(pid, src, dst, order, action="allow", **kw):
    return dict({"id": pid, "name": pid, "src": {"zone": src}, "dst": {"zone": dst}, "action": action, "proto": "any",
                 "ports": "", "return_traffic": False, "enabled": True, "order": order, "builtin": False, "editable": True,
                 "own": True}, **kw)


def mini(*policies):
    """A small v2 state: internal, gateway and one more zone with a network and a device each."""
    return {"zones": [zone("z_int", "Internal", system="internal", builtin=True, internet=True),
                      zone("z_gw", "Gateway", system="gateway", builtin=True),
                      zone("z_a", "Alpha"), zone("z_b", "Beta")],
            "networks": [{"id": "n_int", "name": "Main", "zone": "z_int", "cidr": "192.168.1.0/24", "vlan": 1, "management": False},
                         {"id": "n_a", "name": "Alpha net", "zone": "z_a", "cidr": "192.168.2.0/24", "vlan": 2, "management": False},
                         {"id": "n_b", "name": "Beta net", "zone": "z_b", "cidr": "192.168.3.0/24", "vlan": 3, "management": False}],
            "devices": [{"id": "dev1", "name": "Laptop", "network": "n_a", "ip": "192.168.2.10", "kind": "laptop", "wired": False,
                         "alone_on_port": None, "private_address": False, "protected": None, "fixed_ip": ""},
                        {"id": "ctl", "name": "Controller", "network": "n_int", "ip": "192.168.1.5", "kind": "server", "wired": True,
                         "alone_on_port": True, "private_address": False, "protected": None, "fixed_ip": ""}],
            "allow": [], "policies": list(policies)}


def add_zone(s, **kw):
    s = copy.deepcopy(s)
    s["zones"].append(zone(**kw))
    return s


def step(kind, obj, before=None, after=None):
    return {"kind": kind, "object": obj, "before": before or {}, "after": after or {}}


def new_pol(src, dst, action="allow", **kw):
    return step("policy.create", "new:aaaaaaaaaaaaaaaa", after=dict({"name": "n", "src": {"zone": src}, "dst": {"zone": dst},
                                                                      "action": action}, **kw))


def verdict(state, src, dst, proto=None, port=None):
    return zonefw.ask(state, src, dst, proto, port)["verdict"]


def expand(allow, state):
    """The v1 allow pairs with the wildcard written out."""
    ids = [z["id"] for z in state["zones"]]
    return {(a, b) for x, y in allow for a in (ids if x == "*" else [x]) for b in (ids if y == "*" else [y]) if a != b}


# ---------------------------------------------------------------------------- may A reach B
def test_sample_guests_to_main_depends_on_the_port():
    s = sample()
    v = zonefw.ask(s, "net:n_guest", "net:n_main")
    assert v["verdict"] == "depends" and v["otherwise"] == "block"
    assert [d["id"] for d in v["depends"]] == ["pol_guest_print"]
    assert v["src"]["zone"] == "z_guests" and v["dst"]["zone"] == "z_internal"
    p = zonefw.ask(s, "net:n_guest", "net:n_main", "tcp", 631)
    assert p["verdict"] == "allow" and p["by"]["id"] == "pol_guest_print" and not p["depends"]
    assert verdict(s, "net:n_guest", "net:n_main", "tcp", 9100) == "allow"
    ssh = zonefw.ask(s, "net:n_guest", "net:n_main", "tcp", 22)
    assert ssh["verdict"] == "block" and ssh["by"] is None and ssh["default"] == "zf.default.no_rule"
    assert verdict(s, "net:n_guest", "net:n_main", "udp", 631) == "block"          # the rule is for tcp
    assert verdict(s, "net:n_main", "net:n_guest") == "block"                      # no rule the other way round


def test_same_zone_is_allowed_and_management_reaches_everything():
    s = sample()
    same = zonefw.ask(s, "net:n_main", "net:n_media")
    assert same["verdict"] == "allow" and same["default"] == "zf.default.same_zone" and same["by"] is None
    for net in ("n_main", "n_work", "n_smart", "n_cams", "n_guest"):
        assert verdict(s, "net:n_mgmt", "net:" + net) == "allow", net
    assert verdict(s, "net:n_work", "net:n_mgmt") == "block"
    assert verdict(s, "net:n_main", "net:n_mgmt") == "allow"
    assert verdict(s, "device:d27", "device:d08") == "depends"                      # a visitor phone to the office laptop
    assert verdict(s, "device:d27", "device:d08", "tcp", 631) == "allow"


def test_a_state_without_rules_answers_from_the_allowed_pairs():
    s = adapter.sample_state()
    assert "policies" not in s
    v = zonefw.ask(s, "net:n_guest", "net:n_main")
    assert v["verdict"] == "block" and v["default"] == "zf.default.pairs"
    assert verdict(s, "net:n_main", "net:n_work") == "allow" and verdict(s, "net:n_work", "net:n_main") == "block"
    assert verdict(s, "net:n_mgmt", "net:n_guest") == "allow"                        # ["z_mgmt", "*"]
    assert zonefw.ask(s, "net:n_main", "net:n_media")["default"] == "zf.default.same_zone"


def test_internet_follows_the_zone_flag_without_an_external_zone():
    s = sample()
    ok = zonefw.ask(s, "net:n_main", "internet")
    assert ok["verdict"] == "allow" and ok["default"] == "zf.default.internet_flag"
    assert verdict(s, "net:n_cams", "internet") == "block"
    assert verdict(s, "device:d25", "internet") == "block"
    # with an external zone the rules decide (and none does: the family's default)
    ext = add_zone(s, zid="z_ext", name="External", system="external", builtin=True)
    v = zonefw.ask(ext, "net:n_main", "internet")
    assert v["verdict"] == "block" and v["default"] == "zf.default.no_rule"
    ext["policies"].append(pol("pol_out", "z_internal", "z_ext", 5))
    assert verdict(ext, "net:n_main", "internet") == "allow"
    assert verdict(ext, "net:n_cams", "internet") == "block"


def test_unknown_endpoints_answer_unknown():
    s = sample()
    assert verdict(s, "net:nope", "net:n_main") == "unknown"
    assert verdict(s, "device:nope", "net:n_main") == "unknown"
    assert verdict(s, "ip:not-an-address", "net:n_main") == "unknown"
    assert verdict(s, "ip:10.99.99.99", "net:n_main") == "unknown"                  # private, in no network
    assert verdict(s, "banana", "net:n_main") == "unknown"
    assert zonefw.ask(s, "net:nope", "net:n_main")["src"] is None


def test_addresses_gateway_and_internet_endpoints():
    s = sample()
    assert zonefw.of_address(s, "192.168.50.20")["net"] == "n_main"
    gw = zonefw.of_address(s, "192.168.50.1")
    assert gw["kind"] == "gateway" and gw["zone"] == "z_internal"                   # no gateway zone: the network's zone
    net = zonefw.of_address(s, "8.8.8.8")
    assert net["kind"] == "internet" and net["net"] is None
    assert zonefw.of_address(s, "fe80::1") is None
    with_gw = add_zone(s, zid="z_gw", name="Gateway", system="gateway", builtin=True)
    assert zonefw.of_address(with_gw, "192.168.50.1")["zone"] == "z_gw"
    assert zonefw.resolve(with_gw, "gateway")["zone"] == "z_gw"
    assert zonefw.resolve(s, "zone:z_work")["label"] == "Work"
    assert zonefw.resolve(s, "ip:192.168.40.24")["zone"] == "z_work"


def test_rules_that_cannot_apply_to_a_new_connection_or_are_off_do_not_count():
    est = mini(pol("p_est", "z_a", "z_int", 1, states=["established", "related"]))
    assert verdict(est, "net:n_a", "net:n_int") == "block"                          # answers only
    new = mini(pol("p_new", "z_a", "z_int", 1, states=["new", "established"]))
    assert verdict(new, "net:n_a", "net:n_int") == "allow"
    off = mini(pol("p_off", "z_a", "z_int", 1, enabled=False))
    assert verdict(off, "net:n_a", "net:n_int") == "block"
    v6 = mini(pol("p_v6", "z_a", "z_int", 1, ip_version="v6"))
    assert verdict(v6, "ip:192.168.2.10", "ip:192.168.1.5") == "block"              # an IPv4 question


def test_first_matching_rule_wins_and_the_default_is_configurable():
    s = mini(pol("p_block", "z_a", "z_int", 1, "block"), pol("p_allow", "z_a", "z_int", 2))
    v = zonefw.ask(s, "net:n_a", "net:n_int")
    assert v["verdict"] == "block" and v["by"]["id"] == "p_block"
    s = mini(pol("p_block", "z_a", "z_int", 2, "block"), pol("p_allow", "z_a", "z_int", 1))
    v = zonefw.ask(s, "net:n_a", "net:n_int")
    assert v["verdict"] == "allow" and v["by"]["id"] == "p_allow"
    s["policy_default"] = "allow"
    assert zonefw.ask(s, "net:n_int", "net:n_a")["verdict"] == "allow"
    assert zonefw.ask(s, "net:n_int", "net:n_a")["default"] == "zf.default.no_rule"


def test_ports_protocols_and_narrowed_sides():
    s = mini(pol("p_web", "z_a", "z_int", 1, proto="tcp", ports="80-90,443"))
    assert verdict(s, "net:n_a", "net:n_int", "tcp", 85) == "allow"
    assert verdict(s, "net:n_a", "net:n_int", "tcp", 443) == "allow"
    assert verdict(s, "net:n_a", "net:n_int", "tcp", 91) == "block"
    assert verdict(s, "net:n_a", "net:n_int", "tcp") == "depends"                   # any port of tcp
    assert verdict(s, "net:n_a", "net:n_int") == "depends"
    neg = mini(pol("p_neg", "z_a", "z_int", 1, proto="tcp_udp", ports="!22"))
    assert verdict(neg, "net:n_a", "net:n_int", "udp", 53) == "allow"
    assert verdict(neg, "net:n_a", "net:n_int", "tcp", 22) == "block"
    named = mini(pol("p_list", "z_a", "z_int", 1, proto="tcp", ports="web"))        # a list Lanloom cannot see
    assert verdict(named, "net:n_a", "net:n_int", "tcp", 80) == "depends"
    one_net = mini(pol("p_net", "z_a", "z_int", 1))
    one_net["policies"][0]["dst"] = {"zone": "z_int", "net": "n_int"}
    assert verdict(one_net, "net:n_a", "net:n_int") == "allow"
    assert verdict(one_net, "net:n_a", "zone:z_int") == "depends"                   # a whole zone, the rule names one network
    other = mini(pol("p_other", "z_a", "z_int", 1))
    other["policies"][0]["src"] = {"zone": "z_a", "net": "n_b"}
    assert verdict(other, "net:n_a", "net:n_int") == "block"
    sched = mini(pol("p_sched", "z_a", "z_int", 1, scheduled=True))
    assert verdict(sched, "net:n_a", "net:n_int") == "depends"
    clients = mini(pol("p_cl", "z_a", "z_int", 1))
    clients["policies"][0]["src"] = {"zone": "z_a", "match": {"kind": "client", "items": ["x"]}}
    assert verdict(clients, "net:n_a", "net:n_int") == "depends"


def test_zone_matrix_names_every_pair_of_different_zones():
    s = sample()
    m = zonefw.zone_matrix(s)
    assert len(m) == 6 * 5
    assert m[("z_guests", "z_internal")] == "depends" and m[("z_internal", "z_guests")] == "block"
    assert m[("z_mgmt", "z_guests")] == "allow" and m[("z_work", "z_internal")] == "block"
    assert m[("z_internal", "z_work")] == "allow"


def test_derived_allow_pairs_equal_the_samples_v1_pairs():
    s = sample()
    derived = {tuple(p) for p in neutral.derive_allow(s)}
    assert derived == expand(s["allow"], s)
    assert ("z_mgmt", "z_cameras") in derived and ("z_guests", "z_internal") not in derived     # the port rule narrows


# ---------------------------------------------------------------------------- order of rules
def three():
    return mini(pol("r1", "z_a", "z_int", 10), pol("r2", "z_a", "z_int", 11), pol("r3", "z_a", "z_int", 12),
                pol("other", "z_b", "z_int", 5))


def test_reorder_moves_a_rule_into_a_slot_of_its_own_pair():
    s = three()
    assert zonefw.reorder(s, "r1", 12) == {"r2": 10, "r3": 11, "r1": 12}
    assert zonefw.reorder(s, "r3", 10) == {"r3": 10, "r1": 11, "r2": 12}
    assert zonefw.reorder(s, "r2", 10) == {"r2": 10, "r1": 11, "r3": 12}
    assert zonefw.reorder(s, "r1", 11) == {"r2": 10, "r1": 11, "r3": 12}
    assert zonefw.reorder(s, "r1", 10) == {}                                      # already there
    assert zonefw.reorder(s, "r1", 5) == {}                                       # a slot of another pair
    assert zonefw.reorder(s, "r1", "12") == {}                                    # not a number
    assert zonefw.reorder(s, "nope", 10) == {}


def test_reorder_is_refused_for_a_single_rule_pair_and_builtin_rules():
    s = three()
    assert zonefw.reorder(s, "other", 5) == {}
    s["policies"].append(pol("b1", "z_b", "z_int", 6, builtin=True))
    assert zonefw.reorder(s, "b1", 5) == {} and zonefw.reorder(s, "other", 6) == {}
    assert zonefw.step_lockout(s, step("policy.reorder", "other", {"order": 5}, {"order": 6})) == "zf.block.order_range"
    assert zonefw.step_lockout(s, step("policy.reorder", "b1", {"order": 6}, {"order": 5})) == "fw.block.builtin"
    assert zonefw.step_lockout(s, step("policy.reorder", "r1", {"order": 10}, {"order": 99})) == "zf.block.order_range"
    assert zonefw.step_lockout(s, step("policy.reorder", "zzz", {"order": 10}, {"order": 11})) == "fw.block.unknown"
    assert zonefw.step_lockout(s, step("policy.reorder", "r1", {"order": 10}, {"order": 12})) is None


def test_reorder_and_its_inverse_restore_the_order():
    s = three()
    orig = {p["id"]: p["order"] for p in s["policies"]}
    for pid, target in (("r1", 12), ("r3", 10), ("r2", 12), ("r2", 10)):
        st = step("policy.reorder", pid, {"order": orig[pid]}, {"order": target})
        moved = zonefw.after_state(s, [st])
        assert {p["id"]: p["order"] for p in moved["policies"]} != orig
        assert moved["policies"][[p["id"] for p in moved["policies"]].index("other")]["order"] == 5
        back = zonefw.after_state(moved, [stepkinds.inverse_step(st)])
        # the slot of the moved rule is where it was; every other rule is where it was
        assert {p["id"]: p["order"] for p in back["policies"]} == orig, (pid, target)


def test_after_state_places_a_new_rule_at_the_end_of_its_pair_and_follows_allow():
    s = sample()
    new = zonefw.after_state(s, [new_pol("z_guests", "z_internal")])
    made = next(p for p in new["policies"] if p["id"] == "new:aaaaaaaaaaaaaaaa")
    assert made["order"] == 10011 and made["own"] is True and made["enabled"] is True
    assert verdict(new, "net:n_guest", "net:n_main", "tcp", 22) == "allow"                  # the new rule decides the rest
    assert verdict(s, "net:n_guest", "net:n_main", "tcp", 22) == "block"
    assert ["z_guests", "z_internal"] not in s["allow"] and ["z_guests", "z_internal"] in new["allow"]
    assert s["policies"] == sample()["policies"]                                    # the input is not touched
    gone = zonefw.after_state(s, [step("zone.delete", "z_cameras", {"name": "Cameras"})])
    assert next(n for n in gone["networks"] if n["id"] == "n_cams")["zone"] == "z_internal"
    assert not any("z_cameras" in zonefw.pair_of(p) for p in gone["policies"])
    assert all("z_cameras" not in p for p in gone["allow"])
    up = zonefw.after_state(s, [step("policy.update", "pol_guest_print", {"enabled": True}, {"enabled": False})])
    assert verdict(up, "net:n_guest", "net:n_main", "tcp", 631) == "block"
    moved = zonefw.after_state(s, [step("network.update", "n_cams", {"zone": "z_cameras"}, {"zone": "z_work"})])
    assert zonefw.ask(moved, "net:n_cams", "net:n_work")["default"] == "zf.default.same_zone"


def test_refresh_allow_sets_the_internet_flag_from_rules_of_an_external_zone():
    s = add_zone(mini(pol("p_out", "z_a", "z_int", 1)), zid="z_ext", name="External", system="external", builtin=True)
    s["policies"].append(pol("p_net", "z_a", "z_ext", 2))
    zonefw.refresh_allow(s)
    flags = {z["id"]: z["internet"] for z in s["zones"]}
    assert flags["z_a"] is True and flags["z_b"] is False
    assert ["z_a", "z_int"] in s["allow"] and ["z_a", "z_ext"] in s["allow"]


# ---------------------------------------------------------------------------- refusal rules of single steps
def own_zone(s, zid, **kw):
    s = copy.deepcopy(s)
    next(z for z in s["zones"] if z["id"] == zid).update(own=True, **kw)
    return s


def test_zone_name_rules():
    s = sample()
    assert zonefw.step_lockout(s, step("zone.create", "new:aaaaaaaaaaaaaaaa", after={"name": "  "})) == "zf.block.zone_name"
    assert zonefw.step_lockout(s, step("zone.create", "new:aaaaaaaaaaaaaaaa", after={"name": "GUESTS"})) == "zf.block.zone_name_taken"
    assert zonefw.step_lockout(s, step("zone.create", "new:aaaaaaaaaaaaaaaa", after={"name": " guests "})) == "zf.block.zone_name_taken"
    assert zonefw.step_lockout(s, step("zone.create", "new:aaaaaaaaaaaaaaaa", after={"name": "Lab"})) is None
    assert zonefw.step_lockout(s, step("zone.update", "z_work", {"name": "Work"}, {"name": "cameras"})) == "zf.block.zone_name_taken"
    assert zonefw.step_lockout(s, step("zone.update", "z_work", {"name": "Work"}, {"name": ""})) == "zf.block.zone_name"
    assert zonefw.step_lockout(s, step("zone.update", "z_work", {"name": "Work"}, {"name": "WORK"})) is None       # itself
    assert zonefw.step_lockout(s, step("zone.update", "zzz", {"name": "a"}, {"name": "b"})) == "fw.block.unknown"


def test_built_in_and_system_zones_are_never_changed():
    s = add_zone(sample(), zid="z_gw", name="Gateway", system="gateway", builtin=True)
    for zid in ("z_internal", "z_gw"):
        assert zonefw.step_lockout(s, step("zone.update", zid, {"name": "x"}, {"name": "y"})) == "zf.block.zone_builtin"
        assert zonefw.step_lockout(s, step("zone.delete", zid, {"name": "x"})) == "zf.block.zone_builtin"
        assert fwmodel.lockout_reason(s, step("zone.delete", zid, {"name": "x"})) == "zf.block.zone_builtin"


def test_a_zone_that_lanloom_did_not_create_is_not_deleted():
    s = sample()
    st = step("zone.delete", "z_work", {"name": "Work"})
    assert fwmodel.lockout_reason(s, st) == "fw.block.not_own"
    assert zonefw.step_lockout(s, st) == "fw.block.not_own"


def test_zone_delete_needs_an_empty_zone():
    s = own_zone(sample(), "z_work")                                              # a network (Work) lives in it
    st = step("zone.delete", "z_work", {"name": "Work"})
    assert fwmodel.lockout_reason(s, st) is None
    assert zonefw.step_lockout(s, st) == "zf.block.zone_has_networks"
    s["networks"] = [n for n in s["networks"] if n["id"] != "n_work"]
    assert zonefw.step_lockout(s, st) == "zf.block.zone_has_rules"               # rules still name it
    s["policies"] = [p for p in s["policies"] if "z_work" not in zonefw.pair_of(p)]
    assert zonefw.step_lockout(s, st) is None
    assert zonefw.zone_refs(sample(), "z_work") == {"networks": 1, "rules": 3}


def test_blocking_rules_never_touch_management_or_the_gateway():
    s = add_zone(sample(), zid="z_gw", name="Gateway", system="gateway", builtin=True)
    for src, dst in (("z_guests", "z_mgmt"), ("z_mgmt", "z_work"), ("z_work", "z_gw"), ("z_gw", "z_work")):
        for action in ("block", "reject"):
            st = new_pol(src, dst, action)
            assert zonefw.step_lockout(s, st) == "zf.block.mgmt_rule", (src, dst, action)
            assert fwmodel.lockout_reason(s, step("policy.update", "pol_work_dev", {"action": "allow"}, {"action": action,
                                          "src": {"zone": src}, "dst": {"zone": dst}})) == "zf.block.mgmt_rule"
        assert zonefw.step_lockout(s, new_pol(src, dst, "allow")) is None
    # one network of the management zone
    net = new_pol("z_work", "z_mgmt", "block")
    net["after"]["dst"] = {"zone": "z_mgmt", "net": "n_mgmt"}
    assert zonefw.step_lockout(s, net) == "zf.block.mgmt_rule"
    # blocking between two ordinary zones is fine
    assert zonefw.step_lockout(s, new_pol("z_work", "z_guests", "block")) is None
    assert zonefw.protected_zones(s) == {"z_mgmt", "z_gw"}


def test_rule_fields_are_checked_against_each_other():
    s = sample()
    bad = new_pol("z_guests", "z_work", ports="80")                                # proto any + ports
    assert zonefw.step_lockout(s, bad) == "zf.block.pol_ports_proto"
    assert zonefw.step_lockout(s, new_pol("z_guests", "z_work", proto="tcp", ports="80")) is None
    assert zonefw.step_lockout(s, new_pol("z_guests", "z_work", proto="icmp", ports="80")) == "zf.block.pol_ports_proto"
    net = new_pol("z_guests", "z_work")
    net["after"]["dst"] = {"zone": "z_work", "net": "n_main"}                      # n_main is not in Work
    assert zonefw.step_lockout(s, net) == "zf.block.pol_net_zone"
    net["after"]["dst"] = {"zone": "z_work", "net": "n_work"}
    assert zonefw.step_lockout(s, net) is None
    net["after"]["dst"] = {"zone": "z_nowhere"}
    assert zonefw.step_lockout(s, net) == "fw.block.unknown"
    net["after"]["dst"] = {"zone": "z_work", "net": "n_nowhere"}
    assert zonefw.step_lockout(s, net) == "fw.block.unknown"
    grp = new_pol("z_guests", "z_work")
    grp["after"]["src"] = {"zone": "z_guests", "group": "g1"}
    assert zonefw.step_lockout(s, grp) == "fw.block.capability"
    # an update is judged with the values the rule has now
    assert zonefw.step_lockout(s, step("policy.update", "pol_guest_print", {"proto": "tcp"}, {"proto": "any"})) == "zf.block.pol_ports_proto"
    assert zonefw.step_lockout(s, step("policy.update", "pol_guest_print", {"enabled": True}, {"enabled": False})) is None
    assert zonefw.step_lockout(s, step("policy.update", "pol_nope", {"enabled": True}, {"enabled": False})) == "fw.block.unknown"


def test_built_in_rules_are_not_changed_or_deleted_and_only_own_rules_go():
    s = sample()
    s["policies"].append(pol("sys", "z_work", "z_internal", 1, builtin=True, own=False, editable=False))
    assert zonefw.step_lockout(s, step("policy.update", "sys", {"enabled": True}, {"enabled": False})) == "fw.block.builtin"
    assert zonefw.step_lockout(s, step("policy.delete", "sys", {"name": "sys"})) == "fw.block.not_own"
    assert zonefw.step_lockout(s, step("policy.delete", "pol_int_work", {"name": "x"})) == "fw.block.not_own"      # not own
    assert zonefw.step_lockout(s, step("policy.delete", "pol_guest_print", {"name": "x"})) is None
    assert fwmodel.lockout_reason(s, step("policy.delete", "pol_int_work", {"name": "x"})) == "fw.block.not_own"


def test_networks_move_into_zones_of_their_own_or_the_internal_zone_only():
    s = add_zone(add_zone(sample(), zid="z_ext", name="External", system="external", builtin=True),
                 zid="z_gw", name="Gateway", system="gateway", builtin=True)
    s = add_zone(s, zid="z_vpn", name="VPN", builtin=True)                         # built in without a system name
    for target in ("z_ext", "z_gw", "z_vpn"):
        assert zonefw.step_lockout(s, step("network.update", "n_work", {"zone": "z_work"}, {"zone": target})) == "zf.block.zone_system", target
        assert zonefw.step_lockout(s, step("network.create", "new:aaaaaaaaaaaaaaaa", after={"name": "x", "zone": target})) == "zf.block.zone_system"
    assert zonefw.step_lockout(s, step("network.update", "n_work", {"zone": "z_work"}, {"zone": "z_internal"})) is None
    assert zonefw.step_lockout(s, step("network.update", "n_work", {"zone": "z_work"}, {"zone": "z_guests"})) is None
    assert zonefw.step_lockout(s, step("network.update", "n_work", {"zone": "z_work"}, {"zone": "z_nope"})) == "fw.block.unknown"
    assert zonefw.step_lockout(s, step("network.update", "n_mgmt", {"zone": "z_mgmt"}, {"zone": "z_work"})) == "fw.block.management"
    assert zonefw.zone_target_problem(s, "z_internal") is None and zonefw.zone_target_problem(s, "z_work") is None


# ---------------------------------------------------------------------------- rules over the whole plan
def test_one_order_move_per_zone_pair_and_never_with_a_new_or_removed_rule():
    s = three()
    one = step("policy.reorder", "r1", {"order": 10}, {"order": 12})
    two = step("policy.reorder", "r2", {"order": 11}, {"order": 12})
    assert zonefw.plan_lockout_vars(s, [one, two])[0] == "zf.block.order_one"
    assert zonefw.plan_lockout(s, [one, two]) == "zf.block.order_one"
    assert zonefw.plan_lockout_vars(s, [one])[0] is None
    assert zonefw.plan_lockout_vars(s, [one, new_pol("z_a", "z_int")])[0] == "zf.block.order_mix"
    assert zonefw.plan_lockout_vars(s, [one, step("policy.delete", "r3", {"name": "r3"})])[0] == "zf.block.order_mix"
    # a create in another pair is no problem
    assert zonefw.plan_lockout_vars(s, [one, new_pol("z_b", "z_a")])[0] is None


def test_two_new_zones_with_one_name_in_a_plan():
    s = sample()
    a = step("zone.create", "new:aaaaaaaaaaaaaaaa", after={"name": "Lab"})
    b = step("zone.create", "new:bbbbbbbbbbbbbbbb", after={"name": " lab "})
    c = step("zone.create", "new:cccccccccccccccc", after={"name": "Lab 2"})
    assert zonefw.plan_lockout_vars(s, [a, b]) == ("zf.block.zone_name_taken", {})
    assert zonefw.plan_lockout_vars(s, [a, c])[0] is None


# ---------------------------------------------------------------------------- confirmations
def test_what_needs_its_own_confirmation():
    s = add_zone(sample(), zid="z_ext", name="External", system="external", builtin=True)
    assert zonefw.needs_confirmation(s, new_pol("z_ext", "z_work")) is True                 # let something in from outside
    assert zonefw.needs_confirmation(s, new_pol("z_work", "z_mgmt")) is True                # into the management zone
    assert zonefw.needs_confirmation(s, new_pol("z_work", "z_mgmt", "block")) is False
    assert zonefw.needs_confirmation(s, new_pol("z_work", "z_guests")) is False
    assert zonefw.needs_confirmation(s, new_pol("z_mgmt", "z_mgmt")) is False
    off = new_pol("z_ext", "z_work")
    off["after"]["enabled"] = False
    assert zonefw.needs_confirmation(s, off) is False
    assert zonefw.needs_confirmation(s, step("zone.create", "new:aaaaaaaaaaaaaaaa", after={"name": "x"})) is False
    # a rename of an allow rule that already exists opens nothing new
    rename = step("policy.update", "pol_int_mgmt", {"name": "Main and media to management"}, {"name": "Renamed"})
    assert zonefw.needs_confirmation(s, rename) is False
    widen = step("policy.update", "pol_int_mgmt", {"enabled": True}, {"proto": "tcp"})
    assert zonefw.needs_confirmation(s, widen) is True
    # switching a rule into an opening one
    s["policies"].append(pol("p_off", "z_ext", "z_work", 1, enabled=False))
    assert zonefw.needs_confirmation(s, step("policy.update", "p_off", {"enabled": False}, {"enabled": True})) is True


def test_preview_names_the_confirmation_and_blocks_what_the_rules_refuse():
    s = sample()
    ops = [{"kind": "policy.create", "object": "new:aaaaaaaaaaaaaaaa", "after": {"name": "Work in", "src": {"zone": "z_work"},
                                                                                "dst": {"zone": "z_mgmt"}, "action": "allow"}},
           {"kind": "policy.create", "object": "new:bbbbbbbbbbbbbbbb", "after": {"name": "Cut", "src": {"zone": "z_work"},
                                                                                "dst": {"zone": "z_mgmt"}, "action": "block"}}]
    pv = zonefw.preview(s, zonefw.clean_ops(s, ops))
    assert pv["confirm"] == ["confirm:policy.create:new:aaaaaaaaaaaaaaaa"]
    assert [b[0] for b in pv["blocks"]] == ["zf.block.mgmt_rule"] and len(pv["steps"]) == 1
    assert pv["warnings"][0][0] == "zf.warn.pol_opens" and pv["warnings"][0][1]["to"] == "Management"
    assert pv["changes"][0]["what"] == "zf.change.pol_new"


def test_clean_ops_keeps_only_real_valid_changes():
    s = sample()
    ops = [{"kind": "policy.update", "object": "pol_guest_print", "after": {"enabled": True}},           # no change
           {"kind": "policy.update", "object": "pol_guest_print", "after": {"ports": " 631, 9100 "}},      # same after cleaning
           {"kind": "policy.update", "object": "pol_guest_print", "after": {"enabled": False, "bogus": 1}},
           {"kind": "policy.update", "object": "nope", "after": {"enabled": False}},
           {"kind": "policy.create", "object": "not-new", "after": {"name": "x", "src": {"zone": "z_work"}, "dst": {"zone": "z_cameras"},
                                                                  "action": "allow"}},
           {"kind": "policy.create", "after": {"name": "no sides", "action": "allow"}},
           {"kind": "zone.create", "after": {"name": "Lab", "internet": True}},
           {"kind": "zone.delete", "object": "z_work"},
           {"kind": "move", "object": "d01", "after": {}}, "junk"]
    out = zonefw.clean_ops(s, ops)
    kinds = sorted(v["kind"] for v in out.values())
    assert kinds == ["policy.create", "policy.update", "zone.create", "zone.delete"]
    upd = next(v for v in out.values() if v["kind"] == "policy.update")
    assert upd["before"] == {"enabled": True} and upd["after"] == {"enabled": False}
    zc = next(v for v in out.values() if v["kind"] == "zone.create")
    assert zc["after"] == {"name": "Lab"} and zc["object"].startswith("new:")                      # a new zone is just a name
    pc = next(v for v in out.values() if v["kind"] == "policy.create")
    assert pc["object"].startswith("new:") and pc["after"]["proto"] == "any" and "ports" not in pc["after"]
    zd = next(v for v in out.values() if v["kind"] == "zone.delete")
    assert zd["before"] == {"name": "Work", "internet": True, "discovery": False}


# ---------------------------------------------------------------------------- the paths that must stay open
def lockout_state():
    return mini(pol("pol_gw", "z_a", "z_gw", 1), pol("pol_ctl", "z_a", "z_int", 2), pol("pol_other", "z_b", "z_int", 3))


def disable(pid):
    return step("policy.update", pid, {"enabled": True}, {"enabled": False})


def test_guard_paths_lead_from_the_approver_and_the_executor_to_the_gateway_and_the_controller():
    s = lockout_state()
    paths = zonefw.guard_paths(s, requester="dev1", sources=["192.168.2.77"], targets=[("192.168.1.5", 443, "zf.path.controller")])
    pairs = [(p[0]["label"], p[1]["label"], p[2], p[3]) for p in paths]
    assert ("Laptop", "gateway", "udp", 53) in pairs and ("Laptop", "192.168.1.5", "tcp", 443) in pairs
    assert ("192.168.2.77", "gateway", "udp", 53) in pairs
    assert zonefw.guard_paths(s, requester="nobody") == []
    assert zonefw.guard_paths(sample(), requester="d27") == []                      # the sample has no gateway zone


def test_disabling_the_rule_that_keeps_the_gateway_reachable_is_refused_for_the_approver():
    s = lockout_state()
    why, vars_ = zonefw.plan_lockout_vars(s, [disable("pol_gw")], requester="dev1")
    assert why == "zf.block.lockout_path" and vars_["from"] == "Laptop" and vars_["to"] == "gateway"
    assert zonefw.plan_lockout_vars(s, [disable("pol_gw")], requester="ctl")[0] is None         # another device: no path of its own
    assert zonefw.plan_lockout_vars(s, [disable("pol_other")], requester="dev1")[0] is None
    assert zonefw.plan_lockout_vars(s, [disable("pol_gw")])[0] is None                           # nobody to guard
    # the executor and the controller path
    why, vars_ = zonefw.plan_lockout_vars(s, [disable("pol_ctl")], sources=["192.168.2.77"],
                                          targets=[("192.168.1.5", 443, "zf.path.controller")])
    assert why == "zf.block.lockout_path" and vars_["reason"] == "zf.path.controller" and vars_["from"] == "192.168.2.77"
    # a rule that stays on keeps the path open: moving the guarded network into a zone with a rule keeps it
    keep = lockout_state()
    keep["policies"].append(pol("pol_ctl2", "z_a", "z_int", 4))
    assert zonefw.plan_lockout_vars(keep, [disable("pol_ctl")], sources=["192.168.2.77"],
                                    targets=[("192.168.1.5", 443, "zf.path.controller")])[0] is None


def test_moving_the_approvers_network_into_a_zone_without_a_way_out_is_refused():
    s = lockout_state()
    move = step("network.update", "n_a", {"zone": "z_a"}, {"zone": "z_b"})
    why, vars_ = zonefw.plan_lockout_vars(s, [move], requester="dev1")
    assert why == "zf.block.lockout_path" and vars_["from"] == "Laptop"
    into_internal = step("network.update", "n_a", {"zone": "z_a"}, {"zone": "z_int"})
    assert zonefw.plan_lockout_vars(s, [into_internal], requester="dev1")[0] == "zf.block.lockout_path"
    s["policies"].append(pol("pol_b_gw", "z_b", "z_gw", 4))                         # Beta has its own way to the gateway
    assert zonefw.plan_lockout_vars(s, [move], requester="dev1")[0] is None


def test_review_reports_pairs_that_flip_and_the_cut_path():
    s = lockout_state()
    rv = zonefw.review(s, [disable("pol_gw")], requester="dev1")
    assert [b[0] for b in rv["blocks"]] == ["zf.block.lockout_path"]
    assert {"from": "z_a", "to": "z_gw", "before": "allow", "after": "block"} in rv["pairs"]
    assert ["zf.warn.pair_closes", {"from": "Alpha", "to": "Gateway"}] in rv["warnings"]
    rv = zonefw.review(s, [new_pol("z_b", "z_a")])
    assert {"from": "z_b", "to": "z_a", "before": "block", "after": "allow"} in rv["pairs"]
    assert ["zf.warn.pair_opens", {"from": "Beta", "to": "Alpha"}] in rv["warnings"] and not rv["blocks"]
    assert zonefw.review(s, [step("dhcp.reserve", "new:aaaaaaaaaaaaaaaa", after={"client": "dev1", "ip": "192.168.2.50"})]) == \
        {"warnings": [], "blocks": [], "pairs": []}


def build_plan(state, steps, requester=None, confirmed=()):
    base = fwmodel.preview(state, {})
    base.update(steps=steps, warnings=[], costs={"objects": len(steps)}, requester=requester)
    return fwmodel.build_plan(state, base, target="simulation", user="admin", confirmed=list(confirmed))


def gate(state, plan):
    caps = adapter.SimulatedController(settle_s=0).capabilities()
    fwmodel.gate(state, plan, target="simulation", now=time.time(), quiet=False, busy=False, healthy=(True, ""), caps=caps)


def test_gate_refuses_a_plan_that_cuts_the_approvers_path():
    s = lockout_state()
    steps = list(zonefw.clean_ops(s, [{"kind": "policy.update", "object": "pol_gw", "after": {"enabled": False}}]).values())
    plan = build_plan(s, steps, requester="dev1")
    assert plan["v"] == 2
    with pytest.raises(fwmodel.Refused) as exc:
        gate(s, plan)
    assert exc.value.key == "zf.block.lockout_path" and exc.value.vars["from"] == "Laptop"
    gate(s, build_plan(s, steps, requester="ctl"))                                  # another approver: fine


@pytest.mark.parametrize("mutate, key", [
    (lambda st: st.update(after={"name": "Cut", "src": {"zone": "z_a"}, "dst": {"zone": "z_gw"}, "action": "block"}), "zf.block.mgmt_rule"),
    (lambda st: st.update(after={"name": "Cut", "src": {"zone": "z_a"}, "dst": {"zone": "z_b"}, "action": "allow", "ports": "80"}),
     "zf.block.pol_ports_proto"),
    (lambda st: st.update(after={"name": "Open", "src": {"zone": "z_gw"}, "dst": {"zone": "z_b"}, "action": "allow"}), None),
])
def test_gate_runs_the_step_rules_too(mutate, key):
    s = lockout_state()
    st = new_pol("z_a", "z_b")
    mutate(st)
    st["after"] = dict(zonefw.POLICY_DEFAULTS, **st["after"])
    if st["after"].get("ports") is None or st["after"]["ports"] == "":
        st["after"].pop("ports", None)
    plan = build_plan(s, [st])
    if key is None:
        gate(s, plan)
        return
    with pytest.raises(fwmodel.Refused) as exc:
        gate(s, plan)
    assert exc.value.key == key


def test_gate_wants_the_confirmation_of_an_opening_rule():
    s = lockout_state()
    s["networks"][0]["management"] = True                                          # Main is the management network
    st = new_pol("z_b", "z_int")
    st["after"] = dict(zonefw.POLICY_DEFAULTS, **st["after"])
    st["after"].pop("ports")
    plan = build_plan(s, [st])
    with pytest.raises(fwmodel.Refused) as exc:
        gate(s, plan)
    assert exc.value.key == "fw.block.confirm"
    gate(s, build_plan(s, [st], confirmed=[stepkinds.confirm_token(st)]))


# ---------------------------------------------------------------------------- the simulation, end to end
def make_sim():
    return adapter.SimulatedController(hypervisor=True, settle_s=0)


def run(sim, tmp_path, plan):
    r = fwrunner.Runner(sim, tmp_path / "state", target="simulation", require_signature=False, watch_s=0, check_s=0,
                        sleep=lambda s: None)
    return r.run(plan)


def plan_from(state, steps, confirmed=()):
    return build_plan(state, steps, confirmed=confirmed)


def plan_of(sim, ops):
    st = sim.read()
    zpv = zonefw.preview(st, zonefw.clean_ops(st, ops))
    npv = netconf.preview(st, netconf.clean_ops(st, ops))
    assert not zpv["blocks"] and not npv["blocks"], (zpv["blocks"], npv["blocks"])
    return plan_from(st, zpv["steps"] + npv["steps"], confirmed=zpv["confirm"] + npv["confirm"])


def inverse_plan(sim, res):
    assert res["status"] == "done", res
    return plan_from(sim.read(), res["inverse"])


def core(sim):
    st = sim.read()
    return {"zones": st["zones"], "policies": st["policies"], "net_zone": {n["id"]: n["zone"] for n in st["networks"]},
            "allow": sorted(expand(st["allow"], st))}


def without_ids(items):
    return sorted((tuple(sorted((k, repr(v)) for k, v in p.items() if k != "id")) for p in items))


def zid_named(sim, name):
    return next(z["id"] for z in sim.read()["zones"] if z["name"] == name)


def pid_named(sim, name):
    return next(p["id"] for p in sim.read()["policies"] if p["name"] == name)


def test_the_sample_carries_its_zone_rules_only_with_the_gateway():
    st = sample()
    assert len(st["policies"]) == len(adapter.SAMPLE_POLICIES) == 11
    assert next(z for z in st["zones"] if z["id"] == "z_internal")["system"] == "internal"
    assert "policies" not in adapter.SimulatedController(settle_s=0).read()
    again = adapter.with_sample_rules(st)
    assert again is st                                                              # a state with rules stays as it is
    assert "policies" not in adapter.with_sample_rules({"zones": [{"id": "z1"}], "networks": [], "devices": []})


def test_zone_rule_and_network_changes_run_end_to_end(tmp_path):
    sim = make_sim()
    start = core(sim)
    res = run(sim, tmp_path, plan_of(sim, [{"kind": "zone.create", "after": {"name": "Lab"}}]))
    assert res["status"] == "done", res
    lab = zid_named(sim, "Lab")
    z = next(z for z in sim.read()["zones"] if z["id"] == lab)
    assert z["own"] is True and z["builtin"] is False and res["inverse"][0] == {
        "kind": "zone.delete", "object": lab, "before": {"name": "Lab"}, "after": {}}
    # a network moves into it
    res = run(sim, tmp_path, plan_of(sim, [{"kind": "network.update", "object": "n_cams", "after": {"zone": lab}}]))
    assert res["status"] == "done", res
    assert core(sim)["net_zone"]["n_cams"] == lab
    assert zonefw.ask(sim.read(), "net:n_cams", "net:n_main")["default"] == "zf.default.no_rule"
    # a rule into the internal zone: the allowed pairs follow
    res = run(sim, tmp_path, plan_of(sim, [{"kind": "policy.create", "after": {
        "name": "Lab to main", "src": {"zone": lab}, "dst": {"zone": "z_internal"}, "action": "allow"}}]))
    assert res["status"] == "done", res
    assert [lab, "z_internal"] in sim.read()["allow"]
    assert verdict(sim.read(), "net:n_cams", "net:n_main", "tcp", 22) == "allow"
    first = pid_named(sim, "Lab to main")
    # a second, blocking rule behind it, then the order is swapped (first match wins)
    res = run(sim, tmp_path, plan_of(sim, [{"kind": "policy.create", "after": {
        "name": "Lab no ssh", "src": {"zone": lab}, "dst": {"zone": "z_internal"}, "action": "block", "proto": "tcp", "ports": "22"}}]))
    assert res["status"] == "done", res
    second = pid_named(sim, "Lab no ssh")
    pols = {p["id"]: p for p in sim.read()["policies"]}
    assert pols[second]["order"] == pols[first]["order"] + 1 and pols[second]["own"] is True
    assert verdict(sim.read(), "net:n_cams", "net:n_main", "tcp", 22) == "allow"
    swap = run(sim, tmp_path, plan_of(sim, [{"kind": "policy.reorder", "object": second, "after": {"order": pols[first]["order"]}}]))
    assert swap["status"] == "done", swap
    pols2 = {p["id"]: p for p in sim.read()["policies"]}
    assert (pols2[second]["order"], pols2[first]["order"]) == (pols[first]["order"], pols[second]["order"])
    assert verdict(sim.read(), "net:n_cams", "net:n_main", "tcp", 22) == "block"
    assert verdict(sim.read(), "net:n_cams", "net:n_main", "tcp", 80) == "allow"
    # the order moved back by the inverse plan
    assert run(sim, tmp_path, inverse_plan(sim, swap))["status"] == "done"
    assert {p["id"]: p["order"] for p in sim.read()["policies"] if p["id"] in (first, second)} == \
        {first: pols[first]["order"], second: pols[second]["order"]}
    # change the first rule: switched off, the pair closes
    up = run(sim, tmp_path, plan_of(sim, [{"kind": "policy.update", "object": first, "after": {"enabled": False}}]))
    assert up["status"] == "done"
    assert [lab, "z_internal"] not in sim.read()["allow"] and verdict(sim.read(), "net:n_cams", "net:n_main", "tcp", 80) == "block"
    # delete both rules, put the network back, delete the zone: the start state is back
    assert run(sim, tmp_path, plan_of(sim, [{"kind": "policy.delete", "object": first},
                                            {"kind": "policy.delete", "object": second}]))["status"] == "done"
    assert not [p for p in sim.read()["policies"] if lab in zonefw.pair_of(p)]
    assert run(sim, tmp_path, plan_of(sim, [{"kind": "network.update", "object": "n_cams", "after": {"zone": "z_cameras"}}]))["status"] == "done"
    assert run(sim, tmp_path, plan_of(sim, [{"kind": "zone.delete", "object": lab}]))["status"] == "done"
    end = core(sim)
    assert end == start


def test_the_inverse_plan_of_each_change_restores_the_state(tmp_path):
    sim = make_sim()
    start = core(sim)
    lab_plan = plan_of(sim, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    res = run(sim, tmp_path, lab_plan)
    assert run(sim, tmp_path, inverse_plan(sim, res))["status"] == "done"
    assert core(sim) == start
    for ops in ([{"kind": "policy.update", "object": "pol_guest_print", "after": {"enabled": False, "name": "Printing", "ports": "631"}}],
                [{"kind": "network.update", "object": "n_work", "after": {"zone": "z_guests"}}],
                [{"kind": "policy.create", "after": {"name": "Cams in", "src": {"zone": "z_cameras"}, "dst": {"zone": "z_work"},
                                                     "action": "allow", "proto": "udp", "ports": "5000-5010"}}]):
        res = run(sim, tmp_path, plan_of(sim, ops))
        assert res["status"] == "done", res
        assert core(sim) != start
        assert run(sim, tmp_path, inverse_plan(sim, res))["status"] == "done"
        assert core(sim) == start, ops
    # deleting an own rule: the way back creates it again (under a new id)
    before = without_ids(sim.read()["policies"])
    res = run(sim, tmp_path, plan_of(sim, [{"kind": "policy.delete", "object": "pol_guest_print"}]))
    assert res["status"] == "done" and not [p for p in sim.read()["policies"] if p["id"] == "pol_guest_print"]
    assert run(sim, tmp_path, inverse_plan(sim, res))["status"] == "done"
    assert without_ids(sim.read()["policies"]) == before
    assert core(sim)["allow"] == start["allow"] and core(sim)["net_zone"] == start["net_zone"]


def test_the_runner_refuses_crafted_plans(tmp_path):
    sim = make_sim()
    st = sim.read()
    cut = list(zonefw.clean_ops(st, [{"kind": "policy.create", "after": {
        "name": "Cut", "src": {"zone": "z_guests"}, "dst": {"zone": "z_mgmt"}, "action": "block"}}]).values())
    res = run(sim, tmp_path, plan_from(st, cut))
    assert res["status"] == "refused" and res["reason"] == "zf.block.mgmt_rule"
    # rule order: three own rules in one pair, then two moves / a move and a removal in one plan
    made = run(sim, tmp_path, plan_of(sim, [{"kind": "policy.create", "after": {
        "name": "Rule %d" % i, "src": {"zone": "z_work"}, "dst": {"zone": "z_guests"}, "action": "allow", "proto": "udp"}} for i in range(3)]))
    assert made["status"] == "done", made
    ids = [pid_named(sim, "Rule %d" % i) for i in range(3)]
    orders = {p["id"]: p["order"] for p in sim.read()["policies"] if p["id"] in ids}
    assert len(set(orders.values())) == 3
    st = sim.read()
    first, second, third = sorted(ids, key=orders.get)
    mv = lambda pid, to: step("policy.reorder", pid, {"order": orders[pid]}, {"order": orders[to]})
    res = run(sim, tmp_path, plan_from(st, [mv(first, third), mv(second, third)]))
    assert res["status"] == "refused" and res["reason"] == "zf.block.order_one"
    res = run(sim, tmp_path, plan_from(st, [mv(first, third), step("policy.delete", third, {"name": next(p["name"] for p in st["policies"] if p["id"] == third)})]))
    assert res["status"] == "refused" and res["reason"] == "zf.block.order_mix"
    assert sim.read()["policies"] == st["policies"]                                 # nothing was written
    st = sim.read()
    # a delete of a zone that still has networks is refused even in a hand-made plan
    zc = run(sim, tmp_path, plan_of(sim, [{"kind": "zone.create", "after": {"name": "Lab"}}]))
    assert zc["status"] == "done"
    lab = zid_named(sim, "Lab")
    assert run(sim, tmp_path, plan_of(sim, [{"kind": "network.update", "object": "n_cams", "after": {"zone": lab}}]))["status"] == "done"
    res = run(sim, tmp_path, plan_from(sim.read(), [step("zone.delete", lab, {"name": "Lab"})]))
    assert res["status"] == "refused" and res["reason"] == "zf.block.zone_has_networks"


def test_a_rule_that_opens_into_the_management_zone_needs_its_confirmation_in_the_plan(tmp_path):
    sim = make_sim()
    st = sim.read()
    steps = list(zonefw.clean_ops(st, [{"kind": "policy.create", "after": {
        "name": "Work to management", "src": {"zone": "z_work"}, "dst": {"zone": "z_mgmt"}, "action": "allow"}}]).values())
    res = run(sim, tmp_path, plan_from(st, steps))
    assert res["status"] == "refused" and res["reason"] == "fw.block.confirm"
    res = run(sim, tmp_path, plan_from(st, steps, confirmed=[stepkinds.confirm_token(steps[0])]))
    assert res["status"] == "done", res
    assert ["z_work", "z_mgmt"] in sim.read()["allow"]


def test_a_failed_write_rolls_the_simulation_back(tmp_path):
    sim = adapter.SimulatedController(hypervisor=True, settle_s=0, fail_after_writes=2)
    start = core(sim)
    plan = plan_of(sim, [{"kind": "zone.create", "after": {"name": "Lab"}},
                         {"kind": "zone.create", "after": {"name": "Lab 2"}}])
    res = run(sim, tmp_path, plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert core(sim) == start


# ---------------------------------------------------------------------------- editor and API
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


PRINT_OFF = {"kind": "policy.update", "object": "pol_guest_print", "after": {"enabled": False}}
REACH = "/api/firewall/reach?src=net:n_guest&dst=net:n_main&proto=tcp&port=631"


def test_the_editor_offers_the_zone_firewall(c):
    v = c.get("/api/firewall?source=editor").json()
    zf = v["zonefw"]
    assert zf["internal"] == "z_internal" and zf["external"] is None and zf["gateway"] is None and zf["rules"] is True
    assert zf["refs"]["z_cameras"] == {"networks": 1, "rules": 2} and zf["refs"]["z_guests"] == {"networks": 1, "rules": 2}
    assert set(zf["targets"]) == {"z_internal", "z_work", "z_devices", "z_cameras", "z_guests", "z_mgmt"}
    assert {"zone.create", "policy.reorder", "network.update.zone"} <= set(v["capabilities"]["write"])
    assert {p["id"] for p in v["state"]["policies"]} >= {"pol_guest_print", "pol_int_work"}


def test_reach_before_and_after_a_draft(c):
    r = c.get(REACH).json()
    assert r["before"]["verdict"] == "allow" and r["before"]["by"]["id"] == "pol_guest_print" and r["after"] is None
    assert r["before"]["src"]["label"] == "Guests" and r["before"]["dst"]["zone"] == "z_internal"
    assert c.put("/api/firewall/draft", json={"ops": [PRINT_OFF]}, headers=HDR).status_code == 200
    r = c.get(REACH).json()
    assert r["before"]["verdict"] == "allow" and r["after"]["verdict"] == "block"
    other = c.get("/api/firewall/reach?src=net:n_guest&dst=net:n_main").json()
    assert other["before"]["verdict"] == "depends" and other["after"]["verdict"] == "block"
    gw = c.get("/api/firewall/reach?src=device:d27&dst=gateway").json()["before"]
    assert gw["verdict"] == "allow" and gw["default"] == "zf.default.gateway"
    assert c.delete("/api/firewall/draft", headers=HDR).status_code == 200
    assert c.get(REACH).json()["after"] is None


@pytest.mark.parametrize("query", ["src=net:n_guest&dst=net:n_main&port=abc", "src=net:n_guest&dst=net:n_main&port=0",
                                   "src=net:n_guest&dst=net:n_main&port=70000", "src=net:n_guest&dst=net:n_main&proto=gre",
                                   "src=net:n_guest", "dst=net:n_main", ""])
def test_reach_refuses_bad_questions(c, query):
    r = c.get("/api/firewall/reach?" + query)
    assert r.status_code == 400 and "error" in r.json()


def test_reach_for_an_unknown_source_is_an_error_not_a_crash(c):
    r = c.get(REACH + "&source=int:nope")
    assert r.status_code == 409 and r.json()["error"] == "fw.err.no_state"


def test_the_reach_route_is_a_viewer_route(c):
    rule = access.rule_for("GET", "/api/firewall/reach")
    assert rule is not None and rule.role == "viewer" and not rule.stepup
    assert access.rule_for("PUT", "/api/firewall/draft").role == "operator"
    c.ll.accounts.create("vic", PW, role="viewer")
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "vic", "password": PW}, headers=HDR).status_code == 200
    assert c.get(REACH).json()["before"]["verdict"] == "allow"
    assert c.put("/api/firewall/draft", json={"ops": [PRINT_OFF]}, headers=HDR).status_code == 403
    c.cookies.clear()
    assert c.get(REACH).status_code == 401


def test_preview_lists_the_pairs_that_flip(c):
    c.put("/api/firewall/draft", json={"ops": [PRINT_OFF]}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert not pv["blocks"] and pv["changes"][0]["what"] == "zf.change.pol_update"
    assert {"from": "z_guests", "to": "z_internal", "before": "depends", "after": "block"} in pv["reach"]
    keys = [w[0] for w in pv["warnings"]]
    assert "zf.warn.pol_off" in keys and "zf.warn.pair_closes" in keys
    assert ["zf.warn.pair_closes", {"from": "Guests", "to": "Internal"}] in pv["warnings"]


def test_preview_blocks_a_rule_that_cuts_management_and_wants_confirmation_for_one_that_opens_it(c):
    ops = [{"kind": "zone.create", "object": "new:aaaaaaaaaaaaaaaa", "after": {"name": "Lab"}},
           {"kind": "policy.create", "object": "new:bbbbbbbbbbbbbbbb", "after": {
               "name": "Cut", "src": {"zone": "z_work"}, "dst": {"zone": "z_mgmt"}, "action": "block"}},
           {"kind": "policy.create", "object": "new:cccccccccccccccc", "after": {
               "name": "Work in", "src": {"zone": "z_work"}, "dst": {"zone": "z_mgmt"}, "action": "allow"}}]
    c.put("/api/firewall/draft", json={"ops": ops}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert [b[0] for b in pv["blocks"]] == ["zf.block.mgmt_rule"]
    assert pv["confirm"] == ["confirm:policy.create:new:cccccccccccccccc"]
    assert {ch["what"] for ch in pv["changes"]} == {"zf.change.zone_new", "zf.change.pol_new"}
    assert {"from": "z_work", "to": "z_mgmt", "before": "block", "after": "allow"} in pv["reach"]
    r = c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR)
    assert r.status_code == 409


def test_zone_through_the_whole_api_then_undo(c):
    c.put("/api/firewall/draft", json={"ops": [{"kind": "zone.create", "after": {"name": "Lab"}}]}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert not pv["blocks"] and pv["changes"][0]["what"] == "zf.change.zone_new"
    p = c.post("/api/firewall/plan", json={"confirmed": pv["confirm"]}, headers=HDR).json()
    assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(c)
    job = v["history"][0]
    assert job["status"] == "done", job
    lab = next(z for z in v["state"]["zones"] if z["name"] == "Lab")
    assert lab["own"] is True and v["zonefw"]["refs"][lab["id"]] == {"networks": 0, "rules": 0}
    d = c.post("/api/firewall/undo", json={"plan": job["plan"]}, headers=HDR).json()
    assert [o["kind"] for o in d["ops"].values()] == ["zone.delete"]


def test_new_rules_of_one_pair_keep_the_order_of_the_draft():
    """Two new rules of the same zone pair are created in the order they were drafted (the first decides first),
    whatever their random new: ids are."""
    s = sample()
    for first, second in (("new:ffffffffffffffff", "new:0000000000000001"), ("new:0000000000000001", "new:ffffffffffffffff")):
        ops = [{"kind": "policy.create", "object": first, "after": {"name": "Block things", "src": {"zone": "z_devices"},
                                                                    "dst": {"zone": "z_internal"}, "action": "block"}},
               {"kind": "policy.create", "object": second, "after": {"name": "Allow printing", "src": {"zone": "z_devices"},
                                                                     "dst": {"zone": "z_internal"}, "action": "allow",
                                                                     "proto": "tcp", "ports": "631"}}]
        pv = zonefw.preview(s, zonefw.clean_ops(s, ops))
        assert [st["after"]["name"] for st in pv["steps"]] == ["Block things", "Allow printing"]
        after = zonefw.after_state(s, pv["steps"])
        v = zonefw.ask(after, "net:n_smart", "net:n_main", "tcp", 631)
        assert v["verdict"] == "block" and v["by"]["name"] == "Block things"
