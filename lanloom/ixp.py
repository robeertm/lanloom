"""Exchange points (internet exchanges) for the network routes of the data paths (DW2): an option, switched off by default.

A hop with an address from the peering network of an exchange point is the surest hint at the place of that hop, and the
view can say "exchange point in Frankfurt" instead of an unnamed step. Two sources:

  1. Router names: a small table of exchange names in ``routes.EXCHANGE_NAMES`` (always available, nothing is fetched).
  2. PeeringDB (https://www.peeringdb.com): the prefix lists of the exchange points with name, city and country. Only with
     *Settings > Data paths > Network routes > Exchange points from PeeringDB* switched on (and the routes themselves on),
     this instance fetches three public lists (``/api/ix``, ``/api/ixlan``, ``/api/ixpfx``) for its own network diagnosis:
     at most one check a day and one new copy a month, anonymously, with a size limit, nothing sent but the request for the
     list. The copy is condensed to prefix, exchange name, city and country and kept in ``<data>/routes/ixp-YYYY-MM.json``
     (the previous month stays as a fallback). It is never passed on: not bundled with Lanloom, not part of an export or
     backup, not in any answer of the API - the view only shows the result per hop (name and city of the exchange point).
     PeeringDB's acceptable use policy: "The PeeringDB data may not be passed on in bulk to any other person or
     organization unless approved by PeeringDB." Switching the option off deletes the copy.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import routes

log = logging.getLogger("lanloom")

SOURCE = {"name": "PeeringDB", "url": "https://www.peeringdb.com"}
BASE = "https://www.peeringdb.com/api"
HOST = "www.peeringdb.com"
LISTS = ("ix", "ixlan", "ixpfx")
MAX_BYTES = 10 * 1024 * 1024
MIN_PREFIXES = 500
CHECK_EVERY_S = 86400
TIMEOUT_S = 60
MAGIC = "lanloom-ixp/1"
_FILE = re.compile(r"^ixp-(\d{4}-\d{2})\.json$")


def month_of(now: float) -> str:
    dt = datetime.fromtimestamp(now, timezone.utc)
    return "%04d-%02d" % (dt.year, dt.month)


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lanloom-ixp/1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:        # noqa: S310 - fixed https address of the source
        return r.read(MAX_BYTES + 1)


class Exchanges:
    """Holds the exchange table (newest file, fallback = the one before) and renews it once a month while switched on."""

    def __init__(self, folder: Optional[Path], places: Optional[dict] = None, fetch: Optional[Callable[[str], bytes]] = None,
                 clock: Callable[[], float] = time.time):
        self.folder = Path(folder) if folder else None
        self.places = places or {}
        self.fetch = fetch or http_get
        self.clock = clock
        self.enabled = False
        self.table: Optional[routes.ExchangeTable] = None
        self.lock = threading.Lock()
        self.state = {"last_check": None, "error": None, "file": None, "prefixes": 0, "running": False}
        if self.folder:
            self.load()

    # ---------------------------------------------------------------- files
    def _files(self) -> list:
        if not self.folder:
            return []
        try:
            return sorted((n for n in os.listdir(self.folder) if _FILE.match(n)), reverse=True)
        except OSError:
            return []

    def load(self) -> bool:
        """Load the newest readable file (a broken one is skipped: fallback to the one before)."""
        for n in self._files():
            try:
                with open(self.folder / n, encoding="utf-8") as f:
                    d = json.load(f)
                rows = d.get("prefixes") if isinstance(d, dict) and d.get("v") == MAGIC else None
                if not isinstance(rows, list) or not rows:
                    raise ValueError("format")
                rows = [r for r in rows if isinstance(r, list) and len(r) == 4 and isinstance(r[0], str)]
                self.table = routes.exchange_table(rows, self.places)
                self.state.update(file=n, prefixes=len(rows))
                return True
            except (OSError, ValueError, KeyError, TypeError):
                self.state["error"] = "file"
        return self.table is not None

    def configure(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self.clear()

    def clear(self) -> None:
        """Switched off: the copy is deleted (it is only for this instance's own diagnosis)."""
        with self.lock:
            self.table = None
            self.state.update(file=None, prefixes=0, error=None)
            for n in self._files():
                try:
                    (self.folder / n).unlink()
                except OSError:
                    pass

    # ---------------------------------------------------------------- lookup
    def find(self, ip: str) -> Optional[dict]:
        t = self.table
        return t.find(ip) if (t is not None and self.enabled) else None

    def info(self) -> dict:
        status = "off" if not self.enabled else ("ready" if self.table is not None else ("loading" if self.state["running"]
                                                                                         else ("error" if self.state["error"] else "waiting")))
        month = None
        if self.state["file"]:
            m = _FILE.match(self.state["file"])
            month = m.group(1) if m else None
        return {"status": status, "source": SOURCE["name"], "url": SOURCE["url"], "month": month,
                "prefixes": self.state["prefixes"], "last_check": self.state["last_check"], "error": self.state["error"]}

    # ---------------------------------------------------------------- download
    def _list(self, name: str):
        raw = self.fetch("%s/%s" % (BASE, name))
        if not isinstance(raw, (bytes, bytearray)) or not (2 <= len(raw) <= MAX_BYTES):
            raise ValueError("size")
        d = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("data"), list) or not d["data"]:
            raise ValueError("empty")
        return d

    def build(self, month: str) -> None:
        """Fetch the three lists, condense, check (at least 500 prefixes), store atomically, load."""
        lists = {k: self._list(k) for k in LISTS}
        rows = routes.compact_peeringdb(lists["ixpfx"], lists["ix"], lists["ixlan"])
        if len(rows) < MIN_PREFIXES:
            raise ValueError("too few prefixes (%d)" % len(rows))
        table = routes.exchange_table(rows, self.places)
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / ("ixp-%s.json" % month)
        tmp = target.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": MAGIC, "month": month, "source": SOURCE["name"], "prefixes": rows}, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        self.table = table
        self.state.update(file=target.name, prefixes=len(rows))
        for n in self._files()[2:]:
            try:
                (self.folder / n).unlink()
            except OSError:
                pass

    def due(self, now: Optional[float] = None) -> Optional[str]:
        now = self.clock() if now is None else now
        if not self.enabled or not self.folder or self.state["running"]:
            return None
        want = month_of(now)
        files = self._files()
        if files and _FILE.match(files[0]).group(1) >= want:
            return None
        last = self.state["last_check"]
        if last and now - last < CHECK_EVERY_S:
            return None
        return want

    def step(self, now: Optional[float] = None) -> bool:
        """One check when due (at most once a day, a new copy at most once a month). True = new table loaded."""
        month = self.due(now)
        if not month:
            return False
        with self.lock:
            self.state["running"] = True
            self.state["last_check"] = self.clock() if now is None else now
            try:
                self.build(month)
                self.state["error"] = None
                return True
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.state["error"] = (str(exc)[:60] if isinstance(exc, ValueError) else type(exc).__name__)
                log.info("exchange points: %s", self.state["error"])
                return False
            finally:
                self.state["running"] = False
