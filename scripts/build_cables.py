#!/usr/bin/env python3
"""Build the cable file lanloom/data/cables-osm.json.gz (real cables of the data paths map, DW3; stdlib only).

Usage:  python3 -I scripts/build_cables.py [--date YYYY-MM-DD] [--save-raw DIR] [--from-raw DIR] [--out PATH]
        --save-raw DIR   keeps the three Overpass answers (sea.json, seamark.json, land.json) for a rebuild without network
        --from-raw DIR   builds from answers kept before (no network at all)
        --out PATH       output file (default: lanloom/data/cables-osm.json.gz); the checksum goes next to it (.sha256)
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
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lanloom import cables  # noqa: E402

OVERPASS = os.environ.get("LANLOOM_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
AGENT = "lanloom-build-cables/1"
PAUSE_S = 30
TIMEOUT_S = 300
MAX_ANSWER = 80 * 1024 * 1024
MAX_GZ_BYTES = 400 * 1024
MAX_RAW_BYTES = 1600 * 1024


def fetch(query: str, tries: int = 4) -> bytes:
    """One query; a busy server (429, 502, 503, 504) is asked again after a growing pause, at most `tries` times."""
    for n in range(tries):
        req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": query}).encode(),
                                     headers={"User-Agent": AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                blob = r.read(MAX_ANSWER + 1)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 502, 503, 504) or n == tries - 1:
                raise
            print("  server busy (%d), waiting %d s" % (exc.code, PAUSE_S * (n + 2)), flush=True)
            time.sleep(PAUSE_S * (n + 2))
            continue
        if len(blob) > MAX_ANSWER:
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--save-raw")
    ap.add_argument("--from-raw")
    ap.add_argument("--out", default=str(cables.DATA_FILE))
    a = ap.parse_args(argv)
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
    out = Path(a.out)
    digest = hashlib.sha256(blob).hexdigest()
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    if hashlib.sha256(tmp.read_bytes()).hexdigest() != digest:
        tmp.unlink()
        print("FAIL: checksum of the written file differs")
        return 1
    os.replace(tmp, out)
    out.with_name(out.name.replace(".json.gz", "") + ".sha256").write_text("%s  %s\n" % (digest, out.name), encoding="ascii")
    print("written %s (sha256 %s)" % (out, digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
