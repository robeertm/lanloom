"""Pi-hole read half: model, what is sent, what is refused, error reasons. Totals only - never single queries."""
import asyncio
import copy

import pytest

from lanloom import dnsfilter
from lanloom.integrations import capabilities, get
from lanloom.integrations.pihole import model, read
from lanloom.integrations.transport import Forbidden
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake import PASSWORD, FakePihole


@pytest.fixture()
def srv():
    s = FakePihole()
    yield s
    s.stop()


def reader(s, **kw):
    return read.PiholeReader(url=s.url, password=kw.pop("password", PASSWORD), **kw)


# ---------------------------------------------------------------------------- the translation (pure)
def test_model_sums_the_slots_per_hour_and_drops_pseudo_upstreams():
    st = dnsfilter.clean(model.to_neutral(fx.DATA))
    assert st["family"] == "pihole" and st["version"] == "v6.1" and st["blocking"] is True
    assert st["totals"] == {"queries": 1440, "blocked": 432, "clients": 4, "list_entries": 91234}
    assert sum(q for _h, q, _b in st["hours"]) == 24 * 6 * 10 and all(b == q * 3 // 10 for _h, q, b in st["hours"])
    assert [u["name"] for u in st["upstreams"]] == ["resolver.lan"] and st["upstreams"][0]["ms"] == 12.3
    assert st["upstream_ok"] is True
    assert st["top_clients"][0] == {"ip": "192.0.2.10", "name": "laptop.lan", "q": 900, "b": 300}
    assert st["top_clients"][1]["b"] == 0
    assert [(l["name"], l["entries"]) for l in st["lists"]] == [("lists.example.org/path/hosts.txt", 90000)]
    assert st["lists_updated"] == 1_699_900_000


def test_model_blocking_off_and_silent_upstream():
    raw = copy.deepcopy(fx.DATA)
    raw["blocking"] = {"blocking": "disabled"}
    raw["upstreams"]["upstreams"][2]["statistics"]["response"] = 0.0
    st = model.to_neutral(raw)
    assert st["blocking"] is False and st["upstream_ok"] is False
    raw["summary"]["queries"]["forwarded"] = 0
    assert model.to_neutral(raw)["upstream_ok"] is None


# ---------------------------------------------------------------------------- what is sent
def test_poll_signs_in_once_and_only_gets_totals(srv):
    r = reader(srv)
    ev = asyncio.run(r.poll())[0]
    assert ev["type"] == "dns" and ev["family"] == "pihole" and ev["state"]["totals"]["queries"] == 1440
    asyncio.run(r.poll())
    assert srv.logins == 1                                       # the session is reused
    assert [q["path"] for q in srv.requests if q["method"] == "POST"] == ["/api/auth"]
    assert not any(p.startswith("/api/queries") for p in srv.paths())
    assert PASSWORD not in repr(r) and PASSWORD not in repr(vars(r)) and PASSWORD not in str(ev)


def test_slow_part_is_not_asked_every_poll(srv):
    r = reader(srv)
    r.collect()
    r.collect()
    assert srv.paths().count("/api/lists") == 1 and srv.paths().count("/api/info/version") == 1
    assert srv.paths().count("/api/stats/summary") == 2


def test_session_that_ended_is_renewed_once(srv):
    r = reader(srv)
    r.collect()
    srv.mode = "expire"
    r.collect()
    assert srv.logins == 2


def test_wrong_password_pauses_instead_of_hammering(srv):
    r = reader(srv, password="wrong")
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed" and "wrong" not in repr(e.value) and "wrong" not in str(e.value)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.paused" and srv.logins == 1


def test_rate_limit_pauses(srv):
    srv.mode = "limit"
    with pytest.raises(SourceError) as e:
        reader(srv).collect()
    assert e.value.key == "source.family.paused"


def test_without_password_reads_an_open_api():
    s = FakePihole(password="")
    try:
        assert read.PiholeReader(url=s.url).collect()["summary"]["queries"]["total"] == 1440 and s.logins == 0
    finally:
        s.stop()


def test_old_version_is_a_protocol_error():
    s = FakePihole(data=dict(fx.DATA, summary={"unrelated": 1}))
    try:
        with pytest.raises(SourceError) as e:
            reader(s).collect()
        assert e.value.key == "source.family.protocol"
    finally:
        s.stop()


@pytest.mark.parametrize("url", ["", None, "ftp://x", "http://u:p@x", "http://a b"])
def test_bad_addresses_are_refused(url):
    with pytest.raises(SourceError) as e:
        read.PiholeReader(url=url, password="x")
    assert e.value.key == "source.family.config"


def test_test_reports_details_and_unreachable():
    s = FakePihole()
    ok, key, details = asyncio.run(reader(s).test())
    s.stop()
    assert ok and key == "source.family.ok" and details == {"version": "v6.1", "queries": 1440, "blocked": 432, "clients": 4}
    ok, key, _ = asyncio.run(read.PiholeReader(url="http://127.0.0.1:9", password="x", timeout=1).test())
    assert not ok and key == "source.family.unreachable"


# ---------------------------------------------------------------------------- allow-list and manifest
def test_allow_list_refuses_everything_that_reads_queries_or_changes_something():
    for method, path in [("GET", "/api/queries"), ("GET", "/api/queries?length=100"), ("GET", "/api/stats/query_types"),
                         ("POST", "/api/dns/blocking"), ("DELETE", "/api/auth"), ("PUT", "/api/lists"), ("PATCH", "/api/config"),
                         ("GET", "/api/config"), ("GET", "/api/stats/top_clients?count=50&x=1"), ("POST", "/api/auth/other"),
                         ("GET", "/api/stats/top_domains?blocked=false&count=10")]:
        with pytest.raises(Forbidden):
            read.ALLOW.check(method, path, {"password": "x"} if method == "POST" else None)


def test_login_body_must_be_exactly_the_password():
    for body in ({"password": "x", "totp": 1}, {}, None, {"user": "x"}):
        with pytest.raises(Forbidden):
            read.ALLOW.check("POST", "/api/auth", body)
    read.ALLOW.check("POST", "/api/auth", {"password": "x"})


def test_manifest():
    m = get("pihole")
    assert m.validate() is m and m.read and not m.write and m.catalog == "dns_filter" and m.prefill_scheme == "http"
    caps = capabilities.validate(m.capabilities, "pihole")
    assert caps["read"] == ["dns"] and caps["write"] == {}
    assert m.detect and all(set(s) <= {"port", "title"} for s in m.detect)
