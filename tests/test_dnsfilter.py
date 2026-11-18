"""DNS filter foundation (no product named here): the neutral reading, sums per hour and day, findings, views."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import core as core_mod
from lanloom import dnsfilter
from lanloom.store import Store

NOW = 1_700_000_000.0
DAY = 86400


def reading(**kw):
    base = {"v": 1, "family": "x", "t": NOW, "running": True, "blocking": True, "window_s": DAY,
            "totals": {"queries": 1000, "blocked": 300, "clients": 3, "list_entries": 5}, "hours": [], "days": [],
            "top_blocked": [], "top_clients": [], "upstreams": [], "lists": []}
    base.update(kw)
    return dnsfilter.clean(base)


@pytest.fixture()
def log():
    return dnsfilter.DnsLog(Store(":memory:"))


# ---------------------------------------------------------------------------- sums per hour and day
def test_hours_are_idempotent_and_never_shrink(log):
    h0 = int(NOW // 3600)
    st = reading(hours=[[h0 - 1, 100, 30], [h0, 40, 10]])
    log.record(1, st, NOW)
    log.record(1, st, NOW + 60)
    assert [r[1:] for r in log.hours(1, 2, NOW)] == [[100, 30], [40, 10]]
    log.record(1, reading(hours=[[h0, 25, 5]]), NOW + 120)            # the filter's counters restarted: keep the higher
    assert log.hours(1, 1, NOW)[0][1:] == [40, 10]
    log.record(1, reading(hours=[[h0, 55, 12]]), NOW + 180)
    assert log.hours(1, 1, NOW)[0][1:] == [55, 12]


def test_days_are_the_sum_of_their_hours_and_filter_days_are_kept(log):
    d0 = int(NOW // DAY)
    st = reading(hours=[[d0 * 24 + 1, 10, 2], [d0 * 24 + 2, 20, 5]], days=[[d0 - 1, 500, 100]])
    log.record(1, st, NOW)
    got = {r[0]: r[1:] for r in log.days(1, 3, NOW)}
    assert got[d0] == [30, 7] and got[d0 - 1] == [500, 100] and got[d0 - 2] == [0, 0]


def test_old_rows_are_dropped_and_forget_removes_everything(log):
    h0 = int(NOW // 3600)
    log.record(1, reading(hours=[[h0 - 36 * 24, 5, 1], [h0, 7, 1]]), NOW)
    assert log.store.one("SELECT COUNT(*) AS n FROM dns_sum")["n"] == 1
    log.record(2, reading(hours=[[h0, 7, 1]]), NOW)
    log.forget(1)
    assert not log.has_data(1) and log.has_data(2)
    assert log.store.one("SELECT COUNT(*) AS n FROM dns_day WHERE integ=1")["n"] == 0


def test_nothing_but_sums_is_ever_stored(log):
    """The privacy promise: the tables hold numbers per hour and day, no name, no address, no client."""
    h0 = int(NOW // 3600)
    st = reading(hours=[[h0, 10, 3]], top_blocked=[{"domain": "tracker.example", "n": 3}],
                 top_clients=[{"ip": "192.0.2.10", "name": "laptop", "q": 9, "b": 1}])
    log.record(1, st, NOW)
    for table in ("dns_sum", "dns_day"):
        cols = [r["name"] for r in log.store.query("PRAGMA table_info(%s)" % table)]
        assert cols in (["integ", "hour", "q", "b"], ["integ", "day", "q", "b"])
    blob = repr([dict(r) for r in log.store.query("SELECT * FROM dns_sum")] + [dict(r) for r in log.store.query("SELECT * FROM dns_day")])
    assert "tracker" not in blob and "192.0.2" not in blob and "laptop" not in blob


# ---------------------------------------------------------------------------- findings
def days_of(shares, q=1000, first=100):
    return [[first + i, q, int(q * s)] for i, s in enumerate(shares)]


def test_unusual_blocked_share():
    usual = days_of([0.28, 0.30, 0.27, 0.29, 0.31, 0.28])
    assert dnsfilter.share_shift(usual, 1000, 290) is None
    assert dnsfilter.share_shift(usual, 1000, 480) == {"dir": "high", "now": 48, "usual": 29}
    assert dnsfilter.share_shift(usual, 1000, 90)["dir"] == "low"
    assert dnsfilter.share_shift(usual, 100, 90) is None                       # too few queries to say anything
    assert dnsfilter.share_shift(days_of([0.28, 0.30]), 1000, 800) is None     # too few days
    assert dnsfilter.share_shift(usual, 1000, 400) is None                     # 40 % against 29 %: not 1.5 times


def test_silent_filter_needs_a_usual_traffic_and_complete_hours():
    busy = days_of([0.3] * 7, q=2400)
    hrs = lambda qs: [[i, q, 0] for i, q in enumerate(qs)]
    assert dnsfilter.silent(hrs([100, 100, 0, 0, 5, 0]), busy) is False        # one of the last three complete hours had a query
    assert dnsfilter.silent(hrs([100, 100, 0, 0, 0, 7]), busy) is True         # the current hour is still filling and does not count
    assert dnsfilter.silent(hrs([0, 0, 0, 0, 0, 0]), days_of([0.3] * 7, q=100)) is False      # a quiet filter is not silent
    assert dnsfilter.silent(hrs([0, 0]), busy) is False


def test_findings_list_and_order():
    st = reading(running=True, blocking=False, upstream_ok=False, lists_updated=int(NOW - 30 * DAY))
    keys = [f["key"] for f in dnsfilter.findings(st, [], [], NOW)]
    assert keys == ["dns.finding.protection_off", "dns.finding.upstream_down", "dns.finding.lists_old"]
    assert dnsfilter.findings(reading(running=False), [], [], NOW)[0]["level"] == "alert"
    assert dnsfilter.findings(reading(), [], [], NOW) == []


# ---------------------------------------------------------------------------- the reading is cleaned
def test_clean_keeps_blocked_below_queries_and_sorts_hours():
    st = reading(hours=[[5, 10, 99], [3, 4, 1]])
    assert st["hours"] == [[3, 4, 1], [5, 10, 10]]


def test_clean_refuses_what_is_not_a_reading():
    for bad in ({"v": 2}, {"v": 1, "hours": "x"}, {"v": 1, "totals": {"queries": True}}, {"v": 1, "hours": [[1, 2]]}, [], None):
        with pytest.raises(dnsfilter.ModelError):
            dnsfilter.clean(bad)


def test_clean_cuts_lists_and_names():
    st = dnsfilter.clean({"v": 1, "top_blocked": [{"domain": "A" * 400 + ".example", "n": 1}] * 30,
                          "top_clients": [{"ip": "192.0.2.%d" % (i % 250 + 1), "name": "n\x00\n", "q": 1, "b": None} for i in range(80)]})
    assert len(st["top_blocked"]) == dnsfilter.MAX_TOP and len(st["top_blocked"][0]["domain"]) <= 253
    assert len(st["top_clients"]) == dnsfilter.MAX_CLIENTS and st["top_clients"][0]["name"] == "n"
