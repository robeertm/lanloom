"""Early warnings, host health and "waiting on someone" (W1): fixed series with a fixed clock, the readers of every
source, gauge colours and stale dimming, the waiting state, the routes with their roles and the demo data."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod, forecast as fc, hosthealth as hh, waiting as wt
from lanloom.agent import system
from lanloom.main import create_app
from lanloom.sources import snmp
from lanloom.store import Store

NOW = 1_800_000_000.0           # fixed clock
HOUR, DAY = 3600.0, 86400.0
HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"


def series(fn, days=7.0, step=300.0, now=NOW):
    t0 = now - days * DAY
    out, t = [], t0
    while t < now:
        out.append((t, fn(t)))
        t += step
    return out


def verdict(pts, limit, now=NOW):
    return fc.combine(fc.judge(pts, now, "6h", limit), fc.judge(pts, now, "7d", limit))


# ---------------------------------------------------------------------- forecast (pure, fixed clock)
def test_steady_rise_warns_with_the_time_to_the_limit():
    # 80 % seven days ago, +2 % a day: 94 % now, the limit 95 % is half a day away
    pts = series(lambda t: 80.0 + 2.0 * (t - (NOW - 7 * DAY)) / DAY)
    v = verdict(pts, 95.0)
    assert v["level"] == "warn" and v["shape"] == "rising"
    assert 9 < v["eta_h"] < 15                       # 1 % left at +2 % a day: about 12 h
    assert v["n"] >= 3 and v["window"] == "7d"


def test_slow_rise_is_info_not_warning():
    pts = series(lambda t: 50.0 + 1.0 * (t - (NOW - 7 * DAY)) / DAY)      # 57 % now, 95 % in about 38 days
    v = verdict(pts, 95.0)
    assert v["level"] == "info" and v["eta_h"] > 72


def test_sawtooth_never_warns():
    # a cache that fills for 10 hours and is emptied: rising most of the time, but never a trend
    pts = series(lambda t: 40.0 + 50.0 * (((t - NOW) % (10 * HOUR)) / (10 * HOUR)))
    v = verdict(pts, 95.0)
    assert v["level"] in ("calm", "info") and v["level"] != "warn"


def test_daily_rhythm_in_the_short_span_needs_the_long_spans_consent():
    import math
    pts = series(lambda t: 60.0 + 25.0 * math.sin((t - NOW + 3 * HOUR) / DAY * 2 * math.pi))     # rising through the last 6 h
    short = fc.judge(pts, NOW, "6h", 95.0)
    assert short["direction"] == 1
    v = verdict(pts, 95.0)
    assert v["level"] != "warn"


def test_too_few_points_is_nodata_and_three_are_enough():
    pts = [(NOW - 2 * HOUR, 50.0), (NOW - 1.5 * HOUR, 60.0)]
    assert fc.judge(pts, NOW, "6h", 95.0, step=1800)["level"] == "nodata"
    pts3 = [(NOW - 5.6 * HOUR + i * 1800, 70.0 + i * 2.0) for i in range(12)]
    j = fc.judge(pts3, NOW, "6h", 95.0, step=1800)
    assert j["n"] >= 3 and j["level"] == "warn" and j["eta_h"] < 72


def test_falling_trend_is_info_without_eta_and_flat_is_calm():
    pts = series(lambda t: 90.0 - 3.0 * (t - (NOW - 7 * DAY)) / DAY)
    v = verdict(pts, 95.0)
    assert v["level"] == "info" and v["shape"] == "falling" and v["eta_h"] is None
    flat = verdict(series(lambda t: 41.0), 95.0)
    assert flat["level"] == "calm" and flat["shape"] == "flat"


def test_over_the_limit_already_is_eta_zero():
    pts = series(lambda t: 90.0 + 1.5 * (t - (NOW - 7 * DAY)) / DAY)
    assert verdict(pts, 95.0)["eta_h"] == 0.0


def test_gaps_do_not_make_support_points():
    pts = [p for p in series(lambda t: 50.0 + (t - NOW) / DAY) if not (NOW - 5 * DAY < p[0] < NOW - 1 * DAY)]
    j = fc.judge(pts, NOW, "7d", 95.0)
    assert j["n"] < 14


# ---------------------------------------------------------------------- readers of the sources
def _rows(base, items):
    return [("%s.%s" % (base, i), v) for i, v in items]


def test_snmp_host_resources_parse():
    storage = {
        snmp.OID_HR_STORAGE_TYPE: _rows(snmp.OID_HR_STORAGE_TYPE, [(1, snmp.HR_RAM), (3, snmp.HR_VIRTUAL), (31, snmp.HR_FIXED_DISK),
                                                                    (32, snmp.HR_FIXED_DISK), (33, snmp.HR_FIXED_DISK)]),
        snmp.OID_HR_STORAGE_DESCR: _rows(snmp.OID_HR_STORAGE_DESCR, [(1, "Physical memory"), (3, "Swap space"), (31, "/"), (32, "/srv/data"), (33, "/run")]),
        snmp.OID_HR_STORAGE_UNITS: _rows(snmp.OID_HR_STORAGE_UNITS, [(1, 1024), (3, 1024), (31, 4096), (32, 4096), (33, 4096)]),
        snmp.OID_HR_STORAGE_SIZE: _rows(snmp.OID_HR_STORAGE_SIZE, [(1, 8_000_000), (3, 2_000_000), (31, 10_000_000), (32, 100_000_000), (33, 1_000_000)]),
        snmp.OID_HR_STORAGE_USED: _rows(snmp.OID_HR_STORAGE_USED, [(1, 7_000_000), (3, 500_000), (31, 4_000_000), (32, 81_000_000), (33, 999_000)]),
    }
    proc = _rows(snmp.OID_HR_PROC_LOAD, [(196608, 20), (196609, 40)])
    la = _rows(snmp.OID_LA_LOAD, [(1, "0.80"), (2, "0.60"), (3, "0.45")])
    ucd = [(snmp.OID_UCD_MEM + ".5.0", 8_000_000), (snmp.OID_UCD_MEM + ".6.0", 1_000_000), (snmp.OID_UCD_MEM + ".14.0", 500_000),
           (snmp.OID_UCD_MEM + ".15.0", 2_500_000), (snmp.OID_UCD_MEM + ".3.0", 2_000_000), (snmp.OID_UCD_MEM + ".4.0", 1_500_000)]
    temp = _rows(snmp.OID_LM_TEMP, [(1, 48000), (2, 61500)])
    out = snmp.parse_host_resources(storage, proc, la, ucd, temp)
    assert out["cpu"] == 30.0 and out["cores"] == 2
    assert out["load"] == 0.45
    assert out["mem"] == 50.0                 # UCD: buffers and cache count as free (hrStorage would say 87.5 %)
    assert out["swap"] == 25.0
    assert out["disk"] == 81.0 and out["disk_mount"] == "/srv/data"      # /run is skipped
    assert out["temp"] == 61.5


def _mem_storage(extra=()):
    """hrStorage rows like the common open-source agent: physical memory 6 047 804 kB with total - MemFree used (lanloom#18 figures)."""
    rows = [(1, snmp.HR_RAM, "Physical memory", 1024, 6_047_804, 6_047_804 - 422_928)] + list(extra)
    cols = {snmp.OID_HR_STORAGE_TYPE: 1, snmp.OID_HR_STORAGE_DESCR: 2, snmp.OID_HR_STORAGE_UNITS: 3, snmp.OID_HR_STORAGE_SIZE: 4,
            snmp.OID_HR_STORAGE_USED: 5}
    return {c: _rows(c, [(r[0], r[k]) for r in rows]) for c, k in cols.items()}


BUFCACHE = [(6, snmp.HR_OTHER, "Memory buffers", 1024, 6_047_804, 300_000), (7, snmp.HR_OTHER, "Cached memory", 1024, 2_234_320, 2_234_320)]


def test_snmp_memory_gives_buffers_and_cache_back_without_the_ucd_table():
    # lanloom#18: a firewall read 93 % (total - MemFree) while `free` said ~60 %; the agent serves no UCD memory table
    assert snmp.parse_host_resources(_mem_storage(), [], [], [], [])["mem"] == 93.0            # nothing to give back: as reported
    out = snmp.parse_host_resources(_mem_storage(BUFCACHE), [], [], [], [])
    assert out["mem"] == 51.1                                                                   # (used - buffers - cache) / total
    assert "volumes" not in out and out["disk"] is None                                         # the extra rows are no disks


def test_snmp_memory_prefers_mem_available():
    ucd = [(snmp.OID_UCD_MEM + ".5.0", 6_047_804), (snmp.OID_UCD_MEM + ".6.0", 422_928), (snmp.OID_UCD_MEM + ".27.0", 2_427_996)]
    assert snmp.parse_host_resources(_mem_storage(BUFCACHE), [], [], ucd, [])["mem"] == 59.9     # MemAvailable, the `free` view
    # UCD free without its own buffer/cache rows: the hrStorage rows of the same agent fill in
    ucd = [(snmp.OID_UCD_MEM + ".5.0", 6_047_804), (snmp.OID_UCD_MEM + ".6.0", 422_928)]
    assert snmp.parse_host_resources(_mem_storage(BUFCACHE), [], [], ucd, [])["mem"] == 51.1
    bogus = [(snmp.OID_UCD_MEM + ".5.0", 6_047_804), (snmp.OID_UCD_MEM + ".6.0", 422_928), (snmp.OID_UCD_MEM + ".27.0", 0)]
    assert snmp.parse_host_resources(_mem_storage(BUFCACHE), [], [], bogus, [])["mem"] == 51.1  # a zero MemAvailable is not believed


def test_snmp_memory_gives_the_zfs_cache_back():
    # lanloom#25: a ZFS storage box read 96 % while its own dashboard said ~24 %; the ZFS cache is in no "available" figure
    ucd = [(snmp.OID_UCD_MEM + ".5.0", 6_047_804), (snmp.OID_UCD_MEM + ".6.0", 422_928),
           (snmp.OID_UCD_MEM + ".27.0", 422_928)]
    arc = [(snmp.OID_ZFS_ARC + ".0", 3_000_000)]                                              # the MIB counts in kB
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [])["mem"] == 93.0           # as reported
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [], arc)["mem"] == 43.4      # cache given back
    assert snmp.parse_host_resources(_mem_storage(), [], [], [], [], arc)["mem"] == 43.4       # no UCD table at all
    big = [(snmp.OID_ZFS_ARC + ".0", 99_000_000)]                                             # larger than the memory
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [], big)["mem"] == 93.0      # not believed


def test_snmp_memory_takes_the_zfs_cache_from_the_scalar_when_the_documented_place_says_zero():
    # lanloom#25 (1.65.1 field test): newer firmware answers 0 at the documented place and serves the size as a scalar elsewhere
    ucd = [(snmp.OID_UCD_MEM + ".5.0", 6_047_804), (snmp.OID_UCD_MEM + ".6.0", 422_928), (snmp.OID_UCD_MEM + ".27.0", 422_928)]
    zero = (snmp.OID_ZFS_ARC + ".0", 0)
    scalar = (snmp.OID_ZFS_ARC_SCALAR, 3_000_000)
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [], [zero])["mem"] == 93.0           # zero alone: nothing to give back
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [], [zero, scalar])["mem"] == 43.4    # the scalar fills in
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [], [scalar])["mem"] == 43.4
    other = (snmp.OID_ZFS_ARC_SCALAR[:-2] + ".1.1", 3_000_000)                                           # a table row beside it
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [], [other])["mem"] == 93.0
    first = (snmp.OID_ZFS_ARC + ".0", 1_000_000)                                                         # the documented place wins
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [], [first, scalar])["mem"] == 76.5
    huge = (snmp.OID_ZFS_ARC + ".0", 99_000_000)                                                         # does not fit: next place
    assert snmp.parse_host_resources(_mem_storage(), [], [], ucd, [], [huge, scalar])["mem"] == 43.4


def test_read_host_resources_asks_the_scalar_place_too():
    asked = []

    class Agent:
        def walk(self, base, **kw):
            asked.append(base)
            if base == snmp.OID_HR_PROC_LOAD:
                return [(snmp.OID_HR_PROC_LOAD + ".1", 10)]
            if base == snmp.OID_ZFS_ARC:
                return [(snmp.OID_ZFS_ARC + ".0", 0)]
            if base == snmp.OID_ZFS_ARC_SCALAR[:-2]:
                return [(snmp.OID_ZFS_ARC_SCALAR, 3_000_000)]
            return []

    out = snmp.read_host_resources(Agent())
    assert snmp.OID_ZFS_ARC in asked and snmp.OID_ZFS_ARC_SCALAR[:-2] in asked and out["cpu"] == 10.0


def test_snmp_without_host_tables_is_empty():
    out = snmp.parse_host_resources({}, [], [], [], [])
    assert all(v is None for v in out.values())


def test_hub_sensor_matching():
    m = hh.hub_metric
    assert m({"domain": "sensor", "entity_id": "sensor.processor_use", "name": "Processor use", "unit": "%"}) == "cpu"
    assert m({"domain": "sensor", "entity_id": "sensor.memory_use_percent", "name": "Memory use", "unit": "%"}) == "mem"
    assert m({"domain": "sensor", "entity_id": "sensor.disk_use_percent", "name": "Disk use", "unit": "%"}) == "disk"
    assert m({"domain": "sensor", "entity_id": "sensor.swap_use_percent", "name": "Swap use", "unit": "%"}) == "swap"
    assert m({"domain": "sensor", "entity_id": "sensor.processor_temperature", "name": "Processor temperature", "unit": "°C", "dc": "temperature"}) == "temp"
    assert m({"domain": "sensor", "entity_id": "sensor.load_15m", "name": "Load (15m)", "unit": None}) == "load"
    # never: battery, a room temperature, a free-space figure
    assert m({"domain": "sensor", "entity_id": "sensor.remote_battery", "name": "Remote battery", "unit": "%"}) is None
    assert m({"domain": "sensor", "entity_id": "sensor.living_room_temperature", "name": "Living room temperature", "unit": "°C"}) is None
    assert m({"domain": "sensor", "entity_id": "sensor.disk_free", "name": "Disk free", "unit": "%"}) is None


def test_agent_and_hypervisor_readings():
    a = hh.from_agent({"load": [0.5, 0.7, 0.9], "mem_total": 1000, "mem_avail": 250, "swap_total": 100, "swap_free": 60,
                       "disks": [{"total": 100, "free": 50}, {"total": 200, "free": 20}], "cpu": 12.5, "temp": 55.0,
                       "freq_pct": 80.0, "throttled": True}, 4)
    assert a == {"cpu": 12.5, "mem": 75.0, "swap": 40.0, "load": 0.9, "cores": 4, "disk": 90.0, "temp": 55.0, "freq": 80.0, "throttled": True}
    v = hh.from_hypervisor({"cpu": 0.18, "cores": 8, "mem_used": 14, "mem_total": 32, "load": [1, 2, 3], "swap_used": 1, "swap_total": 4},
                           [{"used": 900 << 20, "total": 1000 << 20}, {"used": 1, "total": 10}])
    assert v["cpu"] == 18.0 and v["mem"] == 43.8 and v["swap"] == 25.0 and v["load"] == 3 and v["disk"] == 90.0


def test_gauge_colours():
    lv = hh.level_of
    assert (lv("cpu", 69.9), lv("cpu", 70), lv("cpu", 90), lv("cpu", 90.1)) == ("ok", "warn", "warn", "danger")
    assert (lv("temp", 69), lv("temp", 75), lv("temp", 86)) == ("ok", "warn", "danger")
    assert (lv("load", 60), lv("load", 95), lv("load", 140)) == ("ok", "warn", "danger")
    assert lv("mem", None) == "none"


def test_agent_reads_temperature_throttle_and_processor(tmp_path):
    z = tmp_path / "thermal"
    for n, kind, milli in ((0, "acpitz", 41000), (1, "x86_pkg_temp", 63500), (2, "iwlwifi_1", 70000)):
        d = z / ("thermal_zone%d" % n)
        d.mkdir(parents=True)
        (d / "type").write_text(kind + "\n")
        (d / "temp").write_text(str(milli) + "\n")
    assert system.temperature(str(z)) == 63.5          # processor-near zones win over a wireless card
    root = tmp_path / "sys"
    (root / "devices/system/cpu/cpu0/cpufreq").mkdir(parents=True)
    (root / "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").write_text("1200000\n")
    (root / "devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq").write_text("2400000\n")
    fw = root / "devices/platform/soc/soc:firmware"
    fw.mkdir(parents=True)
    (fw / "get_throttled").write_text("0x4\n")
    assert system.throttle(str(root)) == {"throttled": True, "freq_pct": 50.0}
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0\n")
    assert system.cpu_percent(str(proc)) is None
    (proc / "stat").write_text("cpu  150 0 150 900 0 0 0 0\n")
    assert system.cpu_percent(str(proc)) == 50.0
    assert system.temperature(str(tmp_path / "none")) is None


# ---------------------------------------------------------------------- HostHealth with a stand-in core
class Notify:
    def __init__(self):
        self.sent = []

    def emit(self, eid, key, data=None, resolve=False, device=None, t=None, **kw):
        self.sent.append((eid, key, resolve, device))


@pytest.fixture()
def health():
    store = Store(":memory:")
    clock = {"t": NOW}
    core = SimpleNamespace(store=store, agents=None, snmp={}, ip_to_device={"192.0.2.10": "d-box", "192.0.2.20": "d-hv"}, hub={},
                           live_configs=lambda: [], notify=Notify())
    h = hh.HostHealth(core, clock=lambda: clock["t"])
    h._names = lambda: {"d-box": {"title": "Box", "kind": "server"}, "d-hv": {"title": "Node", "kind": "server"}}
    return h, core, clock


def test_samples_card_and_stale_dimming(health):
    h, core, clock = health
    core.snmp[1] = {"host": "192.0.2.10", "t": NOW, "hr_t": NOW, "sys": {"name": "box"},
                    "hr": {"cpu": 95.0, "mem": 40.0, "swap": None, "load": 1.0, "cores": 4, "disk": 75.0, "temp": None}}
    assert h.maybe_sample(NOW) == 1
    assert h.maybe_sample(NOW + 10) == 0              # once per slot
    c = h.card("d-box", NOW + 10)
    assert c["known"] and c["ring"]["level"] == "danger" and c["ring"]["metric"] == "cpu"
    g = {x["metric"]: x for x in c["gauges"]}
    assert g["cpu"]["level"] == "danger" and g["mem"]["level"] == "ok" and g["disk"]["level"] == "warn" and g["load"]["value"] == 25.0
    # the source stops: 20 minutes later every gauge is dimmed ("stale"), never red
    c2 = h.card("d-box", NOW + 1200)
    assert all(x["level"] == "stale" and x["stale"] for x in c2["gauges"])
    assert c2["ring"]["level"] == "stale" and c2["sources"][0]["stale"]
    assert h.card("d-unknown", NOW) == {"known": False}


def test_hints_announce_once_and_clear(health):
    h, core, clock = health
    step = hh.SAMPLE_S
    rows = []
    for k in range(int(7 * DAY // step)):
        t = NOW - 7 * DAY + k * step
        rows.append((int(t // step), "d-box", "agent", 10.0, 50.0, None, 0.5, 4, 80.0 + 2.0 * k * step / DAY, None, None, None))
    with h.store.transaction() as st:
        st._db.executemany("INSERT INTO host_samples(slot, device, source, %s) VALUES(?,?,?,%s)" % (", ".join(hh.COLUMNS), ",".join("?" * len(hh.COLUMNS))), rows)
    hints = h.hints(NOW)
    assert [(x["device"], x["metric"]) for x in hints] == [("d-box", "disk")]
    assert hints[0]["eta_h"] < 72
    h.announce(NOW)
    h.announce(NOW + 30)
    assert core.notify.sent == [("host_forecast", "host_forecast:d-box:disk", False, "d-box")]
    h.store.execute("DELETE FROM host_samples")
    h.invalidate()
    h.announce(NOW + 60)
    assert core.notify.sent[-1] == ("host_forecast", "host_forecast:d-box:disk", True, None)


def test_prune_and_forget(health):
    h, core, clock = health
    h.store.execute("INSERT INTO host_samples(slot, device, source, cpu) VALUES(?,?,?,?)", (int((NOW - 40 * DAY) // 300), "d-box", "agent", 1.0))
    h.store.execute("INSERT INTO host_samples(slot, device, source, cpu) VALUES(?,?,?,?)", (int(NOW // 300), "d-hv", "agent", 1.0))
    assert h.prune(NOW) == 1
    h.forget("d-hv")
    assert not h.store.one("SELECT 1 AS x FROM host_samples")


# ---------------------------------------------------------------------- waiting on someone
def note(nid="dev:x", level="warn", human=True, **vars_):
    return {"id": nid, "level": level, "key": "ov.note.dev_offline", "vars": dict({"name": "Printer"}, **vars_), "human": human}


def rounds(notes_fn, mem, start, end, every=4 * HOUR):
    t, out = start, None
    while t <= end:
        out = wt.apply(notes_fn(t), mem, t)
        mem = out[2]
        t += every
    return out


def test_waiting_after_24_hours_unchanged():
    active, waiting, mem = rounds(lambda t: [note(age_dur=int(t - NOW))], {}, NOW, NOW + 24 * HOUR)    # the clock value does not count
    assert active and not waiting                                                                        # exactly 24 h: not yet
    active, waiting, mem = wt.apply([note(age_dur=1)], mem, NOW + 24 * HOUR + 600)
    assert not active and waiting[0]["level"] == "waiting" and waiting[0]["waiting"]["level"] == "warn"
    assert waiting[0]["waiting"]["since"] == round(NOW)


def test_any_change_ends_waiting_at_once():
    _, waiting, mem = rounds(lambda t: [note()], {}, NOW, NOW + 28 * HOUR)
    assert waiting
    active, waiting, mem = wt.apply([note(level="danger")], mem, NOW + 28 * HOUR + 60)
    assert active and not waiting and mem["dev:x"]["since"] == NOW + 28 * HOUR + 60
    active, waiting, mem = wt.apply([note(level="danger"), note("dev:y")], mem, NOW + 28 * HOUR + 120)
    assert len(active) == 2


def test_gap_restarts_and_rows_without_a_person_never_wait():
    mem = {}
    _, _, mem = wt.apply([note(), note("src:1", human=False)], mem, NOW)
    active, waiting, mem = wt.apply([note(), note("src:1", human=False)], mem, NOW + 30 * HOUR)    # 30 h without a round
    assert not waiting and len(active) == 2 and "src:1" not in mem


def test_waiting_rows_do_not_set_the_level(tmp_path):
    from lanloom import overview
    store = Store(":memory:")
    w = wt.Waiting(store)
    w.backdate = lambda n: NOW - 30 * HOUR
    active, waiting = w.split([note(), {"id": "x", "level": "info", "key": "k", "vars": {}}], NOW)
    assert [n["id"] for n in waiting] == ["dev:x"] and [n["id"] for n in active] == ["x"]
    assert store.get_setting(wt.SETTING)["dev:x"]["since"] == NOW - 30 * HOUR      # survives a restart
    assert wt.Waiting(store).is_waiting(note(), NOW)
    assert overview._rank(active[0]["level"]) < overview._rank("warn")


# ---------------------------------------------------------------------- routes, roles, demo
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.data_dir = tmp_path
        c.app_ref = app
        yield c


def _sign_in(c):
    code = (Path(c.data_dir) / "setup-code.txt").read_text().strip()
    assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200


def _wait(fn, secs=40):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    return fn()


def test_routes_need_sign_in_and_demo_shows_everything(client):
    assert client.get("/api/hosthealth").status_code == 401
    assert client.get("/api/hosthealth/d-x").status_code == 401
    _sign_in(client)
    core = client.app_ref.state.ll.core
    hints = _wait(lambda: client.get("/api/hosthealth").json()["forecasts"])
    assert hints and hints[0]["metric"] == "disk" and hints[0]["eta_h"] < 72
    card = client.get("/api/hosthealth/" + hints[0]["device"]).json()
    assert card["known"] and card["ring"]["level"] in ("warn", "danger")
    assert {g["metric"] for g in card["gauges"]} >= {"cpu", "mem", "disk", "temp", "load"}
    disk = next(f for f in card["forecasts"] if f["metric"] == "disk")
    assert disk["level"] == "warn" and disk["n"] >= 3
    assert client.get("/api/hosthealth/d-nothing").json() == {"known": False}
    # a stale source of the sample is dimmed, not red
    home = core.agents.device_of("px-demo0home")
    hc = client.get("/api/hosthealth/" + home).json()
    assert any(s["stale"] for s in hc["sources"])
    # the overview: forecasts and the waiting line (a battery that has needed replacing for two days)
    s = _wait(lambda: (lambda d: d if d.get("waiting") else None)(client.get("/api/overview").json()))
    assert s["forecasts"] and s["waiting"][0]["id"].startswith("onsite:") and s["waiting"][0]["level"] == "waiting"
    assert all(n["id"] != s["waiting"][0]["id"] for n in s["notes"])
    # the nightly report section
    from lanloom.nightly.contrib import REGISTRY, Ctx
    contrib = next(c for c in REGISTRY if c.id == "forecast")
    ctx = Ctx(core=core, journal=None, since=time.time() - DAY, until=time.time(), prev=None)
    assert contrib.available(ctx)
    sec = contrib.build(ctx)
    assert sec["items"] and sec["items"][0]["point"] and sec["items"][0]["key"] == "hh.report.item"
