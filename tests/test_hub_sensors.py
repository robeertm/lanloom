"""Sensor views of the hub bridge: thinning out (numbers, states), the live ring, the answer of the sensor page, and
the history request of the hub source against a fake hub on 127.0.0.1. Fixed clocks, no real sleeping."""
import asyncio
import datetime
import json

import pytest

from lanloom import hubsensors as hs
from lanloom.sources import hub as m
from lanloom.sources._common import SourceError
from test_sources_hub import TOKEN, FakeHub

NOW = 1_800_000_000.0
WALL = 1_800_000_000.0


# ---------------------------------------------------------------------- compress_numeric / compress_stats
def test_compress_numeric_buckets_min_mean_max():
    pts = [(0, 1), (10, 3), (30, "5"), (99, 9), (100, 7)]
    out = hs.compress_numeric(pts, 0, 100, 4)
    assert out == [[12.5, 1.0, 2.0, 3.0], [37.5, 5.0, 5.0, 5.0], [87.5, 7.0, 8.0, 9.0]]


def test_compress_numeric_drops_outside_window_and_non_numbers():
    pts = [(-1, 2), (101, 9), (10, "abc"), (11, None), (12, "nan"), (13, "inf"), (14, "unavailable"), (20, 4)]
    assert hs.compress_numeric(pts, 0, 100, 4) == [[12.5, 4.0, 4.0, 4.0]]
    assert hs.compress_numeric([], 0, 100) == [] and hs.compress_numeric([(1, 1)], 5, 5) == [] and hs.compress_numeric([(1, 1)], 9, 5) == []


def test_compress_numeric_never_exceeds_the_bucket_count():
    pts = [(i * 0.7, i % 13) for i in range(3000)]
    out = hs.compress_numeric(pts, 0, 2100, 50)
    assert 0 < len(out) <= 50
    assert all(p[1] <= p[2] <= p[3] for p in out)
    assert min(p[1] for p in out) == 0 and max(p[3] for p in out) == 12
    assert [p[0] for p in out] == sorted(p[0] for p in out)


def test_compress_stats_merges_hours_into_buckets():
    rows = [[-3600, 0, 1, 2], [-3601, -50, -50, -50], [0, 1, 2, 3], [3600, 5, 6, 7], [7200, 8, 9, 10], [7201, 100, 100, 100]]
    out = hs.compress_stats(rows, 0, 7200, 2)
    assert out == [[1800.0, 0, 1.5, 3], [5400.0, 5, 7.5, 10]]
    assert hs.compress_stats(rows, 10, 10) == []
    assert len(hs.compress_stats([[i * 3600, 0, 1, 2] for i in range(720)], 0, 720 * 3600)) <= hs.BUCKETS


# ---------------------------------------------------------------------- segments
def test_segments_cover_the_window_and_carry_the_state_before_in():
    changes = [(50, "a"), (120, "b"), (150, "b"), (180, "c")]
    assert hs.segments(changes, 100, 200) == [[100, 120, "a"], [120, 180, "b"], [180, 200, "c"]]


def test_segments_merge_equal_neighbours():
    changes = [(0, "on"), (10, "on"), (20, "off"), (30, "off"), (40, "on")]
    assert hs.segments(changes, 0, 50) == [[0, 20, "on"], [20, 40, "off"], [40, 50, "on"]]


def test_segments_edge_cases():
    assert hs.segments([], 0, 100) == []
    assert hs.segments([(-5, "x")], 0, 100) == [[0, 100, "x"]]                 # one old state fills the window
    assert hs.segments([(30, "x")], 0, 100) == [[30, 100, "x"]]                # nothing known before the first change
    assert hs.segments([(30, "x"), (500, "y")], 0, 100) == [[30, 100, "x"]]    # changes after the window are ignored
    assert hs.segments([(30, "x")], 10, 10) == []


def test_segments_beyond_the_limit_keep_the_longest_state_per_bucket():
    changes = []
    for k in range(10):                                  # 20 raw segments: "a" for 10 s and "b" for 90 s, ten times
        changes += [(k * 100, "a"), (k * 100 + 10, "b")]
    out = hs.segments(changes, 0, 1000, 4)
    assert out == [[0.0, 1000.0, "b"]]
    changes = []
    for k in range(10):
        changes += [(k * 100, "a"), (k * 100 + 90, "b")]
    assert hs.segments(changes, 0, 1000, 4) == [[0.0, 1000.0, "a"]]


def test_segments_reduction_stays_within_n_and_inside_the_window():
    changes = [(i, "s%d" % (i // 3)) for i in range(0, 900)]
    out = hs.segments(changes, 0, 900, 20)
    assert 0 < len(out) <= 20
    assert out[0][0] == 0 and out[-1][1] == 900
    assert all(a[1] <= b[0] for a, b in zip(out, out[1:]))


# ---------------------------------------------------------------------- summary
def test_summary_numeric():
    data = [[1, 1.0, 2.0, 3.0], [2, 0.5, 4.0, 9.0]]
    assert hs.summary("numeric", data, "7.5") == {"min": 0.5, "max": 9.0, "mean": 3.0, "last": 7.5}
    assert hs.summary("numeric", [], "x") == {"min": None, "max": None, "mean": None, "last": None}


def test_summary_state_shares():
    res = hs.summary("state", [[0, 30, "on"], [30, 40, "off"], [40, 100, "on"]], "on")
    assert res == {"share": {"on": 0.9, "off": 0.1}, "last": "on", "changes": 2}
    assert hs.summary("state", [], "x") == {"share": {}, "last": "x", "changes": 0}


# ---------------------------------------------------------------------- is_numeric
@pytest.mark.parametrize("entity, state, want", [
    ({"domain": "sensor", "unit": "W"}, "12.5", True),
    ({"domain": "sensor", "unit": "W"}, "unavailable", True),
    ({"domain": "sensor", "unit": "W"}, "abc", False),
    ({"domain": "sensor", "unit": None}, "3", True),
    ({"domain": "sensor", "unit": None}, "on", False),
    ({"domain": "number", "unit": None}, "4", True),
    ({"domain": "light", "unit": None}, "4", False),
    ({"domain": "sensor", "unit": "W"}, "nan", False),
    (None, "1", False),
])
def test_is_numeric(entity, state, want):
    assert hs.is_numeric(entity, state) is want


# ---------------------------------------------------------------------- ring
def test_ring_push_keeps_only_the_last_hour():
    e = {}
    hs.ring_push(e, "sensor.a", "1", NOW - 4000)
    hs.ring_push(e, "sensor.a", "2", NOW - 3000)
    hs.ring_push(e, "sensor.a", "3", NOW)
    assert e["ring"]["sensor.a"] == [(NOW - 3000, "2"), (NOW, "3")]
    assert hs.ring_of(e, "sensor.a", NOW) == [(NOW - 3000, "2"), (NOW, "3")]
    assert hs.ring_of(e, "sensor.a", NOW + 1000) == [(NOW, "3")]               # the reader cuts as well
    assert hs.ring_of(e, "sensor.none", NOW) == [] and hs.ring_of({}, "sensor.a", NOW) == []


def test_ring_push_is_bounded_per_entity():
    e = {}
    for i in range(hs.RING_MAX + 60):
        hs.ring_push(e, "sensor.a", str(i), NOW - 3000 + i)
    lst = e["ring"]["sensor.a"]
    assert len(lst) == hs.RING_MAX and lst[-1][1] == str(hs.RING_MAX + 59) and lst[0][1] == "60"


def test_ring_push_is_bounded_in_entities(monkeypatch):
    monkeypatch.setattr(hs, "RING_ENTITIES", 3)
    e = {}
    for i in range(5):
        hs.ring_push(e, "sensor.e%d" % i, "1", NOW)
    assert sorted(e["ring"]) == ["sensor.e0", "sensor.e1", "sensor.e2"]
    hs.ring_push(e, "sensor.e1", "2", NOW + 1)                                  # known entities keep recording
    assert len(e["ring"]["sensor.e1"]) == 2


# ---------------------------------------------------------------------- series
POWER = {"id": "sensor.power", "domain": "sensor", "unit": "W"}
LAMP = {"id": "light.lamp", "domain": "light", "unit": None}


def test_series_1h_numeric_from_the_ring():
    ring = [(NOW - 1000, "1"), (NOW - 500, "3"), (NOW - 100, "5")]
    out = hs.series(POWER, "1h", "5", ring, None, NOW)
    assert (out["kind"], out["source"], out["range"]) == ("numeric", "live", "1h")
    assert out["start"] == NOW - 3600 and out["end"] == NOW
    assert [p[2] for p in out["points"]] == [1.0, 3.0, 5.0]
    assert out["summary"] == {"min": 1.0, "max": 5.0, "mean": 3.0, "last": 5.0} and "segments" not in out


def test_series_1h_numeric_without_ring_shows_the_current_value():
    out = hs.series(POWER, "1h", "5", [], None, NOW)
    assert out["points"] == [[NOW, 5.0, 5.0, 5.0]]
    assert hs.series(POWER, "1h", "unavailable", [], None, NOW)["points"] == []


def test_series_1h_states_from_the_ring():
    ring = [(NOW - 1800, "off"), (NOW - 600, "on")]
    out = hs.series(LAMP, "1h", "on", ring, None, NOW)
    assert out["kind"] == "state" and out["segments"] == [[NOW - 1800, NOW - 600, "off"], [NOW - 600, NOW, "on"]]
    assert out["summary"]["changes"] == 1 and out["summary"]["last"] == "on" and "points" not in out
    assert hs.series(LAMP, "1h", "on", [], None, NOW)["segments"] == [[NOW - 3600, NOW, "on"]]


def test_series_7d_numeric_prefers_statistics():
    stats = [[NOW - 86400 * 3 + i * 3600, 1.0, 2.0, 4.0] for i in range(48)]
    changes = [[NOW - 100, "9"]]
    out = hs.series(POWER, "7d", "2", [], {"stats": stats, "states": changes}, NOW)
    assert out["source"] == "statistics" and out["kind"] == "numeric"
    assert out["summary"]["min"] == 1.0 and out["summary"]["max"] == 4.0 and out["summary"]["mean"] == 2.0
    assert 0 < len(out["points"]) <= hs.BUCKETS
    assert out["start"] == NOW - 7 * 86400


def test_series_7d_numeric_falls_back_to_history_and_states_use_history():
    out = hs.series(POWER, "7d", "2", [], {"stats": [], "states": [[NOW - 5 * 86400, "2"], [NOW - 86400, "6"]]}, NOW)
    assert out["source"] == "history" and [p[2] for p in out["points"]] == [2.0, 6.0]
    out = hs.series(LAMP, "7d", "on", [], {"stats": [], "states": [[NOW - 5000, "off"], [NOW - 4000, "on"]]}, NOW)
    assert out["source"] == "history" and out["kind"] == "state" and out["segments"][-1] == [NOW - 4000, NOW, "on"]
    out = hs.series(LAMP, "30d", "on", [], {"stats": [[NOW - 10, 1, 1, 1]], "states": []}, NOW)       # a state entity never uses statistics
    assert out["source"] == "history" and out["segments"] == []
    assert hs.series(POWER, "24h", "2", [], None, NOW)["points"] == []


# ---------------------------------------------------------------------- entity_rows
def test_entity_rows_skip_automations_scripts_and_private_domains():
    snap = {"devices": [{"id": "d1", "area": "hall"}], "entities": [
        {"entity_id": "sensor.zeta", "name": "Zeta", "state": "1", "unit": "W", "domain": "sensor", "device_id": "d1", "dc": "power"},
        {"entity_id": "light.alpha", "name": None, "state": "on", "domain": "light", "device_id": None, "area": "porch", "platform": "p", "cat": "config"},
        {"entity_id": "automation.x", "name": "Auto", "domain": "automation"},
        {"entity_id": "script.x", "name": "Script", "domain": "script"},
        {"entity_id": "person.sam", "name": "Sam", "domain": "person"},
        {"entity_id": "camera.door", "name": "Door", "domain": "camera"},
    ]}
    rows = hs.entity_rows(snap, {"sensor.zeta": ("7", NOW - 12.34)}, NOW)
    assert [r["id"] for r in rows] == ["light.alpha", "sensor.zeta"]               # sorted by name (the id when unnamed)
    z = next(r for r in rows if r["id"] == "sensor.zeta")
    assert (z["state"], z["changed_s"], z["area"], z["unit"], z["dc"], z["device"]) == ("7", 12.3, "hall", "W", "power", "d1")
    a = next(r for r in rows if r["id"] == "light.alpha")
    assert (a["name"], a["state"], a["changed_s"], a["area"], a["platform"], a["cat"]) == ("light.alpha", "on", None, "porch", "p", "config")
    assert hs.entity_rows({}, {}, NOW) == [] and hs.entity_rows(None, {}) == []


# ---------------------------------------------------------------------- Hub.history over a fake hub
class HistHub(FakeHub):
    """Answers the two history request types; ``stats_error`` makes the statistics request fail."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.msgs = []
        self.stats_error = None

    async def reply(self, writer, msg):
        t = msg["type"]
        if t == "auth":
            return await super().reply(writer, msg)
        self.msgs.append(msg)
        mid = msg["id"]
        if t == "recorder/statistics_during_period":
            if self.stats_error:
                return await self.send(writer, {"id": mid, "type": "result", "success": False,
                                                "error": {"code": self.stats_error, "message": "x"}})
            eid = msg["statistic_ids"][0]
            rows = [{"start": int((WALL - 3 * 3600) * 1000), "min": 1, "mean": 2.5, "max": 4},
                    {"start": datetime.datetime.fromtimestamp(WALL - 2 * 3600, datetime.timezone.utc).isoformat(), "min": 2, "mean": 3.5, "max": 5.5},
                    {"start": WALL - 3600, "min": "x", "mean": 1, "max": 1}]
            return await self.send(writer, {"id": mid, "type": "result", "success": True, "result": {eid: rows}})
        if t == "history/history_during_period":
            eid = msg["entity_ids"][0]
            rows = [{"s": "5", "lu": WALL - 5000}, {"s": "6", "lc": WALL - 4000}, {"state": "7", "last_changed": WALL - 3000}]
            return await self.send(writer, {"id": mid, "type": "result", "success": True, "result": {eid: rows}})
        await self.send(writer, {"id": mid, "type": "result", "success": True, "result": None})

    def of_type(self, t):
        return [x for x in self.msgs if x["type"] == t]


class Clk:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def hist_run(body, connect=True):
    """Start a fake hub and a connected Hub (fake monotonic clock), run ``body(srv, hub, clk)``."""
    async def go():
        srv = await HistHub().start()
        clk = Clk()
        hub = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN, clock=clk)
        hub._states = {"sensor.power": ["120", "W", "Power", None], "light.lamp": ["off", None, "Lamp", None],
                       "person.sam": ["home", None, "Sam", None]}
        for i in range(8):
            hub._states["sensor.e%d" % i] = ["1", "W", None, None]
        conn = None
        try:
            if connect:
                conn = await hub._open()
                conn.start(None)
                hub._conn = conn
            return await body(srv, hub, clk)
        finally:
            if conn is not None:
                await conn.close()
            await srv.stop()
    return asyncio.run(go())


def test_history_7d_numeric_asks_for_statistics_with_exactly_the_allowed_fields():
    async def body(srv, hub, clk):
        res = await hub.history("sensor.power", "7d", True, wall=WALL)
        return res, srv
    res, srv = hist_run(body)
    (req,) = srv.of_type("recorder/statistics_during_period")
    assert set(req) == {"id", "type", "start_time", "end_time", "statistic_ids", "period", "types"}
    assert req["statistic_ids"] == ["sensor.power"] and req["period"] == "hour" and sorted(req["types"]) == ["max", "mean", "min"]
    assert req["end_time"] == datetime.datetime.fromtimestamp(WALL, datetime.timezone.utc).isoformat(timespec="seconds")
    assert req["start_time"] == datetime.datetime.fromtimestamp(WALL - 7 * 86400, datetime.timezone.utc).isoformat(timespec="seconds")
    assert not srv.of_type("history/history_during_period")
    assert res["stats"] == [[WALL - 3 * 3600, 1.0, 2.5, 4.0], [WALL - 2 * 3600, 2.0, 3.5, 5.5]]      # ms and ISO both parse, a bad row is dropped
    assert res["states"] == [] and res["span"] == "7d" and res["entity_id"] == "sensor.power"
    assert res["start"] == WALL - 7 * 86400 and res["end"] == WALL


def test_history_falls_back_to_the_recorder_when_statistics_are_unknown():
    async def body(srv, hub, clk):
        srv.stats_error = "unknown_command"
        res = await hub.history("sensor.power", "30d", True, wall=WALL)
        again = await hub.history("sensor.e1", "30d", True, wall=WALL)
        return res, again, srv
    res, again, srv = hist_run(body)
    assert len(srv.of_type("recorder/statistics_during_period")) == 1             # not asked a second time in this session
    assert len(srv.of_type("history/history_during_period")) == 2
    assert res["stats"] == [] and [s[1] for s in res["states"]] == ["5", "6", "7"]
    assert [s[0] for s in res["states"]] == [WALL - 5000, WALL - 4000, WALL - 3000]
    req = srv.of_type("history/history_during_period")[0]
    assert set(req) == {"id", "type", "start_time", "end_time", "entity_ids", "minimal_response", "no_attributes", "significant_changes_only"}
    assert req["entity_ids"] == ["sensor.power"] and req["minimal_response"] is True and req["no_attributes"] is True
    assert again["states"]


def test_history_falls_back_when_statistics_fail_or_are_empty():
    async def body(srv, hub, clk):
        srv.stats_error = "not_found"
        return await hub.history("sensor.power", "7d", True, wall=WALL), srv
    res, srv = hist_run(body)
    assert res["stats"] == [] and len(res["states"]) == 3


def test_history_24h_and_state_entities_use_the_recorder_only():
    async def body(srv, hub, clk):
        a = await hub.history("sensor.power", "24h", True, wall=WALL)
        b = await hub.history("light.lamp", "7d", False, wall=WALL)
        return a, b, srv
    a, b, srv = hist_run(body)
    assert not srv.of_type("recorder/statistics_during_period")
    assert len(srv.of_type("history/history_during_period")) == 2
    assert a["stats"] == [] and a["states"] and b["states"]


def test_history_answers_are_cached_until_they_go_stale():
    async def body(srv, hub, clk):
        first = await hub.history("sensor.power", "7d", True, wall=WALL)
        n1 = len(srv.msgs)
        second = await hub.history("sensor.power", "7d", True, wall=WALL)
        assert second is first and len(srv.msgs) == n1                           # the second call sends nothing
        clk.t += m.HISTORY_FRESH_S["7d"] - 1
        await hub.history("sensor.power", "7d", True, wall=WALL)
        assert len(srv.msgs) == n1
        clk.t += 2
        await hub.history("sensor.power", "7d", True, wall=WALL)
        return n1, len(srv.msgs)
    n1, n2 = hist_run(body)
    assert n2 == 2 * n1


def test_history_budget_runs_out_and_refills():
    async def body(srv, hub, clk):
        for i in range(4):                                                        # the burst
            await hub.history("sensor.e%d" % i, "24h", True, wall=WALL)
        with pytest.raises(SourceError) as exc:
            await hub.history("sensor.e4", "24h", True, wall=WALL)
        assert exc.value.key == "source.hub.busy"
        n = len(srv.msgs)
        await hub.history("sensor.e0", "24h", True, wall=WALL)                    # cached: costs nothing, works while busy
        assert len(srv.msgs) == n
        clk.t += 60.0 / m.HISTORY_PER_MIN + 0.01                                  # one token comes back
        await hub.history("sensor.e4", "24h", True, wall=WALL)
        with pytest.raises(SourceError) as exc2:
            await hub.history("sensor.e5", "24h", True, wall=WALL)
        return exc2.value.key
    assert hist_run(body) == "source.hub.busy"


def test_history_refuses_private_unknown_and_malformed_requests_without_asking_the_hub():
    async def body(srv, hub, clk):
        keys = []
        for eid, span in (("person.sam", "7d"), ("sensor.not_there", "7d"), ("not an id", "7d"), ("sensor.power", "1h"),
                          ("sensor.power", "year")):
            with pytest.raises(SourceError) as exc:
                await hub.history(eid, span, True, wall=WALL)
            keys.append(exc.value.key)
        return keys, len(srv.msgs)
    keys, sent = hist_run(body)
    assert keys == ["source.hub.history_item"] * 5 and sent == 0


def test_history_offline_without_a_connection():
    async def body(srv, hub, clk):
        with pytest.raises(SourceError) as exc:
            await hub.history("sensor.power", "7d", True, wall=WALL)
        return exc.value.key
    assert hist_run(body, connect=False) == "source.hub.offline"


def test_history_row_parsers():
    assert m._state_changes([{"s": "on", "lu": 1000.5}, {"lc": 2000, "s": "off"}, {"state": "x", "last_changed": "2026-11-16T07:00:00+00:00"},
                             {"s": 5, "lu": 1}, {"s": "no time"}, "junk", None]) == \
        [[1000.5, "on"], [2000.0, "off"], [datetime.datetime(2026, 11, 16, 7, tzinfo=datetime.timezone.utc).timestamp(), "x"]]
    assert m._state_changes(None) == [] and m._state_changes({}) == []
    assert m._stat_rows([{"start": 1_800_000_000_000, "min": 1, "mean": 2, "max": 3},
                         {"start": "2026-11-16T07:00:00Z", "min": 1.5, "mean": 2.5, "max": 3.5},
                         {"start": 5, "min": True, "mean": 1, "max": 1}, {"start": 1, "min": 1, "mean": None, "max": 1}, 3]) == \
        [[datetime.datetime(2026, 11, 16, 7, tzinfo=datetime.timezone.utc).timestamp(), 1.5, 2.5, 3.5], [1_800_000_000.0, 1.0, 2.0, 3.0]]
    assert m._stat_rows(None) == []
