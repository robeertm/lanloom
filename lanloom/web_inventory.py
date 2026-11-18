"""HTTP routes of the inventory and life cycle (lanloom/inventory.py).

Everybody who sees the device list reads the inventory; operators change the own fields of a device, retire and
reactivate devices and edit several at once; administrators change the thresholds. Every change is written to the audit
log with the names of the fields (never their values: notes and serial numbers stay out of the log).
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from .inventory import OWN_FIELDS


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state

    @app.get("/api/inventory")
    async def inventory_list(request: Request):
        q = {k: v for k, v in request.query_params.items() if k in ("state", "q", "room", "owner", "kind", "warranty", "eol", "provisional", "sort", "dir")}
        return await asyncio.to_thread(st.core.inventory.list, q)

    @app.get("/api/devices/{device_id}/inventory")
    async def inventory_one(device_id: str):
        item = await asyncio.to_thread(st.core.inventory.get, device_id)
        return item if item else c.err("device.err.missing", 404)

    @app.put("/api/devices/{device_id}/inventory")
    async def inventory_change(device_id: str, request: Request):
        data = await c.body(request)
        c.note(request, target=device_id, detail={"fields": sorted(str(k) for k in data)[:12]})
        try:
            item = st.core.inventory.set_fields(device_id, {k: v for k, v in data.items() if k in OWN_FIELDS})
        except ValueError as exc:
            return c.err(str(exc))
        if item and "firmware" in data:
            st.core.vulns.invalidate()            # the own firmware decides vulnerability hints
        return item if item else c.err("device.err.missing", 404)

    @app.post("/api/devices/{device_id}/lifecycle")
    async def inventory_lifecycle(device_id: str, request: Request):
        data = await c.body(request)
        action = str(data.get("action") or "")[:16]
        c.note(request, target=device_id, detail={"action": action})
        try:
            item = st.core.inventory.lifecycle(device_id, action, at=str(data.get("at") or ""), replaced_by=str(data.get("replaced_by") or ""),
                                               note=str(data.get("note") or ""))
        except ValueError as exc:
            return c.err(str(exc))
        return item if item else c.err("device.err.missing", 404)

    @app.post("/api/inventory/bulk")
    async def inventory_bulk(request: Request):
        data = await c.body(request)
        fields = data.get("set") if isinstance(data.get("set"), dict) else {}
        c.note(request, detail={"n": len(data.get("ids") or []) if isinstance(data.get("ids"), list) else 0,
                                "fields": sorted(str(k) for k in fields)[:12], "confirm": bool(data.get("confirm"))})
        try:
            return st.core.inventory.bulk(data.get("ids"), fields, confirm=data.get("confirm") is True)
        except ValueError as exc:
            return c.err(str(exc))

    @app.get("/api/inventory/settings")
    async def inventory_settings():
        return st.core.inventory.settings()

    @app.put("/api/inventory/settings")
    async def inventory_settings_change(request: Request):
        data = await c.body(request)
        c.note(request, detail={"fields": sorted(str(k) for k in data)[:8]})
        try:
            return st.core.inventory.update_settings(data)
        except ValueError as exc:
            return c.err(str(exc))
