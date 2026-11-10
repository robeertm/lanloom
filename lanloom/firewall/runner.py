"""The executor pipeline: verify -> gate -> backup -> apply -> settle -> watch -> done, or roll back on its own.

Used twice: by the external executor service (real target, signature required) and inside the web app for the
simulation target (sample network in memory, no signature needed because nothing is written anywhere).

Files in ``state_dir`` (created 0700, files 0600, never followed through symbolic links):
  backup/<plan>/before.json   raw snapshot of every touched object, written before the first change
  protocol.jsonl              one line per run: plan hash, key id, user, times, result (no secrets, no addresses)
  nonces.json, counters.json  replay protection and signature counters
  active.json                 the running plan; a leftover file after a crash makes the next start roll back first
"""
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path

from . import model
from .canon import digest
from .signature import Invalid, verify_assertion

log = logging.getLogger("lanloom.executor")
NONCE_KEEP = 500


def _safe_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError("state path is not a directory: %s" % path)
    return path


def write_private(path: Path, data, exclusive: bool = False) -> None:
    """Write JSON with mode 0600 without following symbolic links (exclusive: the file must not exist yet)."""
    text = json.dumps(data, indent=1, sort_keys=True).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    if exclusive:
        fd = os.open(path, flags | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(text)
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, flags | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_private(path: Path, default):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return default
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return default
        try:
            return json.loads(fh.read().decode("utf-8"))
        except ValueError:
            return default


class Runner:
    def __init__(self, adapter, state_dir, *, target: str, require_signature: bool = True, keys=None, origins=(),
                 watch_s: float = 3600, check_s: float = 60, quiet_hours: str = "", report=None,
                 clock=time.time, sleep=time.sleep):
        self.adapter = adapter
        self.dir = _safe_dir(Path(state_dir))
        _safe_dir(self.dir / "backup")
        self.target = target
        self.require_signature = require_signature
        self.keys = keys if keys is not None else []
        self.origins = tuple(origins)
        self.watch_s, self.check_s = watch_s, check_s
        self.quiet_hours = quiet_hours
        self.report = report or (lambda ev: None)
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.active = None

    # ------------------------------------------------------------------ helpers
    def _emit(self, plan_id, stage, status="running", **extra):
        ev = dict({"plan": plan_id, "stage": stage, "status": status, "t": int(self.clock())}, **extra)
        try:
            self.report(ev)
        except Exception:
            log.exception("progress report failed")
        return ev

    def _protocol(self, entry: dict) -> None:
        path = self.dir / "protocol.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def history(self, limit: int = 20) -> list:
        try:
            lines = (self.dir / "protocol.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return list(reversed(out))

    def busy(self) -> bool:
        return self.active is not None or (self.dir / "active.json").exists()

    # ------------------------------------------------------------------ crash recovery
    def recover(self) -> dict:
        """A leftover active.json means the process died mid-run: restore the backup before anything else."""
        active = read_private(self.dir / "active.json", None)
        if not active:
            return {}
        snap = read_private(self.dir / "backup" / active.get("plan", "?") / "before.json", None)
        result = {"plan": active.get("plan"), "status": "rolled_back", "reason": "fw.res.recovered"}
        try:
            if snap and active.get("applied"):
                self.adapter.restore(snap["snapshot"])
        except Exception as exc:
            result = {"plan": active.get("plan"), "status": "failed", "reason": "fw.res.rollback_failed", "detail": type(exc).__name__}
        os.unlink(self.dir / "active.json")
        self._protocol(dict(result, t=int(self.clock())))
        self._emit(result["plan"], "done", result["status"], reason=result["reason"])
        return result

    # ------------------------------------------------------------------ the run
    def run(self, plan: dict, assertion=None) -> dict:
        if not self.lock.acquire(blocking=False):
            return self._refuse(plan, model.Refused("fw.block.busy"))
        try:
            return self._run(plan, assertion)
        finally:
            self.active = None
            self.lock.release()

    def _refuse(self, plan, exc) -> dict:
        pid = plan.get("id") if isinstance(plan, dict) else None
        key = exc.key if isinstance(exc, model.Refused) else "fw.block.signature"
        vars_ = exc.vars if isinstance(exc, model.Refused) else {"why": str(exc)}
        self._emit(pid, "gate", "refused", reason=key, vars=vars_)
        self._protocol({"plan": pid, "status": "refused", "reason": key, "t": int(self.clock())})
        return {"plan": pid, "status": "refused", "reason": key, "vars": vars_}

    def _run(self, plan, assertion) -> dict:
        now = self.clock()
        started = int(now)
        key_id = None
        try:
            model.validate_plan(plan)
            pid = plan["id"]
            self._emit(pid, "verify")
            nonces = read_private(self.dir / "nonces.json", [])
            if plan["nonce"] in nonces:
                raise model.Refused("fw.block.nonce")
            if self.require_signature:
                counters = read_private(self.dir / "counters.json", {})
                try:
                    key_id, count = verify_assertion(assertion, digest(plan), self.keys, self.origins, counters)
                except Invalid as exc:
                    raise model.Refused("fw.block.signature", why=str(exc))
                counters[key_id] = count
                write_private(self.dir / "counters.json", counters)
            write_private(self.dir / "nonces.json", (nonces + [plan["nonce"]])[-NONCE_KEEP:])
            self._emit(pid, "gate")
            state = self.adapter.read()
            model.gate(state, plan, target=self.target, now=now, quiet=model.in_quiet_hours(self.quiet_hours, now),
                       busy=(self.dir / "active.json").exists(), healthy=self.adapter.health())
        except model.Refused as exc:
            return self._refuse(plan, exc)
        self.active = pid
        steps = plan["steps"]
        ids = [s["device"] for s in steps]
        # backup and the way back exist before the first write
        self._emit(pid, "backup")
        snap = self.adapter.snapshot(ids)
        bdir = _safe_dir(self.dir / "backup" / pid)
        write_private(bdir / "before.json", {"plan": pid, "t": started, "snapshot": snap}, exclusive=True)
        back = model.inverse(steps)
        write_private(self.dir / "active.json", {"plan": pid, "applied": False, "t": started})
        result = {"plan": pid, "status": "done", "reason": "fw.res.done", "inverse": back, "started": started, "key": key_id}
        try:
            self._emit(pid, "apply")
            write_private(self.dir / "active.json", {"plan": pid, "applied": True, "t": started})
            for st in steps:
                self.adapter.apply(st)
            self._emit(pid, "settle")
            self.adapter.settle(steps)
            until = self.clock() + self.watch_s
            self._emit(pid, "watch", watch_until=int(until))
            result.update(self._watch(pid, steps, snap, until))
        except Exception as exc:
            log.warning("apply failed: %s", type(exc).__name__)
            result.update(self._rollback(pid, snap, None, "fw.res.apply_failed"))
        try:
            os.unlink(self.dir / "active.json")
        except FileNotFoundError:
            pass
        result["finished"] = int(self.clock())
        self._protocol({"plan": pid, "hash": digest(plan).hex()[:16], "key": key_id, "user": plan["requester"].get("user"),
                        "steps": len(steps), "status": result["status"], "reason": result["reason"],
                        "started": started, "finished": result["finished"], "undo_of": plan.get("undo_of")})
        self._emit(pid, "done", result["status"], reason=result["reason"], inverse=result.get("inverse"))
        return result

    def _watch(self, pid, steps, snap, until) -> dict:
        strikes = {}
        while True:
            pr = self.adapter.probe(steps)
            if not pr.get("global_ok", True):
                return self._rollback(pid, snap, None, "fw.res.rolled_back_global")
            missing = set(pr.get("missing") or [])
            for dev in list(strikes):
                if dev not in missing:
                    strikes.pop(dev)
            for dev in missing:
                strikes[dev] = strikes.get(dev, 0) + 1
            failed = sorted(d for d, n in strikes.items() if n >= 2)
            if failed:
                return self._rollback(pid, snap, failed, "fw.res.rolled_back_device")
            if self.clock() >= until:
                return {}
            self.sleep(min(self.check_s, max(0.0, until - self.clock())))

    def _rollback(self, pid, snap, only, reason) -> dict:
        """At most one automatic rollback: everything, or only the devices that did not come back."""
        self._emit(pid, "rollback")
        part = snap if not only or "devices" not in snap else {"devices": [d for d in snap["devices"] if d.get("id") in only]}
        try:
            self.adapter.restore(part)
            self.adapter.settle([])
        except Exception as exc:
            log.error("rollback failed: %s", type(exc).__name__)
            return {"status": "failed", "reason": "fw.res.rollback_failed", "inverse": None}
        return {"status": "rolled_back", "reason": reason, "inverse": None, "rolled_back": only or "all"}
