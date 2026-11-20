"""HTTP API, WebSocket live feed and static web app."""
import argparse
import asyncio
import hashlib
import hmac
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
from fastapi.routing import APIRoute
from starlette.routing import Match

from . import __version__, access, agents as agents_mod, api_v1, apitokens, auth, config as config_mod, demoview, flood, mapmodel, palette, setup_assist, textfmt
from . import sites as sites_mod
from .audit import AuditLog
from .core import Core
from .integrations import catalog
from .crypto import Vault, load_or_create_key
from .store import Store
from . import updates as updates_mod
from . import whatsnew
from . import mqtt_out as mqtt_out_mod
from .updates import Updates

log = logging.getLogger("lanloom")
WEB = Path(__file__).resolve().parent / "web"
I18N = Path(__file__).resolve().parent / "i18n"
LANGS = tuple(textfmt.languages())
STATE_CHANGING = ("POST", "PUT", "PATCH", "DELETE")
# routes reachable without a session (the full table with roles is access.RULES)
PUBLIC_API = {p for (m, p), r in access.RULES.items() if r.role == "public" and p.startswith("/api/") and "{" not in p}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# Response headers on every answer (static files, API, refusals of the guard). HSTS is added only when the request
# really arrived over HTTPS (directly or from a trusted proxy that says so); plain HTTP in the LAN never gets it.
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": ("accelerometer=(), camera=(), display-capture=(), geolocation=(), gyroscope=(), "
                           "hid=(), magnetometer=(), microphone=(), midi=(), payment=(), serial=(), usb=()"),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HSTS = "max-age=31536000"
# Add-on mode: the hub shows the add-on inside its own page (ingress frame on the same origin), so framing by the same
# origin is allowed there - and only there; a plain install keeps "never in a frame".
ADDON_FRAME_HEADERS = {"Content-Security-Policy": CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'"),
                       "X-Frame-Options": "SAMEORIGIN"}
# FastAPI's built-in OpenTelemetry hooks stay off: no spans, metrics or log records, and no exporter from OTEL_*
# environment variables. (No telemetry SDK or exporter is installed either; tests/test_egress.py checks both.)
NO_TELEMETRY = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False, "auto_configure": False}


def i18n_languages() -> list:
    """Languages with a translation file (one ``<lang>.json`` plus ``board.<lang>.json`` each)."""
    return textfmt.languages()


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
            key = load_or_create_key(cfg.data_dir)
        else:
            from .crypto import KEY_BYTES
            key = os.urandom(KEY_BYTES)
        vault = Vault(store, key)
        self.store = store
        self.vault = vault
        self.instance_key = key          # the vaults of further sites (lanloom/sites.py) use the same instance key
        sub = lambda label: hmac.new(key, label, hashlib.sha256).digest()     # separate keys per purpose
        self.accounts = auth.Accounts(store)
        self.sessions = auth.Sessions(store)
        self.passkeys = auth.Passkeys(store)
        from .backup import recovery_pepper
        # the pepper of the recovery codes moves with a restored backup; otherwise it is derived from the instance key
        self.factors = auth.SecondFactor(store, vault, recovery_pepper(vault, sub(b"lanloom-recovery-v1")))
        self.tickets = auth.Tickets()
        self.invites = auth.Invites(store)
        self.notices = auth.Notices(store)
        self.policy = auth.Policy(store)
        self.audit = AuditLog(store, sub(b"lanloom-audit-v1"))
        self.limiter = auth.LoginLimiter()
        self.setup_code = auth.SetupCode(cfg.data_dir, store)
        self._real_core = Core(cfg, store, vault)
        self.notices.listener = self.real_core.notify.on_notice
        self.demo_view = demoview.DemoView(cfg)
        self.updates = Updates(cfg, store)
        self.core.reports.update_state = self.updates.state
        from .firewall.editor import Editor
        self.firewall = Editor(cfg, store)
        self.firewall.live = self.core.live_configs
        self.firewall.journal = self.real_core.reports.journal       # finished real changes -> nightly report
        self.firewall.notify = self.real_core.notify                  # a change put back -> notification rules
        from .guest_protect import app_identity as _app_identity
        self.firewall.identity = lambda: _app_identity(self.core)      # guests that keep the network running
        from .addrusers import address_users as _address_users
        self.firewall.address_users = lambda: _address_users(self.core)  # what uses an address (hub, Lanloom itself)
        from .firewall import report as _fw_report  # noqa: F401 - registers the nightly report section
        from .ai.manager import AI
        from .ai import report as _ai_report  # noqa: F401 - registers the nightly report section
        self.ai = AI(self.core, store, vault, cfg)
        self.core.ai = self.ai
        self.ai.firewall = self.firewall                       # read for suggestions; the AI never writes a draft
        self.core.reports.plain_hook = self.ai.plain_for       # "In plain words" paragraph (optional)
        self.core.reports.plain_delivery = self.ai.deliver_plain
        self.core.reports.night_hook = self.ai.nightly_identify
        self.core.audit = self.audit
        from . import access_report as _access_report  # noqa: F401 - registers the nightly report section
        from .backup import Backups
        from . import backup_report as _backup_report  # noqa: F401 - registers the nightly report section
        from . import availability_report as _availability_report  # noqa: F401 - registers the availability section
        from .controlroom import report as _controlroom_report  # noqa: F401 - registers the control room section
        from . import energy_report as _energy_report  # noqa: F401 - registers the energy section
        from . import failure_report as _failure_report  # noqa: F401 - registers the failure chain section
        from . import resilience_report as _resilience_report  # noqa: F401 - registers the resilience section
        from . import incidents_report as _incidents_report  # noqa: F401 - registers the incidents section
        from . import services_report as _services_report  # noqa: F401 - registers the services section
        from . import storage_report as _storage_report  # noqa: F401 - registers the storage + counter check sections
        from . import inventory_report as _inventory_report  # noqa: F401 - registers the inventory section
        from . import vulns_report as _vulns_report  # noqa: F401 - registers the vulnerability section
        self.backups = Backups(self)
        self.core.backups = self.backups
        from .apitokens import Tokens
        from .ingest import Ingest
        from .webhooks import Webhooks
        from . import api_report as _api_report  # noqa: F401 - registers the nightly report section "API"
        self.tokens = Tokens(store)
        self.ingest = Ingest(store)
        self.webhooks = Webhooks(self.real_core, store, vault)
        self.real_core.tokens, self.real_core.webhooks = self.tokens, self.webhooks
        self.real_core.api_ingest = self.ingest.of_site(sites_mod.HOME)      # the external reports of the home site (M1T)
        self.real_core.reports.journal.listeners.append(self.webhooks.on_journal)
        self.real_core.notify.taps.append(self.webhooks.on_notify)
        self.real_core.reports.report_listeners.append(self.webhooks.on_report)
        from .mqtt_out import MqttOut
        self.mqtt_out = MqttOut(self.real_core, store, vault, cfg)     # opt-in MQTT output (off by default)
        self.real_core.mqtt_out = self.mqtt_out
        if cfg.demo:
            from .demo import Demo
            self.core.demo = Demo()
            if os.environ.get("LANLOOM_DEMO_SEED", "").strip() in ("1", "true", "yes"):
                self.core.demo.seed(self.core)
                from .inventory_demo import seed as seed_inventory
                seed_inventory(self.core)
                from .vulns_demo import seed as seed_vulns
                seed_vulns(self.core)
                self.core.demo.seed_integrations(self.core)
                self.core.demo.seed_reports(self.core)
                self.core.demo.seed_access(self)
                from .notify.demo import seed as seed_notify
                seed_notify(self.core)
                self.ai.seed_demo_hint()            # one open identification suggestion to look at
                from .mqtt_out import seed_demo as seed_mqtt_out
                seed_mqtt_out(store)
                from .webhooks import seed_demo as seed_webhooks
                from .ingest import seed_demo as seed_ingest
                seed_webhooks(self.webhooks)
                seed_ingest(self.ingest)
                store.set_setting("setup_done", True)
        # sites (M1): this installation is the site "home"; further sites get a core of their own when the app starts
        self.sites = sites_mod.Sites(self)
        self.sites.wire(self.real_core)
        if cfg.demo and os.environ.get("LANLOOM_DEMO_SEED", "").strip() in ("1", "true", "yes"):
            from .demo_sites import seed_sites
            seed_sites(self)


    @property
    def real_core(self) -> Core:
        """The core of the home site. A request that reads it is noted as reading the home site (M1T): an answer of
        another site that touched it names both sites and is never drawn as one site's answer."""
        sites_mod.note_served(sites_mod.HOME)
        return self._real_core

    @property
    def core(self) -> Core:
        """The core of this request: the sample network for a demo view (see demoview), else the core of the request's
        site (see sites; the guard picks it among the sites released for the person), else the home site."""
        if demoview.ACTIVE.get() and self.demo_view.core is not None:
            return self.demo_view.core
        sid = sites_mod.ACTIVE.get()
        if sid is None or sid == sites_mod.HOME:
            return self.real_core
        if sid == "":
            raise HTTPException(403, "site.err.none")        # signed in, but no site released for this person
        core = self.sites.cores.get(sid) if getattr(self, "sites", None) else None
        if core is None:
            raise HTTPException(404, "site.err.unknown")     # never fall back to another site's data
        sites_mod.note_served(getattr(core, "site_id", sid))
        return core


def create_app(cfg=None, store: Optional[Store] = None) -> FastAPI:
    cfg = cfg or config_mod.load()
    state = App(cfg, store)

    @asynccontextmanager
    async def lifespan(_app):
        code = state.setup_code.ensure()
        if code:
            log.warning("First start: open the web page and enter the setup code %s (also in %s)", code, state.setup_code.path)
        await state.core.start()
        await state.sites.start()
        state.backups.start()
        mqtt_task = asyncio.create_task(state.mqtt_out.run())
        hooks_task = asyncio.create_task(state.webhooks.run())
        try:
            yield
        finally:
            for task in (mqtt_task, hooks_task):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await state.backups.stop()
            await state.demo_view.stop()
            await state.sites.stop()
            await state.core.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, telemetry=NO_TELEMETRY)
    app.state.ll = state

    # ------------------------------------------------------------------ guards
    proxies = auth.parse_proxies(getattr(cfg, "trusted_proxies", ()))
    proxy_auth = auth.ProxyAuth(cfg, proxies, state)

    def peer_of(request) -> str:
        return request.client.host if request.client else "?"

    def client_of(request) -> str:
        """Rate-limit key of the client (forwarding headers only from LANLOOM_TRUSTED_PROXIES)."""
        return auth.limit_key(auth.client_address(peer_of(request), request.headers, proxies))

    def requester_ip(request) -> str:
        return auth.client_address(peer_of(request), request.headers, proxies)

    def identify(conn) -> Optional[dict]:
        """The signed-in user of a request or WebSocket: trusted auth proxy header first, then the session cookie."""
        user = proxy_auth.user(peer_of(conn), conn.headers, agent=auth.agent_label(conn.headers.get("user-agent", "")),
                               ip=requester_ip(conn))
        if user is not None:
            return user or None          # {} = the proxy named somebody Lanloom refuses
        return state.sessions.resolve(conn.cookies.get(auth.SESSION_COOKIE))

    def current_user(request: Request) -> Optional[dict]:
        return identify(request)

    def secure_cookie(request: Request) -> bool:
        if cfg.cookie_secure == "always":
            return True
        if cfg.cookie_secure == "never":
            return False
        if request.url.scheme == "https":
            return True
        return auth.from_trusted_proxy(peer_of(request), proxies) and request.headers.get("x-forwarded-proto", "") == "https"

    def over_https(request: Request) -> bool:
        """The request really arrived over HTTPS: directly, or from a trusted proxy that says so (never by setting)."""
        if request.url.scheme == "https":
            return True
        return auth.from_trusted_proxy(peer_of(request), proxies) and request.headers.get("x-forwarded-proto", "") == "https"

    public_flood = flood.FloodGuard(flood.PUBLIC_LIMIT)
    user_flood = flood.FloodGuard(flood.USER_LIMIT)
    state.flood = (public_flood, user_flood)

    def flooded(wait: float) -> JSONResponse:
        return JSONResponse({"error": "auth.err.rate", "wait": int(wait) + 1}, status_code=429)

    def match_route(scope) -> tuple:
        """(route template, path params) of the route that will answer, or (None, {})."""
        for route in app.router.routes:
            if isinstance(route, APIRoute):
                m, child = route.matches(scope)
                if m == Match.FULL:
                    return route.path, child.get("path_params", {})
        return None, {}

    def stepup_methods(user: dict) -> list:
        if user.get("source") == "proxy":
            return []
        f = state.factors.summary(user["id"])
        out = (["passkey"] if f["passkeys"] else []) + (["totp"] if f["totp"] else []) + (["recovery"] if f["recovery_left"] else [])
        return out or ["password"]

    def audit_from(request, user, action: str, outcome: str = "ok", target: str = "", detail=None, actor_name=None) -> None:
        try:
            state.audit.record(action, user, target=target, outcome=outcome, ip=requester_ip(request),
                               agent=auth.agent_label(request.headers.get("user-agent", "")), detail=detail, actor_name=actor_name)
        except Exception:        # the audit log must never break the request; a failure is visible in the log
            log.exception("audit log write failed (%s)", action)

    headers_of_mode = dict(SECURITY_HEADERS, **(ADDON_FRAME_HEADERS if getattr(cfg, "addon", False) else {}))

    def origin_ok(conn) -> bool:
        """Origin check of state-changing requests and the live socket. In add-on mode the ingress proxy forwards the
        hub's own address as X-Forwarded-Host; it is believed from that proxy only."""
        origin, host = conn.headers.get("origin"), conn.headers.get("host")
        if auth.origin_allowed(origin, host, cfg.trusted_origins):
            return True
        if getattr(cfg, "addon", False) and auth.from_trusted_proxy(peer_of(conn), proxies):
            fwd = (conn.headers.get("x-forwarded-host") or "").split(",")[0].strip()
            return bool(fwd) and auth.origin_allowed(origin, fwd, ())
        return False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        response = await guarded(request, call_next)
        for name, value in headers_of_mode.items():
            response.headers[name] = value
        if over_https(request):
            response.headers["Strict-Transport-Security"] = HSTS
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            # M2: the answer names the site it comes from, so the page can throw away an answer that does not belong to
            # the site it shows now; and no cache may hand the answer of one site to a request for another
            # M1T: the name is the site of the core whose data the answer carries, never just the site the request asked for
            sent = getattr(request.state, "site_sent", None)
            if sent:
                name = sites_mod.served_name(sent, getattr(request.state, "site_scope", "global"))
                if name == sites_mod.MIXED:
                    log.error("an answer read the data of more than one site: %s %s", request.method, request.url.path)
                response.headers[sites_mod.MIRROR] = name
            vary = [v.strip() for v in response.headers.get("vary", "").split(",") if v.strip()]
            for name in ("Cookie", "X-Lanloom-Site"):
                if name.lower() not in (v.lower() for v in vary):
                    vary.append(name)
            response.headers["Vary"] = ", ".join(vary)
        return response

    async def guarded(request: Request, call_next):
        path = request.url.path
        rule = None
        user = None
        sites_mod.SERVED.set(set())                    # the cores this request reads (M1T)
        if cfg.probe and path.startswith("/api/") and path != "/api/health":
            # trial start of an update candidate: the updater only reads the health check
            return JSONResponse({"error": "app.err.probe"}, status_code=503)
        # an API token (llt_...); the bearer tokens of the executor service and the host agents keep their own routes
        token = (apitokens.parse_bearer(request.headers.get("authorization", ""))
                 if path.startswith("/api/") and not path.startswith(("/api/agent/", "/api/executor/")) else None)
        if token is not None:
            return await token_request(request, call_next, token)
        if path.startswith("/api/"):
            if request.method in STATE_CHANGING:
                if request.headers.get("x-lanloom") != "1":
                    return JSONResponse({"error": "auth.err.header"}, status_code=403)
                if not origin_ok(request):
                    return JSONResponse({"error": "auth.err.origin"}, status_code=403)
            template, params = match_route(request.scope)
            rule = access.rule_for(request.method, template) if template else None
            if rule is not None and rule.role == "executor":
                if not state.firewall.token_ok(request.headers.get("authorization", "")):
                    return JSONResponse({"error": "auth.err.required"}, status_code=401)   # token of the executor service, no session
            elif rule is not None and rule.role == "agent":
                aid, agent_site = None, None
                for site_id, site_core in list(state.sites.cores.items()):      # a host agent belongs to the site it paired with
                    aid = site_core.agents.token_agent(request.headers.get("authorization", ""))
                    if aid:
                        agent_site = site_id
                        break
                if not aid:
                    return JSONResponse({"error": "auth.err.required"}, status_code=401)   # token of a host agent, no session
                how, asked = asked_site(request)
                if how == "header" and asked != agent_site:
                    # a host agent reports for the site it paired with; a report naming another site is refused, never booked
                    return JSONResponse({"error": "site.err.agent_site"}, status_code=409)
                request.state.agent = aid
                sites_mod.ACTIVE.set(agent_site)
            elif rule is not None and rule.role == "public":
                if request.method in STATE_CHANGING:
                    wait = public_flood.hit(client_of(request))
                    if wait:
                        return flooded(wait)
            else:
                user = current_user(request)
                if not user:
                    return JSONResponse({"error": "auth.err.required"}, status_code=401)
                if template is None:
                    pass                                   # unknown /api/ path: answered with 404 below
                elif user.get("stage") == "enroll" and not (rule and rule.enroll):
                    return JSONResponse({"error": "auth.err.enroll"}, status_code=403)
                else:
                    need = rule.role if rule else "admin"   # a route missing from the table: administrators only
                    if not access.allows(user.get("role"), need):
                        if request.method in STATE_CHANGING:
                            audit_from(request, user, rule.audit if rule and rule.audit not in (None, "@") else "access.denied",
                                       "denied", target=template, detail={"need": need})
                        return JSONResponse({"error": "auth.err.role", "need": need}, status_code=403)
                    if rule and rule.stepup and not (user.get("source") == "proxy" or state.sessions.fresh(user)):
                        return JSONResponse({"error": "auth.err.stepup", "methods": stepup_methods(user)}, status_code=403)
                if request.method in STATE_CHANGING:
                    wait = user_flood.hit("user:%s" % (user.get("id") or user.get("username") or "?"))
                    if wait:
                        return flooded(wait)
                request.state.user = user
            blocked = pick_site(request, user if user is not None else (current_user(request) if path == "/api/state" else None),
                                template or path)
            if blocked is not None:
                return blocked
            if state.demo_view.requested(request.cookies):
                # sample network (demo view): only for a signed-in session, and only reading
                viewer = user if user is not None else (current_user(request) if path == "/api/state" else None)
                if viewer and viewer.get("stage") != "enroll":
                    if request.method in STATE_CHANGING and path not in demoview.WRITABLE:
                        return JSONResponse({"error": "demo.err.read_only"}, status_code=409)
                    await state.demo_view.ensure()
                    demoview.ACTIVE.set(True)
        response = await call_next(request)
        if rule is not None and rule.audit and rule.audit != "@" and request.method in STATE_CHANGING:
            info = getattr(request.state, "audit", None) or {}
            if not info.get("skip"):
                code = response.status_code
                outcome = "ok" if code < 400 else ("denied" if code in (401, 403) else "failed")
                target = info.get("target")
                if target is None:
                    target = ",".join("%s" % v for v in params.values()) if params else ""
                audit_from(request, user, info.get("action") or rule.audit, outcome, target=target, detail=info.get("detail"))
        return response

    def asked_site(conn) -> tuple:
        """(how, value): where a request names its site - the header ``X-Lanloom-Site`` (every request of the page), the
        address ``?site=`` (plain links and downloads of the page, the live socket) or the cookie ``ll_site`` (the last site
        of the browser) - or (None, None) when it names none. An empty value counts as naming a site (an unknown one)."""
        for how, value in (("header", conn.headers.get(sites_mod.HEADER)), ("address", conn.query_params.get("site")),
                           ("cookie", conn.cookies.get(sites_mod.COOKIE))):
            if value is not None:
                return how, value.strip()
        return None, None

    def pick_site(conn, viewer: Optional[dict], template: str) -> Optional[JSONResponse]:
        """Set the site of this request (M1, M1T). A request that names a site (header, address or cookie) gets exactly
        that site when it is released for the person; an empty, unknown or not released site is refused with 404 on every
        site-bound route - never answered from another site, not from the person's first one either. Only a request that
        names no site gets the person's first released site. A route of the home site only (firewall editor, AI) refuses
        another site."""
        if not viewer or viewer.get("stage") == "enroll":
            return None
        scope = sites_mod.scope_of(template)
        conn.state.site_scope = scope
        how, asked = asked_site(conn)
        conn.state.site_named = how
        sid, unknown = state.sites.resolve(viewer, how, asked)
        sites_mod.ACTIVE.set(sid if sid is not None else "")
        if unknown:
            conn.state.site_unknown = True
            if scope != "global":
                return JSONResponse({"error": "site.err.unknown"}, status_code=404)
            return None
        if sid:
            conn.state.site_sent = sid                  # named in the answer (M2), from the data it carries (M1T)
        if sid is None and scope != "global":
            return JSONResponse({"error": "site.err.none"}, status_code=403)
        if scope == "primary" and sid != sites_mod.HOME:
            return JSONResponse({"error": "site.err.primary_only"}, status_code=409)
        return None

    async def token_request(request: Request, call_next, token: str):
        """A request with an API token (Authorization: Bearer llt_...): only the public API, no session, no cookies.
        The browser cannot send such a header to another site on its own, so the cross-site checks of the web page
        (X-Lanloom header, Origin) do not apply; the token's own request limit does."""
        path = request.url.path
        if not (path == api_v1.BASE or path.startswith(api_v1.BASE + "/")):
            return JSONResponse({"error": "api.err.v1_only"}, status_code=401)
        try:
            user = state.tokens.authenticate(token, requester_ip(request))
        except apitokens.TokenError as exc:
            if exc.status == 401:
                wait = public_flood.hit(client_of(request))      # guessing tokens is capped per client like the public routes
                if wait:
                    return flooded(wait)
            headers = {"Retry-After": str(int(exc.wait) + 1)} if exc.status == 429 else {"WWW-Authenticate": "Bearer"}
            return JSONResponse({"error": exc.key, **({"wait": int(exc.wait) + 1} if exc.wait else {})}, status_code=exc.status, headers=headers)
        template, params = match_route(request.scope)
        if template is None or not (template == api_v1.BASE or template.startswith(api_v1.BASE + "/")):
            return JSONResponse({"error": "api.err.not_found"}, status_code=404)
        rule = access.rule_for(request.method, template)
        need = rule.role if rule else "admin"
        if user["token"]["scope"] == "ingest":
            ok = (request.method, template) == api_v1.INGEST_ROUTE and access.allows(user["account_role"], "operator")
            if ok:
                user = dict(user, role="operator")
        else:
            ok = rule is not None and not rule.stepup and access.allows(user.get("role"), need)
        if not ok:
            if request.method in STATE_CHANGING:
                audit_from(request, user, rule.audit if rule and rule.audit not in (None, "@") else "access.denied", "denied",
                           target=template, detail={"need": need, "via": "api", "key_name": user["token"]["name"]})
            return JSONResponse({"error": "api.err.scope", "need": api_v1.SCOPE_OF_ROLE.get(need, need)}, status_code=403)
        request.state.user = user
        blocked = pick_site(request, user, template)
        if blocked is not None:
            return blocked
        response = await call_next(request)
        if rule is not None and rule.audit and rule.audit != "@" and request.method in STATE_CHANGING:
            info = getattr(request.state, "audit", None) or {}
            code = response.status_code
            outcome = "ok" if code < 400 else ("denied" if code in (401, 403) else "failed")
            target = info.get("target")
            if target is None:
                target = ",".join("%s" % v for v in params.values()) if params else ""
            audit_from(request, user, info.get("action") or rule.audit, outcome, target=target,
                       detail=dict(info.get("detail") or {}, via="api", key_name=user["token"]["name"]))
        return response

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "request.err.json")
        if not isinstance(data, dict):
            raise HTTPException(400, "request.err.json")
        return data

    def err(key: str, status: int = 400, **extra) -> JSONResponse:
        return JSONResponse(dict({"error": key}, **extra), status_code=status)

    def note(request: Request, **info) -> None:
        """Details for the audit entry the guard writes after the handler (never secrets: see audit.scrub)."""
        current = getattr(request.state, "audit", None) or {}
        current.update(info)
        request.state.audit = current

    def rp_of(request: Request) -> tuple:
        host = (request.headers.get("host") or "").split(":")[0]
        origin = request.headers.get("origin") or ("%s://%s" % (request.url.scheme, request.headers.get("host")))
        return host, origin.rstrip("/")

    # ------------------------------------------------------------------ public
    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/state")
    async def get_state(request: Request):
        user = current_user(request)
        full = bool(user) and user.get("stage") != "enroll"
        language = state.store.get_setting("language", None)
        if user:
            language = state.accounts.prefs(user["id"]).get("language") or language
        demo_view = bool(user) and demoview.ACTIVE.get()
        site_id = sites_mod.ACTIVE.get()
        unknown = bool(getattr(request.state, "site_unknown", False))
        if full and site_id == "":
            full = False            # signed in, but no site released, or the page named a site it cannot see: nothing of any site
            no_site = not unknown
        else:
            no_site = False
        out = {"version": __version__, "setup_needed": state.store.user_count() == 0, "authenticated": bool(user),
               "addon": bool(getattr(cfg, "addon", False)),
               "user": user["username"] if user else None, "demo": cfg.demo or demo_view, "demo_view": demo_view,
               "setup_done": demo_view or bool(state.store.get_setting("setup_done", False)),
               "language": language,
               "rate_unit": state.real_core.rate_unit(),
               "reports_unread": state.core.reports.unread() if full else 0,
               "report_tz_auto": state.core.store.get_setting("report_tz_auto", "") if full else "",
               "ai": state.ai.ready() if full and access.allows(user.get("role"), "operator") else {},
               "can": access.capabilities(user.get("role")) if user else {}, "enroll": bool(user) and user.get("stage") == "enroll",
               "proxy": bool(user) and user.get("source") == "proxy",
               "notices": len(state.notices.unseen(user["id"])) if full else 0,
               "hubs": [{"id": i["id"], "name": i["name"]} for i in state.core.integrations() if i["kind"] == "hub"]
               if full and state.core else [],
               "agents": len(state.core.agents.live) if full and state.core else 0,     # host agents + container sources (E4)
               "dns": sum(1 for i in state.core.integrations() if i["kind"] in state.core.dns_kinds()) if full and state.core else 0}
        if user and not out["enroll"]:
            if unknown and not demo_view:
                # M1T: the page named a site that is empty, unknown or not released - no fallback to another site's data;
                # the page drops the remembered site and starts again with the first released one (named here, nothing more)
                allowed = state.sites.allowed(user)
                out["site"] = {"id": None, "name": "", "home": False, "remote": False, "several": len(allowed) > 1, "unknown": True,
                               "first": allowed[0] if allowed else None, "count": len(allowed)}
            elif demo_view or no_site:
                out["site"] = {"id": None, "name": "", "home": False, "remote": False, "several": False, "none": no_site}
            else:
                sid = site_id or sites_mod.HOME
                allowed = state.sites.allowed(user)
                out["site"] = {"id": sid, "name": state.sites.name(sid), "home": sid == sites_mod.HOME,
                               "remote": bool(getattr(state.core, "remote", False)), "several": len(allowed) > 1,
                               "count": len(allowed), "version": state.sites.version}
        return out

    @app.get("/api/i18n/{lang}")
    async def i18n(lang: str):
        if lang not in LANGS:
            raise HTTPException(404)
        return JSONResponse(load_i18n(lang), headers={"Cache-Control": "no-cache"})

    from . import web_access
    web_access.register(app, web_access.Ctx(
        state=state, cfg=cfg, body=body, err=err, note=note, client_of=client_of, requester_ip=requester_ip,
        secure_cookie=secure_cookie, rp_of=rp_of, audit_from=audit_from, current_user=current_user, langs=LANGS))

    from . import web_backup
    web_backup.register(app, web_backup.Ctx(
        state=state, body=body, err=err, note=note, client_of=client_of, requester_ip=requester_ip, audit_from=audit_from))

    from . import web_api
    web_api.register(app, web_api.Ctx(state=state, body=body, err=err, note=note))
    api_v1.register(app, state, err, api_v1.read_json_limited)
    from . import web_sites
    web_sites.register(app, web_sites.Ctx(state=state, body=body, err=err, note=note))
    from . import web_storage
    web_storage.register(app, web_storage.Ctx(state=state, body=body, err=err, note=note))
    from . import web_history
    web_history.register(app, web_history.Ctx(state=state, err=err))
    from . import web_failure
    web_failure.register(app, web_failure.Ctx(state=state, body=body, err=err, note=note))
    from . import web_resilience
    web_resilience.register(app, web_resilience.Ctx(state=state, body=body, err=err, note=note))
    from . import web_incidents
    web_incidents.register(app, web_incidents.Ctx(state=state, body=body, err=err, note=note))
    from . import web_services
    web_services.register(app, web_services.Ctx(state=state, body=body, err=err, note=note))
    from . import web_controlroom
    web_controlroom.register(app, web_controlroom.Ctx(state=state, body=body, err=err, note=note))
    from . import web_energy
    web_energy.register(app, web_energy.Ctx(state=state, body=body, err=err, note=note))
    from . import web_inventory
    web_inventory.register(app, web_inventory.Ctx(state=state, body=body, err=err, note=note))
    from . import web_export
    web_export.register(app, web_export.Ctx(state=state, body=body, err=err, note=note, load_i18n=load_i18n))
    from . import web_vulns
    web_vulns.register(app, web_vulns.Ctx(state=state, body=body, err=err, note=note))
    from . import web_notify
    web_notify.register(app, web_notify.Ctx(state=state, body=body, err=err, note=note,
                                            agent=lambda r: auth.agent_label(r.headers.get("user-agent", ""))))

    # ------------------------------------------------------------------ settings, networks, scans
    @app.get("/api/settings")
    async def get_settings():
        return state.core.settings()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        data = await body(request)
        note(request, detail={k: v for k, v in data.items() if isinstance(v, (bool, int, float, str, type(None), list))})
        try:
            result = state.core.update_settings(data)
        except ValueError as exc:
            return err(str(exc))
        if data.get("setup_done") and cfg.demo and state.core.demo:
            from .inventory_demo import seed as seed_inventory
            seed_inventory(state.core)
            from .vulns_demo import seed as seed_vulns
            seed_vulns(state.core)
            state.core.demo.seed_integrations(state.core)
            state.core.demo.seed_reports(state.core)
            state.core.demo.seed_access(state)
            state.core.structure_version += 1
        return result

    @app.get("/api/paths")
    async def get_paths(window: str = "24h"):
        return await asyncio.to_thread(state.core.paths_summary, window)

    @app.get("/api/paths/lan")
    async def get_paths_lan(window: str = "24h"):
        return await asyncio.to_thread(state.core.paths_lan, window)

    @app.get("/api/paths/device/{device_id}")
    async def get_paths_device(device_id: str, window: str = "24h"):
        return await asyncio.to_thread(state.core.paths_device, device_id[:80], window)

    @app.get("/api/paths/country/{cc}")
    async def get_paths_country(cc: str, window: str = "24h"):
        try:
            return await asyncio.to_thread(state.core.paths_country, cc[:8], window)
        except KeyError:
            return err("paths.country.invalid", 404)

    @app.post("/api/paths/detail/{action}")
    async def post_paths_detail(action: str, request: Request):
        note(request, detail={"action": action[:20]})
        try:
            return await asyncio.to_thread(state.core.paths_detail_action, action)
        except ValueError as exc:
            return err(str(exc))

    @app.get("/api/paths/routes")
    async def get_paths_routes():
        return await asyncio.to_thread(state.core.paths_routes)

    @app.post("/api/paths/routes/measure")
    async def post_paths_routes_measure(request: Request):
        try:
            return await asyncio.to_thread(state.core.paths_routes_action, "measure")
        except ValueError as exc:
            return err(str(exc))

    @app.post("/api/paths/routes/clear")
    async def post_paths_routes_clear(request: Request):
        return await asyncio.to_thread(state.core.paths_routes_action, "clear")

    # real cables (DW3): the bundled community-mapped cable file (ODbL) or an administrator's import; never fetched from the network
    @app.get("/api/paths/cables")
    async def get_paths_cables(request: Request):
        blob, etag = await asyncio.to_thread(state.core.cables.blob)
        if not blob:
            return err("paths.cables.err.missing", 404)
        head = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Accept-Encoding"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=head)
        if "gzip" in (request.headers.get("accept-encoding") or "").lower():
            return Response(blob, media_type="application/json", headers=dict(head, **{"Content-Encoding": "gzip"}))
        import gzip as _gzip
        return Response(await asyncio.to_thread(_gzip.decompress, blob), media_type="application/json", headers=head)

    @app.get("/api/paths/cables/info")
    async def get_paths_cables_info():
        return await asyncio.to_thread(state.core.cables.info)

    @app.post("/api/paths/cables/import")
    async def post_paths_cables_import(request: Request):
        from .cables import MAX_IMPORT_BYTES
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_IMPORT_BYTES:
            return err("paths.cables.err.too_large", 413)
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > MAX_IMPORT_BYTES:
                return err("paths.cables.err.too_large", 413)
        note(request, detail={"bytes": len(buf)})
        try:
            out = await asyncio.to_thread(state.core.paths_cables_action, "import", bytes(buf))
        except ValueError as exc:
            return err(str(exc))
        note(request, detail={"bytes": len(buf), "sea": out.get("sea"), "land": out.get("land")})
        return out

    @app.post("/api/paths/cables/reset")
    async def post_paths_cables_reset(request: Request):
        return await asyncio.to_thread(state.core.paths_cables_action, "reset")

    @app.get("/api/netflow")
    async def get_netflow():
        return state.core.netflow_status()

    # ------------------------------------------------------------------ MQTT output (opt-in sensors for a hub, lanloom/mqtt_out.py)
    @app.get("/api/mqtt-out")
    async def mqtt_out_view():
        return await asyncio.to_thread(state.mqtt_out.view)

    @app.put("/api/mqtt-out")
    async def mqtt_out_update(request: Request):
        data = await body(request)
        keys = sorted(k for k in data if k in mqtt_out_mod.DEFAULTS or k == "password")
        note(request, target=",".join(keys),
             detail={k: data[k] for k in keys if k in ("enabled", "broker", "tls", "allow_self_signed", "node_id")})
        try:
            return state.mqtt_out.update({k: data[k] for k in keys})
        except ValueError as exc:
            return err(str(exc))

    @app.post("/api/mqtt-out/test")
    async def mqtt_out_test(request: Request):
        data = await body(request)
        patch = {k: data[k] for k in data if k in mqtt_out_mod.DEFAULTS or k == "password"}
        try:
            res = await state.mqtt_out.test(patch)
        except ValueError as exc:
            return err(str(exc))
        note(request, detail={"ok": bool(res.get("ok")), "error": res.get("error") or ""})
        return res

    @app.get("/api/networks")
    async def networks():
        from . import scan
        nets = await asyncio.to_thread(state.core.networks)
        for n in nets:
            ok, key = scan.check_cidr(n.get("cidr", "")) if not cfg.demo else (True, "")
            n["allowed"], n["reason"] = ok, key
        host_mode = any(n.get("host_mode") for n in nets)
        return {"networks": nets, "host_mode": host_mode or cfg.demo}

    # ------------------------------------------------------------------ setup assistant (first start, Settings > Setup)
    @app.get("/api/setup/discover")
    async def setup_discover():
        setup_assist.mark_started(state.store)
        return await asyncio.to_thread(setup_assist.discover, state.core)

    @app.get("/api/setup/suggestions")
    async def setup_suggestions():
        from . import integrations as registry
        items = await asyncio.to_thread(setup_assist.suggestions, state.core)
        fams = {f.family: f for f in registry.families(selectable_only=True)}
        for s in items:                      # the dialog form of a family comes from the registry (same as /api/integrations)
            if s["family"] in fams and state.cfg.demo:
                s["sample"] = "hypervisor" if s["guide"] == "hv" else True
        return {"suggestions": items, "guides": setup_assist.GUIDE_STEPS,
                "timing": setup_assist.timing(state.store, state.core.scan_state)}

    @app.get("/api/setup/timing")
    async def setup_timing():
        return setup_assist.timing(state.store, state.core.scan_state)

    @app.post("/api/setup/demo")
    async def setup_demo(request: Request):
        data = await body(request)
        on = data.get("on") is True and not cfg.demo
        note(request, detail={"on": on})
        resp = JSONResponse({"ok": True, "demo_view": on})
        if on:
            await state.demo_view.ensure()
            resp.set_cookie(demoview.COOKIE, "1", httponly=True, samesite="strict", secure=secure_cookie(request), path="/")
        else:
            resp.delete_cookie(demoview.COOKIE, path="/")
        return resp

    @app.get("/api/scan")
    async def scan_status():
        # "last scan" is the last finished one: a running, stopped or failed scan does not replace it (its state is in "current")
        last = state.core.store.one("SELECT * FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1")
        return {"current": state.core.scan_state, "last": last}

    @app.post("/api/scan")
    async def scan_start(request: Request):
        data = await body(request)
        cidrs = [str(c) for c in (data.get("cidrs") or []) if isinstance(c, str)][:8]
        note(request, detail={"networks": cidrs, "deep": bool(data.get("deep"))})
        try:
            return state.core.start_scan(cidrs, deep=bool(data.get("deep")), consent=bool(data.get("consent")),
                                         snmp_public=data.get("snmp_public") is True)
        except ValueError as exc:
            return err(str(exc) or "scan.err.invalid")

    @app.delete("/api/scan")
    async def scan_cancel():
        return {"ok": state.core.cancel_scan()}

    # ------------------------------------------------------------------ inventory and map
    @app.get("/api/devices")
    async def devices():
        tags = state.core.net_tags()
        return {"devices": [dict(d, tags=tags.get(d["id"])) for d in state.core.devices()]}

    @app.get("/api/overview")
    async def overview():
        """Start page: key figures, health with a short look back, noteworthy issues, new/unsure devices, source freshness."""
        return state.core.overview.summary()

    @app.get("/api/overview/now")
    async def overview_now():
        """The "Right now" rail: active conversations, recent changes, new devices, noteworthy issues."""
        return state.core.overview.right_now()

    @app.get("/api/search")
    async def search(q: str = "", limit: int = palette.LIMIT_GROUP):
        """Command palette: devices, guests, services, automations and hub entities that match (read only)."""
        return await asyncio.to_thread(palette.search, state.core, q[:palette.MAX_QUERY], limit)

    @app.patch("/api/devices/{device_id}")
    async def patch_device(device_id: str, request: Request):
        data = await body(request)
        note(request, detail={"fields": sorted(str(k) for k in data)[:12]})
        try:
            dev = state.core.patch_device(device_id, data)
        except ValueError as exc:
            return err(str(exc))
        return dev if dev else err("device.err.missing", 404)

    @app.post("/api/devices/{device_id}/identity")
    async def device_identity(device_id: str, request: Request):
        data = await body(request)
        action, other = data.get("action"), data.get("other")
        note(request, target="%s,%s" % (device_id, str(other)[:40]), detail={"action": str(action)[:8]})
        try:
            return state.core.set_identity(device_id, action, other)
        except ValueError as exc:
            return err(str(exc), 404 if str(exc) == "device.err.missing" else 400)

    @app.post("/api/devices/{device_id}/protect")
    async def device_protect(device_id: str, request: Request):
        """The protection class of one device (see scan/protect.py): printer, industrial, passive, none, or null to
        go back to the automatic class."""
        data = await body(request)
        mark = data.get("mark")
        note(request, target=device_id, detail={"mark": str(mark)[:12]})
        try:
            return state.core.set_protect(device_id, mark)
        except ValueError as exc:
            return err(str(exc), 404 if str(exc) == "device.err.missing" else 400)

    @app.get("/api/protect")
    async def get_protect():
        return {"devices": await asyncio.to_thread(state.core.protect_list)}

    @app.get("/api/zones")
    async def get_zones():
        return await asyncio.to_thread(state.core.zone_list)

    @app.post("/api/zones/consent")
    async def zone_consent(request: Request):
        data = await body(request)
        cidr, on = str(data.get("cidr") or "")[:64], data.get("on") is True
        note(request, target=cidr, detail={"on": on})
        try:
            return await asyncio.to_thread(state.core.set_zone_consent, cidr, on)
        except ValueError as exc:
            return err(str(exc))

    @app.get("/api/integrate")
    async def get_integrate():
        """What can be connected to which device (the catalog's suggestions, best first). Read only."""
        items = await asyncio.to_thread(catalog.suggestions, state.core)
        return {"items": items, "counts": catalog.counts(items), "classes": catalog.public()}

    @app.get("/api/devices/{device_id}/integrate")
    async def get_device_integrate(device_id: str):
        items = await asyncio.to_thread(catalog.for_device, state.core, device_id)
        return {"items": items}

    @app.get("/api/topology")
    async def get_topology():
        topo = state.core.topology()
        return {k: topo[k] for k in ("links", "nodes", "children", "trunks", "unknown", "infra")}

    @app.delete("/api/devices/{device_id}")
    async def delete_device(device_id: str):
        return {"ok": state.core.forget_device(device_id)}

    @app.get("/api/map")
    async def get_map(view: str = "overview"):
        if view not in ("overview", "network"):
            view = "overview"
        return mapmodel.cached_build(state.core, view)      # fresh after every change; a detail card right after reuses it

    @app.get("/api/component/{comp_id:path}")
    async def component(comp_id: str):
        detail = mapmodel.component_detail(state.core, comp_id)
        return detail if detail else err("component.err.missing", 404)

    # ------------------------------------------------------------------ integrations
    @app.get("/api/integrations")
    async def integrations():
        from . import integrations as registry
        fams = registry.public_list()
        if state.cfg.demo:                 # demo data is a sample network: no product names on screen
            fams = [dict(f, label="Sample hypervisor (local API)", sample="hypervisor") if "guests" in f["capabilities"]["read"]
                    else dict(f, label="Sample controller (local API)", sample=True) for f in fams]
        return {"integrations": state.core.integrations(), "families": fams, "api_level": registry.INTEGRATION_API}

    def integ_note(data: dict) -> dict:
        """Kind, name and the names of changed fields; values of the secret fields never."""
        cfg_in = data.get("config") if isinstance(data.get("config"), dict) else {}
        sec_in = data.get("secrets") if isinstance(data.get("secrets"), dict) else {}
        return {"kind": str(data.get("kind") or "")[:24], "name": str(data.get("name") or "")[:60],
                "fields": sorted(str(k) for k in cfg_in)[:20], "hidden_fields": sorted(str(k) for k, v in sec_in.items() if v)[:20]}

    @app.post("/api/integrations/test")
    async def integration_test(request: Request):
        data = await body(request)
        note(request, target=str(data.get("kind") or "")[:24], detail=integ_note(data))
        try:
            res = await state.core.test_integration(str(data.get("kind") or ""), data.get("config") or {}, data.get("secrets") or {},
                                                    existing_id=data.get("id") if isinstance(data.get("id"), int) else None)
            if isinstance(res, dict):            # what to look at: rejected credentials, certificate, not reachable, other
                res["category"] = "ok" if res.get("ok") else setup_assist.error_category(res.get("key"))
            return res
        except ValueError as exc:
            return err(str(exc))

    @app.post("/api/integrations")
    async def integration_save(request: Request):
        data = await body(request)
        note(request, target=str(data.get("name") or data.get("kind") or "")[:60], detail=integ_note(data))
        try:
            return state.core.save_integration(str(data.get("kind") or ""), str(data.get("name") or ""), data.get("config") or {},
                                               data.get("secrets") or {},
                                               existing_id=data.get("id") if isinstance(data.get("id"), int) else None)
        except ValueError as exc:
            return err(str(exc))

    @app.delete("/api/integrations/{integ_id}")
    async def integration_delete(integ_id: int):
        return {"ok": state.core.delete_integration(integ_id)}

    # ------------------------------------------------------------------ hub bridge (read only)
    @app.get("/api/hub/{integ_id}/bridge")
    async def hub_bridge(integ_id: int):
        view = state.core.hub_bridge(integ_id)
        return view if view is not None else err("hub.err.missing", 404)

    @app.get("/api/hub/{integ_id}/automations")
    async def hub_automations(integ_id: int):
        items = state.core.hub_automations(integ_id)
        return {"automations": items} if items is not None else err("hub.err.missing", 404)

    @app.get("/api/hub/{integ_id}/traces/{entity_id}")
    async def hub_traces(integ_id: int, entity_id: str):
        return {"traces": state.core.hub_traces(integ_id, entity_id)}

    @app.post("/api/hub/{integ_id}/traces/{entity_id}")
    async def hub_traces_request(integ_id: int, entity_id: str, request: Request):
        note(request, target=entity_id[:120])
        try:
            return state.core.hub_want_traces(integ_id, entity_id)
        except ValueError as exc:
            return err(str(exc), 404 if str(exc) == "hub.err.missing" else 409)

    @app.get("/api/hub/{integ_id}/sensors")
    async def hub_sensors(integ_id: int):
        view = state.core.hub_sensors(integ_id)
        return view if view is not None else err("hub.err.missing", 404)

    @app.get("/api/hub/{integ_id}/entity/{entity_id}")
    async def hub_entity(integ_id: int, entity_id: str, range: str = "1h"):
        try:
            return await state.core.hub_history(integ_id, entity_id, range)
        except ValueError as exc:
            key = str(exc)
            return err(key, {"hub.err.missing": 404, "hub.err.entity": 404, "hub.err.busy": 429}.get(key, 409))

    # ------------------------------------------------------------------ Automations tab (read only)
    @app.get("/api/hub/{integ_id}/auto")
    async def hub_auto_tab(integ_id: int):
        view = state.core.hub_auto(integ_id, "tab")
        return view if view is not None else err("hub.err.missing", 404)

    @app.get("/api/hub/{integ_id}/auto/item/{entity_id}")
    async def hub_auto_item(integ_id: int, entity_id: str):
        view = state.core.hub_auto(integ_id, "item", entity_id)
        return view if view is not None else err("auto.err.item", 404)

    @app.get("/api/hub/{integ_id}/auto/board/{entity_id}")
    async def hub_auto_board(integ_id: int, entity_id: str):
        view = state.core.hub_auto(integ_id, "board", entity_id)
        return view if view is not None else err("hub.err.entity", 404)

    def _auto_part(part):
        async def handler(integ_id: int):
            view = state.core.hub_auto(integ_id, part)
            return view if view is not None else err("hub.err.missing", 404)
        handler.__name__ = "hub_auto_" + part
        return handler

    for _part in ("who", "timeline", "plan", "rooms", "health"):
        app.get("/api/hub/{integ_id}/auto/" + _part)(_auto_part(_part))

    @app.post("/api/hub/{integ_id}/match")
    async def hub_match(integ_id: int, request: Request):
        data = await body(request)
        net_id, hub_dev, action = data.get("device"), data.get("hub_device"), data.get("action")
        if not isinstance(hub_dev, str) or not isinstance(net_id, str):
            return err("hub.match.invalid")
        key = "%d:%s" % (integ_id, hub_dev[:64])
        note(request, target=("%s %s" % (net_id[:64], key))[:160], detail={"action": action if action in ("split", "join", "reset") else "?"})
        try:
            return state.core.set_hub_match(net_id, key, action)
        except ValueError as exc:
            return err(str(exc), 404 if str(exc) == "device.err.missing" else 400)

    # ------------------------------------------------------------------ nightly reports (of the request's site)
    from .sites_report import filter_report

    def for_reader(request: Request, rep):
        """A report as this reader may see it: lines about sites the person may not see are taken out (M1)."""
        return filter_report(rep, state.sites.allowed(getattr(request.state, "user", None))) if rep else rep

    @app.get("/api/reports")
    async def reports_list():
        return state.core.reports.listing()

    @app.get("/api/reports/preview")
    async def reports_preview(request: Request):
        return for_reader(request, await state.core.reports.preview())

    @app.get("/api/reports/settings")
    async def reports_settings():
        return state.core.reports.public_settings()

    @app.put("/api/reports/settings")
    async def reports_settings_put(request: Request):
        data = await body(request)
        patch = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        secrets = data.get("secrets") if isinstance(data.get("secrets"), dict) else {}
        note(request, detail={"fields": sorted(str(k) for k in patch)[:30], "hidden_fields": sorted(str(k) for k, v in secrets.items() if v)[:20]})
        try:
            return state.core.reports.update_settings(patch, secrets)
        except ValueError as exc:
            return err(str(exc))

    @app.post("/api/reports/tz")
    async def reports_tz(request: Request):
        data = await body(request)
        state.core.reports.set_auto_tz(str(data.get("tz") or "")[:64])
        return {"ok": True}

    @app.post("/api/reports/test")
    async def reports_test(request: Request):
        data = await body(request)
        try:
            return await state.core.reports.test_channel(str(data.get("channel") or ""))
        except ValueError as exc:
            return err(str(exc))

    @app.get("/api/reports/{day}")
    async def report_get(day: str, request: Request):
        rep = state.core.reports.get(day)
        return for_reader(request, rep) if rep else err("report.err.missing", 404)

    @app.post("/api/reports/{day}/read")
    async def report_read(day: str, request: Request):
        data = await body(request)
        reports = state.core.reports
        return {"ok": reports.mark_read(day, data.get("read", True) is not False), "unread": reports.unread()}

    @app.put("/api/reports/{day}/items")
    async def report_item(day: str, request: Request):
        data = await body(request)
        try:
            rep = state.core.reports.mark_item(day, str(data.get("item") or "")[:200], bool(data.get("done")))
        except ValueError as exc:
            return err(str(exc))
        return rep if rep else err("report.err.missing", 404)

    # ------------------------------------------------------------------ AI (lanloom/ai): explain, providers, local setup
    ai = state.ai

    def ai_err(exc: Exception, status: int = 400) -> JSONResponse:
        key = str(exc)
        return err(key if key.startswith("ai.") else "ai.err.internal", status)

    @app.get("/api/ai")
    async def ai_get():
        return await asyncio.to_thread(ai.public_settings)

    @app.put("/api/ai")
    async def ai_put(request: Request):
        data = await body(request)
        sets = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        secs = data.get("secrets") if isinstance(data.get("secrets"), dict) else {}
        note(request, detail={"fields": sorted(str(k) for k in sets)[:30], "hidden_fields": sorted(str(k) for k, v in secs.items() if v)[:20]})
        try:
            return await asyncio.to_thread(ai.update_settings, data.get("settings") if isinstance(data.get("settings"), dict) else {},
                                           data.get("secrets") if isinstance(data.get("secrets"), dict) else {})
        except ValueError as exc:
            return ai_err(exc)

    @app.post("/api/ai/models")
    async def ai_models(request: Request):
        data = await body(request)
        try:
            return await asyncio.to_thread(ai.models, str(data.get("scope") or ""), data)
        except ValueError as exc:
            return ai_err(exc)

    @app.post("/api/ai/test")
    async def ai_test(request: Request):
        data = await body(request)
        try:
            return await asyncio.to_thread(ai.test, str(data.get("scope") or ""), data)
        except ValueError as exc:
            return ai_err(exc)
        except Exception as exc:
            from .ai.providers import AIError
            if isinstance(exc, AIError):
                return err(exc.key)
            raise

    @app.get("/api/ai/jobs/{job_id}")
    async def ai_job(job_id: str):
        job = ai.job_view(job_id)
        return job if job and job["kind"] != "pair" else err("ai.err.job", 404)

    @app.post("/api/ai/explain")
    async def ai_explain(request: Request):
        data = await body(request)
        subject, ref, lang = str(data.get("subject") or ""), data.get("ref") if isinstance(data.get("ref"), dict) else {}, data.get("lang")
        try:
            if data.get("preview"):
                return await asyncio.to_thread(ai.preview, subject, ref, lang)
            return await asyncio.to_thread(ai.explain, subject, ref, lang)
        except ValueError as exc:
            return ai_err(exc)

    # KI2: ask, plain-words paragraph, identification help, firewall suggestions (all read-only)
    async def ai_call(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except ValueError as exc:
            return ai_err(exc)

    @app.post("/api/ai/ask")
    async def ai_ask(request: Request):
        data = await body(request)
        hist = data.get("history") if isinstance(data.get("history"), list) else []
        note(request, detail={"turns": min(len(hist), 99), "preview": bool(data.get("preview"))})   # never the question itself
        return await ai_call(ai.ask, data.get("question"), hist, data.get("lang"), bool(data.get("preview")))

    @app.post("/api/ai/report")
    async def ai_report(request: Request):
        data = await body(request)
        note(request, detail={"day": str(data.get("day") or "")[:10], "preview": bool(data.get("preview"))})
        return await ai_call(ai.report_plain, str(data.get("day") or ""), data.get("lang"), bool(data.get("preview")))

    @app.post("/api/ai/identify")
    async def ai_identify(request: Request):
        data = await body(request)
        note(request, detail={"device": str(data.get("id") or "")[:80], "preview": bool(data.get("preview"))})
        return await ai_call(ai.identify, str(data.get("id") or ""), data.get("lang"), bool(data.get("preview")))

    @app.get("/api/ai/hints")
    async def ai_hints():
        return {"hints": await asyncio.to_thread(ai.hints_view)}

    @app.post("/api/ai/hints/{device_id}")
    async def ai_hint(device_id: str, request: Request):
        data = await body(request)
        action = str(data.get("action") or "")
        note(request, detail={"device": device_id[:80], "action": action[:12]})
        return await ai_call(ai.hint_decide, device_id, action)

    @app.post("/api/ai/firewall")
    async def ai_firewall(request: Request):
        data = await body(request)
        note(request, detail={"preview": bool(data.get("preview"))})
        return await ai_call(ai.firewall_suggest, data.get("lang"), bool(data.get("preview")))

    @app.get("/api/ai/local")
    async def ai_local():
        return {"setup": ai.setup_state(), "sidecar": ai._sidecar_alive(), "suggestions": await asyncio.to_thread(ai.suggestions),
                "discovered": ai._discovered}

    @app.post("/api/ai/local/discover")
    async def ai_discover(request: Request):
        data = await body(request)
        return await asyncio.to_thread(ai.discover, data.get("ids") if isinstance(data.get("ids"), list) else None)

    @app.post("/api/ai/local/install")
    async def ai_install():
        try:
            return await asyncio.to_thread(ai.install_local)
        except ValueError as exc:
            return ai_err(exc)

    @app.post("/api/ai/local/use")
    async def ai_use(request: Request):
        data = await body(request)
        try:
            return await asyncio.to_thread(ai.use_existing, str(data.get("url") or ""), str(data.get("model") or ""))
        except ValueError as exc:
            return ai_err(exc)

    @app.post("/api/ai/local/remove")
    async def ai_remove():
        try:
            return await asyncio.to_thread(ai.remove_local)
        except ValueError as exc:
            return ai_err(exc)

    @app.post("/api/ai/pairing")
    async def ai_pairing():
        return ai.new_pairing()

    def pair_guard(request: Request) -> Optional[JSONResponse]:
        wait = state.limiter.wait_seconds("ai-pair:" + client_of(request))
        if wait > 0:
            return JSONResponse({"error": "auth.err.rate", "wait": int(wait) + 1}, status_code=429)
        return None

    def pair_refused(request: Request) -> JSONResponse:
        state.limiter.failure("ai-pair:" + client_of(request))
        return err("ai.err.code", 403)

    @app.get("/api/ai/installer/{os_name}")
    async def ai_installer(os_name: str, request: Request, code: str = ""):
        blocked = pair_guard(request)
        if blocked:
            return blocked
        try:
            name, text = ai.installer(os_name, code, request.headers.get("host", ""), request.url.scheme,
                                      request.scope.get("root_path", "") or getattr(cfg, "base_path", "") or "")
        except PermissionError:
            return pair_refused(request)
        except ValueError as exc:
            return ai_err(exc)
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % name})

    @app.post("/api/ai/pair/hello")
    async def ai_pair_hello(request: Request):
        blocked = pair_guard(request)
        if blocked:
            return blocked
        data = await body(request)
        try:
            return ai.pair_hello(data.get("code"), data.get("hw") if isinstance(data.get("hw"), dict) else {})
        except PermissionError:
            return pair_refused(request)

    @app.post("/api/ai/pair")
    async def ai_pair(request: Request):
        blocked = pair_guard(request)
        if blocked:
            return blocked
        data = await body(request)
        try:
            return ai.pair(data.get("code"), requester_ip(request), data)
        except PermissionError:
            return pair_refused(request)
        except ValueError as exc:
            return ai_err(exc)

    @app.post("/api/ai/pair/status")
    async def ai_pair_status(request: Request):
        blocked = pair_guard(request)
        if blocked:
            return blocked
        data = await body(request)
        st = ai.pair_status(data.get("code"), str(data.get("job") or ""))
        return st if st else pair_refused(request)

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
        note(request, target=str(data.get("version") or "")[:20])
        try:
            return state.updates.request(str(data.get("version") or ""))
        except ValueError as exc:
            return err(str(exc))

    @app.post("/api/update/rollback")
    async def update_rollback(request: Request):
        data = await body(request)
        note(request, target=str(data.get("version") or "")[:20], detail={"from": __version__})
        try:
            return state.updates.rollback(str(data.get("version") or ""))
        except ValueError as exc:
            return err(str(exc))

    @app.put("/api/update/settings")
    async def update_settings(request: Request):
        data = await body(request)
        patch = {k: data[k] for k in ("channel", "hold") if k in data}
        note(request, target=",".join(sorted(patch)), detail={k: v for k, v in patch.items() if isinstance(v, (str, bool))})
        try:
            return state.updates.settings(patch)
        except ValueError as exc:
            return err(str(exc))

    # ------------------------------------------------------------------ what's new (changelog in the image)
    def seen_of(request: Request) -> Optional[str]:
        user = getattr(request.state, "user", None) or {}
        if not user.get("id"):
            return None
        seen = state.accounts.prefs(user["id"]).get("whatsnew_seen")
        if not isinstance(seen, str) or not updates_mod.parse_version(seen):
            # first look: start at the running version, so only later versions carry the dot
            state.accounts.set_prefs(user["id"], {"whatsnew_seen": __version__})
            seen = __version__
        return seen

    @app.get("/api/whatsnew")
    async def whatsnew_view(request: Request):
        try:
            limit = int(request.query_params.get("limit") or whatsnew.DEFAULT_LIMIT)
        except ValueError:
            limit = whatsnew.DEFAULT_LIMIT
        return whatsnew.view(seen_of(request), limit)

    @app.post("/api/whatsnew/seen")
    async def whatsnew_seen(request: Request):
        data = await body(request)
        user = getattr(request.state, "user", None) or {}
        v = str(data.get("version") or "")
        if not updates_mod.parse_version(v):
            return err("update.err.invalid_version")
        if updates_mod.is_older(__version__, v):
            v = __version__                      # never ahead of what runs
        seen = seen_of(request)
        if user.get("id") and whatsnew.newer(v, seen):
            state.accounts.set_prefs(user["id"], {"whatsnew_seen": v})
            seen = v
        return {"seen": seen, "unseen": whatsnew.view(seen, 1)["unseen"]}

    # ------------------------------------------------------------------ firewall editor
    from .firewall.model import Refused

    def refused(exc: Refused, status: int = 409) -> JSONResponse:
        return JSONResponse({"error": exc.key, "vars": exc.vars}, status_code=status)

    @app.get("/api/firewall")
    async def fw_view(request: Request):
        src = str(request.query_params.get("source") or "")[:24] or None
        return state.firewall.view(request.state.user, requester_ip(request), src)

    def fw_live_state(source):
        """(key, name, neutral state, age) of the live source a read-only question is about: the one asked for, else the
        first that has rules."""
        live = state.firewall.live_sources()
        pick = next((e for e in live if e["key"] == source), None) or next((e for e in live if (e["state"] or {}).get("policies")), None)
        return pick

    @app.get("/api/firewall/insight")
    async def fw_insight(request: Request):
        """Read only: the rules in plain words, checks with a suggestion, blocked today, hits of the last hour, the board."""
        from .firewall import insight
        entry = fw_live_state(str(request.query_params.get("source") or "")[:24] or None)
        if entry is None:
            return {"source": None}
        st = entry["state"]
        pols = st.get("policies") or []
        hits = state.core.hitlog.summary(entry["key"], pols)
        return {"source": entry["key"], "name": entry["name"], "age_s": entry.get("age_s"),
                "findings": insight.findings(st), "plain": {str(p["id"]): insight.plain(st, p) for p in pols},
                "hits": {"blocked_today": hits["blocked_today"], "top": hits["top"], "since": hits["day_since"],
                         "hour_ok": hits["hour_ok"], "hour": hits["hour"], "day": hits["day"]},
                "board": insight.board(st, hits["hour"]), "ids": st.get("ids")}

    @app.get("/api/firewall/ask")
    async def fw_ask(request: Request):
        """"May A reach B?" from the read rules. Nothing is sent anywhere."""
        from .firewall import insight
        q = request.query_params
        args = (str(q.get("from") or "")[:80], str(q.get("to") or "")[:80], str(q.get("proto") or "")[:8] or None,
                str(q.get("port") or "")[:6] or None)
        source = str(q.get("source") or "")[:24] or None
        if source == "editor":       # the firewall editor (simulation or executor): the answer now and after the draft
            from .firewall import zonefw
            st = state.firewall.state()
            if st is None or not st.get("zones"):
                return JSONResponse({"error": "fw.ask.no_rules"}, status_code=404)
            res = insight.ask(st, *args)
            if res.get("error"):
                return JSONResponse({"error": "fw.ask.err." + res["error"]}, status_code=400)
            steps = state.firewall.draft_steps(request.state.user)
            if steps:
                after = insight.ask(zonefw.after_state(st, steps), *args)
                res["after"] = None if after.get("error") else {k: after.get(k) for k in ("verdict", "verdict_label", "reason", "rule", "certain")}
            res["source"] = "editor"
            return res
        entry = fw_live_state(source)
        if entry is None:
            return JSONResponse({"error": "fw.ask.no_rules"}, status_code=404)
        res = insight.ask(entry["state"], *args)
        if res.get("error"):
            return JSONResponse({"error": "fw.ask.err." + res["error"]}, status_code=400)
        res["source"] = entry["key"]
        return res

    @app.get("/api/ports")
    async def ports_view(request: Request):
        """Faceplates of the switches and access points a controller adapter reads (read only)."""
        return {"fronts": state.core.port_fronts(), "moves": state.core.roamwatch.total()}

    @app.put("/api/firewall/draft")
    async def fw_draft(request: Request):
        data = await body(request)
        moves = data.get("moves") if isinstance(data.get("moves"), dict) else {}
        edits = data.get("edits") if isinstance(data.get("edits"), dict) else None
        ops = data.get("ops") if isinstance(data.get("ops"), (dict, list)) else None
        detail = {"moves": len(moves), "edits": len(edits or {})}
        if ops is not None:
            detail["guest_changes"] = len(ops)
        note(request, detail=detail)
        try:
            return state.firewall.set_draft(request.state.user, moves, edits=edits, ops=ops)
        except Refused as exc:
            return refused(exc)

    @app.delete("/api/firewall/draft")
    async def fw_draft_clear(request: Request):
        state.firewall.clear_draft(request.state.user)
        return {"ok": True}

    @app.post("/api/firewall/preview")
    async def fw_preview(request: Request):
        try:
            return state.firewall.preview(request.state.user, requester_ip(request))
        except Refused as exc:
            return refused(exc)

    @app.post("/api/firewall/plan")
    async def fw_plan(request: Request):
        data = await body(request)
        confirmed = data.get("confirmed") if isinstance(data.get("confirmed"), list) else []
        try:
            return state.firewall.make_plan(request.state.user, requester_ip(request), confirmed, rp_of(request)[0])
        except Refused as exc:
            return refused(exc)

    @app.post("/api/firewall/approve")
    async def fw_approve(request: Request):
        data = await body(request)
        rp_id, origin = rp_of(request)
        note(request, target=str(data.get("plan") or "")[:40])
        try:
            return state.firewall.approve(request.state.user, str(data.get("plan") or ""), data.get("assertion"), rp_id, origin)
        except Refused as exc:
            return refused(exc)

    @app.post("/api/firewall/undo")
    async def fw_undo(request: Request):
        data = await body(request)
        try:
            return state.firewall.undo(request.state.user, str(data.get("plan") or ""))
        except Refused as exc:
            return refused(exc)

    @app.post("/api/firewall/reset")
    async def fw_reset():
        try:
            state.firewall.reset_simulation()
        except Refused as exc:
            return refused(exc)
        return {"ok": True}

    @app.put("/api/firewall/segplan")
    async def fw_segplan(request: Request):
        data = await body(request)
        items = data.get("targets") if isinstance(data.get("targets"), dict) else {}
        note(request, detail={"devices": len(items), "removed": sum(1 for v in items.values() if v is None)})
        try:
            return state.firewall.set_segplan(items)
        except ValueError as exc:
            return err(str(exc))

    @app.get("/api/firewall/keys")
    async def fw_keys(request: Request):
        rp_id, origin = rp_of(request)
        keys = [{k: v for k, v in key.items() if k != "name"} for key in state.firewall.user_keys(request.state.user, rp_id)]
        return {"keys": keys, "origin": origin, "names": [k["name"] for k in state.firewall.user_keys(request.state.user, rp_id)]}

    @app.get("/api/executor/plans")
    async def ex_plans(target: str = "controller"):
        return {"plans": state.firewall.executor_take_plans(target)}

    @app.post("/api/executor/state")
    async def ex_state(request: Request):
        try:
            state.firewall.executor_push_state(await body(request))
        except Refused as exc:
            return refused(exc, 400)
        from .firewall.steps import PLAN_VERSION_MAX
        from .integrations import INTEGRATION_API
        return {"ok": True, "app": {"version": __version__, "plan_version_max": PLAN_VERSION_MAX, "api_level": INTEGRATION_API}}

    @app.post("/api/executor/report")
    async def ex_report(request: Request):
        state.firewall.on_progress(await body(request))
        return {"ok": True}

    # ------------------------------------------------------------------ host agents and container sources (E4)
    def agent_guard(request: Request) -> Optional[JSONResponse]:
        wait = state.limiter.wait_seconds("agent-pair:" + client_of(request))
        if wait > 0:
            return JSONResponse({"error": "auth.err.rate", "wait": int(wait) + 1}, status_code=429)
        return None

    @app.get("/api/agents")
    async def agents_list():
        return {"agents": await asyncio.to_thread(state.core.agents.entries), "server_version": __version__,
                "image": "%s:%s" % (agents_mod.IMAGE, __version__)}

    @app.post("/api/agents/invite")
    async def agents_invite(request: Request):
        data = await body(request)
        out = state.core.agents.invite(str(data.get("name") or ""))
        note(request, target=out["code"][:2] + "…")
        return out

    @app.post("/api/agents/proxy")
    async def agents_proxy(request: Request):
        data = await body(request)
        try:
            out = await asyncio.to_thread(state.core.agents.add_proxy, str(data.get("name") or ""), str(data.get("url") or ""))
        except ValueError as exc:
            return err(str(exc))
        note(request, target=out["id"])
        return out

    @app.post("/api/agents/proxy/test")
    async def agents_proxy_test(request: Request):
        data = await body(request)
        if state.core.demo:
            return {"ok": True, "runtime": "docker", "version": "27.3.1", "containers": 9}
        return await asyncio.to_thread(state.core.agents.test_proxy, str(data.get("url") or ""))

    @app.patch("/api/agents/{agent_id}")
    async def agents_rename(agent_id: str, request: Request):
        data = await body(request)
        try:
            ok = state.core.agents.rename(agent_id, str(data.get("name") or ""))
        except ValueError as exc:
            return err(str(exc))
        return {"ok": True} if ok else err("agent.err.unknown", 404)

    @app.delete("/api/agents/{agent_id}")
    async def agents_revoke(agent_id: str):
        ok = await asyncio.to_thread(state.core.agents.revoke, agent_id)
        return {"ok": True} if ok else err("agent.err.unknown", 404)

    @app.get("/api/agents/{agent_id}/health")
    async def agents_health(agent_id: str, hours: int = 24):
        return await asyncio.to_thread(state.core.agents.health, agent_id, hours)

    @app.get("/api/running")
    async def running_view():
        return await asyncio.to_thread(state.core.agents.running)

    @app.post("/api/agent/pair")
    async def agent_pair(request: Request):
        blocked = agent_guard(request)
        if blocked:
            return blocked
        data = await body(request)
        for site_id, site_core in list(state.sites.cores.items()):     # the agent joins the site its code was made in
            if site_core.agents.has_invite(data.get("code")):
                sites_mod.ACTIVE.set(site_id)
                break
        try:
            out = await asyncio.to_thread(state.core.agents.pair, data.get("code"), data, requester_ip(request))
        except PermissionError:
            state.limiter.failure("agent-pair:" + client_of(request))
            note(request, target="")
            return err("agent.err.code", 403)
        except ValueError as exc:
            return err(str(exc), 409 if str(exc) == "agent.err.proto" else 400)
        note(request, target=out["agent_id"])
        return out

    @app.post("/api/agent/report")
    async def agent_report(request: Request):
        aid = getattr(request.state, "agent", None)
        if not aid:
            return err("auth.err.required", 401)
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        if size > agents_mod.MAX_BODY:
            return err("agent.err.too_large", 413)
        data = await body(request)
        try:
            return await asyncio.to_thread(state.core.agents.ingest, aid, data)
        except ValueError as exc:
            return err(str(exc), 409 if str(exc) == "agent.err.proto" else 400)

    # ------------------------------------------------------------------ live
    @app.websocket("/api/live")
    async def live_feed(ws: WebSocket):
        if not origin_ok(ws):
            await ws.close(code=4403)
            return
        user = identify(ws)
        if not user or user.get("stage") == "enroll" or not access.allows(user.get("role"), access.RULES[("WS", "/api/live")].role):
            await ws.close(code=4401)
            return
        if state.demo_view.requested(ws.cookies):
            core = await state.demo_view.ensure()
        else:
            how, asked = asked_site(ws)
            sid, unknown = state.sites.resolve(user, how, asked)
            if unknown:
                await ws.accept()                    # a close before the handshake reaches the browser as a plain 1006, never as a code
                await ws.close(code=sites_mod.WS_CHANGED)   # M2/M1T: the page named a site that is not (or no longer) its own
                return
            core = state.sites.core(sid) if sid else None
            if core is None:
                await ws.close(code=4403)              # no site released for this person
                return
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue()
        core.clients.add(queue)
        try:
            if core.last_frame:
                await ws.send_text(json.dumps(core.last_frame, separators=(",", ":")))
            while True:
                text = await queue.get()
                await ws.send_text(text)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            core.clients.discard(queue)

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

    base = getattr(cfg, "base_path", "") or ""
    if base:
        app.add_middleware(BasePath, base=base)
    if getattr(cfg, "addon", False) and not getattr(cfg, "addon_lan", False):
        app.add_middleware(PeerGate, allowed=proxies)
    return app


class PeerGate:
    """Add-on mode without direct access: only the ingress proxy (and this host's own health check over loopback) is
    answered; anybody else in the network gets a short refusal that points to the hub's side panel. Covers HTTP and
    the live WebSocket, before any route or file."""

    REFUSAL = (b"Lanloom runs as an add-on here. Open it from the side panel of your home-automation hub, "
               b"or switch on direct access in the add-on options.\n")

    def __init__(self, app, allowed):
        self.app = app
        self.allowed = allowed

    def admits(self, scope) -> bool:
        client = scope.get("client") or ("", 0)
        peer = auth._ip(str(client[0] or ""))
        return peer is not None and (peer.is_loopback or auth.from_trusted_proxy(str(peer), self.allowed))

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and not self.admits(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(self.REFUSAL)).encode("ascii")),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": self.REFUSAL})
            return
        await self.app(scope, receive, send)


class BasePath:
    """Serve below LANLOOM_BASE_PATH (e.g. "/lanloom"): the prefix is removed before routing. Requests without the
    prefix keep working, so a reverse proxy may strip it or pass it on. The web page derives every address (API,
    WebSocket, worker) from its own location, so it needs no setting of its own."""

    def __init__(self, app, base: str):
        self.app = app
        self.base = base

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.base and scope["type"] == "http":
                await send({"type": "http.response.start", "status": 308,
                            "headers": [(b"location", (self.base + "/").encode("ascii")), (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
            if path.startswith(self.base + "/"):
                scope = dict(scope, path=path[len(self.base):], root_path=scope.get("root_path", "") + self.base)
                if "raw_path" in scope and scope["raw_path"]:
                    scope["raw_path"] = scope["raw_path"][len(self.base):]
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------- self-test and entry point
def selftest() -> int:
    """Offline check used by the updater before an image is trusted: no network, no writes outside memory."""
    from types import SimpleNamespace
    problems = []
    try:
        from . import classify, core, demo, live, scan, sources  # noqa: F401 - import check
        from .firewall import adapter as fw_adapter, model as fw_model
        st = fw_adapter.sample_state()
        if not fw_model.preview(st, {"d10": "n_work"})["steps"]:
            problems.append("firewall preview")
        from . import integrations as registry
        if not registry.BUILTIN or any(m.validate() is None for m in registry.BUILTIN):
            problems.append("integration registry")
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
        c.geo.start(background=False)          # bundled country data: loads, passes its samples, books and sums
        if c.geo.db is None:
            problems.append("geo data: %s" % c.geo.error)
        else:
            c.paths.book("self", "3.3.3.3", 1000, 2000)
            c.paths.book("self", "1.1.1.1", 10, 20)        # anycast: its own entry, never a country
            tt = c.paths_summary("24h")["totals"]
            if tt["attributable"] != 3000 or tt["anycast"] != 30:
                problems.append("data paths")
        from . import nightly  # noqa: F401 - import check
        rep = c.reports.assemble(0, time.time(), None, "2026-01-01", [])
        if not rep["sections"] or any(s["summary"]["key"] == "report.section_failed" for s in rep["sections"]):
            problems.append("nightly report")
        from .ai import privacy as ai_privacy, providers as ai_providers
        ps = ai_privacy.Pseudonymizer({"HOST": ["sample-host"]})
        probe_text = "sample-host 192.168.1.20 aa:bb:cc:dd:ee:01"
        sent = ps.out(probe_text)
        if ai_privacy.leaks(sent) or "sample-host" in sent or ps.back(sent) != probe_text or not ai_providers.public_list():
            problems.append("ai privacy layer")
        from . import qr, totp as _totp
        if _totp.code_at("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59) != "287082" or not qr.encode("otpauth://totp/x?secret=A"):  # secret-scan: fake (RFC 6238 vector)
            problems.append("one-time codes")
        if any(r.role not in access.ROLES + ("public", "executor", "agent") for r in access.RULES.values()):
            problems.append("access table")
        en = load_i18n("en")
        if not en:
            problems.append("i18n: no English texts")
        for lang in i18n_languages():
            other = load_i18n(lang)
            if set(other) != set(en):
                problems.append("i18n keys differ (%d en / %d %s)" % (len(en), len(other), lang))
            elif any(textfmt.placeholders(en[k]) != textfmt.placeholders(other[k]) for k in en):
                problems.append("i18n placeholders differ (%s)" % lang)
        if not whatsnew.entries():
            problems.append("CHANGELOG.md missing or unreadable (What's new?)")
        for rel in ("index.html", "js/textfmt.js", "js/app.js", "js/fresh.js", "css/fresh.css", "board/board.js", "board/router.js", "board/bundles.js", "board/endmarks.js", "board/portmarks.js", "js/firewall.js", "js/fwinsight.js", "js/hypervisor.js", "js/hub.js", "css/firewall.css", "css/fwinsight.css", "js/portfront.js", "css/portfront.css", "js/netmodes.js", "css/netmodes.css", "js/paths.js", "js/paths_country.js", "js/paths_routes.js", "css/paths_routes.css", "js/worldmap.js", "js/worldmap_fine.js", "js/worldplaces.js", "css/paths.css", "css/paths_country.css", "js/reports.js", "css/reports.css", "js/ai.js", "css/ai.css", "js/ask.js", "css/ask.css", "js/access.js", "css/access.css", "js/backup.js", "css/backup.css", "js/fuzzy.js", "js/palette.js", "css/palette.css", "js/overview.js", "js/shell.js", "css/overview.css", "js/sites.js", "css/sites.css", "js/storage.js", "css/storage.css", "js/discovery.js", "css/discovery.css", "js/inventory.js", "css/inventory.css", "js/export.js", "css/export.css", "js/history.js", "css/history.css", "js/services.js", "css/services.css", "board/tree.js", "css/tree.css", "js/a11y.js", "css/a11y.css", "js/automations.js", "css/automations.css", "js/sheet.js", "js/now.js", "css/now.css", "js/energy.js", "css/energy.css", "css/tokens.css", "css/palettes.css", "fonts/InterVariable.woff2"):
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
