"""Read half of the OpenWrt family against the stateful fake of the router's ubus API: only checked read calls, one
session for many polls, a sign-in limit, calls the access list leaves out skipped without new sign-ins, secrets never
kept, the account check from the access list, and the topology observation."""
import ast
import asyncio
import json
from pathlib import Path

import pytest

from lanloom.integrations import transport
from lanloom.integrations.openwrt import model, read
from lanloom.integrations.openwrt.read import OpenwrtReader
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake_ubus import FakeUbus


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


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


def reader(fake, clock=None, **kw):
    return OpenwrtReader(url=fake.url, username=kw.pop("username", fx.READ_USER), password=kw.pop("password", fx.READ_PASSWORD),
                         clock=clock or Mono(), **kw)


def body(obj, method, args):
    return model.call_body(model.ZERO_SID if (obj, method) == ("session", "login") else "c" * 32, obj, method, args)


@pytest.mark.parametrize("obj,method,args", [
    ("session", "login", {"username": "lanloom", "password": "x", "timeout": 900}),
    ("system", "board", {}), ("system", "info", {}),
    ("uci", "get", {"config": "network"}), ("uci", "get", {"config": "firewall"}), ("uci", "get", {"config": "dhcp"}),
    ("uci", "get", {"config": "wireless"}),
    ("network.interface", "dump", {}), ("network.device", "status", {}),
    ("luci-rpc", "getHostHints", {}), ("luci-rpc", "getDHCPLeases", {}),
    ("iwinfo", "devices", {}), ("iwinfo", "info", {"device": "phy0-ap0"}), ("iwinfo", "assoclist", {"device": "wlan0"}),
])
def test_read_allow_list_lets_the_read_calls_through(obj, method, args):
    read.ALLOW.check("POST", "/ubus", body(obj, method, args))


@pytest.mark.parametrize("obj,method,args", [
    ("uci", "set", {"config": "firewall", "section": "x", "values": {"enabled": "0"}}),
    ("uci", "add", {"config": "firewall", "type": "rule"}), ("uci", "delete", {"config": "firewall", "section": "x"}),
    ("uci", "apply", {"rollback": False}), ("uci", "commit", {"config": "firewall"}), ("uci", "confirm", {}),
    ("uci", "get", {"config": "rpcd"}), ("uci", "get", {"config": "system"}), ("uci", "get", {"config": "uhttpd"}),
    ("uci", "get", {"config": "firewall", "section": "x"}),
    ("file", "read", {"path": "/etc/shadow"}), ("file", "exec", {"command": "/bin/sh"}), ("system", "reboot", {}),
    ("rc", "init", {"name": "firewall", "action": "stop"}), ("luci", "setPassword", {"username": "root", "password": "x"}),
    ("iwinfo", "scan", {"device": "phy0-ap0"}), ("iwinfo", "info", {"device": "bad name"}),
    ("session", "login", {"username": "lanloom", "password": "x"}),
    ("session", "destroy", {}),
])
def test_read_allow_list_refuses_everything_else(obj, method, args):
    with pytest.raises(transport.Forbidden):
        read.ALLOW.check("POST", "/ubus", body(obj, method, args))


def test_read_allow_list_holds_no_write_rule():
    assert not read.ALLOW.write and [r.method for r in read.ALLOW.rules] == ["POST"] and read.ALLOW.rules[0].query
    with pytest.raises(transport.Forbidden):
        read.ALLOW.check("GET", "/ubus")
    with pytest.raises(transport.Forbidden):
        read.ALLOW.check("POST", "/cgi-bin/luci/admin", body("system", "board", {}))


def test_the_module_reaches_the_network_only_through_the_transport():
    src = Path(read.__file__).read_text()
    names = {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not names & {"http", "socket", "urllib", "requests", "http.client", "ssl"}


def test_a_poll_reads_every_area_with_one_sign_in(fake):
    r = reader(fake)
    raw = r.collect()
    assert fake.login_count == 1
    assert set(raw) >= {"board", "network", "firewall", "dhcp", "wireless", "ifaces", "devices", "hints", "leases", "air"}
    assert set(raw["air"]) == {"phy0-ap0", "phy1-ap0", "phy0-ap1", "phy0-ap2"}         # the client interface is skipped
    r.collect()
    assert fake.login_count == 1                                                        # the session is reused
    assert all(m in ("login", "board", "info", "get", "dump", "status", "getHostHints", "getDHCPLeases", "devices", "info",
                     "assoclist") for o, m, a in fake.requests)
    blob = json.dumps(raw)
    assert not any(s in blob for s in fx.SECRETS)


def test_events_carry_the_state_and_the_topology(fw4):
    r = reader(fw4)
    evs = asyncio.run(r.poll())
    cfg, topo = evs
    assert cfg["type"] == "config" and cfg["family"] == "openwrt" and cfg["capabilities"]["write"] == {}
    assert cfg["capabilities"]["rollback"]["device_native"] is False
    assert topo["type"] == "topology" and topo["source"] == "controller"
    assert topo["devices"][0]["mac"] == fx.ROUTER_MAC and topo["devices"][0]["role"] == "router"
    assert {c["mac"]: c["ssid"] for c in topo["clients"]} == {fx.PHONE_MAC: "Home", fx.PLUG_MAC: "Things"}
    assert all(c["parent_mac"] == fx.ROUTER_MAC and c["medium"] == "radio" for c in topo["clients"])


def test_a_router_without_wireless_or_web_helper_still_reads(fw4):
    f = FakeUbus("23.05", wireless=False, helper=False)
    try:
        r = reader(f)
        ev = r.config_event(r.collect())
        st = ev["state"]
        assert st["ssids"] == [] and st["zones"] and st["policies"]
        assert "ssids" not in ev["capabilities"]["read"]
        n = f.login_count
        r.collect()
        assert f.login_count == n
    finally:
        f.close()


def test_a_call_the_access_list_leaves_out_is_skipped_without_new_sign_ins(fw4):
    fw4.logins[fx.READ_USER][1]["ubus"].pop("luci-rpc")
    clock = Mono()
    r = reader(fw4, clock)
    raw = r.collect()
    assert raw["hints"] is None and raw["leases"] is None and raw["firewall"]
    assert fw4.login_count == 1
    clock.t += 300
    r.collect()
    assert fw4.login_count == 1
    assert len([1 for o, m, a in fw4.requests if o == "luci-rpc"]) == 2                 # skipped for an hour after the first try
    clock.t += read.SKIP_S
    r.collect()
    assert len([1 for o, m, a in fw4.requests if o == "luci-rpc"]) == 4


def test_an_expired_session_signs_in_once_more(fw4):
    clock = Mono()
    r = reader(fw4, clock)
    r.collect()
    fw4.expire_sessions()
    clock.t += read.SESSION_S
    r.collect()
    assert fw4.login_count == 2


def test_a_wrong_password_pauses_and_never_floods(fw4):
    clock = Mono()
    r = reader(fw4, clock, password="wrong")
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.key_refused"
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.paused" and fw4.calls("login") == [("session", "login", {"username": fx.READ_USER, "timeout": 900})]


def test_sign_ins_are_limited_per_hour(fw4):
    clock = Mono()
    r = reader(fw4, clock)
    for _ in range(4):
        r._sid = None
        r._login()
    r._sid = None
    with pytest.raises(SourceError) as e:
        r._login()
    assert e.value.key == "source.family.paused"
    clock.t += 3601
    r._login()


def test_account_scope_from_the_access_list(fw4):
    assert reader(fw4).scope() == "read_only"
    assert reader(fw4, username=fx.WRITE_USER, password=fx.WRITE_PASSWORD).scope() == "can_write"
    assert read.scope_of({"ubus": {"*": ["*"]}, "uci": {"*": ["read", "write"]}}) == "can_write"     # the administrator
    assert read.scope_of({"ubus": {"uci": ["get"]}, "uci": {"firewall": ["read", "write"]}}) == "can_write"
    assert read.scope_of({"ubus": {"uci": ["get"]}, "file": {"/tmp/*": ["write"]}}) == "can_write"
    assert read.scope_of({"ubus": {"file": ["exec"]}}) == "can_write"
    assert read.scope_of({}) == "unknown" and read.scope_of(None) == "unknown"
    assert asyncio.run(reader(fw4).account_scope()) == "read_only"


def test_options_are_checked_and_nothing_secret_shows(fw4):
    for kw in ({"url": "ftp://x"}, {"url": ""}, {"username": "a b"}, {"password": ""}):
        args = dict(url=fw4.url, username=fx.READ_USER, password=fx.READ_PASSWORD)
        args.update(kw)
        with pytest.raises(SourceError):
            OpenwrtReader(**args)
    r = reader(fw4)
    r.collect()
    assert fx.READ_PASSWORD not in repr(r) and r._sid not in repr(r) and r._sid not in str(r)


def test_connection_test(fw4):
    ok, key, details = asyncio.run(reader(fw4).test())
    assert ok and key == "source.family.ok"
    assert details["zones"] == 5 and details["ssids"] == 4 and details["version"] == "23.05" and details["clients"] == 4


def test_unreachable_router_is_a_reason_key():
    r = OpenwrtReader(url="http://127.0.0.1:9", username=fx.READ_USER, password=fx.READ_PASSWORD, timeout=1)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.unreachable"
