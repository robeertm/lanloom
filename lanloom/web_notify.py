"""HTTP routes of the notification rules (lanloom/notify).

Everybody sets their own rules, quiet hours and push devices; operators plan maintenance; administrators set
the shared limits, the protected networks and the heartbeat. The rights come from lanloom/access.py like every route.
Secrets (push endpoints, the heartbeat address, the VAPID private key) are never part of an answer.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request

from .notify import CAP_CHOICES, CHANNELS, COOLDOWN_CHOICES, HB_CHOICES, HOLD_CHOICES, MODES, SEVERITIES
from .notify import events as ev_mod


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable
    agent: Callable


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state

    def me(request: Request) -> dict:
        return request.state.user

    def person(request: Request) -> dict:
        user = me(request)
        lang = None
        try:
            lang = st.accounts.prefs(user["id"]).get("language")
        except Exception:
            pass
        return {"id": user["id"], "username": user.get("username") or "", "role": user.get("role"), "lang": lang}

    def view(request: Request) -> dict:
        n = st.core.notify
        user = me(request)
        admin = user.get("role") == "admin"
        subs = n.push_subs(user["id"])
        out = {
            "events": [{"id": e.id, "group": e.group, "severity": e.severity, "hold": e.hold, "recovery": e.recovery,
                        "personal": e.personal} for e in ev_mod.ordered() if _available(e, st.core)],
            "modes": list(MODES), "severities": list(SEVERITIES), "channels": list(CHANNELS),
            "prefs": n.prefs(user["id"]), "channel_state": n.channel_state(user["id"]),
            "push": {"vapid": n.vapid_public(create=True), "devices": [
                {"id": s["id"], "label": s["label"], "created": s["created"], "last_ok": s["last_ok"], "last_err": s["last_err"]}
                for s in subs]},
            "settings": n.settings(), "choices": {"daily_cap": CAP_CHOICES, "hold_rounds": HOLD_CHOICES,
                                                  "cooldown_h": COOLDOWN_CHOICES, "heartbeat": HB_CHOICES},
            "maintenance": [n.window_view(w) for w in n.maint_list()],
            "active_window": (n.maintenance_at() or {}).get("id"),
            "tz": st.core.reports.tz_name(),
        }
        if admin:
            out["heartbeat"] = n.heartbeat()
        return out

    @app.get("/api/notify")
    async def notify_view(request: Request):
        return await asyncio.to_thread(view, request)

    @app.put("/api/notify/prefs")
    async def notify_prefs(request: Request):
        data = await c.body(request)
        try:
            prefs = st.core.notify.update_prefs(me(request)["id"], data)
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target=str(me(request)["id"]), detail={"rules": sorted((data.get("rules") or {}).keys())[:20],
                                                              "quiet": bool(data.get("quiet")), "email": "email" in data})
        return {"prefs": prefs, "channel_state": st.core.notify.channel_state(me(request)["id"])}

    @app.get("/api/notify/history")
    async def notify_history(request: Request, limit: int = 100, before: int = 0):
        return {"items": await asyncio.to_thread(st.core.notify.history, me(request), limit, before or None)}

    @app.post("/api/notify/push")
    async def notify_push_add(request: Request):
        data = await c.body(request)
        label = str(data.get("label") or c.agent(request) or "")[:80]
        try:
            sub = st.core.notify.add_push(me(request)["id"], data.get("subscription") or {}, label)
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target=str(sub["id"]), detail={"label": label})
        return {"device": sub}

    @app.post("/api/notify/push/forget")
    async def notify_push_forget(request: Request):
        """The browser unsubscribed itself: forget its subscription (looked up by endpoint, own ones only)."""
        data = await c.body(request)
        sid = st.core.notify.push_owner_endpoint(me(request)["id"], str(data.get("endpoint") or ""))
        if sid is None:
            c.note(request, skip=True)
            return {"ok": False}
        st.core.notify.remove_push(sid, me(request)["id"])
        c.note(request, target=str(sid))
        return {"ok": True}

    @app.delete("/api/notify/push/{sub_id}")
    async def notify_push_remove(sub_id: int, request: Request):
        if not st.core.notify.remove_push(sub_id, me(request)["id"]):
            return c.err("notify.err.push_unknown", 404)
        return {"ok": True}

    @app.post("/api/notify/test")
    async def notify_test(request: Request):
        data = await c.body(request)
        try:
            res = await asyncio.to_thread(st.core.notify.test, person(request), str(data.get("channel") or ""))
        except ValueError as exc:
            return c.err(str(exc), 429 if str(exc) == "notify.err.test_wait" else 400)
        c.note(request, detail={"channel": data.get("channel"), "ok": res["ok"]})
        return res

    @app.post("/api/notify/maintenance")
    async def notify_window_add(request: Request):
        data = await c.body(request)
        try:
            w = st.core.notify.add_window(data, by=me(request).get("username") or "")
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target=w["id"], detail={k: w.get(k) for k in ("kind", "title", "start", "end", "days", "from", "to")
                                                 if w.get(k) is not None} | {"devices": len(w["devices"])})
        return {"window": st.core.notify.window_view(w)}

    @app.delete("/api/notify/maintenance/{window_id}")
    async def notify_window_remove(window_id: str, request: Request):
        if not st.core.notify.remove_window(window_id):
            return c.err("notify.err.window_unknown", 404)
        return {"ok": True}

    @app.put("/api/notify/settings")
    async def notify_settings(request: Request):
        data = await c.body(request)
        try:
            cur = st.core.notify.update_settings(data)
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, detail={k: data[k] for k in ("daily_cap", "hold_rounds", "cooldown_h") if k in data}
               | ({"protected": len(cur["protected_cidrs"])} if "protected_cidrs" in data else {}))
        return {"settings": cur}

    @app.put("/api/notify/heartbeat")
    async def notify_heartbeat(request: Request):
        data = await c.body(request)
        try:
            hb = st.core.notify.update_heartbeat({k: data[k] for k in ("url", "every_min", "enabled") if k in data})
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, detail={"enabled": hb["enabled"], "every_min": hb["every_min"], "url_changed": "url" in data})
        return {"heartbeat": hb}

    @app.post("/api/notify/heartbeat/test")
    async def notify_heartbeat_test(request: Request):
        res = await asyncio.to_thread(st.core.notify.beat)
        c.note(request, detail={"ok": res["ok"]})
        return dict(res, heartbeat=st.core.notify.heartbeat())


def _available(e, core) -> bool:
    try:
        return bool(e.available(core))
    except Exception:
        return False
