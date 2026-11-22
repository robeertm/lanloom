"""Core side of the hub bridge: what the bridge reported, kept in memory per connection, and the views built from it.

Nothing here talks to the hub. ``book(entry, ev)`` files one event of the hub source into the connection's entry of
``core.hub``; the ``*_view`` functions build what the API returns. Hub data stays in memory (it is never written to
the database, so backups never contain it); every outward path uses ``hub_privacy.outbound`` / ``redact_snapshot``.

Run counts are kept per automation/script in hourly buckets for 26 hours, so the nightly report can say how often
something ran overnight and spot a runaway automation (``BUSY_PER_HOUR`` runs within one hour).
"""
from __future__ import annotations

import time
from collections import deque

from . import hub_privacy as privacy
from . import hubsensors

HOUR = 3600
KEEP_HOURS = 26
BUSY_PER_HOUR = 60            # an automation/script running this often within one hour is a "runaway"
MAX_BY = 20000
MAX_TRACES = 50
EVENT_MINUTES = 10            # state changes per integration, one-minute buckets
MAX_RING_PENDING = 2000
TOP = 10
CHANGE_KEEP_S = 86400         # who switched what: state changes of the last 24 hours (entity, state, cause)
MAX_CHANGES = 20000
MAX_RUN_LOG = 20000           # automation/script runs of the last 24 hours (the timeline)
MAX_RUN_PENDING = 2000
MAX_FAILED_RUNS = 5           # failed runs kept per automation in the nightly summary
MAX_RUN_SUMMARY_ITEMS = 500
RUNS_WAIT_S = 40.0            # the nightly report waits this long for the summary of failed runs
RUNS_REFRESH_S = 300.0        # the effect chains / mode conflicts ask the hub for a fresh summary at most this often
RUNS_STALE_S = 3600.0         # a summary older than this says "may be out of date" in the views
MAX_RUN_KEPT = 10             # runs kept per automation/script from the summary (effect chains, mode conflicts)
RUN_WORDS = frozenset(("ok", "error", "single", "max_runs", "cancelled", "conditions", "aborted", "other"))


def new_entry(live_cadence) -> dict:
    return {"states": {}, "events": live_cadence, "by": {}, "runs": {}, "deps": None, "traces": {}, "bridge": None}


def book(entry: dict, ev: dict, now: float | None = None) -> None:
    now = time.time() if now is None else now
    etype = ev.get("type")
    if etype == "hub_snapshot":
        entry.update({"snapshot": ev, "t": now})
        entry["eidx"] = {e["entity_id"]: (e.get("platform"), e.get("device_id"), e.get("area"))
                         for e in ev.get("entities") or [] if isinstance(e, dict) and e.get("entity_id")}
    elif etype == "hub_state":
        eid = ev.get("entity_id")
        t = ev.get("t") or now
        entry["states"][eid] = (ev.get("state"), t)
        hubsensors.ring_push(entry, eid, ev.get("state"), t)
        plat = ((entry.get("eidx") or {}).get(eid) or (None,))[0] or "-"
        minute = int(now // 60)
        buckets = entry.setdefault("plat", {})
        bucket = buckets.setdefault(minute, {})
        bucket[plat] = bucket.get(plat, 0) + 1
        for m in [m for m in buckets if m <= minute - EVENT_MINUTES]:
            buckets.pop(m, None)
        pend = entry.setdefault("ring_pending", {})
        if eid in pend or len(pend) < MAX_RING_PENDING:
            pend[eid] = pend.get(eid, 0) + 1
        by = ev.get("by")
        if by and (eid in entry["by"] or len(entry["by"]) < MAX_BY):
            entry["by"][eid] = (by, ev.get("t") or now)
        if privacy.valid_entity_id(eid) and not privacy.is_private(eid) and t >= now - CHANGE_KEEP_S:
            ch = entry.get("changes")
            if ch is None:
                ch = entry["changes"] = deque(maxlen=MAX_CHANGES)
            ch.append((t, eid, ev.get("state"), by if isinstance(by, str) and (by == "user" or privacy.valid_entity_id(by)) else None))
            while ch and ch[0][0] < now - CHANGE_KEEP_S:
                ch.popleft()
    elif etype == "hub_triggers":
        for eid, r in (ev.get("runs") or {}).items():
            if not privacy.valid_entity_id(eid) or not isinstance(r, dict):
                continue
            rec = entry["runs"].setdefault(eid, {"hours": {}, "last": None})
            bucket = int((r.get("last") or now) // HOUR)
            rec["hours"][bucket] = rec["hours"].get(bucket, 0) + int(r.get("n") or 0)
            rec["last"] = r.get("last") or now
            n = int(r.get("n") or 0)
            if n > 0:
                log = entry.get("run_log")
                if log is None:
                    log = entry["run_log"] = deque(maxlen=MAX_RUN_LOG)
                log.append((float(r.get("last") or now), eid, n))
                pend = entry.setdefault("run_pending", {})
                if eid in pend or len(pend) < MAX_RUN_PENDING:
                    pend[eid] = pend.get(eid, 0) + n
            oldest = int(now // HOUR) - KEEP_HOURS
            for b in [b for b in rec["hours"] if b < oldest]:
                rec["hours"].pop(b, None)
    elif etype == "hub_deps":
        entry["deps"] = ev
        entry.pop("auto_cache", None)
    elif etype == "hub_traces":
        eid = ev.get("entity_id")
        if privacy.valid_entity_id(eid):
            entry["traces"][eid] = ev
            while len(entry["traces"]) > MAX_TRACES:
                entry["traces"].pop(next(iter(entry["traces"])))
    elif etype == "hub_runs":
        entry["run_summary"] = clean_run_summary(ev, now)
    elif etype == "hub_bridge":
        entry["bridge"] = ev


def clean_run_summary(ev: dict, now: float) -> dict:
    """The failed runs the bridge reported once for the nightly report: per automation/script entity the newest failed
    runs (time, short error text or None). Kept in memory only, like everything of the hub."""
    items = {}
    for eid, runs in (ev.get("items") or {}).items() if isinstance(ev.get("items"), dict) else ():
        if not privacy.valid_entity_id(eid) or privacy.domain_of(eid) not in ("automation", "script") or privacy.is_private(eid):
            continue
        clean = []
        for r in runs if isinstance(runs, list) else []:
            at = r.get("at") if isinstance(r, dict) else None
            if isinstance(at, (int, float)) and not isinstance(at, bool):
                text = r.get("text")
                clean.append({"at": float(at), "text": text[:privacy.ERROR_MAX] if isinstance(text, str) and text else None})
        if clean:
            items[eid] = clean[:MAX_FAILED_RUNS]
        if len(items) >= MAX_RUN_SUMMARY_ITEMS:
            break
    err = ev.get("error")
    return {"t": float(ev.get("t") or now), "items": items, "runs": clean_runs(ev.get("runs")),
            "checked": int(ev.get("checked") or 0), "error": err if isinstance(err, str) and err.startswith("source.hub.") else None}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _item_id(v, kinds=("automation", "script")):
    return v if privacy.valid_entity_id(v) and privacy.domain_of(v) in kinds and not privacy.is_private(v) else None


def clean_runs(raw) -> dict:
    """The newest runs per automation/script as the bridge reported them: ``{entity: [{at, end, r, trig, by}]}`` newest
    first. ``at``/``end`` are Unix seconds, ``r`` a result word, ``trig`` the entity that started the run (a state, not
    private), ``by`` the automation/script whose run called this one. Anything else is dropped; memory only."""
    out = {}
    for eid, runs in raw.items() if isinstance(raw, dict) else ():
        if not _item_id(eid) or not isinstance(runs, list):
            continue
        keep = []
        for r in runs:
            at = _num(r.get("at")) if isinstance(r, dict) else None
            if at is None:
                continue
            word = r.get("r") if r.get("r") in RUN_WORDS else "other"
            trig = r.get("trig") if privacy.valid_entity_id(r.get("trig")) and not privacy.is_private(r.get("trig")) else None
            by = _item_id(r.get("by"))
            keep.append({"at": at, "end": _num(r.get("end")), "r": word, "trig": trig, "by": by if by != eid else None})
        if keep:
            out[eid] = sorted(keep, key=lambda x: -x["at"])[:MAX_RUN_KEPT]
        if len(out) >= MAX_RUN_SUMMARY_ITEMS:
            break
    return out


def failed_runs(entry: dict, since: float, until: float) -> dict:
    """{entity: [failed runs ended within (since, until]], newest first} from the reported summary."""
    out = {}
    for eid, runs in ((entry.get("run_summary") or {}).get("items") or {}).items():
        hit = [r for r in runs if since < r["at"] <= until]
        if hit:
            out[eid] = sorted(hit, key=lambda r: -r["at"])
    return out


def events_per_integration(entry: dict, now: float) -> list:
    """[(integration domain, state changes per minute over the last ``EVENT_MINUTES`` minutes)], busiest first."""
    minute = int(now // 60)
    total = {}
    for m, bucket in (entry.get("plat") or {}).items():
        if m > minute - EVENT_MINUTES:
            for plat, n in bucket.items():
                total[plat] = total.get(plat, 0) + n
    return sorted(((p, round(n / EVENT_MINUTES, 2)) for p, n in total.items()), key=lambda x: (-x[1], x[0]))


def runs_between(entry: dict, since: float, until: float) -> dict:
    """{entity: runs} within (since, until] at hour granularity (a bucket counts when it starts inside the window)."""
    lo, hi = int(since // HOUR), int(until // HOUR)
    out = {}
    for eid, rec in (entry.get("runs") or {}).items():
        n = sum(c for b, c in rec["hours"].items() if lo <= b <= hi)
        if n:
            out[eid] = n
    return out


def runs_last_hour(entry: dict, now: float) -> dict:
    """{entity: runs within the last 60 minutes}, estimated from the hourly buckets: the current hour counts fully, the
    previous hour with the share that still lies inside the window."""
    cur = int(now // HOUR)
    share = 1.0 - (now % HOUR) / HOUR
    out = {}
    for eid, rec in (entry.get("runs") or {}).items():
        n = rec["hours"].get(cur, 0) + rec["hours"].get(cur - 1, 0) * share
        if n >= 0.5:
            out[eid] = int(round(n))
    return out


def busiest_hour(entry: dict, since: float, until: float) -> dict:
    """{entity: (most runs within one hour, hour start)} within the window."""
    lo, hi = int(since // HOUR), int(until // HOUR)
    out = {}
    for eid, rec in (entry.get("runs") or {}).items():
        best = max(((c, b) for b, c in rec["hours"].items() if lo <= b <= hi), default=None)
        if best:
            out[eid] = (best[0], best[1] * HOUR)
    return out


def _names(entry: dict) -> dict:
    snap = entry.get("snapshot") or {}
    return {e["entity_id"]: e.get("name") or e["entity_id"] for e in snap.get("entities") or []}


def automations_view(entry: dict, now: float | None = None) -> list:
    """Automations and scripts with mode, last run, runs in the last hour/day and the number of references."""
    now = time.time() if now is None else now
    snap = entry.get("snapshot") or {}
    names = _names(entry)
    deps = entry.get("deps") or {}
    refs = dict(deps.get("automations") or {}, **(deps.get("scripts") or {}))
    blocked = {b.get("entity_id") for b in deps.get("blocked") or []}
    in_loop = {e for c in deps.get("cycles") or [] for e in c}
    hour = runs_last_hour(entry, now)
    peak = busiest_hour(entry, now - HOUR, now)       # the current or the previous clock hour
    day = runs_between(entry, now - 86400, now)
    out = []
    for item in (snap.get("automations") or []) + (snap.get("scripts") or []):
        eid = item.get("entity_id")
        r = refs.get(eid) or {}
        rec = (entry.get("runs") or {}).get(eid) or {}
        out.append({"entity_id": eid, "name": names.get(eid, eid), "kind": privacy.domain_of(eid),
                    "state": (entry.get("states", {}).get(eid) or (item.get("state"),))[0], "mode": item.get("mode"),
                    "last": max(x for x in (item.get("last"), rec.get("last"), 0) if x is not None) or None,
                    "runs_hour": hour.get(eid, 0), "runs_day": day.get(eid, 0),
                    "runaway": (peak.get(eid) or (0, 0))[0] >= BUSY_PER_HOUR,
                    "refs": {k: len(r.get(k) or ()) for k in ("trigger", "condition", "action", "related")} if r else None,
                    "blocked": eid in blocked, "loop": eid in in_loop, "traceable": bool(item.get("id"))})
    out.sort(key=lambda a: (not a["runaway"], -a["runs_hour"], -a["runs_day"], a["name"].lower()))
    return out


def bridge_view(core, integ_id: int, entry: dict, now: float | None = None) -> dict:
    """The bridge card: what is read, what is filtered, how the hub is spared, and the busiest automations."""
    now = time.time() if now is None else now
    snap = entry.get("snapshot") or {}
    bridge = entry.get("bridge") or {}
    deps = entry.get("deps") or {}
    integ = next((i for i in core.integrations() if i["id"] == integ_id), None) or {}
    cfg = integ.get("config") or {}
    shown = privacy.shown_domains(cfg)
    autos = automations_view(entry, now)
    names = _names(entry)
    from .sources import hub as hub_src
    return {
        "id": integ_id, "name": integ.get("name"), "admin": bool(cfg.get("admin_mode")),
        "snapshot_age_s": round(now - entry["t"], 1) if entry.get("t") else None,
        "limited": bool(snap.get("limited")),
        "counts": {"devices": len(snap.get("devices") or []), "entities": len(snap.get("entities") or []),
                   "areas": len(snap.get("areas") or []), "automations": len(snap.get("automations") or []),
                   "scripts": len(snap.get("scripts") or []), "trackers": len(snap.get("trackers") or []),
                   "with_mac": sum(1 for d in snap.get("devices") or [] if d.get("macs"))},
        "privacy": dict(privacy.summary(shown), dropped=bridge.get("private_dropped", 0)),
        "protection": {"snapshot_min": round(hub_src.SNAPSHOT_INTERVAL_S / 60), "state_rate": hub_src.STATE_RATE,
                       "config_per_min": hub_src.CONFIG_PER_MIN, "related_per_min": hub_src.RELATED_PER_MIN,
                       "trace_per_min": hub_src.TRACE_PER_MIN, "templates": False,
                       "sent": bridge.get("sent") or {}, "refused": bridge.get("refused", 0),
                       "budget_waits": sum((b or {}).get("waits", 0) for b in (bridge.get("budgets") or {}).values()),
                       "unknown_types": bridge.get("unknown_types") or []},
        "busiest": [{"entity_id": a["entity_id"], "name": a["name"], "runs_hour": a["runs_hour"], "runs_day": a["runs_day"]}
                    for a in autos if a["runs_hour"] or a["runs_day"]][:TOP],
        "runaway": [a["entity_id"] for a in autos if a["runaway"]],
        "cycles": [[{"entity_id": e, "name": names.get(e, e)} for e in c] for c in deps.get("cycles") or []],
        "blocked": [{"entity_id": b.get("entity_id"), "name": names.get(b.get("entity_id"), b.get("entity_id")),
                     "kind": b.get("kind"), "until": b.get("until"), "n": b.get("n")} for b in deps.get("blocked") or []][:50],
        "deps_age_s": round(now - deps["t"], 1) if deps.get("t") else None,
        "allowed": sorted(hub_src.ALLOWED_TYPES - {"auth"}), "admin_types": sorted(hub_src.ADMIN_TYPES),
        "events": sorted(hub_src.ALLOWED_EVENTS),
    }


def traces_view(entry: dict, entity_id: str) -> dict | None:
    fr = (entry.get("traces") or {}).get(entity_id)
    if not fr:
        return None
    return {"entity_id": entity_id, "t": fr.get("t"), "runs": fr.get("runs") or [], "steps": fr.get("steps") or [],
            "error": fr.get("error")}
