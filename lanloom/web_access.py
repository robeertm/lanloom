"""HTTP routes for signing in and access management: first account, sign-in with second factor, passkeys, invitations,
the own account (password, one-time codes, recovery codes, sessions, notices), users, security policy and audit log.

Who may call what is decided by the guard in main.py from access.RULES; the handlers here only check what depends on
the data (last administrator, own sessions, open invitation ...).
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import access, auth, qr, totp
from .audit import RETENTION_CHOICES


@dataclass
class Ctx:
    state: Any
    cfg: Any
    body: Callable
    err: Callable
    note: Callable
    client_of: Callable
    requester_ip: Callable
    secure_cookie: Callable
    rp_of: Callable
    audit_from: Callable
    current_user: Callable
    langs: tuple


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state
    body, err, note = c.body, c.err, c.note

    def agent_of(request: Request) -> str:
        return auth.agent_label(request.headers.get("user-agent", ""))

    def set_session(response: Response, request: Request, user_id: int, method: str = "password", foreign: bool = False,
                    stage: str = "full") -> None:
        token = st.sessions.create(user_id, ip=c.requester_ip(request), agent=agent_of(request), foreign=foreign, stage=stage,
                                   method=method)
        response.set_cookie(auth.SESSION_COOKIE, token, max_age=None if foreign else auth.SESSION_DAYS * 86400, httponly=True,
                            samesite="strict", secure=c.secure_cookie(request), path="/")

    def mfa_state(user: dict) -> dict:
        """{"required", "ok", "due", "overdue"} of the two-factor rule for this user."""
        row = st.accounts.get(user["id"]) or {}
        required = st.policy.required(row.get("role") or user.get("role"))
        has = st.factors.has_any(user["id"])
        due = row.get("mfa_due")
        exempt = row.get("source") == "proxy"
        overdue = required and not has and not exempt and (due is None or due <= time.time())
        return {"required": required and not exempt, "ok": has or not required or exempt, "due": due, "overdue": overdue}

    def rate_blocked(*keys):
        wait = max([st.limiter.wait_seconds(k) for k in keys] + [0])
        return err("auth.err.rate", 429, wait=round(wait) or 1) if wait else None

    def failed_attempt(request: Request, key: str, target_user=None, what: str = "auth.login_failed", detail=None) -> None:
        """Count a failure; when it starts a lock-out, note it for the user concerned and every administrator."""
        before = st.limiter.wait_seconds(key)
        st.limiter.failure(key)
        c.audit_from(request, target_user, what, "failed", target=(target_user or {}).get("username", ""), detail=detail,
                     actor_name=(target_user or {}).get("username", ""))
        if not before and st.limiter.wait_seconds(key):
            data = {"ip": c.requester_ip(request), "agent": agent_of(request), "user": (target_user or {}).get("username", "")}
            c.audit_from(request, target_user, "auth.lockout", "denied", target=data["user"], detail={"user": data["user"]},
                         actor_name=data["user"])
            who = {a["id"] for a in st.accounts.all() if a["role"] == "admin" and not a["disabled"]}
            if target_user:
                who.add(target_user["id"])
            for uid in who:
                st.notices.add(uid, "lockout", data)

    def finish_sign_in(request: Request, user: dict, method: str, foreign: bool) -> JSONResponse:
        """Second factor done (or not needed): full session, or an enrolment session when the rule is overdue."""
        st.accounts.touch_login(user["id"])
        mfa = mfa_state(user)
        if mfa["overdue"]:
            response = JSONResponse({"ok": True, "enroll": True})
            set_session(response, request, user["id"], method, foreign, stage="enroll")
            c.audit_from(request, user, "auth.login", "ok", target=user["username"], detail={"method": method, "enroll": True})
            return response
        response = JSONResponse({"ok": True, "mfa_due": mfa["due"] if mfa["required"] and not mfa["ok"] else None})
        set_session(response, request, user["id"], method, foreign)
        c.audit_from(request, user, "auth.login", "ok", target=user["username"], detail={"method": method, "public_computer": foreign})
        new = st.notices.known(user["id"], auth.limit_key(c.requester_ip(request)), auth.device_key(request.headers.get("user-agent", "")))
        if "addr" in new:        # in a home network a new device comes with a new address; the same address with another browser is no news
            st.notices.add(user["id"], "new_device" if "dev" in new else "new_address",
                           {"ip": c.requester_ip(request), "agent": agent_of(request), "method": method})
        return response

    def public_user(u: dict) -> dict:
        f = st.factors.summary(u["id"])
        mfa = mfa_state(u)
        return {"id": u["id"], "username": u["username"], "role": u["role"], "disabled": bool(u["disabled"]), "source": u["source"],
                "created": u["created"], "last_login": u["last_login"], "totp": f["totp"], "passkeys": f["passkeys"],
                "recovery_left": f["recovery_left"], "mfa": mfa,
                "sessions": len(st.sessions.list(u["id"]))}

    # ------------------------------------------------------------------ first account
    @app.post("/api/setup/account")
    async def setup_account(request: Request):
        data = await body(request)
        client = c.client_of(request)
        blocked = rate_blocked(client)
        if blocked:
            return blocked
        if st.store.user_count() > 0:
            return err("setup.err.done", 409)
        if not st.setup_code.check(str(data.get("code") or "")):
            failed_attempt(request, client, what="auth.setup_failed")
            return err("setup.err.code", 403)
        problem = auth.Accounts.validate(str(data.get("username") or ""), str(data.get("password") or ""))
        if problem:
            return err(problem)
        if data.get("language") in c.langs:
            st.store.set_setting("language", data["language"])
        user_id = st.accounts.create(str(data["username"]), str(data["password"]), role="admin", mfa_due=st.policy.due_from_now())
        st.setup_code.clear()
        st.limiter.success(client)
        user = st.accounts.get(user_id)
        c.audit_from(request, user, "users.joined", "ok", target=user["username"], detail={"role": "admin", "via": "setup code"})
        st.notices.known(user_id, auth.limit_key(c.requester_ip(request)), auth.device_key(request.headers.get("user-agent", "")))
        response = JSONResponse({"ok": True})
        set_session(response, request, user_id)
        return response

    # ------------------------------------------------------------------ sign-in
    @app.post("/api/login")
    async def login(request: Request):
        data = await body(request)
        client = c.client_of(request)
        blocked = rate_blocked(client)
        if blocked:
            return blocked
        name = str(data.get("username") or "")
        foreign = data.get("public_computer") is True
        user = await asyncio.to_thread(st.accounts.verify, name, str(data.get("password") or ""))
        if not user or user["disabled"]:
            known = st.accounts.by_name(name)          # only an existing name is logged, never what was typed otherwise
            failed_attempt(request, client, known, detail={"reason": "disabled" if user else "password"})
            return err("auth.err.invalid", 401)
        st.limiter.success(client)
        f = st.factors.summary(user["id"])
        if f["any"]:
            ticket = st.tickets.create(user["id"], client, foreign)
            methods = (["totp"] if f["totp"] else []) + (["passkey"] if f["passkeys"] else []) + (["recovery"] if f["recovery_left"] else [])
            return {"ok": False, "second": {"ticket": ticket, "methods": methods}}
        return finish_sign_in(request, user, "password", foreign)

    @app.post("/api/login/second")
    async def login_second(request: Request):
        data = await body(request)
        client = c.client_of(request)
        ticket = str(data.get("ticket") or "")
        entry = st.tickets.get(ticket, client)
        if not entry:
            return err("auth.err.ticket", 401)
        ukey = "2fa:%d" % entry["user"]
        blocked = rate_blocked(client, ukey)
        if blocked:
            return blocked
        kind = data.get("kind") if data.get("kind") in ("totp", "recovery") else "totp"
        user = st.accounts.get(entry["user"])
        if not user or user["disabled"]:
            st.tickets.drop(ticket)
            return err("auth.err.ticket", 401)
        if not await asyncio.to_thread(st.factors.check, user["id"], kind, data.get("code")):
            st.tickets.failed(ticket)
            st.limiter.failure(client)
            failed_attempt(request, ukey, user, detail={"reason": kind})
            return err("auth.err.code", 401)
        st.tickets.drop(ticket)
        st.limiter.success(client)
        st.limiter.success(ukey)
        if kind == "recovery":
            left = st.factors.recovery_left(user["id"])
            st.notices.add(user["id"], "recovery_used", {"left": left, "ip": c.requester_ip(request), "agent": agent_of(request)})
        return finish_sign_in(request, user, "password+" + kind, entry["foreign"])

    @app.post("/api/logout")
    async def logout(request: Request):
        st.sessions.destroy(request.cookies.get(auth.SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    @app.post("/api/passkey/login/options")
    async def pk_login_options(request: Request):
        rp_id, origin = c.rp_of(request)
        return st.passkeys.login_options(rp_id, origin)

    @app.post("/api/passkey/login/verify")
    async def pk_login_verify(request: Request):
        data = await body(request)
        client = c.client_of(request)
        blocked = rate_blocked(client)
        if blocked:
            return blocked
        user_id = await asyncio.to_thread(st.passkeys.login_verify, str(data.get("ceremony") or ""), data.get("credential") or {})
        user = st.accounts.get(user_id) if user_id else None
        if not user or user["disabled"]:
            failed_attempt(request, client, user, detail={"reason": "passkey"})
            return err("auth.err.passkey", 401)
        st.limiter.success(client)
        if data.get("ticket"):
            st.tickets.drop(str(data["ticket"]))
        return finish_sign_in(request, user, "passkey", data.get("public_computer") is True)

    # ------------------------------------------------------------------ invitations and reset links (public part)
    @app.post("/api/invite/check")
    async def invite_check(request: Request):
        data = await body(request)
        client = c.client_of(request)
        blocked = rate_blocked(client)
        if blocked:
            return blocked
        inv = st.invites.check(str(data.get("token") or ""))
        if not inv:
            st.limiter.failure(client)
            return err("invite.err.invalid", 404)
        user = st.accounts.get(inv["user_id"]) if inv["user_id"] else None
        return {"kind": inv["kind"], "role": inv["role"], "expires": inv["expires"], "username": user["username"] if user else None,
                "clear_2fa": bool(inv["clear_2fa"])}

    @app.post("/api/invite/accept")
    async def invite_accept(request: Request):
        data = await body(request)
        client = c.client_of(request)
        blocked = rate_blocked(client)
        if blocked:
            return blocked
        inv = st.invites.check(str(data.get("token") or ""))
        if not inv:
            failed_attempt(request, client, what="invite.failed")
            return err("invite.err.invalid", 404)
        password = str(data.get("password") or "")
        if inv["kind"] == "reset":
            user = st.accounts.get(inv["user_id"])
            if not user:
                return err("invite.err.invalid", 404)
            problem = auth.Accounts.validate(user["username"], password)
            if problem:
                return err(problem)
            if not st.invites.consume(inv["id"]):
                return err("invite.err.invalid", 404)
            st.accounts.set_password(user["id"], password)       # also ends every session of the user
            if inv["clear_2fa"]:
                st.factors.clear_all(user["id"])
                st.accounts.set_role(user["id"], user["role"], st.policy.due_from_now())
            st.notices.add(user["id"], "password_reset", {"ip": c.requester_ip(request), "agent": agent_of(request),
                                                          "cleared_2fa": bool(inv["clear_2fa"])})
            c.audit_from(request, user, "users.reset_done", "ok", target=user["username"], detail={"two_factor_removed": bool(inv["clear_2fa"])})
            st.limiter.success(client)
            return {"ok": True, "login": True, "username": user["username"]}
        username = str(data.get("username") or "").strip()
        problem = auth.Accounts.validate(username, password)
        if problem:
            return err(problem)
        if st.accounts.exists(username):
            return err("invite.err.taken", 409)
        if not st.invites.consume(inv["id"]):
            return err("invite.err.invalid", 404)
        uid = st.accounts.create(username, password, role=inv["role"], mfa_due=st.policy.due_from_now())
        user = st.accounts.get(uid)
        c.audit_from(request, user, "users.joined", "ok", target=username, detail={"role": inv["role"], "invited_by": inv["created_by"]})
        st.limiter.success(client)
        st.notices.known(uid, auth.limit_key(c.requester_ip(request)), auth.device_key(request.headers.get("user-agent", "")))
        return finish_sign_in(request, user, "password", data.get("public_computer") is True)

    # ------------------------------------------------------------------ own account
    @app.get("/api/account")
    async def account(request: Request):
        u = request.state.user
        row = st.accounts.get(u["id"])
        return {"username": row["username"], "role": row["role"], "source": row["source"], "factors": st.factors.summary(u["id"]),
                "mfa": mfa_state(u), "prefs": st.accounts.prefs(u["id"]), "public_computer": u.get("foreign", False),
                "method": u.get("method"), "stage": u.get("stage"), "fresh_until": u.get("stepup_until") if u.get("source") != "proxy" else None,
                "can": access.capabilities(row["role"])}

    @app.put("/api/account/prefs")
    async def account_prefs(request: Request):
        data = await body(request)
        prefs = st.accounts.set_prefs(request.state.user["id"], data)
        note(request, detail={"fields": sorted(k for k in data if k in prefs)})
        return {"prefs": prefs}

    @app.post("/api/account/password")
    async def change_password(request: Request):
        data = await body(request)
        u = request.state.user
        user = st.accounts.get(u["id"])
        if not await asyncio.to_thread(st.accounts.verify, user["username"], str(data.get("current") or "")):
            return err("auth.err.invalid", 403)
        problem = auth.Accounts.validate(user["username"], str(data.get("password") or ""))
        if problem:
            return err(problem)
        st.accounts.set_password(user["id"], str(data["password"]))
        response = JSONResponse({"ok": True})
        set_session(response, request, user["id"], u.get("method") or "password", bool(u.get("foreign")))
        return response

    @app.post("/api/account/stepup/options")
    async def stepup_options(request: Request):
        rp_id, origin = c.rp_of(request)
        return st.passkeys.login_options(rp_id, origin, user_id=request.state.user["id"])

    @app.post("/api/account/stepup")
    async def stepup(request: Request):
        data = await body(request)
        u = request.state.user
        client = c.client_of(request)
        ukey = "2fa:%d" % u["id"]
        blocked = rate_blocked(client, ukey)
        if blocked:
            return blocked
        f = st.factors.summary(u["id"])
        kind = str(data.get("kind") or "")
        ok = False
        if kind == "passkey" and f["passkeys"]:
            ok = await asyncio.to_thread(st.passkeys.login_verify, str(data.get("ceremony") or ""), data.get("credential") or {}, u["id"]) == u["id"]
        elif kind in ("totp", "recovery"):
            ok = await asyncio.to_thread(st.factors.check, u["id"], kind, data.get("code"))
        elif kind == "password" and not f["totp"] and not f["passkeys"]:
            row = st.accounts.get(u["id"])
            ok = bool(await asyncio.to_thread(st.accounts.verify, row["username"], str(data.get("password") or "")))
        if not ok:
            failed_attempt(request, ukey, st.accounts.get(u["id"]), what="auth.stepup", detail={"method": kind[:12]})
            return err("auth.err.code" if kind != "passkey" else "auth.err.passkey", 401)
        st.limiter.success(ukey)
        until = st.sessions.mark_stepup(u["sid"]) if u.get("sid") else None
        c.audit_from(request, u, "auth.stepup", "ok", target=u["username"], detail={"method": kind})
        return {"ok": True, "until": until}

    @app.post("/api/account/totp/setup")
    async def totp_setup(request: Request):
        u = request.state.user
        secret = st.factors.begin(u["id"])
        uri = totp.otpauth_uri(secret, u["username"])
        return {"secret": secret, "uri": uri, "qr": qr.to_svg(qr.encode(uri, "M"), scale=4, quiet=4, dark="#000", light="#fff")}   # black on white in every theme: scanners need it

    @app.post("/api/account/totp/enable")
    async def totp_enable(request: Request):
        data = await body(request)
        u = request.state.user
        codes = await asyncio.to_thread(st.factors.enable, u["id"], data.get("code"))
        if not codes:
            note(request, detail={"result": "wrong code"})
            return err("auth.err.code", 400)
        if u.get("stage") == "enroll" and u.get("sid"):
            st.sessions.promote(u["sid"], "password+totp")
        return {"ok": True, "recovery_codes": codes}

    @app.delete("/api/account/totp")
    async def totp_disable(request: Request):
        u = request.state.user
        st.factors.disable(u["id"])
        if not st.factors.has_any(u["id"]):
            row = st.accounts.get(u["id"])
            st.accounts.set_role(u["id"], row["role"], time.time())   # no grace: the next sign-in asks for a factor if required
        return {"ok": True}

    @app.post("/api/account/recovery")
    async def recovery_new(request: Request):
        u = request.state.user
        if not st.factors.has_any(u["id"]):
            return err("account.err.no_factor", 409)
        return {"recovery_codes": st.factors.new_recovery(u["id"])}

    @app.get("/api/passkeys")
    async def pk_list(request: Request):
        return {"passkeys": st.passkeys.list(request.state.user["id"])}

    @app.post("/api/passkey/register/options")
    async def pk_reg_options(request: Request):
        rp_id, origin = c.rp_of(request)
        user = st.accounts.get(request.state.user["id"])
        return st.passkeys.registration_options(user, rp_id, origin)

    @app.post("/api/passkey/register/verify")
    async def pk_reg_verify(request: Request):
        data = await body(request)
        u = request.state.user
        ok = await asyncio.to_thread(st.passkeys.registration_verify, u["id"], str(data.get("ceremony") or ""), data.get("credential") or {},
                                     str(data.get("name") or ""))
        if not ok:
            return err("auth.err.passkey")
        note(request, detail={"name": str(data.get("name") or "")[:60]})
        if u.get("stage") == "enroll" and u.get("sid"):
            st.sessions.promote(u["sid"], "passkey")
        return {"ok": True}

    @app.delete("/api/passkeys/{key_id}")
    async def pk_delete(key_id: int, request: Request):
        u = request.state.user
        ok = st.passkeys.delete(u["id"], key_id)
        if ok and not st.factors.has_any(u["id"]):
            row = st.accounts.get(u["id"])
            st.accounts.set_role(u["id"], row["role"], time.time())
            st.store.execute("DELETE FROM recovery_codes WHERE user_id=?", (u["id"],))
        return {"ok": ok}

    def session_view(rows: list, current_sid=None) -> list:
        return [{"id": r["sid"], "created": r["created"], "last_seen": r["last_seen"], "ip": r["ip"], "agent": r["agent"],
                 "public_computer": bool(r["foreign_pc"]), "method": r["method"], "stage": r["stage"], "current": r["sid"] == current_sid}
                for r in rows]

    @app.get("/api/account/sessions")
    async def my_sessions(request: Request):
        u = request.state.user
        return {"sessions": session_view(st.sessions.list(u["id"]), u.get("sid"))}

    @app.delete("/api/account/sessions")
    async def my_sessions_end_all(request: Request):
        n = st.sessions.destroy_all(request.state.user["id"])
        note(request, detail={"sessions": n})
        response = JSONResponse({"ok": True, "ended": n})
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    @app.delete("/api/account/sessions/{sid}")
    async def my_session_end(sid: str, request: Request):
        note(request, target="session")
        return {"ok": st.sessions.destroy_sid(request.state.user["id"], sid)}

    @app.get("/api/account/notices")
    async def notices(request: Request):
        return {"notices": st.notices.unseen(request.state.user["id"])}

    @app.post("/api/account/notices/seen")
    async def notices_seen(request: Request):
        data = await body(request)
        ids = data.get("ids") if isinstance(data.get("ids"), list) else None
        note(request, skip=True)            # reading one's own notes is not a change worth an entry
        return {"ok": True, "seen": st.notices.seen(request.state.user["id"], ids)}

    # ------------------------------------------------------------------ users (admin)
    def target_user(user_id: int):
        row = st.accounts.get(user_id)
        return row

    @app.get("/api/users")
    async def users(request: Request):
        invites = st.invites.open()
        return {"users": [public_user(u) for u in st.accounts.all()], "invites": invites, "roles": list(access.ROLES),
                "me": request.state.user["id"], "policy": st.policy.get()}

    @app.post("/api/users/invite")
    async def user_invite(request: Request):
        data = await body(request)
        role = data.get("role")
        if role not in access.ROLES:
            return err("users.err.role")
        invite_id, token = st.invites.create("invite", role, request.state.user["username"], note=str(data.get("note") or ""))
        note(request, target=role, detail={"role": role, "invite": invite_id, "note": str(data.get("note") or "")[:80]})
        return {"id": invite_id, "token": token, "expires": time.time() + auth.INVITE_HOURS * 3600, "role": role}

    @app.delete("/api/users/invites/{invite_id}")
    async def invite_revoke(invite_id: int, request: Request):
        note(request, target="invite %d" % invite_id)
        return {"ok": st.invites.revoke(invite_id)}

    @app.patch("/api/users/{user_id}")
    async def user_change(user_id: int, request: Request):
        data = await body(request)
        row = target_user(user_id)
        if not row:
            return err("users.err.missing", 404)
        note(request, target=row["username"])
        role = data.get("role", row["role"])
        disabled = data.get("disabled", bool(row["disabled"]))
        if role not in access.ROLES or not isinstance(disabled, bool):
            return err("users.err.role")
        if row["role"] == "admin" and not row["disabled"] and (role != "admin" or disabled) and not st.accounts.enabled_admins(excluding=user_id):
            return err("users.err.last_admin", 409)
        if disabled and user_id == request.state.user["id"]:
            return err("users.err.self", 409)
        changes = {}
        if role != row["role"]:
            due = st.policy.due_from_now() if st.policy.required(role) and not st.factors.has_any(user_id) else row["mfa_due"]
            st.accounts.set_role(user_id, role, due)
            changes["role"] = [row["role"], role]
        if disabled != bool(row["disabled"]):
            st.accounts.set_disabled(user_id, disabled)
            changes["disabled"] = disabled
        note(request, detail=changes)
        return public_user(st.accounts.get(user_id))

    @app.delete("/api/users/{user_id}")
    async def user_delete(user_id: int, request: Request):
        row = target_user(user_id)
        if not row:
            return err("users.err.missing", 404)
        note(request, target=row["username"], detail={"role": row["role"]})
        if user_id == request.state.user["id"]:
            return err("users.err.self", 409)
        if row["role"] == "admin" and not row["disabled"] and not st.accounts.enabled_admins(excluding=user_id):
            return err("users.err.last_admin", 409)
        st.factors.clear_all(user_id)              # removes the encrypted one-time-code secret too
        return {"ok": st.accounts.delete(user_id)}

    @app.post("/api/users/{user_id}/reset")
    async def user_reset(user_id: int, request: Request):
        data = await body(request)
        row = target_user(user_id)
        if not row:
            return err("users.err.missing", 404)
        note(request, target=row["username"], detail={"two_factor_removed": data.get("clear_2fa") is True})
        if row["source"] == "proxy":
            return err("users.err.proxy", 409)
        invite_id, token = st.invites.create("reset", row["role"], request.state.user["username"], user_id=user_id,
                                             clear_2fa=data.get("clear_2fa") is True)
        return {"id": invite_id, "token": token, "expires": time.time() + auth.INVITE_HOURS * 3600}

    @app.get("/api/users/{user_id}/sessions")
    async def user_sessions(user_id: int):
        if not target_user(user_id):
            return err("users.err.missing", 404)
        return {"sessions": session_view(st.sessions.list(user_id))}

    @app.delete("/api/users/{user_id}/sessions")
    async def user_sessions_end(user_id: int, request: Request):
        row = target_user(user_id)
        if not row:
            return err("users.err.missing", 404)
        n = st.sessions.destroy_all(user_id)
        note(request, target=row["username"], detail={"sessions": n})
        return {"ok": True, "ended": n}

    # ------------------------------------------------------------------ security policy (admin)
    def security_view() -> dict:
        proxy = {"enabled": bool(getattr(c.cfg, "auth_header", "") and getattr(c.cfg, "trusted_proxies", ())),
                 "header": getattr(c.cfg, "auth_header", ""), "groups_header": getattr(c.cfg, "auth_groups_header", ""),
                 "group_roles": [list(p) for p in getattr(c.cfg, "auth_group_roles", ())],
                 "default_role": getattr(c.cfg, "auth_default_role", "")}
        return {"policy": st.policy.get(), "grace_choices": list(auth.GRACE_CHOICES), "retention": st.audit.retention_days(),
                "retention_choices": list(RETENTION_CHOICES), "proxy": proxy, "base_path": getattr(c.cfg, "base_path", ""),
                "stepup_minutes": auth.STEPUP_MIN, "invite_hours": auth.INVITE_HOURS}

    @app.get("/api/security")
    async def security_get():
        return security_view()

    @app.put("/api/security")
    async def security_put(request: Request):
        data = await body(request)
        before = st.policy.get()
        try:
            policy = st.policy.set(data)
        except ValueError as exc:
            return err(str(exc))
        if "audit_retention_days" in data:
            if data["audit_retention_days"] not in RETENTION_CHOICES:
                return err("security.err.retention")
            st.store.set_setting("audit_retention_days", data["audit_retention_days"])
        newly = [r for r in access.ROLES if policy["mfa_required"][r] and not before["mfa_required"][r]]
        if newly:                                      # a fresh grace period for everybody the rule now applies to
            due = st.policy.due_from_now()
            for u in st.accounts.all():
                if u["role"] in newly and not st.factors.has_any(u["id"]):
                    st.accounts.set_role(u["id"], u["role"], due)
        note(request, detail={"mfa_required": policy["mfa_required"], "mfa_grace_hours": policy["mfa_grace_hours"],
                              "audit_retention_days": st.audit.retention_days()})
        return security_view()

    # ------------------------------------------------------------------ audit log (admin)
    chain_cache = {"key": None, "result": None}

    def chain_state() -> dict:
        key = (st.audit._meta("audit_head"), st.audit._meta("audit_anchor"))
        if chain_cache["key"] != key:
            chain_cache["result"] = st.audit.verify()
            chain_cache["key"] = key
        return chain_cache["result"]

    def filters_of(request: Request) -> dict:
        q = request.query_params
        return {k: q.get(k) for k in ("actor", "action", "outcome", "since", "until", "q") if q.get(k)}

    @app.get("/api/audit")
    async def audit_list(request: Request):
        q = request.query_params
        try:
            before = int(q.get("before")) if q.get("before") else None
            limit = int(q.get("limit") or 100)
        except ValueError:
            return err("request.err.json")
        entries = await asyncio.to_thread(st.audit.query, filters_of(request), limit, before)
        chain = await asyncio.to_thread(chain_state)
        return {"entries": entries, "chain": chain, "actions": st.audit.actions(),
                "users": [u["username"] for u in st.accounts.all()], "retention": st.audit.retention_days()}

    @app.get("/api/audit/export")
    async def audit_export(request: Request):
        f = filters_of(request)
        text = await asyncio.to_thread(st.audit.export_csv, f)
        c.audit_from(request, request.state.user, "audit.export", "ok", detail={"filters": f})
        name = "lanloom-audit-%s.csv" % time.strftime("%Y%m%d-%H%M", time.gmtime())
        return Response(text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="%s"' % name})
