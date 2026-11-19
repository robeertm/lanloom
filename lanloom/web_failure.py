"""HTTP routes of the failure chain (lanloom/failure.py). Reading is for everybody who sees the map; a manual dependency is
an operator's change and lands in the audit log.

* ``GET /api/failure/outages``          the outage automatic: devices that are down now and what probably hangs on them
* ``GET /api/failure/candidates``       what a manual dependency may point at (devices, infrastructure, containers, connections)
* ``GET /api/failure/board?view=``      manual dependencies between the tiles of one board (for the hover chain)
* ``GET /api/failure/deps/{node}``      the card: what this depends on (automatic and manual), manual entries
* ``PUT /api/failure/deps/{node}``      set or remove one manual entry ``{target, kind: hard|soft|none|null, note}``
* ``GET /api/failure/{node}``           the chain: ``?dir=down`` (what fails if it fails) or ``up`` (what it depends on), ``&depth=``
Nothing is switched and nothing is sent anywhere.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from . import failure, mapmodel


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    def fl():
        return c.state.core.failure

    @app.get("/api/failure/outages")
    async def failure_outages():
        return await asyncio.to_thread(fl().outages)

    @app.get("/api/failure/candidates")
    async def failure_candidates():
        return {"items": await asyncio.to_thread(fl().candidates)}

    @app.get("/api/failure/board")
    async def failure_board(view: str = "overview"):
        if view not in ("overview", "network"):
            return c.err("fail.err.view", 400)
        core = c.state.core

        def run():
            tiles = {comp["id"] for comp in mapmodel.cached_build(core, view)["components"]}
            return fl().board(tiles)
        return await asyncio.to_thread(run)

    @app.get("/api/failure/deps/{node:path}")
    async def failure_deps(node: str):
        d = await asyncio.to_thread(fl().deps, node)
        return d if d is not None else c.err("fail.err.unknown", 404)

    @app.put("/api/failure/deps/{node:path}")
    async def failure_set_dep(node: str, request: Request):
        data = await c.body(request)
        target = data.get("target")
        kind = data.get("kind") or None
        note = data.get("note") or ""
        if not isinstance(target, str) or not isinstance(note, str) or (kind is not None and kind not in failure.MANUAL_KINDS):
            return c.err("fail.err.invalid", 400)
        try:
            res = await asyncio.to_thread(fl().set_dep, node, target, kind, note)
        except ValueError as e:
            return c.err(str(e), 400)
        c.note(request, node=res["node"], target=res["target"], kind=res["k"] or "removed")
        return res

    @app.get("/api/failure/{node:path}")
    async def failure_chain(node: str, dir: str = "down", depth: int = failure.DEPTH):
        if dir not in ("down", "up"):
            return c.err("fail.err.invalid", 400)
        d = await asyncio.to_thread(fl().chain, node, dir, depth)
        return d if d is not None else c.err("fail.err.unknown", 404)
