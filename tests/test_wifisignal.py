"""W2: signal per wireless device, devices clinging to a far access point, the night report line - and nothing of it stored."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import core, portfront, portnotify, wifisignal
from lanloom.demo_live import live_sample
from lanloom.nightly.contrib import REGISTRY, Ctx
from lanloom.store import Store

T0 = 1_800_000_000.0


def test_clean_keeps_only_plausible_dbm():
    assert [wifisignal.clean(v) for v in (-62, "-62", "-62@5GHz", -61.6, " -70 ")] == [-62, -62, -62, -62, -70]
    assert all(wifisignal.clean(v) is None for v in (None, True, "", "abc", 0, 5, 80, -9, -101, -300, float("nan"), [], {}))
    assert (wifisignal.clean(-100), wifisignal.clean(-10)) == (-100, -10)


def test_grades_follow_the_agreed_limits():
    assert [wifisignal.grade(v) for v in (-40, -65, -66, -75, -76, -95)] == ["good", "good", "fair", "fair", "weak", "weak"]


def feed(r, t, sig, ap="A", band="5ghz", cid="tab"):
    r.feed([{"id": cid, "ap": ap, "band": band, "signal": sig, "name": "Tablet"}], t)


def test_a_device_is_stuck_after_ten_minutes_at_minus_80_or_weaker():
    r = portfront.RoamWatch()
    for k in range(0, 10):
        feed(r, T0 + 60 * k, -86)
    assert r.signal_view("A", True, T0 + 9 * 60)["stuck"] == []                 # nine minutes: not yet
    feed(r, T0 + 600, -86)
    s = r.signal_view("A", True, T0 + 600)
    assert s["stuck"] == [{"name": "Tablet", "signal": -86, "band": "5ghz", "since_s": 600}]
    assert wifisignal.STICK_DBM == -80 and wifisignal.STICK_S == 600


def test_the_limit_is_minus_80_inclusive_and_one_better_reading_starts_over():
    r = portfront.RoamWatch()
    for k in range(0, 11):
        feed(r, T0 + 60 * k, -80)
    assert len(r.signal_view("A", True, T0 + 600)["stuck"]) == 1
    feed(r, T0 + 660, -79)
    assert r.signal_view("A", True, T0 + 660)["stuck"] == []
    for k in range(12, 21):                                                      # the controller keeps delivering (W5: a view
        feed(r, T0 + 60 * k, -85)                                                # of a stale source shows nothing at all)
    assert r.signal_view("A", True, T0 + 720 + 539)["stuck"] == []              # the clock started again at 720
    for k in (21, 22):
        feed(r, T0 + 60 * k, -85)
    assert len(r.signal_view("A", True, T0 + 720 + 600)["stuck"]) == 1


def test_a_missing_reading_a_move_or_a_gap_start_over():
    r = portfront.RoamWatch()
    for k in range(0, 8):
        feed(r, T0 + 60 * k, -90)
    feed(r, T0 + 480, None)                                                      # no reading: no claim
    for k in range(9, 15):
        feed(r, T0 + 60 * k, -90)
    assert r.signal_view("A", True, T0 + 14 * 60)["stuck"] == []
    r2 = portfront.RoamWatch()
    for k in range(0, 8):
        feed(r2, T0 + 60 * k, -90, ap="A")
    for k in range(8, 14):
        feed(r2, T0 + 60 * k, -90, ap="B")                                       # moved to another access point: new start
    assert r2.signal_view("B", True, T0 + 13 * 60)["stuck"] == [] and r2.signal_view("A", True, T0 + 13 * 60) == {}
    r3 = portfront.RoamWatch()
    for k in range(0, 5):
        feed(r3, T0 + 60 * k, -90)
    r3.feed([], T0 + 300)                                                        # gone for a poll
    for k in range(6, 12):
        feed(r3, T0 + 60 * k, -90)
    assert r3.signal_view("A", True, T0 + 11 * 60)["stuck"] == []


def test_without_a_second_access_point_nothing_clings():
    r = portfront.RoamWatch()
    for k in range(0, 12):
        feed(r, T0 + 60 * k, -90)
    now = T0 + 11 * 60
    assert r.signal_view("A", False, now)["stuck"] == [] and r.stuck({"A": "Hall"}, now) == []
    assert r.signal_view("A", False, now)["weakest"][0]["signal"] == -90        # the distribution still shows
    assert len(r.stuck({"A": "Hall", "B": "Garden"}, now)) == 1


def test_the_distribution_per_band_and_the_weakest_devices():
    r = portfront.RoamWatch()
    r.feed([{"id": "a", "ap": "A", "band": "5ghz", "signal": -50, "name": "Phone"}, {"id": "b", "ap": "A", "band": "5ghz", "signal": -70},
            {"id": "c", "ap": "A", "band": "2.4ghz", "signal": -88, "name": "Sensor"}, {"id": "d", "ap": "A", "band": "2.4ghz", "signal": None},
            {"id": "e", "ap": "A", "band": "2.4ghz", "signal": 55, "name": "Bogus"}, {"id": "f", "ap": "B", "band": "5ghz", "signal": -40}], T0)
    s = r.signal_view("A", True, T0)
    assert s["bands"] == {"5ghz": {"good": 1, "fair": 1, "weak": 0}, "2.4ghz": {"good": 0, "fair": 0, "weak": 1}}
    assert [(w["name"], w["signal"]) for w in s["weakest"]] == [("Sensor", -88), ("?", -70), ("Phone", -50)]
    assert r.signal_view("C", True, T0) == {}                                    # no readings: no view, no empty bars


def test_the_faceplate_of_an_access_point_carries_the_signal_view():
    st = live_sample(T0)
    r = portfront.RoamWatch()
    for k in range(0, 12):
        r.feed([{"id": "t", "ap": "MAC-AP1", "band": "5ghz", "signal": -86, "name": "Kitchen tablet"}], T0 + 60 * k)
    keys = {"e_ap1": "MAC-AP1", "e_ap2": "MAC-AP2"}
    ap = next(f for f in portfront.fronts(st, portfront.PortWatch(), r, T0 + 660, ap_keys=keys) if f["name"] == "Access point living room")
    assert ap["signal"]["stuck"][0]["name"] == "Kitchen tablet" and ap["signal"]["bands"]["5ghz"]["weak"] == 1
    only = portfront.fronts(st, portfront.PortWatch(), r, T0 + 660, ap_keys=keys, ap_total=1)
    assert next(f for f in only if f["name"] == "Access point living room")["signal"]["stuck"] == []
    other = next(f for f in portfront.fronts(st, portfront.PortWatch(), r, T0 + 660, ap_keys=keys) if f["name"] == "Access point garden")
    assert "signal" not in other                                                 # no reading at that access point: no empty block


@pytest.fixture()
def c():
    k = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), Store(":memory:"), None)
    st = live_sample(T0)
    k.live_cfg[5] = {"state": st, "caps": {}, "family": "x", "t": time.time()}
    k.integrations = lambda: [{"id": 5, "name": "Sample", "kind": "controller", "enabled": True}]
    k.topo_obs = {"int:5": {"integ": 5, "ev": {"equipment": [{"id": "e_ap1", "mac": "MAC-AP1"}, {"id": "e_ap2", "mac": "MAC-AP2"}]}}}
    return k


def book(k, t, sig, ap="MAC-AP2"):
    k.book_roaming({"clients": [{"mac": "02:aa:bb:cc:dd:01", "medium": "radio", "parent_mac": ap, "band": "5ghz", "signal": sig,
                                 "name": "Kitchen tablet", "ip": "192.168.50.23"}]}, t)


def test_the_core_books_the_signal_and_names_the_stuck_device_with_its_access_point(c):
    now = time.time()
    for k in range(12):
        book(c, now - 660 + 60 * k, -86)
    assert c.ap_names() == {"MAC-AP1": "Access point living room", "MAC-AP2": "Access point garden"}
    s = c.sticky_clients(now)
    assert [(x["name"], x["ap"], x["signal"]) for x in s] == [("Kitchen tablet", "Access point garden", -86)]
    fronts = c.port_fronts(now)
    ap2 = next(f for f in fronts if f["name"] == "Access point garden")
    assert ap2["signal"]["stuck"][0]["since_s"] >= 600


def test_nothing_about_who_sat_where_reaches_the_store(c):
    now = time.time()
    for k in range(12):
        book(c, now - 660 + 60 * k, -86)
    book(c, now, -86, ap="MAC-AP1")                                             # and a move
    portfront.PortWatch(c.store).feed([], now)
    dump = ""
    for name in (r["name"] for r in c.store.query("SELECT name FROM sqlite_master WHERE type='table'")):
        dump += " ".join(str(dict(r)) for r in c.store.query('SELECT * FROM "%s"' % name))
    assert dump.strip()                                                         # the dump really read the tables
    assert "02:aa:bb:cc:dd:01" not in dump and "Kitchen tablet" not in dump and "-86" not in dump
    assert c.roamwatch.moves and all(len(m) == 3 and isinstance(m[0], float) for m in c.roamwatch.moves)
    assert not hasattr(c.roamwatch, "store")


def test_sites_keep_their_own_watch():
    a = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), Store(":memory:"), None)
    b = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), Store(":memory:"), None, site="away", remote=True)
    book(a, T0, -86)
    assert a.roamwatch.air and not b.roamwatch.air and a.roamwatch is not b.roamwatch


def section(c, now):
    sec = next(x for x in REGISTRY if x.id == "ports")
    ctx = Ctx(core=c, journal=None, since=now - 43200, until=now, prev=None)
    return sec, ctx


def test_the_night_report_names_stuck_devices_only_when_there_are_any_and_counts_the_moves(c):
    now = time.time()
    c.portwatch.events["p_e_sw1_12"] = [[now - 3600, "flap"]]
    sec, ctx = section(c, now)
    assert not any("stuck" in i["key"] for i in sec.build(ctx)["items"])                              # nothing clings: no line
    for k in range(12):
        book(c, now - 660 + 60 * k, -86)
    c.roamwatch.moves = [[now - 100, "MAC-AP1", "MAC-AP2"], [now - 3600 * 20, "MAC-AP2", "MAC-AP1"]]    # one in the night, one yesterday
    items = sec.build(ctx)["items"]
    stuck = next(i for i in items if i["key"] == "report.ports.stuck")
    assert stuck["vars"] == {"name": "Kitchen tablet", "ap": "Access point garden", "dbm": -86, "min": 11} and stuck["level"] == "info" and not stuck["point"]
    assert next(i for i in items if i["key"] == "report.ports.roam")["vars"] == {"n": 1}    # only the move inside the period


def test_a_section_with_only_wireless_data_is_available(c):
    sec, ctx = section(c, time.time())
    assert not sec.available(ctx)
    book(c, time.time(), -60)
    assert sec.available(ctx)
    c2 = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), Store(":memory:"), None)
    assert not sec.available(Ctx(core=c2, journal=None, since=0, until=1, prev=None))
    book(c2, T0, -60)
    assert sec.available(Ctx(core=c2, journal=None, since=0, until=1, prev=None))
