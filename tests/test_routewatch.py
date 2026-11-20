"""Network routes (DW2), the scheduler and what it stores (lanloom/routewatch.py) together with the data paths and the API.

The probe helper is always a fake (``RouteWatch(core, helper=fake)``), the clock and the load are fakes too: no probe packet
leaves this machine, no DNS lookup, no download. The addresses the helper "finds" are documentation or private ranges; the
peers booked into the data paths are ordinary addresses that are only looked up in the bundled country table."""
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import datapaths, demo_routes, geo, routes, routewatch
from lanloom.core import Core
from lanloom.main import create_app
from lanloom.routewatch import RouteWatch
from lanloom.store import Store

NOW = 1_790_000_000.0
DAY = 86400.0
HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
PEERS = {"US": "3.3.3.3", "DE": "5.9.1.1", "ZA": "41.0.0.1", "JP": "133.0.0.1", "GB": "81.2.69.142", "AC": "9.9.9.9"}
IPV4 = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
GEO = {"198.51.100.1": {"asn": 64496, "operator": "Example Broadband", "cc": "NL"},
       "198.51.100.50": {"asn": 64497, "operator": "Example Transit", "cc": "US"},
       "192.0.2.9": {"asn": 64498, "operator": "Example Cloud", "cc": "US"}}


def key_of(label):
    return routewatch.route_key(routewatch.ANYCAST if label == "AC" else label, 0, "")


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def public_hops(extra=()):
    return [{"ttl": 1, "ip": "192.168.1.1", "rtt": 0.4, "name": None},
            {"ttl": 2, "ip": "198.51.100.1", "rtt": 5.0, "name": "ae1.ams1.example.net"},
            {"ttl": 3, "ip": "198.51.100.50", "rtt": 30.0, "name": "core1.newyork.example.invalid"},
            {"ttl": 4, "ip": "192.0.2.9", "rtt": 31.0, "name": None, "reached": True}] + list(extra)


HOME_ONLY = [{"ttl": 1, "ip": "192.168.1.1", "rtt": 0.4}, {"ttl": 2, "ip": None, "rtt": None}, {"ttl": 3, "ip": None, "rtt": None}]


class FakeHelper:
    """Stands in for the helper process: records every request, answers from a script."""

    def __init__(self):
        self.calls = []
        self.caps = {"methods": {"udp": True, "tcp": True}, "reasons": {}}
        self.by_method = {}                 # method -> response dict or callable(request) -> dict
        self.on_measure = None              # callable(request), e.g. to move the clock

    def __call__(self, request, timeout):
        self.calls.append((dict(request), timeout))
        if request["mode"] == "check":
            return json.loads(json.dumps(self.caps))
        if self.on_measure:
            self.on_measure(request)
        r = self.by_method.get(request["method"])
        if callable(r):
            return r(request)
        if r is not None:
            return r
        return {"method": request["method"], "hops": public_hops(), "probes": 5, "error": None}

    def measures(self):
        return [c[0] for c in self.calls if c[0]["mode"] == "measure"]

    def checks(self):
        return [c[0] for c in self.calls if c[0]["mode"] == "check"]


@pytest.fixture(scope="module")
def real_geo():
    g = geo.Geo()
    g.start(background=False)
    assert g.db is not None, g.error
    return g


@pytest.fixture()
def env(tmp_path, real_geo, monkeypatch):
    core = Core(SimpleNamespace(data_dir=tmp_path, demo=False, probe=True), Store(":memory:"), None)
    core.geo.db = real_geo.db
    clock = Clock()
    helper = FakeHelper()
    load = {"v": 0.0}
    rw = RouteWatch(core, helper=helper, clock=clock, loadavg=lambda: (load["v"], 0.0, 0.0), cpus=4)
    rw._geo = lambda ip: dict(GEO.get(ip, {}))
    core.routes = rw
    core.paths.route_sink = rw
    monkeypatch.setattr(routewatch, "PAUSE_S", 0.0)
    return SimpleNamespace(core=core, rw=rw, helper=helper, clock=clock, load=load, store=core.store)


def switch(env, **patch):
    patch.setdefault("paths_routes", True)
    env.core.update_settings(patch)


def feed(env, amounts=None, now=None):
    amounts = amounts or {"DE": 5000, "JP": 4000, "AC": 2000, "US": 1000, "ZA": 300, "GB": 100}
    for label, n in amounts.items():
        env.core.paths.book("dev1", PEERS[label], n // 2, n - n // 2, NOW if now is None else now)


def all_tables_text(store):
    names = [r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")]
    out = {}
    for n in names:
        rows = store.query('SELECT * FROM "%s"' % n)
        out[n] = "\n".join(" | ".join(str(v) for v in dict(r).values()) for r in rows)
    return out


# ---------------------------------------------------------------------------------------------------------- option off
def test_option_off_means_no_packet_no_helper_no_memory(env, monkeypatch):
    import socket
    from lanloom import routeprobe

    def boom(*a, **k):
        raise AssertionError("something real happened with the option off")
    monkeypatch.setattr(routeprobe, "measure", boom)
    monkeypatch.setattr(routeprobe, "check", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "gethostbyaddr", boom)
    rw, core = env.rw, env.core
    assert core.settings()["paths_routes"] is False and rw.settings()["enabled"] is False
    now = time.time()
    feed(env, now=now)                                                 # the data paths book flows to six destinations
    env.core.paths.flush(now)
    assert env.core.paths.classify(PEERS["DE"]) == "DE" and rw.wants() is False
    assert rw.candidates == {} and rw.top(5) == [] and rw.due() == []
    assert rw.run_once() == {"ran": False, "reason": "off"}
    st = rw.status()
    assert st["enabled"] is False and st["possible"] is None and st["methods"] == {} and st["stored"] == 0
    assert rw.rows() == [] and core.paths_routes()["routes"] == []
    assert core.paths_routes()["status"]["enabled"] is False
    with pytest.raises(ValueError, match="paths.routes.off"):
        rw.measure_now()
    with pytest.raises(ValueError, match="paths.routes.off"):
        core.paths_routes_action("measure")
    assert env.helper.calls == [] and rw.caps is None                   # not even the check ran
    assert core.paths_country("DE", "24h")["routes"] == []
    assert env.helper.calls == []
    # the traffic itself is still counted by the data paths
    assert core.paths_country("DE", "24h")["totals"]["bytes"] == 5000


def test_switching_off_forgets_the_destinations(env):
    switch(env)
    feed(env)
    assert len(env.rw.candidates) == 6 and env.rw.wants() is True
    switch(env, paths_routes=False)
    assert env.rw.candidates == {} and env.rw.measured == {} and env.rw.wants() is False
    feed(env)
    assert env.rw.candidates == {}                                       # nothing is remembered again


def test_destinations_are_in_memory_only(env):
    switch(env)
    feed(env)
    env.core.paths.flush(NOW)
    assert env.rw.candidates and any("5.9.1.1" in c["ips"] for c in env.rw.candidates.values())
    for table, text in all_tables_text(env.store).items():
        for peer in PEERS.values():
            assert peer not in text, (table, peer)


def test_note_ignores_nothing_to_remember_and_keeps_few_addresses(env, monkeypatch):
    switch(env)
    rw = env.rw
    rw.note("", "DE", 0, "", 100, NOW)
    rw.note("5.9.1.1", "DE", 0, "", 0, NOW)
    rw.note("5.9.1.1", "~own", 0, "", 100, NOW)
    rw.note("5.9.1.1", "~unassigned", 0, "", 100, NOW)
    assert rw.candidates == {}
    for i in range(1, 8):                                                # seven addresses of one group: only the biggest four stay
        rw.note("5.9.1.%d" % i, "DE", 0, "", i * 100, NOW)
    (c,) = rw.candidates.values()
    assert set(c["ips"]) == {"5.9.1.4", "5.9.1.5", "5.9.1.6", "5.9.1.7"}
    assert rw.top(1, NOW)[0][1]["ip"] == "5.9.1.7"
    monkeypatch.setattr(routewatch, "MAX_CANDIDATES", 4)
    for i in range(20):
        rw.note("5.9.2.%d" % i, "X%s" % chr(65 + i), 0, "", 10 + i, NOW)
    assert len(rw.candidates) <= 4


# ---------------------------------------------------------------------------------------------------------- scheduling
def test_due_picks_the_top_groups_by_bytes_and_respects_the_interval(env):
    switch(env, paths_routes_targets=3)
    feed(env)
    rw, helper = env.rw, env.helper
    assert [k for k, _ in rw.due(NOW)] == [key_of("DE"), key_of("JP"), key_of("AC")]
    res = rw.run_once()
    assert res["ran"] is True and res["measured"] == 3
    assert [m["target"] for m in helper.measures()] == ["5.9.1.1", "133.0.0.1", "9.9.9.9"]
    assert all(m["method"] == "udp" and m["names"] is True and m["port"] == 443 and m["budget"] <= routewatch.TARGET_BUDGET_S
               for m in helper.measures())
    assert len(helper.checks()) == 1
    n = len(helper.calls)
    assert rw.run_once() == {"ran": False, "reason": "nothing_due"} and len(helper.calls) == n      # right after: nothing
    env.clock.t += 6 * 3600 - 1
    assert rw.run_once()["reason"] == "nothing_due"
    env.clock.t += 2
    assert rw.run_once()["measured"] == 3 and len(helper.measures()) == 6
    assert len(helper.checks()) == 2                      # what is possible is checked again after 6 hours, not before


def test_the_number_of_groups_and_the_interval_follow_the_settings(env):
    switch(env, paths_routes_targets=1, paths_routes_interval_h=1)
    feed(env)
    assert [k for k, _ in env.rw.due(NOW)] == [key_of("DE")]
    env.rw.run_once()
    env.clock.t += 3600
    assert len(env.rw.due()) == 1
    st = env.rw.settings()
    assert (st["targets"], st["interval_h"]) == (1, 1)


def test_the_biggest_address_of_a_group_is_the_one_measured(env):
    switch(env, paths_routes_targets=1)
    env.core.paths.book("dev1", "5.9.1.1", 50, 50, NOW)
    env.core.paths.book("dev1", "5.9.1.2", 2000, 3000, NOW)
    env.rw.run_once()
    assert [m["target"] for m in env.helper.measures()] == ["5.9.1.2"]


def test_force_and_measure_now_ignore_the_interval(env, monkeypatch):
    switch(env, paths_routes_targets=2)
    feed(env)
    rw, helper = env.rw, env.helper
    assert rw.run_once()["measured"] == 2 and rw.run_once()["reason"] == "nothing_due"
    rw.force = True
    assert rw.run_once()["measured"] == 2 and len(helper.measures()) == 4 and rw.force is False

    class Sync:
        def __init__(self, target=None, name=None, daemon=None, **k):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(routewatch.threading, "Thread", Sync)
    st = rw.measure_now()
    assert len(helper.measures()) == 6 and st["enabled"] is True and st["last_run"] == NOW


def test_a_second_run_never_overlaps(env):
    switch(env)
    feed(env)
    assert env.rw.run_lock.acquire(blocking=False)
    try:
        assert env.rw.run_once() == {"ran": False, "reason": "running"}
    finally:
        env.rw.run_lock.release()
    assert env.helper.calls == []


# ---------------------------------------------------------------------------------------------------------- methods
def test_udp_first_then_tcp(env):
    switch(env, paths_routes_targets=1)
    feed(env)
    assert env.rw.methods() == ["udp", "tcp"]
    env.rw.run_once()
    assert [m["method"] for m in env.helper.measures()] == ["udp"]
    assert env.store.one("SELECT method FROM paths_route")["method"] == "udp"
    assert "scamper" not in env.rw.status() and "scamper_reason" not in env.rw.status()


def test_fallback_udp_without_a_public_hop_tries_tcp(env):
    switch(env, paths_routes_targets=1)
    feed(env)
    env.helper.by_method["udp"] = {"method": "udp", "hops": HOME_ONLY, "probes": 3, "error": None}
    env.rw.run_once()
    assert [m["method"] for m in env.helper.measures()] == ["udp", "tcp"]
    assert env.store.one("SELECT method FROM paths_route")["method"] == "tcp"
    assert env.rw.state["probes"] == 3 + 5


def test_nothing_possible_here(env):
    switch(env)
    feed(env)
    env.helper.caps = {"methods": {"udp": False, "tcp": False},
                       "reasons": {"udp": "socket:EPERM", "tcp": "socket:EPERM"}}
    assert env.rw.run_once() == {"ran": False, "reason": "impossible"}
    assert env.helper.measures() == []
    st = env.rw.status()
    assert st["enabled"] is True and st["possible"] is False and st["reason"] == "socket:EPERM"
    assert st["methods"] == {"udp": False, "tcp": False}
    assert env.rw.rows() == [] and env.store.one("SELECT COUNT(*) AS n FROM paths_route")["n"] == 0
    # without any reason named, the status still says something
    env.helper.caps = {"methods": {}, "reasons": {}}
    env.rw.caps_t = 0.0
    env.rw.caps = None
    assert env.rw.status()["reason"] == "sockets"


def test_a_helper_that_fails_or_talks_nonsense_never_breaks_the_run(env):
    switch(env, paths_routes_targets=2)
    feed(env)
    env.helper.caps = {"methods": "nonsense", "reasons": ["x"]}
    assert env.rw.run_once() == {"ran": False, "reason": "impossible"}
    env.rw.caps = None
    env.helper.caps = {"methods": {"udp": True}, "reasons": {}}
    env.helper.by_method["udp"] = {"error": "timeout"}
    res = env.rw.run_once()
    assert res["ran"] is True and res["measured"] == 0 and env.rw.state["last_error"] == "timeout"
    assert env.rw.rows() == []
    env.clock.t += DAY
    env.helper.by_method["udp"] = {"error": None, "hops": "nonsense", "probes": "x"}
    assert env.rw.run_once()["measured"] == 0


def test_capabilities_are_checked_again_after_a_settings_change(env):
    switch(env)
    env.rw.status()
    assert len(env.helper.checks()) == 1
    env.rw.status()
    assert len(env.helper.checks()) == 1
    switch(env, paths_routes_targets=4)
    env.rw.status()
    assert len(env.helper.checks()) == 2


# ---------------------------------------------------------------------------------------------------------- load and budget
def test_load_and_scan_postpone_the_run(env):
    switch(env, paths_routes_targets=2)
    feed(env)
    env.load["v"] = 4.0                                                   # 4 cores: the limit is 3.4
    assert env.rw.run_once() == {"ran": False, "reason": "load"}
    assert env.rw.state["skipped"] == "load" and env.helper.calls == []
    assert env.rw.status()["skipped"] == "load"
    env.helper.calls.clear()                                              # (the status asked what is possible)
    env.load["v"] = routewatch.LOAD_LIMIT * 4                              # exactly at the limit still runs
    env.core.scan_state = {"state": "running"}
    assert env.rw.run_once() == {"ran": False, "reason": "scan"} and env.helper.calls == []
    env.core.scan_state = {"state": "idle"}
    res = env.rw.run_once()
    assert res["ran"] is True and env.rw.state["skipped"] is None


def test_the_run_stops_when_the_load_rises_in_the_middle(env):
    switch(env, paths_routes_targets=3)
    feed(env)
    values = iter([0.0, 9.0, 9.0, 9.0])
    env.rw.loadavg = lambda: (next(values, 9.0), 0.0, 0.0)
    res = env.rw.run_once()
    assert res["ran"] is True and res["measured"] == 1 and env.rw.state["skipped"] == "load"
    assert len(env.helper.measures()) == 1


def test_the_run_budget_stops_it_early(env):
    switch(env, paths_routes_targets=5)
    feed(env)
    env.helper.on_measure = lambda req: setattr(env.clock, "t", env.clock.t + 60.0)      # every measurement takes a minute
    res = env.rw.run_once()
    assert res["ran"] is True and res["measured"] == 3 and len(env.helper.measures()) == 3    # 150 s budget: 3 of 5
    assert env.rw.state["running"] is False
    assert all(m["budget"] <= routewatch.TARGET_BUDGET_S for m in env.helper.measures())
    # the ones that were left come first the next time (they were never measured)
    env.helper.on_measure = None
    env.clock.t += 1
    assert env.rw.run_once()["measured"] == 2


def test_stopping_ends_a_run_at_once(env):
    switch(env, paths_routes_targets=3)
    feed(env)
    env.rw.stop()
    assert env.rw.run_once()["ran"] is True and env.helper.measures() == []


# ---------------------------------------------------------------------------------------------------------- storage
def test_a_run_stores_no_address_and_no_router_name(env):
    switch(env, paths_routes_targets=3)
    feed(env)
    assert env.rw.run_once()["measured"] == 3
    tables = all_tables_text(env.store)
    assert tables["paths_route"] and tables["paths_route_day"]
    for name in ("paths_route", "paths_route_day"):
        text = tables[name]
        assert not IPV4.search(text), (name, IPV4.search(text) and IPV4.search(text).group())
        for bad in ("2001:db8", "example.net", "example.invalid", "example.com", "ae1", "core1", "newyork."):
            assert bad not in text, (name, bad)
    for name, text in tables.items():                                    # nowhere in the whole database: no router, no destination
        for peer in PEERS.values():
            assert peer not in text, (name, peer)
        for addr in ("198.51.100.1", "198.51.100.50", "192.0.2.9", "192.168.1.1"):
            assert addr not in text, (name, addr)
    r = env.rw.rows()[0]
    assert [s["kind"] for s in r["sections"]][0] == "home" and [s["kind"] for s in r["sections"]][-1] == "target"
    assert json.dumps(env.rw.rows()).count("192.0.2") == 0
    assert env.rw.status()["stored"] == 3


def test_clear_forgets_everything(env):
    switch(env)
    feed(env)
    env.rw.run_once()
    assert env.rw.status()["stored"] > 0 and env.rw.candidates
    env.rw.clear()
    assert env.rw.rows() == [] and env.rw.candidates == {} and env.rw.measured == {}
    assert env.store.one("SELECT COUNT(*) AS n FROM paths_route_day")["n"] == 0


def _sections(*asns, kind="transit"):
    out = [{"kind": "home", "asn": None, "operator": None, "cc": "NL", "place": None, "rtt": 1, "hops": 1}]
    for i, a in enumerate(asns):
        out.append({"kind": "provider" if i == 0 else kind, "asn": a, "operator": "Example %d" % a, "cc": "DE", "place": None, "rtt": 5 + i, "hops": 1})
    out.append({"kind": "target", "asn": None, "operator": None, "cc": "DE", "place": None, "rtt": 9, "hops": 0, "anycast": False,
                "reached": True, "city": None})
    return out


GROUP = {"cc": "DE", "asn": 0, "city": ""}


def test_retention_prunes_rows_older_than_30_days(env):
    switch(env)
    rw = env.rw
    rw.save("old", GROUP, "udp", _sections(64496), now=NOW - 40 * DAY)
    rw.save("edge", GROUP, "udp", _sections(64496), now=NOW - 29 * DAY)
    rw.save("new", GROUP, "udp", _sections(64496), now=NOW - 3600)
    assert rw.prune(NOW) >= 2
    keys = {r["key"] for r in env.store.query("SELECT key FROM paths_route")}
    assert keys == {"edge", "new"}
    days = {r["key"] for r in env.store.query("SELECT key FROM paths_route_day")}
    assert days == {"edge", "new"}
    assert rw.prune(NOW) == 0
    assert routewatch.KEEP_DAYS == 30


def test_a_run_prunes(env):
    switch(env, paths_routes_targets=1)
    feed(env)
    env.rw.save("ancient", GROUP, "udp", _sections(64496), now=NOW - 90 * DAY)
    env.rw.run_once()
    assert env.store.one("SELECT COUNT(*) AS n FROM paths_route WHERE key='ancient'")["n"] == 0


def test_route_changed_between_two_days(env):
    switch(env)
    rw = env.rw
    day1 = (int(NOW // DAY) + 1) * DAY + 3600
    day2 = day1 + DAY
    rw.save("k1", GROUP, "udp", _sections(64496, 64497, 64498), now=day1)
    rw.save("k2", GROUP, "udp", _sections(64496, 64497, 64498), now=day1)
    env.clock.t = day2
    res = rw.save("k1", GROUP, "udp", _sections(64496, 64499, 64498), now=day2)             # another network in the middle
    assert res["changed"] == day2
    rw.save("k2", GROUP, "udp", _sections(64496, 64497, 64498), now=day2)                  # the same order again
    rows = {r["key"]: r for r in rw.rows()}
    assert rows["k1"]["changed_recent"] is True and rows["k1"]["changed"] == day2
    assert rows["k2"]["changed"] is None and rows["k2"]["changed_recent"] is False
    assert [r["key"] for r in rw.changes_between(day2 - 10, day2 + 10)] == ["k1"]
    assert rw.changes_between(day2 + 10, day2 + 20) == []
    # measured once more the same day: the note stays
    rw.save("k1", GROUP, "udp", _sections(64496, 64499, 64498), now=day2 + 600)
    assert {r["key"]: r for r in rw.rows()}["k1"]["changed"] == day2
    # a day later, still the new order: calm again
    env.clock.t = day2 + DAY
    rw.save("k1", GROUP, "udp", _sections(64496, 64499, 64498), now=day2 + DAY)
    assert {r["key"]: r for r in rw.rows()}["k1"]["changed"] is None
    # the note is only "recent" for 36 hours
    rw.save("k3", GROUP, "udp", _sections(64496, 64497), now=day2 + DAY)
    rw.save("k3", GROUP, "udp", _sections(64496, 64500), now=day2 + 2 * DAY)
    env.clock.t = day2 + 2 * DAY + 37 * 3600
    assert {r["key"]: r for r in rw.rows()}["k3"]["changed_recent"] is False


def test_a_changed_chain_by_real_runs_is_noticed(env):
    switch(env, paths_routes_targets=1)
    feed(env)
    env.clock.t = (int(NOW // DAY) + 1) * DAY + 3600
    env.rw.run_once()
    assert env.rw.rows()[0]["changed"] is None
    env.clock.t += DAY
    other = public_hops()
    other[1] = dict(other[1], ip="198.51.100.77")
    env.rw._geo = lambda ip: dict({**GEO, "198.51.100.77": {"asn": 64509, "operator": "Example Other", "cc": "NL"}}.get(ip, {}))
    env.helper.by_method["udp"] = {"method": "udp", "hops": other, "probes": 4, "error": None}
    env.rw.run_once()
    row = env.rw.rows()[0]
    assert row["changed"] == env.clock.t and row["changed_recent"] is True


def test_anycast_destination_has_no_country_operator_or_city(env):
    switch(env, paths_routes_targets=6)
    feed(env)
    assert env.core.paths.classify("9.9.9.9") == datapaths.ANYCAST
    env.rw.run_once()
    ac = [r for r in env.rw.rows() if r["anycast"]]
    assert len(ac) == 1 and ac[0]["cc"] is None and ac[0]["asn"] is None and ac[0]["operator"] is None and ac[0]["city"] is None
    tgt = [s for s in ac[0]["sections"] if s["kind"] == "target"][0]
    assert tgt["anycast"] is True and tgt["cc"] is None and tgt["asn"] is None and tgt["operator"] is None and tgt["city"] is None
    assert tgt["place"] is None
    assert env.store.one("SELECT cc FROM paths_route WHERE key=?", (key_of("AC"),))["cc"] == routewatch.ANYCAST
    de = [r for r in env.rw.rows() if r["cc"] == "DE"][0]
    assert de["anycast"] is False and [s for s in de["sections"] if s["kind"] == "target"][0]["cc"] == "DE"
    assert env.rw.rows(cc="DE") == [de] and env.rw.rows(cc="FR") == []


def test_the_default_geo_lookup_gives_country_only_without_city_data(env):
    got = RouteWatch._geo(env.rw, "3.3.3.3")
    assert got["cc"] == "US" and "place" not in got
    assert RouteWatch._geo(env.rw, "198.51.100.1") == {"cc": None}


# ---------------------------------------------------------------------------------------------------------- the country sheet
def test_country_view_sum_rule_and_the_bubble_moves_to_the_measured_place(env):
    env.clock.t = time.time()
    now = env.clock.t
    switch(env, paths_routes_targets=6)
    env.core.paths.book("dev1", PEERS["US"], 2000, 3000, now)
    env.core.paths.flush(now)
    before = env.core.paths_country("US", "24h")
    assert before["totals"]["bytes"] == 5000 and sum(p["bytes"] for p in before["points"]) == 5000
    assert [(p["key"], p["approx"]) for p in before["points"]] == [(None, True)]       # the middle of the country
    assert before["routes_on"] is True and before["routes"] == []
    assert env.rw.run_once()["measured"] >= 1
    after = env.core.paths_country("US", "24h")
    assert after["totals"]["bytes"] == 5000 and sum(p["bytes"] for p in after["points"]) == after["totals"]["bytes"]
    (pt,) = after["points"]
    assert pt["via"] == "route" and pt["approx"] is True and pt["key"] == "route:0" and pt["name"] == "New York"
    assert abs(pt["lat"] - 40.64) < 0.1 and abs(pt["lon"] + 73.78) < 0.1
    assert len(after["routes"]) == 1 and after["routes"][0]["cc"] == "US"
    assert "192.0.2" not in json.dumps(after) and "example." not in json.dumps(after)


def test_country_view_moves_only_the_operator_without_a_city(env):
    now = time.time()
    p = env.core.paths
    p.book_bucket("dev1", "US", 1000, 2000, now)                     # traffic with an operator but no city
    p.book_detail_bucket("dev1", "US", "", 64500, 1000, 2000, now, None, "Example Transit")
    p.book_bucket("dev1", "US", 500, 500, now)                       # traffic nobody knows anything about
    p.flush(now)
    plain = p.country_view("US", "24h", {}, now)
    chicago = {"name": "Chicago", "lat": 41.88, "lon": -87.63}
    moved = p.country_view("US", "24h", {}, now, route_places={64500: chicago})
    assert plain["totals"]["bytes"] == moved["totals"]["bytes"] == 4000
    for v in (plain, moved):
        assert sum(x["bytes"] for x in v["points"]) == v["totals"]["bytes"]
    assert not any(x.get("via") for x in plain["points"])
    assert [(x["key"], x["bytes"]) for x in moved["points"] if x.get("via") == "route"] == [("route:64500", 3000)]
    assert [x["bytes"] for x in moved["points"] if x["key"] is None] == [1000]       # the rest stays in the middle of the country
    both = p.country_view("US", "24h", {}, now, route_places={64500: chicago, 0: {"name": "Denver", "lat": 39.74, "lon": -104.99}})
    assert sum(x["bytes"] for x in both["points"]) == 4000 and {x["name"] for x in both["points"] if x.get("via")} == {"Chicago", "Denver"}
    # unusable places are ignored and the sum still holds
    junk = p.country_view("US", "24h", {}, now, route_places={64500: {"name": "x"}, 7: "nonsense"})
    assert sum(x["bytes"] for x in junk["points"]) == 4000 and not any(x.get("via") for x in junk["points"])


def test_places_for_uses_only_sure_places_inside_the_country(env):
    rw = env.rw
    def row(key, cc, place):
        secs = _sections(64496)
        secs[1]["place"] = place
        rw.save(key, {"cc": cc, "asn": 0, "city": ""}, "udp", secs, now=NOW)
    row("a", "DE", {"name": "Frankfurt", "cc": "DE", "lat": 50.0, "lon": 8.5, "source": "name", "sure": True})
    row("b", "FR", {"name": "Frankfurt", "cc": "DE", "lat": 50.0, "lon": 8.5, "source": "name", "sure": True})       # not in its country
    row("c", "IT", {"name": "Milan", "cc": "IT", "lat": 45.4, "lon": 9.2, "source": "city", "sure": False})          # only a guess
    assert rw.places_for("DE") == {0: {"name": "Frankfurt", "lat": 50.0, "lon": 8.5}}
    assert rw.places_for("FR") == {} and rw.places_for("IT") == {} and rw.places_for("XX") == {}


# ---------------------------------------------------------------------------------------------------------- settings
def test_settings_validation_clamping_and_persistence(env):
    core = env.core
    for patch, err in (({"paths_routes_targets": 0}, "settings.routes.invalid_targets"), ({"paths_routes_targets": 21}, "settings.routes.invalid_targets"),
                       ({"paths_routes_interval_h": 0}, "settings.routes.invalid_interval"), ({"paths_routes_interval_h": 169}, "settings.routes.invalid_interval")):
        with pytest.raises(ValueError, match=err):
            core.update_settings(patch)
    s = core.settings()
    assert s["paths_routes"] is False and s["paths_routes_targets"] == 5 and s["paths_routes_interval_h"] == 6 and s["paths_routes_peeringdb"] is False
    core.update_settings({"paths_routes": True, "paths_routes_targets": 20, "paths_routes_interval_h": 168, "paths_routes_peeringdb": True})
    s = core.settings()
    assert (s["paths_routes"], s["paths_routes_targets"], s["paths_routes_interval_h"], s["paths_routes_peeringdb"]) == (True, 20, 168, True)
    again = RouteWatch(core, helper=env.helper)                             # a fresh object reads the same values
    assert again.settings() == {"enabled": True, "targets": 20, "interval_h": 168, "peeringdb": True}
    core.update_settings({"paths_routes_targets": "7", "paths_routes_interval_h": 2.5, "paths_routes": "yes"})   # wrong types are ignored
    assert core.settings()["paths_routes_targets"] == 20 and core.settings()["paths_routes"] is True
    # the bounds themselves are fine
    core.update_settings({"paths_routes_targets": 1, "paths_routes_interval_h": 1})
    assert env.rw.settings()["targets"] == 1 and env.rw.settings()["interval_h"] == 1
    # whatever is stored, the scheduler clamps it
    for value, expect in ((99, 20), (-3, 1), ("abc", 5), (None, 5)):
        core.store.set_setting("paths_routes_targets", value)
        assert env.rw.settings()["targets"] == expect


def test_a_remote_site_never_measures(env):
    env.rw.remote = True
    env.core.store.set_setting("paths_routes", True)
    assert env.rw.settings()["enabled"] is False and env.rw.run_once() == {"ran": False, "reason": "off"}
    assert env.rw.status()["reason"] == "remote" and env.helper.calls == []


# ---------------------------------------------------------------------------------------------------------- demo
def test_demo_seed_fills_routes_and_nothing_is_measured(env):
    demo_routes.seed(env.core, now=NOW)
    rw = env.rw
    assert rw.demo is True and env.core.settings()["paths_routes"] is True
    st = rw.status()
    assert st["demo"] is True and st["possible"] is True and st["stored"] == len(demo_routes.ROUTES) == 8
    rows = rw.rows(limit=40)
    assert len(rows) == 8 and any(r["anycast"] for r in rows) and any(r["changed_recent"] for r in rows) and any(r["uncertain"] for r in rows)
    text = json.dumps(rows)
    assert not IPV4.search(text) and "example." not in text
    demo_routes.seed(env.core, now=NOW)                                       # a second time changes nothing
    assert rw.status()["stored"] == 8
    assert rw.run_once() == {"ran": False, "reason": "off"}
    feed(env)
    assert rw.candidates == {} and rw.wants() is False
    assert rw.measure_now()["demo"] is True
    assert env.helper.calls == []
    assert env.core.paths_routes()["routes"] and env.core.paths_routes()["status"]["demo"] is True
    assert rw.places_for("US")                                                 # the sample routes also feed the country sheet


def test_demo_seed_without_a_watcher_is_harmless():
    demo_routes.seed(SimpleNamespace(), now=NOW)


# ---------------------------------------------------------------------------------------------------------- the API
@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    """One sample-network app for all API tests of this module (starting it takes seconds)."""
    tmp = tmp_path_factory.mktemp("routes-api")
    mp = pytest.MonkeyPatch()
    for k, v in (("LANLOOM_DATA", str(tmp)), ("LANLOOM_DEMO", "1"), ("LANLOOM_DEMO_SEED", "1"), ("LANLOOM_UPDATE_CHECK", "0")):
        mp.setenv(k, v)
    try:
        app = create_app(config_mod.load())
        with TestClient(app, base_url="http://lanloom.test") as c:
            code = (tmp / "setup-code.txt").read_text().strip()
            assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
            c.st = app.state.ll
            c.rw = c.st.core.routes
            c.st.accounts.create("vera", PW, role="viewer")
            c.st.accounts.create("olga", PW, role="operator")
            yield c
    finally:
        mp.undo()


@pytest.fixture()
def api(app_client):
    """Back to the sample network before every test (routes seeded, nothing measured)."""
    c = app_client
    c.cookies.clear()
    c.rw.clear()
    c.rw.demo = False
    c.st.core.update_settings({"paths_routes": False})
    demo_routes.seed(c.st.core)
    yield c
    c.cookies.clear()
    c.rw.clear()


def login(c, user):
    c.cookies.clear()
    r = c.post("/api/login", json={"username": user, "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text


def go_live(c):
    """Leave the sample data: the watcher is switched on, with a fake helper and a fake load; nothing real can happen."""
    helper = FakeHelper()
    c.rw.helper = helper
    c.rw.demo = False
    c.rw.loadavg = lambda: (0.0, 0.0, 0.0)
    c.rw.caps = None
    c.rw.clear()
    c.st.core.update_settings({"paths_routes": True})
    c.helper = helper
    return helper


def audit_actions(c):
    return [(e["action"], e.get("outcome")) for e in c.st.audit.query({}, 200)]


def test_api_demo_is_shown_in_the_sample_network(api):
    login(api, "vera")
    d = api.get("/api/paths/routes").json()
    assert d["status"]["demo"] is True and d["status"]["enabled"] is True and len(d["routes"]) == 8
    assert not IPV4.search(json.dumps(d))


def test_api_viewer_reads_but_cannot_measure_or_clear(api):
    helper = go_live(api)
    login(api, "vera")
    r = api.get("/api/paths/routes")
    assert r.status_code == 200 and r.json()["status"]["enabled"] is True and r.json()["routes"] == []
    assert api.post("/api/paths/routes/measure", headers=HDR).status_code == 403
    assert api.post("/api/paths/routes/clear", headers=HDR).status_code == 403
    assert api.put("/api/settings", json={"paths_routes": False}, headers=HDR).status_code == 403
    assert ("paths.routes.measure", "ok") not in audit_actions(api) and ("paths.routes.clear", "ok") not in audit_actions(api)
    api.cookies.clear()
    assert api.get("/api/paths/routes").status_code == 401
    assert api.post("/api/paths/routes/measure", headers=HDR).status_code == 401
    assert helper.measures() == []


def test_api_operator_measures_with_an_audit_entry_but_cannot_clear(api):
    helper = go_live(api)
    api.rw.save("k1", GROUP, "udp", _sections(64496), now=time.time())
    login(api, "olga")
    r = api.post("/api/paths/routes/measure", headers=HDR)
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert ("paths.routes.measure", "ok") in audit_actions(api)
    assert api.post("/api/paths/routes/clear", headers=HDR).status_code == 403
    assert ("paths.routes.clear", "ok") not in audit_actions(api)
    assert len(api.get("/api/paths/routes").json()["routes"]) == 1                # nothing was cleared
    # the action runs on a thread of its own; wait until it is over so nothing runs after the test
    for _ in range(100):
        if not api.rw.state["running"] and not api.rw.run_lock.locked():
            break
        time.sleep(0.05)
    assert helper.measures() == []                                                # no destination was known: nothing to measure


def test_api_measure_with_the_option_off_is_an_error_and_sends_nothing(api):
    helper = go_live(api)
    api.st.core.update_settings({"paths_routes": False})
    login(api, "olga")
    r = api.post("/api/paths/routes/measure", headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "paths.routes.off"
    assert helper.calls == []
    assert api.get("/api/paths/routes").json()["routes"] == []


def test_api_admin_clears_and_it_is_audited(api):
    go_live(api)
    api.rw.save("k1", GROUP, "udp", _sections(64496), now=time.time())
    login(api, "admin")
    assert len(api.get("/api/paths/routes").json()["routes"]) == 1
    r = api.post("/api/paths/routes/clear", headers=HDR)
    assert r.status_code == 200 and r.json()["stored"] == 0
    assert api.get("/api/paths/routes").json()["routes"] == []
    assert ("paths.routes.clear", "ok") in audit_actions(api)


def test_api_settings_validation_and_persistence(api):
    go_live(api)
    login(api, "admin")
    for patch, err in (({"paths_routes_targets": 0}, "settings.routes.invalid_targets"), ({"paths_routes_targets": 21}, "settings.routes.invalid_targets"),
                       ({"paths_routes_interval_h": 0}, "settings.routes.invalid_interval"), ({"paths_routes_interval_h": 169}, "settings.routes.invalid_interval")):
        r = api.put("/api/settings", json=patch, headers=HDR)
        assert r.status_code == 400 and r.json()["error"] == err, patch
    r = api.put("/api/settings", json={"paths_routes": True, "paths_routes_targets": 8, "paths_routes_interval_h": 12, "paths_routes_peeringdb": False}, headers=HDR)
    assert r.status_code == 200
    got = api.get("/api/settings").json()
    assert got["paths_routes"] is True and got["paths_routes_targets"] == 8 and got["paths_routes_interval_h"] == 12
    assert api.get("/api/paths/routes").json()["status"]["targets"] == 8
    assert api.get("/api/paths/routes").json()["status"]["interval_h"] == 12
    r = api.put("/api/settings", json={"paths_routes": False}, headers=HDR)
    assert r.status_code == 200 and api.get("/api/paths/routes").json()["status"]["enabled"] is False
    assert api.get("/api/paths/routes").json()["routes"] == []


# ---------------------------------------------------------------------------------------------------------- run_helper
def test_run_helper_gives_up_on_a_slow_probe_promptly(monkeypatch):
    from lanloom import routeprobe
    monkeypatch.setattr(routeprobe, "measure", lambda req: time.sleep(2.5) or {"error": None})
    monkeypatch.setattr(routewatch.os, "setpriority", lambda *a: None)
    t0 = time.monotonic()
    assert routewatch.run_helper({"mode": "measure", "target": "192.0.2.9"}, 0.3) == {"error": "timeout"}
    assert time.monotonic() - t0 < 1.3


def test_run_helper_answers_and_lowers_the_priority_of_the_worker_thread_only(monkeypatch):
    import threading
    from lanloom import routeprobe
    calls = []
    monkeypatch.setattr(routewatch.os, "setpriority", lambda *a: calls.append(a))
    monkeypatch.setattr(routeprobe, "measure", lambda req: {"method": "udp", "hops": [], "probes": 0, "error": None, "echo": req["target"]})
    monkeypatch.setattr(routeprobe, "check", lambda: {"methods": {"udp": True, "tcp": False}, "reasons": {}})
    res = routewatch.run_helper({"mode": "measure", "target": "192.0.2.9"}, 5)
    assert res["echo"] == "192.0.2.9" and res["error"] is None
    assert len(calls) == 1 and calls[0][0] == routewatch.os.PRIO_PROCESS and calls[0][2] == 19
    assert calls[0][1] != threading.get_native_id()
    assert routewatch.run_helper({"mode": "check"}, 5)["methods"] == {"udp": True, "tcp": False}


def test_run_helper_a_probe_that_raises_is_a_probe_error(monkeypatch):
    from lanloom import routeprobe

    def boom(req):
        raise RuntimeError("odd")
    monkeypatch.setattr(routeprobe, "measure", boom)
    monkeypatch.setattr(routewatch.os, "setpriority", lambda *a: None)
    assert routewatch.run_helper({"mode": "measure"}, 5) == {"error": "probe"}


def test_probe_and_watch_start_no_program():
    import re
    from pathlib import Path
    for name in ("routeprobe.py", "routewatch.py"):
        src = (Path(routewatch.__file__).parent / name).read_text()
        assert not re.search(r"^\s*(import|from)\s+(subprocess|multiprocessing|pty)\b", src, re.M), name
        for bad in ("os.system", "os.popen", "os.exec", "posix_spawn"):
            assert bad not in src, (name, bad)
