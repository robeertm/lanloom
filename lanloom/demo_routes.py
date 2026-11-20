"""Synthetic network routes for the sample network (DW2): the demo never measures anything.

Home country of the sample network is NL (``demo.PATHS_HOME``). Operators are invented (documentation AS numbers, generic
names, the same as in ``demo_paths_detail``), exchange points carry invented names, places are public cities with their
usual centre coordinates. One route shows an approximate place (dashed, "estimated"), one changed since the day before,
one ends at an anycast destination (its target says only "Anycast"), and one gives a measured place to traffic that has no
city in the data paths (the country sheet then draws that bubble there instead of in the middle of the country).
"""
import json
import time

from . import routes, routewatch

# (name, cc, lat, lon)
_P = {
    "ams": ("Amsterdam", "NL", 52.37, 4.90), "rtm": ("Rotterdam", "NL", 51.92, 4.48), "fra": ("Frankfurt", "DE", 50.11, 8.68),
    "lon": ("London", "GB", 51.51, -0.13), "nyc": ("New York", "US", 40.71, -74.01), "iad": ("Ashburn", "US", 39.04, -77.49),
    "lax": ("Los Angeles", "US", 34.05, -118.24), "dub": ("Dublin", "IE", 53.35, -6.26), "zrh": ("Zurich", "CH", 47.38, 8.54),
    "nue": ("Nuremberg", "DE", 49.45, 11.08), "sin": ("Singapore", "SG", 1.35, 103.82), "par": ("Paris", "FR", 48.86, 2.35),
    "mrs": ("Marseille", "FR", 43.30, 5.37), "chi": ("Chicago", "US", 41.88, -87.63),
}


def _place(code, sure=True, source="name"):
    n, cc, lat, lon = _P[code]
    return {"name": n, "cc": cc, "lat": lat, "lon": lon, "source": source, "sure": sure}


def _s(kind, asn=None, operator=None, cc=None, place=None, rtt=None, hops=1, **extra):
    d = {"kind": kind, "asn": asn, "operator": operator, "cc": cc, "place": place, "rtt": rtt, "hops": hops}
    d.update(extra)
    return d


HOME = _s("home", cc="NL", hops=1, rtt=1)
PROVIDER = _s("provider", 64496, "Example Broadband", "NL", _place("ams"), 6, 3)

# (cc, asn, city name or "" , changed?, sections after home+provider)
ROUTES = [
    ("US", 64499, "Ashburn", False, [
        _s("exchange", None, "Example IX", "NL", _place("ams", source="exchange"), 7, 1),
        _s("transit", 64501, "Example Transit", "GB", _place("lon"), 14, 2),
        _s("transit", 64501, "Example Transit", "US", _place("nyc"), 82, 3),
        _s("network", 64499, "Example Cloud Services", "US", _place("iad"), 88, 2),
        _s("target", 64499, "Example Cloud Services", "US", None, 89, 0, anycast=False, reached=True, city="Ashburn")]),
    ("US", 64500, "", False, [
        _s("transit", 64501, "Example Transit", "GB", _place("lon"), 13, 2),
        _s("transit", 64501, "Example Transit", "US", _place("chi"), 104, 3),
        _s("network", 64500, "Sample Video Network", "US", _place("lax"), 141, 2),
        _s("target", 64500, "Sample Video Network", "US", None, None, 0, anycast=False, reached=False, city=None)]),
    ("DE", 64503, "Nuremberg", True, [
        _s("exchange", None, "Example IX", "DE", _place("fra", source="exchange"), 11, 1),
        _s("network", 64503, "Example Hosting", "DE", _place("nue", sure=False, source="city"), 15, 2),
        _s("target", 64503, "Example Hosting", "DE", None, 15, 0, anycast=False, reached=True, city="Nuremberg")]),
    ("IE", 64499, "Dublin", False, [
        _s("transit", 64501, "Example Transit", "GB", _place("lon"), 13, 2),
        _s("network", 64499, "Example Cloud Services", "IE", _place("dub"), 22, 2),
        _s("target", 64499, "Example Cloud Services", "IE", None, 23, 0, anycast=False, reached=True, city="Dublin")]),
    ("CH", 64505, "Zurich", False, [
        _s("exchange", None, "Example IX", "DE", _place("fra", source="exchange"), 11, 1),
        _s("network", 64505, "Example Storage Provider", "CH", _place("zrh"), 18, 2),
        _s("target", 64505, "Example Storage Provider", "CH", None, 18, 0, anycast=False, reached=True, city="Zurich")]),
    ("SG", 64511, "Singapore", False, [
        _s("transit", 64501, "Example Transit", "FR", _place("mrs"), 24, 2),
        _s("network", 64511, "Example Cloud Services", "SG", _place("sin"), 158, 4),
        _s("target", 64511, "Example Cloud Services", "SG", None, 160, 0, anycast=False, reached=True, city="Singapore")]),
    ("FK", 14593, "", False, [
        _s("transit", 64501, "Example Transit", "GB", _place("lon"), 13, 2),
        _s("network", 14593, "Orbit Link", "FK", None, 148, 3),
        _s("target", 14593, "Orbit Link", "FK", None, 150, 0, anycast=False, reached=True, city=None)]),
    ("~anycast", 0, "", False, [
        _s("network", None, None, "NL", _place("ams", sure=False, source="city"), 7, 1),
        _s("target", None, None, None, None, 7, 0, anycast=True, reached=True, city=None)]),
]


def seed(core, now: float = None) -> None:
    """Stored routes for the sample network (only if there are none yet) and the option shown as switched on."""
    rw = getattr(core, "routes", None)
    if rw is None:
        return
    rw.demo = True
    if core.store.one("SELECT 1 AS x FROM paths_route LIMIT 1"):
        return
    now = time.time() if now is None else now
    day = int(now // 86400)
    with core.store.transaction() as st:
        for i, (cc, asn, city, changed, rest) in enumerate(ROUTES):
            city_key = ""
            if city and cc != "~anycast":
                from .demo_paths_detail import CITIES
                region = (CITIES.get(cc, {}).get(city) or ("",))[0]
                city_key = "%s|%s" % (city, region)
            key = routewatch.route_key(cc, asn, city_key)
            sections = [dict(HOME), dict(PROVIDER)] + [dict(s) for s in rest]
            sig = routes.signature(sections)
            t = now - 600 - i * 1500
            prev = None
            if changed:
                old = [dict(HOME), dict(PROVIDER), _s("transit", 64504, "Example Telecom", "DE", _place("fra"), 12, 2)] + [dict(s) for s in rest[1:]]
                prev = routes.signature(old)
                st._db.execute("INSERT OR REPLACE INTO paths_route_day(day, key, sig) VALUES(?,?,?)", (day - 1, key, prev))
            st._db.execute("INSERT OR REPLACE INTO paths_route_day(day, key, sig) VALUES(?,?,?)", (day, key, sig))
            st._db.execute("INSERT OR REPLACE INTO paths_route(key, cc, asn, city, t, method, sections, sig, changed, prev) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (key, cc, asn, city_key, t, "udp", json.dumps(sections, separators=(",", ":")), sig,
                            (now - 5 * 3600) if changed else None, prev))
    core.store.set_setting("paths_routes", True)
