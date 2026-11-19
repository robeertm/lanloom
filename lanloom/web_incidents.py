"""HTTP routes of the rewind (lanloom/rewind.py), the incidents (lanloom/incidents.py) and the chronicle (lanloom/chronicle.py).
Reading is for everybody who sees the map; marking an incident as maintenance (or a note on it) is an operator's change and
lands in the audit log.

* ``GET /api/rewind?range=24h|7d&view=overview|network``   everything to replay a range on a board
* ``GET /api/incidents?days=&kind=``                        the incident list (newest first) with counts per kind
* ``GET /api/incidents/calendar``                           incidents per day, the last 90 days
* ``GET /api/incidents/heatmap?days=``                      parts that went down per hour of day, and journal events
* ``GET /api/incidents/events?hours=``                      the event chronicle (journal rows, with their incident)
* ``GET /api/incidents/night``                              the incidents since the latest nightly report (phone card)
* ``GET /api/incidents/chronicle?days=&kind=``              timeline & data state (firmware jumps, updates, new devices, long outages)
* ``GET /api/incidents/what/{node}``                        "What happened?" for one part
* ``GET /api/incidents/{id}``                               one incident file
* ``PUT /api/incidents/{id}``                               ``{kind: "maintenance"|null, note}`` - the user's mark
Nothing is switched and nothing is sent anywhere.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from . import rewind


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    def inc():
        return c.state.core.incidents

    def tzf():
        rep = getattr(c.state.core, "reports", None)
        tz = getattr(rep, "tz", None) if rep is not None else None
        if callable(tz):
            try:
                zone = tz()
            except Exception:
                zone = None
            if zone is not None:
                from datetime import datetime
                return lambda ts: datetime.fromtimestamp(ts, zone).timetuple()
        return None

    @app.get("/api/rewind")
    async def rewind_data(range: str = "24h", view: str = "overview"):
        if range not in rewind.RANGES or view not in ("overview", "network"):
            return c.err("rw.err.invalid", 400)
        return await asyncio.to_thread(rewind.build, c.state.core, range, view)

    @app.get("/api/incidents")
    async def incidents_list(days: float = 7, kind: str = ""):
        return await asyncio.to_thread(inc().list, days, kind or None)

    @app.get("/api/incidents/calendar")
    async def incidents_calendar():
        return await asyncio.to_thread(lambda: inc().calendar(tz=tzf()))

    @app.get("/api/incidents/heatmap")
    async def incidents_heatmap(days: int = 7):
        days = max(1, min(14, int(days or 7)))
        return await asyncio.to_thread(lambda: inc().heatmap(days=days, tz=tzf()))

    @app.get("/api/incidents/events")
    async def incidents_events(hours: float = 24):
        return await asyncio.to_thread(inc().events, hours)

    @app.get("/api/incidents/night")
    async def incidents_night():
        core = c.state.core

        def run():
            import time
            now = time.time()
            rep = getattr(core, "reports", None)
            latest = rep.latest() if rep is not None and hasattr(rep, "latest") else None
            since = float(latest["since"]) if latest and latest.get("since") else now - 12 * 3600
            until = float(latest["until"]) if latest and latest.get("until") else now
            if now - until > 18 * 3600:                     # no report this morning: the last twelve hours
                since, until = now - 12 * 3600, now
            from .incidents import _short
            items = [_short(i) for i in inc().night(since, until)]
            return {"since": since, "until": until, "items": items,
                    "faults": sum(1 for i in items if i["kind"] != "maintenance")}
        return await asyncio.to_thread(run)

    @app.get("/api/incidents/chronicle")
    async def incidents_chronicle(days: float = 90, kind: str = ""):
        ch = c.state.core.chronicle
        return await asyncio.to_thread(ch.view, days, kind or None)

    @app.get("/api/incidents/what/{node:path}")
    async def incidents_what(node: str):
        d = await asyncio.to_thread(inc().what_happened, node)
        return d if d is not None else c.err("inc.err.unknown", 404)

    @app.get("/api/incidents/{iid}")
    async def incidents_get(iid: str):
        d = await asyncio.to_thread(inc().get, iid)
        return d if d is not None else c.err("inc.err.unknown", 404)

    @app.put("/api/incidents/{iid}")
    async def incidents_mark(iid: str, request: Request):
        data = await c.body(request)
        kind = data.get("kind") or None
        note = data.get("note") or ""
        if kind not in (None, "maintenance") or not isinstance(note, str):
            return c.err("inc.err.invalid", 400)
        try:
            res = await asyncio.to_thread(inc().mark, iid, kind, note)
        except KeyError:
            return c.err("inc.err.unknown", 404)
        except ValueError as e:
            return c.err(str(e), 400)
        c.note(request, detail={"kind": kind or "none", "note": bool(note)})
        return res
