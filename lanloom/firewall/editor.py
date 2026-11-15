"""Firewall editor service inside the web app: view, draft, preview, plan, approval, progress, undo.

Two modes (``LANLOOM_FW_EXECUTOR``):

* ``simulation`` (default): the sample network lives in this process. Applying runs the real executor pipeline
  against it, shortened, and never contacts anything.
* ``external``: an executor service publishes the controller state and pulls approved plans. Every plan needs a
  passkey signature over its hash; the executor verifies it again with keys only it holds.
"""
import os
import threading
import time
from pathlib import Path

from . import model
from . import steps as stepkinds
from .adapter import SimulatedController
from .canon import digest
from ..updates import is_older as _older
from .signature import Invalid, b64u, cose_to_xy, key_fingerprint, verify_assertion

DRAFT_TTL_S = 24 * 3600
UNDO_TTL_S = 24 * 3600
STATE_STALE_S = 120


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _token() -> str:
    path = os.environ.get("LANLOOM_EXECUTOR_TOKEN_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get("LANLOOM_EXECUTOR_TOKEN", "").strip()


class Editor:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        mode = os.environ.get("LANLOOM_FW_EXECUTOR", "simulation").strip().lower()
        self.mode = "external" if mode == "external" else "simulation"
        self.quiet_hours = os.environ.get("LANLOOM_FW_QUIET_HOURS", "").strip()
        self.token = _token() if self.mode == "external" else ""
        self.pending = {}          # plan id -> {"plan", "user", "t"}  (built, not yet approved)
        self.queue = []            # external: approved plans waiting for the executor
        self.progress = {}         # plan id -> last progress event
        self.ext_state = None      # external: last state published by the executor
        self.ext_info = {}
        self.ext_caps = None
        self.ext_seen = 0.0
        self._lock = threading.Lock()
        self._sim = None
        self._runner = None

    # ------------------------------------------------------------------ simulation
    def sim(self):
        if self._sim is None:
            saved = self.store.get_setting("fw_sim_state", None)
            self._sim = SimulatedController(state=saved if isinstance(saved, dict) and saved.get("devices") else None,
                                            persist=lambda s: self.store.set_setting("fw_sim_state", s),
                                            settle_s=float(_env_int("LANLOOM_FW_SIM_SETTLE_S", 3)))
        return self._sim

    def runner(self):
        if self._runner is None:
            from .runner import Runner
            self._runner = Runner(self.sim(), Path(self.cfg.data_dir) / "firewall-simulation", target="simulation",
                                  require_signature=False, watch_s=_env_int("LANLOOM_FW_SIM_WATCH_S", 20), check_s=2,
                                  quiet_hours=self.quiet_hours, report=self.on_progress)
        return self._runner

    # ------------------------------------------------------------------ capabilities
    def capabilities(self) -> dict:
        """Validated capability list of the adapter behind the editor. An executor of an earlier release sends
        none: then the v1 contract applies (moves only)."""
        from ..integrations import capabilities as capmod
        if self.mode == "simulation":
            return capmod.validate(self.sim().capabilities())
        return self.ext_caps or capmod.V1_DEFAULT

    def plan_version_max(self) -> int:
        if self.mode == "simulation":
            return stepkinds.PLAN_VERSION_MAX
        v = self.ext_info.get("plan_version_max")
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else 1

    # ------------------------------------------------------------------ state
    def state(self):
        if self.mode == "simulation":
            return self.sim().read()
        if self.ext_state and time.time() - self.ext_seen < STATE_STALE_S:
            return self.ext_state
        return None

    def _draft_key(self, user) -> str:
        return "fw_draft:%s" % user["id"]

    def draft(self, user) -> dict:
        d = self.store.get_setting(self._draft_key(user), None)
        if not isinstance(d, dict) or time.time() - d.get("t", 0) > DRAFT_TTL_S:
            return {"moves": {}, "t": 0, "undo_of": None}
        return d

    def set_draft(self, user, moves: dict, undo_of=None) -> dict:
        st = self.state()
        if st is None:
            raise model.Refused("fw.err.no_state")
        _, nets, devs = model.index(st)
        clean = {}
        for dev_id, net_id in (moves or {}).items():
            if isinstance(dev_id, str) and isinstance(net_id, str) and dev_id in devs and net_id in nets and devs[dev_id]["network"] != net_id:
                clean[dev_id] = net_id
        if len(clean) > model.MAX_OBJECTS:
            raise model.Refused("fw.block.too_many", n=len(clean), max=model.MAX_OBJECTS)
        from ..integrations import capabilities as capmod
        if clean and not capmod.can(self.capabilities(), "move"):
            raise model.Refused("fw.block.capability", kind="move")
        prev = self.draft(user)
        d = {"moves": clean, "t": int(prev.get("t") or time.time()) if clean else 0,
             "undo_of": undo_of if undo_of is not None else (prev.get("undo_of") if clean else None)}
        self.store.set_setting(self._draft_key(user), d)
        return d

    def clear_draft(self, user) -> None:
        self.store.set_setting(self._draft_key(user), {"moves": {}, "t": 0, "undo_of": None})

    def view(self, user, client_ip: str) -> dict:
        st = self.state()
        out = {"mode": self.mode, "state": None, "targets": {}, "draft": self.draft(user), "quiet": self.is_quiet(),
               "quiet_hours": self.quiet_hours, "executor": self.executor_status(), "running": self.running(),
               "history": self.history(), "requester": None, "max_objects": model.MAX_OBJECTS,
               "capabilities": self.public_capabilities()}
        if st is not None:
            out["state"] = st
            out["targets"] = {d["id"]: model.drop_targets(st, d) for d in st.get("devices") or []}
            out["requester"] = next((d["id"] for d in st.get("devices") or [] if d.get("ip") == client_ip), None)
        return out

    def public_capabilities(self) -> dict:
        from ..integrations import capabilities as capmod
        return capmod.public(self.capabilities())

    def is_quiet(self) -> bool:
        return model.in_quiet_hours(self.quiet_hours, time.time())

    # ------------------------------------------------------------------ preview and plan
    def preview(self, user, client_ip: str) -> dict:
        st = self.state()
        if st is None:
            raise model.Refused("fw.err.no_state")
        moves = self.draft(user).get("moves") or {}
        return model.preview(st, moves, requester_ip=client_ip, quiet=self.is_quiet())

    def make_plan(self, user, client_ip: str, confirmed: list, rp_id: str) -> dict:
        pv = self.preview(user, client_ip)
        if pv["blocks"]:
            raise model.Refused(pv["blocks"][0][0], **pv["blocks"][0][1])
        if not pv["steps"]:
            raise model.Refused("fw.block.empty")
        confirmed = [c for c in (confirmed or []) if isinstance(c, str)]
        if set(pv["confirm"]) - set(confirmed):
            raise model.Refused("fw.block.confirm")
        target = "simulation" if self.mode == "simulation" else (self.ext_info.get("target") or "controller")
        plan = model.build_plan(self.state(), pv, target=target, user=user["username"], confirmed=confirmed,
                                undo_of=self.draft(user).get("undo_of"))
        if plan["v"] > self.plan_version_max():
            raise model.Refused("fw.block.executor_old", v=plan["v"], max=self.plan_version_max())
        model.check_capabilities(plan, self.capabilities())
        now = time.time()
        with self._lock:
            for k in [k for k, v in self.pending.items() if now - v["t"] > model.PLAN_TTL_S]:
                self.pending.pop(k, None)
            self.pending[plan["id"]] = {"plan": plan, "user": user["id"], "t": now}
        sign = None
        if self.mode == "external":
            keys = self.user_keys(user, rp_id)
            sign = {"challenge": b64u(digest(plan)), "allow": [k["id"] for k in keys], "rp_id": rp_id}
        return {"plan": plan, "hash": digest(plan).hex(), "sign": sign}

    def approve(self, user, plan_id: str, assertion, rp_id: str, origin: str) -> dict:
        with self._lock:
            entry = self.pending.get(plan_id)
        if not entry or entry["user"] != user["id"] or time.time() > entry["plan"]["expires"]:
            raise model.Refused("fw.block.expired")
        plan = entry["plan"]
        if self.mode == "simulation":
            if self.runner().busy():
                raise model.Refused("fw.block.busy")
            with self._lock:
                self.pending.pop(plan_id, None)
            self.on_progress({"plan": plan_id, "stage": "queued", "status": "running", "t": int(time.time())})
            self._remember_job(plan, user)
            threading.Thread(target=self.runner().run, args=(plan,), name="fw-simulation", daemon=True).start()
        else:
            try:
                verify_assertion(assertion, digest(plan), self.user_keys(user, rp_id), [origin])
            except Invalid as exc:
                raise model.Refused("fw.block.signature", why=str(exc))
            with self._lock:
                self.pending.pop(plan_id, None)
                self.queue = [q for q in self.queue if time.time() <= q["plan"]["expires"]] + [{"plan": plan, "assertion": assertion}]
            self.on_progress({"plan": plan_id, "stage": "queued", "status": "running", "t": int(time.time())})
            self._remember_job(plan, user)
        self.clear_draft(user)
        return {"ok": True, "plan": plan_id}

    def undo(self, user, plan_id: str) -> dict:
        job = next((j for j in self.history() if j["plan"] == plan_id), None)
        if not job or job.get("status") != "done" or not job.get("inverse") or not job.get("undo_until") or time.time() > job["undo_until"]:
            raise model.Refused("fw.err.undo_gone")
        if any(s.get("kind") != "move" for s in job["inverse"]):
            raise model.Refused("fw.err.undo_gone")      # only moves are drafted in the editor so far
        moves = {s["device"]: s["after"]["network"] for s in job["inverse"]}
        return self.set_draft(user, moves, undo_of=plan_id)

    def reset_simulation(self) -> None:
        if self.mode != "simulation" or self.runner().busy():
            raise model.Refused("fw.block.busy")
        self.sim().reset()

    # ------------------------------------------------------------------ jobs and progress
    def _remember_job(self, plan, user) -> None:
        st = self.state() or {}
        _, nets, devs = model.index(st)
        from . import neutral
        summary = []
        for s in plan["steps"]:
            if s["kind"] == "move":
                summary.append({"name": (devs.get(s["device"]) or {}).get("name", "?"), "from": (nets.get(s["before"]["network"]) or {}).get("name", "?"),
                                "to": (nets.get(s["after"]["network"]) or {}).get("name", "?")})
            else:
                obj = neutral.find(st, s) or {}
                summary.append({"name": obj.get("name") or s["after"].get("name") or s["before"].get("name") or "?",
                                "kind": s["kind"], "fields": stepkinds.changed_fields(s)})
        jobs = self.history()
        jobs.insert(0, {"plan": plan["id"], "user": user["username"], "t": int(time.time()), "status": "running", "stage": "queued",
                        "summary": summary, "undo_of": plan.get("undo_of"), "inverse": None, "undo_until": None})
        self.store.set_setting("fw_jobs", jobs[:20])

    def history(self) -> list:
        jobs = self.store.get_setting("fw_jobs", [])
        return jobs if isinstance(jobs, list) else []

    def running(self):
        for job in self.history():
            if job.get("status") == "running":
                return dict(job, progress=self.progress.get(job["plan"]))
        return None

    def on_progress(self, ev: dict) -> None:
        if not isinstance(ev, dict) or not isinstance(ev.get("plan"), str):
            return
        clean = {k: ev.get(k) for k in ("plan", "stage", "status", "t", "reason", "watch_until", "vars") if k in ev}
        self.progress[ev["plan"]] = clean
        if ev.get("status") in ("done", "rolled_back", "failed", "refused"):
            jobs = self.history()
            for job in jobs:
                if job["plan"] == ev["plan"]:
                    job.update(status=ev["status"], stage=ev.get("stage"), reason=ev.get("reason"), finished=int(time.time()))
                    inv = ev.get("inverse")
                    if ev["status"] == "done" and isinstance(inv, list) and inv:
                        job["inverse"] = inv
                        job["undo_until"] = int(time.time()) + UNDO_TTL_S
            self.store.set_setting("fw_jobs", jobs)
        else:
            jobs = self.history()
            for job in jobs:
                if job["plan"] == ev["plan"] and job.get("status") == "running":
                    job["stage"] = ev.get("stage")
            self.store.set_setting("fw_jobs", jobs)

    # ------------------------------------------------------------------ passkeys that may sign plans
    def user_keys(self, user, rp_id: str) -> list:
        out = []
        for row in self.store.query("SELECT credential_id, public_key, name FROM passkeys WHERE user_id=?", (user["id"],)):
            try:
                x, y = cose_to_xy(row["public_key"])
            except Exception:
                continue
            out.append({"id": b64u(row["credential_id"]), "rp_id": rp_id, "x": x, "y": y, "name": row["name"],
                        "fingerprint": key_fingerprint(row["credential_id"])})
        return out

    # ------------------------------------------------------------------ executor side (external mode)
    def executor_status(self) -> dict:
        if self.mode == "simulation":
            return {"kind": "built-in", "connected": True, "target": "simulation", "armed": False}
        age = time.time() - self.ext_seen if self.ext_seen else None
        from .. import __version__
        from ..integrations import INTEGRATION_API
        ver = self.ext_info.get("version")
        return {"kind": "external", "connected": age is not None and age < STATE_STALE_S, "age_s": int(age) if age is not None else None,
                "target": self.ext_info.get("target"), "armed": bool(self.ext_info.get("armed")),
                "adapter": self.ext_info.get("adapter"), "version": ver, "keys": self.ext_info.get("keys"),
                "family": self.ext_info.get("family"), "plan_version_max": self.plan_version_max(),
                "outdated": bool(ver) and _older(str(ver), __version__)
                or (self.ext_info.get("api_level") not in (None, INTEGRATION_API))}

    def token_ok(self, header: str) -> bool:
        import hmac
        if self.mode != "external" or len(self.token) < 24:
            return False
        given = (header or "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(given.encode(), self.token.encode())

    def executor_push_state(self, data: dict) -> None:
        from . import neutral
        from ..integrations import capabilities as capmod
        st = data.get("state")
        try:
            neutral.validate(st)
        except neutral.ModelError:
            raise model.Refused("fw.block.format")
        self.ext_state = st
        info = data.get("executor") if isinstance(data.get("executor"), dict) else {}
        self.ext_info = {k: info.get(k) for k in ("target", "armed", "adapter", "version", "keys", "family",
                                                     "plan_version_max", "api_level")}
        caps = info.get("capabilities")
        self.ext_caps = None
        if isinstance(caps, dict) and caps.get("write") is not None:
            try:      # the executor sends the public form; rebuild a checked list from it (never wider than declared)
                self.ext_caps = capmod.validate({"family": caps.get("family") or "controller",
                                                 "api": caps.get("api") or {"kind": "local", "version": ""},
                                                 "read": caps.get("read") or [], "write": caps.get("write") or {},
                                                 "limits": {"objects_per_plan": caps.get("objects_per_plan") or model.MAX_OBJECTS}})
            except capmod.CapabilityError:
                self.ext_caps = {"family": "controller", "api": {"kind": "local", "version": ""}, "read": [], "write": {},
                                 "rollback": {}, "limits": {"objects_per_plan": model.MAX_OBJECTS}, "lockout": []}
        self.ext_seen = time.time()

    def executor_take_plans(self, target: str) -> list:
        now = time.time()
        with self._lock:
            take = [q for q in self.queue if q["plan"]["target"] == target and now <= q["plan"]["expires"]]
            self.queue = [q for q in self.queue if q not in take and now <= q["plan"]["expires"]]
        return take


