"""HTTP routes for sites (M1, lanloom/sites.py): the list and overview for everybody (only the sites released for
the person), adding, renaming and removing sites, the combined nightly report and copying a controller connection
to a further site for administrators.

Which site a request belongs to is decided by the guard in main.py (cookie ``ll_site`` / header ``X-Lanloom-Site``,
only among the released sites); these routes never name a site the person may not see."""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from . import sites as sites_mod


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state

    def user_of(request: Request):
        return getattr(request.state, "user", None)

    @app.get("/api/sites")
    async def sites_list(request: Request):
        return st.sites.listing(user_of(request), sites_mod.ACTIVE.get())

    @app.get("/api/sites/overview")
    async def sites_overview(request: Request):
        return await asyncio.to_thread(st.sites.overview, user_of(request))

    @app.post("/api/sites")
    async def sites_create(request: Request):
        data = await c.body(request)
        try:
            out = await st.sites.create(data.get("name"), str(data.get("kind") or "remote"))
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target=out["id"])
        return out

    @app.patch("/api/sites/{site_id}")
    async def sites_rename(site_id: str, request: Request):
        data = await c.body(request)
        try:
            return st.sites.rename(site_id, data.get("name"))
        except LookupError as exc:
            return c.err(str(exc), 404)
        except ValueError as exc:
            return c.err(str(exc))

    @app.delete("/api/sites/{site_id}")
    async def sites_delete(site_id: str):
        try:
            ok = await st.sites.delete(site_id)
        except ValueError as exc:
            return c.err(str(exc))
        return {"ok": True} if ok else c.err("site.err.unknown", 404)

    @app.put("/api/sites/report")
    async def sites_report_mode(request: Request):
        data = await c.body(request)
        try:
            mode = st.sites.set_report_mode(data.get("mode"))
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target=mode)
        return {"report_mode": mode}

    @app.get("/api/sites/controller/{integ_id}")
    async def sites_of_controller(integ_id: int):
        """The sites a controller connection of the current site manages (read only)."""
        try:
            return await st.core.controller_sites(integ_id)
        except ValueError as exc:
            return c.err(str(exc), 404 if str(exc) == "integration.err.missing" else 400)

    @app.post("/api/sites/from-controller")
    async def sites_from_controller(request: Request):
        """A further site that reads one site of an existing controller connection with the same account. The
        connection is copied into the new site (its secrets stay encrypted with the instance key); nothing is
        written to the controller."""
        data = await c.body(request)
        try:
            integ_id = int(data.get("integration"))
        except (TypeError, ValueError):
            return c.err("integration.err.missing", 404)
        ref = str(data.get("ref") or "")
        parts = st.core.integration_parts(integ_id)
        if parts is None:
            return c.err("integration.err.missing", 404)
        kind, cfg, sec = parts
        if not _has_site_field(kind):
            return c.err("site.err.no_site_list")
        from .sources.controller import SITE_RE
        if not SITE_RE.fullmatch(ref):
            return c.err("site.err.controller_site")
        if ref == (cfg.get("site") or "default"):
            return c.err("site.err.same_controller_site")
        try:
            site = await st.sites.create(data.get("name") or ref)
        except ValueError as exc:
            return c.err(str(exc))
        core = st.sites.core(site["id"])
        old_name = next((i["name"] for i in st.core.integrations() if i["id"] == integ_id), kind)
        name = sites_mod.clean_name(data.get("connection_name") or "") or ("%s · %s" % (old_name, ref))[:48]
        try:
            core.save_integration(kind, name, dict(cfg, site=ref), sec)
        except ValueError as exc:
            await st.sites.delete(site["id"])
            return c.err(str(exc))
        c.note(request, target=site["id"], detail={"kind": kind})
        return dict(st.sites.public_row(st.sites.get(site["id"])))


def _has_site_field(kind: str) -> bool:
    from .core import integration_fields
    fields = integration_fields(kind)
    return bool(fields and "site" in fields[0])
