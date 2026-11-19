"""The write half of the UniFi family, stage B gateway settings: internal networks, fixed addresses and port forwards
against the writable fake, alone and inside the executor pipeline - allow-list below the adapter, the gateway rules,
read-back, only own objects deleted, rollback after a partial write, network loss mid-write, crash recovery."""
import json
import os
import stat

import pytest

from lanloom.firewall import model as fwmodel
from lanloom.firewall import netconf
from lanloom.firewall.runner import Runner, write_private
from lanloom.integrations import transport
from lanloom.integrations.credentials import Secret
from lanloom.integrations.unifi import model
from lanloom.integrations.unifi import write as wmod
from lanloom.integrations.unifi.write import UnifiWriter, WriteError

from . import fixtures as fx
from .fake_writer import PW, U_PHONE, U_PRINTER, USER, WritableFake

PHONE = model.oid("c", fx.MAC_PHONE)
PRINTER = model.oid("c", "02:4c:4c:00:01:09")
LAN = model.oid("n", fx.O_NET_LAN)
LAB = model.oid("n", fx.C_NET_LAB)
GUEST_NET = model.oid("n", fx.O_NET_GUEST)
FWD_WEB = model.oid("f", "5f0000000000000000000501")
NEW_NET = {"kind": "network.create", "after": {"name": "Smart home", "cidr": "192.168.60.0/24", "vlan": 60}}


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
    clean = netconf.clean_ops(st, ops)
    return st, netconf.preview(st, clean, critical=[w._host], lockout=lambda s: w.lockout(st, s))


def plan_for(w, ops):
    st, pv = preview(w, ops)
    assert not pv["blocks"], pv["blocks"]
    base = fwmodel.preview(st, {})
    base["steps"] = pv["steps"]
    base["warnings"] = pv["warnings"]
    base["costs"] = {"objects": len(pv["steps"])}
    return fwmodel.build_plan(st, base, target="controller", user="admin", confirmed=pv["confirm"])


def blocked(w, ops):
    _, pv = preview(w, ops)
    return [b[0] for b in pv["blocks"]]


def net_named(fake, name):
    return next((n for n in fake.classic["networkconf"]["data"] if n.get("name") == name), None)


# ---------------------------------------------------------------------------- the allow-list below the adapter
NET_BODY = {"name": "IoT", "purpose": "corporate", "networkgroup": "LAN", "ip_subnet": "192.168.60.1/24", "vlan_enabled": True,
            "vlan": 60, "dhcpd_enabled": True, "dhcpd_start": "192.168.60.100", "dhcpd_stop": "192.168.60.199", "enabled": True,
            "setting_preference": "manual", "internet_access_enabled": True, "ipv6_interface_type": "none"}
FWD_BODY = {"name": "Cam", "enabled": True, "src": "any", "dst_port": "8080", "fwd": "192.168.50.30", "fwd_port": "80",
            "proto": "tcp", "log": False, "pfwd_interface": "wan"}


def test_allow_list_takes_only_internal_networks_and_private_forwards():
    path = "/proxy/network/api/s/default/rest/"
    assert wmod.ALLOW.check("POST", path + "networkconf", NET_BODY).method == "POST"
    assert wmod.ALLOW.check("POST", path + "portforward", FWD_BODY).method == "POST"
    assert wmod.ALLOW.check("DELETE", path + "networkconf/" + fx.C_NET_LAB, None).method == "DELETE"
    for change in ({"purpose": "wan"}, {"purpose": "guest"}, {"ip_subnet": "8.8.8.1/24"}, {"ip_subnet": "10.0.0.1/8"},
                   {"ip_subnet": "192.168.60.0/24"}, {"vlan": 4010}, {"vlan": 1}, {"vlan": "60"}, {"vlan_enabled": False},
                   {"networkgroup": "WAN"}, {"ipv6_interface_type": "pd"}, {"dhcpd_start": "8.8.8.8"}, {"name": ""},
                   {"name": "a\nb"}, {"vpn_type": "x"}):
        with pytest.raises(transport.Forbidden):
            wmod.ALLOW.check("POST", path + "networkconf", dict(NET_BODY, **change))
    missing = dict(NET_BODY)
    missing.pop("vlan")
    with pytest.raises(transport.Forbidden):                   # a complete body or nothing
        wmod.ALLOW.check("POST", path + "networkconf", missing)
    for change in ({"fwd": "8.8.8.8"}, {"src": "203.0.113.0/24"}, {"proto": "any"}, {"dst_port": "0"}, {"dst_port": ""},
                   {"pfwd_interface": "both"}, {"destination_ip": "1.2.3.4"}):
        with pytest.raises(transport.Forbidden):
            wmod.ALLOW.check("POST", path + "portforward", dict(FWD_BODY, **change))
    for body in ({"vlan": 30}, {"ip_subnet": "192.168.61.1/24"}, {"purpose": "corporate"}, {}):
        with pytest.raises(transport.Forbidden):
            wmod.ALLOW.check("PUT", path + "networkconf/" + fx.C_NET_LAB, body)
    with pytest.raises(transport.Forbidden):
        wmod.ALLOW.check("DELETE", path + "networkconf/" + fx.C_NET_LAB, {"x": 1})
    with pytest.raises(transport.Forbidden):
        wmod.ALLOW.check("DELETE", path + "user/" + U_PHONE, None)


# ---------------------------------------------------------------------------- networks
def test_create_rename_and_delete_an_own_network(fake, tmp_path):
    w = writer(fake, tmp_path)
    p = plan_for(w, [NEW_NET])
    assert p["v"] == 2 and p["steps"][0]["after"]["zone"] == model.oid("z", fx.O_ZONE_INT)
    res = runner(w, tmp_path).run(p)
    assert res["status"] == "done", res
    n = net_named(fake, "Smart home")
    assert n["ip_subnet"] == "192.168.60.1/24" and n["vlan"] == 60 and n["purpose"] == "corporate" and n["vlan_enabled"] is True
    assert (n["dhcpd_start"], n["dhcpd_stop"]) == ("192.168.60.100", "192.168.60.199")
    own = tmp_path / "unifi-own.json"
    assert json.loads(own.read_text())["networkconf"] == [n["_id"]] and stat.S_IMODE(os.stat(own).st_mode) == 0o600
    nid = model.oid("n", n["_id"])
    assert res["inverse"][0]["kind"] == "network.delete" and res["inverse"][0]["object"] == nid
    st = w.read()
    mine = next(x for x in st["networks"] if x["id"] == nid)
    assert mine["own"] is True and not any(x.get("own") for x in st["networks"] if x["id"] != nid)
    res = runner(w, tmp_path).run(plan_for(w, [{"kind": "network.update", "object": nid,
                                                "after": {"name": "Devices", "dhcp": {"on": True, "range": ["192.168.60.50", "192.168.60.90"]}}}]))
    assert res["status"] == "done" and n["name"] == "Devices" and (n["dhcpd_start"], n["dhcpd_stop"]) == ("192.168.60.50", "192.168.60.90")
    res = runner(w, tmp_path).run(plan_for(w, [{"kind": "network.delete", "object": nid}]))
    assert res["status"] == "done" and net_named(fake, "Devices") is None
    assert json.loads(own.read_text())["networkconf"] == []


def test_network_rules_before_anything_is_written(fake, tmp_path):
    w = writer(fake, tmp_path)
    after = NEW_NET["after"]
    assert blocked(w, [dict(NEW_NET, after=dict(after, cidr="192.168.50.0/24"))]) == ["fw.block.net_overlap"]
    assert blocked(w, [dict(NEW_NET, after=dict(after, vlan=20))]) == ["fw.block.net_vlan_taken"]
    assert blocked(w, [dict(NEW_NET, after=dict(after, vlan=4050))]) == ["fw.block.net_vlan"]
    assert blocked(w, [dict(NEW_NET, after=dict(after, cidr="100.64.0.0/24"))]) == ["fw.block.net_cidr"]
    assert blocked(w, [dict(NEW_NET, after=dict(after, cidr="10.0.0.0/8"))]) == ["fw.block.net_cidr"]
    # a zone of its own is fine since the zone firewall stage; a system zone other than the internal one is not
    assert blocked(w, [dict(NEW_NET, after=dict(after, zone=model.oid("z", fx.O_ZONE_GUEST)))]) == []
    assert blocked(w, [dict(NEW_NET, after=dict(after, zone=model.oid("z", fx.O_ZONE_EXT)))]) == ["zf.block.zone_system"]
    assert blocked(w, [dict(NEW_NET, after=dict(after, dhcp={"on": True, "range": ["192.168.60.1", "192.168.60.20"]}))]) == ["fw.block.dhcp_range"]
    # the address service of a network with clients that have no fixed address stays on; guest networks stay as they are
    assert blocked(w, [{"kind": "network.update", "object": LAN, "after": {"name": "Home"}}]) == ["fw.block.management"]
    fake.net(fx.C_NET_LAB).update(dhcpd_enabled=True, dhcpd_start="203.0.113.100", dhcpd_stop="203.0.113.199")
    assert blocked(w, [{"kind": "network.update", "object": LAB, "after": {"dhcp": {"on": False, "range": None}}}]) == ["fw.block.dhcp_in_use"]
    assert blocked(w, [{"kind": "network.update", "object": GUEST_NET, "after": {"name": "Visitors"}}]) == ["fw.block.network_kind"]
    # deleting: never a network Lanloom did not create
    assert blocked(w, [{"kind": "network.delete", "object": LAB}]) == ["fw.block.not_own"]
    assert not fake.config_writes


def test_a_network_in_use_or_not_own_is_never_deleted_even_by_a_forced_step(fake, tmp_path):
    w = writer(fake, tmp_path)
    w.read()
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "network.delete", "object": LAB, "before": {"name": "Lab"}, "after": {}})
    assert exc.value.key in ("unifi.network_in_use", "unifi.not_own") and not fake.config_writes
    runner(w, tmp_path).run(plan_for(w, [NEW_NET]))
    nid = model.oid("n", net_named(fake, "Smart home")["_id"])
    st = w.read()
    pv = fwmodel.preview(st, {PHONE: nid})
    assert runner(w, tmp_path).run(fwmodel.build_plan(st, pv, target="controller", user="a", confirmed=pv["confirm"]))["status"] == "done"
    assert blocked(w, [{"kind": "network.delete", "object": nid}]) == ["fw.block.network_in_use"]
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "network.delete", "object": nid, "before": {"name": "Smart home"}, "after": {}})
    assert exc.value.key == "unifi.network_in_use" and net_named(fake, "Smart home") is not None


def test_rollback_after_a_partial_write_removes_the_new_network(fake, tmp_path):
    w = writer(fake, tmp_path)
    fake.fail_config = 2                                  # the network is created, the port forward fails
    p = plan_for(w, [NEW_NET, {"kind": "forward.create", "after": {"name": "Cam", "proto": "tcp", "wan_port": "8081",
                                                                    "dst_ip": "203.0.113.50", "dst_port": "80"}}])
    res = runner(w, tmp_path).run(p)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert net_named(fake, "Smart home") is None and not [f for f in fake.classic["portforward"]["data"] if f["name"] == "Cam"]
    assert json.loads((tmp_path / "unifi-own.json").read_text()) == {"networkconf": [], "portforward": [], "zone": [], "policy": [], "wlanconf": []}


def test_network_loss_mid_write_rolls_back(fake, tmp_path):
    w = writer(fake, tmp_path)
    fake.drop_config = 1                                  # the network is stored, the reply never arrives
    res = runner(w, tmp_path).run(plan_for(w, [NEW_NET]))
    assert res["status"] in ("rolled_back", "failed")
    # without a reply there is no id: nothing was remembered as own, so nothing unknown is deleted
    assert json.loads((tmp_path / "unifi-own.json").read_text() if (tmp_path / "unifi-own.json").exists() else '{"networkconf": []}')["networkconf"] == []


def test_crash_after_create_is_rolled_back_on_the_next_start(fake, tmp_path):
    w = writer(fake, tmp_path)
    p = plan_for(w, [NEW_NET])
    w.apply(p["steps"][0])                                # written, then the process dies
    nid = model.oid("n", net_named(fake, "Smart home")["_id"])
    r = runner(w, tmp_path)
    (tmp_path / "ex" / "backup" / p["id"]).mkdir(parents=True)
    write_private(tmp_path / "ex" / "backup" / p["id"] / "before.json", {"plan": p["id"], "snapshot": {"devices": []}})
    write_private(tmp_path / "ex" / "active.json", {"plan": p["id"], "applied": True, "created": [nid]})
    w2 = writer(fake, tmp_path)                           # a fresh process: knows what it created from its ledger
    w2.read()
    r.adapter = w2
    assert r.recover()["status"] == "rolled_back"
    assert net_named(fake, "Smart home") is None


def test_watch_rolls_back_a_network_that_vanished(fake, tmp_path):
    w = writer(fake, tmp_path)
    p = plan_for(w, [NEW_NET])
    real_probe = w.probe

    def probe(steps):                                     # someone removes the new network right after it was made
        rows = fake.classic["networkconf"]["data"]
        rows[:] = [n for n in rows if n.get("name") != "Smart home"]
        return real_probe(steps)
    w.probe = probe
    now = [1000.0]
    r = Runner(w, tmp_path / "ex", target="controller", require_signature=False, watch_s=60, check_s=10,
               clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    p = dict(p, created=1000, expires=1300)
    res = r.run(p)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.rolled_back_device"
    assert json.loads((tmp_path / "unifi-own.json").read_text())["networkconf"] == []


# ---------------------------------------------------------------------------- fixed addresses
def test_reserve_and_release_a_fixed_address(fake, tmp_path):
    w = writer(fake, tmp_path)
    res = runner(w, tmp_path).run(plan_for(w, [{"kind": "dhcp.reserve", "after": {"client": PHONE, "ip": "192.168.50.77"}}]))
    assert res["status"] == "done", res
    u = fake.user(U_PHONE)
    assert u["use_fixedip"] is True and u["fixed_ip"] == "192.168.50.77" and u["network_id"] == fx.C_NET_LAN
    rid = model.oid("d", fx.MAC_PHONE)
    assert res["inverse"] == [{"kind": "dhcp.release", "object": rid, "before": {"client": PHONE, "ip": "192.168.50.77"}, "after": {}}]
    assert any(r["id"] == rid for r in w.read()["dhcp_reservations"])
    res = runner(w, tmp_path).run(plan_for(w, [{"kind": "dhcp.release", "object": model.oid("d", "02:4c:4c:00:01:09")}]))
    assert res["status"] == "done" and fake.user(U_PRINTER)["use_fixedip"] is False


def test_reservation_rules(fake, tmp_path):
    w = writer(fake, tmp_path)
    assert blocked(w, [{"kind": "dhcp.reserve", "after": {"client": PHONE, "ip": "192.168.50.40"}}]) == ["fw.block.ip_taken"]
    assert blocked(w, [{"kind": "dhcp.reserve", "after": {"client": PHONE, "ip": "192.168.50.1"}}]) == ["fw.block.ip_reserved"]
    assert blocked(w, [{"kind": "dhcp.reserve", "after": {"client": PHONE, "ip": "192.168.70.5"}}]) == ["fw.block.ip_outside"]
    assert blocked(w, [{"kind": "dhcp.reserve", "after": {"client": PRINTER, "ip": "192.168.50.41"}}]) == ["fw.block.reservation_exists"]


def test_rollback_of_a_release_puts_the_address_back(fake, tmp_path):
    w = writer(fake, tmp_path)
    p = plan_for(w, [{"kind": "dhcp.release", "object": model.oid("d", "02:4c:4c:00:01:09")},
                     {"kind": "dhcp.reserve", "after": {"client": PHONE, "ip": "192.168.50.78"}}])
    fake.fail_put = 2
    res = runner(w, tmp_path).run(p)
    assert res["status"] == "rolled_back"
    assert fake.user(U_PRINTER)["use_fixedip"] is True and fake.user(U_PRINTER)["fixed_ip"] == "192.168.50.40"
    assert not fake.user(U_PHONE).get("use_fixedip")


# ---------------------------------------------------------------------------- port forwards
CAM = {"kind": "forward.create", "after": {"name": "Camera", "proto": "tcp", "wan_port": "8443", "dst_ip": "192.168.50.30", "dst_port": "443"}}


def test_create_switch_off_and_delete_an_own_forward(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [CAM])
    token = "confirm:forward.create:" + pv["steps"][0]["object"]
    assert pv["confirm"] == [token] and pv["warnings"][0][0] == "fw.warn.forward_open"
    res = runner(w, tmp_path).run(plan_for(w, [CAM]))
    assert res["status"] == "done", res
    f = next(x for x in fake.classic["portforward"]["data"] if x["name"] == "Camera")
    assert (f["dst_port"], f["fwd"], f["fwd_port"], f["proto"], f["src"], f["enabled"]) == ("8443", "192.168.50.30", "443", "tcp", "any", True)
    fid = model.oid("f", f["_id"])
    res = runner(w, tmp_path).run(plan_for(w, [{"kind": "forward.update", "object": fid, "after": {"enabled": False}}]))
    assert res["status"] == "done" and f["enabled"] is False
    assert blocked(w, [{"kind": "forward.delete", "object": FWD_WEB}]) == ["fw.block.not_own"]
    res = runner(w, tmp_path).run(plan_for(w, [{"kind": "forward.delete", "object": fid}]))
    assert res["status"] == "done" and not [x for x in fake.classic["portforward"]["data"] if x["name"] == "Camera"]


def test_a_forward_without_its_confirmation_is_refused_by_the_gate(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [CAM])
    base = dict(fwmodel.preview(st, {}), steps=pv["steps"])
    p = fwmodel.build_plan(st, base, target="controller", user="admin", confirmed=[])
    res = runner(w, tmp_path).run(p)
    assert res["status"] == "refused" and res["reason"] == "fw.block.confirm" and not fake.config_writes


def test_forward_rules(fake, tmp_path):
    w = writer(fake, tmp_path)
    after = CAM["after"]
    assert blocked(w, [dict(CAM, after=dict(after, dst_ip="192.168.50.1"))]) == ["fw.block.forward_gateway"]
    assert blocked(w, [dict(CAM, after=dict(after, dst_ip="8.8.8.8"))]) == ["fw.block.forward_target"]
    assert blocked(w, [dict(CAM, after=dict(after, dst_ip="192.168.99.5"))]) == ["fw.block.forward_target"]
    assert blocked(w, [dict(CAM, after=dict(after, dst_ip="198.51.100.20"))]) == ["fw.block.forward_target"]   # guest network
    assert blocked(w, [dict(CAM, after=dict(after, wan_port="8080"))]) == ["fw.block.forward_clash"]          # the existing "Web"
    assert blocked(w, [dict(CAM, after=dict(after, wan_port="9000-9010", dst_port="80"))]) == ["fw.block.forward_ports"]
    # renaming or switching off never opens anything: no confirmation, no target check
    _, pv = preview(w, [{"kind": "forward.update", "object": FWD_WEB, "after": {"name": "Website"}}])
    assert not pv["blocks"] and not pv["confirm"]


def test_executor_publishes_the_new_write_keys(fake, tmp_path):
    caps = writer(fake, tmp_path).capabilities()
    for key in ("network.create", "network.update.name", "network.update.dhcp", "network.delete", "dhcp.reserve",
                "dhcp.release", "forward.update.enabled", "forward.delete"):
        assert key in caps["write"]
    assert "network.update.cidr" not in caps["write"] and "network.update.vlan" not in caps["write"]
