"""Offline IP -> country lookup for the data paths view. Never asks anything on the network.

Data: "IP to Country Lite" by DB-IP (https://db-ip.com), licensed CC BY 4.0. The attribution
"IP Geolocation by DB-IP" is shown in the app (data paths view), in the README and in THIRD-PARTY-NOTICES.md.
The image ships one compact file, ``lanloom/data/geo-country.bin.gz``, built with ``scripts/build_geodb.py``
from the monthly CSV. It is replaced by a new Lanloom release, never downloaded at run time.

File format (inside gzip): MAGIC, 4 byte big-endian header length, JSON header, then four raw arrays, all
little-endian: IPv4 range starts (uint32), their country index (uint8), IPv6 range starts cut to the upper
64 bits (uint64) and their country index (uint8). The ranges of the source cover the whole address space
without gaps, so a start list is enough: the country of an address is the one of the last start <= address.
Index 0 is "no country" (reserved/special ranges, source code ZZ).
"""
import bisect
import gzip
import ipaddress
import json
import logging
import struct
import sys
import threading
from array import array
from pathlib import Path
from typing import Optional

from . import geocity

log = logging.getLogger("lanloom")

MAGIC = b"LLGEO1\n"
DATA_FILE = Path(__file__).resolve().parent / "data" / "geo-country.bin.gz"
ATTRIBUTION = {"text": "IP Geolocation by DB-IP", "url": "https://db-ip.com", "license": "CC BY 4.0",
               "license_url": "https://creativecommons.org/licenses/by/4.0/"}
RESERVED = "ZZ"
SAMPLES = (("8.8.8.8", "US"), ("10.0.0.1", RESERVED), ("fd00::1", RESERVED), ("2001:4860:4860::8888", "*"))   # "*": any country


class GeoError(ValueError):
    pass


def _arr(code: str, raw: bytes) -> array:
    a = array(code)
    a.frombytes(raw)
    if sys.byteorder != "little":
        a.byteswap()
    return a


def pack(header: dict, v4_start, v4_idx, v6_start, v6_idx) -> bytes:
    """Raw (uncompressed) file content from the four arrays."""
    parts = []
    for code, values in (("I", v4_start), ("B", v4_idx), ("Q", v6_start), ("B", v6_idx)):
        a = array(code, values)
        if sys.byteorder != "little" and a.itemsize > 1:
            a.byteswap()
        parts.append(a.tobytes())
    head = dict(header, n4=len(v4_start), n6=len(v6_start))
    hb = json.dumps(head, separators=(",", ":"), sort_keys=True).encode()
    return MAGIC + struct.pack(">I", len(hb)) + hb + b"".join(parts)


class GeoDB:
    """Loaded lookup table. ``country(ip)`` -> ISO code, ``RESERVED`` for special ranges, or None (not an address)."""

    def __init__(self, raw: bytes):
        if not raw.startswith(MAGIC):
            raise GeoError("magic")
        off = len(MAGIC)
        (hl,) = struct.unpack(">I", raw[off:off + 4])
        off += 4
        try:
            head = json.loads(raw[off:off + hl])
        except ValueError:
            raise GeoError("header")
        off += hl
        n4, n6 = int(head["n4"]), int(head["n6"])
        codes = head.get("codes") or []
        if not codes or codes[0] != RESERVED or any(len(c) != 2 for c in codes) or len(codes) > 255:
            raise GeoError("codes")
        sizes = (4 * n4, n4, 8 * n6, n6)
        if len(raw) != off + sum(sizes):
            raise GeoError("size")
        arrays = []
        for code, size in zip(("I", "B", "Q", "B"), sizes):
            arrays.append(_arr(code, raw[off:off + size]))
            off += size
        self.v4_start, self.v4_idx, self.v6_start, self.v6_idx = arrays
        if n4 < 1000 or n6 < 1000 or self.v4_start[0] != 0 or self.v6_start[0] != 0:
            raise GeoError("coverage")
        if max(self.v4_idx) >= len(codes) or max(self.v6_idx) >= len(codes):
            raise GeoError("index")
        self.codes = codes
        self.month = head.get("month") or ""
        self.rows = n4 + n6

    @classmethod
    def from_file(cls, path: Path) -> "GeoDB":
        try:
            raw = gzip.decompress(Path(path).read_bytes())
        except (OSError, EOFError) as exc:
            raise GeoError("read: %s" % type(exc).__name__)
        db = cls(raw)
        db.check_samples()
        return db

    def country(self, ip: str) -> Optional[str]:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if addr.version == 4:
            starts, idx, key = self.v4_start, self.v4_idx, int(addr)
        else:
            starts, idx, key = self.v6_start, self.v6_idx, int(addr) >> 64
        i = bisect.bisect_right(starts, key) - 1
        return self.codes[idx[i]] if i >= 0 else None

    def check_samples(self) -> None:
        for ip, want in SAMPLES:
            got = self.country(ip)
            if got != want and not (want == "*" and got and got != RESERVED):
                raise GeoError("sample %s gave %s" % (ip, got))

    def info(self) -> dict:
        return {"loaded": True, "month": self.month, "rows": self.rows, "attribution": ATTRIBUTION}


class Geo:
    """Loads the bundled file once in a background thread; until then (or if it is broken) ``db`` is None."""

    def __init__(self, path: Path = DATA_FILE, detail_dir: Optional[Path] = None, detail: Optional["geocity.CityData"] = None):
        self.path = path
        self.city = detail or geocity.CityData(detail_dir)       # cities and operators: an option, off until switched on
        self._place_cache: dict = {}
        self._place_gen = -1
        self.db: Optional[GeoDB] = None
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._started = False

    def start(self, background: bool = True) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        if background:
            threading.Thread(target=self._load, name="lanloom-geo", daemon=True).start()
        else:
            self._load()

    def _load(self) -> None:
        try:
            self.db = GeoDB.from_file(self.path)
            log.info("geo data %s loaded (%d ranges)", self.db.month, self.db.rows)
        except GeoError as exc:
            self.error = str(exc)
            log.warning("geo data not usable: %s", exc)

    def country(self, ip: str) -> Optional[str]:
        db = self.db
        return db.country(ip) if db else None

    def detail(self, ip: str, cc: str) -> Optional[tuple]:
        """City and network operator of a peer already booked under the country ``cc``:
        (city key or "", AS number or 0, place {name, region, lat, lon} or None, operator name or None); None while the
        option is off or no file is loaded. A city in another country than the bundled country data names (a network
        that moved) is dropped: the country wins. Cached in memory only (never stored with an address)."""
        data = self.city
        if not data.enabled or data.file is None:
            return None
        if self._place_gen != data.generation:
            self._place_cache, self._place_gen = {}, data.generation
        hit = self._place_cache.get((ip, cc))
        if hit is None:
            idx, asn = data.lookup(ip)
            place = data.city(idx) if idx else None
            if place and place["cc"] != cc:
                place = None
            hit = (place["key"] if place else "", asn, place, data.operator(asn) if asn else None)
            if len(self._place_cache) >= 50_000:
                self._place_cache = {}
            self._place_cache[(ip, cc)] = hit
        return hit

    def info(self) -> dict:
        out = self.db.info() if self.db else {"loaded": False, "error": self.error, "attribution": ATTRIBUTION}
        out["detail"] = self.city.info()
        return out
