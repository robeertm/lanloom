#!/usr/bin/env python3
"""Build the offline world base map used by the Lanloom web UI (stdlib only).

Usage:  python3 -I scripts/build_worldmap.py <ne_110m_admin_0_countries.geojson> <ne_50m_admin_0_countries.geojson> <ne_10m_populated_places_simple.geojson>
        python3 -I scripts/build_worldmap.py <ne_110m_admin_0_countries.geojson> <ne_50m_admin_0_countries.geojson>
        python3 -I scripts/build_worldmap.py --refine      (re-apply only the overseas/territory step to the current file)
Optional, after the three inputs: --exclude-places FILE   leaves out every place whose name contains one of the words in FILE
(one per line, any case); for names that must not appear in the published files. The output stays deterministic for the same inputs.
Writes (paths resolved relative to the repository root, independent of the cwd):
  lanloom/web/js/worldmap.js        base map 1:110m (+ label points 1:50m), always loaded
  lanloom/web/js/worldmap_fine.js   fine map 1:50m for high zoom levels, shapes only    (only with three inputs)
  lanloom/web/js/worldplaces.js     orientation places (Natural Earth populated places)  (only with three inputs)
With two inputs (or --refine) only worldmap.js is written; the fine files stay as they are.

Overseas parts: a part of a country far away from its main area (French Guiana in the shape of France, Hawaii in the
shape of the United States) is moved out of ``d``: into its own entry when it lies in a territory with its own ISO code
(TERRITORIES), otherwise into ``o`` (drawn, but never lit together with the country). Territories the geo data knows
but the base map has no shape for get a label point (TERRITORY_POINTS), so traffic to them is shown as a dot.

Source: Natural Earth (public domain). Shapes 1:110m, label points 1:50m, fine shapes 1:50m, places 1:10m. The input files are untrusted data:
they are only read and parsed as JSON, never executed.
Projection: Natural Earth I (Savric et al. 2011), viewBox width 1000, latitudes -58 to 84 degrees.
The JS project(lon, lat) uses the same formula and scale as this script. The output is deterministic (same input, same bytes).
"""
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "lanloom" / "web" / "js"
OUT = JS_DIR / "worldmap.js"
OUT_FINE = JS_DIR / "worldmap_fine.js"
OUT_PLACES = JS_DIR / "worldplaces.js"
WIDTH = 1000.0
LAT_TOP, LAT_BOTTOM = 84.0, -58.0
TOLERANCE = 0.5     # Douglas-Peucker, in map units
MIN_AREA = 1.5      # smaller parts are dropped (never the largest part of a country)
TOLERANCE_FINE = 0.02   # fine map (1:50m): Douglas-Peucker, coordinates rounded to 0.01 (measured: 0.07 -> ~365 kB, 0.02 -> ~630 kB)
MIN_AREA_FINE = 0.01
SAMPLE_POINTS = 48      # fine map: a part is measured with at most this many (evenly thinned) points when judging distances
# Places: a capital or a place up to scalerank 6; rank 0 capital/world city, 1 large, 2 medium, 3 small (from scalerank).
PLACE_MAX_SCALERANK = 6
PLACE_NAME_MAX = 24
# Places whose ISO code Natural Earth leaves open (-99): the code the bundled geo data uses for that area.
PLACE_CODE_FALLBACK = {"KOS": "XK", "SOL": "SO", "CYN": "CY"}
OVERSEAS_DIST = 100.0   # map units (~4000 km at the equator) between a part and the main area of its country
# Territories with their own ISO code that Natural Earth draws inside another country's shape: (code, lon/lat box).
TERRITORIES = (("GF", (-55.0, 1.5, -51.0, 6.0)),)
# Label points (lon, lat) of territories the bundled geo data knows but the 1:50m label data has no entry for.
TERRITORY_POINTS = {
    "BQ": (-68.27, 12.18), "BV": (3.41, -54.42), "CC": (96.87, -12.16), "CX": (105.63, -10.49),
    "GI": (-5.35, 36.14), "GP": (-61.55, 16.25), "MQ": (-61.02, 14.64), "RE": (55.53, -21.11),
    "SJ": (15.6, 78.2), "TK": (-171.85, -9.2), "UM": (166.62, 19.28), "YT": (45.14, -12.83),
}


def _raw(lon, lat):
    l = math.radians(lon)
    p = math.radians(lat)
    p2 = p * p
    p4 = p2 * p2
    x = l * (0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4)))
    y = p * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4)))
    return x, y


_X_MAX = _raw(180, 0)[0]
SCALE = WIDTH / (2 * _X_MAX)
_Y_TOP = _raw(0, LAT_TOP)[1]
HEIGHT = round((_Y_TOP - _raw(0, LAT_BOTTOM)[1]) * SCALE)


def project(lon, lat):
    x, y = _raw(lon, lat)
    return (x + _X_MAX) * SCALE, (_Y_TOP - y) * SCALE


def _dp(pts, tol):
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy)
        dmax, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = (math.hypot(px - ax, py - ay) if n == 0
                 else abs(dx * (ay - py) - (ax - px) * dy) / n)
            if d > dmax:
                dmax, idx = d, i
        if dmax > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def _area(pts):
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def _num(v, dec=1):
    t = ("%.*f" % (dec, v)).rstrip("0").rstrip(".")
    if t in ("-0", ""):
        t = "0"
    return t.replace("-0.", "-.") if t.startswith("-0.") else (t[1:] if t.startswith("0.") else t)


def _ring_path(pts, dec=1):
    """pts rounded to 10^-dec -> 'Mx y l dx dy ... z' (absolute start, relative continuation)."""
    f = 10 ** dec
    q = [(round(x * f), round(y * f)) for x, y in pts]
    d = [q[0]]
    for p in q[1:]:
        if p != d[-1]:
            d.append(p)
    if len(d) > 1 and d[0] == d[-1]:
        d.pop()
    if len(d) < 3:
        return None
    nums = [_num(d[0][0] / f, dec), _num(d[0][1] / f, dec)]
    out = "M" + nums[0] + (nums[1] if nums[1].startswith("-") else " " + nums[1]) + "l"
    first = True
    for (x0, y0), (x1, y1) in zip(d, d[1:]):
        for t in (_num((x1 - x0) / f, dec), _num((y1 - y0) / f, dec)):
            out += t if (t.startswith("-") or first) else " " + t
            first = False
    return out + "z"


def _rings(geom):
    """All rings of a Polygon/MultiPolygon: (points, is_outer)."""
    if geom["type"] == "Polygon":
        polys = [geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        return []
    return [([project(lo, la) for lo, la, *_ in r], i == 0) for poly in polys for i, r in enumerate(poly)]


def _path(geom, tol=TOLERANCE, min_area=MIN_AREA, dec=1):
    rings = _rings(geom)
    if not rings:
        return None
    cand = []
    for pts, outer in rings:
        pts = _dp([(round(x, dec + 2), round(y, dec + 2)) for x, y in pts], tol)
        cand.append((_area(pts) if len(pts) >= 3 else 0.0, pts, outer))
    largest = max((k for k in cand if k[2]), key=lambda k: k[0], default=None)
    parts = []
    for area, pts, outer in cand:
        if area < min_area and not (largest is not None and (area, pts) == (largest[0], largest[1])):
            continue
        p = _ring_path(pts, dec)
        if p:
            parts.append(p)
    if not parts and largest is not None:
        p = _ring_path(largest[1], dec)
        if p:
            parts.append(p)
    return "".join(parts) or None


def _parse_rings(d):
    """'Mx yl dx dy ...zMx y...' (format of _ring_path) -> [(source text, [absolute points], area)]."""
    out = []
    for part in d.split("M")[1:]:
        nums = [float(x) for x in re.findall(r"-?(?:\d+\.?\d*|\.\d+)", part)]
        if len(nums) < 2:
            continue
        x, y = nums[0], nums[1]
        pts = [(x, y)]
        for i in range(2, len(nums) - 1, 2):
            x += nums[i]
            y += nums[i + 1]
            pts.append((x, y))
        out.append(("M" + part, pts, _area(pts)))
    return out


def unproject(x, y):
    """Map units -> (lon, lat): latitude by bisection (y depends on it alone), then longitude exactly."""
    lo, hi = -90.0, 90.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if project(0, mid)[1] > y:      # y grows southwards
            lo = mid
        else:
            hi = mid
    lat = (lo + hi) / 2
    p = math.radians(lat)
    p2 = p * p
    p4 = p2 * p2
    f = 0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4))
    return math.degrees((x / SCALE - _X_MAX) / f), lat


def _thin(pts, n):
    """Evenly thinned point list (deterministic); a distance between two parts does not need the fine outline."""
    if not n or len(pts) <= n:
        return pts
    return [pts[i * (len(pts) - 1) // (n - 1)] for i in range(n)]


def _gap(a, b, sample=None):
    """Smallest distance between two point lists in map units at the equator, measured on the globe (so the two
    sides of the date line are neighbours, e.g. the far east of Russia). ``sample``: use at most that many points."""
    ga = [unproject(x, y) for x, y in _thin(a, sample)]
    gb = [unproject(x, y) for x, y in _thin(b, sample)]
    best = float("inf")
    for lon1, lat1 in ga:
        for lon2, lat2 in gb:
            dlon = abs(lon1 - lon2) % 360.0
            dlon = min(dlon, 360.0 - dlon) * math.cos(math.radians((lat1 + lat2) / 2))
            best = min(best, math.hypot(dlon, lat1 - lat2))
    return best * WIDTH / 360.0


def _in_box(pts, box):
    lo0, la0, lo1, la1 = box
    x0, y1 = project(lo0, la0)
    x1, y0 = project(lo1, la1)
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return x0 <= cx <= x1 and y0 <= cy <= y1


def refine(countries, points, sample=None, labels=True):
    """Overseas parts out of the main shape, own entries for territories with their own code, territory points.
    ``sample``: thin the point lists when measuring distances (fine map); ``labels=False``: no label points x/y."""
    for cc in sorted(countries):
        c = countries[cc]
        rings = _parse_rings(c["d"])
        if len(rings) < 2:
            continue
        main = max(rings, key=lambda r: r[2])
        # parts close to the main area or to a part already kept belong to the main area (island chains)
        keep, far = [main], []
        rest = [r for r in rings if r is not main]
        changed = True
        while changed:
            changed = False
            for r in list(rest):
                if any(_gap(r[1], k[1], sample) <= OVERSEAS_DIST for k in keep):
                    keep.append(r)
                    rest.remove(r)
                    changed = True
        far = rest
        if not far:
            continue
        other = []
        for r in far:
            code = next((t for t, box in TERRITORIES if t not in countries and _in_box(r[1], box)), None)
            if code:
                cx = sum(p[0] for p in r[1]) / len(r[1])
                cy = sum(p[1] for p in r[1]) / len(r[1])
                countries[code] = {"d": r[0]}
                if labels:
                    countries[code].update({"x": round(cx, 1), "y": round(cy, 1)})
                points.pop(code, None)
            else:
                other.append(r[0])
        c["d"] = "".join(r[0] for r in rings if any(r is k for k in keep))
        if other:
            c["o"] = "".join(other)
    for code, (lon, lat) in TERRITORY_POINTS.items():
        if code not in countries and code not in points:
            x, y = project(lon, lat)
            points[code] = [round(x, 1), round(y, 1)]
    return countries, points


def _code(p):
    for k in ("ISO_A2_EH", "ISO_A2"):
        c = (p.get(k) or "").strip()
        if c and c != "-99":
            return c
    return None


def own_code_first(f):
    return 0 if (f["properties"].get("ISO_A2") or "-99") != "-99" else 1


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["features"]


def build_fine(features50):
    """Fine map: shapes only, 1:50m, same keys/rules as the base map (overseas parts out of ``d``). Small states that are
    only points in the base map have their shape here."""
    shapes = {}
    for f in sorted(features50, key=own_code_first):
        c = _code(f["properties"])
        if not c:
            continue
        d = _path(f["geometry"], TOLERANCE_FINE, MIN_AREA_FINE, 2)
        if d:
            shapes[c] = shapes.get(c, "") + d
    fine = {c: {"d": d} for c, d in sorted(shapes.items())}
    refine(fine, {}, SAMPLE_POINTS, labels=False)
    return fine


def _excluder(path):
    """A predicate for the names listed in the file at ``path`` (whole words, any case); None means nothing is excluded."""
    if not path:
        return lambda name: False
    words = [w.strip() for w in Path(path).read_text(encoding="utf-8").splitlines() if w.strip() and not w.lstrip().startswith("#")]
    rx = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(re.escape(w) for w in words), re.I) if words else None
    return lambda name: bool(rx and rx.search(name))


def build_places(features, exclude=None):
    """Orientation places: [x, y, name, ISO-A2, rank], rank 0 capital/world city ... 3 small (from scalerank)."""
    skip = _excluder(exclude)
    out = []
    for f in features:
        p = f["properties"]
        sr = p.get("scalerank")
        capital = (p.get("featurecla") or "").startswith("Admin-0 capital") or p.get("adm0cap") == 1
        if sr is None or not (capital or sr <= PLACE_MAX_SCALERANK):
            continue
        rank = 0 if (capital or sr <= 1) else 1 if sr <= 3 else 2 if sr <= 5 else 3
        name = (p.get("name") or "").strip()
        if not name or len(name) > PLACE_NAME_MAX:
            name = (p.get("nameascii") or name).strip()
        cc = (p.get("iso_a2") or "").strip()
        if not cc or cc == "-99":
            cc = PLACE_CODE_FALLBACK.get(p.get("adm0_a3"), "")
        if not name or not cc or skip(name) or skip((p.get("nameascii") or "")):
            continue
        x, y = project(p["longitude"], p["latitude"])
        out.append([round(x, 2), round(y, 2), name, cc, rank])
    out.sort(key=lambda o: (o[4], o[2], o[0], o[1]))
    return out


def main(path110, path50, path_places=None, exclude=None):
    f110, f50 = _load(path110), _load(path50)
    # Shapes: one code, several features -> concatenate (main feature with its own ISO_A2 first;
    # ISO_A2_EH resolves France, Norway, Kosovo and similar -99 cases)
    shapes = {}
    for f in sorted(f110, key=own_code_first):
        c = _code(f["properties"])
        if not c:
            continue
        d = _path(f["geometry"])
        if d:
            shapes[c] = shapes.get(c, "") + d
    # Label points from 50m
    labels = {}
    for f in sorted(f50, key=own_code_first):
        p = f["properties"]
        c = _code(p)
        if not c or c in labels:
            continue
        lon = p.get("LABEL_X")
        lat = p.get("LABEL_Y")
        if lon is None or lat is None:
            ring = f["geometry"]["coordinates"]
            while isinstance(ring[0][0], list):
                ring = ring[0]
            lon = sum(q[0] for q in ring) / len(ring)
            lat = sum(q[1] for q in ring) / len(ring)
        x, y = project(lon, lat)
        labels[c] = (round(x, 1), round(y, 1))
    # Shapes without a 50m entry are dropped
    countries = {c: {"d": d, "x": labels[c][0], "y": labels[c][1]} for c, d in shapes.items() if c in labels}
    points = {c: [labels[c][0], labels[c][1]] for c in labels if c not in countries}
    refine(countries, points)
    write(countries, points)
    if path_places:
        write_fine(build_fine(f50))
        write_places(build_places(_load(path_places), exclude))


def write(countries, points):

    header = (
        "/* Offline world base map - GENERATED, do not edit by hand.\n"
        " * Source: Natural Earth (public domain), shapes 1:110m, label points 1:50m.\n"
        " * Projection: Natural Earth I (Savric et al. 2011), viewBox 0 0 %d %d, latitudes -58 to 84 degrees.\n"
        " * Simplified (Douglas-Peucker %.1f), coordinates rounded to 0.1, relative path commands (fill rule evenodd).\n"
        " * Keys are ISO 3166-1 alpha-2 codes. `countries`: shape `d` + label point, `o` = far overseas parts (never lit with\n"
        " * the country); `points`: microstates and territories without a shape ([x, y]).\n"
        " * Regenerate with: python3 -I scripts/build_worldmap.py <ne_110m_admin_0_countries.geojson> <ne_50m_admin_0_countries.geojson>\n"
        " */\n" % (WIDTH, HEIGHT, TOLERANCE))
    js = (
        header + "(function () {\n  'use strict';\n  window.Lanloom = window.Lanloom || {};\n"
        "  const XM = %r, YO = %r, S = %r;\n" % (_X_MAX, _Y_TOP, round(SCALE, 10))
        + "  window.Lanloom.worldmap = {\n    w: %d, h: %d,\n    countries: %s,\n    points: %s,\n"
        % (WIDTH, HEIGHT, json.dumps(countries, ensure_ascii=True, separators=(",", ":")),
           json.dumps(points, separators=(",", ":")))
        + "    project(lon, lat) {\n"
        "      const l = lon * Math.PI / 180, p = lat * Math.PI / 180, p2 = p * p, p4 = p2 * p2;\n"
        "      const x = l * (0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4)));\n"
        "      const y = p * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4)));\n"
        "      return [(x + XM) * S, (YO - y) * S];\n    },\n  };\n})();\n")
    OUT.write_text(js, encoding="utf-8")
    print("h=%d countries=%d points=%d bytes=%d" % (HEIGHT, len(countries), len(points), len(js.encode())))


FINE_HEADER = (
    "/* Fine world map for high zoom levels - GENERATED, do not edit by hand.\n"
    " * Source: Natural Earth (public domain), countries 1:50m. Projection as in worldmap.js (Natural Earth I, viewBox 0 0 %d %d).\n"
    " * Simplified (Douglas-Peucker %.2f, parts smaller than %.2f are dropped), coordinates rounded to 0.01, relative path\n"
    " * commands (fill rule evenodd). Same keys and rules as worldmap.js (ISO-A2, `o` = far overseas parts); small states that\n"
    " * are only points there have their shape here. No label points (those are in worldmap.js).\n"
    " * Regenerate with: python3 -I scripts/build_worldmap.py <ne_110m_admin_0_countries.geojson> <ne_50m_admin_0_countries.geojson> <ne_10m_populated_places_simple.geojson>\n"
    " */\n")
PLACES_HEADER = (
    "/* Orientation places for the world map - GENERATED, do not edit by hand.\n"
    " * Source: Natural Earth (public domain), populated places 1:10m (capitals and places up to scalerank %d).\n"
    " * Row: [x, y, name, ISO-A2, rank] - x/y in map units of worldmap.js (0.01), rank 0 capital/world city, 1 large,\n"
    " * 2 medium, 3 small (from scalerank). Sorted by rank, then name.\n"
    " * Regenerate with: python3 -I scripts/build_worldmap.py <ne_110m_admin_0_countries.geojson> <ne_50m_admin_0_countries.geojson> <ne_10m_populated_places_simple.geojson>\n"
    " */\n")


def render_fine(fine):
    return (FINE_HEADER % (WIDTH, HEIGHT, TOLERANCE_FINE, MIN_AREA_FINE)
            + "(function () {\n  'use strict';\n  window.Lanloom = window.Lanloom || {};\n"
            "  window.Lanloom.worldmapFine = { countries: %s };\n})();\n"
            % json.dumps(fine, ensure_ascii=True, separators=(",", ":")))


def render_places(places):
    rows = ",\n".join(json.dumps(o, ensure_ascii=True, separators=(",", ":")) for o in places)
    return (PLACES_HEADER % PLACE_MAX_SCALERANK
            + "(function () {\n  'use strict';\n  window.Lanloom = window.Lanloom || {};\n"
            "  window.Lanloom.worldplaces = [\n" + rows + "\n  ];\n})();\n")


def write_fine(fine):
    js = render_fine(fine)
    OUT_FINE.write_text(js, encoding="utf-8")
    print("fine: shapes=%d with-overseas=%d bytes=%d" % (len(fine), sum(1 for v in fine.values() if v.get("o")), len(js.encode())))


def write_places(places):
    js = render_places(places)
    OUT_PLACES.write_text(js, encoding="utf-8")
    print("places: %d bytes=%d" % (len(places), len(js.encode())))


def refine_current():
    """--refine: apply refine() to the generated file in place (the shapes themselves stay as they are)."""
    src = OUT.read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    i = src.index("countries: ") + len("countries: ")
    countries, _ = dec.raw_decode(src[i:])
    j = src.index("points: ") + len("points: ")
    points, _ = dec.raw_decode(src[j:])
    for c in countries.values():          # start from the unrefined state, so the step can run again
        if c.get("o"):
            c["d"] += c.pop("o")
    refine(countries, points)
    write(countries, points)


if __name__ == "__main__":
    if sys.argv[1:] == ["--refine"]:
        refine_current()
    else:
        args = sys.argv[1:]
        exclude = None
        if "--exclude-places" in args:
            i = args.index("--exclude-places")
            if i != len(args) - 2 or len(args[:i]) != 3:
                sys.exit(__doc__)
            exclude = args[i + 1]
            args = args[:i]
        if len(args) not in (2, 3):
            sys.exit(__doc__)
        main(*args, exclude=exclude)
