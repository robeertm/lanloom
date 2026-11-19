"""HTTP routes of the resilience view (lanloom/resilience.py): the score, the weak spots, the emergency scenarios, the layers, the
risk lens on the map and the own fields per component. Reading is for everybody who sees the map; the own fields (power source,
spare) and the battery figures of the scenarios are an operator's change and land in the audit log. Nothing is switched.

* ``GET /api/resilience``                     score and grade with 90 days of history, top 10 weak spots, scenarios, layers
* ``GET /api/resilience/lens?view=``          risk per tile of one board (overview or network) for the lens
* ``GET /api/resilience/scenario/{id}``       one scenario in the format of the failure chain (``?minute=`` for the power one)
* ``GET /api/resilience/part/{node}``         the card: risk, the seven axes with reasons, recommendation, own fields
* ``PUT /api/resilience/part/{node}``         own fields ``{power, spare, note}`` (empty values remove them)
* ``PUT /api/resilience/settings``            ``{runtime_min, reserve_min}``: runtime when the UPS reports none, reserve for a clean shutdown
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request

from . import mapmodel


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    def rs():
        return c.state.core.resilience

    @app.get("/api/resilience")
    async def resilience():
        return await asyncio.to_thread(rs().public)

    @app.get("/api/resilience/lens")
    async def resilience_lens(view: str = "overview"):
        if view not in ("overview", "network"):
            return c.err("rs.err.view", 400)
        core = c.state.core

        def run():
            tiles = {comp["id"] for comp in mapmodel.cached_build(core, view)["components"]}
            return rs().lens(tiles)
        return await asyncio.to_thread(run)

    @app.get("/api/resilience/scenario/{sid:path}")
    async def resilience_scenario(sid: str, minute: Optional[float] = None):
        if minute is not None and not 0 <= minute <= 100000:
            return c.err("rs.err.invalid", 400)
        d = await asyncio.to_thread(rs().scenario, sid, minute)
        return d if d is not None else c.err("rs.err.unknown", 404)

    @app.get("/api/resilience/part/{node:path}")
    async def resilience_part(node: str):
        d = await asyncio.to_thread(rs().part, node)
        return d if d is not None else c.err("rs.err.unknown", 404)

    @app.put("/api/resilience/part/{node:path}")
    async def resilience_set_part(node: str, request: Request):
        data = await c.body(request)
        if not isinstance(data, dict):
            return c.err("rs.err.invalid", 400)
        try:
            res = await asyncio.to_thread(rs().set_part, node, data)
        except ValueError as e:
            return c.err(str(e), 400)
        f = res["fields"]
        c.note(request, node=res["node"], power=f.get("power") or "", spare=f.get("spare") or "", note=bool(f.get("note")))
        return res

    @app.put("/api/resilience/settings")
    async def resilience_settings(request: Request):
        data = await c.body(request)
        if not isinstance(data, dict):
            return c.err("rs.err.invalid", 400)
        try:
            cfg = await asyncio.to_thread(rs().set_settings, data)
        except ValueError as e:
            return c.err(str(e), 400)
        c.note(request, target="resilience.settings", runtime_min=cfg["runtime_min"], reserve_min=cfg["reserve_min"])
        return cfg
