"""The read half against a local stand-in of the REST interface: what is sent, what is refused, error reasons, fallbacks."""
import asyncio
import json
import time

import pytest

from lanloom.firewall import neutral
from lanloom.integrations import capabilities
from lanloom.integrations.credentials import Secret
from lanloom.integrations.mikrotik import read
from lanloom.integrations.transport import Forbidden
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake_router import FakeRouter


@pytest.fixture(autouse=True)
def fresh_pause():
    read._PAUSED.clear()
    yield
    read._PAUSED.clear()


@pytest.fixture()
def rtr():
    srv = FakeRouter()
    yield srv
    srv.stop()


def reader(srv, **kw):
    args = {"url": srv.url, "username": fx.USER, "password": fx.PASSWORD}
    args.update(kw)
    return read.MikrotikReader(**args)


# ---------------------------------------------------------------------------- constructor
@pytest.mark.parametrize("kw", [{"url": ""}, {"url": None}, {"url": "ftp://x"}, {"url": "https://user:pw@x"}, {"url": "https://a b"},
                                {"url": "https://x/rest"}, {"password": ""}, {"password": None}, {"username": ""},
                                {"username": "a b"}, {"username": "a\nb"}, {"username": "a:b"}])
def test_bad_settings_are_refused(kw):
    args = {"url": "https://192.0.2.1", "username": fx.USER, "password": fx.PASSWORD}
    args.update(kw)
    with pytest.raises(SourceError) as e:
        read.MikrotikReader(**args)
    assert e.value.key == "source.family.config"


def test_secret_wrapper_is_accepted_and_nothing_shows_the_password(rtr):
    r = reader(rtr, password=Secret(fx.PASSWORD))
    r.collect()
    for text in (repr(r), str(r), repr(r._t), repr(vars(r)), repr(read.ALLOW.rules)):
        assert fx.PASSWORD not in text
    assert repr(r) == "MikrotikReader(%s)" % rtr.url.split("//")[1]


# ---------------------------------------------------------------------------- the allow-list
def test_read_allow_list_holds_get_rules_only():
    assert read.ALLOW.write is False
    assert {r.method for r in read.ALLOW.rules} == {"GET"}
    assert all(r.fields is None for r in read.ALLOW.rules)
    paths = set(read.LIGHT.values()) | set(read.FULL.values()) | set(read.ACCOUNT_PATHS.values())
    assert len(read.ALLOW.rules) == len(paths)


def test_every_path_is_a_rest_path_and_no_command_path_is_among_them():
    for path in list(read.LIGHT.values()) + list(read.FULL.values()) + list(read.ACCOUNT_PATHS.values()):
        assert path.startswith("/rest/") and "?" not in path and ".." not in path
        words = set(path.split("/"))
        assert not words & {"print", "set", "add", "remove", "reboot", "shutdown", "export", "file", "script", "reset",
                            "password", "enable", "disable", "run", "monitor", "scan", "check-for-updates"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/rest/system/identity", "/rest/ip/firewall/filter", "/rest/interface/ether1", "/rest/system/reboot",
                                  "/rest/ip/firewall/filter/*B0", "/rest/user"])
def test_every_changing_method_is_refused_before_a_byte_is_sent(rtr, method, path):
    r = reader(rtr)
    for body in (None, {}, {"name": "x"}):
        with pytest.raises(Forbidden):
            r._t.request(method, path, body)
    assert rtr.requests == [] and r._t.refused == 3 and r._t.sent == 0


@pytest.mark.parametrize("path", [
    "/rest/system/reboot", "/rest/system/shutdown", "/rest/system/reset-configuration", "/rest/export", "/rest/file",
    "/rest/ip/firewall/filter/*B0", "/rest/ip/firewall/filter?chain=input", "/rest/interface/ether1", "/rest/system/script",
    "/rest/system/resource/print", "/rest/ip/address/../user", "/rest/user/active", "/rest/ip/service", "/rest/tool/fetch",
    "/rest/system/identity/", "/rest", "/", "/webfig/", "/rest/ppp/secret", "/rest/ip/hotspot/user",
])
def test_every_get_outside_the_fixed_paths_is_refused(rtr, path):
    r = reader(rtr)
    with pytest.raises(Forbidden):
        r._t.request("GET", path)
    assert rtr.requests == []


def test_a_get_with_a_body_is_not_a_thing_either(rtr):
    r = reader(rtr)
    with pytest.raises(Forbidden):
        r._t.request("GET", "/rest/system/resource", {"a": 1})
    assert rtr.requests == []


def test_only_get_was_ever_sent_by_a_full_poll_and_the_account_check(rtr):
    r = reader(rtr)
    r.collect()
    r.scope()
    assert {x["method"] for x in rtr.requests} == {"GET"}
    assert set(rtr.paths()) <= set(read.LIGHT.values()) | set(read.FULL.values()) | set(read.ACCOUNT_PATHS.values())


# ---------------------------------------------------------------------------- authentication
def test_basic_authentication_on_every_request_and_the_password_only_there(rtr):
    reader(rtr).collect()
    assert rtr.requests and all(x["has_auth"] for x in rtr.requests)
    for x in rtr.requests:
        assert fx.PASSWORD not in x["path"] and fx.PASSWORD.encode() not in x["body"]
        assert fx.PASSWORD not in json.dumps(x["headers"])


# ---------------------------------------------------------------------------- the reads
def test_full_poll_asks_each_path_once_and_returns_every_area(rtr):
    r = reader(rtr)
    raw = r.collect()
    assert sorted(rtr.paths()) == sorted(set(read.LIGHT.values()) | set(read.FULL.values()))
    assert len(rtr.paths()) == len(read.LIGHT) + len(read.FULL)
    assert set(raw) == set(read.LIGHT) | set(read.FULL)
    assert raw["resource"]["board-name"] == "hEX S" and isinstance(raw["interfaces"], list)
    assert raw["identity"] == {"name": "edge-router"}                      # single objects come back as an object
    assert set(r.last_areas) == set(raw)


def test_a_light_poll_asks_the_light_paths_only_and_reuses_the_last_full_answer(rtr):
    r = reader(rtr)
    r.collect()
    rtr.requests.clear()
    raw = r.collect(light=True)
    assert sorted(rtr.paths()) == sorted(read.LIGHT.values())
    assert set(raw) == set(read.LIGHT) | set(read.FULL)
    assert raw["filter"] and raw["addresses"] and raw["identity"]


def test_a_light_poll_before_any_full_poll_has_no_config_areas(rtr):
    raw = reader(rtr).collect(light=True)
    assert "filter" not in raw and "addresses" not in raw and raw["interfaces"]


def test_a_value_is_scrubbed_of_secrets_before_it_is_kept():
    obj = {"name": "x", "password": "p", "user": "u", "nested": [{"private-key": "k", "ok": "1", "pre-shared-key": "z"}],
           "secret": "s", "token": "t", "certificate": "c", "keyring": {"a": 1}}
    out = read.scrub(obj)
    assert out == {"name": "x", "user": "u", "nested": [{"ok": "1"}], "keyring": {"a": 1}}


def test_secrets_of_the_router_never_enter_the_collected_data(rtr):
    rtr.data["pppoe_clients"][0]["password"] = "pppoe-secret-pw"
    rtr.data["interfaces"][0]["wpa-pre-shared-key"] = "radio-key"
    raw = reader(rtr).collect()
    assert "pppoe-secret-pw" not in json.dumps(raw) and "radio-key" not in json.dumps(raw)


def test_config_event_shape_and_capabilities(rtr):
    r = reader(rtr)
    ev = r.config_event(r.collect())
    neutral.validate(ev["state"])
    assert ev["type"] == "config" and ev["source"] == "family" and ev["family"] == "mikrotik" and isinstance(ev["t"], (int, float))
    caps = capabilities.validate(ev["capabilities"], "mikrotik")
    assert caps["write"] == {} and caps["api"]["version"] == fx.VERSION and r.capabilities()["write"] == {}
    assert {"inventory", "clients", "topology", "networks", "ports", "policies", "port_forwards", "wans"} <= set(caps["read"])
    assert "ssids" not in caps["read"] and "zones" not in caps["read"]


def test_poll_returns_topology_and_config_and_a_light_poll_only_topology(rtr):
    r = reader(rtr)
    full = asyncio.run(r.poll(light=False))
    assert [e["type"] for e in full] == ["topology", "config"]
    light = asyncio.run(r.poll(light=True))
    assert [e["type"] for e in light] == ["topology"]
    assert light[0]["devices"] and light[0]["devices"][0]["name"] == "edge-router"        # the cached identity is still used


def test_test_probe_counts(rtr):
    ok, key, details = asyncio.run(reader(rtr).test())
    assert ok and key == "source.family.ok"
    assert details == {"version": fx.VERSION, "site": "edge-router", "devices": 7, "networks": 3, "ports": 6, "port_forwards": 2}


# ---------------------------------------------------------------------------- sign-in problems
def test_wrong_password_pauses_instead_of_retrying(rtr):
    r = reader(rtr, password="not-the-password")
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed" and "not-the-password" not in repr(e.value) + str(e.value)
    assert len(rtr.requests) == 1                               # the first refused request ended the poll
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.paused" and len(rtr.requests) == 1
    with pytest.raises(SourceError) as e:
        reader(rtr, password="not-the-password").collect()           # also not through another object for the same login
    assert e.value.key == "source.family.paused" and len(rtr.requests) == 1
    assert reader(rtr).collect()["resource"]                         # a corrected password is tried at once


def test_the_pause_ends_after_fifteen_minutes(rtr, monkeypatch):
    r = reader(rtr, password="not-the-password")
    with pytest.raises(SourceError):
        r.collect()
    assert read.PAUSE_S == 900.0
    t0 = time.monotonic()
    monkeypatch.setattr(read.time, "monotonic", lambda: t0 + 899)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.paused"
    monkeypatch.setattr(read.time, "monotonic", lambda: t0 + 901)
    n = len(rtr.requests)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed" and len(rtr.requests) == n + 1


def test_a_pause_of_one_login_does_not_stop_another_user(rtr):
    with pytest.raises(SourceError):
        reader(rtr, username="someone").collect()
    assert reader(rtr).collect()["resource"]


def test_http_errors_other_replies_and_unreachable(rtr):
    rtr.mode = "http"
    rtr.http_status = 503
    with pytest.raises(SourceError) as e:
        reader(rtr).collect()
    assert e.value.key == "source.family.protocol"
    rtr.mode = "html"
    with pytest.raises(SourceError) as e:
        reader(rtr).collect()
    assert e.value.key == "source.family.protocol"                  # a page instead of JSON: not the router's REST interface
    rtr.stop()
    with pytest.raises(SourceError) as e:
        reader(rtr, timeout=1.0).collect()
    assert e.value.key == "source.family.unreachable"


# ---------------------------------------------------------------------------- user rights and releases
def test_a_path_the_user_may_not_read_is_left_out_for_an_hour_and_the_rest_shows(rtr):
    rtr.denied = {"/ip/firewall/", "/ip/dhcp-server/lease"}
    r = reader(rtr)
    raw = r.collect()
    assert "filter" not in raw and "nat" not in raw and "leases" not in raw and raw["interfaces"] and raw["routes"]
    ev = r.config_event(raw)
    assert "policies" not in ev["capabilities"]["read"] and "port_forwards" not in ev["capabilities"]["read"]
    assert "dhcp_reservations" not in ev["capabilities"]["read"] and "wans" in ev["capabilities"]["read"]
    assert ev["state"]["raw"] == [] and ev["state"]["port_forwards"] == []
    rtr.requests.clear()
    r.collect()
    assert not any("/ip/firewall/" in p or "lease" in p for p in rtr.paths())     # not asked again within the hour


def test_a_skipped_path_is_asked_again_after_an_hour(rtr, monkeypatch):
    rtr.denied = {"/ip/firewall/filter"}
    r = reader(rtr)
    r.collect()
    assert read.SKIP_S == 3600.0
    rtr.denied = set()
    t0 = time.monotonic()
    monkeypatch.setattr(read.time, "monotonic", lambda: t0 + 3500)
    assert "filter" not in r.collect()
    monkeypatch.setattr(read.time, "monotonic", lambda: t0 + 3700)
    assert r.collect()["filter"]


def test_a_missing_path_or_missing_package_is_left_out_whatever_status_says(rtr):
    rtr.missing = {"/interface/ethernet/poe"}
    rtr.bad_request = {read.LIGHT["wifi_reg"], "/interface/wireless/registration-table"}
    r = reader(rtr)
    raw = r.collect()
    assert "poe" not in raw and "wifi_reg" not in raw and "wireless_reg" not in raw and raw["interfaces"]
    st = r.config_event(raw)["state"]
    assert st["ports"] and all(p["poe"] is None for p in st["ports"])
    assert all(d["wired"] for d in st["devices"])                           # without a registration table nobody is wireless


def test_a_user_that_may_read_nothing_is_an_access_problem(rtr):
    rtr.denied = {"/rest/"}
    with pytest.raises(SourceError) as e:
        reader(rtr).collect()
    assert e.value.key == "source.family.forbidden"


def test_a_user_that_may_not_read_the_interface_list_is_an_access_problem(rtr):
    rtr.denied = {"/rest/interface", "/rest/system/resource"}
    with pytest.raises(SourceError) as e:
        reader(rtr).collect()
    assert e.value.key == "source.family.forbidden"


def test_a_router_that_answers_neither_resources_nor_interfaces_is_a_protocol_problem(rtr):
    rtr.missing = {"/rest/interface", "/rest/system/resource"}
    with pytest.raises(SourceError) as e:
        reader(rtr).collect()
    assert e.value.key in ("source.family.protocol", "source.family.forbidden")


def test_empty_lists_are_answers_not_missing_areas(rtr):
    rtr.data["filter"] = []
    r = reader(rtr)
    raw = r.collect()
    assert raw["filter"] == []
    assert "policies" in r.config_event(raw)["capabilities"]["read"]


# ---------------------------------------------------------------------------- account rights
def test_account_scope_read_only_for_a_view_only_group_and_the_builtin_read_group(rtr):
    assert reader(rtr).scope() == "read_only"
    rtr.users = fx.users("read")                                      # the router's own read group (it also has test, sniff, ...)
    assert asyncio.run(reader(rtr).account_scope()) == "read_only"
    assert [x["method"] for x in rtr.requests] and {x["method"] for x in rtr.requests} == {"GET"}


def test_account_scope_can_write_for_a_group_with_write_policy(rtr):
    rtr.users = fx.users("full")
    assert reader(rtr).scope() == "can_write"
    assert asyncio.run(reader(rtr).account_scope()) == "can_write"


@pytest.mark.parametrize("policy,expect", [("read,api,rest-api,write", "can_write"), ("read,policy", "can_write"),
                                           ("read,reboot", "can_write"), ("read,ftp", "can_write"),
                                           ("read,!write,!policy,!reboot,!ftp", "read_only"), ("read,test,sniff,sensitive,password,romon", "read_only"),
                                           ("api,rest-api", "unknown"), ("", "unknown")])
def test_account_scope_by_policy_list(rtr, policy, expect):
    rtr.groups = [{"name": "viewonly", "policy": policy}]
    assert reader(rtr).scope() == expect


def test_account_scope_is_unknown_when_the_lists_cannot_be_read_or_do_not_name_the_user(rtr):
    rtr.denied = {"/rest/user"}
    assert reader(rtr).scope() == "unknown"
    rtr.denied = set()
    rtr.users = [{"name": "somebody-else", "group": "full"}]
    assert reader(rtr).scope() == "unknown"
    rtr.users = fx.users("nonexistent-group")
    assert reader(rtr).scope() == "unknown"


def test_account_scope_is_unknown_for_a_wrong_login_and_an_unreachable_router(rtr):
    r = reader(rtr, password="wrong")
    assert r.scope() == "unknown"
    with pytest.raises(SourceError) as e:                              # the account check did not use up the sign-in: the poll says why
        r.collect()
    assert e.value.key == "source.family.auth_failed"
    assert r.scope() == "unknown" and len(rtr.requests) == 2           # and now it is paused: nothing more is sent
    rtr.stop()
    assert reader(rtr, timeout=1.0).scope() == "unknown"
