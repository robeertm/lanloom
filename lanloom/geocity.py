"""Cities and network operators for the data paths view: an option, switched off by default.

Source: "IP to City Lite" and "IP to ASN Lite" by DB-IP (https://db-ip.com), licensed CC BY 4.0. The attribution
"IP Geolocation by DB-IP" is shown in the data paths view, in the README and in THIRD-PARTY-NOTICES.md.

Lanloom's rule is that nothing leaves the house unless the user switches it on. The country file ships inside the image
(``geo.py``); the city file is about 86 MB, so it is NOT part of the image. With *Settings > Data paths > Cities and
network operators* switched on, exactly these two files are fetched from the source, at most one check a day and one
new file a month (never an address, never anything about the installation), and condensed into one compact local file
``<data>/geo/cities-YYYY-MM.bin`` of a few tens of megabytes:

  * the files are read as a stream (a gzip that is cut off or damaged fails at its end, the whole file is never held in
    memory); ranges that follow each other with the same city or operator are merged, the arrays are written to
    temporary files while they grow, so the peak memory of a build is the table of distinct cities and operators,
  * the new file replaces the old one only after it passed the checks (size window of the download, complete gzip,
    enough rows, ascending ranges, well-known samples); the previous file stays as the fallback, older ones are removed,
  * the finished file is mapped read-only (``mmap``), so a lookup costs no heap at all,
  * any failure leaves the old file in place (or no file: the view then says that cities are unavailable, never 0).

File format (all arrays little-endian, every section starts at a multiple of 8 bytes): MAGIC, 4 byte big-endian header
length, JSON header (month, created, sections [name, type, n, off, len] with off relative to the data start, the
number of rows read), padding, sections:
  c4s/c4i, c6s/c6i  city runs: IPv4 start (I) or IPv6 start cut to the upper 64 bits (Q) and the index of the city
                    (I, 0 = no city; gaps and unusable ranges have 0),
  a4s/a4i, a6s/a6i  operator runs: the same with the AS number (I, 0 = none),
  coff, ctxt, clat, clon  city table: offsets (I, n + 1) into the text block "name NUL region NUL country",
                    latitude and longitude of the city centre in hundredths of a degree (h),
  aasn, aoff, atxt  operators: sorted AS numbers (I), offsets (I, n + 1) and the text block of their names.
"""
import bisect
import csv
import gzip
import hashlib
import ipaddress
import json
import logging
import mmap
import os
import shutil
import struct
import sys
import threading
import time
import urllib.request
import zlib
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("lanloom")

MAGIC = b"LLCITY1\n"
URL = "https://download.db-ip.com/free/dbip-{kind}-lite-{month}.csv.gz"
HOST = "download.db-ip.com"
ATTRIBUTION = {"text": "IP Geolocation by DB-IP", "url": "https://db-ip.com", "license": "CC BY 4.0",
               "license_url": "https://creativecommons.org/licenses/by/4.0/"}
CHECK_EVERY_S = 86400          # at most one check a day
FIRST_DAY = 2                  # the source publishes the file of a month on its 1st: one day of margin
KEEP_FILES = 2                 # the newest plus one fallback
MIN_FREE_BYTES = 600_000_000   # free space needed to build (download of both files plus the temporary arrays)
CHUNK = 1 << 16
SPILL_CHUNK = 1 << 15
MAX_TEXT = 80                  # characters of a city key and an operator name that are kept
STATE_FILE = "state.json"


class GeoCityError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    """What a real source file has to look like. Tests use ``Limits.small`` with a tiny check file."""
    city_rows: int = 2_000_000             # rows of the city CSV (a real file has about 7.8 million)
    asn_rows: int = 200_000
    v6_runs: int = 10_000                  # IPv6 runs in the finished arrays
    city_size: tuple = (20_000_000, 150_000_000)     # bytes of the city download
    asn_size: tuple = (1_000_000, 100_000_000)
    # (address, country the city must be in or None, AS number or None, city required)
    samples: tuple = (("8.8.8.8", "US", 15169, True), ("1.1.1.1", None, 13335, True),
                      ("2001:4860:4860::8888", None, 15169, False))

    @staticmethod
    def small(samples: tuple = ()) -> "Limits":
        return Limits(city_rows=1, asn_rows=1, v6_runs=0, city_size=(1, 10_000_000), asn_size=(1, 10_000_000), samples=samples)


def _ip_key(text: str):
    """(family, integer) of an address, IPv6 cut to the upper 64 bits; None for no address."""
    try:
        a = ipaddress.ip_address(str(text).split("/")[0].strip())
    except ValueError:
        return None
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return (4, int(a)) if a.version == 4 else (6, int(a) >> 64)


def wanted_month(now: float) -> str:
    t = time.gmtime(now)
    y, m = t.tm_year, t.tm_mon
    if t.tm_mday < FIRST_DAY:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return "%04d-%02d" % (y, m)


def month_before(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return "%04d-%02d" % (y, m)


# ====================================================================== building (stream in, arrays out)
class Spill:
    """An array that grows on disk: values are written to a temporary file in chunks."""

    def __init__(self, code: str, path: Path):
        self.code, self.path, self.n = code, path, 0
        self.buf = array(code)
        self.f = open(path, "wb")

    def append(self, v) -> None:
        self.buf.append(v)
        self.n += 1
        if len(self.buf) >= SPILL_CHUNK:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            self.buf.tofile(self.f)
            self.buf = array(self.code)

    def close(self) -> int:
        self.flush()
        self.f.close()
        return self.n

    @property
    def nbytes(self) -> int:
        return self.n * array(self.code).itemsize


class Runs:
    """Ascending ranges (start, end, value) become runs (start, value) without gaps: a gap gets the value 0."""

    def __init__(self, fam: int, start: Spill, value: Spill):
        self.fam, self.start, self.value = fam, start, value
        self.last_start, self.last_end, self.cur = -1, -1, None
        self.limit = (1 << 32) if fam == 4 else (1 << 64)

    def _emit(self, start: int, v: int) -> None:
        if self.cur == v:
            return
        self.start.append(start)
        self.value.append(v)
        self.cur = v

    def add(self, a: int, b: int, v: int) -> None:
        if b < a:
            raise GeoCityError("range")
        if a < self.last_start:
            raise GeoCityError("not ascending")
        if a <= self.last_end:
            return                         # finer than /64 (IPv6) or a duplicate: the first one wins
        self.last_start = a
        if a != self.last_end + 1:
            self._emit(self.last_end + 1, 0)
        self._emit(a, v)
        self.last_end = b

    def finish(self) -> None:
        if self.last_end + 1 < self.limit and self.last_end >= 0:
            self._emit(self.last_end + 1, 0)


def _rows(path: Path):
    """CSV rows of a gzip file; a file that is cut off or damaged ends in GeoCityError (the CRC is read at the end)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="") as f:
            yield from csv.reader(f)
    except (OSError, EOFError, gzip.BadGzipFile, csv.Error, zlib.error) as exc:
        raise GeoCityError("gzip or csv: %s" % type(exc).__name__)


def _clean(text: str) -> str:
    return " ".join((text or "").replace("\0", " ").split())[:MAX_TEXT]


class Builder:
    """Builds the file from the two downloaded sources. ``work`` is a folder of its own for the temporary arrays."""

    def __init__(self, work: Path, progress: Optional[Callable[[dict], None]] = None):
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.progress = progress or (lambda p: None)
        self.rows = {"city": 0, "asn": 0}

    def _spill(self, name: str, code: str) -> Spill:
        return Spill(code, self.work / (name + ".part"))

    def read_asn(self, path: Path) -> dict:
        names = {}
        runs = {}
        spills = {}
        for fam in (4, 6):
            spills[fam] = (self._spill("a%ds" % fam, "I" if fam == 4 else "Q"), self._spill("a%di" % fam, "I"))
            runs[fam] = Runs(fam, *spills[fam])
        for z in _rows(path):
            if not z:
                continue
            self.rows["asn"] += 1
            if self.rows["asn"] % 100_000 == 0:
                self.progress({"step": "read", "rows": self.rows["asn"]})
            if len(z) < 3:
                raise GeoCityError("operator row")
            s, e = _ip_key(z[0]), _ip_key(z[1])
            try:
                asn = int(z[2])
            except ValueError:
                raise GeoCityError("operator row")
            if not s or not e or s[0] != e[0] or not (0 < asn < 2 ** 32):
                raise GeoCityError("operator row")
            runs[s[0]].add(s[1], e[1], asn)
            if asn not in names:
                names[asn] = _clean(z[3] if len(z) > 3 else "")
        for fam in (4, 6):
            runs[fam].finish()
        self.asn_spills = spills
        return names

    def read_city(self, path: Path) -> tuple:
        """-> (offsets, text spill, lat, lon, index by key). Cities are numbered in order of appearance, 0 = none."""
        spills = {}
        runs = {}
        for fam in (4, 6):
            spills[fam] = (self._spill("c%ds" % fam, "I" if fam == 4 else "Q"), self._spill("c%di" % fam, "I"))
            runs[fam] = Runs(fam, *spills[fam])
        text = open(self.work / "ctxt.part", "wb")
        off, tlen = array("I", [0]), 0
        lat, lon = array("h", [0]), array("h", [0])
        index = {}                            # 8 byte digest of the city -> number; only this lives in memory
        off.append(0)                         # off = [0, 0, end of city 1, end of city 2 ...]: city i is off[i]..off[i+1]
        n = 0
        for z in _rows(path):
            if not z:
                continue
            self.rows["city"] += 1
            if self.rows["city"] % 100_000 == 0:
                self.progress({"step": "read", "rows": self.rows["city"]})
            if len(z) < 8:
                raise GeoCityError("city row")
            s, e = _ip_key(z[0]), _ip_key(z[1])
            if not s or not e or s[0] != e[0]:
                raise GeoCityError("city row")
            cc = z[3].strip().upper()
            if len(cc) != 2 or not cc.isalpha():
                raise GeoCityError("city row (country)")
            try:
                la, lo = float(z[6]), float(z[7])
            except ValueError:
                raise GeoCityError("city row (coordinates)")
            if not (-90 <= la <= 90 and -180 <= lo <= 180):
                raise GeoCityError("coordinates out of range")
            name, region = _clean(z[5]), _clean(z[4])
            if cc in ("ZZ", "XX") or not name:
                idx = 0
            else:
                digest = int.from_bytes(hashlib.blake2b(("%s\0%s\0%s" % (name, region, cc)).encode("utf-8"),
                                                         digest_size=8).digest(), "big")
                idx = index.get(digest)
                if idx is None:
                    n += 1
                    idx = index[digest] = n
                    rec = ("%s\0%s\0%s" % (name, region, cc)).encode("utf-8")
                    text.write(rec)
                    tlen += len(rec)
                    off.append(tlen)
                    lat.append(round(la * 100))
                    lon.append(round(lo * 100))
            runs[s[0]].add(s[1], e[1], idx)
        text.close()
        for fam in (4, 6):
            runs[fam].finish()
        self.city_spills = spills
        self.city_count = n
        self.city_text = self.work / "ctxt.part"
        return off, lat, lon

    def write(self, target: Path, month: str, city_off, city_lat, city_lon, asn_names: dict) -> None:
        """Assemble the final file from the spilled arrays."""
        for pair in list(self.city_spills.values()) + list(self.asn_spills.values()):
            for sp in pair:
                sp.close()
        sections = []                          # (name, code, n, source) with source a Path or bytes
        for kind, spills in (("c", self.city_spills), ("a", self.asn_spills)):
            for fam in (4, 6):
                st, ix = spills[fam]
                sections.append(("%s%ds" % (kind, fam), st.code, st.n, st.path))
                sections.append(("%s%di" % (kind, fam), ix.code, ix.n, ix.path))
        sections.append(("coff", "I", len(city_off), _bytes(city_off)))
        sections.append(("ctxt", "B", self.city_text.stat().st_size, self.city_text))
        sections.append(("clat", "h", len(city_lat), _bytes(city_lat)))
        sections.append(("clon", "h", len(city_lon), _bytes(city_lon)))
        asns = sorted(asn_names)
        aoff, atxt = array("I", [0]), bytearray()
        for a in asns:
            atxt += asn_names[a].encode("utf-8")
            aoff.append(len(atxt))
        sections.append(("aasn", "I", len(asns), _bytes(array("I", asns))))
        sections.append(("aoff", "I", len(aoff), _bytes(aoff)))
        sections.append(("atxt", "B", len(atxt), bytes(atxt)))
        head, off = [], 0
        for name, code, n, src in sections:
            size = n * array(code).itemsize
            head.append({"name": name, "type": code, "n": n, "off": off, "len": size})
            off += (size + 7) // 8 * 8
        hb = json.dumps({"month": month, "created": int(time.time()), "rows_city": self.rows["city"],
                         "rows_asn": self.rows["asn"], "cities": self.city_count, "operators": len(asns),
                         "sections": head}, separators=(",", ":")).encode("utf-8")
        pre = len(MAGIC) + 4 + len(hb)
        pad = (8 - pre % 8) % 8
        with open(target, "wb") as f:
            f.write(MAGIC + struct.pack(">I", len(hb)) + hb + b" " * pad)
            for (name, code, n, src), h in zip(sections, head):
                if isinstance(src, (bytes, bytearray)):
                    f.write(src)
                else:
                    with open(src, "rb") as g:
                        shutil.copyfileobj(g, f, CHUNK * 16)
                f.write(b"\0" * ((8 - h["len"] % 8) % 8))
            f.flush()
            os.fsync(f.fileno())


def _bytes(a: array) -> bytes:
    if sys.byteorder != "little":
        a = array(a.typecode, a)
        a.byteswap()
    return a.tobytes()


# ====================================================================== reading (mmap, no heap)
class CityFile:
    """The finished file, mapped read-only. ``lookup(ip)`` -> (city number, AS number), 0 = none."""

    def __init__(self, path: Path):
        if sys.byteorder != "little":
            raise GeoCityError("byte order")
        self.path = Path(path)
        try:
            self._f = open(self.path, "rb")
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError) as exc:
            raise GeoCityError("open: %s" % type(exc).__name__)
        try:
            self._parse()
        except Exception:
            self.close()
            raise

    def _parse(self) -> None:
        mm = self._mm
        if mm[:len(MAGIC)] != MAGIC:
            raise GeoCityError("magic")
        try:
            (hl,) = struct.unpack(">I", mm[len(MAGIC):len(MAGIC) + 4])
            head = json.loads(mm[len(MAGIC) + 4:len(MAGIC) + 4 + hl].decode("utf-8"))
            pre = len(MAGIC) + 4 + hl
            base = pre + (8 - pre % 8) % 8
            view = memoryview(mm)
            self._view = view
            self.s = {}
            for sec in head["sections"]:
                off, ln, n, code = int(sec["off"]), int(sec["len"]), int(sec["n"]), sec["type"]
                if code not in ("I", "Q", "h", "B") or ln != n * array(code).itemsize or base + off + ln > len(mm):
                    raise GeoCityError("section %s" % sec.get("name"))
                part = view[base + off:base + off + ln]
                self.s[sec["name"]] = part if code == "B" else part.cast(code)
            self.month = str(head.get("month") or "")
            self.created = head.get("created")
            self.rows = {"city": int(head.get("rows_city") or 0), "asn": int(head.get("rows_asn") or 0)}
            self.cities = int(head["cities"])
            self.operators = int(head["operators"])
            s = self.s
            need = ("c4s", "c4i", "c6s", "c6i", "a4s", "a4i", "a6s", "a6i", "coff", "ctxt", "clat", "clon", "aasn", "aoff", "atxt")
            if any(k not in s for k in need):
                raise GeoCityError("sections")
            if len(s["c4s"]) != len(s["c4i"]) or len(s["c6s"]) != len(s["c6i"]) or len(s["a4s"]) != len(s["a4i"]) \
                    or len(s["a6s"]) != len(s["a6i"]):
                raise GeoCityError("runs")
            if len(s["coff"]) != self.cities + 2 or len(s["clat"]) != self.cities + 1 or len(s["clon"]) != self.cities + 1 \
                    or len(s["aoff"]) != self.operators + 1 or len(s["aasn"]) != self.operators:
                raise GeoCityError("tables")
        except GeoCityError:
            raise
        except (ValueError, KeyError, TypeError, struct.error, UnicodeDecodeError) as exc:
            raise GeoCityError("header: %s" % type(exc).__name__)

    def close(self) -> None:
        try:
            for k in list(getattr(self, "s", {})):
                self.s[k].release()
            self.s = {}
            if getattr(self, "_view", None) is not None:
                self._view.release()
            self._mm.close()
            self._f.close()
        except (ValueError, BufferError, OSError):
            pass

    @staticmethod
    def _find(starts, values, x: int) -> int:
        i = bisect.bisect_right(starts, x) - 1
        return values[i] if i >= 0 else 0

    def lookup(self, ip: str) -> tuple:
        k = _ip_key(ip)
        if not k:
            return 0, 0
        fam, x = k
        s = self.s
        return (self._find(s["c%ds" % fam], s["c%di" % fam], x), self._find(s["a%ds" % fam], s["a%di" % fam], x))

    def city(self, idx: int) -> Optional[dict]:
        s = self.s
        if not (0 < idx <= self.cities):
            return None
        raw = bytes(s["ctxt"][s["coff"][idx]:s["coff"][idx + 1]]).decode("utf-8", "replace")
        parts = raw.split("\0")
        if len(parts) != 3:
            return None
        name, region, cc = parts
        return {"key": ("%s|%s" % (name, region))[:MAX_TEXT], "name": name, "region": region, "cc": cc,
                "lat": s["clat"][idx] / 100.0, "lon": s["clon"][idx] / 100.0}

    def operator(self, asn: int) -> Optional[str]:
        s = self.s
        i = bisect.bisect_left(s["aasn"], asn)
        if i < len(s["aasn"]) and s["aasn"][i] == asn:
            return bytes(s["atxt"][s["aoff"][i]:s["aoff"][i + 1]]).decode("utf-8", "replace")
        return None

    def check(self, limits: Limits) -> None:
        """Plausibility of a freshly built file; raises GeoCityError with the reason."""
        if self.rows["city"] < limits.city_rows or self.rows["asn"] < limits.asn_rows:
            raise GeoCityError("too few rows (cities %d, operators %d)" % (self.rows["city"], self.rows["asn"]))
        s = self.s
        if len(s["c6s"]) < limits.v6_runs or len(s["a6s"]) < limits.v6_runs:
            raise GeoCityError("too few IPv6 runs")
        for name in ("c4s", "c6s", "a4s", "a6s"):
            prev = -1
            for v in s[name]:
                if v <= prev:
                    raise GeoCityError("not ascending (%s)" % name)
                prev = v
        for name in ("c4i", "c6i"):
            if len(s[name]) and max(s[name]) > self.cities:
                raise GeoCityError("city number out of range")
        for ip, cc, asn, need_city in limits.samples:
            idx, a = self.lookup(ip)
            c = self.city(idx)
            if asn is not None and a != asn:
                raise GeoCityError("sample %s: operator" % ip)
            if need_city and not c:
                raise GeoCityError("sample %s: city" % ip)
            if cc and c and c["cc"] != cc:
                raise GeoCityError("sample %s: country" % ip)

    def info(self) -> dict:
        return {"month": self.month, "cities": self.cities, "operators": self.operators, "created": self.created}


# ====================================================================== download
def http_download(url: str, dest: Path, max_bytes: int, progress: Optional[Callable[[int, int], None]] = None) -> None:
    """Streams one file to ``dest``. Only https to the source host, no redirect to anywhere else, size capped."""
    if not url.startswith("https://" + HOST + "/"):
        raise GeoCityError("destination")
    req = urllib.request.Request(url, headers={"User-Agent": "lanloom-geo/1"})
    n = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        final = getattr(r, "url", url) or url
        if not final.startswith("https://" + HOST + "/"):
            raise GeoCityError("redirect")
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            n += len(b)
            if n > max_bytes:
                raise GeoCityError("size")
            f.write(b)
            if progress:
                progress(n, total)


# ====================================================================== the part that lives in the app
class CityData:
    """Holds the current file, switches on and off, checks daily and builds a new file when one is due."""

    def __init__(self, folder: Optional[Path], fetch: Optional[Callable] = None, clock: Callable[[], float] = time.time,
                 limits: Limits = Limits(), min_free: int = MIN_FREE_BYTES):
        self.folder = Path(folder) if folder else None
        self.fetch = fetch or http_download
        self.clock = clock
        self.limits = limits
        self.min_free = min_free
        self.enabled = False
        self.demo = False                        # the sample network: answers "ready" without a file (its figures are synthetic)
        self.file: Optional[CityFile] = None
        self.generation = 0                      # rises when the file changes (caches that depend on it start over)
        self.state = {"phase": "idle", "error": None, "last_check": None, "progress": None, "updated": None}
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.interval = 3600.0
        self.first_delay = 20.0

    @property
    def available(self) -> bool:
        return self.folder is not None

    # -------------------------------------------------------------- files
    def _files(self) -> list:
        if not self.folder:
            return []
        try:
            names = [n for n in os.listdir(self.folder) if n.startswith("cities-") and n.endswith(".bin") and len(n) == 18]
        except OSError:
            return []
        return sorted(names, reverse=True)

    def load(self) -> bool:
        """Map the newest readable file (a damaged one is skipped)."""
        for n in self._files():
            try:
                f = CityFile(self.folder / n)
            except GeoCityError as exc:
                self.state["error"] = "%s: %s" % (n, exc)
                continue
            old, self.file = self.file, f
            self.generation += 1
            if old:
                old.close()
            return True
        return self.file is not None

    def _read_state(self) -> None:
        try:
            d = json.loads((self.folder / STATE_FILE).read_text())
            self.state["last_check"] = float(d.get("last_check")) if d.get("last_check") else None
        except (OSError, ValueError, TypeError):
            pass

    def _write_state(self) -> None:
        try:
            tmp = self.folder / (STATE_FILE + ".tmp")
            tmp.write_text(json.dumps({"last_check": self.state["last_check"]}))
            os.replace(tmp, self.folder / STATE_FILE)
        except OSError:
            pass

    # -------------------------------------------------------------- switch
    def configure(self, enabled: bool, background: bool = True) -> None:
        enabled = bool(enabled) and self.available
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if not enabled:
            self._stop.set()
            self.state.update(phase="idle", progress=None)
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.enabled = False
            self.state["error"] = "folder: %s" % type(exc).__name__
            return
        self._read_state()
        self.load()
        self._stop.clear()
        if background and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._loop, name="lanloom-geocity", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        self._stop.wait(self.first_delay)
        while self.enabled and not self._stop.is_set():
            try:
                self.step()
            except Exception:                    # never lose the thread
                log.exception("city data check failed")
            self._stop.wait(self.interval)

    # -------------------------------------------------------------- lookup (no cost while off)
    def lookup(self, ip: str) -> tuple:
        f = self.file
        if not self.enabled or f is None:
            return 0, 0
        return f.lookup(ip)

    def city(self, idx: int):
        f = self.file
        return f.city(idx) if f else None

    def operator(self, asn: int):
        f = self.file
        return f.operator(asn) if f else None

    # -------------------------------------------------------------- update
    def due(self, now: Optional[float] = None) -> Optional[str]:
        now = self.clock() if now is None else now
        if not (self.enabled and self.available) or self.state["phase"] not in ("idle", "error"):
            return None
        want = wanted_month(now)
        if self.file and self.file.month >= want:
            return None
        lc = self.state["last_check"]
        if lc and now - lc < CHECK_EVERY_S:
            return None
        return want

    def refresh_now(self) -> bool:
        """Forget the daily limit once (an administrator asked): the next step() checks."""
        if not self.enabled or self.state["phase"] not in ("idle", "error"):
            return False
        self.state["last_check"] = None
        return True

    def step(self, now: Optional[float] = None) -> bool:
        """One check; True when a new file was loaded."""
        want = self.due(now)
        if not want:
            return False
        with self._lock:
            self.state.update(phase="download", error=None, last_check=self.clock() if now is None else now, progress=None)
            self._write_state()
            try:
                months = [want] if self.file else [want, month_before(want)]
                last = None
                for m in months:
                    try:
                        self.build(m)
                        self.state.update(phase="idle", error=None, progress=None, updated=self.clock())
                        return True
                    except _error_types() as exc:
                        last = "%s: %s" % (m, str(exc)[:100] if isinstance(exc, GeoCityError) else type(exc).__name__)
                        log.warning("city data %s", last)
                self.state.update(phase="error", error=last, progress=None)
                return False
            finally:
                if self.state["phase"] not in ("idle", "error"):
                    self.state["phase"] = "idle"

    def build(self, month: str) -> None:
        folder = self.folder
        if shutil.disk_usage(folder).free < self.min_free:
            raise GeoCityError("not enough free space")
        work = folder / ("build-" + month)
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        target = folder / ("cities-%s.bin" % month)
        tmp = Path(str(target) + ".tmp")
        try:
            builder = Builder(work, progress=lambda p: self.state.__setitem__("progress", p))
            sources = {}
            for kind, size in (("asn", self.limits.asn_size), ("city", self.limits.city_size)):
                dest = work / (kind + ".csv.gz")
                self.state.update(phase="download", progress={"step": "download", "kind": kind, "bytes": 0, "total": 0})
                self.fetch(URL.format(kind=kind, month=month), dest, size[1],
                           lambda n, t, k=kind: self.state.__setitem__("progress", {"step": "download", "kind": k, "bytes": n, "total": t}))
                n = dest.stat().st_size
                if not (size[0] <= n <= size[1]):
                    raise GeoCityError("size of %s: %d" % (kind, n))
                sources[kind] = dest
            self.state["phase"] = "build"
            names = builder.read_asn(sources["asn"])
            os.remove(sources["asn"])
            off, lat, lon = builder.read_city(sources["city"])
            os.remove(sources["city"])
            self.state["progress"] = {"step": "write"}
            builder.write(tmp, month, off, lat, lon, names)
            del names, off, lat, lon
            self.state["phase"] = "check"
            check = CityFile(tmp)
            try:
                check.check(self.limits)
            finally:
                check.close()
            os.replace(tmp, target)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
        self.load()
        for n in self._files()[KEEP_FILES:]:
            try:
                os.remove(self.folder / n)
            except OSError:
                pass

    def clear(self) -> None:
        """Remove the files (an administrator asked, or the option was switched off with the wish to delete)."""
        f, self.file = self.file, None
        self.generation += 1
        if f:
            f.close()
        for n in self._files():
            try:
                os.remove(self.folder / n)
            except OSError:
                pass
        self.state.update(error=None, updated=None, progress=None)

    def info(self) -> dict:
        f = self.file
        phase = self.state["phase"]
        if self.demo:
            return {"enabled": True, "available": True, "status": "ready", "phase": "idle", "progress": None, "error": None,
                    "last_check": None, "month": time.strftime("%Y-%m", time.gmtime(self.clock())), "cities": 120000,
                    "operators": 70000, "size": 0, "demo": True, "attribution": ATTRIBUTION}
        if not self.available:
            status = "unavailable"
        elif not self.enabled:
            status = "off"
        elif phase in ("download", "build", "check"):
            status = "loading"
        elif f is not None:
            status = "ready"
        elif self.state["error"]:
            status = "error"
        else:
            status = "waiting"
        return {"enabled": self.enabled, "available": self.available, "status": status, "phase": phase,
                "progress": self.state["progress"], "error": self.state["error"], "last_check": self.state["last_check"],
                "month": f.month if f else None, "cities": f.cities if f else 0, "operators": f.operators if f else 0,
                "size": _size(f), "attribution": ATTRIBUTION}


def _size(f: Optional[CityFile]) -> int:
    try:
        return f.path.stat().st_size if f else 0
    except OSError:
        return 0


def _error_types() -> tuple:
    import urllib.error
    return (GeoCityError, OSError, ValueError, EOFError, MemoryError, urllib.error.URLError)
