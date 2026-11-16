"""Audit log: who changed what, when, from where.

* Append-only table `audit`. Every entry carries the hash of the entry before it and its own hash, an HMAC-SHA-256 over
  both, keyed with a key derived from the instance key (secret.key). Editing, inserting or deleting an entry in the
  middle breaks the chain, and somebody who only has the database cannot compute valid hashes for forged entries.
* The newest hash is also kept in the meta table, so cutting entries off the end is noticed as long as that record
  is not rolled back together with them.
* Entries older than the retention (default 365 days) are removed; the hash of the last removed entry becomes the
  anchor the remaining chain starts from.
* Never secrets: details pass through scrub(), which drops values under names that look like secrets, shortens long
  texts and limits depth. Handlers only hand over curated details (names of changed fields, never request bodies).
"""
import csv
import hashlib
import hmac
import io
import json
import re
import threading
import time
from typing import Optional

from .store import Store

RETENTION_DEFAULT = 365
RETENTION_CHOICES = (30, 90, 180, 365, 730, 1825, 3650)
PAGE_MAX = 500
EXPORT_MAX = 100000

_SECRET_NAME = re.compile(r"(pass(word|phrase)?|secret|token|api[_-]?key|auth[_-]?key|priv[_-]?key|community|otp|"
                          r"cookie|credential|assertion|recovery|^code$|_code$|^key$|signature|bearer)", re.I)
_FIELDS = ("id", "t", "actor", "actor_id", "role", "action", "target", "outcome", "ip", "agent", "detail")


def scrub(value, depth: int = 0):
    """A copy of `value` that is safe to log."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:40]:
            k = str(k)[:60]
            out[k] = "[hidden]" if _SECRET_NAME.search(k) else scrub(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [scrub(v, depth + 1) for v in items[:20]]
        if len(items) > 20:
            out.append("... +%d" % (len(items) - 20))
        return out
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    text = str(value)
    return text if len(text) <= 200 else text[:197] + "..."


def _canon(entry: dict) -> bytes:
    return json.dumps([entry.get(f) for f in _FIELDS], separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")


class AuditLog:
    def __init__(self, store: Store, key: bytes, clock=time.time):
        self.store = store
        self.key = key
        self.clock = clock
        self._lock = threading.Lock()
        self._last_prune = 0.0

    def _mac(self, prev: str, entry: dict) -> str:
        return hmac.new(self.key, prev.encode("ascii") + b"\n" + _canon(entry), hashlib.sha256).hexdigest()

    def _meta(self, key: str) -> str:
        row = self.store.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else ""

    def _set_meta(self, key: str, value: str) -> None:
        self.store.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    # ------------------------------------------------------------------ write
    def record(self, action: str, actor: Optional[dict] = None, target: str = "", outcome: str = "ok", ip: str = "",
               agent: str = "", detail=None, actor_name: Optional[str] = None) -> int:
        actor = actor or {}
        entry = {"t": round(float(self.clock()), 3),
                 "actor": str(actor_name if actor_name is not None else actor.get("username") or "")[:64],
                 "actor_id": actor.get("id") if isinstance(actor.get("id"), int) else None,
                 "role": str(actor.get("role") or "")[:16], "action": str(action)[:64], "target": str(target or "")[:120],
                 "outcome": str(outcome)[:16], "ip": str(ip or "")[:64], "agent": str(agent or "")[:80],
                 "detail": json.dumps(scrub(detail or {}), separators=(",", ":"), ensure_ascii=False, sort_keys=True)}
        with self._lock, self.store.transaction() as tx:
            last = tx.one("SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1")
            prev = last["hash"] if last else (self._meta("audit_anchor") or "")
            entry["id"] = (last["id"] if last else int(self._meta("audit_anchor_id") or 0)) + 1
            entry["hash"] = self._mac(prev, entry)
            tx.execute("INSERT INTO audit(id, t, actor, actor_id, role, action, target, outcome, ip, agent, detail, prev, hash)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (entry["id"], entry["t"], entry["actor"], entry["actor_id"], entry["role"], entry["action"], entry["target"],
                        entry["outcome"], entry["ip"], entry["agent"], entry["detail"], prev, entry["hash"]))
            self._set_meta("audit_head", "%d:%s" % (entry["id"], entry["hash"]))
        if entry["t"] - self._last_prune > 3600:
            self.prune()
        return entry["id"]

    # ------------------------------------------------------------------ retention
    def retention_days(self) -> int:
        v = self.store.get_setting("audit_retention_days", RETENTION_DEFAULT)
        return v if v in RETENTION_CHOICES else RETENTION_DEFAULT

    def prune(self, now: Optional[float] = None) -> int:
        now = float(self.clock() if now is None else now)
        self._last_prune = now
        cutoff = now - self.retention_days() * 86400
        with self._lock, self.store.transaction() as tx:
            last_old = tx.one("SELECT id, hash FROM audit WHERE t < ? ORDER BY id DESC LIMIT 1", (cutoff,))
            if not last_old:
                return 0
            n = tx.execute("DELETE FROM audit WHERE id <= ?", (last_old["id"],)).rowcount
            self._set_meta("audit_anchor", last_old["hash"])
            self._set_meta("audit_anchor_id", str(last_old["id"]))
        return n

    def rewrite(self, entries: list) -> int:
        """Replace the whole log with `entries` (oldest first), numbered from 1 and chained with this instance's key.

        Only used when a backup is restored into an instance without history of its own (lanloom/backup.py): the
        backup's log (checked against the old key when it was written) moves along, followed by the few entries of the
        new instance. The restore itself is recorded right after, naming how many entries came from the file."""
        n = 0
        with self._lock, self.store.transaction() as tx:
            tx.execute("DELETE FROM audit")
            prev = ""
            for src in entries:
                if not isinstance(src, dict):
                    continue
                n += 1
                detail = src.get("detail")
                if not isinstance(detail, str):
                    detail = json.dumps(scrub(detail or {}), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
                try:
                    t = round(float(src.get("t") or 0), 3)
                except (TypeError, ValueError):
                    t = 0.0
                entry = {"id": n, "t": t, "actor": str(src.get("actor") or "")[:64],
                         "actor_id": src.get("actor_id") if isinstance(src.get("actor_id"), int) and not isinstance(src.get("actor_id"), bool) else None,
                         "role": str(src.get("role") or "")[:16], "action": str(src.get("action") or "")[:64],
                         "target": str(src.get("target") or "")[:120], "outcome": str(src.get("outcome") or "")[:16],
                         "ip": str(src.get("ip") or "")[:64], "agent": str(src.get("agent") or "")[:80], "detail": detail[:4000]}
                entry["hash"] = self._mac(prev, entry)
                tx.execute("INSERT INTO audit(id, t, actor, actor_id, role, action, target, outcome, ip, agent, detail, prev, hash)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (entry["id"], entry["t"], entry["actor"], entry["actor_id"], entry["role"], entry["action"], entry["target"],
                            entry["outcome"], entry["ip"], entry["agent"], entry["detail"], prev, entry["hash"]))
                prev = entry["hash"]
            tx.execute("DELETE FROM meta WHERE key IN ('audit_anchor', 'audit_anchor_id', 'audit_head')")
            if n:
                tx.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('audit_head', ?)", ("%d:%s" % (n, prev),))
        return n

    # ------------------------------------------------------------------ check
    def verify(self) -> dict:
        """Walk the chain. {"ok", "count", "broken_at", "reason"}."""
        expected = self._meta("audit_anchor") or ""
        expected_id = int(self._meta("audit_anchor_id") or 0)
        count = 0
        last = None
        for row in self.store.query("SELECT * FROM audit ORDER BY id"):
            count += 1
            if row["id"] != expected_id + 1:
                return {"ok": False, "count": count, "broken_at": row["id"], "reason": "audit.chain.gap"}
            if row["prev"] != expected:
                return {"ok": False, "count": count, "broken_at": row["id"], "reason": "audit.chain.link"}
            if not hmac.compare_digest(self._mac(expected, row), row["hash"]):
                return {"ok": False, "count": count, "broken_at": row["id"], "reason": "audit.chain.changed"}
            expected, expected_id, last = row["hash"], row["id"], row
        head = self._meta("audit_head")
        if head and (last is None or head != "%d:%s" % (last["id"], last["hash"])):
            return {"ok": False, "count": count, "broken_at": int(head.split(":")[0]), "reason": "audit.chain.tail"}
        return {"ok": True, "count": count, "broken_at": None, "reason": ""}

    # ------------------------------------------------------------------ read
    @staticmethod
    def _where(f: dict) -> tuple:
        clauses, args = [], []
        if f.get("actor"):
            clauses.append("actor = ? COLLATE NOCASE")
            args.append(str(f["actor"])[:64])
        if f.get("action"):
            a = str(f["action"])[:64]
            if a.endswith("."):
                clauses.append("action LIKE ?")
                args.append(a.replace("%", "").replace("_", r"\_") + "%")
            else:
                clauses.append("action = ?")
                args.append(a)
        if f.get("outcome"):
            clauses.append("outcome = ?")
            args.append(str(f["outcome"])[:16])
        for key, op in (("since", ">="), ("until", "<")):
            try:
                if f.get(key) not in (None, ""):
                    clauses.append("t %s ?" % op)
                    args.append(float(f[key]))
            except (TypeError, ValueError):
                pass
        if f.get("q"):
            q = "%" + str(f["q"])[:80].replace("%", "") + "%"
            clauses.append("(target LIKE ? OR detail LIKE ? OR ip LIKE ? OR action LIKE ?)")
            args += [q, q, q, q]
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def query(self, filters: Optional[dict] = None, limit: int = 100, before: Optional[int] = None) -> list:
        f = dict(filters or {})
        where, args = self._where(f)
        if before:
            where += (" AND " if where else " WHERE ") + "id < ?"
            args.append(int(before))
        limit = max(1, min(PAGE_MAX, int(limit or 100)))
        rows = self.store.query("SELECT id, t, actor, role, action, target, outcome, ip, agent, detail FROM audit%s ORDER BY id DESC LIMIT ?"
                                % where, tuple(args) + (limit,))
        for r in rows:
            try:
                r["detail"] = json.loads(r["detail"])
            except ValueError:
                r["detail"] = {}
        return rows

    def actions(self) -> list:
        return [r["action"] for r in self.store.query("SELECT DISTINCT action FROM audit ORDER BY action")]

    def export_csv(self, filters: Optional[dict] = None) -> str:
        where, args = self._where(dict(filters or {}))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "time_utc", "user", "role", "action", "target", "outcome", "address", "device", "detail", "hash"])
        for r in self.store.query("SELECT * FROM audit%s ORDER BY id LIMIT ?" % where, tuple(args) + (EXPORT_MAX,)):
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["t"]))
            w.writerow([_cell(v) for v in (r["id"], when, r["actor"], r["role"], r["action"], r["target"], r["outcome"], r["ip"],
                                           r["agent"], r["detail"], r["hash"])])
        return buf.getvalue()

    def between(self, since: float, until: float, prefixes: tuple = ()) -> list:
        rows = self.store.query("SELECT id, t, actor, role, action, target, outcome, ip, detail FROM audit WHERE t > ? AND t <= ? ORDER BY id",
                                (since, until))
        out = []
        for r in rows:
            if prefixes and not r["action"].startswith(prefixes):
                continue
            try:
                r["detail"] = json.loads(r["detail"])
            except ValueError:
                r["detail"] = {}
            out.append(r)
        return out


def _cell(v) -> str:
    """CSV cell that a spreadsheet will not run as a formula."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s
