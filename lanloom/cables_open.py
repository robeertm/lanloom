"""Real cables, open sources besides the community map: a second, separate data file with more cable lines and cable facts.

``lanloom/data/cables-open.json.gz`` holds data that is in the public domain or under CC0, so it may ship next to the code without
any share-alike condition. It is built at build time only (``scripts/build_cables.py``, step ``open``); the app never downloads it.

Sources (each line and each fact carries its source):
  * ``noaa``      Submarine Cables of U.S. waters, NOAA Office for Coastal Management and BOEM (MarineCadastre.gov), published as
                  CC0 1.0. The data set holds cable corridors (polygons around the cable, telecommunication and power mixed). Kept:
                  named cable systems in service or under construction; power, utility, telegraph, research, range and military
                  cables, out-of-service and abandoned ones are left out. The corridor becomes a centre line (two sides of the ring
                  fold into one at the map tolerance). Only the part in U.S. waters is known: an end out at sea is no landing
                  (``part``), only ends near a coast are.
  * ``wikidata``  Facts of submarine communication cables from Wikidata (CC0 1.0): owners or operators, year in service, length,
                  design capacity, countries, and whether the cable was taken out of service. Matched to the lines by name and
                  alias, never by position.

Merge rules (the same at build time and run time, ``merge``):
  * a line of an open source whose name (or the cable it names in Wikidata) is already in the community map is a duplicate and is
    dropped - the community line stays, it is never drawn twice,
  * facts fill gaps only: an operator of the community map is never replaced, owners are added as their own field,
  * a cable that Wikidata lists as out of service leaves the lines (it must never become a candidate of the estimate),
  * every line keeps ``src`` (``osm`` or ``noaa``), a line with facts ``wd`` (the Wikidata item).

File format ``lanloom-cables-open/1`` (JSON, gzip):
  {"format", "notice", "date", "changes", "sources": [{"id", "name", "license", "license_url", "url", "attribution", "date"}],
   "counts": {"sea", "land", "named_sea", "facts", "dup"},
   "sea": [{"id", "n", "op", "d", "km", "ends", "src", "part"?}], "land": [], "meta": [{"wd", "n", "k", "own", "yr", "km", "cap",
   "cc", "end"}]}
  ``k``: the name keys (``name_keys``) of the label and the aliases; ``cap``: design capacity in Tbit/s; ``end``: year out of
  service. Paths, ends and lengths as in ``cables.py``.
"""
import datetime
import json
import math
import re
import sqlite3
import struct
import unicodedata
from typing import Optional

from . import cables

FORMAT = "lanloom-cables-open/1"
DATA_FILE = cables.DATA_FILE.parent / "cables-open.json.gz"
NOTICE = ("Cable lines and cable facts from open sources (public domain / CC0 1.0), kept apart from the community-mapped cable "
          "file. Each line and fact names its source (src, wd). Incomplete: lines of U.S. waters only, facts only for cables "
          "with an entry in the open knowledge base.")
SOURCES = {
    "noaa": {"id": "noaa", "name": "NOAA Office for Coastal Management, BOEM: Submarine Cables (MarineCadastre.gov)",
             "short": "NOAA / BOEM", "license": "CC0 1.0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
             "url": "https://catalog.data.gov/dataset/submarine-cables", "attribution": "NOAA Office for Coastal Management, BOEM"},
    "wikidata": {"id": "wikidata", "name": "Wikidata: submarine communications cables", "short": "Wikidata", "license": "CC0 1.0",
                 "license_url": "https://creativecommons.org/publicdomain/zero/1.0/", "url": "https://www.wikidata.org/wiki/Q506572",
                 "attribution": "Wikidata"},
}
CHANGES = ("NOAA: named cable systems in service or under construction only; power, utility, telegraph, research, range and military "
           "cables, out-of-service and abandoned ones left out; corridors turned into centre lines, simplified (Douglas-Peucker, "
           "0.05 map units), projected onto the world map and rounded to 0.01; lines whose cable the community map already holds "
           "left out. Wikidata: label, aliases, owners or operators, year in service, length, design capacity, countries and the "
           "year out of service of items that are a submarine communications cable; owner lists shortened to 80 characters.")
# build time only (scripts/build_cables.py holds the two addresses; the app never connects to them)
WIKIDATA_QUERY = """SELECT ?i ?label (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
  (GROUP_CONCAT(DISTINCT ?ownerLabel; separator="|") AS ?owners) (GROUP_CONCAT(DISTINCT ?opLabel; separator="|") AS ?ops)
  (MIN(?inc) AS ?inception) (MIN(?svc) AS ?service) (MAX(?lenM) AS ?len) (MAX(?capA) AS ?cap) (SAMPLE(?capU) AS ?capUnit)
  (GROUP_CONCAT(DISTINCT ?iso; separator="|") AS ?cc) (MIN(?end) AS ?ended) WHERE {
  ?i wdt:P31/wdt:P279* wd:Q506572 .
  ?i rdfs:label ?label . FILTER(LANG(?label) = "en")
  OPTIONAL { ?i skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }
  OPTIONAL { ?i wdt:P127 ?owner . ?owner rdfs:label ?ownerLabel . FILTER(LANG(?ownerLabel) = "en") }
  OPTIONAL { ?i wdt:P137 ?op . ?op rdfs:label ?opLabel . FILTER(LANG(?opLabel) = "en") }
  OPTIONAL { ?i wdt:P571 ?inc }
  OPTIONAL { ?i wdt:P729 ?svc }
  OPTIONAL { ?i p:P2043/psn:P2043/wikibase:quantityAmount ?lenM }
  OPTIONAL { ?i p:P2957/psv:P2957 [ wikibase:quantityAmount ?capA ; wikibase:quantityUnit ?capU ] }
  OPTIONAL { ?i wdt:P17/wdt:P297 ?iso }
  OPTIONAL { { ?i wdt:P576 ?end } UNION { ?i wdt:P2669 ?end } }
} GROUP BY ?i ?label"""
CAP_UNITS = {"Q1084321": 1.0, "Q2243141": 0.001, "Q7350781": 0.000001}      # Tbit/s, Gbit/s, Mbit/s -> Tbit/s
MIN_META = 100                    # a build with fewer cable facts is a broken answer
MIN_NOAA = 20                     # ... with fewer lines of U.S. waters as well
MAX_META = 4000
NOAA_STATUS_OUT = ("out of service", "abandoned", "retired", "removed")
NOAA_KIND_OUT = re.compile(r"power|utility|utilities|electric|energy|transmission|\bkv\b|wind|telegraph|hvdc|interconnect|"
                           r"hydrophone|observatory|research|science|scientific|range|missile|monitoring|\bleo\s*15\b|"
                           r"\brsn\b|pacwave|accelerated|multiple cables|telephone cable|xlpe|^<?null>?$", re.I)
_COMPANY = re.compile(r"\b(inc|llc|lp|ltd|limited|corp|corporation|co|company|communications?|telecom\w*|tele\w*|cables?|networks?|"
                      r"group|holdings?|s\.?a\.?s?|a/?s|ag|gmbh|plc|consortium|systems?|technolog\w*|infrastructure|services|"
                      r"international|partners|operators?|owners?|crossing|usa|pacific|atlantic|americas?|telco|telcom|comms|fib(?:re|er)|"
                      r"subsea|digital|media|mobile|wireless|net\w*|data|links?)\b", re.I)


def owner_text(owner) -> Optional[str]:
    """Owner field -> companies only: parts that read like a person's name (two or three capitalised words without any company
    word) are left out, so the data names no private persons."""
    if not owner or owner.casefold() in ("<null>", "null", "none", "unknown"):
        return None
    parts = [p.strip() for p in re.split(r",|;|/| and ", owner) if p.strip()]
    keep = []
    for p in parts:
        words = p.split()
        person = 2 <= len(words) <= 3 and all(re.match(r"^[A-Z][a-z]+\.?$", w) for w in words) and not _COMPANY.search(p)
        if not person:
            keep.append(p)
    return _join(keep)


NOAA_OWNER_OUT = re.compile(r"department of defense|\bnavy\b|university|institution|power|electric|energy|utility", re.I)


# ---------------------------------------------------------------------------------------------------------- names
_DROP = re.compile(r"\b(submarine|undersea|subsea|fib(?:re|er)|optic|optical|cables?|systems?|networks?|segments?|the|"
                   r"communications?)\b")


def name_keys(name) -> set:
    """Name -> the keys it matches under: lower case, accents and punctuation gone, filler words (cable, system, network ...) gone;
    a text in brackets gives a second key without it. Keys shorter than 3 characters do not count."""
    if not isinstance(name, str) or not name.strip():
        return set()
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").casefold().replace("&", " and ")
    out = set()
    for v in (s, re.sub(r"\([^)]*\)", " ", s)):
        k = re.sub(r"[^0-9a-z]+", "", _DROP.sub(" ", v))
        if len(k) >= 3:
            out.add(k)
    return out


def _clean(v, n=80) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f<>]", " ", v)).strip()
    return v[:n] or None


def _join(names, n=80) -> Optional[str]:
    """Names -> "A, B, C" of at most n characters (whole names only, "…" when some were left out)."""
    seen, out = set(), []
    for x in names:
        x = _clean(x)
        if x and x.casefold() not in seen:
            seen.add(x.casefold())
            out.append(x)
    text = ""
    for i, x in enumerate(out):
        t = (text + ", " + x) if text else x
        if len(t) > n - 2:
            return (text + " …") if text else x[:n]
        text = t
    return text or None


# ---------------------------------------------------------------------------------------------------------- Wikidata
def _year(v) -> Optional[int]:
    m = re.match(r"^\+?(\d{4})-", str(v or ""))
    y = int(m.group(1)) if m else None
    return y if y and 1850 <= y <= 2100 else None


def meta_from_answer(raw) -> list:
    """SPARQL answer (bytes, text or dict) of WIKIDATA_QUERY -> [{"wd", "n", "k", "own", "yr", "km", "cap", "cc", "end"}]; broken
    input gives [] (never an exception)."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        d = json.loads(raw) if isinstance(raw, str) else raw
        rows = d["results"]["bindings"]
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError, RecursionError):
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for b in rows[:MAX_META]:
        if not isinstance(b, dict):
            continue

        def val(k):
            v = b.get(k)
            return v.get("value") if isinstance(v, dict) and isinstance(v.get("value"), str) else ""
        m = re.search(r"/(Q\d{1,12})$", val("i"))
        label = _clean(val("label"))
        if not m or not label:
            continue
        aliases = [a for a in val("aliases").split("|") if _clean(a)]
        keys = set()
        for x in [label] + aliases:
            keys |= name_keys(x)
        if not keys:
            continue
        try:
            km = int(round(float(val("len")) / 1000.0)) if val("len") else None
        except ValueError:
            km = None
        cap = None
        try:
            f = CAP_UNITS.get(val("capUnit").rsplit("/", 1)[-1])
            if f and val("cap"):
                cap = round(float(val("cap")) * f, 3) or None
        except ValueError:
            cap = None
        cc = sorted({c.upper() for c in val("cc").split("|") if re.match(r"^[A-Za-z]{2}$", c)})
        own = [x for x in val("owners").split("|") if x] or [x for x in val("ops").split("|") if x]
        out.append({"wd": m.group(1), "n": label, "k": sorted(keys), "own": _join(own),
                    "yr": _year(val("service")) or _year(val("inception")), "km": km if km and 0 < km < 60000 else None,
                    "cap": cap if cap and cap < 10000 else None, "cc": cc[:12], "end": _year(val("ended"))})
    out.sort(key=lambda e: int(e["wd"][1:]))
    return out


def meta_index(meta) -> dict:
    """Name key -> fact record; a key that two different items share is ambiguous and left out."""
    idx, bad = {}, set()
    for m in meta or ():
        if not isinstance(m, dict):
            continue
        for k in m.get("k") or ():
            if k in idx and idx[k].get("wd") != m.get("wd"):
                bad.add(k)
            idx.setdefault(k, m)
    for k in bad:
        idx.pop(k, None)
    return idx


def lookup(idx, name) -> Optional[dict]:
    for k in sorted(name_keys(name)):
        if k in idx:
            return idx[k]
    return None


# ---------------------------------------------------------------------------------------------------------- NOAA (GeoPackage)
def _wkb(m, o=0):
    """Well-known binary at offset o -> ([rings or lines as [(lon, lat)]], new offset); only the geometry types of the data set."""
    bo = "<" if m[o] == 1 else ">"
    t = struct.unpack_from(bo + "I", m, o + 1)[0]
    o += 5
    dim = 2
    if t & 0x80000000:
        dim += 1
    if t & 0x40000000:
        dim += 1
    t &= 0x0FFFFFFF
    if t >= 1000:
        dim = {1: 3, 2: 3, 3: 4}.get(t // 1000, 2)
        t %= 1000
    step = 8 * dim
    if t == 2:                                          # line string
        n = struct.unpack_from(bo + "I", m, o)[0]
        o += 4
        return [[struct.unpack_from(bo + "dd", m, o + i * step) for i in range(n)]], o + n * step
    if t == 3:                                          # polygon: the outer ring only
        nr = struct.unpack_from(bo + "I", m, o)[0]
        o += 4
        rings = []
        for r in range(nr):
            n = struct.unpack_from(bo + "I", m, o)[0]
            o += 4
            if r == 0:
                rings.append([struct.unpack_from(bo + "dd", m, o + i * step) for i in range(n)])
            o += n * step
        return rings, o
    if t in (5, 6):                                     # multi line string, multi polygon
        n = struct.unpack_from(bo + "I", m, o)[0]
        o += 4
        out = []
        for _ in range(n):
            part, o = _wkb(m, o)
            out += part
        return out, o
    raise ValueError("geometry type %d" % t)


def gpkg_geometry(blob) -> list:
    """GeoPackage geometry blob -> lines or outer rings [[(lon, lat)]]; broken input gives []."""
    try:
        b = bytes(blob)
        if b[:2] != b"GP":
            return []
        env = (b[3] >> 1) & 7
        start = 8 + {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[env]
        parts, _ = _wkb(memoryview(b), start)
    except (ValueError, KeyError, IndexError, struct.error, TypeError):
        return []
    return [[(float(x), float(y)) for x, y in p if -180 <= x <= 180 and -90 <= y <= 90] for p in parts if len(p) >= 2]


def centre_line(ring) -> list:
    """A thin corridor (closed ring around a cable) -> the cable line: the two points of the ring that lie furthest apart are the
    ends; one side of the ring between them is the line (both sides are a few dozen metres apart). A ring that is no corridor
    (an area) gives its longest side, the same way."""
    pts = list(ring)
    if len(pts) > 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 2:
        return pts

    def far(i):
        a = pts[i]
        return max(range(len(pts)), key=lambda j: (pts[j][0] - a[0]) ** 2 * math.cos(math.radians(a[1])) ** 2 + (pts[j][1] - a[1]) ** 2)
    i = far(0)
    j = far(i)
    a, b = min(i, j), max(i, j)
    one, two = pts[a:b + 1], pts[b:] + pts[:a + 1]
    return one if len(one) >= len(two) else two[::-1]


def noaa_rows(path) -> list:
    """GeoPackage of the NOAA submarine cables -> [(name, owner, status, geometry blob)] (read only)."""
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        table = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'features' ORDER BY table_name LIMIT 1").fetchone()[0]
        if not re.match(r"^[A-Za-z0-9_]+$", table):
            return []
        cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
        geo = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?", (table,)).fetchone()[0]
        need = ("shortName", "cableSystem", "owner", "status")
        if not all(c in cols for c in need) or geo not in cols:
            return []
        rows = con.execute('SELECT "cableSystem", "shortName", "owner", "status", "%s" FROM %s' % (geo, table)).fetchall()
    finally:
        con.close()
    return [(_clean(cs) or _clean(sn), _clean(ow), _clean(st), g) for cs, sn, ow, st, g in rows]


def noaa_keep(name, owner, status) -> bool:
    if not name or cables.RETIRED_NAME.match(name):
        return False
    if (status or "").casefold() in NOAA_STATUS_OUT:
        return False
    if NOAA_KIND_OUT.search(name) or (owner and (NOAA_OWNER_OUT.search(owner) or NOAA_KIND_OUT.search(owner))):
        return False
    return True


def noaa_ways(rows) -> list:
    """Rows -> ways in the form of cables.ways_from_answer (named, at sea), one per corridor; the name groups them later."""
    out = []
    for name, owner, status, blob in rows:
        if not noaa_keep(name, owner, status):
            continue
        for ring in gpkg_geometry(blob):
            line = centre_line(ring)
            if len(line) >= 2 and cables._length_km(line) >= 1.0:
                out.append({"id": None, "n": name, "ref": None, "op": owner_text(owner),
                            "sea": True, "pts": line})
    return out


def noaa_entries(ways, near=None) -> list:
    """Ways -> cable entries (cables.bundle per name key). The ends: only free ends near land are landings (``near``: a function
    (lon, lat) -> bool, the build script uses the world map and its places; without it every free end counts). A cable whose
    ends all lie out at sea is left out; a cable with an end out at sea is marked ``part`` (only the part in U.S. waters is
    known)."""
    groups = {}
    for w in ways:
        keys = sorted(name_keys(w["n"]))
        if not keys:
            continue
        groups.setdefault(keys[0], []).append(w)
    out = []
    for key, ws in groups.items():
        names = {}
        for w in ws:
            names[w["n"]] = names.get(w["n"], 0) + 1
        name = sorted(names, key=lambda x: (-names[x], len(x), x))[0]
        e = cables._entry(name, [dict(w, n=name) for w in ws])
        if not e:
            continue
        free = _free_ends(ws)
        land = [p for p in free if near(p[0], p[1])] if near else list(free)
        if not land:
            continue
        e["ends"] = _far_pair(land)
        if len(land) < len(free):
            e["part"] = 1
        e["src"] = "noaa"
        out.append(e)
    out.sort(key=lambda e: (e["n"].casefold(), -e["km"]))
    for i, e in enumerate(out):
        e["id"] = "n%d" % i
    return out


def _free_ends(ways) -> list:
    count = {}
    for w in ways:
        for p in (w["pts"][0], w["pts"][-1]):
            k = (round(p[0], 3), round(p[1], 3))
            count.setdefault(k, [p, 0])[1] += 1
    return [v[0] for v in count.values() if v[1] == 1] or [v[0] for v in count.values()]


def _far_pair(points) -> list:
    pts = points[:120]
    if len(pts) == 1:
        p = [round(pts[0][0], 3), round(pts[0][1], 3)]
        return [p, list(p)]
    best, pair = -1.0, (pts[0], pts[-1])
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            d = cables.distance_km(a[1], a[0], b[1], b[0])
            if d > best:
                best, pair = d, (a, b)
    return [[round(pair[0][0], 3), round(pair[0][1], 3)], [round(pair[1][0], 3), round(pair[1][1], 3)]]


# ---------------------------------------------------------------------------------------------------------- build and merge
def build(base, noaa, meta, date: str, source_dates=None) -> dict:
    """The open data set: NOAA entries without the duplicates of ``base`` (the community-mapped data set) and the facts."""
    sd = source_dates or {}
    dup = 0
    keep = []
    have, idx = _base_keys(base, meta)
    for e in noaa or ():
        m = lookup(idx, e["n"])
        if (name_keys(e["n"]) & have["keys"]) or (m and m["wd"] in have["wd"]):
            dup += 1
            continue
        if m and m.get("end") and m["end"] <= int(date[:4]):
            continue
        keep.append(e)
    sources = [dict(SOURCES[s], date=sd.get(s) or date) for s in ("noaa", "wikidata")]
    return {"format": FORMAT, "notice": NOTICE, "date": date, "changes": CHANGES, "sources": sources,
            "counts": {"sea": len(keep), "land": 0, "named_sea": len(keep), "facts": len(meta or ()), "dup": dup},
            "sea": keep, "land": [], "meta": list(meta or ())}


def _base_keys(base, meta):
    idx = meta_index(meta)
    have = {"keys": set(), "wd": set()}
    for kind in ("sea", "land"):
        for e in (base or {}).get(kind) or ():
            if isinstance(e, dict) and e.get("n"):
                have["keys"] |= name_keys(e["n"])
                m = lookup(idx, e["n"])
                if m:
                    have["wd"].add(m["wd"])
    return have, idx


_ID = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
_WD = re.compile(r"^Q\d{1,12}$")


def problems(d, minimum_meta: int = MIN_META, minimum_lines: int = MIN_NOAA) -> list:
    """Checks of an open data set: format, sources with licence, lines like cables.problems, facts. Empty = good."""
    if not isinstance(d, dict) or d.get("format") != FORMAT:
        return ["format"]
    out = []
    src = d.get("sources")
    if not isinstance(src, list) or not src or not all(isinstance(s, dict) and s.get("license") and s.get("license_url") and s.get("id")
                                                       and s.get("name") for s in src):
        out.append("sources")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(d.get("date") or "")):
        out.append("date")
    sea, land, meta = d.get("sea"), d.get("land"), d.get("meta")
    if not isinstance(sea, list) or not isinstance(land, list) or not isinstance(meta, list):
        return out + ["lists"]
    if len(sea) + len(land) < minimum_lines:
        out.append("too few lines (%d < %d)" % (len(sea) + len(land), minimum_lines))
    if len(meta) < minimum_meta:
        out.append("too few facts (%d < %d)" % (len(meta), minimum_meta))
    if len(meta) > MAX_META:
        out.append("too many facts")
    probe = {"format": cables.FORMAT, "license": cables.LICENSE, "attribution": cables.ATTRIBUTION, "notice": "Open Database License",
             "date": d.get("date"), "sea": sea, "land": land}
    out += [p for p in cables.problems(probe, 0) if p not in ("license header",)]
    ids = set()
    for kind in ("sea", "land"):
        for i, e in enumerate(d[kind]):
            if isinstance(e, dict) and e.get("src") not in SOURCES:
                out.append("%s[%d] src" % (kind, i))
            if isinstance(e, dict):
                ids.add(e.get("id"))
    for i, m in enumerate(meta):
        if not isinstance(m, dict) or not _WD.match(str(m.get("wd") or "")) or not isinstance(m.get("k"), list) or not m["k"]:
            out.append("meta[%d]" % i)
            continue
        for k in ("n", "own"):
            if m.get(k) is not None and (not isinstance(m[k], str) or len(m[k]) > 80):
                out.append("meta[%d] %s" % (i, k))
        for k in ("yr", "km", "end"):
            if m.get(k) is not None and (not isinstance(m[k], int) or isinstance(m[k], bool)):
                out.append("meta[%d] %s" % (i, k))
        if len(out) > 20:
            break
    return out


def merge(base, extra, year: Optional[int] = None) -> dict:
    """Community-mapped data set + open data set -> one data set for the view and the estimate (the rules in the module text). The
    licence fields of ``base`` stay (its attribution is shown beside the map); the open sources come in ``sources``."""
    if not isinstance(base, dict):
        return base
    if not isinstance(extra, dict):
        return base
    year = year or datetime.date.today().year
    meta = extra.get("meta") or []
    have, idx = _base_keys(base, meta)
    out = dict(base)
    retired = facts = dup = 0
    for kind in ("sea", "land"):
        lst = []
        for e in base.get(kind) or ():
            if not isinstance(e, dict):
                continue
            e = dict(e)
            e.setdefault("src", "osm")
            m = lookup(idx, e.get("n"))
            if m:
                if m.get("end") and m["end"] <= year:
                    retired += 1
                    continue
                facts += _enrich(e, m)
            lst.append(e)
        ids = {e.get("id") for e in lst}
        for e in extra.get(kind) or ():
            if not isinstance(e, dict) or e.get("id") in ids:
                continue
            m = lookup(idx, e.get("n"))
            if (name_keys(e.get("n")) & have["keys"]) or (m and m["wd"] in have["wd"]):
                dup += 1
                continue
            e = dict(e)
            if m:
                if m.get("end") and m["end"] <= year:
                    retired += 1
                    continue
                facts += _enrich(e, m)
            lst.append(e)
        out[kind] = lst
    c = dict(base.get("counts") or {})
    c.update({"sea": len(out["sea"]), "land": len(out["land"]), "named_sea": sum(1 for e in out["sea"] if e.get("n")),
              "named_land": sum(1 for e in out["land"] if e.get("n")),
              "osm_sea": sum(1 for e in out["sea"] if e.get("src") == "osm"),
              "open_sea": sum(1 for e in out["sea"] if e.get("src") != "osm"),
              "facts": facts, "retired": retired, "dup": dup})
    out["counts"] = c
    out["sources"] = [s for s in extra.get("sources") or () if isinstance(s, dict)]
    out["open_date"] = extra.get("date")
    return out


def _enrich(e, m) -> int:
    """Facts fill gaps only; returns 1 when the entry got facts."""
    e["wd"] = m["wd"]
    for k in ("own", "yr", "cap"):
        if m.get(k) and not e.get(k):
            e[k] = m[k]
    if m.get("km") and not e.get("wkm"):
        e["wkm"] = m["km"]
    return 1


def encode(d) -> bytes:
    return cables.encode(d)


def decode(blob) -> Optional[dict]:
    return cables.decode(blob)
