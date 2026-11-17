"""HTTP routes for data keeping (lanloom/storage.py) and the counter self-check (lanloom/counters.py).

Administrators see the sizes per area, change retentions and start a clean-up (both written to the audit log);
everybody who sees the map sees the counter findings in the source view.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state

    @app.get("/api/storage")
    async def storage_overview():
        return await asyncio.to_thread(st.core.storage.overview)

    @app.put("/api/storage/retention")
    async def storage_retention(request: Request):
        data = await c.body(request)
        c.note(request, detail={k: v for k, v in data.items() if isinstance(v, int) and not isinstance(v, bool)})
        try:
            return {"retention": st.core.storage.update(data)}
        except ValueError as exc:
            return c.err(str(exc))

    @app.post("/api/storage/cleanup")
    async def storage_cleanup(request: Request):
        try:
            result = await asyncio.to_thread(st.core.storage.run)
        except ValueError as exc:
            return c.err(str(exc), 409)
        c.note(request, detail={"removed": result["removed"], "freed": result["freed"], "vacuum": result["vacuum"]})
        return result

    @app.post("/api/diagnostics")
    async def diagnostics(request: Request):
        """The redacted diagnostic file (lanloom/diagnostics.py); the download is written to the audit log."""
        from . import diagnostics as diag
        doc = await asyncio.to_thread(diag.build, st)
        c.note(request, detail={"replaced": doc["redaction"]["replaced"]})
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})

    @app.get("/api/selfcheck/counters")
    async def counter_check():
        return st.core.counters.view()
