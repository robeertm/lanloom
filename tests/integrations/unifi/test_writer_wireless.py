"""The write half of the UniFi family, stage B wireless networks and blocked clients - against the writable fake
(console and classic style), alone and inside the executor pipeline. A password is never part of a plan, a snapshot or
a result; an SSID that carries a protected device or the executor keeps its clients; blocking is always confirmed and
goes back on the way back; the allow-list below the adapter has no kick and no radio settings."""
import json

import pytest

from lanloom.firewall import model as fwmodel
from lanloom.firewall import wireless
from lanloom.firewall.runner import Runner
from lanloom.integrations import transport
from lanloom.integrations.credentials import Secret
from lanloom.integrations.unifi import model
from lanloom.integrations.unifi import write as wmod
from lanloom.integrations.unifi.write import UnifiWriter, WriteError

from . import fixtures as fx
from .fake_writer import PW, USER, WritableFake

LAN = model.oid("n", fx.O_NET_LAN)
LAB = model.oid("n", fx.C_NET_LAB)
GUEST = model.oid("n", fx.O_NET_GUEST)
WAN = model.oid("n", fx.O_NET_WAN)
HOME = model.oid("s", fx.O_WIFI)
PHONE = model.oid("c", fx.MAC_PHONE)
OFFICE = model.oid("c", fx.MAC_OFFICE)


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
    return st, wireless.preview(st, wireless.clean_ops(st, ops), lockout=lambda s: w.lockout(st, s))


def plan_from(st, steps, confirmed=()):
    base = fwmodel.preview(st, {})
    base.update(steps=steps, warnings=[], costs={"objects": len(steps)})
    return fwmodel.build_plan(st, base, target="controller", user="admin", confirmed=list(confirmed))


def wlans(fake):
    return fake.classic["wlanconf"]["data"]


def wlan(fake, name):
    return next((w for w in wlans(fake) if w.get("name") == name), None)


def user(fake, mac):
    return next(u for u in fake.users() if u.get("mac") == mac)


# ---------------------------------------------------------------------------- reading
def test_ssids_and_blocks_read(fake, tmp_path):
    st = writer(fake, tmp_path).read()
    home = next(s for s in st["ssids"] if s["id"] == HOME)
    assert home["security"] == "wpa2_personal" and home["network"] == LAN
    assert not home.get("own")
    phone = next(d for d in st["devices"] if d["id"] == PHONE)
    assert phone["ssid"] == "Home" and phone["blocked"] is False
    assert "x_passphrase" not in json.dumps(st) and fx.PASS not in json.dumps(st)


# ---------------------------------------------------------------------------- SSIDs
def test_a_new_ssid_gets_a_random_password_that_no_plan_or_result_carries(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [{"kind": "ssid.create", "object": "new:1ab10001", "after": {"name": "Lab", "network": LAB}}])
    assert not pv["blocks"] and pv["steps"][0]["after"]["security"] == "wpa2_wpa3_personal"
    assert any(x[0] == "ws.warn.password_new" for x in pv["warnings"])
    res = runner(w, tmp_path).run(plan_from(st, pv["steps"], confirmed=pv["confirm"]))
    assert res["status"] == "done", res
    made = wlan(fake, "Lab")
    assert made["security"] == "wpapsk" and made["wpa3_support"] and made["wpa3_transition"] and made["wlan_bands"] == ["2g", "5g"]
    assert len(made["x_passphrase"]) == 24 and made["networkconf_id"] == fx.C_NET_LAB and made["wpa_enc"] == "ccmp"
    assert made["x_passphrase"] not in json.dumps(res)
    st2 = w.read()
    new = next(s for s in st2["ssids"] if s["name"] == "Lab")
    assert new["own"] is True and res["inverse"][0] == {"kind": "ssid.delete", "object": new["id"], "before": pv["steps"][0]["after"],
                                                         "after": {}}
    snap = w.snapshot([new["id"]])
    assert made["x_passphrase"] not in json.dumps(snap)
    # the way back: the own SSID can be deleted again
    back = runner(w, tmp_path / "2").run(plan_from(st2, res["inverse"]))
    assert back["status"] == "done" and wlan(fake, "Lab") is None


def test_rename_writes_only_the_name_and_never_a_password(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [{"kind": "ssid.update", "object": HOME, "after": {"name": "Home 2", "hidden": True}}])
    assert any(x[0] == "ws.warn.rename" and x[1]["n"] == 1 for x in pv["warnings"])     # the phone has to join again
    res = runner(w, tmp_path).run(plan_from(st, pv["steps"], confirmed=pv["confirm"]))
    assert res["status"] == "done", res
    w0 = wlan(fake, "Home 2")
    assert w0["hide_ssid"] is True and w0["x_passphrase"] == fx.PASS
    assert ("PUT", "wlanconf", "5f0000000000000000000601") in fake.config_writes
    assert w.writes[-1][1] == ["hide_ssid", "name"]


def test_an_open_ssid_without_a_password_gets_one_when_it_is_protected(fake, tmp_path):
    w = writer(fake, tmp_path)
    wlans(fake).append({"_id": "5f0000000000000000000602", "name": "Visitors", "security": "open", "networkconf_id": fx.C_NET_GUEST,
                        "wlan_bands": ["2g"], "enabled": True, "hide_ssid": False, "is_guest": True})
    st = w.read()
    sid = next(s["id"] for s in st["ssids"] if s["name"] == "Visitors")
    st, pv = preview(w, [{"kind": "ssid.update", "object": sid, "after": {"security": "wpa2_personal"}}])
    assert any(x[0] == "ws.warn.password_new" for x in pv["warnings"])
    assert runner(w, tmp_path).run(plan_from(st, pv["steps"]))["status"] == "done"
    assert wlan(fake, "Visitors")["security"] == "wpapsk" and len(wlan(fake, "Visitors")["x_passphrase"]) == 24


@pytest.mark.parametrize("op,why", [
    ({"kind": "ssid.update", "object": HOME, "after": {"network": LAB}}, "ws.block.private_keys"),   # private keys bind networks
    ({"kind": "ssid.update", "object": HOME, "after": {"security": "wpa3_personal"}}, "ws.block.private_keys"),
    ({"kind": "ssid.delete", "object": HOME, "after": {}}, "fw.block.not_own"),
    ({"kind": "ssid.create", "object": "new:0000000a", "after": {"name": "Open", "network": LAB, "security": "open"}}, "ws.block.open_not_guest"),
    ({"kind": "ssid.create", "object": "new:0000000a", "after": {"name": "Up", "network": WAN}}, "fw.block.network_kind"),
    ({"kind": "ssid.create", "object": "new:0000000a", "after": {"name": "home", "network": LAB}}, "ws.block.name_taken"),
    ({"kind": "ssid.create", "object": "new:0000000a", "after": {"name": "Six", "network": LAB, "bands": ["6ghz"]}}, "ws.block.6ghz_wpa3"),
    ({"kind": "ssid.update", "object": HOME, "after": {"name": "Home 3"}}, None),
])
def test_family_and_neutral_ssid_rules(fake, tmp_path, op, why):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [op])
    got = pv["blocks"][0][0] if pv["blocks"] else None
    assert got == why, pv


def test_an_open_guest_ssid_needs_its_own_confirmation(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [{"kind": "ssid.create", "object": "new:0000000b", "after": {"name": "Free", "network": GUEST, "security": "open"}}])
    assert not pv["blocks"] and pv["confirm"] == ["confirm:ssid.create:new:0000000b"]
    assert runner(w, tmp_path).run(plan_from(st, pv["steps"]))["reason"] == "fw.block.confirm"
    assert runner(w, tmp_path / "2").run(plan_from(w.read(), pv["steps"], confirmed=pv["confirm"]))["status"] == "done"
    assert wlan(fake, "Free")["security"] == "open" and "x_passphrase" not in wlan(fake, "Free")


def test_the_ssid_the_executor_uses_keeps_its_clients(fake, tmp_path, monkeypatch):
    w = writer(fake, tmp_path)
    st = w.read()
    monkeypatch.setattr(w, "self_address", lambda: "192.168.50.30")      # the executor runs on the phone
    off = {"kind": "ssid.update", "object": HOME, "before": {"enabled": True}, "after": {"enabled": False}}
    assert w.lockout(st, off) == "ws.block.self_path"
    hide = {"kind": "ssid.update", "object": HOME, "before": {"hidden": False}, "after": {"hidden": True}}
    assert w.lockout(st, hide) is None                                    # hiding never drops anyone
    block = {"kind": "client.block", "object": PHONE, "before": {"blocked": False}, "after": {"blocked": True}}
    assert w.lockout(st, block) == "ws.block.self_path"


# ---------------------------------------------------------------------------- blocks
def test_block_is_confirmed_and_undone_on_the_way_back(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [{"kind": "client.block", "object": PHONE, "after": {"blocked": True}}])
    assert pv["confirm"] == ["confirm:client.block:%s" % PHONE] and pv["warnings"][0][0] == "ws.warn.block"
    assert runner(w, tmp_path).run(plan_from(st, pv["steps"]))["reason"] == "fw.block.confirm"
    res = runner(w, tmp_path / "2").run(plan_from(w.read(), pv["steps"], confirmed=pv["confirm"]))
    assert res["status"] == "done", res
    assert fake.stamgr == [("block-sta", fx.MAC_PHONE)] and user(fake, fx.MAC_PHONE)["blocked"] is True
    assert res["inverse"] == [{"kind": "client.block", "object": PHONE, "before": {"blocked": True}, "after": {"blocked": False}}]
    st2 = w.read()
    assert next(d for d in st2["devices"] if d["id"] == PHONE)["blocked"] is True       # offline now, still listed
    back = runner(w, tmp_path / "3").run(plan_from(st2, res["inverse"], confirmed=["confirm:client.block:%s" % PHONE]))
    assert back["status"] == "done" and user(fake, fx.MAC_PHONE)["blocked"] is False
    assert fake.stamgr[-1] == ("unblock-sta", fx.MAC_PHONE)


def test_a_failure_mid_plan_unblocks_again(fake, tmp_path):
    w = writer(fake, tmp_path)
    st, pv = preview(w, [{"kind": "client.block", "object": PHONE, "after": {"blocked": True}},
                         {"kind": "ssid.update", "object": HOME, "after": {"hidden": True}}])
    fake.fail_config = 1                                                  # the SSID write fails
    res = runner(w, tmp_path).run(plan_from(st, pv["steps"], confirmed=pv["confirm"]))
    assert res["status"] == "rolled_back", res
    assert user(fake, fx.MAC_PHONE)["blocked"] is False and wlan(fake, "Home")["hide_ssid"] is False


def test_a_repeated_block_sends_nothing(fake, tmp_path):
    w = writer(fake, tmp_path)
    w.read()
    step = {"kind": "client.block", "object": OFFICE, "before": {"blocked": False}, "after": {"blocked": True}}
    w.apply(step)
    w.read()
    w.apply(step)
    assert fake.stamgr == [("block-sta", fx.MAC_OFFICE)]


# ---------------------------------------------------------------------------- below the adapter
def allows(method, path, body):
    try:
        wmod.ALLOW.check(method, path, body)
        return True
    except transport.Forbidden:
        return False


def test_allow_list_has_no_kick_and_no_radio_settings():
    sta = "/proxy/network/api/s/default/cmd/stamgr"
    assert allows("POST", sta, {"cmd": "block-sta", "mac": fx.MAC_PHONE})
    assert allows("POST", sta, {"cmd": "unblock-sta", "mac": fx.MAC_PHONE})
    assert not allows("POST", sta, {"cmd": "kick-sta", "mac": fx.MAC_PHONE})
    assert not allows("POST", sta, {"cmd": "block-sta", "mac": "FF:FF:FF:FF:FF:FF"})
    assert not allows("POST", sta, {"cmd": "block-sta", "mac": fx.MAC_PHONE, "minutes": 5})
    put = "/proxy/network/api/s/default/rest/wlanconf/5f0000000000000000000601"
    assert allows("PUT", put, {"name": "Home", "hide_ssid": True})
    for bad in ({"channel": 6}, {"tx_power_mode": "high"}, {"x_passphrase": "short"}, {"name": "x" * 33},
                {"security": "wpaeap"}, {"radius_profile_id": "5f0000000000000000000601"}, {}):
        assert not allows("PUT", put, bad), bad
    assert allows("DELETE", put, None)


def test_transport_refuses_a_body_the_builder_never_makes(fake, tmp_path):
    w = writer(fake, tmp_path)
    w.read()
    with pytest.raises(WriteError) as e:
        w._write("PUT", "wlanconf", "5f0000000000000000000601", {"channel": 11})
    assert e.value.key == "unifi.refused_by_allow_list"


def test_passwords_are_long_and_random():
    a, b = UnifiWriter._new_password(), UnifiWriter._new_password()
    assert a != b and len(a) == 24 and wmod._passphrase(a) is None
