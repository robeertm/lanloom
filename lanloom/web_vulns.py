"""HTTP routes of the vulnerability hints (lanloom/vulns.py).

Everybody who sees the device list reads the hints; operators set a hint aside ("does not apply", with a note) and
bring it back; administrators import a newer feed file or go back to the bundled one. Every change is written to the
audit log (the note's length, never its text).
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from .vulns import MAX_IMPORT_BYTES


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state

    @app.get("/api/vulns")
    async def vulns_list(request: Request):
        q = {k: v for k, v in request.query_params.items() if k in ("show", "severity", "q")}
        return await asyncio.to_thread(st.core.vulns.list, q)

    @app.get("/api/vulns/feed")
    async def vulns_feed():
        return st.core.vulns.feed_info()

    @app.get("/api/devices/{device_id}/vulns")
    async def vulns_device(device_id: str):
        res = await asyncio.to_thread(st.core.vulns.device, device_id)
        return res if res else c.err("device.err.missing", 404)

    @app.post("/api/vulns/dismiss")
    async def vulns_dismiss(request: Request):
        data = await c.body(request)
        did, vid, text = str(data.get("device") or "")[:80], str(data.get("id") or "")[:40], str(data.get("note") or "")
        c.note(request, target="%s|%s" % (did, vid), detail={"note_chars": len(text.strip())})
        user = getattr(request.state, "user", None) or {}
        try:
            return await asyncio.to_thread(st.core.vulns.dismiss, did, vid, text, user.get("username") or "")
        except ValueError as exc:
            return c.err(str(exc), 404)

    @app.post("/api/vulns/restore")
    async def vulns_restore(request: Request):
        data = await c.body(request)
        did, vid = str(data.get("device") or "")[:80], str(data.get("id") or "")[:40]
        c.note(request, target="%s|%s" % (did, vid))
        try:
            return await asyncio.to_thread(st.core.vulns.restore, did, vid)
        except ValueError as exc:
            return c.err(str(exc), 404)

    @app.post("/api/vulns/feed")
    async def vulns_import(request: Request):
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_IMPORT_BYTES:
            return c.err("vulns.err.too_large", 413)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_IMPORT_BYTES:
                return c.err("vulns.err.too_large", 413)
            chunks.append(chunk)
        try:
            res = await asyncio.to_thread(st.core.vulns.import_feed, b"".join(chunks))
        except ValueError as exc:
            c.note(request, detail={"bytes": size, "ok": False})
            return c.err(str(exc))
        c.note(request, detail={"bytes": size, "entries": res["entries"], "as_of": res["as_of"]})
        return res

    @app.delete("/api/vulns/feed")
    async def vulns_import_remove(request: Request):
        c.note(request, detail={})
        return await asyncio.to_thread(st.core.vulns.remove_import)
