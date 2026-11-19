"""Chronicle ("Timeline & data state"): the long memory of the network, collected by Lanloom itself once a day.

One row per thing worth remembering, kept ``KEEP_DAYS`` days (much longer than the slots and the journal):

    firmware      {title, from, to}          a device reports another firmware or software version than the day before
    version       {from, to}                 Lanloom itself was updated (from the journal, ``app_start`` with ``prev``)
    new_device    {title, ip, kind}          a device seen for the first time (from the journal, ``dev_new``)
    long_outage   {title, kind, dur, inc}    an incident (not maintenance) of more than ``LONG_S`` (lanloom/incidents.py)

``collect(now)`` is cheap and idempotent: it looks at what happened since the last collection (a setting holds the time
and the firmware seen last), so it runs after every nightly report, at start and whenever the chronicle is read and the
last collection is older than a day. Rows carry titles and versions, never addresses of the outside world or secrets.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

KEEP_DAYS = 400
LONG_S = 3600
EVERY_S = 86400.0
STATE = "chronicle_state"
KINDS = ("firmware", "version", "new_device", "long_outage")
MAX_LIST = 500
SCHEMA = """
CREATE TABLE IF NOT EXISTS chronicle (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
    data TEXT NOT NULL, UNIQUE(kind, subject, t));
CREATE INDEX IF NOT EXISTS chronicle_t ON chronicle(t);
"""


class Chronicle:
    def __init__(self, core, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self.lock = threading.Lock()
        with self.store.transaction() as tx:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    tx._db.execute(stmt)

    def state(self) -> dict:
        st = self.store.get_setting(STATE, {}) or {}
        st.setdefault("at", 0.0)
        st.setdefault("fw", {})
        return st

    def _add(self, rows: list) -> int:
        if not rows:
            return 0
        with self.store.transaction() as tx:
            before = tx._db.total_changes
            tx._db.executemany("INSERT OR IGNORE INTO chronicle(t, kind, subject, data) VALUES(?,?,?,?)",
                               [(float(t), k, s, json.dumps(d, separators=(",", ":"))) for t, k, s, d in rows])
            return tx._db.total_changes - before

    def maybe_collect(self, now: Optional[float] = None) -> int:
        now = self.clock() if now is None else now
        if now - float(self.state()["at"]) < EVERY_S:
            return 0
        return self.collect(now)

    def collect(self, now: Optional[float] = None) -> int:
        """Look at what happened since the last collection. Returns the number of new rows."""
        now = self.clock() if now is None else now
        with self.lock:
            st = self.state()
            since = float(st["at"]) or now - 35 * 86400
            rows = []
            # firmware and software versions: compared with the last collection (the first one only remembers)
            seen = dict(st["fw"])
            first = not seen and not st["at"]
            try:
                devs = self.core.devices()
            except Exception:
                devs = []
            for d in devs:
                fw = str(d.get("firmware") or "").strip()[:40]
                if not fw:
                    continue
                old = seen.get(d["id"])
                if old and old != fw and not first:
                    rows.append((now, "firmware", d["id"], {"title": str(d.get("title") or d.get("ip") or "")[:60], "from": old, "to": fw,
                                                            "kind": d.get("kind") or "unknown"}))
                seen[d["id"]] = fw
            j = getattr(getattr(self.core, "reports", None), "journal", None)
            if j is not None:
                for ev in j.between(since, now, ("dev_new", "app_start")):
                    data = ev.get("data") or {}
                    if ev["kind"] == "dev_new" and data.get("id"):
                        rows.append((ev["t"], "new_device", data["id"], {"title": str(data.get("title") or "")[:60], "ip": str(data.get("ip") or "")[:45],
                                                                        "kind": data.get("kind") or "unknown"}))
                    elif ev["kind"] == "app_start" and data.get("prev") and data.get("prev") != data.get("version"):
                        rows.append((ev["t"], "version", "lanloom", {"from": str(data["prev"])[:20], "to": str(data.get("version") or "")[:20]}))
            inc = getattr(self.core, "incidents", None)
            if inc is not None:
                try:
                    inc.collect(now)
                    for r in self.store.query('SELECT id, start, "end", kind, root, data FROM incident WHERE "end" IS NOT NULL AND "end" > ? AND "end" <= ? '
                                              'AND kind != ? AND "end" - start > ?', (since, now, "maintenance", LONG_S)):
                        d = json.loads(r["data"])
                        rows.append((r["start"], "long_outage", r["root"], {"title": d["root"].get("title") or "", "kind": d["root"].get("kind") or "",
                                                                          "dur": int(r["end"] - r["start"]), "inc": r["id"], "members": len(d.get("members") or [])}))
                except Exception:
                    pass
            n = self._add(rows)
            # forget the versions of devices that are gone (the setting stays small)
            ids = {d["id"] for d in devs}
            st["fw"] = {k: v for k, v in seen.items() if k in ids} if ids else seen
            st["at"] = now
            self.store.set_setting(STATE, st)
            self.store.execute("DELETE FROM chronicle WHERE t < ?", (now - KEEP_DAYS * 86400,))
            return n

    def between(self, since: float, until: float, kinds: tuple = ()) -> list:
        rows = self.store.query("SELECT t, kind, subject, data FROM chronicle WHERE t > ? AND t <= ? ORDER BY t DESC LIMIT ?", (since, until, MAX_LIST))
        out = []
        for r in rows:
            if kinds and r["kind"] not in kinds:
                continue
            try:
                r["data"] = json.loads(r["data"])
            except ValueError:
                r["data"] = {}
            out.append(r)
        return out

    def view(self, days: float = 90, kind: Optional[str] = None, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        self.maybe_collect(now)
        days = max(1.0, min(float(KEEP_DAYS), float(days or 90)))
        items = self.between(now - days * 86400, now, (kind,) if kind in KINDS else ())
        counts = {k: 0 for k in KINDS}
        for r in self.between(now - days * 86400, now):
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        st = self.state()
        return {"t": now, "days": days, "items": items, "counts": counts, "collected": st["at"] or None,
                "first": (self.store.one("SELECT MIN(t) AS t FROM chronicle") or {}).get("t")}
