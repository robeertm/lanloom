"""Nightly report section of the hub bridge (one section per home-automation hub connection).

Shown only when a hub connection exists and its bridge reported something. What it says, from the bridge's own
counters (kept in memory; after a restart it covers the time since the start):

* runaway automations or scripts: ran ``BUSY_PER_HOUR`` times or more within one hour (a point);
* scripts that call each other in a loop (a point: the hub can run into its recursion limit there);
* lookups the bridge pauses because the hub answered them with an error;
* the busiest automations of the night (a note);
* messages the bridge refused to send (should never happen; a point if it does).

People, trackers, zones, cameras, calendars and notifications never appear (they never reach Lanloom). The summary
line has counts only - the AI features use only the summary of this section (``PRIVATE_SECTIONS``).
"""
from __future__ import annotations

from .. import hubbridge
from .contrib import Contribution, Ctx, _short, item, register, section

ID = "hub"
BUSY_NOTES = 3


def _hubs(ctx: Ctx) -> list:
    return [i for i in ctx.core.integrations() if i.get("enabled") and i["kind"] == "hub"]


def _available(ctx: Ctx) -> bool:
    return any((ctx.core.hub.get(i["id"]) or {}).get("snapshot") for i in _hubs(ctx))


def _section(ctx: Ctx, integ: dict) -> dict | None:
    entry = ctx.core.hub.get(integ["id"]) or {}
    snap = entry.get("snapshot")
    if not snap:
        return None
    names = {e["entity_id"]: e.get("name") or e["entity_id"] for e in snap.get("entities") or []}
    name_of = lambda eid: _short(names.get(eid, eid))           # noqa: E731
    runs = hubbridge.runs_between(entry, ctx.since, ctx.until)
    peaks = hubbridge.busiest_hour(entry, ctx.since, ctx.until)
    deps = entry.get("deps") or {}
    bridge = entry.get("bridge") or {}
    items, runaway = [], set()
    for eid, (n, at) in sorted(peaks.items()):
        if n >= hubbridge.BUSY_PER_HOUR:
            runaway.add(eid)
            items.append(item("hub:%d:busy:%s:%d" % (integ["id"], eid, at), "report.hub.runaway", "warn", True,
                              name=name_of(eid), n=n, at_at=at))
    for comp in deps.get("cycles") or []:
        items.append(item("hub:%d:loop:%s" % (integ["id"], ",".join(comp)), "report.hub.loop", "warn", True,
                          names=", ".join(name_of(e) for e in comp)))
    for b in deps.get("blocked") or []:
        eid = b.get("entity_id") or ""
        items.append(item("hub:%d:blocked:%s" % (integ["id"], eid), "report.hub.blocked", "info", False,
                          name=name_of(eid), until_at=b.get("until") or ctx.until))
    for eid, n in sorted(runs.items(), key=lambda x: (-x[1], x[0])):
        if eid in runaway:
            continue
        if len([i for i in items if i["key"] == "report.hub.busy"]) >= BUSY_NOTES:
            break
        items.append(item("hub:%d:top:%s" % (integ["id"], eid), "report.hub.busy", "info", False, name=name_of(eid), n=n))
    if bridge.get("refused"):
        items.append(item("hub:%d:refused" % integ["id"], "report.hub.refused", "danger", True, n=int(bridge["refused"])))
    total = sum(runs.values())
    parts = [{"key": "report.hub.part." + k, "vars": {"n": v}} for k, v in (
        ("runaway", len(runaway)), ("loops", len(deps.get("cycles") or [])), ("blocked", len(deps.get("blocked") or [])),
        ("private", int(bridge.get("private_dropped") or 0))) if v]
    if not total:
        return section(ID, "report.sec.hub", "report.hub.quiet", items, parts=parts, name=integ["name"],
                       n=len(snap.get("automations") or []))
    return section(ID, "report.sec.hub", "report.hub.summary", items, parts=parts, name=integ["name"],
                   n=len(runs), runs=total)


def _build(ctx: Ctx) -> list:
    return [s for s in (_section(ctx, i) for i in _hubs(ctx)) if s]


register(Contribution(ID, 45, "report.sec.hub", _available, _build))
