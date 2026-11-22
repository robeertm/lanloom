#!/usr/bin/env python3
"""Build the cable files of the real cables view (data paths map; stdlib only):
lanloom/data/cables-osm.json.gz (community-mapped cables, ODbL) and lanloom/data/cables-open.json.gz (open sources, CC0).

Usage:  python3 -I scripts/build_cables.py [--step all|osm|open] [--date YYYY-MM-DD] [--save-raw DIR] [--from-raw DIR]
                                           [--out PATH] [--out-open PATH]
        --step           osm: only the community-mapped file; open: only the open file (against the community file there is);
                         all (default): both, the open file against the community file just built
        --save-raw DIR   keeps the answers (sea.json, seamark.json, land.json, noaa.zip, wikidata.json) for a rebuild without network
        --from-raw DIR   builds from answers kept before (no network at all)
        --out PATH       community file (default: lanloom/data/cables-osm.json.gz); the checksum goes next to it (.sha256)
        --out-open PATH  open file (default: lanloom/data/cables-open.json.gz); checksum next to it as well
Open sources (lanloom/cables_open.py): the submarine cables of U.S. waters of NOAA / BOEM (one zip file with a GeoPackage, CC0) and
the facts of submarine communications cables of Wikidata (one SPARQL query, CC0). Both are untrusted data too: the zip is read
in memory with a size limit, the GeoPackage only opened read-only, the query answer only parsed with json.
Source: OpenStreetMap, (c) OpenStreetMap contributors, Open Database License 1.0. The queries (lanloom/cables.py, QUERIES)
run against a public Overpass server at build time only - with a pause between them, because those servers are a shared,
free resource. The app itself never downloads cable data. The answers are untrusted data: only parsed with json.

Checks before anything is replaced (exit code 1, nothing written, on any failure):
  * licence header, format, at least MIN_SEA sea cables, every path and end valid, unique ids (cables.problems),
  * known samples: a transatlantic cable, a cable from Europe towards Asia, a short North Sea crossing,
  * size budget: at most MAX_GZ_BYTES compressed and MAX_RAW_BYTES uncompressed,
  * the written file is read back and its SHA-256 compared before it replaces the old one (atomic rename).
"""
import argparse
import datetime
import hashlib
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lanloom import cables, cables_open, infraroute  # noqa: E402

OVERPASS = os.environ.get("LANLOOM_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
AGENT = "lanloom-build-cables/1"
PAUSE_S = 30
TIMEOUT_S = 300
MAX_ANSWER = 80 * 1024 * 1024
MAX_GZ_BYTES = 400 * 1024
MAX_RAW_BYTES = 1600 * 1024
MAX_OPEN_GZ = 160 * 1024
MAX_OPEN_RAW = 640 * 1024
MAX_ZIP = 64 * 1024 * 1024
MAX_GPKG = 128 * 1024 * 1024
WORLD = ROOT / "lanloom" / "web" / "js" / "worldmap.js"
PLACE_UNITS = 2.5                # ~100 km on the world map
NOAA_URL = "https://marinecadastre.gov/downloads/data/mc/SubmarineCable.zip"
WIKIDATA_URL = "https://query.wikidata.org/sparql"


def fetch(query: str, tries: int = 4, url: str = None, method: str = "POST", limit: int = MAX_ANSWER, accept: str = "application/json") -> bytes:
    """One query (Overpass by default); a busy server (429, 502, 503, 504) or one that stops answering is asked again after a
    growing pause, at most `tries` times."""
    for n in range(tries):
        if method == "GET":
            req = urllib.request.Request((url or OVERPASS) + (("?" + urllib.parse.urlencode({"query": query})) if query else ""),
                                         headers={"User-Agent": AGENT, "Accept": accept})
        else:
            req = urllib.request.Request(url or OVERPASS, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": AGENT, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                blob = r.read(limit + 1)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 502, 503, 504) or n == tries - 1:
                raise
            print("  server busy (%d), waiting %d s" % (exc.code, PAUSE_S * (n + 2)), flush=True)
            time.sleep(PAUSE_S * (n + 2))
            continue
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:     # a full server also just stops answering
            if n == tries - 1:
                raise
            print("  no answer (%s), waiting %d s" % (type(exc).__name__, PAUSE_S * (n + 2)), flush=True)
            time.sleep(PAUSE_S * (n + 2))
            continue
        if len(blob) > limit:
            raise ValueError("answer too large")
        return blob
    raise ValueError("no answer")


def _near(p, lon0, lon1, lat0, lat1):
    return lon0 <= p[0] <= lon1 and lat0 <= p[1] <= lat1


def samples(d) -> list:
    """Known cables that every good build contains (by their landing areas, never by name)."""
    sea = d.get("sea") or []
    out = []

    def has(a, b, km_min, km_max):
        for e in sea:
            p, q = e["ends"]
            if km_min <= e["km"] <= km_max and ((_near(p, *a) and _near(q, *b)) or (_near(q, *a) and _near(p, *b))):
                return True
        return False
    # North America east coast <-> western Europe
    if not has((-80, -60, 34, 46), (-11, 3, 42, 60), 4500, 9000):
        out.append("sample: no transatlantic cable")
    # Europe (Mediterranean or Atlantic coast) <-> South and East Asia
    if not any(e["km"] >= 8000 and any(_near(p, -10, 30, 30, 60) for p in e["ends"]) and any(_near(p, 60, 145, -10, 40) for p in e["ends"]) for e in sea):
        out.append("sample: no cable from Europe towards Asia")
    # a short North Sea crossing (Great Britain <-> the continent)
    if not has((-5, 2, 50, 59), (2.5, 11, 51, 58), 150, 1200):
        out.append("sample: no North Sea crossing")
    return out


def write(blob: bytes, out: Path) -> bool:
    """Write atomically, read back and compare the SHA-256; the checksum file goes next to it."""
    digest = hashlib.sha256(blob).hexdigest()
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    if hashlib.sha256(tmp.read_bytes()).hexdigest() != digest:
        tmp.unlink()
        print("FAIL: checksum of the written file differs")
        return False
    os.replace(tmp, out)
    out.with_name(out.name.replace(".json.gz", "") + ".sha256").write_text("%s  %s\n" % (digest, out.name), encoding="ascii")
    print("written %s (sha256 %s)" % (out, digest))
    return True


def build_osm(a) -> int:
    ways = []
    for i, (name, query) in enumerate(cables.QUERIES):
        if a.from_raw:
            blob = (Path(a.from_raw) / (name + ".json")).read_bytes()
        else:
            if i:
                time.sleep(PAUSE_S)
            print("query %s ..." % name, flush=True)
            blob = fetch(query)
            if a.save_raw:
                Path(a.save_raw).mkdir(parents=True, exist_ok=True)
                (Path(a.save_raw) / (name + ".json")).write_bytes(blob)
        w = cables.ways_from_answer(blob)
        print("  %s: %d ways (%d bytes)" % (name, len(w), len(blob)), flush=True)
        if not w and name != "land":
            print("FAIL: empty answer for %s" % name)
            return 1
        ways += w
        del blob
    d = cables.build(ways, a.date)
    errors = cables.problems(d) + samples(d)
    blob = cables.encode(d)
    raw_len = len(json.dumps(d, ensure_ascii=True, separators=(",", ":")))
    if len(blob) > MAX_GZ_BYTES:
        errors.append("size budget: %d bytes compressed > %d" % (len(blob), MAX_GZ_BYTES))
    if raw_len > MAX_RAW_BYTES:
        errors.append("size budget: %d bytes uncompressed > %d" % (raw_len, MAX_RAW_BYTES))
    if cables.decode(blob) != d:
        errors.append("read back differs")
    c = d["counts"]
    print("sea %d (named %d), land %d (named %d), %d bytes gzip, %d bytes JSON" % (c["sea"], c["named_sea"], c["land"], c["named_land"], len(blob), raw_len))
    if errors:
        print("FAIL:\n  " + "\n  ".join(errors[:30]))
        return 1
    return 0 if write(blob, Path(a.out)) else 1


def noaa_from_zip(blob: bytes) -> list:
    """The zip of the NOAA submarine cables -> rows of cables_open.noaa_rows (the GeoPackage goes into a private temporary folder
    only because sqlite needs a file; nothing in the zip is executed or extracted by its own name)."""
    import tempfile
    import zipfile
    if len(blob) > MAX_ZIP:
        raise ValueError("zip too large")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        items = [i for i in z.infolist() if i.filename.lower().endswith(".gpkg") and 0 < i.file_size <= MAX_GPKG]
        if len(items) != 1:
            raise ValueError("expected one GeoPackage in the zip")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cables.gpkg"
            with z.open(items[0]) as src, open(target, "wb") as dst:
                left = MAX_GPKG
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    left -= len(chunk)
                    if left < 0:
                        raise ValueError("GeoPackage too large")
                    dst.write(chunk)
            return cables_open.noaa_rows(target)


def near_land():
    """(lon, lat) -> True near land: inside or close to a country shape of the world map (infraroute.CountryShapes, about 50 km) or
    at most PLACE_UNITS from one of its places (islands without a shape). An end where a data set of national waters stops out at
    sea (about 370 km off the coast) is no landing."""
    import re
    shapes = infraroute.CountryShapes(infraroute.load_world(WORLD))
    text = (WORLD.parent / "worldplaces.js").read_text(encoding="utf-8")
    places = [(float(x), float(y)) for x, y in re.findall(r"\[(\d+(?:\.\d+)?),(\d+(?:\.\d+)?),\"", text)]
    if not shapes or len(places) < 1000:
        raise ValueError("world map or places missing")

    def near(lon, lat):
        x, y = cables.project(lon, lat)
        if shapes.country_at(x, y):
            return True
        return any(abs(px - x) <= PLACE_UNITS and abs(py - y) <= PLACE_UNITS and math.hypot(px - x, py - y) <= PLACE_UNITS for px, py in places)
    return near


def samples_open(d) -> list:
    """Known facts that every good build of the open file contains (by structure, never by name)."""
    out = []
    meta = d.get("meta") or []
    if sum(1 for m in meta if m.get("own")) < 30:
        out.append("sample: fewer than 30 cables with owners")
    if sum(1 for m in meta if m.get("yr")) < 60:
        out.append("sample: fewer than 60 cables with a year")
    if not any(len(m.get("cc") or ()) >= 2 and m.get("km") and m["km"] >= 4000 for m in meta):
        out.append("sample: no long cable between two countries")
    sea = d.get("sea") or []
    # a cable between the Hawaiian islands lies completely in U.S. waters: two different landings on the islands
    if not any(e["ends"][0] != e["ends"][1] and all(-161 <= p[0] <= -154 and 18 <= p[1] <= 23 for p in e["ends"]) for e in sea):
        out.append("sample: no cable between two Hawaiian islands")
    # a cable that leaves U.S. waters ends at the edge of the data set, not on land: such ends are no landings
    if not any(e.get("part") for e in sea):
        out.append("sample: no cable marked as known only in U.S. waters")
    return out


def build_open(a) -> int:
    raw = Path(a.from_raw) if a.from_raw else None
    if raw:
        zblob = (raw / "noaa.zip").read_bytes()
        wblob = (raw / "wikidata.json").read_bytes()
    else:
        print("download NOAA submarine cables ...", flush=True)
        zblob = fetch(None, url=NOAA_URL, method="GET", limit=MAX_ZIP, accept="application/zip")
        time.sleep(5)
        print("query Wikidata ...", flush=True)
        wblob = fetch(cables_open.WIKIDATA_QUERY, url=WIKIDATA_URL, method="GET", accept="application/sparql-results+json")
        if a.save_raw:
            Path(a.save_raw).mkdir(parents=True, exist_ok=True)
            (Path(a.save_raw) / "noaa.zip").write_bytes(zblob)
            (Path(a.save_raw) / "wikidata.json").write_bytes(wblob)
    rows = noaa_from_zip(zblob)
    meta = cables_open.meta_from_answer(wblob)
    ways = cables_open.noaa_ways(rows)
    entries = cables_open.noaa_entries(ways, near_land())
    print("  noaa: %d rows, %d corridors kept, %d cables; wikidata: %d cables with facts" % (len(rows), len(ways), len(entries), len(meta)), flush=True)
    base = cables.decode(Path(a.out).read_bytes())
    if base is None or cables.problems(base):
        print("FAIL: the community-mapped file %s is missing or broken (build it first)" % a.out)
        return 1
    d = cables_open.build(base, entries, meta, a.date)
    errors = cables_open.problems(d) + samples_open(d)
    blob = cables_open.encode(d)
    raw_len = len(json.dumps(d, ensure_ascii=True, separators=(",", ":")))
    if len(blob) > MAX_OPEN_GZ:
        errors.append("size budget: %d bytes compressed > %d" % (len(blob), MAX_OPEN_GZ))
    if raw_len > MAX_OPEN_RAW:
        errors.append("size budget: %d bytes uncompressed > %d" % (raw_len, MAX_OPEN_RAW))
    if cables_open.decode(blob) != d:
        errors.append("read back differs")
    merged = cables_open.merge(base, d, int(a.date[:4]))
    if cables.problems(merged):
        errors.append("merged data set: " + ", ".join(cables.problems(merged)[:5]))
    c, m = d["counts"], merged["counts"]
    print("open: %d lines (%d already in the community map), %d cable facts, %d bytes gzip, %d bytes JSON" % (c["sea"], c["dup"], c["facts"], len(blob), raw_len))
    print("merged: sea %d (named %d, %d from open sources), land %d; facts on %d lines, %d retired cables left out"
          % (m["sea"], m["named_sea"], m["open_sea"], m["land"], m["facts"], m["retired"]))
    if errors:
        print("FAIL:\n  " + "\n  ".join(errors[:30]))
        return 1
    return 0 if write(blob, Path(a.out_open)) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--step", choices=("all", "osm", "open"), default="all")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--save-raw")
    ap.add_argument("--from-raw")
    ap.add_argument("--out", default=str(cables.DATA_FILE))
    ap.add_argument("--out-open", default=str(cables_open.DATA_FILE))
    a = ap.parse_args(argv)
    if a.step in ("all", "osm"):
        rc = build_osm(a)
        if rc or a.step == "osm":
            return rc
        if not a.from_raw:
            time.sleep(PAUSE_S)
    return build_open(a)


if __name__ == "__main__":
    sys.exit(main())
