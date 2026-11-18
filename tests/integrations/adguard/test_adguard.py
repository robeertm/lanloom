"""AdGuard Home read half: model, what is sent, what is refused, error reasons. Totals only - never single queries."""
import asyncio
import copy

import pytest

from lanloom import dnsfilter
from lanloom.integrations import capabilities, get
from lanloom.integrations.adguard import model, read
from lanloom.integrations.transport import Forbidden
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake import PASSWORD, USER, FakeAdguard

NOW = 1_700_000_000.0


@pytest.fixture()
def srv():
    s = FakeAdguard()
    yield s
    s.stop()


def reader(s, **kw):
    return read.AdguardReader(url=s.url, username=kw.pop("username", USER), password=kw.pop("password", PASSWORD), **kw)


# ---------------------------------------------------------------------------- the translation (pure)
def test_model_counts_hours_and_blocked_replacements():
    st = dnsfilter.clean(model.to_neutral(fx.DATA, NOW))
    assert st["family"] == "adguard" and st["running"] is True and st["blocking"] is True
    assert st["totals"]["queries"] == 2400 and st["totals"]["blocked"] == 615
    assert len(st["hours"]) == 24 and st["hours"][-1][0] == int(NOW // 3600)
    assert st["hours"][-1][2] == 26 and st["hours"][0][2] == 25
    assert st["top_clients"][0] == {"ip": "192.0.2.10", "name": "", "q": 1500, "b": None}
    assert st["top_clients"][1]["name"] == "phone" and st["top_clients"][1]["ip"] == ""
    assert st["upstreams"] == [{"name": "192.0.2.53:53", "n": 1500, "ms": 21.0}] and st["upstream_ok"] is True
    # the stand is the newest enabled list; the list address (with its token) and the user's own rules never travel
    assert st["lists_updated"] == 1_699_956_000
    assert [l["name"] for l in st["lists"]] == ["Main list", "Off list"]
    assert "SECRET" not in repr(st) and "secret-private-name" not in repr(st)


def test_model_day_counters_become_days():
    raw = copy.deepcopy(fx.DATA)
    raw["stats"].update(time_units="days", dns_queries=[10, 20, 30], blocked_filtering=[1, 2, 3])
    st = model.to_neutral(raw, NOW)
    assert st["hours"] == [] and [d[1:] for d in st["days"]] == [[10, 1], [20, 2], [30, 3]]
    assert st["window_s"] == 3 * 86400


# ---------------------------------------------------------------------------- what is sent
def test_poll_is_three_gets_with_basic_auth_and_no_post(srv):
    r = reader(srv)
    ev = asyncio.run(r.poll())[0]
    assert ev["type"] == "dns" and ev["family"] == "adguard" and ev["state"]["totals"]["blocked"] == 615
    assert srv.paths() == ["/control/status", "/control/stats", "/control/filtering/status"]
    assert {q["method"] for q in srv.requests} == {"GET"}
    assert all(q["headers"]["authorization"].startswith("Basic ") for q in srv.requests)
    asyncio.run(r.poll())
    assert srv.paths().count("/control/filtering/status") == 1
    assert PASSWORD not in repr(r) and PASSWORD not in repr(vars(r)) and PASSWORD not in str(ev)


def test_wrong_password_pauses(srv):
    r = reader(srv, password="wrong")
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed" and "wrong" not in repr(e.value)
    n = len(srv.requests)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.paused" and len(srv.requests) == n


@pytest.mark.parametrize("kw", [{"username": ""}, {"password": ""}, {"username": "a:b"}, {"username": "a\nb"}, {"url": ""},
                                {"url": "ftp://x"}, {"url": "http://u:p@x"}])
def test_bad_settings_are_refused(kw):
    args = {"url": "http://192.0.2.2", "username": USER, "password": PASSWORD}
    args.update(kw)
    with pytest.raises(SourceError) as e:
        read.AdguardReader(**args)
    assert e.value.key == "source.family.config"


# ---------------------------------------------------------------------------- allow-list and manifest
def test_allow_list_refuses_the_query_log_and_everything_that_changes_something():
    for method, path in [("GET", "/control/querylog"), ("GET", "/control/querylog?limit=100"), ("POST", "/control/querylog_clear"),
                         ("POST", "/control/protection"), ("POST", "/control/filtering/add_url"), ("GET", "/control/clients"),
                         ("POST", "/control/stats_reset"), ("PUT", "/control/stats")]:
        with pytest.raises(Forbidden):
            read.ALLOW.check(method, path, {"x": 1} if method == "POST" else None)


def test_manifest():
    m = get("adguard")
    assert m.validate() is m and m.read and not m.write and m.catalog == "dns_filter" and m.prefill_scheme == "http"
    caps = capabilities.validate(m.capabilities, "adguard")
    assert caps["read"] == ["dns"] and caps["write"] == {}
    assert m.detect and all(set(s) <= {"port", "title"} for s in m.detect)
