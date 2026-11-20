"""Infrastructure routes (lanloom/infraroute.py, view "Real cables"): every data path runs over land routes, cables, radio or satellite.

The graph is tested without a network and without the bundled cable file: the test cables are built here with real geometry and
invented names. The API tests run in the sample network (no measurement, no download)."""
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import cableroute, cables, datapaths, demo_routes
from lanloom import config as config_mod
from lanloom import infraroute as N
from lanloom.main import create_app

ROOT = Path(__file__).resolve().parent.parent
HDR = {"X-Lanloom": "1"}
PW = "correct horse battery 42"
WORLD = N.load_world(ROOT / "lanloom" / "web" / "js" / "worldmap.js")
COUNTRIES = N.country_points(WORLD)
HOME = {"cc": "DE", "lat": 50.5, "lon": 9.5}


def _way(wid, name, pts):
    return {"type": "way", "id": wid, "tags": {"communication": "line", "location": "submarine", "name": name},
            "geometry": [{"lon": lon, "lat": lat} for lon, lat in pts]}


def _data(*ways):
    raw = json.dumps({"version": 0.6, "elements": list(ways)}).encode()
    return cables.build(cables.ways_from_answer(raw), "2026-11-20")


# Test cables (lon, lat): Atlantic Cornwall -> New York, North Sea NL -> GB, Mediterranean Marseille -> Alexandria, Malta -> Sicily
ATLANTIC = _way(1, "Test cable Atlantic", [(-4.55, 50.83), (-30.0, 48.0), (-55.0, 43.0), (-73.95, 40.6)])
NORTH_SEA = _way(2, "Test cable North Sea", [(4.45, 52.2), (3.0, 52.4), (1.75, 52.5)])
MEDITERRANEAN = _way(3, "Test cable Mediterranean", [(5.37, 43.28), (8.0, 40.0), (16.0, 35.5), (29.9, 31.2)])
MALTA = _way(4, "Test cable Malta", [(14.55, 35.83), (14.85, 36.4), (14.95, 36.72)])
DATA = _data(ATLANTIC, NORTH_SEA, MEDITERRANEAN, MALTA)


@pytest.fixture(scope="module")
def routes():
    return N.InfraRoutes(COUNTRIES, WORLD)


@pytest.fixture(scope="module")
def answer(routes):
    return routes.compute(DATA, HOME, [])


def _edges(a, ids):
    return [a["edges"][i.lstrip("~")] for i in ids]


def _points(a, ids):
    """Edges of a way in running direction as one polyline (like the view)."""
    pts = []
    for i in ids:
        p = a["edges"][i.lstrip("~")]["p"]
        p = p[::-1] if i.startswith("~") else p
        pts.extend(p if not pts else p[1:])
    return pts


def _sections(*places, target_rtt=None):
    """Condensed sections as ``routes.condense`` stores them: home, one section per place (name, country, lat, lon, rtt, sure)."""
    out = [{"kind": "home", "asn": None, "operator": None, "cc": "DE", "place": None, "rtt": 1, "hops": 1}]
    for name, lat, lon, cc, rtt, sure in places:
        out.append({"kind": "transit", "asn": None, "operator": None, "cc": cc, "rtt": rtt, "hops": 1,
                    "place": {"name": name, "cc": cc, "lat": lat, "lon": lon, "source": "name" if sure else "city", "sure": sure}})
    if target_rtt is not None:
        out.append({"kind": "target", "asn": None, "operator": None, "cc": None, "place": None, "rtt": target_rtt, "hops": 0,
                    "anycast": False, "reached": True, "city": None})
    return out


def _route(key, cc, sections, rtt=None, cables_=None):
    return {"key": key, "cc": cc, "t": 5, "sections": sections, "rtt": rtt, "cables": cables_ or []}


def test_the_list_is_consistent():
    names = set(N.HUBS)
    for k in N.CORRIDORS + N.SEA_ROUTES:
        assert k[0] in names and k[1] in names, k
    for k, (n, cc, la, lo) in N.HUBS.items():
        assert -60 < la < 85 and -180 <= lo <= 180 and len(cc) == 2 and n, k
    pairs = [tuple(sorted(k[:2])) for k in N.CORRIDORS]
    assert len(pairs) == len(set(pairs)), "every corridor only once"
    assert {"Munich", "Vienna", "Warsaw", "Moscow"} <= {v[0] for v in N.HUBS.values()}
    assert N.HUBS["bom"][0] == "Mumbai"


def test_every_hub_is_reachable():
    n = N.InfraNet(None, COUNTRIES, WORLD)             # even without cable data (corridors + sea routes carry alone)
    s = n.access("s:test", HOME["lat"], HOME["lon"], "DE", island=False)
    dist, _ = n.tree(s)
    missing = [n.key[i] for i in range(len(n.kind)) if n.kind[i] == "hub" and i not in dist]
    assert not missing, missing


def test_every_country_has_a_way_made_of_edges_only(answer):
    a = answer
    without = [cc for cc, c in COUNTRIES.items() if cc not in a["countries"]]
    assert not without, without
    for cc, z in a["countries"].items():
        assert z["path"], cc
        assert z["origin"] in ("land", "cable", "radio", "sat")
        pts = _points(a, z["path"])
        for i, j in zip(z["path"], z["path"][1:]):      # connected: every edge starts where the previous one ended
            p, q = a["edges"][i.lstrip("~")]["p"], a["edges"][j.lstrip("~")]["p"]
            end = p[0] if i.startswith("~") else p[-1]
            begin = q[-1] if j.startswith("~") else q[0]
            assert end == pytest.approx(begin, abs=0.02), (cc, i, j)
        assert pts[0] == pytest.approx(a["home"], abs=0.02), cc
        if z["origin"] != "sat":
            c = COUNTRIES[cc]
            assert pts[-1] == pytest.approx([c["x"], c["y"]], abs=0.05), cc
    assert all(k["kind"] in ("land", "cable", "sea", "landing", "access", "radio", "sat") for k in a["edges"].values())
    assert a["sources"] == ["hubs", "corridors", "cables_osm"]


def test_bundling_equal_corridors_share_edges(answer):
    us, ca = answer["countries"]["US"]["path"], answer["countries"]["CA"]["path"]
    common = [i for i, j in zip(us, ca) if i == j]
    assert len(common) >= 3, "USA and Canada share the way over the Atlantic"
    every = [i.lstrip("~") for z in answer["countries"].values() for i in z["path"]]
    assert set(every) <= set(answer["edges"])


def test_over_the_sea_never_on_a_land_edge(answer):
    for cc in ("US", "GB", "IE", "IS", "JP", "AU", "BR", "ZA", "IN", "SG"):
        z = answer["countries"][cc]
        kinds = [k["kind"] for k in _edges(answer, z["path"])]
        assert "cable" in kinds or "sea" in kinds, cc
        assert z["origin"] == "cable", cc
    us = [k for k in _edges(answer, answer["countries"]["US"]["path"]) if k["kind"] == "cable"]
    assert any(k["name"] == "Test cable Atlantic" for k in us), "the mapped cable wins over the estimated sea route"
    for cc in ("FR", "AT", "PL", "ES", "IT"):
        assert answer["countries"][cc]["origin"] == "land", cc


def test_landing_points_hang_in_their_own_country():
    n = N.InfraNet(DATA, COUNTRIES, WORLD)
    seen = 0
    for k in n.edges:
        if k["a"] != "landing":
            continue
        landing, hub = k["u"], k["v"]
        if n.cc[landing]:
            assert n.cc[landing] == n.cc[hub] or n.cc[landing] not in N.ISLANDS, (n.cc[landing], n.name[hub])
            seen += 1
    assert seen >= 6
    # Malta cable: the end on Sicily hangs on Palermo (Italy), not over the sea on Valletta
    sicily = min(n.landings, key=lambda i: (n.lat[i] - 36.72) ** 2 + (n.lon[i] - 14.95) ** 2)
    assert n.cc[sicily] == "IT"
    hubs = {n.name[k["v"]] for k in n.edges if k["a"] == "landing" and k["u"] == sicily}
    assert hubs == {"Palermo"}


def test_country_shapes_assign_points():
    f = N.CountryShapes(WORLD)
    P = cableroute.project
    assert f.country_at(*P(13.4, 52.5)) == "DE"
    assert f.country_at(*P(-4.55, 50.83)) == "GB"               # coast just outside the coarse shape
    assert f.country_at(*P(12.45, 41.9)) == "VA"                # microstate without a shape: over its point
    assert f.country_at(*P(-30.0, 45.0)) is None                # middle of the Atlantic


def test_country_points_come_from_the_map_and_project_back():
    c = COUNTRIES["DE"]
    assert cableroute.project(c["lon"], c["lat"]) == pytest.approx((c["x"], c["y"]), abs=0.01)
    assert "VA" in COUNTRIES and len(COUNTRIES) > 200


def test_satellite_remote_network_and_latency(routes, answer):
    aq = answer["countries"]["AQ"]
    assert aq["origin"] == "sat" and _edges(answer, aq["path"])[-1]["kind"] == "sat" and aq.get("gateway_name")
    us = answer["countries"]["US"]["sat"]                       # every other country knows its earth station
    assert us["gateway"] and us["gateway_name"] and us["path"]
    assert N.satellite(14593) == "leo" and N.satellite(7155) == "geo"
    assert N.satellite(None, "Example Satellite Services") == "geo" and N.satellite(None, "Example Satcom Ltd") == "geo"
    assert N.satellite(3320, "Example Telecom AG") is None
    # a name without the neutral words is never matched by name (only the AS number table and the neutral words count)
    assert N.satellite(64512, "Example Network Services") is None
    # latency jump: hops to Johannesburg normal, then +560 ms to the destination in Lesotho (country without a hub) -> geostationary
    sec = _sections(("Frankfurt", 50.11, 8.68, "DE", 8, True), ("Johannesburg", -26.2, 28.05, "ZA", 180, True), target_rtt=742)
    a = routes.compute(DATA, HOME, [_route("LS:1:a", "LS", sec, 742.0)])
    r = a["routes"]["LS:1:a"]
    assert r["origin"] == "sat" and r["orbit"] == "geo" and r["jump_ms"] >= 500 and r["gateway_name"] == "Johannesburg" and r["target"]
    # the same latency to a well connected country (Hong Kong has hubs) is no satellite
    sec2 = _sections(("Frankfurt", 50.11, 8.68, "DE", 8, True), ("Johannesburg", -26.2, 28.05, "ZA", 180, True), target_rtt=742)
    a2 = routes.compute(DATA, HOME, [_route("HK:1:a", "HK", sec2, 742.0)])
    assert a2["routes"]["HK:1:a"]["origin"] == "route"


def test_radio_for_islands_near_a_hub():
    n = N.InfraNet(DATA, COUNTRIES, WORLD)
    # invented island 60 km off Valletta (island state without its own cable and without a hub)
    i = n.access("l:QZ", 35.9, 15.2, "QZ", island=True)
    assert [n.edges[e]["a"] for _, _, e, _ in n.adj[i]] == ["radio"]
    # far out: no radio, a cable (course estimated) - very far: satellite
    j = n.access("l:QY", 30.0, -40.0, "QY", island=True)
    assert [n.edges[e]["a"] for _, _, e, _ in n.adj[j]] == ["sea"]
    k = n.access("l:QX", -50.0, -140.0, "QX", island=True)
    assert [n.edges[e]["a"] for _, _, e, _ in n.adj[k]] == ["sat"]


def test_measured_way_follows_the_places_and_the_estimated_cable(routes):
    clean = _sections(("Frankfurt", 50.11, 8.68, "DE", 6, True), ("Amsterdam", 52.37, 4.9, "NL", 12, True),
                      ("London", 51.51, -0.13, "GB", 16, True), ("New York", 40.71, -74.0, "US", 80, True), target_rtt=82)
    est = cableroute.estimate(clean, DATA)
    atlantic = [e for e in est if e["name"] == "Test cable Atlantic"]
    assert atlantic and atlantic[0]["step"] == 2, "the step counting fits the places (London -> New York)"
    a = routes.compute(DATA, HOME, [_route("US:1:x", "US", clean, 82.0, est)])
    r = a["routes"]["US:1:x"]
    assert r["origin"] == "route" and r["via"][:3] == ["Frankfurt", "Amsterdam", "London"] and "New York" in r["via"]
    assert "Test cable Atlantic" in r["cables"]
    # an outlier (geo database) drops out: no detour to Turkey
    dirty = _sections(("Frankfurt", 50.11, 8.68, "DE", 6, True), ("Amsterdam", 52.37, 4.9, "NL", 12, True),
                      ("London", 51.51, -0.13, "GB", 16, True), ("Springfield", 40.0, 30.0, "TR", 17, False),
                      ("New York", 40.71, -74.0, "US", 80, True), target_rtt=82)
    pref = [{"step": 2, "name": "Test cable Atlantic", "alternative": False}]
    b = routes.compute(DATA, HOME, [_route("US:2:x", "US", dirty, 82.0, pref)])["routes"]["US:2:x"]
    assert "Ankara" not in b["via"] and "Istanbul" not in b["via"]
    assert 0 < r["measured_edges"] <= len(r["path"])
    assert "end" not in r and "conflict" not in r
    assert _points(a, r["path"])[-1] == pytest.approx([COUNTRIES["US"]["x"], COUNTRIES["US"]["y"]], abs=0.05)


def test_anycast_conflict_ends_at_the_measured_place(routes):
    # "USA" per database, but 19 ms to the destination in Berlin: the USA are excluded (light in glass)
    sec = _sections(("Frankfurt", 50.11, 8.68, "DE", 6, True), ("Berlin", 52.52, 13.4, "DE", 18.9, True), target_rtt=18.9)
    a = routes.compute(DATA, HOME, [_route("US:3:y", "US", sec, 18.9)])
    r = a["routes"]["US:3:y"]
    assert r["conflict"]["rtt"] == 18.9 and r["conflict"]["place"] == "Berlin" and r["conflict"]["km"] > 7000
    assert r["end"] == pytest.approx(list(cableroute.project(13.4, 52.52)), abs=0.05)
    # not reached (the destination does not answer): no conclusion about the country, the way continues to the USA
    sec2 = _sections(("Frankfurt", 50.11, 8.68, "DE", 6, True), ("Berlin", 52.52, 13.4, "DE", 18.9, True))
    a2 = routes.compute(DATA, HOME, [_route("US:4:z", "US", sec2, 18.9)])
    assert "conflict" not in a2["routes"]["US:4:z"] and "end" not in a2["routes"]["US:4:z"]


def test_exit_of_the_home_from_the_measurements(routes):
    rs = []
    for i, (la, lo) in enumerate(((52.52, 13.4), (52.37, 4.9), (48.86, 2.35))):
        sec = _sections(("Hamburg", 53.55, 9.99, "DE", 5, True), ("Destination", la, lo, "DE", 9, True), target_rtt=9)
        rs.append(_route("DE:%d:a" % i, "DE", sec, 9.0))
    a = routes.compute(DATA, HOME, rs)
    assert a["countries"]["FR"]["via"][0] == "Hamburg", "all measurements leave the home over Hamburg"
    b = routes.compute(DATA, HOME, [])
    assert b["countries"]["FR"]["via"][0] != "Hamburg"


def test_works_without_cable_data_and_without_routes(routes):
    a = routes.compute(None, {"cc": "NL"}, None)
    assert a["sources"] == ["hubs", "corridors"] and a["cables_stand"] is None and a["routes"] == {}
    assert a["countries"]["US"]["origin"] == "cable"            # over the estimated sea routes
    assert routes.compute(None, {"cc": "QQ"}, None) is None     # a home without a point


def test_cache_stand_size_and_speed(routes):
    home = dict(HOME, lat=51.0)
    t = time.time()
    a = routes.compute(DATA, home, [])
    assert time.time() - t < 5.0
    assert routes.compute(DATA, home, []) is a, "second call from the cache"
    st = N.InfraRoutes.stand(DATA, home, [])
    assert a["stand"] == st and st != N.InfraRoutes.stand(dict(DATA, date="2026-12-01"), home, [])
    assert st != N.InfraRoutes.stand(DATA, home, [], version=2)
    assert len(json.dumps(a, separators=(",", ":"))) < 400_000


def test_enter_the_country(answer):
    gb = answer["countries"]["GB"]
    assert 0 < gb["enter"] <= len(gb["path"])
    assert "enter" not in answer["countries"]["AQ"]


def test_inland_ways_between_the_hubs_of_a_country(answer, routes):
    de = answer["inland"]["DE"]
    assert {"muc", "fra", "ber", "ham"} <= set(de["hubs"]) and de["paths"]
    hubs_of = {k: v["cc"] for k, v in answer["hubs"].items()}
    for ab, ids in de["paths"].items():
        a, b = ab.split("|")
        assert a in de["hubs"] and b in de["hubs"] and ids
        for i in ids:
            e = answer["edges"][i.lstrip("~")]
            assert e["kind"] not in ("sat", "access", "radio")
        assert _points(answer, ids)[0] == pytest.approx(de["hubs"][a], abs=0.05)
        assert _points(answer, ids)[-1] == pytest.approx(de["hubs"][b], abs=0.05)
    assert "AQ" not in answer["inland"]                         # one hub or none: nothing to connect
    n = N.InfraNet(DATA, COUNTRIES, WORLD)
    il = n.inland("GR")
    assert il is n.inland("GR"), "once per country"
    for w in il["ways"].values():
        for e, _ in w:
            assert n.edges[e]["a"] not in ("sat", "access", "radio")


def test_a_route_is_computed_once_per_measurement(routes):
    sec = _sections(("Frankfurt", 50.11, 8.68, "DE", 6, True), ("Paris", 48.86, 2.35, "FR", 12, True), target_rtt=14)
    r = _route("FR:9:a", "FR", sec, 14.0)
    a = routes.compute(DATA, HOME, [r])
    assert any(k[1] == "FR:9:a" and k[2] == 5 for k in routes._routes)
    cached = next(v for k, v in routes._routes.items() if k[1] == "FR:9:a")
    b = routes.compute(DATA, dict(HOME, lat=50.6), [r, _route("FR:9:b", "FR", sec, 14.0)])
    assert routes._routes[next(k for k in routes._routes if k[1] == "FR:9:a")] is cached
    assert "FR:9:a" in a["routes"] and {"FR:9:a", "FR:9:b"} <= set(b["routes"]) and "_w" not in b["routes"]["FR:9:a"]


def test_operator_lists_carry_sat_only_when_it_applies():
    assert datapaths._with_sat({"asn": 14593, "name": "AS14593", "bytes": 1})["sat"] == "leo"
    assert datapaths._with_sat({"asn": 7155, "name": "x", "bytes": 1})["sat"] == "geo"
    assert "sat" not in datapaths._with_sat({"asn": 3320, "name": "Example Telecom", "bytes": 1})


def test_module_is_english_and_names_no_operator():
    text = (ROOT / "lanloom" / "infraroute.py").read_text(encoding="utf-8")
    assert not re.search(r"[äöüÄÖÜß]", text)
    # operators are recognised by AS number and by two neutral words only - no name pattern of any company in the code
    assert N.SAT_WORDS.pattern == "satellite|satcom"
    assert re.findall(r"re\.compile\(([^)]*)\)", text) == ['r"satellite|satcom", re.I']


# ---------------------------------------------------------------------------------------------------------- API
@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("infra-api")
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
            demo_routes.seed(c.st.core)
            yield c
    finally:
        mp.undo()


def login(c, user):
    c.cookies.clear()
    r = c.post("/api/login", json={"username": user, "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text


def test_api_viewer_reads_the_infra_routes_with_etag_and_304(app_client):
    c = app_client
    login(c, "vera")
    r = c.get("/api/paths/infra")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=3600"
    d = r.json()
    assert r.headers["etag"] == '"%s"' % d["stand"] and d["countries"]["US"]["path"] and d["routes"]
    assert d["sources"] == ["hubs", "corridors", "cables_osm"]
    assert c.get("/api/paths/infra", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert c.get("/api/paths/infra", headers={"If-None-Match": '"other"'}).status_code == 200
    # the routes view tells the page when to fetch again
    rr = c.get("/api/paths/routes").json()
    assert rr["infra_stand"] == d["stand"]
    assert set(d["routes"]) <= {x["key"] for x in rr["routes"]}
    c.cookies.clear()
    assert c.get("/api/paths/infra").status_code == 401


def test_api_without_a_home_country_there_is_no_answer(app_client):
    c = app_client
    login(c, "vera")
    core = c.st.core
    old = core.store.get_setting("home_country", "")
    core.store.set_setting("home_country", "")
    try:
        assert c.get("/api/paths/infra").status_code == 404
        assert c.get("/api/paths/routes").json()["infra_stand"] is None
    finally:
        core.store.set_setting("home_country", old)
