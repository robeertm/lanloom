"""The read half against a local fake of the Open API: what is sent, what is refused, error reasons, fallbacks."""
import asyncio
import json
import re

import pytest

from lanloom.firewall import neutral
from lanloom.integrations import capabilities
from lanloom.integrations.omada import read
from lanloom.integrations.transport import Forbidden
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake_controller import FakeController


@pytest.fixture(autouse=True)
def fresh_tokens():
    read._reset_for_tests()
    yield
    read._reset_for_tests()


@pytest.fixture()
def ctl():
    srv = FakeController()
    yield srv
    srv.stop()


def reader(srv, **kw):
    args = {"url": srv.url, "client_id": fx.CLIENT_ID, "client_secret": fx.CLIENT_SECRET}
    args.update(kw)
    return read.OmadaReader(**args)


def paths(srv, method="GET"):
    return [re.sub(r"\A/openapi/v1/%s/sites/%s/?" % (fx.CID, fx.SITE_ID), "", r["path"]) for r in srv.requests if r["method"] == method]


# ---------------------------------------------------------------------------- constructor
@pytest.mark.parametrize("kw", [{"url": ""}, {"url": None}, {"url": "ftp://x"}, {"url": "https://user:pw@x"}, {"url": "https://a b"},
                                {"client_secret": ""}, {"client_secret": None}, {"client_id": ""}, {"client_id": "a b"},
                                {"client_id": "a\nb"}, {"controller_id": "../x"}, {"controller_id": "a b"}])
def test_bad_settings_are_refused(kw):
    args = {"url": "https://192.0.2.1:8043", "client_id": fx.CLIENT_ID, "client_secret": fx.CLIENT_SECRET}
    args.update(kw)
    with pytest.raises(SourceError) as e:
        read.OmadaReader(**args)
    assert e.value.key == "source.family.config"


def test_repr_str_and_state_hide_the_secret_and_the_token(ctl):
    r = reader(ctl)
    r.collect()
    token = next(iter(ctl.tokens))
    for text in (repr(r), str(r), repr(r._t), repr(vars(r)), repr(read._TOKENS)):
        assert fx.CLIENT_SECRET not in text and token not in text


# ---------------------------------------------------------------------------- the allow-list
def test_read_allow_list_holds_gets_and_the_token_exchange_only():
    assert read.ALLOW.write is False
    posts = [r for r in read.ALLOW.rules if r.method == "POST"]
    assert len(posts) == 1 and posts[0].login and posts[0].fields == frozenset({"omadacId", "client_id", "client_secret"})
    assert all(r.method in ("GET", "POST") for r in read.ALLOW.rules)


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/openapi/v1/%s/sites/%s/clients" % (fx.CID, fx.SITE_ID), {"a": 1}),
    ("DELETE", "/openapi/v1/%s/sites/%s/clients/3C-00-5E-30-00-01" % (fx.CID, fx.SITE_ID), None),
    ("POST", "/openapi/v1/%s/sites/%s/clients/3C-00-5E-30-00-01/block" % (fx.CID, fx.SITE_ID), {}),
    ("POST", "/openapi/v1/%s/sites/%s/devices/02-00-5E-20-00-01/reboot" % (fx.CID, fx.SITE_ID), {}),
    ("GET", "/openapi/v1/%s/sites/%s/clients/3C-00-5E-30-00-01" % (fx.CID, fx.SITE_ID), None),          # one client's detail: not asked for
    ("GET", "/openapi/v1/%s/sites/%s/wireless-network/ssids/ss-home" % (fx.CID, fx.SITE_ID), None),    # the detail reply carries the passphrase
    ("GET", "/openapi/v1/%s/sites/%s/wireless-network/wlans/wl-default/ssids/ss-home" % (fx.CID, fx.SITE_ID), None),
    ("GET", "/openapi/v1/%s/sites/%s/devices?page=1&pageSize=100&x=1" % (fx.CID, fx.SITE_ID), None),
    ("GET", "/openapi/v1/%s/sites/%s/devices/../x" % (fx.CID, fx.SITE_ID), None),
    ("GET", "/openapi/v1/%s/users" % fx.CID, None),
    ("GET", "/openapi/authorize/token?grant_type=client_credentials", None),
    ("POST", "/openapi/authorize/token?grant_type=client_credentials", {"omadacId": "x", "client_id": "y"}),
    ("POST", "/openapi/authorize/token?grant_type=client_credentials", {"omadacId": "x", "client_id": "y", "client_secret": "z", "extra": 1}),
    ("POST", "/openapi/authorize/token?grant_type=authorization_code", {"omadacId": "x", "client_id": "y", "client_secret": "z"}),
    ("POST", "/openapi/authorize/login", {"omadacId": "x", "client_id": "y", "client_secret": "z"}),
])
def test_everything_else_is_refused_before_a_byte_is_sent(ctl, method, path, body):
    r = reader(ctl)
    with pytest.raises(Forbidden):
        r._t.request(method, path, body)
    assert ctl.requests == [] and r._t.refused == 1


# ---------------------------------------------------------------------------- the full read
def test_full_poll_asks_exactly_the_documented_paths_with_one_token(ctl):
    r = reader(ctl)
    raw = r.collect()
    q = "?page=1&pageSize=100"
    top = [r_["path"] for r_ in ctl.requests if not r_["path"].startswith("/openapi/v1/%s/sites/%s" % (fx.CID, fx.SITE_ID))]
    assert top == ["/api/info", "/openapi/authorize/token?grant_type=client_credentials", "/openapi/v1/%s/sites%s" % (fx.CID, q)]
    assert paths(ctl)[2:] == ["devices" + q, "clients" + q, "lan-networks" + q, "lan-profiles" + q, "acls/osg-acls" + q,
                              "acls/osw-acls" + q, "acls/eap-acls" + q, "gateways/%s/wan-status" % fx.GW, "switches/%s" % fx.CORE,
                              "stat/switches/%s" % fx.CORE, "wireless-network/wlans", "wireless-network/wlans/wl-default/ssids" + q]
    assert ctl.exchanges == 1
    data = [r_ for r_ in ctl.requests if r_["path"].startswith("/openapi/v1")]
    assert data and all(r_["auth_scheme"] == "AccessToken" for r_ in data)
    assert next(r_ for r_ in ctl.requests if r_["path"] == "/api/info")["has_auth"] is False
    assert raw["site"]["name"] == "Default" and raw["info"]["controllerVer"] == fx.VERSION


def test_the_secret_only_travels_in_the_token_exchange(ctl):
    reader(ctl).collect()
    for rec in ctl.requests:
        if rec["path"].startswith("/openapi/authorize/token"):
            assert json.loads(rec["body"]) == {"omadacId": fx.CID, "client_id": fx.CLIENT_ID, "client_secret": fx.CLIENT_SECRET}
        else:
            assert fx.CLIENT_SECRET.encode() not in rec["body"] and fx.CLIENT_SECRET not in rec["path"]
            assert fx.CLIENT_SECRET not in json.dumps(rec["headers"])


def test_only_get_and_the_one_post_were_ever_sent(ctl):
    reader(ctl).collect()
    assert {r["method"] for r in ctl.requests} == {"GET", "POST"}


def test_a_second_poll_reuses_the_token(ctl):
    r = reader(ctl)
    r.collect()
    r.collect(light=True)
    reader(ctl).collect(light=True)                                   # another object for the same controller and application
    assert ctl.exchanges == 1


def test_config_event_shape_and_capabilities(ctl):
    r = reader(ctl)
    ev = r.config_event(r.collect())
    neutral.validate(ev["state"])
    assert ev["type"] == "config" and ev["source"] == "family" and ev["family"] == "omada" and isinstance(ev["t"], (int, float))
    caps = capabilities.validate(ev["capabilities"], "omada")
    assert caps["write"] == {} and caps["api"]["version"] == fx.VERSION and r.capabilities()["write"] == {}
    assert {"inventory", "clients", "topology", "networks", "acl", "ports", "port_profiles", "ssids", "wans"} <= set(caps["read"])


def test_poll_returns_topology_and_config_and_a_light_poll_only_topology(ctl):
    r = reader(ctl)
    full = asyncio.run(r.poll(light=False))
    assert [e["type"] for e in full] == ["topology", "config"]
    ctl.requests.clear()
    light = asyncio.run(r.poll(light=True))
    assert [e["type"] for e in light] == ["topology"]
    assert [p for p in paths(ctl) if not p.startswith(("/api/", "/openapi/"))] == ["devices?page=1&pageSize=100", "clients?page=1&pageSize=100"]


def test_offline_switches_are_not_asked_for_their_ports(ctl):
    reader(ctl).collect()
    asked = paths(ctl)
    assert "switches/%s" % fx.CORE in asked and not any(fx.DESK in p for p in asked)


def test_lists_are_read_over_all_pages(ctl):
    ctl.page_cap = 2
    raw = reader(ctl).collect()
    assert len(raw["clients"]) == 7 and len(raw["devices"]) == 5
    assert any(p.startswith("clients?page=4") for p in paths(ctl))


def test_test_probe_counts(ctl):
    ok, key, details = asyncio.run(reader(ctl).test())
    assert ok and key == "source.family.ok"
    assert details == {"version": fx.VERSION, "site": "Default", "devices": 5, "clients": 7, "networks": 3, "ssids": 4, "ports": 8}


def test_test_probe_reports_the_reason(ctl):
    ctl.mode = "bad_secret"
    ok, key, _ = asyncio.run(reader(ctl).test())
    assert not ok and key == "source.family.auth_failed"


# ---------------------------------------------------------------------------- controller id and sites
def test_the_controller_id_is_found_by_itself(ctl):
    r = reader(ctl)
    r.collect()
    assert json.loads(next(x for x in ctl.requests if x["method"] == "POST")["body"])["omadacId"] == fx.CID


def test_a_given_controller_id_is_used(ctl):
    reader(ctl, controller_id=fx.CID).collect()
    assert ctl.exchanges == 1


def test_missing_id_the_controller_does_not_announce_is_a_settings_problem():
    srv = FakeController(announce_id=False)
    try:
        with pytest.raises(SourceError) as e:
            reader(srv).collect()
        assert e.value.key == "source.family.config" and srv.exchanges == 0
    finally:
        srv.stop()


def test_wrong_controller_id_has_its_own_reason(ctl):
    with pytest.raises(SourceError) as e:
        reader(ctl, controller_id="ffffffffffffffffffffffffffffffff").collect()
    assert e.value.key == "source.family.controller_id"


def test_site_by_name_is_case_insensitive_primary_is_the_default(ctl):
    assert reader(ctl, site="DEFAULT").collect()["site"]["name"] == "Default"
    assert reader(ctl).collect()["site"]["name"] == "Default"


def test_unknown_site_and_ambiguous_sites_are_settings_problems(ctl):
    with pytest.raises(SourceError) as e:
        reader(ctl, site="Nowhere").collect()
    assert e.value.key == "source.family.config"
    ctl.sites = [dict(s, primary=False) for s in ctl.sites]
    with pytest.raises(SourceError) as e:
        reader(ctl).collect()
    assert e.value.key == "source.family.config"
    ctl.sites = ctl.sites[:1]
    assert reader(ctl).collect()["site"]["name"] == "Default"            # a single site needs no name


# ---------------------------------------------------------------------------- sign-in problems
def test_refused_exchange_pauses_instead_of_retrying(ctl):
    ctl.mode = "bad_secret"
    r = reader(ctl)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed" and fx.CLIENT_SECRET not in repr(e.value) + str(e.value)
    assert ctl.exchanges == 1
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.paused" and ctl.exchanges == 1          # no second attempt reached the controller
    with pytest.raises(SourceError) as e:
        reader(ctl).collect()                                                     # also not through another object
    assert e.value.key == "source.family.paused" and ctl.exchanges == 1


def test_exchanges_are_capped_per_hour(ctl):
    r = reader(ctl)
    for _ in range(read.LOGINS_PER_HOUR):
        r._forget_token()
        r.collect(light=True)
    r._forget_token()
    with pytest.raises(SourceError) as e:
        r.collect(light=True)
    assert e.value.key == "source.family.paused" and ctl.exchanges == read.LOGINS_PER_HOUR


def test_an_expired_token_is_renewed_once_and_the_poll_goes_on(ctl):
    r = reader(ctl)
    r.collect(light=True)
    ctl.expire_next = True
    raw = r.collect(light=True)
    assert len(raw["devices"]) == 5 and ctl.exchanges == 2


def test_a_token_the_controller_keeps_refusing_ends_in_an_account_problem(ctl):
    r = reader(ctl)
    r.collect(light=True)
    ctl.tokens.clear()
    real = ctl._token

    def stubborn(h, parts, raw):                                  # the exchange succeeds but the new token is refused too
        real(h, parts, raw)
        ctl.tokens.clear()

    ctl._token = stubborn
    with pytest.raises(SourceError) as e:
        r.collect(light=True)
    assert e.value.key == "source.family.auth_failed"


def test_http_errors_and_unreachable(ctl):
    ctl.mode = "http"
    with pytest.raises(SourceError) as e:
        reader(ctl).collect()
    assert e.value.key == "source.family.protocol"
    ctl.stop()
    with pytest.raises(SourceError) as e:
        reader(ctl, timeout=1.0).collect()
    assert e.value.key == "source.family.unreachable"


# ---------------------------------------------------------------------------- roles and releases
def test_an_area_the_role_may_not_read_is_left_out_the_rest_shows(ctl):
    ctl.denied = {"acls/", "lan-profiles"}
    r = reader(ctl)
    raw = r.collect()
    assert "acl_gateway" not in raw and "profiles" not in raw and len(raw["devices"]) == 5
    assert set(r.last_denied) == {"acl_eap", "acl_gateway", "acl_switch", "profiles"}
    ev = r.config_event(raw)
    assert "acl" not in ev["capabilities"]["read"] and "port_profiles" not in ev["capabilities"]["read"]
    assert ev["state"]["acl_digest"] == [] and len(ev["state"]["equipment"]) == 5


def test_a_role_that_may_not_read_devices_is_an_access_problem(ctl):
    ctl.denied = {"/devices"}
    with pytest.raises(SourceError) as e:
        reader(ctl).collect()
    assert e.value.key == "source.family.forbidden"


def test_areas_an_older_release_does_not_have_are_marked_missing(ctl):
    ctl.missing = {"acls/eap-acls", "wan-status", "lan-profiles"}
    r = reader(ctl)
    raw = r.collect()
    ev = r.config_event(raw)
    assert "wans" not in raw and "profiles" not in raw and "acl_eap" not in raw and "acl_gateway" in raw
    assert "wans" not in ev["capabilities"]["read"] and "port_profiles" not in ev["capabilities"]["read"]
    assert "acl" in ev["capabilities"]["read"]                                # the other two rule lists still answer


def test_account_scope_is_unknown(ctl):
    assert asyncio.run(reader(ctl).account_scope()) == "unknown"
