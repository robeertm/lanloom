"""Backup chains: is every thing that should be copied really copied - "unknown" instead of "ok" when nobody can tell.

A **chain** is one thing that is backed up: a guest of a hypervisor, the backup of a home-automation hub, a timer of a host agent
(names with backup, snapshot, dump, restic, borg), or a chain a person declared and whose script reports to Lanloom
(``POST /api/backups/chains/{id}/run``). For every chain:

* ``ok``      - a success is known and younger than 1.5 times the expected interval, and the source of that knowledge is fresh;
* ``late``    - the newest success is older than that;
* ``failed``  - the newest attempt failed after the newest success;
* ``running`` - a run is going on (the card shows **progress**, never a stopwatch; the progress is an estimate from the throughput
  Lanloom learned from earlier runs, or exact when the job reports it);
* ``none``    - something that should be backed up has no backup at all (a guest outside every job);
* ``unknown`` - never reported, or the source that tells has been silent for 15 minutes: Lanloom does not say ok then.

"Restore last checked on" is a date a person sets (a backup nobody restored is a hope): it is only shown, with a hint when it is
older than 180 days, and never changes a chain's level.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

from . import hubfacts
from .levels import STALE_S, num, worst

KEEP_DAYS = 400
DEFAULT_EXPECT_S = 7 * 86400.0
LATE_FACTOR = 1.5
RESTORE_OLD_DAYS = 180
RUN_SILENT_S = 3600.0              # a script that reported progress and then fell silent for this long (and longer than the learned
                                   # duration) is no longer "running" - otherwise a crashed script would hide a failure for ever
SOURCE_STALE_S = STALE_S
NAME_WORDS = ("backup", "snapshot", "dump", "restic", "borg", "duplic", "rsync", "offsite")
MAX_CHAINS = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_chains (id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '',
    expect_s REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS backup_runs (chain TEXT NOT NULL, t REAL NOT NULL, ok INTEGER NOT NULL, bytes REAL, duration_s REAL);
CREATE INDEX IF NOT EXISTS backup_runs_chain ON backup_runs(chain, t);
CREATE TABLE IF NOT EXISTS backup_checks (chain TEXT PRIMARY KEY, t REAL NOT NULL);
CREATE TABLE IF NOT EXISTS backup_learn (chain TEXT PRIMARY KEY, bps REAL, dur_s REAL, size REAL, n INTEGER NOT NULL DEFAULT 0);
"""
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_ID_RX = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,47}\Z")


def expect_from_schedule(text) -> float:
    """Expected seconds between two backups from a schedule text (``02:00``, ``sat 03:00``, ``mon..fri 21:00``); the default for the rest."""
    s = str(text or "").strip().lower()
    if not s:
        return DEFAULT_EXPECT_S
    if re.fullmatch(r"(\*-\*-\* )?\d{1,2}:\d{2}(:\d{2})?", s) or s in ("daily", "*:0/0"):
        return 86400.0
    if "hour" in s or "/" in s and not any(d in s for d in _DAYS):         # "*:0/15", "*/6:00": several times a day
        return 3600.0 * 3
    if ".." in s and any(d in s for d in _DAYS):
        return 3 * 86400.0                     # weekdays only: the weekend gap is normal
    days = [d for d in _DAYS if d in s]
    if len(days) == 1:
        return 7 * 86400.0
    if len(days) > 1:
        return 3.5 * 86400.0
    return DEFAULT_EXPECT_S


def judge(last_ok, last_fail, running, expect_s, source_age, now, ever_seen=True) -> tuple:
    """(status, level) from the facts; the only place that decides what a chain's state is."""
    if running:
        return "running", "ok"
    if source_age is not None and source_age > SOURCE_STALE_S:
        return "unknown", "unknown"
    if last_fail is not None and (last_ok is None or last_fail > last_ok):
        return "failed", "danger"
    if last_ok is None:
        return ("none", "warn") if ever_seen else ("unknown", "unknown")
    return ("ok", "ok") if now - last_ok <= expect_s * LATE_FACTOR else ("late", "warn")


def parse_time(value) -> Optional[float]:
    n = num(value)
    if n is not None and n > 1e9:
        return n
    s = str(value or "").strip()
    if not s or s in ("unknown", "unavailable", "none"):
        return None
    import datetime as dt
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def _free_pct(store) -> Optional[float]:
    """Free share of a backup storage in percent (None when the storage or its size is not known)."""
    total = num((store or {}).get("total"))
    if not total or total <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1 - (num(store.get("used")) or 0.0) / total))), 1)


class BackupWatch:
    def __init__(self, core, clock=time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self._lock = threading.RLock()
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.store.execute(stmt)
        self.live: dict = {}               # chain id -> {"since": t, "pct": float|None} while a run is going on

    # ------------------------------------------------------------------ chains a person declares
    def manual(self) -> list:
        return [dict(r) for r in self.store.query("SELECT id, name, source, target, expect_s, created FROM backup_chains ORDER BY name")]

    def save_chain(self, chain_id: str, data: dict, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        cid = str(chain_id or "").strip().lower()
        if not _ID_RX.match(cid):
            raise ValueError("chain.err.id")
        name = str(data.get("name") or "").strip()[:64]
        if not name:
            raise ValueError("chain.err.name")
        hours = num(data.get("expect_hours"))
        if hours is None or not 1 <= hours <= 24 * 90:
            raise ValueError("chain.err.expect")
        if not self.store.one("SELECT id FROM backup_chains WHERE id=?", (cid,)) and len(self.manual()) >= 60:
            raise ValueError("chain.err.many")
        self.store.execute("INSERT INTO backup_chains(id, name, source, target, expect_s, created) VALUES (?,?,?,?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET name=excluded.name, source=excluded.source, target=excluded.target, expect_s=excluded.expect_s",
                           (cid, name, str(data.get("source") or "")[:64], str(data.get("target") or "")[:64], hours * 3600.0, now))
        return {"id": cid}

    def delete_chain(self, chain_id: str) -> bool:
        if not self.store.one("SELECT id FROM backup_chains WHERE id=?", (chain_id,)):
            return False
        for t in ("backup_chains", "backup_runs", "backup_learn"):
            self.store.execute("DELETE FROM %s WHERE %s=?" % (t, "id" if t == "backup_chains" else "chain"), (chain_id,))
        self.store.execute("DELETE FROM backup_checks WHERE chain IN (?, ?)", (chain_id, "manual:" + chain_id))      # keyed by the id of the list
        self.live.pop(chain_id, None)
        return True

    def report_run(self, chain_id: str, ok: bool, size=None, duration_s=None, now: Optional[float] = None) -> bool:
        """A script says: this chain just finished (``ok`` or not), optionally with its size and duration (-> learned throughput)."""
        if not self.store.one("SELECT id FROM backup_chains WHERE id=?", (chain_id,)):
            return False
        now = self.clock() if now is None else now
        size, dur = num(size), num(duration_s)
        self.store.execute("INSERT INTO backup_runs(chain, t, ok, bytes, duration_s) VALUES (?,?,?,?,?)",
                           (chain_id, now, 1 if ok else 0, size, dur))
        self.store.execute("DELETE FROM backup_runs WHERE t < ?", (now - KEEP_DAYS * 86400,))
        self.live.pop(chain_id, None)
        if ok:
            self._learn(chain_id, size, dur)
        return True

    def report_progress(self, chain_id: str, pct, now: Optional[float] = None) -> bool:
        if not self.store.one("SELECT id FROM backup_chains WHERE id=?", (chain_id,)):
            return False
        now = self.clock() if now is None else now
        p = num(pct)
        cur = self.live.get(chain_id) or {"since": now}
        self.live[chain_id] = {"since": cur["since"], "pct": None if p is None else max(0.0, min(99.0, p)), "reported": now}
        return True

    def set_restore_checked(self, chain_id: str, t: Optional[float] = None, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        t = now if t is None else num(t)
        if t is None or t > now + 60 or t < now - 20 * 365 * 86400:
            raise ValueError("chain.err.date")
        self.store.execute("INSERT INTO backup_checks(chain, t) VALUES (?,?) ON CONFLICT(chain) DO UPDATE SET t=excluded.t", (chain_id, t))

    # ------------------------------------------------------------------ learned throughput
    def _learn(self, cid: str, size, dur) -> None:
        if not dur or dur <= 0:
            return
        row = self.store.one("SELECT bps, dur_s, size, n FROM backup_learn WHERE chain=?", (cid,)) or {"bps": None, "dur_s": None, "size": None, "n": 0}
        bps = (size / dur) if size else None

        def ewma(old, new):
            return new if old is None or new is None else round(0.7 * old + 0.3 * new, 3)
        self.store.execute("INSERT OR REPLACE INTO backup_learn(chain, bps, dur_s, size, n) VALUES (?,?,?,?,?)",
                           (cid, ewma(row["bps"], bps) if bps else row["bps"], ewma(row["dur_s"], dur), ewma(row["size"], size) if size else row["size"], row["n"] + 1))

    def learned(self, cid: str) -> dict:
        return self.store.one("SELECT bps, dur_s, size, n FROM backup_learn WHERE chain=?", (cid,)) or {"bps": None, "dur_s": None, "size": None, "n": 0}

    def _expire_silent(self, now: float) -> None:
        """Forget runs a script announced and never finished (a crashed script reports nothing more)."""
        for cid, lv in list(self.live.items()):
            if lv.get("reported") is None:
                continue
            limit = max(RUN_SILENT_S, 1.5 * (self.learned(cid)["dur_s"] or 0.0))
            if now - lv["reported"] > limit:
                self.live.pop(cid, None)

    def _progress(self, cid: str, now: float) -> dict:
        lv = self.live.get(cid) or {}
        since = lv.get("since", now)
        learn = self.learned(cid)
        pct = lv.get("pct")
        basis = "reported" if pct is not None else ""
        if pct is None and learn["dur_s"]:
            pct, basis = min(95.0, 100.0 * max(0.0, now - since) / learn["dur_s"]), "learned"
        eta = None
        if pct is not None and learn["dur_s"] and basis == "learned":
            eta = max(0.0, learn["dur_s"] - (now - since))
        return {"pct": None if pct is None else round(pct, 1), "eta_s": None if eta is None else round(eta), "basis": basis,
                "learned_bps": learn["bps"], "learned_runs": learn["n"]}

    def _track(self, cid: str, running: bool, started, now: float, ended_ok=None) -> None:
        """Remember when a run started; when it ends cleanly, learn how long it took."""
        lv = self.live.get(cid)
        if running and lv is None:
            self.live[cid] = {"since": started or now, "pct": None}
        elif not running and lv is not None and not lv.get("reported"):
            if ended_ok:
                self._learn(cid, None, max(1.0, now - lv["since"]))
            self.live.pop(cid, None)

    # ------------------------------------------------------------------ chains found in the sources
    def _from_hypervisor(self, now: float) -> list:
        out = []
        core = self.core
        try:
            entries = core.live_configs(86400.0)
        except Exception:
            entries = []
        for e in entries:
            st = e.get("state") or {}
            guests = st.get("guests") or []
            if not guests:
                continue
            jobs = [j for j in st.get("backup_jobs") or [] if j.get("enabled", True)]
            stores = {}
            for sto in st.get("storage") or []:                # a job names its storage by name, the list also has opaque ids
                for k in (sto.get("name"), sto.get("id")):
                    if k:
                        stores.setdefault(k, sto)
            for g in guests:
                if g.get("template"):
                    continue                                       # a template is not a thing to back up
                b = g.get("backup") or {}
                job = next((j for j in jobs if g["id"] in (j.get("guests") or []) or (j.get("selection") == "all" and g["id"] not in (j.get("excluded") or []))), None)
                expect = expect_from_schedule(job.get("schedule")) if job else DEFAULT_EXPECT_S
                last_ok, fail = num(b.get("last_t")), num(b.get("failed_t"))
                running = False
                out.append({"id": "hv:%s:%s" % (e["key"].split(":")[-1], g["id"]), "name": g.get("name") or "?", "kind": "hypervisor",
                            "source": "%s %s" % ("VM" if g.get("kind") == "vm" else "CT", g.get("vmid") or ""), "target": (job or {}).get("storage") or "",
                            "last_ok": last_ok, "fail": fail, "running": running, "expect_s": expect, "source_age": float(e.get("age_s") or 0),
                            "ever": bool(job) or last_ok is not None or (isinstance(g.get("backup"), dict) and g.get("status") == "running"),    # a running guest outside every job: "none", not "unknown"
                            "next_t": num((job or {}).get("next_t")), "origin": e.get("name") or "",
                            "target_free": _free_pct(stores.get((job or {}).get("storage")))})
        return out

    def _from_hub(self, now: float) -> list:
        by: dict = {}
        for ent in hubfacts.entities(self.core):
            if "backup" not in ent["text"] or ent["state"] in (None, "unavailable"):      # an entity that tells nothing is no reading
                continue
            g = by.setdefault(ent["integ"], {"t": 0.0})
            text = ent["text"]
            if "last successful" in text or "last backup" in text:
                g["last_ok"] = parse_time(ent["state"])
            elif "next" in text and "schedul" in text:
                g["next_t"] = parse_time(ent["state"])
            elif "state" in text or "manager" in text:
                g["state"] = str(ent["state"] or "")
            elif ent["domain"] == "event":
                g["event"] = str(ent["state"] or "")
            if ent["t"]:
                g["t"] = max(g["t"], float(ent["t"]))
        out = []
        for integ, g in by.items():
            if "last_ok" not in g and "state" not in g:
                continue
            running = g.get("state") in ("create_backup", "receive_backup", "in_progress", "creating")
            blocked = g.get("state") == "blocked"
            failed_t = g["t"] if str(g.get("event") or "").lower() in ("failed", "failure") else None
            out.append({"id": "hub:%s" % integ, "name": "hub-backup", "kind": "hub", "source": "", "target": "", "last_ok": g.get("last_ok"),
                        "fail": failed_t, "running": running, "expect_s": 86400.0, "source_age": max(0.0, now - g["t"]) if g["t"] else None,
                        "ever": True, "next_t": g.get("next_t"), "origin": "", "blocked": blocked})
        return out

    def _from_agents(self, now: float) -> list:
        out = []
        agents = getattr(self.core, "agents", None)
        if agents is None:
            return out
        for aid, st in list((agents.live or {}).items()):
            units = st.get("units") or {}
            host = None
            for tm in units.get("timers") or []:
                name = str(tm.get("name") or "")
                if not any(w in name.lower() for w in NAME_WORDS):
                    continue
                last = num(tm.get("last"))
                nxt = num(tm.get("next"))
                gap = (nxt - last) if last and nxt and nxt > last else 86400.0
                failed = tm.get("unit_state") == "failed" or str(tm.get("unit_result") or tm.get("result") or "success") not in ("success", "")
                running = tm.get("unit_state") in ("active", "activating")
                if host is None:
                    try:
                        host = agents.device_of(aid)
                    except Exception:
                        host = None
                out.append({"id": "agent:%s:%s" % (aid, name.rsplit(".", 1)[0]), "name": name.rsplit(".", 1)[0], "kind": "agent", "source": "", "target": "",
                            "last_ok": None if failed else last, "fail": last if failed else None, "running": running, "expect_s": gap,
                            "source_age": max(0.0, now - float(st.get("t") or 0)) if st.get("t") else None, "ever": last is not None,
                            "next_t": nxt, "origin": "", "device": host})
        return out

    def _from_manual(self, now: float) -> list:
        out = []
        for c in self.manual():
            ok = self.store.one("SELECT MAX(t) AS t FROM backup_runs WHERE chain=? AND ok=1", (c["id"],))
            bad = self.store.one("SELECT MAX(t) AS t FROM backup_runs WHERE chain=? AND ok=0", (c["id"],))
            out.append({"id": "manual:" + c["id"], "key": c["id"], "name": c["name"], "kind": "manual", "source": c["source"], "target": c["target"],
                        "last_ok": ok["t"] if ok else None, "fail": bad["t"] if bad else None, "running": c["id"] in self.live, "expect_s": c["expect_s"],
                        "source_age": None, "ever": bool((ok and ok["t"]) or (bad and bad["t"])), "next_t": None, "origin": ""})
        return out

    # ------------------------------------------------------------------ the answer
    def chains(self, now: Optional[float] = None) -> list:
        now = self.clock() if now is None else now
        with self._lock:
            self._expire_silent(now)
            raw = self._from_manual(now) + self._from_hypervisor(now) + self._from_hub(now) + self._from_agents(now)
            checks = {r["chain"]: r["t"] for r in self.store.query("SELECT chain, t FROM backup_checks")}
            out = []
            for c in raw[:MAX_CHAINS]:
                cid = c["id"]
                live_key = c.get("key") or cid
                if c["kind"] != "manual":
                    self._track(cid, c["running"], None, now, ended_ok=c["last_ok"] is not None and not c["fail"])
                    running = cid in self.live
                else:
                    running = c["running"]
                    live_key = c["key"]
                status, level = judge(c["last_ok"], c["fail"], running, c["expect_s"], c["source_age"], now, c["ever"])
                if c["kind"] == "manual" and not c["ever"] and status != "running":
                    status, level = "unknown", "unknown"
                if c.get("blocked") and status == "ok":
                    status, level = "late", "warn"
                age = None if c["last_ok"] is None else max(0.0, now - c["last_ok"])
                rc = checks.get(cid)
                out.append({"id": cid, "name": c["name"], "kind": c["kind"], "source": c["source"], "target": c["target"], "status": status, "level": level,
                            "last_ok_t": c["last_ok"], "last_fail_t": c["fail"], "age_s": None if age is None else round(age), "expect_s": c["expect_s"],
                            "next_t": c.get("next_t"), "progress": self._progress(live_key, now) if status == "running" else None,
                            "restore_checked_t": rc, "restore_hint": "never" if rc is None else ("old" if now - rc > RESTORE_OLD_DAYS * 86400 else "ok"),
                            "device": c.get("device"), "editable": c["kind"] == "manual", "origin": c.get("origin") or "",
                            "target_free_pct": c.get("target_free")})
        order = {"danger": 0, "warn": 1, "unknown": 2, "ok": 3}
        out.sort(key=lambda r: (order.get(r["level"], 4), r["name"].lower()))
        return out

    def view(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        rows = self.chains(now)
        sm = {k: 0 for k in ("ok", "late", "failed", "running", "unknown", "none")}
        for r in rows:
            sm[r["status"]] = sm.get(r["status"], 0) + 1
        return {"t": now, "chains": rows, "summary": sm, "level": worst(r["level"] for r in rows) if rows else "none",
                "restore_old_days": RESTORE_OLD_DAYS}
