"""HTTP routes of the services, the Links view and the certificate clock (lanloom/services.py).

Everybody who sees the map reads; an operator can tell Lanloom that a service marked "Removed?" is gone for good.

* ``GET /api/services``                       every service of the known devices with its probe state, the web interfaces
                                              (the "Links" view) and the TLS certificates; plus a summary
* ``GET /api/devices/{id}/services``           the services of one device (the section of its card)
* ``POST /api/services/forget {"key"}``        forget a service ("Removed?" confirmed); a newer scan that lists it again brings it back

The probe itself runs in the background every three minutes and only looks at what is already known (see services.py).
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    @app.get("/api/services")
    async def services_all():
        core = c.state.core                   # per request: a demo session has its own core
        rows = await asyncio.to_thread(core.services.listing)
        return {"rows": rows, "summary": core.services.summary(rows), "enabled": bool(core.store.get_setting("service_probe", True)),
                "interval_s": 180, "generated": core.services.clock()}

    @app.get("/api/devices/{device_id}/services")
    async def services_of(device_id: str):
        core = c.state.core
        if core.store.device(device_id) is None:
            return c.err("device.err.missing", 404)
        rows = await asyncio.to_thread(core.services.device_services, device_id)
        return {"rows": rows, "enabled": bool(core.store.get_setting("service_probe", True))}

    @app.post("/api/services/forget")
    async def services_forget(request: Request):
        data = await c.body(request)
        key = str(data.get("key") or "")[:200]
        c.note(request, target=key)
        if "|" not in key or not key.rpartition("|")[2].isdigit():
            return c.err("service.err.key")
        if not c.state.core.services.forget(key):
            return c.err("service.err.missing", 404)
        return {"ok": True}
