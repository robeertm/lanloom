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

from . import guests, model, netconf, segplan, zonefw
from . import steps as stepkinds
from .adapter import SimulatedController, with_sample_gateway, with_sample_hypervisor
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
        self.live = None           # callable -> fresh configurations read by family adapters (set by the app)
        self.journal = None        # event journal of the nightly report (set by the app)
        self.notify = None         # notification rules (set by the app)
        self.identity = None       # callable -> ({address: reason}, {mac hash: reason}): what the app knows about the
                                   # network (its own address, name servers, gateway, hub/controller); critical guests
        self.address_users = None  # callable -> {address: [{"via", "by"}]}: what uses an address (hub, Lanloom itself)

    # ------------------------------------------------------------------ simulation
    def sim(self):
        if self._sim is None:
            saved = self.store.get_setting("fw_sim_state", None)
            self._sim = SimulatedController(state=with_sample_gateway(with_sample_hypervisor(saved)) if isinstance(saved, dict) and saved.get("devices") else None,
                                            hypervisor=True,
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

    EMPTY = {"moves": {}, "edits": {}, "ops": {}, "t": 0, "undo_of": None}

    def draft(self, user) -> dict:
        d = self.store.get_setting(self._draft_key(user), None)
        if not isinstance(d, dict) or time.time() - d.get("t", 0) > DRAFT_TTL_S:
            return dict(self.EMPTY, moves={}, edits={}, ops={})
        d.setdefault("edits", {})             # drafts of earlier releases carried moves only
        d.setdefault("ops", {})               # ... and no guest changes
        return d

    def set_draft(self, user, moves: dict, undo_of=None, edits: dict = None, ops=None) -> dict:
        """Moves {device: network}, edits {device: {"name"?, "fixed_ip"?}} and guest changes {key: {"kind", "object",
        "after"}} (see ``guests.clean_ops``). ``edits=None`` / ``ops=None`` keep those of the current draft (a client
        that only sends moves, as earlier releases did, does not lose them)."""
        st = self.state()
        if st is None:
            raise model.Refused("fw.err.no_state")
        _, nets, devs = model.index(st)
        clean = {}
        for dev_id, net_id in (moves or {}).items():
            if isinstance(dev_id, str) and isinstance(net_id, str) and dev_id in devs and net_id in nets and devs[dev_id]["network"] != net_id:
                clean[dev_id] = net_id
        prev = self.draft(user)
        ed = model.clean_edits(st, prev.get("edits") if edits is None else edits)
        raw_ops = prev.get("ops") if ops is None else ops
        gops = guests.clean_ops(st, raw_ops)
        gops.update(netconf.clean_ops(st, raw_ops))
        gops.update(zonefw.clean_ops(st, raw_ops))
        n = len(set(clean) | set(ed)) + len(gops)
        if n > model.MAX_OBJECTS:
            raise model.Refused("fw.block.too_many", n=n, max=model.MAX_OBJECTS)
        from ..integrations import capabilities as capmod
        caps = self.capabilities()
        if clean and not capmod.can(caps, "move"):
            raise model.Refused("fw.block.capability", kind="move")
        for dev_id, fields in ed.items():
            for f, v in fields.items():
                probe = {"kind": "client.set", "object": dev_id, "before": {f: model._cur(devs[dev_id], f)}, "after": {f: v}}
                if capmod.stage_for(caps, probe) is None:
                    raise model.Refused("fw.block.capability", kind="client.set." + f)
        for step in gops.values():
            if capmod.stage_for(caps, step) is None:
                raise model.Refused("fw.block.capability", kind=step["kind"])
        busy = bool(clean or ed or gops)
        d = {"moves": clean, "edits": ed, "ops": {k: {"kind": v["kind"], "object": v["object"], "after": v["after"]} for k, v in gops.items()},
             "t": int(prev.get("t") or time.time()) if busy else 0,
             "undo_of": undo_of if undo_of is not None else (prev.get("undo_of") if busy else None)}
        self.store.set_setting(self._draft_key(user), d)
        return d

    def clear_draft(self, user) -> None:
        self.store.set_setting(self._draft_key(user), dict(self.EMPTY, moves={}, edits={}, ops={}))

    def live_sources(self) -> list:
        try:
            return list(self.live() or []) if self.live else []
        except Exception:
            return []

    def view(self, user, client_ip: str, source: str = None) -> dict:
        """The tab's data. ``source``: ``editor`` (simulation or the external executor) or ``int:<id>`` (a
        configuration read by a device-family adapter, shown read-only). Without a choice a live configuration is
        shown when the editor only has the simulation."""
        live = self.live_sources()
        keys = {e["key"] for e in live}
        if source not in keys and source != "editor":
            source = live[0]["key"] if live and self.mode == "simulation" else "editor"
        sources = [{"key": "editor", "kind": self.mode}] + [
            {"key": e["key"], "kind": "live", "name": e["name"], "family": e.get("family"), "age_s": e.get("age_s")} for e in live]
        if source != "editor":
            entry = next(e for e in live if e["key"] == source)
            from ..integrations import capabilities as capmod
            st = entry["state"]
            return {"mode": "live", "source": source, "sources": sources, "state": st, "targets": {},
                    "segplan": segplan.status(st, segplan.load(self.store)), "uses": self.dependencies(st),
                    "draft": dict(self.EMPTY, moves={}, edits={}, ops={}), "quiet": self.is_quiet(), "quiet_hours": self.quiet_hours,
                    "executor": self.executor_status(), "running": None, "history": [], "max_objects": model.MAX_OBJECTS,
                    "requester": next((d["id"] for d in st.get("devices") or [] if d.get("ip") == client_ip), None),
                    "capabilities": capmod.public(dict(entry["caps"], write={})),     # a live source is shown, never written
                    "live": {"name": entry["name"], "family": entry.get("family"), "age_s": entry.get("age_s"),
                             "version": (st.get("controller") or {}).get("version"), "apis": (st.get("controller") or {}).get("apis")}}
        st = self.state()
        out = {"mode": self.mode, "source": "editor", "sources": sources, "state": None, "targets": {}, "draft": self.draft(user),
               "quiet": self.is_quiet(), "quiet_hours": self.quiet_hours, "executor": self.executor_status(),
               "running": self.running(), "history": self.history(), "requester": None, "max_objects": model.MAX_OBJECTS,
               "capabilities": self.public_capabilities()}
        if st is not None:
            out["state"] = st
            out["targets"] = {d["id"]: model.drop_targets(st, d) for d in st.get("devices") or []}
            out["segplan"] = segplan.status(st, segplan.load(self.store))
            try:
                segplan.remember(self.store, out["segplan"])
            except Exception:
                pass
            out["uses"] = self.dependencies(st)
            zf = self.zone_info(st)
            if zf:
                out["zonefw"] = zf
            if any(k.startswith(("network.", "dhcp.", "forward.")) for k in (self.capabilities().get("write") or {})):
                out["netconf"] = {"templates": [netconf.suggest(st, t["id"]) for t in netconf.TEMPLATES],
                                  "default_zone": netconf.default_zone(st),
                                  "refs": {n["id"]: netconf.references(st, n["id"]) for n in st.get("networks") or []}}
            out["requester"] = next((d["id"] for d in st.get("devices") or [] if d.get("ip") == client_ip), None)
            if st.get("guests"):
                crit = self.critical(st)
                out["guests"] = {"critical": crit, "bridges": {g["id"]: sorted(guests.target_bridges(st, g)) for g in st["guests"]
                                                              if not g.get("template")}}
        return out

    def zone_info(self, st: dict):
        """What the zone firewall cards need (only when the adapter can change zones or rules): zones with what refers
        to them, the system zones, the network zones may hold."""
        write = self.capabilities().get("write") or {}
        if not any(k.startswith(("zone.", "policy.")) or k == "network.update.zone" for k in write) or not st.get("zones"):
            return None
        return {"refs": {z["id"]: zonefw.zone_refs(st, z["id"]) for z in st.get("zones") or []},
                "internal": zonefw.system_zone(st, "internal"), "external": zonefw.system_zone(st, "external"),
                "gateway": zonefw.system_zone(st, "gateway"),
                "targets": [z["id"] for z in st.get("zones") or [] if zonefw.zone_target_problem(st, z["id"]) is None],
                "rules": bool(st.get("policies") is not None)}

    def guard(self, st: dict) -> dict:
        """Paths a plan must keep open, from what the app knows: Lanloom's own address to the gateway and to the
        controllers and hubs it reads (the approver's device is added by the preview)."""
        addrs = {}
        if self.identity:
            try:
                addrs, _ = self.identity()
            except Exception:
                addrs = {}
        sources = sorted(ip for ip, why in addrs.items() if why == "hv.why.self")
        targets = [(ip, 443, "zf.path.controller") for ip, why in sorted(addrs.items()) if why == "hv.why.controller"]
        return {"sources": sources, "targets": targets}

    def reach(self, user, src: str, dst: str, proto: str = None, port: int = None, source: str = None) -> dict:
        """"May A reach B?": the answer for the shown configuration and, in the editor, after the current draft."""
        if source and source != "editor":
            entry = next((e for e in self.live_sources() if e["key"] == source), None)
            if entry is None:
                raise model.Refused("fw.err.no_state")
            return {"before": zonefw.ask(entry["state"], src, dst, proto, port), "after": None}
        st = self.state()
        if st is None:
            raise model.Refused("fw.err.no_state")
        out = {"before": zonefw.ask(st, src, dst, proto, port), "after": None}
        d = self.draft(user)
        steps = []
        try:
            pv = model.preview(st, d.get("moves") or {}, edits=model.clean_edits(st, d.get("edits")))
            steps = list(pv["steps"])
        except model.Refused:
            steps = []
        steps += list(netconf.clean_ops(st, d.get("ops")).values()) + list(zonefw.clean_ops(st, d.get("ops")).values())
        if steps:
            out["after"] = zonefw.ask(zonefw.after_state(st, steps), src, dst, proto, port)
        return out

    def set_segplan(self, items: dict) -> dict:
        """Mark target networks (``{device id: {"net": id} | {"label": text} | None}``); Lanloom data, nothing written to
        a controller."""
        return segplan.set_targets(self.store, items)

    def critical(self, st: dict) -> dict:
        """{guest id: reason}: guests that keep the network or Lanloom running (see ``guests.critical_guests``), from
        what the app knows; the simulation adds the sample's own name server and broker."""
        addrs, macs = {}, {}
        if self.identity:
            try:
                addrs, macs = self.identity()
            except Exception:
                addrs, macs = {}, {}
        if self.mode == "simulation":
            addrs = dict((st.get("hints") or {}).get("critical_addresses") or {}, **addrs)
        return guests.critical_guests(st, addrs, macs)

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
        d = self.draft(user)
        pv = model.preview(self._with_deps(st), d.get("moves") or {}, requester_ip=client_ip, quiet=self.is_quiet(),
                           edits=model.clean_edits(st, d.get("edits")))
        gops = guests.clean_ops(st, d.get("ops"))
        nops = netconf.clean_ops(st, d.get("ops"))
        zops = zonefw.clean_ops(st, d.get("ops"))
        parts = []
        if gops:
            parts.append(guests.preview(st, gops, self.critical(st)))
        if nops:
            parts.append(netconf.preview(st, nops, deps=self.dependencies(st), critical=self.forward_critical(st)))
        if zops:
            parts.append(zonefw.preview(st, zops))
        for g in parts:
            pv["steps"] = pv["steps"] + g["steps"]
            pv["changes"] = pv["changes"] + g["changes"]
            pv["warnings"] = pv["warnings"] + g["warnings"]
            pv["blocks"] = pv["blocks"] + g["blocks"]
            pv["confirm"] = pv["confirm"] + g["confirm"]
        if parts:
            pv["costs"] = {"objects": len(pv["steps"])}
            if len(pv["steps"]) > model.MAX_OBJECTS and not any(b[0] == "fw.block.too_many" for b in pv["blocks"]):
                pv["blocks"].append(["fw.block.too_many", {"n": len(pv["steps"]), "max": model.MAX_OBJECTS}])
            if self.is_quiet() and any(g["steps"] for g in parts) and "quiet" not in pv["confirm"]:
                pv["warnings"].append(["fw.warn.quiet", {}])
                pv["confirm"].append("quiet")
        if pv["steps"]:
            # which zone pairs open or close, and the paths that must stay open (approver, Lanloom, controller)
            g = self.guard(st)
            rv = zonefw.review(st, pv["steps"], requester=pv.get("requester"), sources=g["sources"], targets=g["targets"])
            pv["warnings"] = pv["warnings"] + rv["warnings"]
            pv["blocks"] = pv["blocks"] + rv["blocks"]
            if rv["pairs"]:
                pv["reach"] = rv["pairs"]
        return pv

    def _with_deps(self, st: dict) -> dict:
        """The state plus what uses its devices by address (for the preview's warnings; not part of the fingerprint)."""
        extra = []
        for dev_id, items in self.dependencies(st, own=False).items():
            extra.extend(dict(x, device=dev_id) for x in items)
        if not extra:
            return st
        return dict(st, dependencies=list(st.get("dependencies") or []) + extra)

    def dependencies(self, st: dict, own: bool = True) -> dict:
        """{device id: [{"via", "by"}]} - what uses each device of the state by its address: the hub (integrations,
        presence trackers, automations) and Lanloom's own sources. ``own``: include the state's own list."""
        out = {}
        if own:
            for dep in st.get("dependencies") or []:
                if isinstance(dep, dict) and dep.get("device"):
                    out.setdefault(dep["device"], []).append({k: dep[k] for k in ("by", "via") if k in dep})
        users = {}
        if self.address_users:
            try:
                users = self.address_users() or {}
            except Exception:
                users = {}
        if users:
            for d in st.get("devices") or []:
                seen = set()
                for ip in {d.get("ip"), d.get("fixed_ip")} - {None, ""}:
                    for u in users.get(ip, []):
                        key = (u.get("via"), u.get("by"))
                        if key not in seen:
                            seen.add(key)
                            out.setdefault(d["id"], []).append({"via": u.get("via"), "by": u.get("by")})
        return out

    def forward_critical(self, st: dict) -> list:
        """Addresses a port forward must never reach: the gateway, the controller and hubs Lanloom reads."""
        addrs = {}
        if self.identity:
            try:
                addrs, _ = self.identity()
            except Exception:
                addrs = {}
        return sorted(ip for ip, why in addrs.items() if why in ("hv.why.gateway", "hv.why.controller"))

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
        if any(s.get("kind") not in ("move", "client.set") + guests.GUEST_KINDS + netconf.NET_KINDS + zonefw.ZONE_KINDS for s in job["inverse"]):
            raise model.Refused("fw.err.undo_gone")      # only kinds this editor drafts
        moves, edits, ops = {}, {}, {}
        for s in job["inverse"]:
            if s["kind"] in guests.GUEST_KINDS or s["kind"] in netconf.NET_KINDS or s["kind"] in zonefw.ZONE_KINDS:
                ops[guests.op_key(s["kind"], s["object"])] = {"kind": s["kind"], "object": s["object"], "after": s["after"]}
                continue
            if s["kind"] == "move":
                moves[s["device"]] = s["after"]["network"]
                continue
            after = s["after"]
            if "network" in after:
                moves[s["object"]] = after["network"]
            fields = {f: after[f] for f in model.EDIT_FIELDS if f in after}
            if fields:
                edits[s["object"]] = fields
        return self.set_draft(user, moves, undo_of=plan_id, edits=edits, ops=ops)

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
            elif s["kind"] in netconf.NET_KINDS:
                item = {"name": netconf.label(st, s), "kind": s["kind"],
                        "fields": [f for f in stepkinds.changed_fields(s) if f not in ("client", "zone", "purpose")]}
                if s["kind"] in ("dhcp.reserve", "dhcp.release"):
                    item["ip"] = (s["after"] or s["before"]).get("ip")
                summary.append(item)
            elif s["kind"] in guests.GUEST_KINDS:
                _, label = guests._label(st, s)
                fields = [f for f in stepkinds.changed_fields(s) if f != "guest"]
                item = {"name": label, "kind": s["kind"], "fields": fields if s["kind"] in ("guest.nic", "guest.fw_rule") else []}
                if s["kind"] == "guest.power":
                    item["status"] = s["after"]["status"]
                elif s["kind"] == "guest.firewall":
                    item["on"] = s["after"]["firewall"]
                summary.append(item)
            else:
                obj = neutral.find(st, s) or {}
                item = {"name": obj.get("name") or s["after"].get("name") or s["before"].get("name") or "?",
                        "kind": s["kind"], "fields": stepkinds.changed_fields(s)}
                if s["kind"] == "client.set":
                    if "network" in s["after"]:
                        item["to"] = (nets.get(s["after"]["network"]) or {}).get("name", "?")
                    if "name" in s["after"]:
                        item["name_new"] = s["after"]["name"]
                    if "fixed_ip" in s["after"]:
                        item["fixed_ip"] = s["after"]["fixed_ip"]
                summary.append(item)
        jobs = self.history()
        jobs.insert(0, {"plan": plan["id"], "user": user["username"], "t": int(time.time()), "status": "running", "stage": "queued",
                        "summary": summary, "undo_of": plan.get("undo_of"), "inverse": None, "undo_until": None,
                        "target": plan.get("target")})
        self.store.set_setting("fw_jobs", jobs[:20])

    def _journal_change(self, job: dict) -> None:
        """A finished change on a real controller goes into the event journal (nightly report section "Changes");
        rehearsals on the sample network do not. No addresses, no secrets: names, counts, the result."""
        if job.get("target") in (None, "simulation"):
            return
        names = [str(x.get("name") or "?")[:40] for x in (job.get("summary") or [])][:10]
        if self.notify is not None and job["status"] in ("rolled_back", "failed"):
            try:
                self.notify.emit("fw_change", "fw_change:%s" % job["plan"], once=True,
                                 severity="critical" if job["status"] == "failed" else "warning",
                                 data={"what": ", ".join(names[:3]) or "?", "who": str(job.get("user") or "?")[:64],
                                       "result_key": job.get("reason") if str(job.get("reason") or "").startswith("fw.res.") else "fw.res.apply_failed"})
            except Exception:
                pass
        if self.journal is None:
            return
        try:
            self.journal.add("fw_change", {"plan": job["plan"], "user": str(job.get("user") or "")[:64], "status": job["status"],
                                           "reason": job.get("reason") or "", "n": len(job.get("summary") or []),
                                           "names": [str(x.get("name") or "?")[:40] for x in (job.get("summary") or [])][:10],
                                           "undo_of": job.get("undo_of")})
        except Exception:
            pass

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
                    first = job.get("status") == "running"
                    job.update(status=ev["status"], stage=ev.get("stage"), reason=ev.get("reason"), finished=int(time.time()))
                    inv = ev.get("inverse")
                    if ev["status"] == "done" and isinstance(inv, list) and inv:
                        job["inverse"] = inv
                        job["undo_until"] = int(time.time()) + UNDO_TTL_S
                    if first:
                        self._journal_change(job)
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


