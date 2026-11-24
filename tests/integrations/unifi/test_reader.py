"""Read half of the UniFi Network family against a local fake controller (both APIs, console and self-hosted
layout). The fake records every request; the tests check what was sent, not only what came back."""
import asyncio
import json
import re
import time
import types

import pytest

from lanloom.integrations.credentials import Secret
from lanloom.integrations.transport import Forbidden
from lanloom.integrations.unifi import model, read, session
from lanloom.sources import SourceError
from lanloom.sources._common import error_key

from . import fixtures as fx
from .fake_controller import KEY, PW, USER, FakeController

LOGIN_PATHS = ("/api/auth/login", "/api/login")


@pytest.fixture(autouse=True)
def fresh_ledger():
    session._reset_for_tests()
    yield
    session._reset_for_tests()


@pytest.fixture()
def make():
    made = []

    def factory(style="console", classic=True, official=True, data=None):
        f = FakeController(style, classic, official, data)
        made.append(f)
        return f

    yield factory
    for f in made:
        f.stop()


def reader(fake, key=KEY, user=USER, password=PW, site="default", **kw):
    return read.UnifiReader(url=fake.url, site=site, username=user, password=password, api_key=key, timeout=3.0, **kw)


def run(coro):
    return asyncio.run(coro)


def key_of(call):
    with pytest.raises(SourceError) as exc:
        call()
    return exc.value.key


# ---------------------------------------------------------------------------- configuration
@pytest.mark.parametrize("kw", [
    {"url": "ftp://192.0.2.1"}, {"url": ""}, {"url": "https://u:p@192.0.2.1"},  # secret-scan: fake
    {"url": "https://192.0.2.1/x"}, {"url": "https://192.0.2.1", "api_key": None, "username": None},
    {"url": "https://192.0.2.1", "api_key": "k", "site": "bad site"}, {"url": "https://192.0.2.1", "username": "u"},
    {"url": "https://192.0.2.1", "api_key": "k", "username": "u\x00x", "password": "p"},
])
def test_bad_settings_are_refused(kw):
    base = {"url": "https://192.0.2.1", "api_key": "k"}
    base.update(kw)
    with pytest.raises(SourceError) as exc:
        read.UnifiReader(**base)
    assert exc.value.key == "source.family.config"


def test_key_only_account_only_and_both_are_accepted():
    for kw in ({"api_key": "k"}, {"username": "u", "password": "p"}, {"api_key": "k", "username": "u", "password": "p"}):
        r = read.UnifiReader(url="https://192.0.2.1:8443/", **kw)
        assert r.family == "unifi" and r.name == "family" and r.interval >= 5.0


# ---------------------------------------------------------------------------- what is read, console layout
def test_both_apis_give_the_same_state_as_the_fixture_and_official_is_preferred(make):
    fake = make("console")
    r = reader(fake)
    raw = r.collect(False)
    assert set(raw) >= {"info", "o_networks", "o_zones", "o_policies", "o_wifi", "o_devices", "o_clients", "o_wans", "o_vpn",
                        "sysinfo", "networkconf", "zone", "policies", "device", "sta", "user", "portconf", "portforward"}
    assert model.to_neutral(raw) == model.to_neutral(fx.both())
    assert r.last_raw_keys == tuple(sorted(raw))
    paths = fake.paths()
    assert "/proxy/network/integration/v1/info" in paths
    assert "/proxy/network/integration/v1/sites/%s/firewall/policies" % fx.SITE_ID in paths
    assert "/proxy/network/api/s/default/rest/portforward" in paths and "/proxy/network/v2/api/site/default/firewall-policies" in paths
    for oid in (fx.O_NET_LAN, fx.O_NET_GUEST):                      # the overview has no details: each network is read once
        assert paths.count("/proxy/network/integration/v1/sites/%s/networks/%s" % (fx.SITE_ID, oid)) == 1
    assert paths.count("/proxy/network/integration/v1/sites/%s/wifi/broadcasts/%s" % (fx.SITE_ID, fx.O_WIFI)) == 1
    assert {p.rsplit("/", 1)[-1] for p in paths if "/devices/" in p} == {fx.O_DEV_GW, fx.O_DEV_SW, fx.O_DEV_AP}


def test_light_poll_reads_only_clients_and_devices(make):
    fake = make("console")
    raw = reader(fake).collect(True)
    assert set(raw) == {"info", "o_clients", "o_devices", "device", "sta"}
    assert all(not re.search(r"networks|firewall|portforward|wlanconf|rest/user", p) for p in fake.paths())


def test_classic_only_and_key_only(make):
    fake = make("console")
    classic = reader(fake, key=None).collect(False)
    assert not any(k.startswith("o_") or k == "info" for k in classic)
    assert model.to_neutral(classic) == model.to_neutral(fx.classic())
    assert not any("integration" in p for p in fake.paths())
    fake2 = make("console")
    official = reader(fake2, user=None, password=None).collect(False)
    assert not any(k in official for k in ("device", "sta", "networkconf"))
    assert model.to_neutral(official) == model.to_neutral(fx.official())
    assert fake2.logins == 0 and fake2.methods() == {"GET"}


def test_self_hosted_layout_has_no_prefix_and_its_own_sign_in(make):
    fake = make("classic")
    r = reader(fake)
    raw = r.collect(False)
    assert model.to_neutral(raw) == model.to_neutral(fx.both())
    posts = [p for m, p, *_ in fake.requests if m == "POST"]
    assert posts == ["/api/auth/login", "/api/login"]               # console style tried first, then the self-hosted one
    assert [p for p in fake.paths() if p.startswith("/proxy/network")] == ["/proxy/network/integration/v1/info"]      # the probe
    assert "/integration/v1/info" in fake.paths() and "/api/s/default/rest/user" in fake.paths()
    again = fake.logins
    r.collect(True)
    assert fake.logins == again                                     # the session (and its style) is reused
    assert fake.count("POST", r"login") == 2


# ---------------------------------------------------------------------------- paging, details, site
def _clients(n):
    return [{"type": "WIRELESS", "id": fx.uid("9101", i), "name": "Client %d" % i, "ipAddress": "192.168.50.%d" % (i % 250 + 1),
             "macAddress": "02:4c:4c:02:%02x:%02x" % (i // 256, i % 256), "uplinkDeviceId": fx.O_DEV_AP, "access": {"type": "DEFAULT"}}
            for i in range(n)]


def test_official_lists_are_paged_with_the_limit_of_200(make):
    data = fx.official()
    data["o_clients"] = _clients(450)
    fake = make("console", classic=False, data=data)
    raw = reader(fake, user=None, password=None).collect(True)
    assert len(raw["o_clients"]) == 450 and len({c["id"] for c in raw["o_clients"]}) == 450
    paged = [(o, lim) for name, o, lim in fake.page_log if name == "/clients"]
    assert paged == [(0, 200), (200, 200), (400, 200)]
    assert all(lim == 200 for _, _, lim in fake.page_log)
    ev = model.topology_event(raw, 1.0)
    assert len(ev["clients"]) == 450 and {c["parent_mac"] for c in ev["clients"]} == {fx.MAC_AP}


def test_paging_stops_at_the_total_count(make):
    data = fx.official()
    data["o_clients"] = _clients(400)
    fake = make("console", classic=False, data=data)
    assert len(reader(fake, user=None, password=None).collect(True)["o_clients"]) == 400
    assert [(o, lim) for name, o, lim in fake.page_log if name == "/clients"] == [(0, 200), (200, 200)]


def test_page_limit_and_detail_budget_are_bounded(make, monkeypatch):
    data = fx.official()
    data["o_clients"] = _clients(30)
    fake = make("console", classic=False, data=data)
    monkeypatch.setattr(read, "PAGE", 10)
    monkeypatch.setattr(read, "MAX_PAGES", 2)
    assert len(reader(fake, user=None, password=None).collect(True)["o_clients"]) == 20      # never an endless loop
    fake2 = make("console", classic=False)
    monkeypatch.setattr(read, "MAX_DETAILS", 2)
    raw = reader(fake2, user=None, password=None).collect(False)
    assert sum(1 for p in fake2.paths() if re.search(r"/(networks|devices|wifi/broadcasts)/[0-9a-f-]{36}$", p)) == 2
    assert len(raw["o_devices"]) == 3                                # the overview rows stay, only without details


def test_site_is_resolved_by_internal_reference(make):
    fake = make("console", classic=False)
    fake.sites = [{"id": fx.SITE_ID, "internalReference": "default"}, {"id": fx.SITE_OTHER, "internalReference": "branch"}]
    reader(fake, user=None, password=None, site="branch").collect(True)
    assert any("/sites/%s/clients" % fx.SITE_OTHER in p for p in fake.paths())
    assert not any("/sites/%s/" % fx.SITE_ID in p for p in fake.paths())
    fake3 = make("console", classic=False)
    fake3.sites = list(fake.sites)
    assert key_of(lambda: reader(fake3, user=None, password=None, site="nope").collect(True)) == "source.family.config"
    fake4 = make("console", classic=False)
    fake4.sites = [{"id": fx.SITE_OTHER, "internalReference": "main"}]               # a single site is taken whatever its name
    reader(fake4, user=None, password=None, site="default").collect(True)
    assert any("/sites/%s/clients" % fx.SITE_OTHER in p for p in fake4.paths())


def test_no_official_api_on_the_controller_means_classic_only(make):
    fake = make("console", official=False)
    r = reader(fake)
    raw = r.collect(False)
    assert not any(k.startswith("o_") for k in raw) and "networkconf" in raw
    n = len(fake.requests)
    r.collect(True)
    assert not any("integration" in req[1] for req in fake.requests[n:])               # the answer "not there" is remembered


# ---------------------------------------------------------------------------- what is ever sent
def test_only_get_and_sign_in_are_sent_and_every_path_is_on_the_allow_list(make):
    fake = make("console")
    r = reader(fake)
    run(r.poll(False))
    r._sign_out()
    assert fake.methods() == {"GET", "POST"}
    posts = [(p, body) for m, p, _, body in fake.requests if m == "POST"]
    assert posts[0] == ("/api/auth/login", {"username": USER, "password": PW})
    assert posts[-1][0] == "/api/auth/logout" and posts[-1][1] == {}
    assert {p for p, _ in posts[1:-1]} == set()
    for m, path, heads, body in fake.requests:
        if m == "GET":
            read.ALLOW.check("GET", path.split("?")[0])
    assert fake.logins == 1 and r._t.refused == 0


def test_anything_outside_the_allow_list_is_refused_before_a_byte_is_sent(make):
    fake = make("console")
    r = reader(fake)
    assert key_of(lambda: r._get("/proxy/network/api/s/default/rest/admin")) == "source.family.refused"
    assert key_of(lambda: r._get("/proxy/network/api/s/default/rest/user/../../self")) == "source.family.refused"
    assert key_of(lambda: r._get("/proxy/network/api/s/default/cmd/devmgr")) == "source.family.refused"
    for method, path, body in (("PUT", "/proxy/network/api/s/default/rest/user/1", {"name": "x"}),
                               ("DELETE", "/proxy/network/api/s/default/rest/user/1", None),
                               ("PATCH", "/proxy/network/integration/v1/sites/%s/networks/%s" % (fx.SITE_ID, fx.O_NET_LAN), {"a": 1}),
                               ("POST", "/proxy/network/api/s/default/cmd/devmgr", {"cmd": "restart"}),
                               ("POST", "/proxy/network/integration/v1/sites/%s/firewall/policies" % fx.SITE_ID, {"name": "x"}),
                               ("POST", "/api/auth/login", {"username": "u", "password": "p", "remember": True}),
                               ("POST", "/api/auth/login", {"username": "u"}),
                               ("POST", "/api/auth/logout", {"x": 1}),
                               ("GET", "/proxy/network/api/s/default/rest/user", {"x": 1})):
        with pytest.raises(Forbidden):
            r._t.request(method, path, body)
    assert fake.requests == [] and r._t.sent == 0 and r._t.refused >= 12


def test_allow_list_is_frozen_and_read_only():
    assert all(rule.method in ("GET", "POST") for rule in read.ALLOW.rules) and read.ALLOW.write is False
    assert [r.path.pattern for r in read.ALLOW.rules if r.method == "POST"] == [r"\A(?:/api/(?:auth/)?login)\Z", r"\A(?:/api/(?:auth/)?logout)\Z"]
    assert isinstance(read.ALLOW.rules, tuple)


# ---------------------------------------------------------------------------- session, cookies, csrf
def test_console_sign_in_keeps_name_and_value_of_the_cookie_and_sends_the_csrf_token(make):
    fake = make("console")
    reader(fake, key=None).collect(True)
    gets = [(p, h) for m, p, h, _ in fake.requests if m == "GET"]
    assert gets
    for _, heads in gets:
        assert heads["cookie"] == "TOKEN=sid1" and heads["x-csrf-token"] == fx_csrf()
        assert "x-api-key" not in heads


def fx_csrf():
    from .fake_controller import CSRF
    return CSRF


def test_self_hosted_sign_in_takes_the_csrf_token_from_its_cookie(make):
    fake = make("classic")
    reader(fake, key=None).collect(True)
    heads = [h for m, p, h, _ in fake.requests if m == "GET"]
    assert heads and all(h["cookie"] == "unifises=sid1; csrf_token=%s" % fx_csrf() and h["x-csrf-token"] == fx_csrf() for h in heads)


def test_api_key_is_sent_to_the_official_api_only_and_the_password_only_at_sign_in(make):
    fake = make("console")
    reader(fake).collect(False)
    for m, path, heads, body in fake.requests:
        if m == "GET":
            assert ("x-api-key" in heads) == ("integration" in path)
            assert (heads.get("x-api-key") == KEY) if "integration" in path else ("cookie" in heads)
        else:
            assert body["password"] == PW
        assert PW not in path and KEY not in path


def test_session_of_two_readers_is_shared_and_one_sign_in_suffices(make):
    fake = make("console")
    a, b = reader(fake, key=None), reader(fake, key=None)
    a.collect(True)
    b.collect(True)
    assert fake.logins == 1


def test_one_sign_in_again_after_401_per_poll(make):
    fake = make("console")
    r = reader(fake, key=None)
    r.collect(True)
    assert fake.logins == 1
    fake.expire_next = 1
    assert "sta" in r.collect(True) and fake.logins == 2            # one 401, one new sign-in, the poll goes on
    fake.expire_next = 1
    assert "sta" in r.collect(True) and fake.logins == 3            # the allowance is per poll
    fake.expire_next = 2                                            # the retry is refused as well: no second try
    assert key_of(lambda: r.collect(True)) == "source.family.auth_failed"
    assert fake.logins == 4


def test_no_sign_in_again_after_403(make):
    fake = make("console")
    r = reader(fake, key=None)
    r.collect(True)
    fake.fail[r"rest/user"] = 403
    assert key_of(lambda: r.collect(False)) == "source.family.forbidden"
    assert fake.logins == 1 and fake.count("POST", r"login") == 1


def test_refused_sign_in_pauses_every_further_attempt(make, monkeypatch):
    fake = make("console")
    r = reader(fake, key=None, password="wrong-password")          # secret-scan: fake
    assert key_of(lambda: r.collect(True)) == "source.family.auth_failed"
    posts = fake.count("POST", r"login")
    assert posts == 2                                               # console style, then the self-hosted one
    assert key_of(lambda: r.collect(True)) == "source.family.paused"
    other = reader(fake, key=None, password="wrong-password")       # secret-scan: fake
    assert key_of(lambda: other.collect(True)) == "source.family.paused"      # the ledger is process wide
    assert fake.count("POST", r"login") == posts
    good = reader(fake, key=None)                                   # another password is another key
    assert "sta" in good.collect(True)
    real = time.time()                                              # the pause ends after 15 minutes
    monkeypatch.setattr(session, "time", types.SimpleNamespace(time=lambda: real + session.PAUSE_AFTER_REFUSAL_S + 1))
    assert key_of(lambda: r.collect(True)) == "source.family.auth_failed"
    assert fake.count("POST", r"login") == posts + 1 + 2          # the good reader, then two more attempts of the wrong one


def test_sign_ins_are_capped_per_hour(make):
    fake = make("console")
    fake.always_401 = True                                          # a session that never holds would sign in forever
    r = reader(fake, key=None)
    keys = [key_of(lambda: r.collect(True)) for _ in range(4)]
    assert keys == ["source.family.auth_failed"] * 3 + ["source.family.paused"]
    assert fake.logins == session.LOGINS_PER_HOUR == 6
    assert key_of(lambda: r.collect(True)) == "source.family.paused" and fake.logins == 6


def test_session_cache_helpers():
    s = session.shared("192.0.2.1", "u", "p")
    assert session.shared("192.0.2.1", "u", "p") is s and session.shared("192.0.2.1", "u", "q") is not s
    s.absorb([("Set-Cookie", "TOKEN=abc; Path=/; Partitioned"), ("Set-Cookie", "bad cookie=1"), ("Set-Cookie", "x=" + "y" * 5000),
              ("X-Updated-CSRF-Token", "tok1")])
    assert s.headers() == {"Cookie": "TOKEN=abc", "X-CSRF-Token": "tok1"} and repr(s) == "Session(signed_in=True)"
    s.absorb([("Set-Cookie", "TOKEN=; Max-Age=0")])
    assert "Cookie" not in s.headers()
    for i in range(session.MAX_COOKIES + 5):
        s.absorb([("Set-Cookie", "c%d=v" % i)])
    assert len(s._cookies) == session.MAX_COOKIES
    s.forget()
    assert s.headers() == {} and not s.signed_in


# ---------------------------------------------------------------------------- failures of the other API
def test_api_key_refused(make):
    fake = make("console")
    r = reader(fake, key="wrong-key", user=None, password=None)   # secret-scan: fake
    assert key_of(lambda: r.collect(False)) == "source.family.key_refused"
    ok, key, details = run(r.test())
    assert (ok, key, details) == (False, "source.family.key_refused", {})
    assert not any(req[0] == "POST" for req in fake.requests)


def test_classic_areas_the_release_does_not_have_are_skipped(make):
    fake = make("console", official=False)
    for area in ("portforward", "firewallgroup", "firewallrule", "zone", "policies"):
        fake.classic.pop(area)
    r = reader(fake)
    raw = r.collect(False)
    assert not any(k in raw for k in ("portforward", "firewallgroup", "firewallrule", "zone", "policies"))
    assert {"networkconf", "device", "sta", "user", "wlanconf", "portconf", "sysinfo"} <= set(raw)
    assert "portforward" not in r.last_raw_keys
    st = model.to_neutral(raw)
    assert st["policies"] == [] and st["zones"] == [] and st["port_forwards"] == []
    fake.fail[r"rest/wlanconf"] = 404
    assert "wlanconf" not in r.collect(False)


def test_unexpected_answers_become_reasons_not_crashes(make):
    fake = make("console", official=False)
    r = reader(fake, key=None)
    r.collect(True)
    fake.fail[r"stat/device"] = 500
    assert key_of(lambda: r.collect(True)) == "source.family.protocol"
    fake.fail.clear()
    fake.classic["device"] = {"meta": {"rc": "error", "msg": "api.err.Invalid"}}
    assert "device" not in r.collect(True)
    fake.stop()
    assert key_of(lambda: r.collect(True)) == "source.family.unreachable"
    assert error_key("family", SourceError("source.family.unreachable")) == "source.family.unreachable"


# ---------------------------------------------------------------------------- capabilities, scope, secrets, test()
def test_capabilities_are_refined_from_what_was_read(make):
    fake = make("console")
    both = reader(fake)
    ev = [e for e in run(both.poll(False)) if e["type"] == "config"][0]
    caps = ev["capabilities"]
    assert caps["api"] == {"kind": "official-local", "version": "11.0.1"} and caps["write"] == {}
    assert {"ports", "port_profiles", "port_forwards", "dhcp_reservations", "acl", "zones", "policies"} <= set(caps["read"])
    assert both.capabilities() is caps
    assert ev["type"] == "config" and ev["source"] == "family" and ev["family"] == "unifi" and ev["state"]["v"] == 2

    fake2 = make("console", official=False)
    only_acct = reader(fake2, key=None)
    caps = [e for e in run(only_acct.poll(False)) if e["type"] == "config"][0]["capabilities"]
    assert caps["api"] == {"kind": "classic-local", "version": "10.2.0"} and "acl" not in caps["read"]
    assert {"ports", "port_profiles", "port_forwards", "dhcp_reservations"} <= set(caps["read"])

    fake3 = make("console", classic=False)
    only_key = reader(fake3, user=None, password=None)
    caps = [e for e in run(only_key.poll(False)) if e["type"] == "config"][0]["capabilities"]
    assert caps["api"]["kind"] == "official-local" and caps["api"]["version"] == "11.0.1"
    assert not {"port_profiles", "port_forwards", "dhcp_reservations"} & set(caps["read"]) and "acl" in caps["read"]
    assert "ports" in caps["read"] and "zones" in caps["read"]


def test_capabilities_drop_what_is_absent():
    r = read.UnifiReader(url="https://192.0.2.1", api_key="k")
    st = model.to_neutral(fx.only(fx.official(), "info", "o_devices"))
    assert "zones" not in r._refine({"info": {}, "o_devices": []}, st)["read"] and "policies" not in r.capabilities()["read"]
    st = model.to_neutral({"o_networks": []})
    caps = r._refine({"o_networks": []}, st)
    assert "ports" not in caps["read"] and caps["api"]["version"] == ""


def test_poll_events(make):
    fake = make("console")
    r = reader(fake)
    light = run(r.poll(True))
    assert [e["type"] for e in light] == ["topology"]
    full = run(r.poll(False))
    assert [e["type"] for e in full] == ["topology", "config"]
    assert full[0]["source"] == "controller" and len(full[0]["clients"]) == 5


@pytest.mark.parametrize("reply, scope", [
    ({"site_role": "readonly"}, "read_only"), ({"site_role": "admin"}, "can_write"), ({"is_super": True, "site_role": "readonly"}, "can_write"),
    ({"is_owner": True}, "can_write"), ({"site_role": "other"}, "unknown"), ({}, "unknown"),
    ({"is_owner": True, "site_role": "readonly"}, "read_only"),     # a self-hosted controller reports both (#21): the site role decides
])
def test_account_scope_of_a_view_only_account(make, reply, scope):
    fake = make("console")
    fake.self_reply = fx.classic_wrap([dict({"name": "viewer"}, **reply)])
    assert run(reader(fake, key=None).account_scope()) == scope


def test_account_scope_without_an_account_or_without_an_answer(make):
    fake = make("console")
    assert run(reader(fake, user=None, password=None).account_scope()) == "unknown" and fake.requests == []
    fake.fail[r"/self$"] = 404
    assert run(reader(fake, key=None).account_scope()) == "unknown"
    fake.fail.clear()
    fake.expire_next = 1                                            # the account check signs in again once as well
    assert run(reader(fake, key=None).account_scope()) == "read_only" and fake.logins == 2


def test_secrets_never_show_up_in_repr_str_or_errors(make):
    fake = make("console")
    r = reader(fake)
    texts = [repr(r), str(r), "%s" % r, "{}".format(r), repr(vars(r)), repr(r._session), repr(r._t), repr(r._password), repr(r._key),
             str(r._password), "%s" % r._key, "{}".format(r._key)]
    for t in texts:
        assert PW not in t and KEY not in t
    assert repr(r) == "UnifiReader(127.0.0.1:%d, site='default')" % fake.port
    assert isinstance(r._password, Secret) and isinstance(r._key, Secret) and r._password.reveal() == PW
    with pytest.raises(TypeError):
        json.dumps({"k": r._key})
    bad = reader(fake, key="wrong-key", user=None, password=None)   # secret-scan: fake
    with pytest.raises(SourceError) as exc:
        bad.collect(True)
    assert "wrong-key" not in str(exc.value) and "wrong-key" not in repr(exc.value)
    wrong = reader(fake, key=None, password="wrong-password")       # secret-scan: fake
    with pytest.raises(SourceError) as exc:
        wrong.collect(True)
    assert "wrong-password" not in str(exc.value) + repr(wrong) + repr(vars(wrong))
    secret_in = Secret(PW)
    assert read.UnifiReader(url=fake.url, username=USER, password=secret_in)._password is secret_in


def test_test_returns_ok_with_counts(make):
    fake = make("console")
    ok, key, details = run(reader(fake).test())
    expected = model.to_neutral(fx.both())
    assert ok is True and key == "source.family.ok"
    assert details == {"version": "11.0.1", "networks": len(expected["networks"]), "zones": 3, "policies": 6,
                       "devices": len(expected["equipment"]), "clients": len(expected["devices"])}
    assert PW not in json.dumps(details) and KEY not in json.dumps(details)


def test_test_reports_a_refused_account(make):
    fake = make("console")
    ok, key, details = run(reader(fake, key=None, password="wrong-password").test())     # secret-scan: fake
    assert (ok, key, details) == (False, "source.family.auth_failed", {})


# ---------------------------------------------------------------------------- the running loop
def test_run_emits_status_topology_and_config_and_signs_out(make, monkeypatch):
    import types
    from lanloom.integrations.unifi import read as readmod
    monkeypatch.setattr(readmod, "time", types.SimpleNamespace(monotonic=lambda: 5.0))   # freshly booted machine: monotonic near 0
    fake = make("console")
    r = reader(fake)
    events = []

    async def go():
        task = asyncio.create_task(r.run(events.append))
        for _ in range(100):
            await asyncio.sleep(0.1)
            if any(e["type"] == "config" for e in events):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    kinds = [e["type"] for e in events]
    assert "topology" in kinds and "config" in kinds
    assert [e for e in events if e["type"] == "status"][0]["ok"] is True
    assert fake.count("POST", r"/api/auth/logout") == 1             # the session is closed on the way out
    assert not fake.sessions
