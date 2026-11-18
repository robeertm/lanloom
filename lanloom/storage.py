"""Data keeping: how long each kind of data stays, the nightly clean-up, and how much space every area takes.

Kinds of data and their retention (Settings > Storage, administrators):

    live         live rings in memory (per trace one sample per second)       5 / 10 / 30 / 60 minutes
    hours        data paths per hour                                          1 / 2 / 3 / 7 days
    days         data paths per day                                           31 / 92 / 183 / 366 days
    availability event journal (devices offline/back, uplink, connections)    35 / 90 / 180 / 365 days
    reports      nightly reports (the same setting as under Reports)          7 / 14 / 30 / 90 / 365 days
    audit        audit log (the same setting as under Security)               30 days ... 10 years
    scans        finished network scans (history rows)                        last 50 / 200 / 1000

The clean-up runs once a night (03:xx in the report time zone, or at the next start when a night was missed): it
deletes what is older than the retention, refreshes the query planner statistics (``PRAGMA optimize``) and compacts the
file (``VACUUM``) when at least a sixth of it (and 4 MB) is free pages. A compaction needs room for a copy of the file;
with too little free disk space it is skipped and the nightly report says so.

Sizes per area come from SQLite's page statistics (``dbstat``); builds without it fall back to row counts. Nothing here
talks to the network.
"""
import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("lanloom")

CHOICES = {
    "live_min": (5, 10, 30, 60),
    "paths_hours": (25, 49, 73, 169),
    "paths_days": (31, 92, 183, 366),
    "availability_days": (35, 90, 180, 365),
    "scans_keep": (50, 200, 1000),
}
DEFAULTS = {"live_min": 10, "paths_hours": 49, "paths_days": 31, "availability_days": 35, "scans_keep": 200}

# area -> tables (indexes are counted with their table); order = display order
AREAS = (
    ("inventory", ("devices", "phys_manual", "integrations", "secrets", "settings", "meta")),
    ("paths", ("paths_hour", "paths_day", "paths_seen", "paths_pair_hour", "paths_pair_day")),
    ("availability", ("events", "avail_day", "traffic_min", "dns_sum", "dns_day")),
    ("reports", ("reports", "report_marks")),
    ("audit", ("audit",)),
    ("scans", ("scans",)),
    ("accounts", ("users", "passkeys", "sessions", "recovery_codes", "invites", "notices", "known_logins")),
)
RUN_HOUR = 3                     # local hour of the nightly clean-up
LOOP_EVERY_S = 600
VACUUM_MIN_BYTES = 4 * 1024 * 1024
VACUUM_MIN_SHARE = 1 / 6
HISTORY_DAYS = 21                # daily database sizes kept for the growth estimate
LOW_DAYS_LEFT = 30               # nightly report point: at this growth the disk is full within this many days
SAMPLE_BYTES = 24                # one live-ring sample in memory (three doubles, see live.Ring)


def process_rss() -> Optional[int]:
    """Resident memory of this process in bytes (from /proc), else None."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _dir_bytes(path: Path, skip: tuple = ()) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class Storage:
    def __init__(self, core, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self._running = False
        self.apply()

    # ------------------------------------------------------------------ settings
    def retention(self) -> dict:
        raw = self.store.get_setting("retention", {}) or {}
        out = dict(DEFAULTS)
        for k, choices in CHOICES.items():
            if raw.get(k) in choices:
                out[k] = raw[k]
        out["reports_days"] = int(self.core.reports.settings()["keep_days"])
        audit = getattr(self.core, "audit", None)
        out["audit_days"] = audit.retention_days() if audit is not None else None
        return out

    def choices(self) -> dict:
        from .audit import RETENTION_CHOICES
        from .nightly import KEEP_CHOICES
        out = {k: list(v) for k, v in CHOICES.items()}
        out["reports_days"] = list(KEEP_CHOICES)
        out["audit_days"] = list(RETENTION_CHOICES)
        return out

    def update(self, patch: dict) -> dict:
        """Change retentions; all or nothing. Raises ValueError("storage.err.choice") for a value off the list."""
        if not isinstance(patch, dict):
            raise ValueError("storage.err.choice")
        from .audit import RETENTION_CHOICES
        from .nightly import KEEP_CHOICES
        allowed = dict(CHOICES, reports_days=KEEP_CHOICES, audit_days=RETENTION_CHOICES)
        for k, v in patch.items():
            if k not in allowed or isinstance(v, bool) or v not in allowed[k]:
                raise ValueError("storage.err.choice")
        raw = dict(self.store.get_setting("retention", {}) or {})
        for k in CHOICES:
            if k in patch:
                raw[k] = patch[k]
        self.store.set_setting("retention", raw)
        if "reports_days" in patch:
            self.core.reports.update_settings({"keep_days": patch["reports_days"]})
        if "audit_days" in patch:
            self.store.set_setting("audit_retention_days", patch["audit_days"])
        self.apply()
        return self.retention()

    def apply(self) -> None:
        """Push the retentions into the parts that hold the data (memory ring, data paths)."""
        r = self.retention()
        self.core.ring.resize(int(r["live_min"]) * 60)
        self.core.paths.keep_hours = int(r["paths_hours"])
        self.core.paths.keep_days = int(r["paths_days"])
        self.core.availability.keep_days = int(r["availability_days"])

    # ------------------------------------------------------------------ sizes
    def _page_sizes(self) -> Optional[dict]:
        """{table: bytes} including its indexes, or None without dbstat."""
        try:
            rows = self.store.query("SELECT name, SUM(pgsize) AS b FROM dbstat GROUP BY name")
        except Exception:
            return None
        owner = {r["name"]: r["tbl_name"] for r in self.store.query("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
        out: dict = {}
        for r in rows:
            t = owner.get(r["name"], r["name"])
            out[t] = out.get(t, 0) + int(r["b"] or 0)
        return out

    def _rows(self, table: str) -> int:
        try:
            return int(self.store.one("SELECT COUNT(*) AS n FROM %s" % table)["n"])
        except Exception:
            return 0

    def db_file_bytes(self) -> dict:
        path = self.store.path
        out = {"db": 0, "wal": 0}
        if path == ":memory:":
            try:
                pc = self.store.one("PRAGMA page_count")
                ps = self.store.one("PRAGMA page_size")
                out["db"] = int(list(pc.values())[0]) * int(list(ps.values())[0])
            except Exception:
                pass
            return out
        for key, suffix in (("db", ""), ("wal", "-wal")):
            try:
                out[key] = os.stat(path + suffix).st_size
            except OSError:
                pass
        return out

    def free_pages_bytes(self) -> int:
        try:
            fl = int(list(self.store.one("PRAGMA freelist_count").values())[0])
            ps = int(list(self.store.one("PRAGMA page_size").values())[0])
            return fl * ps
        except Exception:
            return 0

    def live_memory(self) -> dict:
        ring = self.core.ring
        samples = ring.samples()
        return {"keys": len(ring.data), "samples": samples, "bytes": samples * SAMPLE_BYTES + len(ring.data) * 200}

    def overview(self) -> dict:
        pages = self._page_sizes()
        areas = []
        for area, tables in AREAS:
            rows = sum(self._rows(t) for t in tables)
            b = sum(pages.get(t, 0) for t in tables) if pages is not None else None
            areas.append({"id": area, "bytes": b, "rows": rows})
        files = self.db_file_bytes()
        data_dir = getattr(self.core.cfg, "data_dir", None)
        disk = other = backups = None
        if data_dir and self.store.path != ":memory:":
            try:
                du = shutil.disk_usage(str(data_dir))
                disk = {"total": du.total, "free": du.free}
            except OSError:
                disk = None
            bdir = Path(data_dir) / "backups"
            backups = _dir_bytes(bdir) if bdir.is_dir() else 0
            total = _dir_bytes(Path(data_dir))
            other = max(0, total - backups - files["db"] - files["wal"] - self._shm_bytes())
        return {"retention": self.retention(), "choices": self.choices(), "areas": areas, "dbstat": pages is not None,
                "db_bytes": files["db"], "wal_bytes": files["wal"], "free_pages_bytes": self.free_pages_bytes(),
                "backups_bytes": backups, "other_bytes": other, "disk": disk, "live": self.live_memory(),
                "rss_bytes": process_rss(), "last_run": self.store.get_setting("storage_state", None),
                "next_run": self.next_run(), "growth": self.growth()}

    def _shm_bytes(self) -> int:
        try:
            return os.stat(self.store.path + "-shm").st_size
        except OSError:
            return 0

    # ------------------------------------------------------------------ growth history
    def note_size(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        day = datetime.fromtimestamp(now, self.core.reports.tz()).date().isoformat()
        hist = [h for h in (self.store.get_setting("storage_history", []) or []) if isinstance(h, list) and len(h) == 2]
        hist = [h for h in hist if h[0] != day] + [[day, self.db_file_bytes()["db"]]]
        self.store.set_setting("storage_history", hist[-HISTORY_DAYS:])

    def growth(self) -> dict:
        """Average growth of the database per day over the kept history (bytes/day; None with under 3 days)."""
        hist = [h for h in (self.store.get_setting("storage_history", []) or []) if isinstance(h, list) and len(h) == 2]
        if len(hist) < 3:
            return {"per_day": None, "days": len(hist)}
        try:
            d0 = datetime.fromisoformat(hist[0][0]).date()
            d1 = datetime.fromisoformat(hist[-1][0]).date()
        except ValueError:
            return {"per_day": None, "days": len(hist)}
        span = max(1, (d1 - d0).days)
        return {"per_day": max(0, int((hist[-1][1] - hist[0][1]) / span)), "days": len(hist)}

    def days_left(self) -> Optional[int]:
        g = self.growth()["per_day"]
        data_dir = getattr(self.core.cfg, "data_dir", None)
        if not g or not data_dir or self.store.path == ":memory:":
            return None
        try:
            free = shutil.disk_usage(str(data_dir)).free
        except OSError:
            return None
        return int(free / g)

    # ------------------------------------------------------------------ clean-up
    def next_run(self) -> float:
        tz = self.core.reports.tz()
        now = datetime.fromtimestamp(self.clock(), tz)
        at = now.replace(hour=RUN_HOUR, minute=15, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        return at.timestamp()

    def due(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        last = (self.store.get_setting("storage_state", None) or {}).get("t")
        if not isinstance(last, (int, float)):
            return True
        if now - last > 36 * 3600:
            return True                                    # a missed night: catch up
        local = datetime.fromtimestamp(now, self.core.reports.tz())
        last_local = datetime.fromtimestamp(last, self.core.reports.tz())
        return local.hour >= RUN_HOUR and last_local.date() < local.date()

    def run(self, now: Optional[float] = None, vacuum: Optional[bool] = None) -> dict:
        """Delete what is past its retention, then optimise (and compact when worth it). Returns the run summary."""
        now = self.clock() if now is None else now
        if self._running:
            raise ValueError("storage.err.busy")
        self._running = True
        started = time.monotonic()
        try:
            r = self.retention()
            if self.store.path != ":memory:":
                try:
                    self.store.execute("PRAGMA wal_checkpoint(PASSIVE)")   # sizes of the main file mean something
                except Exception:
                    pass
            before = self.db_file_bytes()["db"]
            removed = {}
            # data paths: flush first, so nothing pending lands in an hour that was just removed
            try:
                self.core.paths.flush(now)
            except Exception:
                log.exception("data paths flush before clean-up failed")
            for part in (self.core.availability, self.core.traffic_log):
                try:
                    part.flush(now)
                except Exception:
                    log.exception("history flush before clean-up failed")
            hour = int(now // 3600)
            with self.store.transaction() as tx:
                removed["paths"] = (tx._db.execute("DELETE FROM paths_hour WHERE hour < ?", (hour - int(r["paths_hours"]),)).rowcount
                                    + tx._db.execute("DELETE FROM paths_day WHERE day < ?", (hour // 24 - int(r["paths_days"]),)).rowcount
                                    + tx._db.execute("DELETE FROM paths_pair_hour WHERE hour < ?", (hour - int(r["paths_hours"]),)).rowcount
                                    + tx._db.execute("DELETE FROM paths_pair_day WHERE day < ?", (hour // 24 - int(r["paths_days"]),)).rowcount)
                removed["scans"] = tx._db.execute(
                    "DELETE FROM scans WHERE finished IS NOT NULL AND id NOT IN (SELECT id FROM scans ORDER BY id DESC LIMIT ?)",
                    (int(r["scans_keep"]),)).rowcount
            n_events = self.store.one("SELECT COUNT(*) AS n FROM events")["n"]
            n_reports = self.store.one("SELECT COUNT(*) AS n FROM reports")["n"]
            self.core.reports.prune()               # reports + journal (journal keeps at least availability_days)
            removed["availability"] = n_events - self.store.one("SELECT COUNT(*) AS n FROM events")["n"]
            removed["reports"] = n_reports - self.store.one("SELECT COUNT(*) AS n FROM reports")["n"]
            audit = getattr(self.core, "audit", None)
            removed["audit"] = audit.prune(now) if audit is not None else 0
            try:
                self.store.execute("PRAGMA optimize")
            except Exception:
                log.debug("optimize failed", exc_info=True)
            free_pages = self.free_pages_bytes()
            size = self.db_file_bytes()["db"]
            want = free_pages >= VACUUM_MIN_BYTES and free_pages >= size * VACUUM_MIN_SHARE
            if vacuum is not None:
                want = vacuum and free_pages > 0
            compacted, skipped = False, None
            if want:
                room = None
                data_dir = getattr(self.core.cfg, "data_dir", None)
                if data_dir and self.store.path != ":memory:":
                    try:
                        room = shutil.disk_usage(str(data_dir)).free
                    except OSError:
                        room = None
                if room is not None and room < size * 1.2:
                    skipped = "storage.vacuum.no_room"
                else:
                    try:
                        self.store.execute("VACUUM")
                        compacted = True
                    except Exception:
                        log.warning("VACUUM failed", exc_info=True)
                        skipped = "storage.vacuum.failed"
            if self.store.path != ":memory:":
                try:
                    self.store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
            after = self.db_file_bytes()["db"]
            state = {"t": now, "dur_s": round(time.monotonic() - started, 2), "removed": removed,
                     "before": before, "after": after, "freed": max(0, before - after), "vacuum": compacted, "skipped": skipped}
            self.store.set_setting("storage_state", state)
            self.note_size(now)
            return state
        finally:
            self._running = False

    async def loop(self) -> None:
        await asyncio.sleep(120)                           # never in the busy first minutes after a start
        while True:
            try:
                if self.due():
                    state = await asyncio.to_thread(self.run)
                    log.info("storage clean-up: removed %s, %s -> %s bytes in %.1f s", state["removed"], state["before"],
                             state["after"], state["dur_s"])
            except Exception:
                log.exception("storage clean-up failed")
            await asyncio.sleep(LOOP_EVERY_S)
