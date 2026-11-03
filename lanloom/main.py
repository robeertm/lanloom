"""HTTP API, WebSocket live feed and static web app."""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, auth, config as config_mod, mapmodel
from .core import Core
from .crypto import Vault, load_or_create_key
from .store import Store
from .updates import Updates

log = logging.getLogger("lanloom")
WEB = Path(__file__).resolve().parent / "web"
I18N = Path(__file__).resolve().parent / "i18n"
LANGS = ("en", "de")
STATE_CHANGING = ("POST", "PUT", "PATCH", "DELETE")
PUBLIC_API = {"/api/health", "/api/state", "/api/login", "/api/setup/account", "/api/passkey/login/options",
              "/api/passkey/login/verify"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def load_i18n(lang: str) -> dict:
    out = {}
    for name in ("%s.json" % lang, "board.%s.json" % lang):
        try:
            out.update(json.loads((I18N / name).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return out


class App:
    """Everything the request handlers need, created once per process."""

    def __init__(self, cfg, store: Optional[Store] = None):
        self.cfg = cfg
        if store is None:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            store = Store(str(cfg.data_dir / "lanloom.db"))
            vault = Vault(store, load_or_create_key(cfg.data_dir))
        else:
            from .crypto import KEY_BYTES
            vault = Vault(store, os.urandom(KEY_BYTES))
        self.store = store
        self.vault = vault
        self.accounts = auth.Accounts(store)
        self.sessions = auth.Sessions(store)
        self.passkeys = auth.Passkeys(store)
        self.limiter = auth.LoginLimiter()
        self.setup_code = auth.SetupCode(cfg.data_dir, store)
        self.core = Core(cfg, store, vault)
        self.updates = Updates(cfg, store)
        if cfg.demo:
            from .demo import Demo
            self.core.demo = Demo()
            if os.environ.get("LANLOOM_DEMO_SEED", "").strip() in ("1", "true", "yes"):
                self.core.demo.seed(self.core)
                self.core.demo.seed_integrations(self.core)
                store.set_setting("setup_done", True)


def create_app(cfg=None, store: Optional[Store] = None) -> FastAPI:
    cfg = cfg or config_mod.load()
    state = App(cfg, store)

    @asynccontextmanager
    async def lifespan(_app):
        code = state.setup_code.ensure()
        if code:
            log.warning("First start: open the web page and enter the setup code %s (also in %s)", code, state.setup_code.path)
        await state.core.start()
        try:
            yield
        finally:
            await state.core.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ll = state

    # ------------------------------------------------------------------ guards
    proxies = auth.parse_proxies(getattr(cfg, "trusted_proxies", ()))

    def peer_of(request: Request) -> str:
        return request.client.host if request.client else "?"

    def client_of(request: Request) -> str:
        """Rate-limit key of the client (forwarding headers only from LANLOOM_TRUSTED_PROXIES)."""
        return auth.limit_key(auth.client_address(peer_of(request), request.headers, proxies))

    def current_user(request: Request) -> Optional[dict]:
        return state.sessions.resolve(request.cookies.get(auth.SESSION_COOKIE))

    def secure_cookie(request: Request) -> bool:
        if cfg.cookie_secure == "always":
            return True
        if cfg.cookie_secure == "never":
            return False
        if request.url.scheme == "https":
            return True
        return auth.from_trusted_proxy(peer_of(request), proxies) and request.headers.get("x-forwarded-proto", "") == "https"

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if cfg.probe and path.startswith("/api/") and path != "/api/health":
            # trial start of an update candidate: the updater only reads the health check
            return JSONResponse({"error": "app.err.probe"}, status_code=503)
        if path.startswith("/api/"):
            if request.method in STATE_CHANGING:
                if request.headers.get("x-lanloom") != "1":
                    return JSONResponse({"error": "auth.err.header"}, status_code=403)
                if not auth.origin_allowed(request.headers.get("origin"), request.headers.get("host"), cfg.trusted_origins):
                    return JSONResponse({"error": "auth.err.origin"}, status_code=403)
            if path not in PUBLIC_API and not path.startswith("/api/i18n/"):
                user = current_user(request)
                if not user:
                    return JSONResponse({"error": "auth.err.required"}, status_code=401)
                request.state.user = user
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "request.err.json")
        if not isinstance(data, dict):
            raise HTTPException(400, "request.err.json")
        return data

    def set_session(response: Response, request: Request, user_id: int) -> None:
        token = state.sessions.create(user_id)
        response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                            samesite="strict", secure=secure_cookie(request), path="/")

    def err(key: str, status: int = 400, **extra) -> JSONResponse:
        return JSONResponse(dict({"error": key}, **extra), status_code=status)

    # ------------------------------------------------------------------ public
    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/state")
    async def get_state(request: Request):
        user = current_user(request)
        return {"version": __version__, "setup_needed": state.store.user_count() == 0, "authenticated": bool(user),
                "user": user["username"] if user else None, "demo": cfg.demo,
                "setup_done": bool(state.store.get_setting("setup_done", False)),
                "language": state.store.get_setting("language", None)}

    @app.get("/api/i18n/{lang}")
    async def i18n(lang: str):
        if lang not in LANGS:
            raise HTTPException(404)
        return JSONResponse(load_i18n(lang), headers={"Cache-Control": "no-cache"})

    @app.post("/api/setup/account")
    async def setup_account(request: Request):
        data = await body(request)
        client = client_of(request)
        wait = state.limiter.wait_seconds(client)
        if wait:
            return err("auth.err.rate", 429, wait=round(wait))
        if state.store.user_count() > 0:
            return err("setup.err.done", 409)
        if not state.setup_code.check(str(data.get("code") or "")):
            state.limiter.failure(client)
            return err("setup.err.code", 403)
        problem = auth.Accounts.validate(str(data.get("username") or ""), str(data.get("password") or ""))
        if problem:
            return err(problem)
        if data.get("language") in LANGS:
            state.store.set_setting("language", data["language"])
        user_id = state.accounts.create(str(data["username"]), str(data["password"]))
        state.setup_code.clear()
        state.limiter.success(client)
        response = JSONResponse({"ok": True})
        set_session(response, request, user_id)
        return response

    @app.post("/api/login")
    async def login(request: Request):
        data = await body(request)
        client = client_of(request)
        wait = state.limiter.wait_seconds(client)
        if wait:
            return err("auth.err.rate", 429, wait=round(wait))
        user = await asyncio.to_thread(state.accounts.verify, str(data.get("username") or ""), str(data.get("password") or ""))
        if not user:
            state.limiter.failure(client)
            return err("auth.err.invalid", 401)
        state.limiter.success(client)
        response = JSONResponse({"ok": True})
        set_session(response, request, user["id"])
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        state.sessions.destroy(request.cookies.get(auth.SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    def rp_of(request: Request) -> tuple:
        host = (request.headers.get("host") or "").split(":")[0]
        origin = request.headers.get("origin") or ("%s://%s" % (request.url.scheme, request.headers.get("host")))
        return host, origin.rstrip("/")

    @app.post("/api/passkey/login/options")
    async def pk_login_options(request: Request):
        rp_id, origin = rp_of(request)
        return state.passkeys.login_options(rp_id, origin)

    @app.post("/api/passkey/login/verify")
    async def pk_login_verify(request: Request):
        data = await body(request)
        client = client_of(request)
        wait = state.limiter.wait_seconds(client)
        if wait:
            return err("auth.err.rate", 429, wait=round(wait))
        user_id = await asyncio.to_thread(state.passkeys.login_verify, str(data.get("ceremony") or ""), data.get("credential") or {})
        if not user_id:
            state.limiter.failure(client)
            return err("auth.err.passkey", 401)
        state.limiter.success(client)
        response = JSONResponse({"ok": True})
        set_session(response, request, user_id)
        return response

    # ------------------------------------------------------------------ account
    @app.get("/api/passkeys")
    async def pk_list(request: Request):
        return {"passkeys": state.passkeys.list(request.state.user["id"])}

    @app.post("/api/passkey/register/options")
    async def pk_reg_options(request: Request):
        rp_id, origin = rp_of(request)
        user = state.accounts.get(request.state.user["id"])
        return state.passkeys.registration_options(user, rp_id, origin)

    @app.post("/api/passkey/register/verify")
    async def pk_reg_verify(request: Request):
        data = await body(request)
        ok = await asyncio.to_thread(state.passkeys.registration_verify, request.state.user["id"],
                                     str(data.get("ceremony") or ""), data.get("credential") or {}, str(data.get("name") or ""))
        return {"ok": True} if ok else err("auth.err.passkey")

    @app.delete("/api/passkeys/{key_id}")
    async def pk_delete(key_id: int, request: Request):
        return {"ok": state.passkeys.delete(request.state.user["id"], key_id)}

    @app.post("/api/account/password")
    async def change_password(request: Request):
        data = await body(request)
        user = state.accounts.get(request.state.user["id"])
        if not await asyncio.to_thread(state.accounts.verify, user["username"], str(data.get("current") or "")):
            return err("auth.err.invalid", 403)
        problem = auth.Accounts.validate(user["username"], str(data.get("password") or ""))
        if problem:
            return err(problem)
        state.accounts.set_password(user["id"], str(data["password"]))
        response = JSONResponse({"ok": True})
        set_session(response, request, user["id"])
        return response

    # ------------------------------------------------------------------ settings, networks, scans
    @app.get("/api/settings")
    async def get_settings():
        return state.core.settings()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        data = await body(request)
        result = state.core.update_settings(data)
        if data.get("setup_done") and cfg.demo and state.core.demo:
            state.core.demo.seed_integrations(state.core)
            state.core.structure_version += 1
        return result

    @app.get("/api/networks")
    async def networks():
        from . import scan
        nets = await asyncio.to_thread(state.core.networks)
        for n in nets:
            ok, key = scan.check_cidr(n.get("cidr", "")) if not cfg.demo else (True, "")
            n["allowed"], n["reason"] = ok, key
        host_mode = any(n.get("host_mode") for n in nets)
        return {"networks": nets, "host_mode": host_mode or cfg.demo}

    @app.get("/api/scan")
    async def scan_status():
        last = state.store.one("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
        return {"current": state.core.scan_state, "last": last}

    @app.post("/api/scan")
    async def scan_start(request: Request):
        data = await body(request)
        cidrs = [str(c) for c in (data.get("cidrs") or []) if isinstance(c, str)][:8]
        try:
            return state.core.start_scan(cidrs, deep=bool(data.get("deep")), consent=bool(data.get("consent")))
        except ValueError as exc:
            return err(str(exc) or "scan.err.invalid")

    @app.delete("/api/scan")
    async def scan_cancel():
        return {"ok": state.core.cancel_scan()}

    # ------------------------------------------------------------------ inventory and map
    @app.get("/api/devices")
    async def devices():
        return {"devices": state.core.devices()}

    @app.patch("/api/devices/{device_id}")
    async def patch_device(device_id: str, request: Request):
        data = await body(request)
        dev = state.core.patch_device(device_id, data)
        return dev if dev else err("device.err.missing", 404)

    @app.delete("/api/devices/{device_id}")
    async def delete_device(device_id: str):
        return {"ok": state.core.forget_device(device_id)}

    @app.get("/api/map")
    async def get_map(view: str = "overview"):
        if view not in ("overview", "network"):
            view = "overview"
        return mapmodel.build(state.core, view)

    @app.get("/api/component/{comp_id:path}")
    async def component(comp_id: str):
        detail = mapmodel.component_detail(state.core, comp_id)
        return detail if detail else err("component.err.missing", 404)

    # ------------------------------------------------------------------ integrations
    @app.get("/api/integrations")
    async def integrations():
        return {"integrations": state.core.integrations()}

    @app.post("/api/integrations/test")
    async def integration_test(request: Request):
        data = await body(request)
        try:
            return await state.core.test_integration(str(data.get("kind") or ""), data.get("config") or {}, data.get("secrets") or {},
                                                     existing_id=data.get("id") if isinstance(data.get("id"), int) else None)
        except ValueError as exc:
            return err(str(exc))

    @app.post("/api/integrations")
    async def integration_save(request: Request):
        data = await body(request)
        try:
            return state.core.save_integration(str(data.get("kind") or ""), str(data.get("name") or ""), data.get("config") or {},
                                               data.get("secrets") or {},
                                               existing_id=data.get("id") if isinstance(data.get("id"), int) else None)
        except ValueError as exc:
            return err(str(exc))

    @app.delete("/api/integrations/{integ_id}")
    async def integration_delete(integ_id: int):
        return {"ok": state.core.delete_integration(integ_id)}

    # ------------------------------------------------------------------ updates
    @app.get("/api/update")
    async def update_state():
        return await state.updates.check(force=False)

    @app.post("/api/update/check")
    async def update_check():
        return await state.updates.check(force=True)

    @app.post("/api/update/apply")
    async def update_apply(request: Request):
        data = await body(request)
        try:
            return state.updates.request(str(data.get("version") or ""))
        except ValueError as exc:
            return err(str(exc))

    # ------------------------------------------------------------------ live
    @app.websocket("/api/live")
    async def live_feed(ws: WebSocket):
        if not auth.origin_allowed(ws.headers.get("origin"), ws.headers.get("host"), cfg.trusted_origins):
            await ws.close(code=4403)
            return
        if not state.sessions.resolve(ws.cookies.get(auth.SESSION_COOKIE)):
            await ws.close(code=4401)
            return
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue()
        state.core.clients.add(queue)
        try:
            if state.core.last_frame:
                await ws.send_text(json.dumps(state.core.last_frame, separators=(",", ":")))
            while True:
                text = await queue.get()
                await ws.send_text(text)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            state.core.clients.discard(queue)

    # ------------------------------------------------------------------ static web app
    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/{path:path}")
    async def static(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        target = (WEB / path).resolve()
        if not str(target).startswith(str(WEB)) or not target.is_file():
            raise HTTPException(404)
        headers = {"Cache-Control": "no-cache"}
        return FileResponse(target, headers=headers)

    return app


# ---------------------------------------------------------------------- self-test and entry point
def selftest() -> int:
    """Offline check used by the updater before an image is trusted: no network, no writes outside memory."""
    from types import SimpleNamespace
    problems = []
    try:
        from . import classify, core, demo, live, scan, sources  # noqa: F401 - import check
        import updater  # noqa: F401
        store = Store(":memory:")
        cfg = SimpleNamespace(data_dir=Path("/nonexistent/lanloom-selftest"), host="127.0.0.1", port=0,
                              update_check=False, demo=True, probe=True, release_feed="", image_repo="", cookie_secure="never",
                              trusted_origins=(), trusted_proxies=())
        c = core.Core(cfg, store, None)
        c.demo = demo.Demo()
        c.ingest({"hosts": [demo.scan_host(h) for h in demo.HOSTS]}, None)
        for view in ("overview", "network"):
            m = mapmodel.build(c, view)
            if not m["components"]:
                problems.append("empty map view " + view)
        frame = c.tick(time.time())
        if "dots" not in frame:
            problems.append("live tick")
        en, de = load_i18n("en"), load_i18n("de")
        if not en or set(en) != set(de):
            problems.append("i18n keys differ (%d en / %d de)" % (len(en), len(de)))
        for rel in ("index.html", "js/app.js", "board/board.js", "board/router.js", "css/tokens.css", "fonts/InterVariable.woff2"):
            if not (WEB / rel).is_file():
                problems.append("missing web asset " + rel)
        store.close()
    except Exception as exc:     # report, never crash with a traceback only
        problems.append("%s: %s" % (type(exc).__name__, exc))
    if problems:
        for p in problems:
            print("selftest: FAIL", p)
        return 1
    print("selftest: ok", __version__)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lanloom")
    parser.add_argument("--selftest", action="store_true", help="offline self-test, exit code 0 = ok")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.selftest:
        return selftest()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load()
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        log.error("The data directory %s is not writable (%s). Mount a volume there that user 10001 may write to.",
                  cfg.data_dir, exc.strerror or exc)
        return 3
    import uvicorn
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", access_log=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
