"""Availability over time (S2): 5-minute slots, incidents, repair time, wobbly candidates; traffic per minute for the plots."""
import time

import pytest
from fastapi.testclient import TestClient

from lanloom import auth, availability as av, config as config_mod, trafficlog as tl
from lanloom.main import create_app
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}
T0 = 1_800_000_000 // av.DAY_S * av.DAY_S + 12 * 3600          # a fixed noon (UTC)
UP, DOWN, NONE = av.pack(5, 0), av.pack(0, 5), 0


def cells(spec: str) -> bytearray:
    return bytearray({"u": UP, "d": DOWN, "p": av.pack(2, 3), ".": NONE}[ch] for ch in spec)


# ------------------------------------------------------------------ pure reading
def test_slot_states_and_share():
    assert [av.slot_state(b) for b in (NONE, UP, DOWN, av.pack(1, 1))] == ["unknown", "up", "down", "partial"]
    assert av.share_of(cells("....")) is None                       # nothing known: no number, never 100 %
    assert av.share_of(cells("uuud")) == 0.75
    assert av.share_of(cells("u.d.")) == 0.5                         # unknown slots do not count at all
    assert av.pack(99, 99) == 0xFF                                   # capped at 15 looks per nibble


def test_incidents_merge_flapping_but_not_across_unknown():
    now = T0
    start = int(now // av.SLOT_S) - 19
    inc = av.incidents(cells("uuddduuuduuuuudd..uu")[:20], start, now)
    # dd d | uuu | d  -> two incidents; the late d,d run then unknown
    assert [round(i["dur"] / av.SLOT_S) for i in inc] == [3, 1, 2]
    flap = av.incidents(cells("uududduuuuuuuuuuuuuu"), start, now)   # d u dd: one good slot between = one flapping incident
    assert len(flap) == 1 and flap[0]["dur"] == 4 * av.SLOT_S
    gap = av.incidents(cells("uud.dduuuuuuuuuuuuuu"), start, now)     # unknown between: two incidents
    assert len(gap) == 2


def test_open_incident_and_mttr():
    now = T0 + 100
    cur = int(now // av.SLOT_S)
    inc = av.incidents(cells("uuddduuuduudd"), cur - 12, now)
    assert inc[-1]["open"] and inc[-1]["to"] is None
    assert av.mttr(inc) == int((3 * 300 + 1 * 300) / 2)             # only the incidents that ended count
    assert av.mttr(av.incidents(cells("uuuu"), cur - 3, now)) is None


# ------------------------------------------------------------------ store
@pytest.fixture()
def avail():
    store = Store(":memory:")
    clock = {"t": T0}
    a = av.Availability(store, clock=lambda: clock["t"])
    a.clk = clock
    return a


def test_sample_roundtrip_and_flush(avail):
    for k in range(5):
        avail.sample("dev:a", True, T0 + k * 60)
    avail.sample("dev:a", False, T0 + 5 * 60)
    slot = int(T0 // av.SLOT_S)
    c = avail.cells("dev:a", slot, slot + 1)
    assert av.slot_state(c[0]) == "up" and av.ups_of(c[0]) == 5 and av.slot_state(c[1]) == "down"
    avail.flush(T0 + 1000)
    avail.cur.clear()                                                # read back from the database
    again = avail.cells("dev:a", slot, slot + 1)
    assert bytes(again) == bytes(c)
    assert avail.cells("dev:none", slot, slot + 1) == bytearray(2)    # no data: unknown, not up


def test_retention_prunes_old_days(avail):
    avail.keep_days = 3
    avail.sample("dev:old", True, T0 - 10 * av.DAY_S)
    avail.sample("dev:new", True, T0)
    avail.flush(T0)
    assert avail.subjects() == ["dev:new"]


def test_detail_and_stability_ranking(avail):
    now = T0
    cur = int(now // av.SLOT_S)
    names = {"dev:ok": {"title": "Steady"}, "dev:wob": {"title": "Wobbly"}, "dev:down": {"title": "Down now"}, "dev:none": {"title": "No data"}}
    for slot in range(cur - 3 * av.PER_DAY, cur):
        for sid in ("dev:ok", "dev:wob", "dev:down"):
            day, idx = divmod(slot, av.PER_DAY)
            cl = avail._day_cells(sid, day)
            bad = (sid == "dev:wob" and slot % 150 in (0, 1)) or (sid == "dev:down" and slot >= cur - 4)
            cl[idx] = DOWN if bad else UP
    avail.dirty.update(k for k in avail.cur)
    stab = avail.stability(names, now)
    ids = [r["id"] for r in stab["rows"]]
    assert ids == ["dev:down", "dev:wob", "dev:ok"]                  # open first, then wobbly, then calm; no data is not listed
    wob = stab["rows"][1]
    assert wob["wobbly"] and wob["incidents7"] >= av.WOBBLY_INCIDENTS and wob["mttr_s"] == 2 * av.SLOT_S
    assert stab["rows"][0]["open"] and not stab["rows"][2]["incidents7"] and stab["rows"][2]["share35"] == 1.0
    assert stab["summary"]["wobbly"] == 1 and stab["summary"]["open"] == 1 and stab["summary"]["subjects"] == 3
    det = avail.detail("dev:wob", now)
    assert len(det["bars"]) == 35 and det["bars"][-1] is not None and det["bars"][0] is None and len(det["slots24"]) == av.PER_DAY
    assert det["list"][0]["from"] > det["list"][-1]["from"]          # newest first


def test_forget(avail):
    avail.sample("dev:x", True, T0)
    avail.flush(T0)
    avail.forget("dev:x")
    assert avail.subjects() == [] and not avail.has_data()


# ------------------------------------------------------------------ watcher guard
class _Dev:
    pass


def test_watch_books_looks_but_not_when_nothing_answers(avail):
    from lanloom.nightly.journal import Watch
    w = Watch.__new__(Watch)
    w.core = type("C", (), {"availability": avail})()
    w._book_looks([("a", True), ("b", True), ("c", False)], T0)
    slot = int(T0 // av.SLOT_S)
    assert av.slot_state(avail.cells("dev:c", slot, slot)[0]) == "down" and av.slot_state(avail.cells("dev:a", slot, slot)[0]) == "up"
    w._book_looks([("d", False), ("e", False), ("f", False)], T0)       # nobody answers: our own network, not three faults
    assert avail.cells("dev:d", slot, slot)[0] == 0
    w._book_looks([("g", False)], T0)                                   # one device alone can really be down
    assert av.slot_state(avail.cells("dev:g", slot, slot)[0]) == "down"


# ------------------------------------------------------------------ traffic log
@pytest.fixture()
def log():
    store = Store(":memory:")
    return tl.TrafficLog(store, clock=lambda: T0)


def test_traffic_ranges_and_flip(log):
    for k in range(120):                                   # two hours of 600 B/s sent, 6000 B/s received
        log.book("t1", 600 * 60, 6000 * 60, T0 - k * 60)
    one = log.series([("t1", False)], "1h", T0)
    assert len(one["points"]) == 60 and one["points"][-1] == [600, 6000] and one["step"] == 60
    flipped = log.series([("t1", True)], "1h", T0)
    assert flipped["points"][-1] == [6000, 600]
    six = log.series([("t1", False)], "6h", T0)
    assert len(six["points"]) == 360 and six["points"][0] == [0, 0] and six["points"][-1] == [600, 6000]
    day = log.series([("t1", False)], "24h", T0)
    assert len(day["points"]) == 288 and day["step"] == 300 and day["peak"] == [600, 6000]
    week = log.series([("t1", False)], "7d", T0)
    assert len(week["points"]) == 336 and week["step"] == 1800
    assert week["total"][0] == 600 * 60 * 120


def test_traffic_sums_traces_and_survives_flush(log):
    log.book("a", 6000, 0, T0)
    log.book("b", 0, 12000, T0)
    log.flush(T0 + 400)
    log.cur.clear(); log.old.clear()
    both = log.series([("a", False), ("b", False)], "1h", T0)
    assert both["points"][-1] == [100, 200]
    log.book("a", 6000, 0, T0 + 1)                          # the day that was written is read back before it is added to
    assert log.series([("a", False)], "1h", T0 + 1)["points"][-1][0] == 200


def test_traffic_crosses_midnight_and_prunes(log):
    mid = T0 // tl.DAY_S * tl.DAY_S + tl.DAY_S
    log.book("t", 60_000, 0, mid - 30)
    log.book("t", 120_000, 0, mid + 30)
    got = log.series([("t", False)], "1h", mid + 600)
    assert [p[0] for p in got["points"] if p[0]] == [1000, 2000]
    log.flush(mid + 600)
    log.book("t", 1, 0, mid - 20 * tl.DAY_S)
    log.flush(mid + 600)
    assert log.store.one("SELECT COUNT(*) AS n FROM traffic_min WHERE day < ?", (mid // tl.DAY_S - tl.KEEP_DAYS,))["n"] == 0


# ------------------------------------------------------------------ routes (demo)
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        uid = app.state.ll.accounts.create("viewer1", "a-long-test-password", role="viewer")
        c.cookies.set(auth.SESSION_COOKIE, app.state.ll.sessions.create(uid, ip="127.0.0.1", agent="test"))
        deadline = time.time() + 15
        while time.time() < deadline and not c.get("/api/stability").json().get("summary", {}).get("subjects"):
            time.sleep(0.2)
        yield c


def test_routes_demo_history(client):
    stab = client.get("/api/stability").json()
    assert stab["summary"]["subjects"] > 20 and stab["summary"]["wobbly"] >= 1 and stab["summary"]["mttr_s"]
    top = stab["rows"][0]
    assert len(top["bars"]) == 35
    det = client.get("/api/availability/" + top["id"]).json()
    assert det["title"] == top["title"] and len(det["slots24"]) == 288 and det["list"]
    assert client.get("/api/availability/dev:nope").status_code == 404
    comp = client.get("/api/map?view=overview").json()["components"]
    host = next(c["id"] for c in comp if c.get("kind") in ("server", "nas", "router", "host", "device") and not str(c["id"]).startswith(("zone", "area", "topic")))
    plot = client.get("/api/traffic/%s?range=24h" % host)
    assert plot.status_code in (200, 404)
    if plot.status_code == 200:
        assert len(plot.json()["points"]) == 288
    assert client.get("/api/traffic/x?range=2y").status_code == 400
