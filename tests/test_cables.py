"""Real cables (DW3): the OpenStreetMap cable file (ODbL), its build checks, the estimate over the cable graph and the API.

Places in the graph tests are invented points on an empty sea (no real coast needed); the cable data of those tests is
built here and never touches the bundled file. The API tests run in the sample network (no measurement, no download)."""
import gzip
import hashlib
import importlib.util
import json
import re
import socket
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import cableroute, cables, demo_routes
from lanloom import config as config_mod
from lanloom.main import create_app

ROOT = Path(__file__).resolve().parent.parent
HDR = {"X-Lanloom": "1"}
PW = "correct horse battery 42"
SPEC = importlib.util.spec_from_file_location("lanloom_build_cables", ROOT / "scripts" / "build_cables.py")
build_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_script)


# ---------------------------------------------------------------------------------------------------------- helpers
def way(wid, pts, name=None, sea=True, **tags):
    t = dict(tags)
    if sea:
        t.setdefault("communication", "line")
        t.setdefault("location", "submarine")
    else:
        t.setdefault("communication", "line")
    if name:
        t["name"] = name
    return {"type": "way", "id": wid, "tags": t, "geometry": [{"lon": lon, "lat": lat} for lon, lat in pts]}


def answer(*ways):
    return json.dumps({"version": 0.6, "elements": list(ways)}).encode()


def line(lon0, lat0, lon1, lat1, n=12):
    return [(lon0 + (lon1 - lon0) * i / n, lat0 + (lat1 - lat0) * i / n) for i in range(n + 1)]


def place(name, cc, lat, lon, rtt=None, sure=True):
    return {"kind": "transit", "cc": cc, "rtt": rtt, "place": {"name": name, "cc": cc, "lat": lat, "lon": lon, "source": "name", "sure": sure}}


def dataset(*ways, date="2026-11-19"):
    d = cables.build(cables.ways_from_answer(answer(*ways)), date)
    assert not cables.problems(d, cables.MIN_SEA_IMPORT), cables.problems(d, cables.MIN_SEA_IMPORT)
    return d


# invented places on both sides of an open sea (the "Western" and "Eastern" shores), countries from the test ranges
WEST = ("Westport", "XA", 10.0, -40.0)
EAST = ("Eastport", "XB", 10.0, -10.0)          # ~3280 km away: a sea crossing
INLAND = ("Midtown", "XB", 10.0, -9.0)


# ---------------------------------------------------------------------------------------------------------- the bundled file
def test_bundled_file_carries_the_odbl_header_and_passes_its_checks():
    blob = cables.DATA_FILE.read_bytes()
    d = cables.decode(blob)
    assert d is not None
    assert cables.problems(d) == []
    assert d["license"] == "ODbL 1.0" and d["attribution"] == "© OpenStreetMap contributors"
    assert "Open Database License" in d["notice"] and "Incomplete" in d["notice"]
    assert d["attribution_url"] == "https://www.openstreetmap.org/copyright"
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", d["date"])
    assert d["changes"] and len(d["queries"]) == len(cables.QUERIES)
    assert d["counts"]["sea"] == len(d["sea"]) >= cables.MIN_SEA
    assert build_script.samples(d) == []


def test_bundled_file_matches_its_checksum_and_size_budget():
    blob = cables.DATA_FILE.read_bytes()
    digest, name = (cables.DATA_FILE.parent / "cables-osm.sha256").read_text(encoding="ascii").split()
    assert name == cables.DATA_FILE.name and hashlib.sha256(blob).hexdigest() == digest, "edited by hand? rebuild with scripts/build_cables.py"
    assert len(blob) <= build_script.MAX_GZ_BYTES
    assert len(gzip.decompress(blob)) <= build_script.MAX_RAW_BYTES


def test_licence_file_and_third_party_notices_name_osm_and_odbl():
    lic = (ROOT / "lanloom" / "data" / "CABLES-LICENSE.md").read_text(encoding="utf-8")
    for want in ("OpenStreetMap contributors", "Open Database License", "Derivative Database", "Collective Database",
                 "Produced Work", "incomplete, community-mapped", "build_cables.py"):
        assert want in lic, want
    notes = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    row = next(l for l in notes.splitlines() if "cables-osm.json.gz" in l)
    assert "OpenStreetMap" in row and "ODbL" in row and "opendatacommons.org/licenses/odbl" in row
    assert "lanloom/data/CABLES-LICENSE.md" in (ROOT / ".dockerignore").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "OpenStreetMap contributors" in readme and "ODbL" in readme


def test_no_cable_data_from_a_subscriber_only_source_in_the_repository():
    """Only OpenStreetMap in the ODbL file (every entry has the fields of the OSM build) and only the open sources (public domain /
    CC0, tests/test_cables_open.py) in the second file; no file name points at another source."""
    d = cables.decode(cables.DATA_FILE.read_bytes())
    for e in d["sea"] + d["land"]:
        assert set(e) == {"id", "n", "op", "d", "km", "ends"}
        assert re.match(r"^(w\d+|[sl]\d+)$", e["id"])
    names = [p.name.lower() for p in (ROOT / "lanloom").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert not [n for n in names if "cable" in n and n not in ("cables.py", "cableroute.py", "cables-osm.json.gz", "cables-osm.sha256",
                                                               "cables_open.py", "cables-open.json.gz", "cables-open.sha256",
                                                               "cables-license.md", "paths_cables.js", "paths_cables.css")]


# ---------------------------------------------------------------------------------------------------------- building and checking
def test_build_bundles_by_name_and_drops_power_disused_and_short_pieces():
    ws = cables.ways_from_answer(answer(
        way(1, line(-40, 10, -25, 10), "Example Link"), way(2, line(-25, 10, -10, 10), "Example Link"),
        way(3, line(-40, 20, -10, 20), "Power Sea Link", power="cable"),
        way(4, line(-40, 30, -10, 30), "Old Link", **{"disused:communication": "line"}),
        way(5, line(-40, 31, -10, 31), "TAT-8"),
        way(6, line(-40, 32, -39.99, 32)),                                   # 1 km, unnamed
        way(7, line(-40, 40, -39.8, 40), "Short Land", sea=False),            # 17 km land: too short for a backbone
        way(8, line(0, 50, 5, 50), "Backbone", sea=False),                    # ~360 km land
        {"type": "node", "id": 9}, {"type": "way", "id": 10, "geometry": "x"}))
    sea, land = cables.bundle(ws)
    assert [e["n"] for e in sea] == ["Example Link"] and sea[0]["id"] == "w1"
    assert 3200 < sea[0]["km"] < 3400
    assert sorted(sea[0]["ends"]) == [[-40.0, 10.0], [-10.0, 10.0]]
    assert [e["n"] for e in land] == ["Backbone"]


def test_broken_answers_give_nothing_and_never_raise():
    for raw in (b"", b"{", b"[]", b'{"elements": 5}', "not json", b"\xff\xfe", {"elements": [None, 1, "x"]}):
        assert cables.ways_from_answer(raw) == []


def test_problems_catch_broken_geometry_missing_licence_and_too_few_cables():
    d = dataset(way(1, line(-40, 10, -10, 10), "A"), way(2, line(-40, 12, -10, 12), "B"))
    assert cables.problems(d) == ["too few sea cables (2 < %d)" % cables.MIN_SEA]
    broken = json.loads(json.dumps(d))
    broken["sea"][0]["d"] = "M1 2 Z<script>"
    assert "sea[0] geometry" in cables.problems(broken, 1)
    broken = json.loads(json.dumps(d))
    broken["sea"][1]["ends"] = [[200, 0], [0, 0]]
    assert "sea[1] ends" in cables.problems(broken, 1)
    broken = json.loads(json.dumps(d))
    del broken["notice"]
    assert "license header" in cables.problems(broken, 1)
    broken = json.loads(json.dumps(d))
    broken["sea"][1]["id"] = broken["sea"][0]["id"]
    assert "sea[1] id" in cables.problems(broken, 1)
    assert cables.problems({"format": "other"}) == ["format"]


def test_build_script_refuses_a_thin_answer_and_writes_nothing(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "sea.json").write_bytes(answer(way(1, line(-40, 10, -10, 10), "A")))
    (raw / "seamark.json").write_bytes(answer(way(2, line(-40, 12, -10, 12), "B")))
    (raw / "land.json").write_bytes(answer())
    out = tmp_path / "cables-osm.json.gz"
    assert build_script.main(["--step", "osm", "--from-raw", str(raw), "--out", str(out)]) == 1
    assert not out.exists() and not (tmp_path / "cables-osm.sha256").exists()


def test_build_script_writes_atomically_with_a_checksum(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "sea.json").write_bytes(answer(way(1, line(-40, 10, -10, 10), "A")))
    (raw / "seamark.json").write_bytes(answer(way(2, line(-40, 12, -10, 12), "B")))
    (raw / "land.json").write_bytes(answer())
    monkeypatch.setattr(cables, "MIN_SEA", 1)
    monkeypatch.setattr(build_script, "samples", lambda d: [])
    out = tmp_path / "cables-osm.json.gz"
    assert build_script.main(["--step", "osm", "--from-raw", str(raw), "--out", str(out), "--date", "2026-01-02"]) == 0
    blob = out.read_bytes()
    assert (tmp_path / "cables-osm.sha256").read_text().split()[0] == hashlib.sha256(blob).hexdigest()
    assert cables.decode(blob)["date"] == "2026-01-02"
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------------------------------------- the estimate
def _route(*pls):
    return [{"kind": "home", "cc": "XA", "place": None, "rtt": 1}] + [place(*p[:4], rtt=p[4] if len(p) > 4 else None) for p in pls]


def test_one_fitting_cable_is_likely_and_the_path_follows_it():
    d = dataset(way(1, line(-40.5, 10, -10.5, 10), "Example Link"), way(2, line(-40, 40, -10, 40), "Far Link"))
    secs = _route(WEST + (20,), EAST + (60,))
    est = cableroute.estimate(secs, d)
    assert len(est) == 1
    e = est[0]
    assert e["name"] == "Example Link" and e["id"] == "w1" and e["confidence"] == "likely" and e["sure"] is False
    assert e["reason"]["basis"] == "latency" and e["reason"]["rtt_diff_ms"] == 40.0 and e["reason"]["candidates"] == 1
    assert e["from"] == "Westport" and e["to"] == "Eastport"
    p = cableroute.path(secs, est)
    assert [x["kind"] for x in p] == ["air", "cable", "air"] and p[1]["id"] == "w1"
    assert cableroute.crossings(secs) == [{"step": 0, "from": "Westport", "to": "Eastport"}]


def test_two_similar_cables_are_only_possible_and_the_second_is_the_alternative():
    d = dataset(way(1, line(-40.5, 10, -10.5, 10), "Alpha"), way(2, line(-40.5, 10.5, -10.5, 10.5), "Beta"))
    est = cableroute.estimate(_route(WEST + (20,), EAST + (60,)), d)
    assert [e["name"] for e in est] == ["Alpha", "Beta"]
    assert [e["confidence"] for e in est] == ["possible", "possible"] and [e["alternative"] for e in est] == [False, True]


def test_a_route_too_fast_for_the_cable_rules_it_out():
    d = dataset(way(1, line(-40.5, 10, -10.5, 10), "Example Link"))
    assert cableroute.estimate(_route(WEST + (20,), EAST + (25,)), d) == []           # 5 ms cannot cover 3300 km of glass


def test_contradicting_latency_is_named_and_never_likely():
    d = dataset(way(1, line(-40.5, 10, -10.5, 10), "Example Link"))
    est = cableroute.estimate(_route(WEST + (60,), EAST + (20,)), d)
    assert est and est[0]["reason"]["basis"] == "latency_contradicts" and est[0]["confidence"] == "possible"
    est = cableroute.estimate(_route(WEST, EAST), d)
    assert est and est[0]["reason"]["basis"] == "no_latency" and est[0]["confidence"] == "possible"


def test_no_basis_gives_no_estimate_but_a_crossing():
    d = dataset(way(1, line(-40, 40, -10, 40), "Far Link"))
    secs = _route(WEST + (20,), EAST + (60,))
    assert cableroute.estimate(secs, d) == []
    assert cableroute.crossings(secs) == [{"step": 0, "from": "Westport", "to": "Eastport"}]
    assert [x["kind"] for x in cableroute.path(secs, [])] == ["air"]


def test_same_country_is_never_a_sea_crossing_and_an_island_is():
    d = dataset(way(1, line(-40.5, 10, -10.5, 10), "Example Link"))
    same = _route(("Westport", "XA", 10.0, -40.0, 20), ("Eastport", "XA", 10.0, -10.0, 60))
    assert cableroute.crossings(same) == [] and cableroute.estimate(same, d) == []
    near = (52.0, 4.0, "A", 1.0, "NL"), (51.5, 0.0, "B", 2.0, "GB")
    assert cableroute.sea_crossing(*near) is True
    assert cableroute.sea_crossing((52.0, 4.0, "A", 1.0, "NL"), (50.0, 8.0, "B", 2.0, "DE")) is False


def test_branches_and_gaps_follow_the_cable_not_the_system_length():
    # a trunk with a long branch to the north, and a small gap in the data in the middle of the trunk
    d = dataset(way(1, line(-40.5, 10, -26, 10), "Branchy"), way(2, line(-25.8, 10, -10.5, 10), "Branchy"),
                way(3, line(-26, 10, -26, 40), "Branchy"))
    est = cableroute.estimate(_route(WEST + (20,), EAST + (60,)), d)
    assert est and est[0]["name"] == "Branchy"
    assert est[0]["cable_km"] < 3600 < est[0]["km"]              # along the trunk, not the whole system with its branch


def test_route_places_skip_unknown_places_and_repeats():
    secs = [{"kind": "home", "cc": "XA", "place": None, "rtt": 1}, place("A", "XA", 1, 2, 5), place("A", "XA", 1, 2, 6),
            {"kind": "transit", "place": {"lat": "x"}}, place("B", "XB", 3, 4, None), {"kind": "target", "place": {"lat": 95, "lon": 0}}]
    assert cableroute.route_places(secs) == [(1.0, 2.0, "A", 5.0, "XA"), (3.0, 4.0, "B", None, "XB")]


def test_read_path_reads_relative_and_absolute_commands():
    assert cableroute.read_path("M1 2l1 1 1-1M10 10L11 11H12V13") == [[(1, 2), (2, 3), (3, 2)], [(10, 10), (11, 11), (12, 11), (12, 13)]]
    assert cableroute.read_path("") == [] and cableroute.read_path("garbage") == []


def test_projection_round_trip():
    for lon, lat in ((0, 0), (-73.9, 40.7), (103.8, 1.35), (179.9, -45), (-179.9, 70)):
        x, y = cableroute.project(lon, lat)
        assert cables.project(lon, lat) == (x, y)
        lo, la = cableroute.unproject(x, y)
        assert abs(lo - lon) < 1e-6 and abs(la - lat) < 1e-6


def test_demo_routes_get_estimates_from_the_bundled_file():
    d = cables.CableData(None).data()
    secs = [demo_routes.HOME, demo_routes.PROVIDER] + demo_routes.ROUTES[0][4]
    est = cableroute.estimate(secs, d)
    steps = {e["step"] for e in est}
    assert steps == {0, 1}                                       # North Sea and Atlantic crossings
    assert all(e["sure"] is False and e["confidence"] in ("likely", "possible") for e in est)
    assert any(x["kind"] == "cable" for x in cableroute.path(secs, est))


# ---------------------------------------------------------------------------------------------------------- run time data
def test_cable_data_prefers_a_good_import_and_falls_back_to_the_bundled_file(tmp_path):
    cd = cables.CableData(tmp_path)
    assert cd.info()["source"] == "bundled"
    info = cd.import_answer(answer(way(1, line(-40, 10, -10, 10), "Own Link")), "2026-11-19")
    assert info["source"] == "import" and info["osm_sea"] == 1 and (tmp_path / cables.IMPORT_NAME).is_file()
    assert info["sea"] == 1 + info["open_sea"]                   # the lines of the open sources stay next to an own import
    assert cables.CableData(tmp_path).info()["source"] == "import"
    (tmp_path / cables.IMPORT_NAME).write_bytes(b"broken")
    again = cables.CableData(tmp_path)
    assert again.info()["source"] == "bundled" and again.info()["error"] == "paths.cables.err.import_broken"
    assert cd.reset()["source"] == "bundled" and not (tmp_path / cables.IMPORT_NAME).exists()


def test_import_refuses_answers_without_cables_and_keeps_the_data(tmp_path):
    cd = cables.CableData(tmp_path)
    for raw in (b"", b"{}", answer(way(1, line(0, 0, 1, 1), "Power", power="cable"))):
        with pytest.raises(ValueError):
            cd.import_answer(raw, "2026-11-19")
    assert cd.info()["source"] == "bundled"
    with pytest.raises(ValueError, match="paths.cables.err.no_folder"):
        cables.CableData(None).import_answer(answer(way(1, line(-40, 10, -10, 10), "A")), "2026-11-19")


def test_nothing_is_fetched_from_the_network(monkeypatch):
    """Loading, estimating and the API never open a connection: the cable data ships with the app."""
    def refuse(*a, **k):
        raise AssertionError("network access")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    d = cables.CableData(None).data()
    secs = [demo_routes.HOME, demo_routes.PROVIDER] + demo_routes.ROUTES[0][4]
    assert cableroute.estimate(secs, d)
    src = (ROOT / "lanloom" / "cables.py").read_text() + (ROOT / "lanloom" / "cableroute.py").read_text()
    assert "urllib" not in src and "socket" not in src and "http.client" not in src and "overpass-api" not in src
    js = (ROOT / "lanloom" / "web" / "js" / "paths_cables.js").read_text()
    assert not re.search(r"https?://(?!www\.openstreetmap\.org|opendatacommons\.org|www\.w3\.org)", js)


# ---------------------------------------------------------------------------------------------------------- API
@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cables-api")
    mp = pytest.MonkeyPatch()
    for k, v in (("LANLOOM_DATA", str(tmp)), ("LANLOOM_DEMO", "1"), ("LANLOOM_DEMO_SEED", "1"), ("LANLOOM_UPDATE_CHECK", "0")):
        mp.setenv(k, v)
    try:
        app = create_app(config_mod.load())
        with TestClient(app, base_url="http://lanloom.test") as c:
            code = (tmp / "setup-code.txt").read_text().strip()
            assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
            c.st = app.state.ll
            c.st.accounts.create("vera", PW, role="viewer")
            c.st.accounts.create("olga", PW, role="operator")
            c.tmp = tmp
            demo_routes.seed(c.st.core)
            yield c
    finally:
        mp.undo()


def login(c, user):
    c.cookies.clear()
    r = c.post("/api/login", json={"username": user, "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text


def audit_actions(c):
    return [(e["action"], e.get("outcome")) for e in c.st.audit.query({}, 200)]


def test_api_viewer_reads_the_cables_compressed_with_an_etag(app_client):
    c = app_client
    login(c, "vera")
    r = c.get("/api/paths/cables")
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip" and r.headers["etag"]
    d = r.json()
    assert d["format"] == "lanloom-cables/1" and d["attribution"] == "© OpenStreetMap contributors"
    assert c.get("/api/paths/cables", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    info = c.get("/api/paths/cables/info").json()
    assert info["available"] and info["source"] == "bundled" and info["license"] == "ODbL 1.0"
    c.cookies.clear()
    assert c.get("/api/paths/cables").status_code == 401


def test_api_routes_carry_crossings_estimates_and_a_path_without_addresses(app_client):
    c = app_client
    login(c, "vera")
    d = c.get("/api/paths").json()
    us = next(r for r in d["routes"] if r["cc"] == "US" and r["city"] == "Ashburn")
    assert [x["step"] for x in us["crossings"]] == [0, 1]
    assert us["cables"] and all(e["sure"] is False for e in us["cables"]) and "line" not in us["cables"][0]
    assert any(p["kind"] == "cable" for p in us["path"])
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", json.dumps(d["routes"]))
    sheet = c.get("/api/paths/country/IE").json()
    assert any(e["name"] for r in sheet["routes"] for e in r["cables"])


def test_api_only_an_admin_imports_or_resets_with_audit_entries(app_client):
    c = app_client
    raw = answer(way(1, line(-40, 10, -10, 10), "Own Link"))
    for user in ("vera", "olga"):
        login(c, user)
        assert c.post("/api/paths/cables/import", content=raw, headers=HDR).status_code == 403
        assert c.post("/api/paths/cables/reset", headers=HDR).status_code == 403
    login(c, "admin")
    core = c.st.core
    core.cables.folder = c.tmp / "cables-test"           # the sample network has no folder of its own for an import
    try:
        r = c.post("/api/paths/cables/import", content=raw, headers=dict(HDR, **{"Content-Type": "application/json"}))
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "import" and r.json()["osm_sea"] == 1
        assert c.post("/api/paths/cables/import", content=b"{}", headers=HDR).json()["error"] == "paths.cables.err.no_cables"
        big = c.post("/api/paths/cables/import", content=b"x", headers=dict(HDR, **{"Content-Length": str(cables.MAX_IMPORT_BYTES + 1)}))
        assert big.status_code in (400, 413)
        assert c.post("/api/paths/cables/reset", headers=HDR).json()["source"] == "bundled"
    finally:
        core.cables.folder = None
        core.cables.reset()
    acts = audit_actions(c)
    assert ("paths.cables.import", "ok") in acts and ("paths.cables.reset", "ok") in acts


def test_estimates_are_kept_in_memory_only(app_client):
    c = app_client
    login(c, "vera")
    c.get("/api/paths")
    db = c.st.core.store
    tables = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")]
    assert not [t for t in tables if "cable" in t]
    for t in ("paths_route",):
        rows = db.query("SELECT sections FROM %s" % t)
        assert rows and not any("confidence" in r["sections"] for r in rows)
    assert time.time()
