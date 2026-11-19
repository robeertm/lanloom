"""Synthetic cities and network operators for the sample network's data paths (the demo never uses the real city file).

The operators are invented (documentation AS numbers, generic names); the cities are public places with the usual centre
coordinates, so the maps in the sample network look like a real one without naming anything from a real network.
Every country's traffic is split by fixed weights; a city of ``None`` means the source knows the operator but not the city
(drawn as an approximate bubble)."""
from typing import Optional

# cc -> {city name: (region, latitude, longitude)}
CITIES = {
    "NL": {"Amsterdam": ("North Holland", 52.37, 4.90), "Rotterdam": ("South Holland", 51.92, 4.48), "Utrecht": ("Utrecht", 52.09, 5.12)},
    "US": {"Ashburn": ("Virginia", 39.04, -77.49), "Portland": ("Oregon", 45.52, -122.68), "Dallas": ("Texas", 32.78, -96.80),
           "Los Angeles": ("California", 34.05, -118.24), "Chicago": ("Illinois", 41.88, -87.63)},
    "DE": {"Frankfurt": ("Hesse", 50.11, 8.68), "Nuremberg": ("Bavaria", 49.45, 11.08), "Falkenstein": ("Saxony", 50.48, 12.37)},
    "IE": {"Dublin": ("Leinster", 53.35, -6.26)},
    "CH": {"Zurich": ("Zurich", 47.38, 8.54), "Geneva": ("Geneva", 46.20, 6.14)},
    "FR": {"Paris": ("Ile-de-France", 48.86, 2.35), "Roubaix": ("Hauts-de-France", 50.69, 3.17)},
    "SE": {"Stockholm": ("Stockholm", 59.33, 18.07)},
    "GB": {"London": ("England", 51.51, -0.13), "Manchester": ("England", 53.48, -2.24)},
    "SG": {"Singapore": ("Singapore", 1.35, 103.82)},
    "FI": {"Helsinki": ("Uusimaa", 60.17, 24.94)},
    "CN": {"Beijing": ("Beijing", 39.91, 116.40)},
    "KR": {"Seoul": ("Seoul", 37.57, 126.98)},
}
# cc -> [(AS number, operator name, weight, [(city or None, weight)])]
OPERATORS = {
    "NL": [(64496, "Example Broadband", 5, [("Amsterdam", 5), ("Rotterdam", 2), ("Utrecht", 1)]),
           (64497, "Example Cloud", 3, [("Amsterdam", 4), (None, 1)]),
           (64498, "Example Transit", 1, [(None, 1)])],
    "US": [(64499, "Example Cloud Services", 6, [("Ashburn", 5), ("Portland", 2), ("Dallas", 1)]),
           (64500, "Sample Video Network", 5, [("Los Angeles", 3), ("Ashburn", 2), (None, 2)]),
           (64501, "Example Transit", 2, [(None, 1)]),
           (64502, "Sample Hosting", 1, [("Chicago", 1), (None, 1)])],
    "DE": [(64503, "Example Hosting", 4, [("Falkenstein", 3), ("Nuremberg", 2)]), (64504, "Example Telecom", 2, [("Frankfurt", 3), (None, 1)])],
    "IE": [(64499, "Example Cloud Services", 1, [("Dublin", 1)])],
    "CH": [(64505, "Example Storage Provider", 1, [("Zurich", 3), ("Geneva", 1)])],
    "FR": [(64506, "Example Hosting", 3, [("Roubaix", 2), ("Paris", 2)]), (64507, "Example Telecom", 1, [(None, 1)])],
    "SE": [(64508, "Example Streaming", 1, [("Stockholm", 3), (None, 1)])],
    "GB": [(64509, "Example Broadband", 2, [("London", 3), ("Manchester", 1), (None, 2)]), (64510, "Example Transit", 1, [(None, 1)])],
    "SG": [(64511, "Example Cloud Services", 1, [("Singapore", 1)])],
    "FI": [(65536, "Example Hosting", 1, [("Helsinki", 1)])],
    "CN": [(65537, "Example Telecom", 1, [("Beijing", 3), (None, 2)])],
    "KR": [(65538, "Example Telecom", 1, [("Seoul", 2), (None, 1)])],
}


def city_key(cc: str, name: str) -> str:
    return "%s|%s" % (name, CITIES[cc][name][0])


def split(cc: str, tx: int, rx: int) -> list:
    """[(city key or "", AS number, tx, rx)] adding up exactly to tx and rx; [] for a country without a table."""
    ops = OPERATORS.get(cc)
    if not ops:
        return []
    shares = []
    for asn, _name, w, cities in ops:
        tot = sum(cw for _c, cw in cities)
        for city, cw in cities:
            shares.append((city_key(cc, city) if city else "", asn, w * cw / tot))
    total = sum(s for _k, _a, s in shares)
    out, left_tx, left_rx = [], tx, rx
    for i, (key, asn, s) in enumerate(shares):
        last = i == len(shares) - 1
        a = left_tx if last else int(tx * s / total)
        b = left_rx if last else int(rx * s / total)
        out.append((key, asn, a, b))
        left_tx -= a
        left_rx -= b
    return out


def places(cc: str) -> list:
    return [(cc, city_key(cc, n), n, v[0], v[1], v[2]) for n, v in CITIES.get(cc, {}).items()]


def operators() -> dict:
    out = {}
    for ops in OPERATORS.values():
        for asn, name, _w, _c in ops:
            out[asn] = name
    return out


def lookup_best_city(cc: str, asn: int) -> Optional[str]:
    for a, _n, _w, cities in OPERATORS.get(cc, ()):
        if a == asn:
            named = [c for c, w in sorted(cities, key=lambda x: -x[1]) if c]
            return city_key(cc, named[0]) if named else None
    return None
