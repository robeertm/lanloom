"""HTTP routes for backup and restore (lanloom/backup.py).

Administrators export, schedule, list, download, delete, check and restore (restore and export need a fresh step-up
confirmation, see access.RULES). Before the first account exists, the set-up page can restore a backup instead of
creating an account; those three routes need the one-time set-up code, are rate-limited and close for good once an
account exists. Passphrases are never logged, never part of an audit entry and never in an error message.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import auth
from .backup import UPLOAD_MAX, BackupError

SETUP_SLOT = "setupupload"
SETUP_HEADER = "x-lanloom-setup-code"


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable
    client_of: Callable
    requester_ip: Callable
    audit_from: Callable


def _key_of(response: JSONResponse) -> str:
    import json
    try:
        return str(json.loads(response.body).get("error") or "")[:60]
    except ValueError:
        return ""


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state
    body, err, note = c.body, c.err, c.note

    def refused(exc: BackupError) -> JSONResponse:
        status = {"backup.err.newer": 409, "backup.err.newer_file": 409, "backup.err.busy": 409, "backup.err.missing": 404,
                  "backup.err.upload_gone": 404, "backup.err.too_large": 413}.get(exc.key, 400)
        return err(exc.key, status, vars=exc.vars)

    def agent_of(request: Request) -> str:
        return auth.agent_label(request.headers.get("user-agent", ""))

    def rate_blocked(key: str):
        wait = st.limiter.wait_seconds(key)
        return err("auth.err.rate", 429, wait=round(wait) or 1) if wait else None

    def ref_of(data: dict) -> dict:
        return {"upload": str(data.get("upload") or "")[:40], "file": str(data.get("file") or "")[:140]}

    async def checked(request: Request, data: dict, limit_key: str):
        """(validated content, None) or (None, error response). Wrong passphrases count towards the rate limit."""
        blocked = rate_blocked(limit_key)
        if blocked:
            return None, blocked
        try:
            blob = await asyncio.to_thread(st.backups.blob_of, ref_of(data))
            _, content = await asyncio.to_thread(st.backups.check, blob, data.get("passphrase") if isinstance(data.get("passphrase"), str) else "")
        except BackupError as exc:
            if exc.key == "backup.err.wrong_passphrase":
                st.limiter.failure(limit_key)
            return None, refused(exc)
        st.limiter.success(limit_key)
        return content, None

    # ------------------------------------------------------------------ administrators
    @app.get("/api/backup")
    async def backup_state():
        return await asyncio.to_thread(st.backups.public_state)

    @app.put("/api/backup/settings")
    async def backup_settings(request: Request):
        data = await body(request)
        patch = {k: data[k] for k in ("enabled", "every_hours", "keep", "history") if k in data}
        phrase = data.get("passphrase") if isinstance(data.get("passphrase"), str) and data.get("passphrase") else None
        note(request, detail=dict(patch, new_key=bool(phrase)))
        try:
            if phrase:
                from .backup import check_passphrase
                check_passphrase(phrase)
            return await asyncio.to_thread(st.backups.update_schedule, patch, phrase)
        except BackupError as exc:
            return refused(exc)

    @app.post("/api/backup/export")
    async def backup_export(request: Request):
        data = await body(request)
        history = data.get("history") is True
        note(request, detail={"history": history})
        try:
            name, blob = await asyncio.to_thread(st.backups.export, data.get("passphrase") if isinstance(data.get("passphrase"), str) else "", history)
        except BackupError as exc:
            return refused(exc)
        note(request, target=name, detail={"history": history, "bytes": len(blob)})
        return Response(blob, media_type="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % name, "X-Lanloom-File": name})

    @app.post("/api/backup/run")
    async def backup_run(request: Request):
        try:
            out = await asyncio.to_thread(st.backups.run_now, "manual")
        except BackupError as exc:
            return refused(exc)
        note(request, target=out["name"], detail={"bytes": out["size"], "removed": out["removed"]})
        return dict(out, state=await asyncio.to_thread(st.backups.public_state))

    @app.get("/api/backup/files/{name}")
    async def backup_download(name: str, request: Request):
        try:
            path = st.backups.file_path(name)
        except BackupError as exc:
            return refused(exc)
        c.audit_from(request, getattr(request.state, "user", None), "backup.download", target=name)
        return FileResponse(path, media_type="application/octet-stream", filename=name)

    @app.delete("/api/backup/files/{name}")
    async def backup_delete(name: str, request: Request):
        note(request, target=name[:120])
        try:
            st.backups.delete_file(name)
        except BackupError as exc:
            return refused(exc)
        return await asyncio.to_thread(st.backups.public_state)

    async def receive(request: Request, slot=None):
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > UPLOAD_MAX:
            return refused(BackupError("backup.err.too_large"))
        try:
            return await st.backups.receive(request.stream(), slot=slot)
        except BackupError as exc:
            return refused(exc)

    @app.post("/api/backup/upload")
    async def backup_upload(request: Request):
        out = await receive(request)
        if isinstance(out, dict):
            note(request, detail={"bytes": out["size"], "version": out["meta"].get("app")})
        return out

    @app.post("/api/backup/check")
    async def backup_check(request: Request):
        data = await body(request)
        content, refusal = await checked(request, data, "backup:" + c.client_of(request))
        if refusal:
            return refusal
        return await asyncio.to_thread(st.backups.preview, content)

    @app.post("/api/backup/restore")
    async def backup_restore(request: Request):
        data = await body(request)
        user = getattr(request.state, "user", None)
        if data.get("confirm") is not True:
            return err("backup.err.confirm")
        content, refusal = await checked(request, data, "backup:" + c.client_of(request))
        if refusal:
            c.audit_from(request, user, "backup.restore_failed", "failed", detail={"reason": _key_of(refusal)})
            return refusal
        try:
            summary = await asyncio.to_thread(st.backups.restore, content, user, c.requester_ip(request), agent_of(request))
        except BackupError as exc:
            c.audit_from(request, user, "backup.restore_failed", "failed", detail={"reason": exc.key})
            return refused(exc)
        if data.get("upload"):
            st.backups.drop_upload(str(data["upload"]))
        response = JSONResponse({"ok": True, "summary": summary, "signed_out": True})
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    # ------------------------------------------------------------------ first start: restore instead of a new account
    def setup_guard(request: Request, code: str):
        client = c.client_of(request)
        blocked = rate_blocked(client)
        if blocked:
            return blocked
        if st.store.user_count() > 0:
            return err("setup.err.done", 409)
        if not st.setup_code.check(code or ""):
            st.limiter.failure(client)
            c.audit_from(request, None, "auth.setup_failed", "failed", detail={"step": "restore"})
            return err("setup.err.code", 403)
        return None

    @app.post("/api/setup/restore/upload")
    async def setup_restore_upload(request: Request):
        refusal = setup_guard(request, request.headers.get(SETUP_HEADER, ""))
        if refusal:
            return refusal
        return await receive(request, slot=SETUP_SLOT)

    @app.post("/api/setup/restore/check")
    async def setup_restore_check(request: Request):
        data = await body(request)
        refusal = setup_guard(request, str(data.get("code") or ""))
        if refusal:
            return refusal
        content, refusal = await checked(request, {"upload": SETUP_SLOT, "passphrase": data.get("passphrase")}, c.client_of(request))
        if refusal:
            return refusal
        return await asyncio.to_thread(st.backups.preview, content)

    @app.post("/api/setup/restore")
    async def setup_restore(request: Request):
        data = await body(request)
        refusal = setup_guard(request, str(data.get("code") or ""))
        if refusal:
            return refusal
        content, refusal = await checked(request, {"upload": SETUP_SLOT, "passphrase": data.get("passphrase")}, c.client_of(request))
        if refusal:
            return refusal
        try:
            summary = await asyncio.to_thread(st.backups.restore, content, None, c.requester_ip(request), agent_of(request))
        except BackupError as exc:
            return refused(exc)
        st.backups.drop_upload(SETUP_SLOT)
        users = [u["username"] for u in st.store.query("SELECT username FROM users WHERE disabled=0 AND source='local' ORDER BY username COLLATE NOCASE")]
        return {"ok": True, "summary": summary, "users": users[:20]}
