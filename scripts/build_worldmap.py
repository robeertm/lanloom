#!/usr/bin/env python3
"""Build the offline world base map used by the Lanloom web UI (stdlib only).

Usage:  python3 -I scripts/build_worldmap.py <ne_110m_admin_0_countries.geojson> <ne_50m_admin_0_countries.geojson>
        python3 -I scripts/build_worldmap.py --refine      (re-apply only the overseas/territory step to the current file)
Writes lanloom/web/js/worldmap.js (path resolved relative to the repository root, independent of the cwd).

Overseas parts: a part of a country far away from its main area (French Guiana in the shape of France, Hawaii in the
shape of the United States) is moved out of ``d``: into its own entry when it lies in a territory with its own ISO code
(TERRITORIES), otherwise into ``o`` (drawn, but never lit together with the country). Territories the geo data knows
but the base map has no shape for get a label point (TERRITORY_POINTS), so traffic to them is shown as a dot.

Source: Natural Earth (public domain). Shapes 1:110m, label points 1:50m. The input files are untrusted data:
they are only read and parsed as JSON, never executed.
Projection: Natural Earth I (Savric et al. 2011), viewBox width 1000, latitudes -58 to 84 degrees.
The JS project(lon, lat) uses the same formula and scale as this script.
"""
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "lanloom" / "web" / "js" / "worldmap.js"
WIDTH = 1000.0
LAT_TOP, LAT_BOTTOM = 84.0, -58.0
TOLERANCE = 0.5     # Douglas-Peucker, in map units
MIN_AREA = 1.5      # smaller parts are dropped (never the largest part of a country)
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


def _num(v):
    t = ("%.1f" % v).rstrip("0").rstrip(".")
    if t in ("-0", ""):
        t = "0"
    return t.replace("-0.", "-.") if t.startswith("-0.") else (t[1:] if t.startswith("0.") else t)


def _ring_path(pts):
    """pts rounded to 0.1 -> 'Mx y l dx dy ... z' (absolute start, relative continuation)."""
    q = [(round(x * 10), round(y * 10)) for x, y in pts]
    d = [q[0]]
    for p in q[1:]:
        if p != d[-1]:
            d.append(p)
    if len(d) > 1 and d[0] == d[-1]:
        d.pop()
    if len(d) < 3:
        return None
    nums = [_num(d[0][0] / 10), _num(d[0][1] / 10)]
    out = "M" + nums[0] + (nums[1] if nums[1].startswith("-") else " " + nums[1]) + "l"
    first = True
    for (x0, y0), (x1, y1) in zip(d, d[1:]):
        for t in (_num((x1 - x0) / 10), _num((y1 - y0) / 10)):
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


def _path(geom):
    rings = _rings(geom)
    if not rings:
        return None
    cand = []
    for pts, outer in rings:
        pts = _dp([(round(x, 3), round(y, 3)) for x, y in pts], TOLERANCE)
        cand.append((_area(pts) if len(pts) >= 3 else 0.0, pts, outer))
    largest = max((k for k in cand if k[2]), key=lambda k: k[0], default=None)
    parts = []
    for area, pts, outer in cand:
        if area < MIN_AREA and not (largest is not None and (area, pts) == (largest[0], largest[1])):
            continue
        p = _ring_path(pts)
        if p:
            parts.append(p)
    if not parts and largest is not None:
        p = _ring_path(largest[1])
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


def _gap(a, b):
    """Smallest distance between two point lists in map units at the equator, measured on the globe (so the two
    sides of the date line are neighbours, e.g. the far east of Russia)."""
    ga = [unproject(x, y) for x, y in a]
    gb = [unproject(x, y) for x, y in b]
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


def refine(countries, points):
    """Overseas parts out of the main shape, own entries for territories with their own code, territory points."""
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
                if any(_gap(r[1], k[1]) <= OVERSEAS_DIST for k in keep):
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
                countries[code] = {"d": r[0], "x": round(cx, 1), "y": round(cy, 1)}
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


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["features"]


def main(path110, path50):
    f110, f50 = _load(path110), _load(path50)

    def own_code_first(f):
        return 0 if (f["properties"].get("ISO_A2") or "-99") != "-99" else 1

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
    elif len(sys.argv) != 3:
        sys.exit(__doc__)
    else:
        main(sys.argv[1], sys.argv[2])
