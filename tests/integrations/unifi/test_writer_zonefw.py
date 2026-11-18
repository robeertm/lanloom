"""The write half of the UniFi family, stage B zone firewall: zones of their own, networks moved between zones, rules
between zones and their order - against the writable fake (console and classic style), alone and inside the executor
pipeline. Allow-list below the adapter, only own zones and rules deleted, a zone in use never removed, rollback after a
partial write, rules that cannot be carried over stay read-only, the executor keeps its way to the controller."""
import copy
import json
import os
import stat

import pytest

from lanloom.firewall import model as fwmodel
from lanloom.firewall import netconf, steps as stepkinds, zonefw
from lanloom.firewall.runner import Runner
from lanloom.integrations import transport
from lanloom.integrations.credentials import Secret
from lanloom.integrations.unifi import model
from lanloom.integrations.unifi import write as wmod
from lanloom.integrations.unifi.write import UnifiWriter, WriteError

from . import fixtures as fx
from .fake_writer import PW, USER, WritableFake

LAB_NET = model.oid("n", fx.C_NET_LAB)
DEFAULT_NET = model.oid("n", fx.O_NET_LAN)


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


def previews(w, ops):
    """(state, zone preview, network preview) of drafted changes, the family rules included."""
    st = w.read()
    lock = lambda s: w.lockout(st, s)
    zpv = zonefw.preview(st, zonefw.clean_ops(st, ops), lockout=lock)
    npv = netconf.preview(st, netconf.clean_ops(st, ops), critical=[w._host], lockout=lock)
    return st, zpv, npv


def plan_from(st, steps, confirmed=()):
    base = fwmodel.preview(st, {})
    base.update(steps=steps, warnings=[], costs={"objects": len(steps)})
    return fwmodel.build_plan(st, base, target="controller", user="admin", confirmed=list(confirmed))


def plan_for(w, ops):
    st, zpv, npv = previews(w, ops)
    assert not zpv["blocks"] and not npv["blocks"], (zpv["blocks"], npv["blocks"])
    return plan_from(st, zpv["steps"] + npv["steps"], confirmed=zpv["confirm"] + npv["confirm"])


def undo(w, tmp_path, res):
    """Run the inverse of a finished plan, with the confirmation of every step that needs one (the editor's undo does
    the same through its preview)."""
    st = w.read()
    tokens = [stepkinds.confirm_token(s) for s in res["inverse"] if stepkinds.needs_confirmation(s) or zonefw.needs_confirmation(st, s)]
    return runner(w, tmp_path).run(plan_from(st, res["inverse"], confirmed=tokens))


def blocked(w, ops):
    _, zpv, npv = previews(w, ops)
    return [b[0] for b in zpv["blocks"] + npv["blocks"]]


def run(w, tmp_path, ops):
    res = runner(w, tmp_path).run(plan_for(w, ops))
    assert res["status"] == "done", res
    return res


def zid(w, name):
    return next(z["id"] for z in w.read()["zones"] if z["name"] == name)


def pid(w, name):
    return next(p["id"] for p in w.read()["policies"] if p["name"] == name)


def order_of(w, rule):
    return next(p["order"] for p in w.read()["policies"] if p["id"] == rule)


def classic_zone(fake, name):
    return next((z for z in fake.zones() if z.get("name") == name), None)


def classic_rule(fake, name):
    return next((p for p in fake.policies() if p.get("name") == name), None)


def new_rule(name, src, dst, action="allow", **kw):
    return {"kind": "policy.create", "after": dict({"name": name, "src": {"zone": src}, "dst": {"zone": dst}, "action": action}, **kw)}


def snapshot(fake):
    return {"zones": [dict(z, network_ids=sorted(z["network_ids"])) for z in copy.deepcopy(fake.zones())],
            "policies": copy.deepcopy(fake.policies()),
            "nets": {n["_id"]: n.get("firewall_zone_id") for n in fake.classic["networkconf"]["data"] if n.get("firewall_zone_id")}}


def own_ledger(tmp_path):
    return json.loads((tmp_path / "unifi-own.json").read_text())


# ---------------------------------------------------------------------------- the allow-list below the adapter
OID1, OID2, OID3 = "5f0000000000000000001101", "5f0000000000000000001102", "5f0000000000000000001103"
SIDE = {"zone_id": OID1, "matching_target": "ANY", "port_matching_type": "ANY", "match_opposite_ports": False}
POLICY = {"name": "Lab to main", "enabled": True, "action": "ALLOW", "create_allow_respond": False, "logging": False,
          "protocol": "all", "ip_version": "BOTH", "connection_state_type": "ALL", "connection_states": [], "match_ip_sec": False,
          "match_opposite_protocol": False, "icmp_typename": "ANY", "icmp_v6_typename": "ANY", "schedule": {"mode": "ALWAYS"},
          "source": dict(SIDE), "destination": dict(SIDE, zone_id=OID2)}
REORDER = {"source_zone_id": OID1, "destination_zone_id": OID2, "before_predefined_ids": [OID3], "after_predefined_ids": []}


@pytest.mark.parametrize("prefix", ["", "/proxy/network"])
def test_allow_list_has_a_rule_for_every_zone_and_rule_call(prefix):
    base = prefix + "/v2/api/site/default/"
    check = wmod.ALLOW.check
    assert check("POST", base + "firewall/zone", {"name": "Lab", "network_ids": []}).method == "POST"
    assert check("PUT", base + "firewall/zone/" + OID1, {"_id": OID1, "name": "Lab", "network_ids": [OID2]}).method == "PUT"
    assert check("DELETE", base + "firewall/zone/" + OID1, None).method == "DELETE"
    assert check("POST", base + "firewall-policies", POLICY).method == "POST"
    assert check("PUT", base + "firewall-policies/" + OID1, POLICY).method == "PUT"
    assert check("DELETE", base + "firewall-policies/" + OID1, None).method == "DELETE"
    assert check("PUT", base + "firewall-policies/batch-reorder", REORDER).method == "PUT"
    assert check("GET", base + "firewall/zone", None).method == "GET"
    assert check("GET", base + "firewall-policies/" + OID1, None).method == "GET"
    ok_side = dict(SIDE, matching_target="NETWORK", network_ids=[OID3], match_opposite_networks=False)
    assert check("POST", base + "firewall-policies", dict(POLICY, source=ok_side, destination=dict(SIDE, port_matching_type="SPECIFIC",
                                                                                                 port="80,443"))).method == "POST"


@pytest.mark.parametrize("change", [
    {"action": "DROP"}, {"action": "allow"}, {"connection_state_type": "CUSTOM"}, {"connection_states": ["INVALID"]},
    {"schedule": {"mode": "EVERY_WEEK"}}, {"schedule": {"mode": "ALWAYS", "x": 1}}, {"protocol": "icmp"}, {"protocol": "ALL"},
    {"ip_version": "IPV6"}, {"match_ip_sec": True}, {"match_opposite_protocol": True}, {"icmp_typename": "ECHO"},
    {"enabled": "yes"}, {"name": ""}, {"name": "a\nb"}, {"hits": 5}, {"index": 1}, {"predefined": True}, {"_id": OID1},
    {"source": dict(SIDE, matching_target="NETWORK", network_ids=[OID1, OID2], match_opposite_networks=False)},
    {"source": dict(SIDE, matching_target="NETWORK", network_ids=[], match_opposite_networks=False)},
    {"source": dict(SIDE, matching_target="NETWORK", network_ids=[OID1], match_opposite_networks=True)},
    {"source": dict(SIDE, matching_target="CLIENT")},
    {"source": dict(SIDE, network_ids=[OID1])},
    {"source": dict(SIDE, match_opposite_ports=True)},
    {"source": dict(SIDE, zone_id="not-an-id")},
    {"source": dict(SIDE, extra=1)},
    {"destination": dict(SIDE, port_matching_type="SPECIFIC")},
    {"destination": dict(SIDE, port="80")},
    {"destination": dict(SIDE, port_matching_type="SPECIFIC", port="0")},
    {"destination": dict(SIDE, port_matching_type="SPECIFIC", port="80;rm")},
])
def test_a_rule_body_with_a_wrong_field_or_value_is_refused(change):
    path = "/proxy/network/v2/api/site/default/firewall-policies"
    with pytest.raises(transport.Forbidden):
        wmod.ALLOW.check("POST", path, dict(POLICY, **change))
    with pytest.raises(transport.Forbidden):
        wmod.ALLOW.check("PUT", path + "/" + OID1, dict(POLICY, **change))


def test_incomplete_extra_or_misplaced_zone_and_order_bodies_are_refused():
    base = "/proxy/network/v2/api/site/default/"
    check = wmod.ALLOW.check
    missing = dict(POLICY)
    missing.pop("schedule")
    for method, path in (("POST", "firewall-policies"), ("PUT", "firewall-policies/" + OID1)):
        with pytest.raises(transport.Forbidden):                                  # a complete body or nothing
            check(method, base + path, missing)
    for body in ({"name": "Lab"}, {"name": "Lab", "network_ids": [], "default_zone": True}, {"name": "", "network_ids": []},
                 {"name": "Lab", "network_ids": ["x"]}, {"name": "Lab", "network_ids": "a"}, {"name": "a\x00", "network_ids": []}):
        with pytest.raises(transport.Forbidden):
            check("POST", base + "firewall/zone", body)
    for body in ({"name": "Lab", "network_ids": []}, {"_id": OID1, "name": "Lab"}, {"_id": "x", "name": "Lab", "network_ids": []},
                 {"_id": OID1, "name": "Lab", "network_ids": [], "zone_key": "internal"}):
        with pytest.raises(transport.Forbidden):
            check("PUT", base + "firewall/zone/" + OID1, body)
    for body in (dict(REORDER, extra=1), dict(REORDER, source_zone_id="x"), dict(REORDER, before_predefined_ids="a"),
                 {k: v for k, v in REORDER.items() if k != "after_predefined_ids"}, dict(REORDER, after_predefined_ids=[1])):
        with pytest.raises(transport.Forbidden):
            check("PUT", base + "firewall-policies/batch-reorder", body)
    for method, path in (("DELETE", "firewall-policies/batch-reorder"), ("POST", "firewall/zone/" + OID1),
                         ("POST", "firewall-policies/" + OID1), ("PATCH", "firewall/zone/" + OID1),
                         ("POST", "firewall/zone-x"), ("PUT", "firewall/zone"), ("POST", "firewall/policy"),
                         ("DELETE", "firewall/zone")):
        with pytest.raises(transport.Forbidden):
            check(method, base + path, None if method == "DELETE" else {"name": "Lab", "network_ids": []})
    with pytest.raises(transport.Forbidden):
        check("DELETE", base + "firewall/zone/" + OID1, {"x": 1})


def test_executor_publishes_the_zone_and_rule_write_keys(fake, tmp_path):
    caps = writer(fake, tmp_path).capabilities()
    for key in ("zone.create", "zone.update.name", "zone.delete", "policy.create", "policy.update", "policy.delete",
                "policy.reorder", "network.update.zone"):
        assert key in caps["write"], key
    assert "zone.update.internet" not in caps["write"]


# ---------------------------------------------------------------------------- which rules can be carried over
def test_only_rules_without_anything_extra_are_editable():
    plain = {"_id": OID1, "name": "x", "action": "ALLOW", "enabled": True, "protocol": "all", "ip_version": "BOTH",
             "create_allow_respond": True, "logging": False, "connection_state_type": "ALL", "connection_states": [],
             "schedule": {"mode": "ALWAYS"}, "source": dict(SIDE), "destination": dict(SIDE, zone_id=OID2), "index": 10000,
             "predefined": False}
    assert model.classic_editable(plain) is True
    for change in ({"hits": 3}, {"last_hit": 1}, {"connection_state_type": "CUSTOM", "connection_states": ["INVALID"]},
                   {"connection_states": ["NEW"]}, {"schedule": {"mode": "EVERY_WEEK"}}, {"predefined": True},
                   {"action": "DROP"}, {"match_ip_sec": True}, {"match_opposite_protocol": True}, {"protocol": "icmp"},
                   {"ip_version": "IPV6"}, {"icmp_typename": "ECHO"}, {"source": dict(SIDE, matching_target="CLIENT")},
                   {"source": dict(SIDE, extra=1)}, {"source": None}):
        assert model.classic_editable(dict(plain, **change)) is False, change
    assert model.classic_editable(None) is False


def test_the_sites_own_rules_stay_read_only(fake, tmp_path):
    w = writer(fake, tmp_path)
    assert fake.policies() and not any(model.classic_editable(p) for p in fake.policies())      # extra keys, states, schedules
    st = w.read()
    assert all(p["editable"] is False for p in st["policies"])
    for rule in st["policies"]:
        assert blocked(w, [{"kind": "policy.update", "object": rule["id"], "after": {"enabled": not rule["enabled"]}}]) == ["fw.block.builtin"]
        assert blocked(w, [{"kind": "policy.delete", "object": rule["id"]}]) == ["fw.block.builtin"]
    # even a forced step is refused below the preview, and nothing is written
    first = st["policies"][0]
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "policy.update", "object": first["id"], "before": {"enabled": True}, "after": {"enabled": False}})
    # (the fixture's rule ids are not 24-character ids, so the allow-list may stop the read first; a real site's would give
    # "unifi.rule_not_editable")
    assert exc.value.key in ("unifi.rule_not_editable", "unifi.refused_by_allow_list")
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "policy.delete", "object": first["id"], "before": {"name": first["name"]}, "after": {}})
    assert exc.value.key == "unifi.not_own"
    assert not fake.config_writes and len(fake.policies()) == 5


# ---------------------------------------------------------------------------- a whole round of changes
def test_zone_network_rules_order_update_and_delete_through_the_runner(fake, tmp_path):
    w = writer(fake, tmp_path)
    start = snapshot(fake)
    internal = zid(w, "Internal")
    guest = zid(w, "Guest")
    # a zone of its own
    res = run(w, tmp_path, [{"kind": "zone.create", "after": {"name": "Lab zone"}}])
    z = classic_zone(fake, "Lab zone")
    assert z is not None and z["network_ids"] == [] and len(fake.zones()) == 4
    own = tmp_path / "unifi-own.json"
    assert own_ledger(tmp_path)["zone"] == [z["_id"]] and stat.S_IMODE(os.stat(own).st_mode) == 0o600
    lab = zid(w, "Lab zone")
    assert res["inverse"][0]["kind"] == "zone.delete" and res["inverse"][0]["object"] == lab
    mine = next(x for x in w.read()["zones"] if x["id"] == lab)
    assert mine["own"] is True and mine["builtin"] is False
    # rename it
    run(w, tmp_path, [{"kind": "zone.update", "object": lab, "after": {"name": "Lab"}}])
    assert z["name"] == "Lab" and z["network_ids"] == []
    # the Lab network moves into it (and the internal zone lets go of it)
    res = run(w, tmp_path, [{"kind": "network.update", "object": LAB_NET, "after": {"zone": lab}}])
    assert fake.net(fx.C_NET_LAB)["firewall_zone_id"] == z["_id"] and z["network_ids"] == [fx.C_NET_LAB]
    assert fx.C_NET_LAB not in classic_zone(fake, "Internal")["network_ids"]
    assert next(n for n in w.read()["networks"] if n["id"] == LAB_NET)["zone"] == lab
    assert res["inverse"][0]["after"] == {"zone": internal}
    # two rules in one pair (the second goes to the end), a blocking rule to the guest zone, a rule for a few ports
    run(w, tmp_path, [new_rule("Lab out", lab, internal)])
    run(w, tmp_path, [new_rule("Lab dns", lab, internal, proto="udp", ports="53")])
    run(w, tmp_path, [new_rule("Lab not guest", lab, guest, "block", proto="tcp", ports="22-23")])
    out, dns, noguest = (classic_rule(fake, n) for n in ("Lab out", "Lab dns", "Lab not guest"))
    assert out["action"] == "ALLOW" and out["protocol"] == "all" and out["source"]["zone_id"] == z["_id"]
    assert out["destination"]["zone_id"] == classic_zone(fake, "Internal")["_id"] and out["schedule"] == {"mode": "ALWAYS"}
    assert dns["protocol"] == "udp" and dns["destination"]["port_matching_type"] == "SPECIFIC" and dns["destination"]["port"] == "53"
    assert noguest["action"] == "BLOCK" and noguest["protocol"] == "tcp" and noguest["destination"]["port"] == "22-23"
    assert dns["index"] > out["index"]
    assert sorted(own_ledger(tmp_path)["policy"]) == sorted(p["_id"] for p in (out, dns, noguest))
    st = w.read()
    assert all(next(p for p in st["policies"] if p["name"] == n)["own"] is True for n in ("Lab out", "Lab dns", "Lab not guest"))
    assert zonefw.ask(st, "net:" + LAB_NET, "net:" + DEFAULT_NET)["verdict"] == "allow"
    # the order: the second rule takes the first rule's place, the first moves down
    rid_out, rid_dns = pid(w, "Lab out"), pid(w, "Lab dns")
    slot = order_of(w, rid_out)
    res = run(w, tmp_path, [{"kind": "policy.reorder", "object": rid_dns, "after": {"order": slot}}])
    assert fake.policy(dns["_id"])["index"] < fake.policy(out["_id"])["index"]
    assert ("PUT", "firewall-policies/batch-reorder", None) in fake.config_writes
    assert order_of(w, rid_dns) == slot
    assert zonefw.ask(w.read(), "net:" + LAB_NET, "net:" + DEFAULT_NET, "udp", 53)["by"]["id"] == rid_dns
    assert res["inverse"][0]["kind"] == "policy.reorder"
    run(w, tmp_path, [{"kind": "policy.reorder", "object": rid_dns, "after": {"order": order_of(w, rid_out)}}])      # and back
    assert fake.policy(dns["_id"])["index"] > fake.policy(out["_id"])["index"]
    # change a rule: name, switched off, other ports
    res = run(w, tmp_path, [{"kind": "policy.update", "object": rid_out, "after": {"name": "Lab to main", "enabled": False}}])
    assert out["name"] == "Lab to main" and out["enabled"] is False and out["action"] == "ALLOW"
    assert zonefw.ask(w.read(), "net:" + LAB_NET, "net:" + DEFAULT_NET, "tcp", 80)["verdict"] == "block"
    assert res["inverse"][0]["before"] == {"name": "Lab to main", "enabled": False}
    run(w, tmp_path, [{"kind": "policy.update", "object": rid_dns, "after": {"ports": "53,5353"}}])
    assert dns["destination"]["port"] == "53,5353"
    # take it all apart: rules first, the network back into the internal zone, then the zone
    run(w, tmp_path, [{"kind": "policy.delete", "object": r} for r in (rid_out, rid_dns, pid(w, "Lab not guest"))])
    assert not [p for p in fake.policies() if p["name"].startswith("Lab")] and own_ledger(tmp_path)["policy"] == []
    run(w, tmp_path, [{"kind": "network.update", "object": LAB_NET, "after": {"zone": internal}}])
    assert fake.net(fx.C_NET_LAB)["firewall_zone_id"] == classic_zone(fake, "Internal")["_id"]
    run(w, tmp_path, [{"kind": "zone.delete", "object": lab}])
    assert classic_zone(fake, "Lab") is None and own_ledger(tmp_path)["zone"] == []
    assert snapshot(fake) == start


def test_the_inverse_plan_restores_zone_rule_and_network(fake, tmp_path):
    w = writer(fake, tmp_path)
    start = snapshot(fake)
    res = run(w, tmp_path, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    lab = zid(w, "Lab")
    mid = snapshot(fake)
    assert undo(w, tmp_path, res)["status"] == "done"
    assert snapshot(fake) == start
    run(w, tmp_path, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    lab = zid(w, "Lab")
    mid = snapshot(fake)
    res = run(w, tmp_path, [{"kind": "network.update", "object": LAB_NET, "after": {"zone": lab}}])
    moved = snapshot(fake)
    assert moved != mid
    assert undo(w, tmp_path, res)["status"] == "done"
    assert snapshot(fake) == mid
    run(w, tmp_path, [new_rule("Lab out", lab, zid(w, "Internal"), proto="tcp", ports="443")])
    before = snapshot(fake)
    res = run(w, tmp_path, [{"kind": "policy.update", "object": pid(w, "Lab out"),
                             "after": {"name": "Renamed", "enabled": False, "ports": "8443"}}])
    assert snapshot(fake) != before
    assert undo(w, tmp_path, res)["status"] == "done"
    assert snapshot(fake) == before
    # deleting an own rule: the way back creates it again, the same rule
    res = run(w, tmp_path, [{"kind": "policy.delete", "object": pid(w, "Lab out")}])
    assert classic_rule(fake, "Lab out") is None
    assert undo(w, tmp_path, res)["status"] == "done"
    back = classic_rule(fake, "Lab out")
    assert back and back["protocol"] == "tcp" and back["destination"]["port"] == "443" and back["action"] == "ALLOW"
    assert back["_id"] in own_ledger(tmp_path)["policy"]


# ---------------------------------------------------------------------------- zone rules before anything is written
def test_zone_and_rule_rules_before_anything_is_written(fake, tmp_path):
    w = writer(fake, tmp_path)
    internal, guest = zid(w, "Internal"), zid(w, "Guest")
    ext = zid(w, "External")
    assert blocked(w, [{"kind": "zone.create", "after": {"name": "guest"}}]) == ["zf.block.zone_name_taken"]
    assert blocked(w, [{"kind": "zone.create", "after": {"name": "x" * 40}}]) == []                      # the allow-list cuts it later
    assert blocked(w, [{"kind": "zone.update", "object": internal, "after": {"name": "Home"}}]) == ["zf.block.zone_builtin"]
    assert blocked(w, [{"kind": "zone.update", "object": ext, "after": {"name": "Home"}}]) == ["zf.block.zone_builtin"]
    assert blocked(w, [{"kind": "zone.delete", "object": guest}]) == ["fw.block.not_own"]                # not created by Lanloom
    assert blocked(w, [{"kind": "zone.delete", "object": ext}]) == ["zf.block.zone_builtin"]
    # networks go to a zone of their own or the internal zone, never to the external one
    assert blocked(w, [{"kind": "network.update", "object": LAB_NET, "after": {"zone": ext}}]) == ["zf.block.zone_system"]
    assert blocked(w, [{"kind": "network.update", "object": LAB_NET, "after": {"zone": guest}}]) == []
    assert blocked(w, [{"kind": "network.update", "object": DEFAULT_NET, "after": {"zone": guest}}]) == ["fw.block.management"]
    # the internal zone holds the management network: no blocking rule from or to it, towards a zone nobody knows, with
    # ports but no protocol
    assert blocked(w, [new_rule("cut", guest, internal, "block")]) == ["zf.block.mgmt_rule"]
    assert blocked(w, [new_rule("cut", internal, guest, "reject")]) == ["zf.block.mgmt_rule"]
    assert blocked(w, [new_rule("p", guest, ext, ports="80")]) == ["zf.block.pol_ports_proto"]
    assert blocked(w, [new_rule("allow in", guest, internal)]) == []                                     # allowed, but confirmed
    _, zpv, _ = previews(w, [new_rule("allow in", guest, internal)])
    assert len(zpv["confirm"]) == 1 and zpv["warnings"][0][0] == "zf.warn.pol_opens"
    # what the family cannot carry over
    assert blocked(w, [new_rule("icmp", guest, ext, proto="icmp")]) == ["fw.block.capability"]
    assert blocked(w, [{"kind": "zone.update", "object": ext, "after": {"internet": True}}]) != []
    assert not fake.config_writes


def test_the_gate_wants_the_confirmation_of_a_rule_into_the_management_zone(fake, tmp_path):
    w = writer(fake, tmp_path)
    internal, guest = zid(w, "Internal"), zid(w, "Guest")
    st, zpv, _ = previews(w, [new_rule("allow in", guest, internal)])
    res = runner(w, tmp_path).run(plan_from(st, zpv["steps"]))
    assert res["status"] == "refused" and res["reason"] == "fw.block.confirm" and not fake.config_writes
    res = runner(w, tmp_path).run(plan_from(st, zpv["steps"], confirmed=zpv["confirm"]))
    assert res["status"] == "done", res
    assert classic_rule(fake, "allow in")["action"] == "ALLOW"


# ---------------------------------------------------------------------------- a zone in use is never removed
def test_zone_delete_with_a_network_is_blocked_and_refused_below_the_preview(fake, tmp_path):
    w = writer(fake, tmp_path)
    run(w, tmp_path, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    lab = zid(w, "Lab")
    run(w, tmp_path, [{"kind": "network.update", "object": LAB_NET, "after": {"zone": lab}}])
    assert blocked(w, [{"kind": "zone.delete", "object": lab}]) == ["zf.block.zone_has_networks"]
    w.read()
    writes = list(fake.config_writes)
    forced = {"kind": "zone.delete", "object": lab, "before": {"name": "Lab"}, "after": {}}
    with pytest.raises(WriteError) as exc:
        w.apply(forced)
    assert exc.value.key == "unifi.zone_in_use"
    assert fake.config_writes == writes and classic_zone(fake, "Lab")["network_ids"] == [fx.C_NET_LAB]
    # through the runner, with the checks of the executor in front, the plan does not even start
    st = w.read()
    res = runner(w, tmp_path).run(plan_from(st, [forced]))
    assert res["status"] == "refused" and res["reason"] == "zf.block.zone_has_networks"
    assert fake.config_writes == writes


def test_zone_delete_with_a_rule_is_blocked_and_the_controller_never_deletes_it_silently(fake, tmp_path):
    w = writer(fake, tmp_path)
    run(w, tmp_path, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    lab, guest = zid(w, "Lab"), zid(w, "Guest")
    run(w, tmp_path, [new_rule("Lab to guest", lab, guest, proto="tcp", ports="80")])
    assert blocked(w, [{"kind": "zone.delete", "object": lab}]) == ["zf.block.zone_has_rules"]
    w.read()
    writes = list(fake.config_writes)
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "zone.delete", "object": lab, "before": {"name": "Lab"}, "after": {}})
    assert exc.value.key == "unifi.zone_in_use"
    assert fake.config_writes == writes and classic_zone(fake, "Lab") is not None and classic_rule(fake, "Lab to guest") is not None
    # a zone the write half did not create is not touched either
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "zone.delete", "object": guest, "before": {"name": "Guest"}, "after": {}})
    assert exc.value.key == "unifi.not_own" and fake.config_writes == writes


# ---------------------------------------------------------------------------- partial write, rollback
def test_rollback_after_a_partial_write_removes_the_new_zone_and_rule(fake, tmp_path):
    w = writer(fake, tmp_path)
    start = snapshot(fake)
    plan = plan_for(w, [{"kind": "zone.create", "after": {"name": "Lab"}}, {"kind": "zone.create", "after": {"name": "Lab 2"}}])
    fake.fail_config = 2                                  # the first zone is created, the second write fails
    res = runner(w, tmp_path).run(plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert classic_zone(fake, "Lab") is None and classic_zone(fake, "Lab 2") is None
    assert snapshot(fake) == start
    assert own_ledger(tmp_path) == {"networkconf": [], "portforward": [], "zone": [], "policy": []}
    # a zone and a rule: the rule write fails, the zone goes again
    fake.fail_config = 0
    fake.config_writes.clear()
    run(w, tmp_path, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    lab, guest = zid(w, "Lab"), zid(w, "Guest")
    mid = snapshot(fake)
    st, zpv, _ = previews(w, [new_rule("Lab to guest", lab, guest, proto="tcp", ports="80"),
                              {"kind": "zone.create", "after": {"name": "Lab 3"}}])
    steps = sorted(zpv["steps"], key=lambda s: s["kind"] != "zone.create")          # the zone first, then the rule
    fake.fail_config = len(fake.config_writes) + 2
    res = runner(w, tmp_path).run(plan_from(st, steps, confirmed=zpv["confirm"]))
    assert res["status"] == "rolled_back"
    assert classic_zone(fake, "Lab 3") is None and classic_rule(fake, "Lab to guest") is None
    assert snapshot(fake) == mid
    assert own_ledger(tmp_path)["zone"] == [classic_zone(fake, "Lab")["_id"]] and own_ledger(tmp_path)["policy"] == []


def test_rollback_puts_a_renamed_rule_and_a_moved_network_back(fake, tmp_path):
    w = writer(fake, tmp_path)
    run(w, tmp_path, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    lab, internal = zid(w, "Lab"), zid(w, "Internal")
    run(w, tmp_path, [new_rule("Lab out", lab, internal)])
    run(w, tmp_path, [{"kind": "network.update", "object": LAB_NET, "after": {"zone": lab}}])
    base = snapshot(fake)
    out = pid(w, "Lab out")
    st, zpv, npv = previews(w, [{"kind": "policy.update", "object": out, "after": {"name": "Renamed", "enabled": False}},
                                {"kind": "network.update", "object": LAB_NET, "after": {"zone": internal}},
                                new_rule("Lab more", lab, internal, proto="udp", ports="53")])
    by_kind = {s["kind"]: s for s in zpv["steps"] + npv["steps"]}
    steps = [by_kind["policy.update"], by_kind["network.update"], by_kind["policy.create"]]       # the create fails last
    fake.fail_config = len(fake.config_writes) + 3
    res = runner(w, tmp_path).run(plan_from(st, steps, confirmed=zpv["confirm"] + npv["confirm"]))
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert classic_rule(fake, "Renamed") is None and classic_rule(fake, "Lab out")["enabled"] is True
    assert classic_rule(fake, "Lab more") is None
    assert snapshot(fake) == base


def test_network_loss_mid_write_of_a_zone_rolls_back(fake, tmp_path):
    w = writer(fake, tmp_path)
    plan = plan_for(w, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    fake.drop_config = 1                                  # the zone is stored, the reply never arrives
    res = runner(w, tmp_path).run(plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    # without a reply the id is unknown, so nothing is remembered as own: nothing unknown is ever deleted later (the
    # zone stays on the site, empty, as the network of the same kind of fault does)
    assert not (tmp_path / "unifi-own.json").exists() or own_ledger(tmp_path)["zone"] == []
    assert classic_zone(fake, "Lab") is not None and classic_zone(fake, "Lab")["network_ids"] == []


# ---------------------------------------------------------------------------- the executor keeps its way to the controller
def lab_with_a_way_in(w, tmp_path):
    """A zone with the Lab network in it and one rule from it to the internal zone (the way to the controller)."""
    run(w, tmp_path, [{"kind": "zone.create", "after": {"name": "Lab"}}])
    lab, internal = zid(w, "Lab"), zid(w, "Internal")
    run(w, tmp_path, [{"kind": "network.update", "object": LAB_NET, "after": {"zone": lab}}])
    run(w, tmp_path, [new_rule("Lab out", lab, internal)])
    return lab, internal


def test_disabling_the_rule_that_opens_the_executors_path_is_refused(fake, tmp_path, monkeypatch):
    w = writer(fake, tmp_path)
    lab, internal = lab_with_a_way_in(w, tmp_path)
    monkeypatch.setattr(w, "_controller_ip", lambda: "192.168.50.5")                 # the controller: in Default (internal zone)
    monkeypatch.setattr(w, "self_address", lambda: "203.0.113.50")                   # this executor: in Lab
    out = pid(w, "Lab out")
    steps = [s for s in zonefw.clean_ops(w.read(), [{"kind": "policy.update", "object": out, "after": {"enabled": False}}]).values()]
    before = list(fake.config_writes)
    assert w.plan_lockout(w.read(), steps) == "zf.block.lockout_path"
    res = runner(w, tmp_path).run(plan_from(w.read(), steps))
    assert res["status"] == "refused" and res["reason"] == "zf.block.lockout_path"
    assert fake.config_writes == before and classic_rule(fake, "Lab out")["enabled"] is True
    # moving the executor's own network out of the zone cuts the same path
    guest = zid(w, "Guest")
    move = list(zonefw.clean_ops(w.read(), []).values()) + list(netconf.clean_ops(
        w.read(), [{"kind": "network.update", "object": LAB_NET, "after": {"zone": guest}}]).values())
    res = runner(w, tmp_path).run(plan_from(w.read(), move))
    assert res["status"] == "refused" and res["reason"] == "zf.block.lockout_path" and fake.config_writes == before
    # a change that leaves the path alone goes through
    ok = list(zonefw.clean_ops(w.read(), [{"kind": "policy.update", "object": out, "after": {"name": "Lab to main"}}]).values())
    assert w.plan_lockout(w.read(), ok) is None
    assert runner(w, tmp_path).run(plan_from(w.read(), ok))["status"] == "done"
    assert classic_rule(fake, "Lab to main")["enabled"] is True


def test_a_path_that_cannot_be_placed_is_not_guarded(fake, tmp_path):
    w = writer(fake, tmp_path)
    lab_with_a_way_in(w, tmp_path)
    out = pid(w, "Lab out")
    steps = list(zonefw.clean_ops(w.read(), [{"kind": "policy.update", "object": out, "after": {"enabled": False}}]).values())
    assert w.plan_lockout(w.read(), steps) is None                                   # the fake listens on a loopback address
    assert runner(w, tmp_path).run(plan_from(w.read(), steps))["status"] == "done"
    assert classic_rule(fake, "Lab out")["enabled"] is False


def test_the_approvers_device_keeps_its_way_through_the_gate(fake, tmp_path):
    w = writer(fake, tmp_path)
    lab, internal = lab_with_a_way_in(w, tmp_path)
    st = w.read()
    lab_dev = next(d for d in st["devices"] if d["network"] == LAB_NET)
    out = pid(w, "Lab out")
    steps = list(zonefw.clean_ops(st, [{"kind": "policy.update", "object": out, "after": {"enabled": False}}]).values())
    base = fwmodel.preview(st, {})
    base.update(steps=steps, warnings=[], costs={"objects": 1}, requester=lab_dev["id"])
    plan = fwmodel.build_plan(st, base, target="controller", user="admin", confirmed=["self"])
    res = runner(w, tmp_path).run(plan)
    # the approver's device sits in Lab and loses nothing it had: its way to the gateway only counts when the gateway is a
    # zone of its own (this site has none), so there is no path to guard and the plan goes through
    assert zonefw.guard_paths(st, requester=lab_dev["id"]) == []
    assert res["status"] == "done", res
    assert classic_rule(fake, "Lab out")["enabled"] is False
