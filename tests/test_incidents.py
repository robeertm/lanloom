"""Rewind and incidents (S7/S8): episodes from the availability slots, bundling over the failure graph (root, members, a probable
root that is not seen down itself), the kinds (outage, flapping, blip, maintenance from a window, an own update or a person's
mark), "What happened?", the 90-day calendar, the chronicle, the nightly report section rendered in every language with a fixed
clock, the rewind encoding, the routes with their role rules and the audit entry, and the sample network."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import availability as av, chronicle as chronicle_mod, config as config_mod, failure, incidents, rewind
from lanloom.nightly.contrib import Ctx
from lanloom.nightly.journal import Journal
from lanloom.nightly.render import Renderer
from lanloom.store import Store
from lanloom.textfmt import languages

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_795_000_200.0                     # a fixed clock, on a 5-minute slot edge
SLOT = av.SLOT_S
HDR = {"X-Lanloom": "1"}


# ------------------------------------------------------------------ pure parts
def test_runs_merge_short_gaps_and_count_them():
    up, down = av.pack(5, 0), av.pack(0, 5)
    cells = bytearray([up, down, up, down, up, up, down, up, up, up, down, down])
    runs = incidents.runs_of(cells)
    assert runs == [[1, 7, 3], [10, 12, 1]]          # gaps of one and two good slots merge, three are apart
    eps = incidents.episodes("dev:x", cells, 100, (100 + len(cells)) * SLOT)
    assert eps[0]["runs"] == 3 and not eps[0]["open"] and eps[0]["to"] == (100 + 7) * SLOT
    assert eps[1]["open"] and eps[1]["to"] is None   # the last slot is the current one: still going


def test_unknown_slots_are_never_down():
    cells = bytearray([av.pack(5, 0), 0, 0, 0, av.pack(5, 0)])
    assert incidents.runs_of(cells) == []


def test_kinds():
    assert incidents.kind_of(1, 300, False, False) == "blip"
    assert incidents.kind_of(1, 300, True, False) == "outage"          # an open one is never a blip yet
    assert incidents.kind_of(3, 1500, False, False) == "flapping"
    assert incidents.kind_of(1, 3600, False, False) == "outage"
    assert incidents.kind_of(3, 1500, False, True) == "maintenance"


def _ep(node, start, runs=1, end=None):
    return {"subject": "dev:" + node, "node": node, "from": float(start), "to": None if end is None else float(end), "open": end is None, "runs": runs}


def _graph():
    g = failure.Graph()
    for n, kind in (("sw", "switch"), ("ap", "ap"), ("cam", "camera"), ("cam2", "camera"), ("tv", "tv"), ("ph", "phone"), ("gw", "router")):
        g.node(n, kind="device", title=n.upper(), tiles=[n], dev_kind=kind)
    for a, b in (("sw", "gw"), ("ap", "sw"), ("cam", "ap"), ("cam2", "ap"), ("tv", "sw"), ("ph", "ap")):
        g.edge(a, b, "port", "sure")
    g.index()
    return g


def _walkers(g):
    def walk(direction):
        def f(n):
            found = failure.Failure.search(g, [n], direction) if n in g.nodes else {}
            return {k: (v[0], v[1], v[2], {"key": g.edges[v[3]][2], "cert": g.edges[v[3]][3], "vars": g.edges[v[3]][4]}) for k, v in found.items()}
        return f
    can = lambda n: g.nodes[n]["dev_kind"] not in ("phone", "computer")     # noqa: E731
    return walk("down"), walk("up"), can


def test_bundle_root_members_and_a_member_that_was_down_before():
    g = _graph()
    down, up, can = _walkers(g)
    grp = [_ep("sw", 1000), _ep("ap", 1000), _ep("cam", 1060), _ep("ph", 1000), _ep("tv", 600)]
    out = incidents.bundle(grp, down, up, can)
    sw = next(b for b in out if b["root"] == "sw")
    assert {m["node"] for m in sw["members"]} == {"ap", "cam", "ph"}
    assert all(m["cert"] == "sure" for m in sw["members"]) and sw["root_cert"] == "sure"
    assert any(b["root"] == "tv" for b in out)                 # it went down five minutes before the switch: not taken down by it


def test_bundle_names_the_shared_upstream_and_phones_never_start_one():
    g = _graph()
    down, up, can = _walkers(g)
    out = incidents.bundle([_ep("cam", 1000), _ep("cam2", 1030)], down, up, can)
    assert len(out) == 1 and out[0]["root"] == "ap" and out[0]["root_cert"] == "likely" and not out[0]["observed"]
    assert out[0]["root_why"]["key"] == "shared" and {m["node"] for m in out[0]["members"]} == {"cam", "cam2"}
    assert incidents.bundle([_ep("ph", 1000)], down, up, can) == []


def test_rewind_levels_round_trip_within_twenty_percent():
    for r in (1, 7, 300, 12_345, 2_000_000, 900_000_000):
        back = rewind.decode(rewind.encode([r]))[0]
        assert abs(back - r) / r < 0.2, (r, back)
    assert rewind.encode([0, -1]) == "00" and rewind.decode("0") == [0.0]


# ------------------------------------------------------------------ the store with a fixed clock
class _Fail:
    def __init__(self, g):
        self.g = g

    def graph(self):
        return self.g

    search = staticmethod(failure.Failure.search)

    def resolve(self, g, ident):
        return ident if ident in g.nodes else None


class _Notify:
    def __init__(self):
        self.windows = []

    def maintenance_at(self, t, device=None):
        for w in self.windows:
            if w["start"] <= t < w["end"] and (not w["devices"] or device in w["devices"]):
                return w
        return None


class _Reports:
    def __init__(self, store):
        self.journal = Journal(store, clock=lambda: NOW)


class _Core:
    def __init__(self):
        self.store = Store(":memory:")
        self.availability = av.Availability(self.store, clock=lambda: NOW)
        self.reports = _Reports(self.store)
        self.failure = _Fail(_graph())
        self.notify = _Notify()
        self.audit = None
        self.chronicle = None
        self.fw = {}
        for n, kind in (("sw", "switch"), ("ap", "ap"), ("cam", "camera"), ("cam2", "camera"), ("tv", "tv"), ("ph", "phone"), ("gw", "router")):
            self.store.execute("INSERT INTO devices(id, ip, mac, data, kind, confidence, title, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                               (n, "192.168.1.%d" % (len(n) * 7), None, "{}", kind, 1.0, n.upper() + " device", NOW - 86400 * 40, NOW))
        self.incidents = incidents.Incidents(self, clock=lambda: NOW)

    def devices(self):
        return [{"id": n, "title": n.upper() + " device", "kind": "x", "firmware": fw} for n, fw in self.fw.items()]


def _book(core, node, down_from, down_to, start=NOW - 6 * 3600):
    """One look a minute from ``start`` to now; down in [down_from, down_to) (seconds before NOW)."""
    t = start
    while t < NOW:
        core.availability.sample("dev:" + node, not (NOW - down_from <= t < NOW - down_to), t)
        t += 60


@pytest.fixture()
def core():
    return _Core()


def test_switch_outage_bundles_its_parts_and_the_file_tells_the_story(core):
    for n in ("gw", "sw", "ap", "cam", "cam2", "tv", "ph"):
        _book(core, n, 3 * 3600, 3 * 3600 - 20 * 60) if n not in ("gw",) else _book(core, n, 0, 0)
    core.reports.journal.add("dev_offline", {"id": "sw", "title": "SW device"}, NOW - 3 * 3600 + 30)
    core.reports.journal.add("dev_new", {"id": "new1", "title": "Strange box"}, NOW - 4 * 3600)
    d = core.incidents.list(1)
    assert len(d["items"]) == 1
    i = d["items"][0]
    assert i["kind"] == "outage" and i["root"]["id"] == "sw" and i["counts"]["members"] == 5
    assert i["start"] == NOW - 3 * 3600 + 30                  # the journal's exact time refines the slot edge
    f = core.incidents.get(i["id"])
    keys = [e["key"] for e in f["timeline"]]
    assert keys[0] == "inc.tl.root_down" and "inc.tl.root_back" in keys and keys.count("inc.tl.member_down") == 5
    assert f["heal"]["key"] == "inc.heal.self"
    assert [b["key"] for b in f["before"]] == ["inc.before.dev_new"]
    assert f["strip"]["subject"] == "dev:sw" and 0 < f["strip"]["share"] < 1
    w = core.incidents.what_happened("cam")
    assert w["incident"]["id"] == i["id"] and not w["is_root"] and w["member"]["cert"] == "sure"
    assert core.incidents.what_happened("gw")["calm"]


def test_flapping_blip_and_maintenance_window(core):
    _book(core, "gw", 0, 0)
    t = NOW - 6 * 3600
    while t < NOW:                      # tv: three drop-outs five minutes apart; cam: one short drop-out; sw: inside a window
        ago = NOW - t
        tv_down = any(a - 300 < ago <= a for a in (4 * 3600, 4 * 3600 - 600, 4 * 3600 - 1200))
        core.availability.sample("dev:tv", not tv_down, t)
        core.availability.sample("dev:cam", not (2 * 3600 - 300 < ago <= 2 * 3600), t)
        core.availability.sample("dev:sw", not (5 * 3600 - 600 < ago <= 5 * 3600), t)
        t += 60
    core.notify.windows.append({"id": "w1", "title": "Switch firmware", "start": NOW - 5.2 * 3600, "end": NOW - 4.5 * 3600, "devices": ["sw"]})
    kinds = {i["root"]["id"]: i for i in core.incidents.list(1)["items"]}
    assert kinds["tv"]["kind"] == "flapping" and kinds["tv"]["runs"] == 3
    assert kinds["cam"]["kind"] == "blip"
    assert kinds["sw"]["kind"] == "maintenance" and kinds["sw"]["why_kind"] == "window" and kinds["sw"]["window"]["name"] == "Switch firmware"


def test_own_update_is_maintenance_and_a_mark_changes_the_kind(core):
    _book(core, "gw", 0, 0)
    _book(core, "cam", 3600, 1800)
    core.reports.journal.add("app_stop", {"version": "1.0.0"}, NOW - 7200)
    core.reports.journal.add("app_start", {"version": "1.1.0", "prev": "1.0.0", "down_s": 40}, NOW - 7160)
    core.reports.journal.add("app_start", {"version": "1.1.0"}, NOW - 600)            # a plain restart is nothing
    items = core.incidents.list(1)["items"]
    up = [i for i in items if i["root"]["id"] == "lanloom"]
    assert len(up) == 1 and up[0]["kind"] == "maintenance" and up[0]["why_kind"] == "update" and up[0]["dur"] == 40
    cam = next(i for i in items if i["root"]["id"] == "cam")
    assert cam["kind"] == "outage"
    core.incidents.mark(cam["id"], "maintenance", "planned move")
    again = next(i for i in core.incidents.list(1)["items"] if i["root"]["id"] == "cam")
    assert again["kind"] == "maintenance" and again["why_kind"] == "marked" and again["mark"]["note"] == "planned move"
    core.incidents.mark(cam["id"], None, "")
    assert next(i for i in core.incidents.list(1)["items"] if i["root"]["id"] == "cam")["kind"] == "outage"
    with pytest.raises(KeyError):
        core.incidents.mark("nope", "maintenance")
    with pytest.raises(ValueError):
        core.incidents.mark(cam["id"], "outage")


def test_rows_outlive_the_slots_for_the_calendar(core):
    _book(core, "gw", 0, 0)
    old = NOW - 60 * 86400
    data = {"id": "iold", "start": old, "end": old + 900, "open": False, "dur": 900, "kind": "outage", "runs": 1,
            "root": {"id": "tv", "kind": "tv", "title": "TV", "subject": "dev:tv", "cert": "sure", "observed": True}, "members": [],
            "counts": {"members": 0, "sure": 0, "likely": 0}}
    core.store.execute('INSERT INTO incident(id, start, "end", kind, root, data) VALUES(?,?,?,?,?,?)', ("iold", old, old + 900, "outage", "tv", json.dumps(data)))
    core.incidents.collect(force=True)
    cal = core.incidents.calendar(tz=lambda ts: time.gmtime(ts))
    assert len(cal["days"]) == 90 and cal["max"] >= 1
    hit = [d for d in cal["days"] if d["faults"]]
    assert hit and hit[0]["down_s"] == 900
    hm = core.incidents.heatmap(tz=lambda ts: time.gmtime(ts))
    assert len(hm["down"]) == 7 and all(len(r) == 24 for r in hm["down"])


def test_chronicle_remembers_firmware_jumps_new_devices_and_long_outages(core):
    core.chronicle = ch = chronicle_mod.Chronicle(core, clock=lambda: NOW)
    core.fw = {"sw": "4.2.1"}
    assert ch.collect(NOW - 86400) == 0                       # the first collection only remembers
    core.fw = {"sw": "4.2.2"}
    core.reports.journal.add("dev_new", {"id": "n1", "title": "New box", "ip": "192.168.1.9", "kind": "iot"}, NOW - 3600)
    _book(core, "gw", 0, 0)
    _book(core, "tv", 5 * 3600, 3 * 3600)
    assert ch.collect(NOW) >= 3
    kinds = {r["kind"]: r for r in ch.view(30)["items"]}
    assert kinds["firmware"]["data"]["from"] == "4.2.1" and kinds["firmware"]["data"]["to"] == "4.2.2"
    assert kinds["new_device"]["subject"] == "n1"
    assert kinds["long_outage"]["data"]["dur"] >= 2 * 3600
    assert ch.collect(NOW) == 0                               # idempotent
    before = core.incidents.before(NOW + 600, "sw")
    assert any(b["key"] == "inc.before.firmware" and b["near"] for b in before)


@pytest.mark.parametrize("lang", sorted(languages()))
def test_nightly_section_in_every_language_with_a_fixed_clock(core, lang):
    from lanloom import incidents_report
    _book(core, "gw", 0, 0)
    for n in ("sw", "ap", "cam"):
        _book(core, n, 3 * 3600, 3 * 3600 - 40 * 60)
    _book(core, "tv", 3600, 0)                                 # still down when the report is made
    core.reports.journal.add("app_stop", {"version": "1.0.0"}, NOW - 5 * 3600)
    core.reports.journal.add("app_start", {"version": "1.1.0", "prev": "1.0.0", "down_s": 30}, NOW - 5 * 3600 + 30)
    ctx = Ctx(core=core, journal=core.reports.journal, since=NOW - 8 * 3600, until=NOW, prev=None)
    assert incidents_report._available(ctx)
    sec = incidents_report._build(ctx)
    keys = {i["key"]: i for i in sec["items"]}
    assert keys["inc.report.outage_n"]["point"] and keys["inc.report.outage_n"]["vars"]["n"] == 2
    assert keys["inc.report.open"]["point"] and keys["inc.report.update"]["point"] is False
    assert sec["summary"]["key"] == "inc.report.summary" and sec["summary"]["vars"] == {"n": 2, "m": 1}
    from datetime import timezone
    r = Renderer(lang, timezone.utc, time.strftime("%Y-%m-%d", time.gmtime(NOW)))
    texts = [r.line(sec["summary"])] + [r.line(i) for i in sec["items"]] + [r.tr(sec["title"])]
    for text in texts:
        assert "{" not in text and "inc." not in text, text
    assert any("SW device" in x for x in texts)
    empty = Ctx(core=_Core(), journal=core.reports.journal, since=NOW - 3600, until=NOW, prev=None)
    assert not incidents_report._available(empty)               # without slots the section is left out


# ------------------------------------------------------------------ "Rewind to <time>" in the page (Node)
def test_rewind_understands_typed_times():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    now = 1_795_000_000           # Wednesday 2026-11-18 11:06:40 UTC (the test runs in UTC)
    cases = ["14:30", "7:00", "yesterday 9:15", "gestern 9:15", "mon 8:00", "Mo 8:00", "3 h", "90 min", "2:30 pm", "25:00", "nonsense", "9"]
    proc = subprocess.run([node, str(ROOT / "tests" / "js" / "rewind_time_test.js"), json.dumps(cases), str(now)], capture_output=True, text=True,
                          timeout=60, env=dict(os.environ, TZ="UTC"))
    assert proc.returncode == 0, proc.stderr
    got = dict(zip(cases, json.loads(proc.stdout)))
    day = now - now % 86400
    assert got["14:30"] == day - 86400 + 14.5 * 3600            # later than now: yesterday
    assert got["7:00"] == day + 7 * 3600
    assert got["yesterday 9:15"] == got["gestern 9:15"] == day - 86400 + 9.25 * 3600
    assert got["mon 8:00"] == got["Mo 8:00"] == day - 2 * 86400 + 8 * 3600
    assert got["3 h"] == now - 3 * 3600 and got["90 min"] == now - 5400
    assert got["2:30 pm"] == day - 86400 + 14.5 * 3600
    assert got["25:00"] is None and got["nonsense"] is None and got["9"] is None


# ------------------------------------------------------------------ the sample network and the routes
GARDEN_AP, SWITCH = "d-024c4c000004", "d-024c4c000002"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("LANLOOM_DATA", str(tmp_path_factory.mktemp("incidents")))
    mp.setenv("LANLOOM_DEMO", "1")
    mp.setenv("LANLOOM_DEMO_SEED", "1")
    mp.setenv("LANLOOM_UPDATE_CHECK", "0")
    from lanloom import auth
    from lanloom.main import create_app
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        ll = app.state.ll
        users = {role: ll.accounts.create(role + "1", "a-long-test-password", role=role) for role in ("viewer", "operator")}
        cookies = {role: ll.sessions.create(uid, ip="127.0.0.1", agent="test") for role, uid in users.items()}

        def as_(role):
            c.cookies.set(auth.SESSION_COOKIE, cookies[role])
            return c
        deadline = time.time() + 40
        while time.time() < deadline:           # the topology of the demo arrives with its first controller readings
            r = ll.core.failure.chain(SWITCH, "down")
            if ll.core.availability.has_data() and r and r["counts"]["devices"] >= 10:
                break
            time.sleep(0.5)
        ll.core.store.set_setting(incidents.STATE, {})
        ll.core.incidents.collect(force=True)
        yield app, as_
    mp.undo()


def test_sample_network_has_bundled_incidents_and_a_calendar(demo):
    _app, as_ = demo
    c = as_("viewer")
    d = c.get("/api/incidents?days=7").json()
    by = {}
    for i in d["items"]:
        by.setdefault(i["root"]["id"], []).append(i)
    garden = next(i for i in by[GARDEN_AP] if i["open"])
    assert garden["kind"] == "outage" and garden["counts"]["members"] >= 3
    big = max(by[SWITCH], key=lambda i: i["counts"]["members"])
    assert big["counts"]["members"] >= 10 and big["root"]["cert"] == "sure"
    assert any(i["kind"] == "maintenance" and i.get("why_kind") == "window" for i in by[SWITCH])
    assert any(i["kind"] == "flapping" for i in d["items"])
    assert any(i["root"]["observed"] is False and i["root"]["cert"] == "likely" for i in d["items"])
    f = c.get("/api/incidents/" + big["id"]).json()
    assert any(b["key"] == "inc.before.firmware" for b in f["before"])
    cal = c.get("/api/incidents/calendar").json()
    assert len(cal["days"]) == 90 and sum(x["faults"] for x in cal["days"][:40]) > 5          # older than the slots
    assert c.get("/api/incidents/what/" + GARDEN_AP).json()["incident"]["id"] == garden["id"]
    assert c.get("/api/incidents/nope").status_code == 404
    s = c.get("/api/overview").json()
    assert s["incidents"]["open"] >= 1 and any(n["id"].startswith("inc:") for n in s["notes"])


def test_rewind_answer_holds_traffic_states_and_marks(demo):
    _app, as_ = demo
    c = as_("viewer")
    for rng, step in (("24h", 60), ("7d", 300)):
        d = c.get("/api/rewind?range=%s&view=network" % rng).json()
        assert d["step"] == step and d["n"] == {"24h": 1440, "7d": 2016}[rng]
        assert d["traffic"] and all(len(a) == d["n"] and len(b) == d["n"] for a, b in d["traffic"].values())
        assert GARDEN_AP in d["states"] and d["states"][GARDEN_AP][-2] in "dp"
        assert d["marks"] and len(d["totals"]) == d["n"]
    assert c.get("/api/rewind?range=1y").status_code == 400
    for path in ("/api/incidents/heatmap", "/api/incidents/events?hours=24", "/api/incidents/night", "/api/incidents/chronicle"):
        assert c.get(path).status_code == 200, path


def test_marking_is_an_operators_change_with_an_audit_entry(demo):
    app, as_ = demo
    c = as_("viewer")
    iid = c.get("/api/incidents?days=7").json()["items"][-1]["id"]
    assert c.put("/api/incidents/" + iid, json={"kind": "maintenance"}, headers=HDR).status_code == 403
    c = as_("operator")
    r = c.put("/api/incidents/" + iid, json={"kind": "maintenance", "note": "planned"}, headers=HDR)
    assert r.status_code == 200 and r.json()["mark"]["kind"] == "maintenance"
    assert c.put("/api/incidents/" + iid, json={"kind": "bogus"}, headers=HDR).status_code == 400
    entries = [e for e in app.state.ll.audit.query({"action": "incident.mark"}) if e["outcome"] == "ok"]
    assert entries and entries[0]["target"] == iid and entries[0]["detail"] == {"kind": "maintenance", "note": True}
    assert c.put("/api/incidents/" + iid, json={"kind": None}, headers=HDR).status_code == 200
