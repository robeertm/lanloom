"""HTTP routes for availability over time and traffic plots (lanloom/availability.py, lanloom/trafficlog.py).

Read-only, for everybody who sees the map:

* ``GET /api/stability``            the "Stability" list (35 daily bars, shares, incidents, repair time, wobbly)
* ``GET /api/availability/{id}``    one subject: ``dev:<device id>`` or ``src:<connection key>`` - 24 h in 5-minute slots, 35 days
* ``GET /api/traffic/{comp_id}``    bytes per second of one board node over ``?range=1h|6h|24h|7d``
* ``GET /api/dns``                  every DNS filter in short (totals of 24 hours, health, findings, hours); data of the leitstand card
* ``GET /api/dns/{integration}``    one filter: totals, hours and days, top blocked names, top clients (matched to devices), upstreams,
                                    block lists, findings. Totals only: single queries are never read or stored.
* ``GET /api/maintenance/{comp_id}`` "Simulate maintenance": what would lose its way if this device were switched off (reads the
                                    resolved topology; nothing is switched, nothing is sent)
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI

from . import dnsfilter, mapmodel, trafficlog


@dataclass
class Ctx:
    state: Any
    err: Callable


def subject_names(core) -> dict:
    """{subject: {"title", "kind"}} of everything the Stability list may show."""
    out = {}
    for dev in core.store.devices(include_hidden=False):
        if dev["data"].get("is_self"):
            continue
        out["dev:" + dev["id"]] = {"title": dev.get("user_title") or dev["title"] or dev["ip"] or dev.get("mac") or dev["id"],
                                   "kind": dev.get("user_kind") or dev["kind"] or ""}
    for integ in core.integrations():
        out["src:int:%d" % integ["id"]] = {"title": integ["name"], "kind": "source"}
    services = getattr(core, "services", None)
    if services is not None:                 # services that were looked at (E5): the Stability list shows those with a history
        for r in services.listing():
            if r["state"] not in ("unprobed", "unknown"):
                out["svc:" + r["key"]] = {"title": "%s · %s :%d" % (r["title"], r["name"], r["port"]), "kind": "service"}
    return out


def register(app: FastAPI, c: Ctx) -> None:
    @app.get("/api/stability")
    async def stability():
        core = c.state.core                  # per request: the sample network of a demo session has its own core
        return await asyncio.to_thread(lambda: core.availability.stability(subject_names(core)))

    @app.get("/api/availability/{subject:path}")
    async def availability(subject: str):
        core = c.state.core
        names = subject_names(core)
        if subject not in names:
            return {"known": False}          # not an error: a hidden device, a map tile that is no device, a connection that is gone
        out = await asyncio.to_thread(core.availability.detail, subject)
        out["known"] = True
        out["title"], out["kind"] = names[subject]["title"], names[subject]["kind"]
        return out

    @app.get("/api/traffic/{comp_id:path}")
    async def traffic(comp_id: str, range: str = "1h"):
        core = c.state.core
        if range not in trafficlog.RANGES:
            return c.err("traffic.err.range")
        traces = mapmodel.component_traces(core, comp_id)
        if traces is None:
            return c.err("component.err.missing", 404)
        return await asyncio.to_thread(core.traffic_log.series, traces, range)

    @app.get("/api/dns")
    async def dns_overview():
        core = c.state.core
        return {"filters": await asyncio.to_thread(dnsfilter.overview, core)}

    @app.get("/api/dns/{integ_id}")
    async def dns_filter(integ_id: int):
        core = c.state.core
        return await asyncio.to_thread(dnsfilter.filter_view, core, integ_id) or c.err("component.err.missing", 404)

    @app.get("/api/maintenance/{comp_id:path}")
    async def maintenance(comp_id: str):
        core = c.state.core
        return await asyncio.to_thread(simulate, core, comp_id) or c.err("component.err.missing", 404)


MAX_AFFECTED = 80


def simulate(core, comp_id: str):
    """Everything that hangs below the device in the resolved topology (switch ports, access points, their clients), the
    internet when it is the gateway, and the connections Lanloom itself holds to it. None when the device is unknown."""
    from . import topology
    dev = core.store.device(comp_id)
    if dev is None:
        return None
    topo = core.topology()
    below = topology.subtree(topo["children"], comp_id)
    rows = []
    for node in sorted(below):
        d = core.store.device(node)
        n = topo["nodes"].get(node) or {}
        if d is not None and d["hidden"]:
            continue
        rows.append({"id": node if d is not None else "", "title": (d.get("user_title") or d["title"] or d["ip"]) if d is not None else (n.get("title") or node),
                     "kind": (d.get("user_kind") or d["kind"]) if d is not None else (n.get("kind") or "unknown"),
                     "device": d is not None})
    rows.sort(key=lambda r: (r["kind"] not in ("router", "switch", "ap", "nas", "server"), r["title"].lower()))
    holds = []
    for integ in core.integrations():
        host = str((integ.get("config") or {}).get("host") or "")
        if dev["ip"] and host == dev["ip"]:
            holds.append({"id": integ["id"], "name": integ["name"]})
    return {"id": comp_id, "n": len(rows), "affected": rows[:MAX_AFFECTED], "more": max(0, len(rows) - MAX_AFFECTED),
            "gateway": bool(dev["data"].get("is_gateway")), "self": bool(dev["data"].get("is_self")), "connections": holds}
