"""Offline manufacturer lookup by hardware address prefix (OUI). Never asks anything on the network.

Data: the public IEEE registration authority lists MA-L (24-bit prefixes), MA-M (28-bit) and MA-S (36-bit).
The image ships one compact file, ``lanloom/data/oui.bin.gz``, built with ``scripts/build_oui.py`` from the
three CSV files. It is replaced by a new Lanloom release (the update feed), never downloaded at run time.
A newer table can also be dropped into the data directory (``<data>/oui/oui.bin.gz``); it is used only when
it passes the same checks and is newer than the bundled one.

Rules:
  * locally administered addresses (bit 0x02 of the first octet) are random or private: never a manufacturer;
  * group (multicast) addresses are not device addresses;
  * the longest registered prefix wins (an MA-S block inside an MA-L block of the registration authority);
  * the registry's own "private" entries give no name.

File format (inside gzip): MAGIC, 4 byte big-endian header length, JSON header (``date``, ``names`` count, ``n24``,
``n28``, ``n36``, ``sha256`` of everything after the header), the vendor names (UTF-8, one per line, then one empty
line), then six little-endian arrays: 24-bit prefixes (uint32) + name index (uint32), 28-bit prefixes (uint32) +
index, 36-bit prefixes (uint64) + index. Prefix arrays are sorted ascending.
"""
from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import logging
import re
import struct
import sys
import threading
from array import array
from pathlib import Path
from typing import Optional

log = logging.getLogger("lanloom")

MAGIC = b"LLOUI1\n"
DATA_FILE = Path(__file__).resolve().parent / "data" / "oui.bin.gz"
SOURCE = {"text": "IEEE Registration Authority (MA-L, MA-M, MA-S)", "url": "https://standards-oui.ieee.org/"}
MAC_RX = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
MIN_ROWS = 20_000

# origin classes of a hardware address
REGISTERED = "registered"        # the prefix is registered: the name comes from the registry
RANDOM = "random"                # locally administered: private/random address, never a manufacturer
UNREGISTERED = "unregistered"    # globally unique bit set, but the prefix is not in the table
NO_MAC = "no_mac"                # only an IP address is known (routed network, no neighbour entry)


class OuiError(ValueError):
    pass


def norm_mac(mac) -> Optional[str]:
    if not isinstance(mac, str):
        return None
    m = mac.strip().lower().replace("-", ":")
    if re.fullmatch(r"[0-9a-f]{12}", m):
        m = ":".join(m[i:i + 2] for i in range(0, 12, 2))
    if not MAC_RX.match(m) or m == "00:00:00:00:00:00" or m == "ff:ff:ff:ff:ff:ff":
        return None
    return m


def is_random(mac: str) -> bool:
    """Locally administered (randomised/private) address."""
    m = norm_mac(mac)
    return bool(m) and bool(int(m[:2], 16) & 0x02)


def is_group(mac: str) -> bool:
    m = norm_mac(mac)
    return bool(m) and bool(int(m[:2], 16) & 0x01)


def mac_int(mac: str) -> int:
    return int(mac.replace(":", ""), 16)


def _arr(code: str, raw: bytes) -> array:
    a = array(code)
    a.frombytes(raw)
    if sys.byteorder != "little":
        a.byteswap()
    return a


def pack(header: dict, names: list, tables: dict) -> bytes:
    """Raw (uncompressed) file content. tables = {24: [(prefix, idx)], 28: [...], 36: [...]} (any order)."""
    body = ["\n".join(names).encode("utf-8") + b"\n\n"]
    counts = {}
    for bits, code in ((24, "I"), (28, "I"), (36, "Q")):
        rows = sorted(tables.get(bits) or [])
        counts["n%d" % bits] = len(rows)
        for values, c in (([p for p, _ in rows], code), ([i for _, i in rows], "I")):
            a = array(c, values)
            if sys.byteorder != "little":
                a.byteswap()
            body.append(a.tobytes())
    payload = b"".join(body)
    head = dict(header, names=len(names), sha256=hashlib.sha256(payload).hexdigest(), **counts)
    hb = json.dumps(head, separators=(",", ":"), sort_keys=True).encode()
    return MAGIC + struct.pack(">I", len(hb)) + hb + payload


class OuiDB:
    """Loaded table. ``lookup(mac)`` -> registered name or None."""

    def __init__(self, raw: bytes):
        if not raw.startswith(MAGIC):
            raise OuiError("magic")
        off = len(MAGIC)
        if len(raw) < off + 4:
            raise OuiError("size")
        (hl,) = struct.unpack(">I", raw[off:off + 4])
        off += 4
        try:
            head = json.loads(raw[off:off + hl])
        except ValueError:
            raise OuiError("header")
        off += hl
        payload = raw[off:]
        if hashlib.sha256(payload).hexdigest() != head.get("sha256"):
            raise OuiError("checksum")
        end = payload.find(b"\n\n")
        if end < 0:
            raise OuiError("names")
        try:
            names = payload[:end].decode("utf-8").split("\n")
        except UnicodeDecodeError:
            raise OuiError("names")
        if len(names) != int(head.get("names", -1)):
            raise OuiError("names")
        pos = end + 2
        self.tables = {}
        for bits, code, width in ((24, "I", 4), (28, "I", 4), (36, "Q", 8)):
            n = int(head.get("n%d" % bits, -1))
            if n < 0:
                raise OuiError("counts")
            prefixes = _arr(code, payload[pos:pos + n * width])
            pos += n * width
            idx = _arr("I", payload[pos:pos + n * 4])
            pos += n * 4
            if len(prefixes) != n or len(idx) != n or (n and max(idx) >= len(names)):
                raise OuiError("index")
            if any(prefixes[i] >= prefixes[i + 1] for i in range(n - 1)):
                raise OuiError("order")
            self.tables[bits] = (prefixes, idx)
        if pos != len(payload):
            raise OuiError("size")
        self.names = names
        self.date = str(head.get("date") or "")
        self.rows = sum(len(t[0]) for t in self.tables.values())

    @classmethod
    def from_file(cls, path: Path, min_rows: int = MIN_ROWS) -> "OuiDB":
        try:
            raw = gzip.decompress(Path(path).read_bytes())
        except (OSError, EOFError) as exc:
            raise OuiError("read: %s" % type(exc).__name__)
        db = cls(raw)
        if db.rows < min_rows:
            raise OuiError("coverage")
        return db

    def lookup(self, mac) -> Optional[str]:
        m = norm_mac(mac)
        if not m or is_random(m) or is_group(m):
            return None
        v = mac_int(m)
        for bits in (36, 28, 24):
            prefixes, idx = self.tables[bits]
            key = v >> (48 - bits)
            i = bisect.bisect_left(prefixes, key)
            if i < len(prefixes) and prefixes[i] == key:
                return self.names[idx[i]] or None
        return None

    def info(self) -> dict:
        return {"loaded": True, "date": self.date, "rows": self.rows, "source": SOURCE}


class Oui:
    """Loads the table once (bundled file, or a newer valid drop-in); until then (or if broken) ``db`` is None.

    ``override`` replaces the table completely (demo mode uses a small fictional registry keyed by the demo's
    invented addresses, so screenshots never show real organisation names): {"names": {mac: name}, "random": set}.
    """

    def __init__(self, path: Path = DATA_FILE, extra: Optional[Path] = None):
        self.path = path
        self.extra = extra
        self.db: Optional[OuiDB] = None
        self.error: Optional[str] = None
        self.override: Optional[dict] = None
        self.origin = "bundled"
        self._lock = threading.Lock()
        self._started = False

    def start(self, background: bool = True) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        if background:
            threading.Thread(target=self._load, name="lanloom-oui", daemon=True).start()
        else:
            self._load()

    def _load(self) -> None:
        try:
            db = OuiDB.from_file(self.path)
        except OuiError as exc:
            db, self.error = None, str(exc)
            log.warning("manufacturer table not usable: %s", exc)
        if self.extra and Path(self.extra).is_file() and not Path(self.extra).is_symlink():
            try:
                newer = OuiDB.from_file(self.extra)
                if db is None or newer.date > db.date:
                    db, self.origin, self.error = newer, "data-dir", None
            except OuiError as exc:
                log.warning("manufacturer table in the data directory ignored: %s", exc)
        self.db = db
        if db:
            log.info("manufacturer table %s loaded (%d prefixes, %s)", db.date, db.rows, self.origin)

    def lookup(self, mac) -> Optional[str]:
        if self.override is not None:
            return (self.override.get("names") or {}).get(norm_mac(mac) or "")
        db = self.db
        return db.lookup(mac) if db else None

    def origin_of(self, mac) -> str:
        """Origin class of an address: registered / random / unregistered / no_mac."""
        m = norm_mac(mac)
        if not m or is_group(m):
            return NO_MAC
        if self.override is not None:
            if m in (self.override.get("random") or ()):
                return RANDOM
            return REGISTERED if self.lookup(m) else UNREGISTERED
        if is_random(m):
            return RANDOM
        return REGISTERED if self.lookup(m) else UNREGISTERED

    def info(self) -> dict:
        if self.override is not None:
            return {"loaded": True, "date": "demo", "rows": len(self.override.get("names") or {}), "source": SOURCE, "origin": "demo"}
        if self.db:
            return dict(self.db.info(), origin=self.origin)
        return {"loaded": False, "error": self.error, "source": SOURCE}


SUFFIX_RX = re.compile(
    r"[\s,.]+(co\.?,?\s*ltd\.?|company\s+limited|corporation|corp\.?|incorporated|inc\.?|limited|ltd\.?|llc|l\.l\.c\.|"
    r"gmbh(\s*&\s*co\.?\s*kg)?|ag|kg|s\.?a\.?|s\.?p\.?a\.?|s\.?r\.?l\.?|b\.?v\.?|n\.?v\.?|oy|ab|a/s|as|pte\.?|pty\.?|plc|sas|"
    r"co\.?|technology|technologies|electronics)\.?$", re.I)


def clean_name(name: str) -> str:
    """Registry spelling -> display name: whitespace collapsed, legal-form suffixes dropped, at most 48 chars."""
    n = re.sub(r"\s+", " ", (name or "").replace(" ", " ")).strip().strip(",")
    for _ in range(4):
        m = SUFFIX_RX.search(n)
        if not m or m.start() < 2:
            break
        n = n[:m.start()].strip().strip(",")
    return n[:48]


REGISTRY = Oui()
