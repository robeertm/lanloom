"""HTTP routes of Settings > API: API tokens (everybody, for themselves), webhooks (administrators).

These routes belong to the web page: they need a signed-in session and never accept an API token, so a token can never
make another token or change a webhook. Rights come from lanloom/access.py like every route; making a token and adding or
changing a webhook need a fresh second confirmation (step-up). A token and a webhook secret are in the answer exactly
once, right after they were made.
"""
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from . import access, apitokens


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state

    def me(request: Request) -> dict:
        return request.state.user

    # ------------------------------------------------------------------ tokens
    @app.get("/api/tokens")
    async def tokens_list(request: Request):
        user = me(request)
        admin = access.allows(user.get("role"), "admin")
        return {"tokens": st.tokens.list(user["id"]), "others": st.tokens.list_all(except_user=user["id"]) if admin else None,
                "scopes": apitokens.allowed_scopes(user.get("role")), "all_scopes": list(apitokens.SCOPES),
                "expiry_days": list(apitokens.EXPIRY_DAYS), "default_expiry_days": apitokens.DEFAULT_EXPIRY_DAYS,
                "rates": list(apitokens.RATES), "default_rate": apitokens.DEFAULT_RATE, "max": apitokens.MAX_PER_USER,
                "events": st.webhooks.events() if admin else None}

    @app.post("/api/tokens")
    async def tokens_create(request: Request):
        data = await c.body(request)
        user = me(request)
        if user.get("source") == "proxy" and not user.get("id"):
            return c.err("api.err.proxy")
        try:
            out = st.tokens.create(user, data.get("name"), str(data.get("scope") or ""),
                                   data.get("expires_days", apitokens.DEFAULT_EXPIRY_DAYS), data.get("rate", apitokens.DEFAULT_RATE))
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target=out["name"], detail={"scope": out["scope"], "days": int(data.get("expires_days") or apitokens.DEFAULT_EXPIRY_DAYS),
                                                     "prefix": out["prefix"]})
        return out

    @app.delete("/api/tokens/{token_id}")
    async def tokens_revoke(token_id: int, request: Request):
        row = st.tokens.revoke(token_id, me(request))
        if row is None:
            return c.err("api.err.not_found", 404)
        c.note(request, target=row["name"], detail={"prefix": row["prefix"], "owner_id": row["user_id"]})
        return {"ok": True}

    # ------------------------------------------------------------------ webhooks (administrators)
    def hooks_view() -> dict:
        w = st.webhooks
        return {"webhooks": w.list(), "events": w.events(), "deliveries": w.deliveries(limit=30), "max": 20,
                "tolerance_s": 300, "demo": bool(getattr(st.core, "demo", None))}

    @app.get("/api/webhooks")
    async def webhooks_list():
        return hooks_view()

    @app.post("/api/webhooks")
    async def webhooks_add(request: Request):
        data = await c.body(request)
        data.pop("id", None)
        try:
            out = st.webhooks.save(data, by=me(request).get("username") or "")
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target=out["name"], detail={"events": out["events"], "target": out["target"]})
        return out

    @app.put("/api/webhooks/{hook_id}")
    async def webhooks_change(hook_id: str, request: Request):
        data = await c.body(request)
        data["id"] = hook_id[:40]
        try:
            out = st.webhooks.save(data, by=me(request).get("username") or "")
        except LookupError as exc:
            return c.err(str(exc), 404)
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target=out["name"], detail={"events": out["events"], "enabled": out["enabled"], "address_changed": bool(data.get("url"))})
        return out

    @app.delete("/api/webhooks/{hook_id}")
    async def webhooks_delete(hook_id: str, request: Request):
        h = st.webhooks.hook(hook_id[:40])
        if not h or not st.webhooks.delete(h["id"]):
            return c.err("webhooks.err.missing", 404)
        c.note(request, target=h.get("name") or h["id"])
        return {"ok": True}

    @app.post("/api/webhooks/{hook_id}/test")
    async def webhooks_test(hook_id: str, request: Request):
        import asyncio
        try:
            res = await asyncio.to_thread(st.webhooks.test, hook_id[:40], me(request).get("username") or "")
        except LookupError as exc:
            return c.err(str(exc), 404)
        c.note(request, detail={"ok": res["ok"], "code": res["code"]})
        return res

    @app.post("/api/webhooks/{hook_id}/secret")
    async def webhooks_rotate(hook_id: str, request: Request):
        try:
            out = st.webhooks.rotate(hook_id[:40])
        except LookupError as exc:
            return c.err(str(exc), 404)
        c.note(request, target=out["name"])
        return out

    @app.get("/api/webhooks/{hook_id}/deliveries")
    async def webhooks_deliveries(hook_id: str, limit: int = 50):
        if not st.webhooks.hook(hook_id[:40]):
            return c.err("webhooks.err.missing", 404)
        return {"deliveries": st.webhooks.deliveries(hook_id[:40], limit)}

    @app.post("/api/webhooks/deliveries/{delivery_id}/retry")
    async def webhooks_redeliver(delivery_id: int):
        if not st.webhooks.redeliver(delivery_id):
            return c.err("webhooks.err.not_retryable", 404)
        return {"ok": True}
