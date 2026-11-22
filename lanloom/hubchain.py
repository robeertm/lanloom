"""Effect chains and mode conflicts of the Automations tab (core side, read only), built from the run summaries.

The bridge reads the stored runs of all automations and scripts (``trace/list`` per domain, one reply each, admin mode)
and reports, per item, its newest runs: when it started, how it ended, which entity started it and which automation or
script called it (context ids are resolved to ``by`` inside the bridge). ``hubbridge.clean_runs`` keeps them in memory.
Two views come from the same summary - nothing is asked per automation:

* **Effect chain** (``chains``): a state change of a device that an automation or script caused, followed back through
  the runs that led to it: sensor -> automation -> (automation/script ...) -> device. Every run used must have started at
  most ``WINDOW_S`` (one hour) before the change and no later than the run it led to; at most ``MAX_DEPTH`` automations
  or scripts form a chain; a loop (a run whose caller is already in the chain) ends it.
* **Mode conflicts** (``mode_events``): runs the hub did not carry out because of the mode of the automation - ``single``
  dropped a start while a run was still going, ``restart`` stopped a run ``RESTART_N`` times within an hour, ``queued``
  / ``parallel`` hit the maximum number of runs. The hub keeps only a few runs per item (about five), so a count is a
  floor of what happened.

Texts use display names only (entity names of the hub; an unknown entity is written humanised); ids appear only as link
targets. Hub data stays in memory.
"""
from __future__ import annotations

import time

from . import hub_privacy as privacy
from . import hubbridge

WINDOW_S = 3600.0             # a run counts for a chain when it started at most this long before the change
SLACK_S = 2.0                 # a calling run may start up to this much after the run it calls (clock jitter)
MAX_DEPTH = 4                 # automations/scripts in one chain
MAX_CHAINS = 100              # chains of one view
PER_ENTITY = 3                # chains per device (different callers)
TRIES = 60                    # older changes of one cause tried when the newest one has no run behind it
RESTART_N = 3                 # stopped runs within one hour that make a "restart" conflict
KINDS = ("single", "restart", "max_runs")
MODE_OF_KIND = {"single": ("single",), "restart": ("restart",), "max_runs": ("queued", "parallel")}


# ---------------------------------------------------------------------- the summary behind both views
def summary(entry: dict, now: float | None = None) -> dict:
    """What the views know about the run summary: ``state`` is ``ok`` (read, possibly ``stale``), ``waiting`` (admin mode
    but not read yet), ``admin_off`` (no admin mode: nothing is asked), ``no_hub`` or ``error`` (with ``error`` key)."""
    now = time.time() if now is None else now
    snap = entry.get("snapshot") or {}
    if not snap:
        return {"state": "no_hub", "t": None, "age_s": None}
    if not snap.get("admin"):
        return {"state": "admin_off", "t": None, "age_s": None}
    got = entry.get("run_summary")
    if not got:
        st = (entry.get("run_status") or {}).get("key")
        return {"state": "error" if st not in (None, "ok") else "waiting", "t": None, "age_s": None,
                "error": st if st not in (None, "ok") else None}
    age = max(0.0, now - got["t"])
    if got.get("error"):
        return {"state": "error", "t": got["t"], "age_s": round(age, 1), "error": got["error"]}
    return {"state": "ok", "t": got["t"], "age_s": round(age, 1), "stale": age > hubbridge.RUNS_STALE_S,
            "runs": sum(len(v) for v in (got.get("runs") or {}).values())}


def _runs(entry: dict) -> dict:
    return (entry.get("run_summary") or {}).get("runs") or {}


# ---------------------------------------------------------------------- effect chains
def _run_before(runs, upto: float, floor: float):
    """The newest run that started within [floor, upto]."""
    best = None
    for r in runs or ():
        if floor <= r["at"] <= upto and (best is None or r["at"] > best["at"]):
            best = r
    return best


def _node(ctx, eid: str, **extra) -> dict:
    d = privacy.domain_of(eid)
    return dict({"id": eid, "name": ctx.name(eid), "domain": d, "kind": d if d in ("automation", "script") else "entity"}, **extra)


def trace_back(ctx, runs: dict, eid: str, by: str, t: float, state=None) -> dict | None:
    """The chain behind one change of ``eid`` at ``t`` caused by the automation/script ``by``: ``{"links": [...], ...}``
    (origin first, the device last) or None when nothing upstream of the direct cause is known."""
    floor = t - WINDOW_S
    back, seen, cur, upto = [], set(), by, t
    cut = cycle = False
    origin = None
    while cur:
        if cur in seen:
            cycle = True
            break
        if len(back) >= MAX_DEPTH:
            cut = True
            break
        seen.add(cur)
        run = _run_before(runs.get(cur), upto, floor)
        back.append(_node(ctx, cur, at=run["at"] if run else None, result=run["r"] if run else None))
        if not run:
            break
        if run.get("by"):
            cur, upto = run["by"], run["at"] + SLACK_S
            continue
        if run.get("trig"):
            origin = run["trig"]
        break
    if origin is None and len(back) < 2:
        return None
    links = ([_node(ctx, origin, state=ctx.state(origin))] if origin else []) + list(reversed(back))
    links.append(_node(ctx, eid, state=state, at=t))
    return {"entity": eid, "name": ctx.name(eid), "state": state, "t": t, "links": links, "origin": bool(origin),
            "cut": cut, "cycle": cycle, "depth": len(back)}


def chains(ctx, entities=None, item: str | None = None, since: float | None = None) -> list:
    """Effect chains of the last 24 hours (newest first). ``entities`` limits them to those devices, ``item`` to the chains
    that pass through this automation/script. At most ``PER_ENTITY`` per device, ``MAX_CHAINS`` in all."""
    from . import hubauto
    runs = _runs(ctx.entry)
    if not runs:
        return []
    since = ctx.now - hubbridge.CHANGE_KEEP_S if since is None else since
    wanted = set(entities) if entities is not None else None
    done, tried, out = {}, {}, []
    for t, eid, state, by in sorted(hubauto._changes(ctx, since), key=lambda c: -c[0]):
        if hubauto._cause_kind(by) not in ("automation", "script") or (wanted is not None and eid not in wanted):
            continue
        key = (eid, by)
        if key in done or tried.get(key, 0) >= TRIES or done.get(eid, 0) >= PER_ENTITY:
            continue
        got = trace_back(ctx, runs, eid, by, t, state)       # the newest change of this cause that has a chain
        if not got:
            tried[key] = tried.get(key, 0) + 1
            continue
        done[key] = True
        done[eid] = done.get(eid, 0) + 1
        if item is not None and not any(n["id"] == item for n in got["links"]):
            continue
        out.append(got)
        if len(out) >= MAX_CHAINS:
            break
    return out


# ---------------------------------------------------------------------- mode conflicts
def _max_in_hour(times: list) -> int:
    times = sorted(times)
    best, lo = 0, 0
    for hi, t in enumerate(times):
        while t - times[lo] > 3600:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


def mode_events(ctx, since: float, until: float) -> list:
    """Runs the hub dropped, stopped or queued out because of the mode, per automation/script within (since, until]:
    ``[{"entity", "name", "kind", "mode", "n", "last"}]`` most first. ``kind`` is ``single`` (a start dropped while a run
    went on), ``restart`` (``RESTART_N`` or more stops within an hour) or ``max_runs`` (``queued``/``parallel`` hit their
    limit)."""
    out = []
    for eid, runs in _runs(ctx.entry).items():
        mode = (ctx.items.get(eid) or {}).get("mode")
        inside = [r for r in runs if since < (r.get("end") or r["at"]) <= until]
        for kind, word in (("single", "single"), ("restart", "cancelled"), ("max_runs", "max_runs")):
            hit = [r for r in inside if r["r"] == word]
            if not hit or (kind == "restart" and mode != "restart") or (kind == "max_runs" and mode not in MODE_OF_KIND["max_runs"] and mode is not None):
                continue
            n = len(hit)
            if kind == "restart":
                n = _max_in_hour([r.get("end") or r["at"] for r in hit])
                if n < RESTART_N:
                    continue
            out.append({"entity": eid, "name": ctx.name(eid), "kind": kind, "mode": mode, "n": n,
                        "last": max(r.get("end") or r["at"] for r in hit)})
    out.sort(key=lambda e: (-e["n"], -e["last"], e["entity"], e["kind"]))
    return out


def modes_by_item(ctx) -> dict:
    """``{entity: [event]}`` of the last 24 hours (cached on the context)."""
    got = getattr(ctx, "_modes", None)
    if got is None:
        got = {}
        for ev in mode_events(ctx, ctx.now - hubbridge.CHANGE_KEEP_S, ctx.now):
            got.setdefault(ev["entity"], []).append(ev)
        ctx._modes = got
    return got
