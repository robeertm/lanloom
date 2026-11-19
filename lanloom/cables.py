"""Real cables (DW3): sea and land telecommunication cables from OpenStreetMap for the data paths map.

Source: OpenStreetMap (c) OpenStreetMap contributors, Open Database License 1.0. The cable file
``lanloom/data/cables-osm.json.gz`` is a derived database and stays under the ODbL (see ``lanloom/data/CABLES-LICENSE.md``);
it is a separate file next to the code, never mixed into it. It is built at build time only, by ``scripts/build_cables.py``
(Overpass queries below); the app never downloads cable data by itself. An administrator may import an Overpass answer of
their own (off until used; nothing is fetched for that either: the file comes from the administrator's browser).

OpenStreetMap is incomplete: not every cable is mapped, many have no name. The view says so ("incomplete - community-mapped"),
never "all cables". Which cable a measured route used is always an estimate (``cableroute.py``).

This module is the one place of the logic (standard library only): the build script and the app both use it.

File format ``lanloom-cables/1`` (JSON, gzip):
  {"format", "notice", "license", "license_url", "attribution", "attribution_url", "changes", "date", "queries",
   "counts": {"sea", "land", "named_sea", "named_land"},
   "sea": [{"id", "n": name|None, "op": operator|None, "d": path, "km": length, "ends": [[lon, lat], [lon, lat]]}, ...],
   "land": [...]}
  ``d``: path commands in the units of the world map (Natural Earth I, viewBox width 1000, latitudes up to 84 degrees north),
  absolute start, relative continuation, rounded to 0.01. ``id``: "w" + the smallest OpenStreetMap way id of the cable (stable
  between builds as long as that way exists).
"""
import gzip
import hashlib
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Optional

FORMAT = "lanloom-cables/1"
DATA_FILE = Path(__file__).resolve().parent / "data" / "cables-osm.json.gz"
IMPORT_NAME = "cables-import.json.gz"
ATTRIBUTION = "© OpenStreetMap contributors"
ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"
LICENSE = "ODbL 1.0"
LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
NOTICE = ("Derived database of OpenStreetMap data (c) OpenStreetMap contributors, made available under the Open Database "
          "License 1.0 (https://opendatacommons.org/licenses/odbl/1-0/). This file stays under the ODbL; the program code "
          "next to it does not. Incomplete - community-mapped: not every cable is in OpenStreetMap, many have no name.")
CHANGES = ("Telecommunication cables only (power, pipeline, disused and decommissioned ways and retired cable systems left out); "
           "ways of the same name bundled into one cable; unnamed sea pieces under 5 km and unnamed land lines without an "
           "operator or under 100 km left out, land cables under 30 km left out; lines simplified (Douglas-Peucker, 0.05 map "
           "units), projected onto the world map and rounded to 0.01.")
# Overpass queries (build time only, scripts/build_cables.py)
QUERIES = (
    ("sea", '[out:json][timeout:240];way["communication"="line"]["location"~"underwater|submarine"];out tags geom;'),
    ("seamark", '[out:json][timeout:240];way["seamark:type"="cable_submarine"];out tags geom;'),
    ("land", '[out:json][timeout:240];(way["communication"="line"]["location"!~"underwater|submarine"]["name"];'
             'way["communication"="line"]["location"!~"underwater|submarine"]["operator"];'
             'way["telecom"="line"]["location"!~"underwater|submarine"]["name"];);out tags geom;'),
)
MIN_SEA = 200                    # a build with fewer sea cables is a broken answer (the bundled file has many more)
MIN_SEA_IMPORT = 1               # an administrator's own import may be a region
MAX_IMPORT_BYTES = 40 * 1024 * 1024
MAX_ENTRIES = 6000
TOLERANCE = 0.05                 # Douglas-Peucker in map units
DECIMALS = 2
MIN_KM_UNNAMED_SEA = 5.0         # short unnamed coastal pieces are left out
MIN_KM_LAND_UNNAMED = 100.0      # unnamed land lines only with an operator and at least this long
MIN_KM_LAND = 30.0               # land cables shorter than this are local lines, not backbones
LAND_LOCATIONS = ("underground", "overhead", "overground", "air", "buried", "surface", "aerial")
# no power, supply or disused lines: OpenStreetMap carries over a thousand power sea cables and dozens of retired
# telecommunication cables under the same tags - they must never become a candidate of the estimate
CATEGORY_OUT = ("power", "transmission", "mooring", "ferry", "sensor", "tunnel", "disused", "pipeline")
LIFECYCLE_OUT = ("disused", "abandoned", "razed", "removed", "demolished", "dismantled", "destroyed", "historic")
STATUS_OUT = ("not_in_use", "disused", "abandoned", "decommissioned", "removed")
RETIRED_NAME = re.compile(r"^\s*(P?TAT|CANTAT|TAT\s*-?\s*\d)", re.I)    # every TAT, PTAT and CANTAT system is retired


def out_of_service(tags) -> bool:
    """True for ways that are no (working) telecommunication cable: power (power=*, voltage, category power/transmission ...),
    disused (disused=yes, disused:communication, status not_in_use ...) or known retired systems."""
    if not isinstance(tags, dict):
        return False
    for k, v in tags.items():
        kl, vl = str(k).lower(), str(v).lower()
        if kl == "power" or kl.endswith(":power") or kl == "voltage" or kl.startswith("voltage:"):
            return True
        if kl.split(":")[0] in LIFECYCLE_OUT and vl not in ("no", "0", "false"):
            return True
        if kl.endswith(":category") and any(x in vl for x in CATEGORY_OUT):
            return True
        if (kl.endswith(":status") or kl in ("status", "operational_status")) and vl in STATUS_OUT:
            return True
    name = tags.get("name") or tags.get("seamark:name") or tags.get("seamark:cable_submarine:name") or ""
    return bool(RETIRED_NAME.match(str(name)))


# ---------------------------------------------------------------------------------------------------------- geometry
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
    """The formula and scale of scripts/build_worldmap.py (Natural Earth I, width 1000, cut at 84 degrees north)."""
    x, y = _raw(lon, lat)
    return (x + _X_MAX) * SCALE, (_Y_TOP - y) * SCALE


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def _length_km(pts):
    return sum(distance_km(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:]))


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
            d = math.hypot(px - ax, py - ay) if n == 0 else abs(dx * (ay - py) - (ax - px) * dy) / n
            if d > dmax:
                dmax, idx = d, i
        if dmax > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def _num(v):
    t = ("%.*f" % (DECIMALS, v)).rstrip("0").rstrip(".")
    if t in ("-0", ""):
        t = "0"
    return t[1:] if t.startswith("0.") else ("-" + t[2:] if t.startswith("-0.") else t)


def path_text(parts):
    """Polylines (lists of (x, y) in map units) -> "Mx yl dx dy ...Mx y l ..." (absolute start, relative continuation)."""
    f = 10 ** DECIMALS
    out = []
    for pts in parts:
        q = []
        for x, y in pts:
            p = (round(x * f), round(y * f))
            if not q or p != q[-1]:
                q.append(p)
        if len(q) < 2:
            continue
        s = "M" + _num(q[0][0] / f) + " " + _num(q[0][1] / f) + "l"
        first = True
        for (x0, y0), (x1, y1) in zip(q, q[1:]):
            for t in (_num((x1 - x0) / f), _num((y1 - y0) / f)):
                s += t if (t.startswith("-") or first) else " " + t
                first = False
        out.append(s)
    return "".join(out)


def _valid(lon, lat):
    return (isinstance(lon, (int, float)) and isinstance(lat, (int, float)) and not isinstance(lon, bool)
            and not isinstance(lat, bool) and -180 <= lon <= 180 and -90 <= lat <= 90)


def _parts(points):
    """Points [(lon, lat)] -> projected polylines, split at the date line (+-180 degrees, the crossing point is added so
    both halves reach the edge of the map), simplified."""
    parts, cur, prev = [], [], None
    for lon, lat in points:
        if prev is not None and abs(lon - prev[0]) > 180:
            east = prev[0] > 0
            l2 = lon + (360 if east else -360)
            t = ((180 if east else -180) - prev[0]) / (l2 - prev[0])
            cl = prev[1] + t * (lat - prev[1])
            cur.append(project(180 if east else -180, cl))
            parts.append(cur)
            cur = [project(-180 if east else 180, cl)]
        cur.append(project(lon, lat))
        prev = (lon, lat)
    parts.append(cur)
    return [_dp(p, TOLERANCE) for p in parts if len(p) >= 2]


# ---------------------------------------------------------------------------------------------------------- from Overpass
def _text(tags, *keys):
    for k in keys:
        v = tags.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f<>]", "", v)).strip()[:80] or None
    return None


def ways_from_answer(raw) -> list:
    """Overpass answer (bytes, text or dict) -> [{"id", "n", "ref", "op", "sea", "pts": [(lon, lat), ...]}]; broken input
    gives [] (never an exception)."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        d = json.loads(raw) if isinstance(raw, str) else raw
        elements = d.get("elements") if isinstance(d, dict) else None
    except (ValueError, AttributeError, UnicodeDecodeError, RecursionError):
        return []
    if not isinstance(elements, list):
        return []
    out = []
    for e in elements:
        if not isinstance(e, dict) or e.get("type") != "way" or not isinstance(e.get("geometry"), list):
            continue
        tags = e.get("tags") if isinstance(e.get("tags"), dict) else {}
        if out_of_service(tags):
            continue
        if not (tags.get("communication") == "line" or tags.get("telecom") == "line" or tags.get("seamark:type") == "cable_submarine"):
            continue
        pts = [(float(g["lon"]), float(g["lat"])) for g in e["geometry"] if isinstance(g, dict) and _valid(g.get("lon"), g.get("lat"))]
        if len(pts) < 2:
            continue
        loc = str(tags.get("location") or "").lower()
        sea = loc in ("underwater", "submarine") or (tags.get("seamark:type") == "cable_submarine" and loc not in LAND_LOCATIONS)
        wid = e.get("id") if isinstance(e.get("id"), int) and not isinstance(e.get("id"), bool) else None
        out.append({"id": wid, "n": _text(tags, "name", "seamark:name"), "ref": _text(tags, "ref"), "op": _text(tags, "operator"),
                    "sea": sea, "pts": pts})
    return out


def _ends(ways):
    """The two free ends of a bundle that lie furthest apart (points where two pieces meet do not count)."""
    count = {}
    for w in ways:
        for p in (w["pts"][0], w["pts"][-1]):
            k = (round(p[0], 4), round(p[1], 4))
            count.setdefault(k, [p, 0])[1] += 1
    free = [v[0] for v in count.values() if v[1] == 1] or [v[0] for v in count.values()]
    free = free[:120]
    if len(free) == 1:
        return [[round(free[0][0], 3), round(free[0][1], 3)]] * 2
    best, pair = -1.0, (free[0], free[-1])
    for i, a in enumerate(free):
        for b in free[i + 1:]:
            d = distance_km(a[1], a[0], b[1], b[0])
            if d > best:
                best, pair = d, (a, b)
    return [[round(pair[0][0], 3), round(pair[0][1], 3)], [round(pair[1][0], 3), round(pair[1][1], 3)]]


def _entry(name, ways):
    parts = []
    for w in ways:
        parts += _parts(w["pts"])
    d = path_text(parts)
    if not d:
        return None
    ids = [w["id"] for w in ways if w.get("id") is not None]
    ops = sorted({w["op"] for w in ways if w.get("op")})
    return {"id": ("w%d" % min(ids)) if ids else None, "n": name, "op": ", ".join(ops)[:80] if ops else None, "d": d,
            "km": int(round(sum(_length_km(w["pts"]) for w in ways))), "ends": _ends(ways)}


def bundle(ways) -> tuple:
    """Ways -> (sea, land): the same name (or reference) is one cable with several pieces; without a name one entry per way.
    Left out: unnamed sea pieces under 5 km; unnamed land lines without operator or under 100 km; land cables under 30 km.
    The same way id counts once."""
    seen, groups, single = set(), {}, {"sea": [], "land": []}
    for w in ways:
        if w.get("id") is not None:
            if w["id"] in seen:
                continue
            seen.add(w["id"])
        kind = "sea" if w["sea"] else "land"
        name = w["n"] or w["ref"]
        if name:
            groups.setdefault((kind, name.casefold()), (name, []))[1].append(w)
        else:
            km = _length_km(w["pts"])
            if kind == "sea" and km < MIN_KM_UNNAMED_SEA:
                continue
            if kind == "land" and not (w["op"] and km >= MIN_KM_LAND_UNNAMED):
                continue
            single[kind].append(w)
    out = {"sea": [], "land": []}
    for (kind, _), (name, ws) in groups.items():
        e = _entry(name, ws)
        if e:
            out[kind].append(e)
    for kind in ("sea", "land"):
        for w in single[kind]:
            e = _entry(None, [w])
            if e:
                out[kind].append(e)
    out["land"] = [e for e in out["land"] if e["km"] >= MIN_KM_LAND]
    used = set()
    for kind in ("sea", "land"):
        out[kind].sort(key=lambda e: (e["n"] is None, (e["n"] or "").casefold(), -e["km"], e["d"][:30]))
        for i, e in enumerate(out[kind]):
            if e["id"] is None or e["id"] in used:
                e["id"] = "%s%d" % ("s" if kind == "sea" else "l", i)
            used.add(e["id"])
    return out["sea"], out["land"]


def build(ways, date: str) -> dict:
    sea, land = bundle(ways)
    return {"format": FORMAT, "notice": NOTICE, "license": LICENSE, "license_url": LICENSE_URL, "attribution": ATTRIBUTION,
            "attribution_url": ATTRIBUTION_URL, "changes": CHANGES, "date": date, "queries": [q for _, q in QUERIES],
            "counts": {"sea": len(sea), "land": len(land), "named_sea": sum(1 for e in sea if e["n"]),
                       "named_land": sum(1 for e in land if e["n"])},
            "sea": sea, "land": land}


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PATH = re.compile(r"^M-?[\d.]+ -?[\d.]+l[-\d. ]+(M-?[\d.]+ -?[\d.]+l[-\d. ]+)*$")


def problems(d, minimum: Optional[int] = None) -> list:
    """Checks of a data set: format, licence header, enough sea cables (default MIN_SEA), valid paths and ends. Empty = good."""
    minimum = MIN_SEA if minimum is None else minimum
    out = []
    if not isinstance(d, dict) or d.get("format") != FORMAT:
        return ["format"]
    if d.get("license") != LICENSE or d.get("attribution") != ATTRIBUTION or "Open Database License" not in str(d.get("notice") or ""):
        out.append("license header")
    if not _DATE.match(str(d.get("date") or "")):
        out.append("date")
    sea, land = d.get("sea"), d.get("land")
    if not isinstance(sea, list) or not isinstance(land, list):
        return out + ["lists"]
    if len(sea) < minimum:
        out.append("too few sea cables (%d < %d)" % (len(sea), minimum))
    if len(sea) + len(land) > MAX_ENTRIES:
        out.append("too many entries")
    ids = set()
    for kind, lst in (("sea", sea), ("land", land)):
        for i, e in enumerate(lst):
            where = "%s[%d]" % (kind, i)
            if not isinstance(e, dict):
                out.append(where + " entry")
                continue
            if not isinstance(e.get("d"), str) or not _PATH.match(e["d"]):
                out.append(where + " geometry")
            if not isinstance(e.get("km"), int) or isinstance(e.get("km"), bool) or e["km"] < 0:
                out.append(where + " length")
            en = e.get("ends")
            if not (isinstance(en, list) and len(en) == 2 and all(isinstance(p, list) and len(p) == 2 and _valid(*p) for p in en)):
                out.append(where + " ends")
            for k in ("n", "op"):
                if e.get(k) is not None and (not isinstance(e[k], str) or len(e[k]) > 80):
                    out.append(where + " " + k)
            if not isinstance(e.get("id"), str) or not e["id"] or e["id"] in ids:
                out.append(where + " id")
            ids.add(e.get("id"))
            if len(out) > 20:
                return out
    return out


def encode(d: dict) -> bytes:
    """Data set -> gzip bytes (fixed time stamp, so the same data gives the same file)."""
    raw = json.dumps(d, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return gzip.compress(raw, compresslevel=9, mtime=0)


def decode(blob: bytes) -> Optional[dict]:
    try:
        return json.loads(gzip.decompress(blob).decode("ascii"))
    except (OSError, ValueError, EOFError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------------------------------------- run time
class CableData:
    """The cable data of the app: an administrator's import (if there is one and it is good) or the bundled file.
    Loaded on first use only (the map asks for it in the "Real cables" view); never fetched from the network."""

    def __init__(self, folder: Optional[Path] = None, bundled: Path = DATA_FILE):
        self.folder = Path(folder) if folder else None
        self.bundled = Path(bundled)
        self.lock = threading.Lock()
        self._data = None
        self._blob = None
        self._etag = None
        self._source = None
        self._error = None
        self.version = 0

    def _import_path(self) -> Optional[Path]:
        return self.folder / IMPORT_NAME if self.folder else None

    def _set(self, blob: bytes, d: dict, source: str) -> None:
        self._blob, self._data, self._source = blob, d, source
        self._etag = '"%s"' % hashlib.sha256(blob).hexdigest()[:20]
        self.version += 1

    def _load(self) -> None:
        if self._data is not None:
            return
        imp = self._import_path()
        if imp and imp.is_file():
            try:
                blob = imp.read_bytes()
                d = decode(blob)
                if d is not None and not problems(d, MIN_SEA_IMPORT):
                    self._set(blob, d, "import")
                    return
                self._error = "paths.cables.err.import_broken"
            except OSError:
                self._error = "paths.cables.err.import_broken"
        try:
            blob = self.bundled.read_bytes()
        except OSError:
            self._error = "paths.cables.err.missing"
            return
        d = decode(blob)
        if d is None or problems(d, MIN_SEA):
            self._error = "paths.cables.err.missing"
            return
        self._set(blob, d, "bundled")

    def data(self) -> Optional[dict]:
        with self.lock:
            self._load()
            return self._data

    def blob(self) -> tuple:
        """(gzip bytes, ETag) of the current data set or (None, None)."""
        with self.lock:
            self._load()
            return self._blob, self._etag

    def info(self) -> dict:
        with self.lock:
            self._load()
            d = self._data or {}
            c = d.get("counts") or {}
            return {"available": self._data is not None, "source": self._source, "date": d.get("date"),
                    "sea": c.get("sea", 0), "land": c.get("land", 0), "named_sea": c.get("named_sea", 0),
                    "attribution": ATTRIBUTION, "attribution_url": ATTRIBUTION_URL, "license": LICENSE, "license_url": LICENSE_URL,
                    "error": self._error, "import_max_mb": MAX_IMPORT_BYTES // (1024 * 1024), "etag": self._etag}

    def import_answer(self, raw: bytes, date: str) -> dict:
        """An administrator's own Overpass answer (JSON with ``out geom``) -> checked data set, kept as the import. Raises
        ValueError with a translation key; the current data stays as it was on any error."""
        if not self.folder:
            raise ValueError("paths.cables.err.no_folder")
        if not raw or len(raw) > MAX_IMPORT_BYTES:
            raise ValueError("paths.cables.err.too_large")
        ways = ways_from_answer(raw)
        if not ways:
            raise ValueError("paths.cables.err.no_cables")
        d = build(ways, date)
        if problems(d, MIN_SEA_IMPORT):
            raise ValueError("paths.cables.err.no_cables")
        blob = encode(d)
        target = self._import_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        with self.lock:
            self._error = None
            self._set(blob, d, "import")
        return self.info()

    def reset(self) -> dict:
        """Back to the bundled file (the import is deleted)."""
        imp = self._import_path()
        if imp:
            try:
                imp.unlink()
            except FileNotFoundError:
                pass
        with self.lock:
            self._data = self._blob = self._etag = self._source = None
            self._error = None
            self.version += 1
        return self.info()
