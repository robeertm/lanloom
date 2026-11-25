"""Data keeping (storage.py), counter check (counters.py), change channel (Core.map_change), frame budget assets.

Fixed clocks only; sample addresses (192.168.50.0/24, RFC 5737 outside)."""
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from lanloom import core as core_mod
from lanloom import counters, storage
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.nightly import contrib
from lanloom.store import Store

TZ = "Europe/Amsterdam"
Z = ZoneInfo(TZ)
WEB = Path(__file__).resolve().parent.parent / "lanloom" / "web"


def at(y, mo, d, hh=0, mm=0) -> float:
    return datetime(y, mo, d, hh, mm, tzinfo=Z).timestamp()


def make(tmp_path, path=":memory:"):
    store = Store(path)
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    store.set_setting("report_cfg", {"time": "06:30", "tz": TZ, "keep_days": 30, "link": ""})
    return c


# ---------------------------------------------------------------------- retention
def test_defaults_match_the_old_fixed_values(tmp_path):
    c = make(tmp_path)
    r = c.storage.retention()
    assert r["live_min"] * 60 == c.ring.size == 600
    assert (c.paths.keep_hours, c.paths.keep_days) == (49, 31)
    assert r["reports_days"] == 30 and r["audit_days"] is None or isinstance(r["audit_days"], int)


def test_update_is_all_or_nothing_and_applies(tmp_path):
    c = make(tmp_path)
    c.ring.push("t1", 1.0, 5, 6)
    with pytest.raises(ValueError):
        c.storage.update({"live_min": 30, "paths_days": 7})          # 7 is not offered: nothing changes
    assert c.storage.retention()["live_min"] == 10
    with pytest.raises(ValueError):
        c.storage.update({"live_min": True})                         # a boolean is not a number here
    r = c.storage.update({"live_min": 30, "paths_hours": 169, "paths_days": 366, "reports_days": 90, "scans_keep": 50})
    assert r["live_min"] == 30 and c.ring.size == 1800 and c.ring.data["t1"].cap == 1800
    assert c.ring.series("t1") == [(1.0, 5, 6)]                      # samples survive the resize
    assert (c.paths.keep_hours, c.paths.keep_days) == (169, 366)
    assert c.reports.settings()["keep_days"] == 90                   # the report setting is the same setting
    assert make(tmp_path).storage.retention()["live_min"] == 10      # another store: defaults


def test_cleanup_removes_only_what_is_past_retention(tmp_path):
    c = make(tmp_path)
    now = at(2026, 11, 17, 3, 20)
    hour = int(now // 3600)
    db = c.store
    for h in (hour - 60, hour - 30, hour - 1):
        db.execute("INSERT INTO paths_hour(hour, device, cc, tx, rx) VALUES(?,?,?,?,?)", (h, "d1", "DE", 1, 1))
    for d in (hour // 24 - 40, hour // 24 - 3):
        db.execute("INSERT INTO paths_day(day, device, cc, tx, rx) VALUES(?,?,?,?,?)", (d, "d1", "DE", 1, 1))
    for k in range(260):
        db.execute("INSERT INTO scans(started, finished, mode, cidrs, status, hosts) VALUES(?,?,?,?,?,?)",
                   (now - 9000 + k, now - 8000 + k, "gentle", "[]", "done", 1))
    db.execute("INSERT INTO events(t, kind, data) VALUES(?,?,?)", (now - 50 * 86400, "dev_offline", "{}"))
    db.execute("INSERT INTO events(t, kind, data) VALUES(?,?,?)", (now - 10 * 86400, "dev_offline", "{}"))
    c.reports.clock = lambda: now
    c.storage.clock = lambda: now
    st = c.storage.run(now)
    assert st["removed"]["paths"] == 2                               # hour -60 and day -40
    assert db.one("SELECT COUNT(*) AS n FROM paths_hour")["n"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM scans")["n"] == 200
    assert st["removed"]["availability"] == 1                        # 50 days > 35; 10 days stays
    assert db.get_setting("storage_state")["t"] == now
    assert len(db.get_setting("storage_history")) == 1


def test_journal_keeps_the_longer_of_availability_and_reports(tmp_path):
    c = make(tmp_path)
    now = at(2026, 11, 17, 4)
    c.reports.clock = c.reports.journal.clock = lambda: now
    c.store.execute("INSERT INTO events(t, kind, data) VALUES(?,?,?)", (now - 80 * 86400, "wan_down", "{}"))
    c.storage.update({"availability_days": 90})
    c.storage.run(now)
    assert c.store.one("SELECT COUNT(*) AS n FROM events")["n"] == 1
    c.storage.update({"availability_days": 35})
    c.storage.run(now)
    assert c.store.one("SELECT COUNT(*) AS n FROM events")["n"] == 0


def test_vacuum_on_a_file_database_frees_space_and_sizes_per_area(tmp_path):
    c = make(tmp_path, str(tmp_path / "lanloom.db"))
    now = at(2026, 11, 17, 3, 30)
    rows = [(int(now // 3600) - 400 - k % 300, "d%d" % k, "DE", 10, 10) for k in range(20000)]
    with c.store.transaction() as tx:
        tx._db.executemany("INSERT OR IGNORE INTO paths_hour(hour, device, cc, tx, rx) VALUES(?,?,?,?,?)", rows)
    c.store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = c.storage.db_file_bytes()["db"]
    ov = c.storage.overview()
    paths = next(a for a in ov["areas"] if a["id"] == "paths")
    assert ov["dbstat"] and paths["bytes"] > 0.5 * before and paths["rows"] == 20000
    assert ov["disk"]["free"] > 0 and ov["rss_bytes"] is None or ov["rss_bytes"] > 0
    c.reports.clock = lambda: now
    st = c.storage.run(now, vacuum=True)
    assert st["vacuum"] is True and st["after"] < before / 2 and st["removed"]["paths"] == 20000


def test_due_once_a_night_and_after_a_missed_night(tmp_path):
    c = make(tmp_path)
    s = c.storage
    assert s.due(at(2026, 11, 17, 1))                                # never ran
    c.store.set_setting("storage_state", {"t": at(2026, 11, 17, 3, 15)})
    assert not s.due(at(2026, 11, 17, 14))                           # already ran today
    assert not s.due(at(2026, 11, 18, 2, 50))                        # before 03:00
    assert s.due(at(2026, 11, 18, 3, 5))
    assert s.due(at(2026, 11, 19, 2))                                # more than 36 h: catch up


def test_growth_and_storage_report_section(tmp_path):
    c = make(tmp_path)
    c.store.set_setting("storage_history", [["2026-11-01", 1_000_000], ["2026-11-08", 1_700_000], ["2026-11-11", 2_000_000]])
    assert c.storage.growth() == {"per_day": 100_000, "days": 3}
    from lanloom import storage_report  # noqa: F401 - registers
    ctx = contrib.Ctx(core=c, journal=c.reports.journal, since=at(2026, 11, 16, 6, 30), until=at(2026, 11, 17, 6, 30), prev=None)
    assert storage_report._storage_available(ctx)
    sec = storage_report._storage(ctx)
    assert sec["summary"]["key"] == "storage.report.summary_growth" and sec["summary"]["vars"]["growth_bytes"] == 100_000
    c.storage.days_left = lambda: 12
    sec = storage_report._storage(ctx)
    assert [i["key"] for i in sec["items"] if i["point"]] == ["storage.report.full_soon"]


def test_storage_section_absent_on_a_fresh_install(tmp_path):
    c = make(tmp_path)
    from lanloom import storage_report
    ctx = contrib.Ctx(core=c, journal=c.reports.journal, since=0, until=1e10, prev=None)
    assert not storage_report._storage_available(ctx)


def test_ring_is_compact_wraps_and_forgets_silent_keys():
    from lanloom.live import Ring
    r = Ring(5)
    for k in range(8):
        r.push("a", 100.0 + k, k, 2 * k)
    assert r.series("a") == [(103.0, 3, 6), (104.0, 4, 8), (105.0, 5, 10), (106.0, 6, 12), (107.0, 7, 14)]
    assert r.data["a"].buf.itemsize * len(r.data["a"].buf) == 5 * 3 * 8      # 24 bytes a sample
    r.resize(3)
    assert r.series("a") == [(105.0, 5, 10), (106.0, 6, 12), (107.0, 7, 14)]
    r.push("b", 200.0, 1, 1)
    assert r.prune(202.5) == 1 and list(r.data) == ["b"] and r.samples() == 1


# ---------------------------------------------------------------------- counter check
@pytest.mark.parametrize("ref,val,lower,state", [(100, 100, True, "ok"), (100, 81, True, "ok"), (100, 124, True, "ok"),
                                                 (100, 130, True, "double"), (100, 70, True, "missing"), (100, 30, False, "ok"),
                                                 (100, 200, False, "double")])
def test_judge_bounds(ref, val, lower, state):
    assert counters.judge(ref, val, lower)[1] == state


def test_hourly_evaluation_uplink_and_host(tmp_path):
    c = make(tmp_path)
    c.store.set_setting("netflow_enabled", True)
    cc = c.counters
    t0 = at(2026, 11, 17, 10, 0)
    cc.feed("wan", 100_000_000, t0 + 10)
    cc.feed("netflow", 150_000_000, t0 + 20)       # 1.5x: counted twice
    cc.feed("host_if", 40_000_000, t0 + 30)
    cc.feed("host_flow", 20_000_000, t0 + 40)      # only half: never judged as missing (TCP only)
    cc.tick(t0 + 3605)                             # next hour: evaluate
    res = {r["pair"]: r for r in cc.results()}
    assert res["uplink_flows"]["state"] == "double" and res["uplink_flows"]["factor"] == 1.5
    assert res["host_flows"]["state"] == "ok"
    # one hour is judged on its rolling day: 100 MB of uplink traffic is too little to judge, so no finding yet
    assert res["uplink_flows"]["wfactor"] == 1.5 and res["uplink_flows"]["wstate"] == "ok"
    assert res["uplink_flows"]["whours"] == 1
    assert cc.findings() == []
    # little traffic is not judged; a gap of hours is not judged
    cc.feed("wan", 1_000_000, t0 + 3700)
    cc.tick(t0 + 3 * 3600 + 5)
    assert len(cc.results()) == 2


def test_uplink_without_any_flows_is_missing_and_without_netflow_not_judged(tmp_path):
    c = make(tmp_path)
    cc = c.counters
    t0 = at(2026, 11, 17, 10)
    assert cc.evaluate(1, {"wan": 90_000_000, "netflow": 0, "host_if": 0, "host_flow": 0}) == []
    c.store.set_setting("netflow_enabled", True)
    out = cc.evaluate(int(t0 // 3600), {"wan": 90_000_000, "netflow": 0, "host_if": 0, "host_flow": 0})
    assert out[0]["state"] == "missing" and out[0]["factor"] == 0


def test_counter_section_in_the_nightly_report(tmp_path):
    c = make(tmp_path)
    from lanloom import storage_report
    h = int(at(2026, 11, 17, 2) // 3600)
    c.counters.save([{"hour": h, "pair": "host_flows", "ref": 1, "val": 2, "factor": 2.0, "state": "double"},
                     {"hour": h + 1, "pair": "host_flows", "ref": 1, "val": 1, "factor": 1.0, "state": "ok"},
                     {"hour": h, "pair": "uplink_flows", "ref": 1, "val": 2, "factor": 2.0, "state": "double",
                      "wfactor": 1.31, "wstate": "double", "whours": 24},
                     {"hour": h + 1, "pair": "uplink_flows", "ref": 1, "val": 1, "factor": 1.0, "state": "ok",
                      "wfactor": 1.02, "wstate": "ok", "whours": 24}])
    ctx = contrib.Ctx(core=c, journal=c.reports.journal, since=at(2026, 11, 16, 6, 30), until=at(2026, 11, 17, 6, 30), prev=None)
    assert storage_report._counters_available(ctx)
    sec = storage_report._counters(ctx)
    # the host pair is judged per hour; the uplink pair by its latest window, which adds up again
    assert [(i["key"], i["vars"]["pct"]) for i in sec["items"]] == [("counters.report.double", 200)]
    assert sec["summary"]["key"] == "counters.report.summary_off"


def _hour(cc, hour, wan, nf):
    return cc.evaluate(hour, {"wan": wan, "netflow": nf, "host_if": 0, "host_flow": 0})


def test_late_exporter_hours_balance_over_the_day(tmp_path):
    """An exporter without active timeout: five hours of a long download show 0 %, the hour it ends brings it all."""
    c = make(tmp_path)
    c.store.set_setting("netflow_enabled", True)
    cc = c.counters
    h = int(at(2026, 11, 25, 0) // 3600)
    for k in range(5):
        assert _hour(cc, h + k, 100_000_000, 0)[0]["state"] == "missing"
    last = _hour(cc, h + 5, 100_000_000, 600_000_000)[0]
    assert last["state"] == "double" and last["factor"] == 6.0          # the hour alone looks counted six times
    assert last["wstate"] == "ok" and last["wfactor"] == 1.0 and last["whours"] == 6
    assert cc.findings() == []
    assert cc.view()["findings"] == []
    from lanloom import storage_report
    ctx = contrib.Ctx(core=c, journal=c.reports.journal, since=at(2026, 11, 24, 6, 30), until=at(2026, 11, 25, 6, 30), prev=None)
    assert storage_report._counters(ctx)["items"] == []


def test_quiet_hours_count_in_the_day_window(tmp_path):
    """Hours below the 50 MB threshold are not judged on their own, but their bytes belong to the day sum."""
    c = make(tmp_path)
    c.store.set_setting("netflow_enabled", True)
    cc = c.counters
    h = int(at(2026, 11, 25, 0) // 3600)
    for k in range(8):                                   # a quiet night: 30 MB per hour, the flows still open
        assert _hour(cc, h + k, 30_000_000, 0) == []
    last = _hour(cc, h + 8, 60_000_000, 300_000_000)[0]  # the morning: the night's connections end
    assert last["wstate"] == "ok" and last["wfactor"] == 1.0 and last["whours"] == 9
    assert cc.findings() == []


def test_a_day_that_is_really_double_counted_is_still_reported(tmp_path):
    c = make(tmp_path)
    c.store.set_setting("netflow_enabled", True)
    cc = c.counters
    h = int(at(2026, 11, 25, 0) // 3600)
    for k in range(6):
        row = _hour(cc, h + k, 100_000_000, 200_000_000)[0]
    assert row["wstate"] == "double" and row["wfactor"] == 2.0
    assert [r["hour"] for r in cc.findings()] == [h + 5]                # one point for the day, not six
    from lanloom import storage_report
    ctx = contrib.Ctx(core=c, journal=c.reports.journal, since=at(2026, 11, 24, 6, 30), until=at(2026, 11, 25, 6, 30), prev=None)
    items = storage_report._counters(ctx)["items"]
    assert [(i["key"], i["vars"]["pct"]) for i in items] == [("counters.report.day_double", 200)]
    assert "n" not in items[0]["vars"]


def test_a_day_with_flows_missing_is_reported_once(tmp_path):
    c = make(tmp_path)
    c.store.set_setting("netflow_enabled", True)
    cc = c.counters
    h = int(at(2026, 11, 25, 0) // 3600)
    for k in range(4):
        row = _hour(cc, h + k, 100_000_000, 40_000_000)[0]
    assert row["wstate"] == "missing" and row["wfactor"] == 0.4
    assert len(cc.findings()) == 1


def test_the_window_keeps_24_hours_and_drops_older_ones(tmp_path):
    c = make(tmp_path)
    c.store.set_setting("netflow_enabled", True)
    cc = c.counters
    h = int(at(2026, 11, 20, 0) // 3600)
    _hour(cc, h, 100_000_000, 900_000_000)                                # a burst more than a day ago
    for k in range(25, 50):
        row = _hour(cc, h + k, 100_000_000, 100_000_000)[0]
    assert len(cc.window()) == counters.WINDOW_H
    assert cc.window()[0][0] == h + 49 - counters.WINDOW_H + 1
    assert row["wstate"] == "ok" and row["wfactor"] == 1.0 and row["whours"] == 24
    assert len(cc.results()) <= counters.KEEP
    # a clock jump back drops the hours that now lie in the future
    _hour(cc, h + 30, 100_000_000, 100_000_000)
    assert [e[0] for e in cc.window()] == [h + 26, h + 27, h + 28, h + 29, h + 30]


def test_the_window_is_not_kept_without_netflow(tmp_path):
    c = make(tmp_path)
    cc = c.counters
    _hour(cc, 100, 100_000_000, 0)
    assert cc.window() == []


def test_old_results_without_a_window_still_judge_by_the_hour(tmp_path):
    c = make(tmp_path)
    from lanloom import storage_report
    h = int(at(2026, 11, 17, 2) // 3600)
    c.counters.save([{"hour": h, "pair": "uplink_flows", "ref": 1, "val": 2, "factor": 2.0, "state": "double"},
                     {"hour": h + 1, "pair": "uplink_flows", "ref": 1, "val": 1, "factor": 1.0, "state": "ok"}])
    assert [r["hour"] for r in c.counters.findings()] == [h]
    ctx = contrib.Ctx(core=c, journal=c.reports.journal, since=at(2026, 11, 16, 6, 30), until=at(2026, 11, 17, 6, 30), prev=None)
    sec = storage_report._counters(ctx)
    assert [(i["key"], i["vars"]["pct"]) for i in sec["items"]] == [("counters.report.double", 200)]
    # once a result with a window follows, the old hours no longer count for that pair
    c.counters.save([{"hour": h + 2, "pair": "uplink_flows", "ref": 1, "val": 1, "factor": 1.0, "state": "ok",
                      "wfactor": 1.1, "wstate": "ok", "whours": 3}])
    assert c.counters.findings() == []


def test_effective_state_prefers_the_window_only_for_the_uplink():
    assert counters.effective({"pair": "uplink_flows", "state": "double", "factor": 2.0, "wstate": "ok", "wfactor": 1.0}) == ("ok", 1.0)
    assert counters.effective({"pair": "uplink_flows", "state": "double", "factor": 2.0}) == ("double", 2.0)
    assert counters.effective({"pair": "host_flows", "state": "double", "factor": 2.0, "wstate": "ok"}) == ("double", 2.0)
    assert counters.judge_window([]) == (None, "ok", 0, 0, 0)
    assert counters.judge_window([[1, 10, 30]])[1] == "ok"                # too little traffic to judge


def test_the_source_view_marks_a_shifted_hour_and_shows_the_day():
    js = (WEB / "js" / "views.js").read_text()
    i = js.index("function counterSection")
    body = js[i:js.index("function openSources", i)]
    assert "counters.state.shifted" in body and "chip--muted" in body
    assert "counters.report.day_double" in body and "counters.window" in body and "counters.explain.day" in body


def test_snmp_uplink_and_host_bytes_feed_the_meters(tmp_path):
    c = make(tmp_path)
    c.store.upsert_device({"id": "gw", "ip": "192.168.50.1", "mac": None, "data": {"is_gateway": True}, "kind": "router",
                           "confidence": 1, "title": "Router"}, None)
    c.store.upsert_device({"id": "me", "ip": "192.168.50.2", "mac": None, "data": {"is_self": True}, "kind": "server",
                           "confidence": 1, "title": "Me"}, None)
    c._refresh_index()
    import time
    now = time.time()
    c._book_snmp(None, {"host": "192.168.50.1", "ifaces": [{"name": "wan", "in": 700, "out": 300, "oper": "up", "type": 6}]}, now)
    c.on_event("host", None, {"type": "iface", "ifaces": [{"name": "eth0", "tx": 50, "rx": 70}]})
    c._book_flows([{"peer_ip": "203.0.113.9", "tx": 10, "rx": 20}, {"peer_ip": "172.17.0.5", "tx": 999, "rx": 999}], now)
    assert c.counters.acc["host_if"] == 120
    assert c.counters.acc["host_flow"] == 30                          # a container bridge peer is not counted
    assert c.counters.acc["wan"] in (0, 1000)                         # 1000 when the router interface was picked


# ---------------------------------------------------------------------- change channel
def test_map_version_moves_only_on_change(tmp_path):
    c = make(tmp_path)
    c.store.upsert_device({"id": "d1", "ip": "192.168.50.20", "mac": None, "data": {}, "kind": "computer", "confidence": 1,
                           "title": "PC"}, None)
    import time
    t = time.time() + 10_000                                          # the scan sighting is long past
    v0 = c.map_change(t)
    assert c.map_change(t + 6) == v0                                  # nothing changed
    c.presence["d1"] = t + 7
    assert c.map_change(t + 7.5) == v0                                # checked at most every 5 s
    assert c.map_change(t + 12) == v0 + 1                             # device came up
    frame = c.tick(t + 13)
    assert frame["mapver"] == v0 + 1 and "pathsver" in frame


# ---------------------------------------------------------------------- browser budget (static checks)
def test_no_blur_or_glass_over_the_animated_layers():
    for css in list((WEB / "css").glob("*.css")) + list((WEB / "board").glob("*.css")):
        text = css.read_text(encoding="utf-8")
        for m in re.finditer(r"backdrop-filter\s*:\s*([^;]+);", text):
            assert m.group(1).strip() == "none", "%s: backdrop-filter %s" % (css.name, m.group(1))


def test_dots_have_a_frame_budget_and_pause_out_of_view():
    js = (WEB / "board" / "dots.js").read_text(encoding="utf-8")
    assert "IntersectionObserver" in js and "visibilitychange" in js
    assert "STEPS = [60, 30, 20, 15, 10]" in js and "MOTION = { auto: 30, full: 60, saver: 15, off: 0 }" in js
    assert "getContext('webgl'" in js and "failIfMajorPerformanceCaveat" in js   # only asked, never drawn with
    assert js.count("getContext('2d')") >= 2


def test_map_and_paths_views_do_not_poll():
    views = (WEB / "js" / "views.js").read_text(encoding="utf-8")
    body = views[views.index("function mapView"):views.index("function emptyState")]
    assert "setInterval" not in body and "mapver" in body
    paths = (WEB / "js" / "paths.js").read_text(encoding="utf-8")
    assert "pathsver" in paths and "60000" not in paths
