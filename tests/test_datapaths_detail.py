"""Data paths with cities and network operators (DW1): booking with the option on and off, caps, the sum rule of the
country sheet (header, bubbles and device list add up to the same number), no address in the database, retention."""
import csv
import gzip
import time
from pathlib import Path

import pytest

from lanloom import datapaths, geo, geocity
from lanloom.datapaths import DataPaths
from lanloom.store import Store

T0 = 1_790_000_000.0
NOW = time.mktime(time.strptime("2026-11-19 12:00:00", "%Y-%m-%d %H:%M:%S"))
PEER_A, PEER_B, PEER_C, PEER_D = "3.3.3.3", "3.3.4.4", "5.9.1.1", "3.3.5.5"      # real-country addresses, only looked up


@pytest.fixture(scope="module")
def real_geo():
    g = geo.Geo()
    g.start(background=False)
    assert g.db is not None, g.error
    return g


def _gz(path, rows):
    with gzip.open(path, "wt", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


def _net(ip):
    a = ".".join(ip.split(".")[:3])
    return a + ".0", a + ".255"


def build_city_data(tmp_path, real_geo, extra_city=(), extra_asn=()):
    """Cities for the /24 of every peer above; the country in the file is the one the bundled data names."""
    cc = {p: real_geo.country(p) for p in (PEER_A, PEER_B, PEER_C, PEER_D)}
    assert all(len(c) == 2 and c != "ZZ" for c in cc.values())
    city = [  # ascending by start address
        [*_net(PEER_A), "NA", cc[PEER_A], "North", "Alphaville", "10.00", "20.00"],
        [*_net(PEER_B), "NA", cc[PEER_B], "", "", "0", "0"],                        # operator known, city not
        [*_net(PEER_D), "NA", cc[PEER_D], "South", "Deltaburg", "11.00", "21.00"],
        [*_net(PEER_C), "NA", "ZZ" if cc[PEER_C] == "ZZ" else cc[PEER_C], "Coast", "Gammaport", "12.00", "22.00"],
    ] + list(extra_city)
    asn = [
        [*_net(PEER_A), "64500", "Example Transit"],
        [*_net(PEER_B), "64500", "Example Transit"],
        [*_net(PEER_D), "64502", "Example Cloud"],
        [*_net(PEER_C), "64501", "Example Broadband"],
    ] + list(extra_asn)

    def key(r):
        a = [int(x) for x in r[0].split(".")]
        return a
    city.sort(key=key)
    asn.sort(key=key)
    src_c, src_a = _gz(tmp_path / "c.csv.gz", city), _gz(tmp_path / "a.csv.gz", asn)

    def fetch(url, dest, max_bytes, progress=None):
        dest.write_bytes((src_a if "-asn-" in url else src_c).read_bytes())

    data = geocity.CityData(tmp_path / "geo", fetch=fetch, clock=lambda: NOW, limits=geocity.Limits.small(), min_free=1)
    return data, cc


@pytest.fixture()
def env(tmp_path, real_geo):
    data, cc = build_city_data(tmp_path, real_geo)
    g = geo.Geo(detail=data)
    g.db = real_geo.db                                  # the bundled country data, loaded once for the module
    store = Store(":memory:")
    paths = DataPaths(store, g, lambda: {"home_country": "", "paths_own_networks": ["198.51.100.0/24"], "paths_no_internet": []}, clock=lambda: NOW)
    return paths, g, data, cc, store


def turn_on(data):
    data.configure(True, background=False)
    assert data.step(NOW) is True


def total_bytes(view):
    return view["totals"]["bytes"]


def sum_points(view):
    return sum(p["bytes"] for p in view["points"])


def test_off_books_no_detail(env):
    paths, g, data, cc, store = env
    paths.book("dev1", PEER_A, 100, 200, NOW)
    paths.flush(NOW)
    assert store.one("SELECT COUNT(*) AS n FROM paths_city_hour")["n"] == 0
    view = paths.country_view(cc[PEER_A], "24h", {}, NOW)
    assert view["totals"]["bytes"] == 300 and view["cities"] == [] and view["operators"] == []
    # nothing known about cities: one approximate bubble in the middle of the country carries the whole sum
    assert view["points"] == [{"key": None, "approx": True, "bytes": 300, "name": "", "region": "", "lat": None, "lon": None}]
    assert view["geo"]["detail"]["status"] == "off"


def test_on_books_cities_and_operators_and_the_sum_rule_holds(env):
    paths, g, data, cc, store = env
    paths.book("dev1", PEER_A, 100, 200, NOW - 7200)           # before the option: counted in the country only
    turn_on(data)
    paths.book("dev1", PEER_A, 1000, 2000, NOW)                # a city and an operator
    paths.book("dev2", PEER_D, 40, 60, NOW)                    # another city, another operator
    paths.book("dev1", PEER_B, 500, 500, NOW)                  # operator known, city not
    paths.flush(NOW)
    view = paths.country_view(cc[PEER_A], "24h", {"dev1": {"title": "Printer", "kind": "printer"}}, NOW)
    cities = {c["name"]: c for c in view["cities"]}
    assert cities["Alphaville"]["bytes"] == 3000 and cities["Alphaville"]["operators"][0]["name"] == "Example Transit"
    ops = {o["name"]: o for o in view["operators"]}
    assert ops["Example Transit"]["bytes"] == 3000 + 1000 and ops["Example Transit"]["cities"][0]["name"] == "Alphaville"
    t = view["totals"]
    assert t["bytes"] == 300 + 3000 + 100 + 1000                # the country tables are the one truth
    assert t["operators"] == len([o for o in view["operators"] if not o["other"]]) == 2
    assert t["cities"] == len(view["cities"])
    assert sum_points(view) == total_bytes(view)               # the sum rule: map == header
    pts = {(p["key"], p["approx"]): p for p in view["points"]}
    assert pts[("Alphaville|North", False)]["bytes"] == 3000 and pts[("Alphaville|North", False)]["lat"] == 10.0
    # the operator without a city for this peer is drawn at its largest known city (Alphaville), marked approximate
    assert pts[("Alphaville|North", True)]["bytes"] == 1000
    # bytes from before the option, no operator and no city: the middle of the country
    assert pts[(None, True)]["bytes"] == 300
    assert sum(d["bytes"] for d in view["devices"]) == total_bytes(view)
    assert view["devices"][0]["title"] in ("Printer", "")


def test_sum_rule_with_many_shapes(env, monkeypatch):
    paths, g, data, cc, store = env
    turn_on(data)
    peers = (PEER_A, PEER_B, PEER_C, PEER_D)
    n = 0
    for i, p in enumerate(peers * 3):
        paths.book("dev%d" % (i % 3), p, 10 * (i + 1), 7 * (i + 1), NOW - i * 3600)
        n += 17 * (i + 1)
    paths.flush(NOW)
    for window in ("24h", "7d", "30d"):
        for c in {cc[p] for p in peers}:
            v = paths.country_view(c, window, {}, NOW)
            assert sum_points(v) == total_bytes(v) == sum(d["bytes"] for d in v["devices"])
            assert sum(pt["tx"] + pt["rx"] for pt in v["series"]["points"]) == total_bytes(v)
    # with a very small map budget the small bubbles merge into the middle of the country and the sum still holds
    monkeypatch.setattr(datapaths, "MAX_MAP_POINTS", 2)
    paths._country_cache.clear()
    v = paths.country_view(cc[PEER_A], "24h", {}, NOW)
    assert len(v["points"]) <= 2 and sum_points(v) == total_bytes(v)


def test_caps_condense_into_other(env, monkeypatch):
    paths, g, data, cc, store = env
    monkeypatch.setattr(datapaths, "MAX_PLACES", 1)
    monkeypatch.setattr(datapaths, "MAX_OPERATORS", 1)
    turn_on(data)
    for p in (PEER_A, PEER_D, PEER_B):
        paths.book("dev1", p, 10, 10, NOW)
    paths.flush(NOW)
    rows = store.query("SELECT city, asn FROM paths_city_day ORDER BY city, asn")
    cities = {r["city"] for r in rows if r["city"]}
    asns = {r["asn"] for r in rows}
    same = [p for p in (PEER_A, PEER_D, PEER_B) if cc[p] == cc[PEER_A]]
    if len(same) > 1:
        assert datapaths.OTHER_CITY in cities and datapaths.OTHER_ASN in asns
    v = paths.country_view(cc[PEER_A], "24h", {}, NOW)
    assert sum_points(v) == total_bytes(v)
    assert all(not o["name"] for o in v["operators"] if o["other"])
    # the caps continue after a restart: a new DataPaths reads what is already stored for the day
    again = DataPaths(store, g, lambda: {}, clock=lambda: NOW)
    again.book("dev1", PEER_D, 1, 1, NOW)
    assert len(again._caps[(int(NOW // 86400), cc[PEER_D])][0]) <= 1


def test_anycast_own_and_unassigned_never_get_a_city(env):
    paths, g, data, cc, store = env
    turn_on(data)
    paths.book("dev1", "8.8.8.8", 10, 10, NOW)                  # anycast
    paths.book("dev1", "198.51.100.7", 10, 10, NOW)             # own (a network listed in the settings)
    paths.book("dev1", "10.1.2.3", 10, 10, NOW)                 # reserved: own
    paths.flush(NOW)
    assert store.one("SELECT COUNT(*) AS n FROM paths_city_hour")["n"] == 0
    assert store.one("SELECT COUNT(*) AS n FROM paths_place")["n"] == 0


def test_city_in_another_country_than_the_country_data_is_dropped(env, real_geo, tmp_path):
    paths, g, data, cc, store = env
    # the city file says PEER_A is in a different country: the country of the bundled data wins, no city is booked
    wrong = "AA" if cc[PEER_A] != "AA" else "BB"
    d2, _ = build_city_data(tmp_path, real_geo)
    g2 = geo.Geo(detail=d2)
    g2.db = real_geo.db
    d2.configure(True, background=False)
    d2.step(NOW)
    place = g2.detail(PEER_A, wrong)
    assert place is not None and place[0] == "" and place[2] is None and place[1] == 64500
    ok = g2.detail(PEER_A, cc[PEER_A])
    assert ok[0] == "Alphaville|North" and ok[2]["name"] == "Alphaville"


def test_no_address_reaches_the_database(env):
    paths, g, data, cc, store = env
    turn_on(data)
    for p in (PEER_A, PEER_B, PEER_C, PEER_D):
        paths.book("dev1", p, 5, 5, NOW)
    paths.flush(NOW)
    dump = []
    for t in ("paths_hour", "paths_day", "paths_seen", "paths_city_hour", "paths_city_day", "paths_place", "paths_operator"):
        dump += [tuple(r.values()) if hasattr(r, "values") else tuple(r) for r in store.query("SELECT * FROM %s" % t)]
    text = repr(dump)
    for p in (PEER_A, PEER_B, PEER_C, PEER_D, "3.3.3", "5.9.1"):
        assert p not in text
    # a place carries the centre of the peer's city (hundredths of a degree), not a user position
    assert store.one("SELECT lat, lon FROM paths_place WHERE name='Alphaville'")["lat"] == 10.0


def test_retention_forget_and_orphans(env):
    paths, g, data, cc, store = env
    turn_on(data)
    paths.book("dev1", PEER_A, 5, 5, NOW - 40 * 86400)
    paths.book("dev1", PEER_D, 5, 5, NOW)
    paths.flush(NOW)
    paths.keep_hours, paths.keep_days = 49, 31
    paths.flush(NOW)
    # the old day is gone, and the names nothing refers to any more are gone with it
    assert store.one("SELECT COUNT(*) AS n FROM paths_city_day WHERE day < ?", (int(NOW // 86400) - 31,))["n"] == 0
    names = {r["name"] for r in store.query("SELECT name FROM paths_place")}
    assert "Deltaburg" in names and "Alphaville" not in names
    paths.forget_device("dev1")
    assert store.one("SELECT COUNT(*) AS n FROM paths_city_day")["n"] == 0
    paths.book("dev1", PEER_D, 5, 5, NOW)
    paths.flush(NOW)
    paths.clear_detail()
    assert store.one("SELECT COUNT(*) AS n FROM paths_city_day")["n"] == 0
    assert store.one("SELECT COUNT(*) AS n FROM paths_place")["n"] == 0
    assert store.one("SELECT SUM(tx) AS n FROM paths_hour")["n"] > 0        # the country figures stay


def test_invalid_country_codes_are_refused(env):
    paths = env[0]
    for bad in ("", "X", "123", "A_", "<>"):
        with pytest.raises(KeyError):
            paths.country_view(bad, "24h", {}, NOW)
    v = paths.country_view("de", "bogus", {}, NOW)
    assert v["cc"] == "DE" and v["window"] == "24h" and v["totals"]["bytes"] == 0 and v["points"] == []
