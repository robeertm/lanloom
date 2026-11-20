"""External reports: another program delivers a short report of its own (a backup job, a check script, a host agent of
somebody else) through ``POST /api/v1/ingest``. Lanloom keeps it, lists it under Settings > API and puts it into the next
nightly report.

Only with an API token whose scope allows it (``ingest`` - delivers and reads nothing -, or ``operate``/``admin``), at
most 64 KiB per report and 200 reports a day. The text is stored as text: it is never interpreted as markup, and links are
not taken over. Kept for 35 days (at most 1000 reports).

Body::

    {"source": "nightly-backup", "title": "Backup of the file server", "level": "ok",
     "summary": "312 GB, 41 min", "items": [{"text": "3 files skipped (in use)", "level": "info"}]}
"""
import json
import time
from typing import Callable, Optional

MAX_BYTES = 64 * 1024
MAX_PER_DAY = 200
MAX_ITEMS = 50
LEVELS = ("ok", "info", "warn", "danger")
KEEP_DAYS = 35
KEEP_ROWS = 1000
LIMITS = {"source": 60, "title": 120, "summary": 500, "text": 300}


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def clean(data) -> dict:
    """The stored form of one report. Raises ValueError with a translation key."""
    if not isinstance(data, dict):
        raise ValueError("api.err.ingest_body")
    source, title = _text(data.get("source"), LIMITS["source"]), _text(data.get("title"), LIMITS["title"])
    if not source or not title:
        raise ValueError("api.err.ingest_fields")
    level = data.get("level") or "info"
    if level not in LEVELS:
        raise ValueError("api.err.ingest_level")
    items = data.get("items") or []
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError("api.err.ingest_items")
    out_items = []
    for it in items:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            raise ValueError("api.err.ingest_items")
        lvl = it.get("level") or "info"
        if lvl not in LEVELS:
            raise ValueError("api.err.ingest_level")
        text = _text(it.get("text"), LIMITS["text"])
        if text:
            out_items.append({"text": text, "level": lvl})
    return {"source": source, "title": title, "level": level, "summary": _text(data.get("summary"), LIMITS["summary"]),
            "items": out_items}


class Ingest:
    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    def add(self, data, size: int, token: Optional[dict] = None, actor: str = "", site: str = "home") -> dict:
        """Store one external report for ``site`` - the site the request named (M1T: never a default when several exist)."""
        rep = clean(data)
        now = self.clock()
        n = self.store.one("SELECT COUNT(*) AS n FROM api_ingest WHERE t > ?", (now - 86400,))["n"]
        if n >= MAX_PER_DAY:
            raise OverflowError("api.err.ingest_full")
        cur = self.store.execute("INSERT INTO api_ingest(t, source, title, level, data, bytes, token_id, actor, site) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (now, rep["source"], rep["title"], rep["level"],
                                  json.dumps({"summary": rep["summary"], "items": rep["items"]}, separators=(",", ":"), ensure_ascii=False),
                                  int(size), (token or {}).get("id"), str(actor or "")[:64], str(site)))
        if cur.lastrowid % 50 == 0:
            self.prune()
        return {"id": cur.lastrowid, "accepted": True, "t": now, "items": len(rep["items"])}

    @staticmethod
    def _view(r: dict) -> dict:
        data = json.loads(r["data"] or "{}")
        return {"id": r["id"], "t": r["t"], "source": r["source"], "title": r["title"], "level": r["level"],
                "summary": data.get("summary") or "", "items": data.get("items") or [], "by": r.get("actor") or ""}

    def list(self, limit: int = 50, since: Optional[float] = None, site: Optional[str] = None) -> list:
        limit = max(1, min(int(limit or 50), 200))
        where, args = [], []
        if since:
            where.append("t > ?"); args.append(float(since))
        if site is not None:
            where.append("site = ?"); args.append(str(site))
        rows = self.store.query("SELECT * FROM api_ingest %s ORDER BY t DESC, id DESC LIMIT ?" % ("WHERE " + " AND ".join(where) if where else ""),
                                tuple(args) + (limit,))
        return [self._view(r) for r in rows]

    def between(self, since: float, until: float, site: Optional[str] = None) -> list:
        if site is None:
            rows = self.store.query("SELECT * FROM api_ingest WHERE t > ? AND t <= ? ORDER BY t", (since, until))
        else:
            rows = self.store.query("SELECT * FROM api_ingest WHERE t > ? AND t <= ? AND site = ? ORDER BY t", (since, until, str(site)))
        return [self._view(r) for r in rows]

    def of_site(self, site: str) -> "SiteIngest":
        return SiteIngest(self, site)

    def prune(self) -> None:
        self.store.execute("DELETE FROM api_ingest WHERE t < ?", (self.clock() - KEEP_DAYS * 86400,))
        self.store.execute("DELETE FROM api_ingest WHERE id NOT IN (SELECT id FROM api_ingest ORDER BY id DESC LIMIT ?)", (KEEP_ROWS,))


class SiteIngest:
    """The external reports of one site, for that site's nightly report (M1T): a report delivered for one site never shows
    in the report of another."""

    def __init__(self, ingest: Ingest, site: str):
        self.ingest, self.site = ingest, site

    def list(self, limit: int = 50, since: Optional[float] = None) -> list:
        return self.ingest.list(limit, since, site=self.site)

    def between(self, since: float, until: float) -> list:
        return self.ingest.between(since, until, site=self.site)


def seed_demo(ingest: Ingest, now: Optional[float] = None) -> None:
    if ingest.store.one("SELECT 1 AS x FROM api_ingest LIMIT 1"):
        return
    now = time.time() if now is None else now
    samples = [
        (26 * 3600, "file-server-backup", "Backup of the file server", "ok", "312 GB in 41 min", [{"text": "3 files skipped (in use)", "level": "info"}]),
        (9 * 3600, "disk-check", "Disk check of the media box", "warn", "One disk reports reallocated sectors",
         [{"text": "Disk 2: 14 reallocated sectors (was 9)", "level": "warn"}, {"text": "Disk 1: healthy", "level": "ok"}]),
        (2 * 3600, "certificate-check", "Certificates of the home services", "ok", "All 6 valid for more than 30 days", []),
    ]
    for ago, src, title, level, summary, items in samples:
        ingest.store.execute("INSERT INTO api_ingest(t, source, title, level, data, bytes, token_id, actor) VALUES(?,?,?,?,?,?,?,?)",
                             (now - ago, src, title, level, json.dumps({"summary": summary, "items": items}), 300, None, "admin"))
