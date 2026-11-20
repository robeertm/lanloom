"""Estimated route over the real cables (DW3): which sea cable did a measured network route probably use?

Pure functions, standard library only, no network, no files. Inputs are plain data: the condensed sections of a network route
(``routes.condense``: kind, cc, place {name, cc, lat, lon, sure}, rtt - never an address) and the cable data set
(``cables.py``, community-mapped, ODbL). The same logic as the template it was adapted from, with English names and
structured reasons (the view words them in every language).

From the line of a cable its landing points (free ends of its pieces) and a small graph are built; the length between two
landing points follows the cable itself (not the whole system length - large systems with many branches would never fit).

A sea crossing lies between two consecutive places of a route when they are in different countries and more than 1500 km
apart, or when one of the two countries is an island (GB, IE, IS, JP, ...: Amsterdam to London is only 360 km, but over
the North Sea). Never inside one country (New York to Los Angeles is 4000 km over land).
A cable fits when
  1. one landing point is near the place before and another near the place after (at most 800 km; for short crossings at
     most 0.8 x the distance, at least 250 km),
  2. the two landing points are at least half as far apart as the places,
  3. the glass path (land to the landing + cable + land) fits the round-trip time: at most delta-RTT/2 x 200 km/ms plus a
     tolerance (max(150 km, half the distance) - hop times vary). Without round-trip times only 1. and 2. count.
Score: the land legs plus a penalty for unnamed pieces. Confidence:
  * "likely" - exactly one cable fits (and latencies exist and both landings are within 400 km of their places),
  * "possible" - several fit about as well ("possible: A or B"), or the one fit is loose,
  * no basis -> no entry (the view says "cable unknown").
Never sure: the field ``sure`` is always False. The reason names places, round-trip times, landing points and lengths.

Path: the polyline (map units) of a route from the first place to the destination - over the places of the route, at sea
crossings with an estimate over the landing points and the cable itself. Parts: {"kind": "cable"|"air", "p": [[x, y], ...]}
("air" = path estimated, straight line).
"""
import heapq
import math
import re

MIN_CROSS_KM = 1500.0
LANDING_MAX_KM = 800.0
LANDING_MAX_FAR_KM = 1100.0        # crossings over 5000 km (places often far inland)
LANDING_MIN_KM = 250.0
NEAR_KM = 400.0                    # "likely" only when both landing points are this near their places (and latencies exist)
KM_PER_MS = 200.0                  # light in glass: about 200 km per ms (out and back -> delta-RTT/2)
TOLERANCE_KM = 150.0
CLEARLY_BETTER_KM = 300.0
UNNAMED_PENALTY_KM = 150.0
GAP_MAP = 6.0                      # pieces whose ends lie at most 6 map units (~40 km) apart count as connected ...
GAP_FACTOR = 2.0                   # ... with double length (a gap in the data is no cable)
LINE_MAX = 400                     # at most this many points per cable line in an answer
ISLANDS = frozenset("GB IE IS JP TW PH ID NZ AU LK MT CY SG BH MV MU MG CU JM DO HT PR TT BS BB FJ PG NC PF RE GL FO CV KM SC BM KY".split())

# ---------------------------------------------------------------------------------------------------------- projection
def _raw(lon, lat):
    l, p = math.radians(lon), math.radians(lat)
    p2 = p * p
    p4 = p2 * p2
    x = l * (0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4)))
    y = p * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4)))
    return x, y


_X_MAX = _raw(180, 0)[0]
SCALE = 1000.0 / (2 * _X_MAX)
_Y_TOP = _raw(0, 84.0)[1]


def project(lon, lat):
    """Natural Earth I, width 1000, cut at 84 degrees north - the formula of the world map and of cables.project."""
    x, y = _raw(lon, lat)
    return (x + _X_MAX) * SCALE, (_Y_TOP - y) * SCALE


def unproject(x, y):
    """Inverse of project (Newton for the latitude) -> (lon, lat)."""
    ry = _Y_TOP - y / SCALE
    rx = x / SCALE - _X_MAX
    p = ry
    for _ in range(25):
        p2 = p * p
        p4 = p2 * p2
        f = p * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4))) - ry
        fd = 1.007226 + p2 * (3 * 0.015085 + p4 * (7 * -0.044475 + 9 * 0.028874 * p2 - 11 * 0.005916 * p4))
        d = f / fd
        p -= d
        if abs(d) < 1e-11:
            break
    p2 = p * p
    p4 = p2 * p2
    t = 0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4))
    return math.degrees(rx / t), math.degrees(p)


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------------------------------------- reading a path
_NUM = re.compile(r"-?(?:\d+\.?\d*|\.\d+)")


def read_path(d):
    """"Mx yl dx dy ..." (absolute start, relative continuation - the format of cables.path_text; also L/l/M/m/H/h/V/v)
    -> [[(x, y), ...], ...]."""
    parts, cur = [], None
    x = y = 0.0
    for m in re.finditer(r"([MmLlHhVvZz])([^MmLlHhVvZz]*)", d or ""):
        cmd, nums = m.group(1), [float(z) for z in _NUM.findall(m.group(2))]
        if cmd in "Zz":
            continue
        if cmd in "Mm":
            if len(nums) < 2:
                continue
            x, y = (nums[0], nums[1]) if cmd == "M" else (x + nums[0], y + nums[1])
            cur = [(x, y)]
            parts.append(cur)
            rest, cmd = nums[2:], ("L" if cmd == "M" else "l")
        else:
            rest = nums
        if cur is None:
            cur = [(x, y)]
            parts.append(cur)
        if cmd in "Ll":
            for i in range(0, len(rest) - 1, 2):
                x, y = (rest[i], rest[i + 1]) if cmd == "L" else (x + rest[i], y + rest[i + 1])
                cur.append((x, y))
        elif cmd in "Hh":
            for v in rest:
                x = v if cmd == "H" else x + v
                cur.append((x, y))
        elif cmd in "Vv":
            for v in rest:
                y = v if cmd == "V" else y + v
                cur.append((x, y))
    return [t for t in parts if len(t) >= 2]


# ---------------------------------------------------------------------------------------------------------- graph of one cable
class CableGraph:
    """One cable as a graph: nodes = ends of its pieces (merged where they touch), edges = pieces (length in km along the
    line) and small gaps (double weight). Landing points = nodes with exactly one edge (free ends)."""

    def __init__(self, entry):
        self.name = entry.get("n") if isinstance(entry.get("n"), str) else None
        self.id = entry.get("id") if isinstance(entry.get("id"), str) else None
        self.km_total = float(entry.get("km") or 0)
        self.nodes = []           # [(x, y)]
        self.edges = {}           # i -> [(j, km, points (from i to j))]
        self.landings = []        # [(lat, lon, node)]
        try:
            parts = read_path(entry.get("d") or "")
        except (ValueError, TypeError):
            parts = []
        # plausible? A path of a few map units that belongs to a length of thousands of km is a dummy -> only the ends
        if parts and self.km_total > 0 and sum(self._km(t) for t in parts) < self.km_total * 0.2:
            parts = []
        for t in parts:
            a, b = self._node(t[0]), self._node(t[-1])
            km = self._km(t)
            self.edges.setdefault(a, []).append((b, km, t))
            self.edges.setdefault(b, []).append((a, km, t[::-1]))
        self._gaps()
        self._join()
        for i, (x, y) in enumerate(self.nodes):
            if len(self.edges.get(i, ())) == 1 and 0.5 < x < 999.5:        # ends at the date line are no landing
                lon, lat = unproject(x, y)
                self.landings.append((lat, lon, i))
        ends = entry.get("ends")
        if isinstance(ends, list) and len(ends) == 2:                       # the two ends of the data set are always landings
            for p in ends:
                try:
                    lon, lat = float(p[0]), float(p[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if not any(abs(lat - l[0]) < 0.05 and abs(lon - l[1]) < 0.05 for l in self.landings):
                    self.landings.append((lat, lon, self._nearest(project(lon, lat))))

    @staticmethod
    def _km(t):
        pts = [unproject(x, y) for x, y in t]
        return sum(distance_km(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:]) if abs(a[0] - b[0]) < 180)

    def _node(self, p):
        for i, q in enumerate(self.nodes):
            if abs(q[0] - p[0]) < 0.02 and abs(q[1] - p[1]) < 0.02:
                return i
        self.nodes.append((p[0], p[1]))
        return len(self.nodes) - 1

    def _nearest(self, p):
        if not self.nodes:
            return None
        best = min(range(len(self.nodes)), key=lambda i: (self.nodes[i][0] - p[0]) ** 2 + (self.nodes[i][1] - p[1]) ** 2)
        q = self.nodes[best]
        return best if math.hypot(q[0] - p[0], q[1] - p[1]) < GAP_MAP * 2 else None

    def _link(self, i, j):
        a, b = unproject(*self.nodes[i]), unproject(*self.nodes[j])
        km = (distance_km(a[1], a[0], b[1], b[0]) if abs(a[0] - b[0]) < 180 else 0.0) * GAP_FACTOR
        self.edges.setdefault(i, []).append((j, km, [self.nodes[i], self.nodes[j]]))
        self.edges.setdefault(j, []).append((i, km, [self.nodes[j], self.nodes[i]]))

    def _gaps(self):
        """Free ends near another node (a gap in the data, the date line) are connected - with double weight."""
        free = [i for i, v in self.edges.items() if len(v) == 1]
        for i in free:
            xi, yi = self.nodes[i]
            best, bd = None, None
            for j in range(len(self.nodes)):
                if j == i or any(k[0] == j for k in self.edges.get(i, [])):
                    continue
                xj, yj = self.nodes[j]
                d = math.hypot(xi - xj, yi - yj)
                if (xi < 0.5 and xj > 999.5) or (xi > 999.5 and xj < 0.5):     # date line: same latitude, other edge
                    d = abs(yi - yj)
                if d <= GAP_MAP and (bd is None or d < bd):
                    best, bd = j, d
            if best is not None:
                self._link(i, best)

    def _join(self):
        """Parts of a cable that are still apart after closing the gaps (the data often holds only the sea pieces of a
        system, not the way over land between them) are joined over the nearest pair of nodes - double weight."""
        if not self.edges:
            return

        def part(start):
            seen, st = {start}, [start]
            while st:
                i = st.pop()
                for j, _, _ in self.edges.get(i, []):
                    if j not in seen:
                        seen.add(j)
                        st.append(j)
            return seen
        for _ in range(200):
            first = part(next(iter(self.edges)))
            rest = [i for i in self.edges if i not in first]
            if not rest:
                return
            best = None
            for i in first:
                xi, yi = self.nodes[i]
                for j in rest:
                    xj, yj = self.nodes[j]
                    d = (xi - xj) ** 2 + (yi - yj) ** 2
                    if best is None or d < best[0]:
                        best = (d, i, j)
            self._link(best[1], best[2])

    def way(self, la, lb):
        """Shortest way along the cable between two landings (entries of self.landings) -> (km, points) or (None, None)."""
        a, b = la[2], lb[2]
        if a is None or b is None or a == b or a not in self.edges or b not in self.edges:
            return None, None
        dist, prev, heap = {a: 0.0}, {}, [(0.0, a)]
        while heap:
            d, i = heapq.heappop(heap)
            if i == b:
                break
            if d > dist.get(i, 1e18):
                continue
            for j, km, pts in self.edges.get(i, []):
                nd = d + km
                if nd < dist.get(j, 1e18):
                    dist[j], prev[j] = nd, (i, pts)
                    heapq.heappush(heap, (nd, j))
        if b not in dist:
            return None, None
        pieces, i = [], b
        while i != a:
            i0, pts = prev[i]
            pieces.append(pts)
            i = i0
        points = []
        for pts in reversed(pieces):
            points.extend(pts if not points else pts[1:])
        return dist[b], points


_GRAPHS = {}


def graphs(data):
    """Graphs of every sea cable of a data set (built once per data set and kept in memory)."""
    sea = data.get("sea") if isinstance(data, dict) else None
    if not isinstance(sea, list):
        return []
    k = (id(sea), len(sea), str(data.get("date")))
    hit = _GRAPHS.get(k)
    # the entry keeps the list itself: while it is cached its id cannot be given to another data set (a freed list's id is reused,
    # and a test data set of the same length and date then got the graphs of the one before)
    g = hit[1] if hit is not None and hit[0] is sea else None
    if g is None:
        g = []
        for e in sea:
            if not isinstance(e, dict):
                continue
            try:
                cg = CableGraph(e)
            except (TypeError, ValueError, KeyError, IndexError, ZeroDivisionError):
                continue
            if cg.landings:
                g.append((e, cg))
        if len(_GRAPHS) > 3:
            _GRAPHS.clear()
        _GRAPHS[k] = (sea, g)
    return g


# ---------------------------------------------------------------------------------------------------------- places of a route
def route_places(sections):
    """Sections of a route -> [(lat, lon, name, rtt, cc)] with a known place, without repeating the same place."""
    out = []
    for s in sections if isinstance(sections, list) else []:
        if not isinstance(s, dict):
            continue
        p = s.get("place")
        try:
            lat, lon = float(p["lat"]), float(p["lon"])
        except (TypeError, KeyError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        cc = p.get("cc") if isinstance(p.get("cc"), str) else (s.get("cc") if isinstance(s.get("cc"), str) else None)
        rtt = s.get("rtt")
        r = float(rtt) if isinstance(rtt, (int, float)) and not isinstance(rtt, bool) and math.isfinite(rtt) else None
        if out and abs(out[-1][0] - lat) < 0.01 and abs(out[-1][1] - lon) < 0.01:
            if out[-1][3] is None and r is not None:
                out[-1] = out[-1][:3] + (r, out[-1][4])
            continue
        out.append((lat, lon, str(p.get("name") or "")[:60], r, cc))
    return out


def sea_crossing(a, b):
    """Two places -> True when a sea probably lies between them (far, or island <-> another country)."""
    air = distance_km(a[0], a[1], b[0], b[1])
    if a[4] and b[4] and a[4] == b[4]:
        return False                       # inside one country over land
    if air > MIN_CROSS_KM:
        return True
    if air < 80 or not a[4] or not b[4]:
        return False
    return a[4] in ISLANDS or b[4] in ISLANDS


def _near_name(lat, lon, places):
    """Nearest known city (router place table) to a landing point - only a hint, at most 150 km, else None."""
    best, bd = None, 150.0
    for o in (places or {}).values() if isinstance(places, dict) else []:
        try:
            d = distance_km(lat, lon, float(o["lat"]), float(o["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if d < bd:
            best, bd = o.get("name"), d
    return best


def _line(points):
    """Points (map units) -> at most LINE_MAX points, rounded to 0.01."""
    if not points:
        return []
    step = max(1, int(math.ceil(len(points) / LINE_MAX)))
    q = points[::step]
    if q[-1] != points[-1]:
        q.append(points[-1])
    return [[round(x, 2), round(y, 2)] for x, y in q]


def estimate_crossing(a, b, data, places=None, at_most=2):
    """One sea crossing (place a -> place b) -> the fitting cables (best first), see the module text. Empty = no basis."""
    air = distance_km(a[0], a[1], b[0], b[1])
    radius = (LANDING_MAX_FAR_KM if air > 5000 else LANDING_MAX_KM) if air > MIN_CROSS_KM else max(LANDING_MIN_KM, min(LANDING_MAX_KM, air * 0.8))
    d_rtt = (b[3] - a[3]) if (a[3] is not None and b[3] is not None) else None
    limit = d_rtt / 2.0 * KM_PER_MS + max(TOLERANCE_KM, air * 0.5) if d_rtt is not None and d_rtt > 0 else None
    cand = []
    for e, g in graphs(data):
        la = min(g.landings, key=lambda l: distance_km(a[0], a[1], l[0], l[1]))
        lb = min(g.landings, key=lambda l: distance_km(b[0], b[1], l[0], l[1]))
        if la is lb:
            continue
        da, db = distance_km(a[0], a[1], la[0], la[1]), distance_km(b[0], b[1], lb[0], lb[1])
        if da > radius or db > radius:
            continue
        apart = distance_km(la[0], la[1], lb[0], lb[1])
        if apart < air * 0.5:
            continue
        km, points = g.way(la, lb)
        if km is None:                                   # no graph (dummy, gap): the whole length, straight between the ends
            km = g.km_total or apart
            points = [project(la[1], la[0]), project(lb[1], lb[0])]
        km = max(km, apart)
        glass = da + km + db
        if limit is not None and glass > limit:
            continue
        score = da + db + (0 if g.name else UNNAMED_PENALTY_KM)
        cand.append((score, e, g, la, lb, da, db, km, points, glass))
    cand.sort(key=lambda t: (t[0], str(t[2].name)))
    chosen = []
    for t in cand:
        if len(chosen) >= max(1, at_most):
            break
        if chosen and (t[0] > cand[0][0] + CLEARLY_BETTER_KM or (t[2].name and t[2].name == chosen[0][2].name)):
            continue
        chosen.append(t)
    tight = bool(chosen) and limit is not None and chosen[0][5] <= NEAR_KM and chosen[0][6] <= NEAR_KM
    confidence = "likely" if len(chosen) == 1 and tight else "possible"
    out = []
    for i, (score, e, g, la, lb, da, db, km, points, glass) in enumerate(chosen):
        basis = "latency" if limit is not None else ("latency_contradicts" if d_rtt is not None else "no_latency")
        out.append({
            "from": a[2] or None, "to": b[2] or None, "id": g.id, "name": g.name, "operator": e.get("op") if isinstance(e.get("op"), str) else None,
            "km": int(round(g.km_total or km)), "cable_km": int(round(km)), "ends": e.get("ends"),
            "landings": [[round(la[1], 3), round(la[0], 3)], [round(lb[1], 3), round(lb[0], 3)]],
            "line": _line(points), "sure": False, "confidence": confidence, "alternative": i > 0,
            "reason": {"basis": basis, "from_rtt": a[3], "to_rtt": b[3], "from_cc": a[4], "to_cc": b[4],
                       "air_km": int(round(air)), "rtt_diff_ms": round(d_rtt, 1) if d_rtt is not None else None,
                       "glass_max_km": int(round(limit)) if limit is not None else None, "glass_km": int(round(glass)),
                       "landing_from": {"near": _near_name(la[0], la[1], places), "km_to_place": int(round(da))},
                       "landing_to": {"near": _near_name(lb[0], lb[1], places), "km_to_place": int(round(db))},
                       "candidates": len(cand)}})
    return out


def estimate(sections, data, at_most=2, places=None):
    """Every sea crossing of a route -> [estimate per fitting cable] (best first, perhaps one alternative). Never sure."""
    if not isinstance(data, dict) or not isinstance(data.get("sea"), list):
        return []
    pl = route_places(sections)
    out = []
    for i, (a, b) in enumerate(zip(pl, pl[1:])):
        if not sea_crossing(a, b):
            continue
        for x in estimate_crossing(a, b, data, places, at_most):
            x["step"] = i
            out.append(x)
    return out


def crossings(sections):
    """Every sea crossing of a route -> [{"step", "from", "to"}] (also those without a fitting cable: "cable unknown")."""
    pl = route_places(sections)
    return [{"step": i, "from": a[2] or None, "to": b[2] or None} for i, (a, b) in enumerate(zip(pl, pl[1:])) if sea_crossing(a, b)]


def path(sections, estimates, start=None):
    """Polyline from the start to the destination (map units): parts {"kind": "cable"|"air", "p": [[x, y], ...]}; a cable
    only at crossings with an estimate (the best one, never the alternative). start = (lon, lat) (else the first place)."""
    pl = route_places(sections)
    pts = []
    if start is not None:
        try:
            pts.append((float(start[1]), float(start[0]), "", None, None))
        except (TypeError, ValueError, IndexError):
            pass
    offset = len(pts)
    for p in pl:
        if pts and abs(pts[-1][0] - p[0]) < 0.01 and abs(pts[-1][1] - p[1]) < 0.01:
            if offset and len(pts) == 1:
                offset = 0                                       # the first place is the start: steps count from here
            continue
        pts.append(p)
    if len(pts) < 2:
        return []
    best = {x["step"]: x for x in (estimates or []) if isinstance(x, dict) and not x.get("alternative") and isinstance(x.get("step"), int)}
    parts = []

    def air(p, q):
        parts.append({"kind": "air", "p": [[round(p[0], 2), round(p[1], 2)], [round(q[0], 2), round(q[1], 2)]]})

    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        pa, pb = project(a[1], a[0]), project(b[1], b[0])
        x = best.get(i - offset)
        if x and x.get("line"):
            ln = x["line"]
            if math.hypot(ln[0][0] - pa[0], ln[0][1] - pa[1]) > math.hypot(ln[-1][0] - pa[0], ln[-1][1] - pa[1]):
                ln = ln[::-1]                                    # the landing near a first
            air(pa, ln[0])
            parts.append({"kind": "cable", "p": ln, "id": x.get("id"), "name": x.get("name")})
            air(ln[-1], pb)
        else:
            air(pa, pb)
    merged = []                                                  # consecutive air pieces become one polyline
    for t in parts:
        if merged and t["kind"] == "air" and merged[-1]["kind"] == "air":
            merged[-1]["p"].append(t["p"][-1])
        else:
            merged.append(t)
    return merged
