"""HTTP routes of the control room (lanloom/controlroom): the page, the internet watch, the storage boxes, the UPS and the backup chains.

Everybody who sees the map reads. Admin data of a mobile router (cell id, tracking area, position) is in the answer for administrators
only. An operator keeps the list of backup chains (declare, delete, restore check, report a run); an administrator picks the internet
probe targets (the only traffic the control room makes by itself).

* ``GET /api/controlroom``                       the blocks of the page, what needs a look, freshness of the sources
* ``GET /api/wan``                               internet state with both axes, lines, failover, history, mobile-router cards
* ``PUT /api/wan/probe {enabled, dns, tcp[]}``   targets of the internet probe (admin)
* ``GET /api/nas``                               storage-box cards
* ``GET /api/ups``                               battery backups
* ``GET /api/backups``                           backup chains with status, progress and "restore last checked"
* ``PUT /api/backups/chains/{id}``               declare or change a chain {name, source, target, expect_hours}
* ``DELETE /api/backups/chains/{id}``            forget a declared chain
* ``POST /api/backups/chains/{id}/run``          a script reports a run {ok, size?, duration_s?}
* ``POST /api/backups/chains/{id}/progress``     a script reports progress {pct}
* ``POST /api/backups/restore-check``            {chain, t?}: a person says "restored and checked on ..." (default now)
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from . import access
from .controlroom import cellular


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    def is_admin(request: Request) -> bool:
        return access.allows((getattr(request.state, "user", None) or {}).get("role"), "admin")

    @app.get("/api/controlroom")
    async def room(request: Request):
        core = c.state.core
        return await asyncio.to_thread(core.controlroom.snapshot, None, is_admin(request))

    @app.get("/api/wan")
    async def wan(request: Request):
        core = c.state.core
        now = core.controlroom.clock()

        def build():
            v = core.controlroom.wan.view(now)
            v["cellular"] = cellular.cards(core, now, is_admin(request), v["lines"])
            return v
        return await asyncio.to_thread(build)

    @app.put("/api/wan/probe")
    async def wan_probe(request: Request):
        data = await c.body(request)
        try:
            cfg = c.state.core.controlroom.wan.set_probe(data)
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target="wan.probe", detail={"enabled": cfg["enabled"], "targets": len(cfg["tcp"]) + (1 if cfg["dns"] else 0)})
        return cfg

    @app.get("/api/nas")
    async def nas(request: Request):
        core = c.state.core
        snap = await asyncio.to_thread(core.controlroom.snapshot, None, False)
        return {"t": snap["t"], "cards": snap["nas"]}

    @app.get("/api/ups")
    async def ups():
        core = c.state.core
        return {"t": core.controlroom.clock(), "units": await asyncio.to_thread(core.controlroom.ups.view)}

    @app.get("/api/backups")
    async def backups():
        core = c.state.core
        return await asyncio.to_thread(core.controlroom.backups.view)

    @app.put("/api/backups/chains/{chain_id}")
    async def chain_save(chain_id: str, request: Request):
        data = await c.body(request)
        c.note(request, target=chain_id[:48])
        try:
            return c.state.core.controlroom.backups.save_chain(chain_id, data)
        except ValueError as exc:
            return c.err(str(exc))

    @app.delete("/api/backups/chains/{chain_id}")
    async def chain_delete(chain_id: str, request: Request):
        c.note(request, target=chain_id[:48])
        if not c.state.core.controlroom.backups.delete_chain(chain_id):
            return c.err("chain.err.missing", 404)
        return {"ok": True}

    @app.post("/api/backups/chains/{chain_id}/run")
    async def chain_run(chain_id: str, request: Request):
        data = await c.body(request)
        c.note(request, target=chain_id[:48], detail={"ok": bool(data.get("ok"))})
        if not c.state.core.controlroom.backups.report_run(chain_id, bool(data.get("ok")), data.get("size"), data.get("duration_s")):
            return c.err("chain.err.missing", 404)
        return {"ok": True}

    @app.post("/api/backups/chains/{chain_id}/progress")
    async def chain_progress(chain_id: str, request: Request):
        data = await c.body(request)
        c.note(request, target=chain_id[:48])
        if not c.state.core.controlroom.backups.report_progress(chain_id, data.get("pct")):
            return c.err("chain.err.missing", 404)
        return {"ok": True}

    @app.post("/api/backups/restore-check")
    async def restore_check(request: Request):
        data = await c.body(request)
        chain = str(data.get("chain") or "")[:120]
        c.note(request, target=chain)
        known = {r["id"] for r in c.state.core.controlroom.backups.chains()}
        if chain not in known:
            return c.err("chain.err.missing", 404)
        try:
            c.state.core.controlroom.backups.set_restore_checked(chain, data.get("t"))
        except (ValueError, TypeError):
            return c.err("chain.err.date")
        return {"ok": True}
