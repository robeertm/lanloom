"""The public, versioned API: ``/api/v1/...``.

Every route of this part is declared once, with ``@api.op(...)``: method, path, scope, summary, parameters and the JSON
schema of the answer. The same declaration registers the FastAPI route and becomes the OpenAPI description
(``GET /api/v1/openapi.json``), the reference in Settings > API and the contract test (tests/test_api_contract.py checks
that the routes of the app, the rights table and this description name the same routes and that real answers match the
schemas). Fields are only ever added within ``v1``; renaming or removing one needs ``/api/v2``.

Who may call it: a signed-in session of the web page, or a program with an API token (``Authorization: Bearer llt_...``,
see lanloom/apitokens.py). The scope of each route is the lowest role in lanloom/access.py (read = viewer, operate =
operator, admin = administrator); a token with the scope ``ingest`` reaches only ``POST /api/v1/ingest``.
"""
import asyncio
from collections import namedtuple
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, access, datapaths, ingest as ingest_mod, sites as sites_mod, webhooks as webhooks_mod
from .nightly import render as render_mod

API_VERSION = "1"
BASE = "/api/v1"
SCOPE_OF_ROLE = {"viewer": "read", "operator": "operate", "admin": "admin"}
INGEST_ROUTE = ("POST", BASE + "/ingest")

Op = namedtuple("Op", "method path name tag summary description params body response example")


# ---------------------------------------------------------------- JSON schema helpers (OpenAPI 3.1 = JSON Schema 2020-12)
def s_str(desc: str = "", enum=None, nullable: bool = False) -> dict:
    out = {"type": ["string", "null"] if nullable else "string"}
    if enum:
        out["enum"] = list(enum) + ([None] if nullable else [])
    if desc:
        out["description"] = desc
    return out


def s_num(desc: str = "", nullable: bool = False, integer: bool = False) -> dict:
    t = "integer" if integer else "number"
    out = {"type": [t, "null"] if nullable else t}
    if desc:
        out["description"] = desc
    return out


def s_int(desc: str = "", nullable: bool = False) -> dict:
    return s_num(desc, nullable, integer=True)


def s_bool(desc: str = "") -> dict:
    return {"type": "boolean", "description": desc} if desc else {"type": "boolean"}


def s_arr(items: dict, desc: str = "") -> dict:
    out = {"type": "array", "items": items}
    if desc:
        out["description"] = desc
    return out


def s_obj(props: dict, required=None, extra: bool = False, desc: str = "", nullable: bool = False) -> dict:
    out = {"type": ["object", "null"] if nullable else "object", "properties": props,
           "required": list(props) if required is None else list(required), "additionalProperties": extra}
    if desc:
        out["description"] = desc
    return out


def s_map(values: dict, desc: str = "") -> dict:
    out = {"type": "object", "additionalProperties": values}
    if desc:
        out["description"] = desc
    return out


TS = "seconds since 1970-01-01 UTC"
DEVICE = s_obj({
    "id": s_str("stable id of the device (d-<hardware address> or d-ip-<address>)"),
    "title": s_str("name shown in Lanloom (your own name if you set one)"),
    "kind": s_str("device kind, e.g. router, switch, ap, nas, printer, phone, unknown"),
    "ip": s_str("IPv4 address, empty when only the hardware address is known"),
    "mac": s_str("hardware address", nullable=True),
    "ipv6": s_arr(s_str()),
    "vendor": s_str("manufacturer", nullable=True),
    "model": s_str(nullable=True),
    "firmware": s_str(nullable=True),
    "status": s_str("up = answers now, down = stopped answering, off = marked as often switched off and not answering "
                     "right now, unknown = not checked yet", ("up", "down", "off", "unknown")),
    "hidden": s_bool("hidden from the map by a person"),
    "gateway": s_bool("the default gateway of a scanned network"),
    "hostnames": s_arr(s_str()),
    "services": s_arr(s_str(), "short labels of open services, e.g. HTTPS, SSH, MQTT"),
    "sources": s_arr(s_str(), "where Lanloom knows the device from: scan, adapter, hub, ..."),
    "zone": s_str("network zone, when zones are set up", nullable=True),
    "merged_into": s_str("id of the device this interface belongs to", nullable=True),
    "first_seen": s_num(TS, nullable=True),
    "last_seen": s_num(TS, nullable=True),
})
ERROR = s_obj({"error": s_str("translation key of the error, e.g. api.err.token")}, extra=True)
INCIDENT = s_obj({"from": s_num(TS), "to": s_num(TS, nullable=True), "dur": s_int("seconds"), "open": s_bool()})
SHARE = lambda d: s_num("share of the time the subject answered, 0..1 (%s)" % d, nullable=True)  # noqa: E731
AVAIL_ROW = s_obj({"id": s_str("dev:<device id> or src:int:<connection id>"), "title": s_str(), "kind": s_str(),
                   "share24": SHARE("24 hours"), "share7": SHARE("7 days"), "share35": SHARE("35 days"),
                   "incidents7": s_int("outages in the last 7 days"), "mttr_s": s_int("mean time to repair in seconds", nullable=True),
                   "longest_s": s_int("longest outage of the last 7 days in seconds"), "open": s_bool("an outage is going on now"),
                   "wobbly": s_bool("several outages in the last 7 days")})
REPORT_ROW = s_obj({"day": s_str("YYYY-MM-DD"), "created": s_num(TS), "level": s_str(enum=("ok", "info", "warn", "danger")),
                    "points": s_int("items that need a look"), "open": s_int("of those, not ticked yet"), "read": s_bool()})
ITEM = s_obj({"id": s_str(), "level": s_str(enum=("ok", "info", "warn", "danger")), "point": s_bool("needs a look"),
              "done": s_num("ticked at (" + TS + ")", nullable=True), "key": s_str("translation key"), "vars": s_map({}),
              "text": s_str("the item in the requested language")}, required=("id", "level", "point", "text"), extra=True)
SECTION = s_obj({"id": s_str(), "title": s_str("section title in the requested language"), "level": s_str(enum=("ok", "info", "warn", "danger")),
                 "summary": s_str("one line in the requested language"), "items": s_arr(ITEM), "more": s_int(nullable=True)},
                required=("id", "title", "level", "summary", "items"))
INGESTED = s_obj({"id": s_int(), "t": s_num(TS), "source": s_str(), "title": s_str(), "level": s_str(enum=ingest_mod.LEVELS),
                  "summary": s_str(), "items": s_arr(s_obj({"text": s_str(), "level": s_str(enum=ingest_mod.LEVELS)})),
                  "by": s_str("account of the token that delivered it")})


class Registry:
    def __init__(self, app: FastAPI):
        self.app = app
        self.ops: list = []

    def op(self, method: str, path: str, *, tag: str, summary: str, response: dict, description: str = "",
           params: tuple = (), body: Optional[dict] = None, example=None):
        """Declare one route of the public API. ``params``: (name, in, schema, required, description) tuples."""
        def deco(fn: Callable):
            self.app.add_api_route(path, fn, methods=[method], name="v1_" + fn.__name__)
            self.ops.append(Op(method, path, fn.__name__, tag, summary, description, tuple(params), body, response, example))
            return fn
        return deco


def P(name: str, where: str, schema: dict, required: bool = False, desc: str = "") -> tuple:
    return (name, where, schema, required, desc)


def scope_of(method: str, path: str) -> str:
    if (method, path) == INGEST_ROUTE:
        return "ingest"
    rule = access.rule_for(method, path)
    return SCOPE_OF_ROLE.get(rule.role, rule.role) if rule else "admin"


def openapi(ops: list) -> dict:
    """The OpenAPI 3.1 description of the public API, built from the declarations."""
    paths: dict = {}
    for o in ops:
        scope = scope_of(o.method, o.path)
        oapi_path = o.path.replace(":path}", "}")
        entry = {
            "operationId": o.name, "summary": o.summary, "tags": [o.tag], "x-lanloom-scope": scope,
            "security": [{"token": []}, {"session": []}],
            "parameters": [{"name": n, "in": w, "required": bool(req or w == "path"), "schema": sch,
                            **({"description": d} if d else {})} for (n, w, sch, req, d) in o.params],
            "responses": {
                "200": {"description": "OK", "content": {"application/json": {"schema": o.response,
                                                                               **({"example": o.example} if o.example is not None else {})}}},
                "401": {"description": "no or an unknown/expired token", "content": {"application/json": {"schema": ERROR}}},
                "403": {"description": "the token's scope or the account's role is too low", "content": {"application/json": {"schema": ERROR}}},
                "429": {"description": "too many requests with this token (see Retry-After)", "content": {"application/json": {"schema": ERROR}}},
            },
        }
        if o.description:
            entry["description"] = o.description
        if o.body is not None:
            entry["requestBody"] = {"required": True, "content": {"application/json": {"schema": o.body}}}
            entry["responses"]["400"] = {"description": "the body is not valid", "content": {"application/json": {"schema": ERROR}}}
            entry["responses"]["413"] = {"description": "the body is too large", "content": {"application/json": {"schema": ERROR}}}
        if "{" in o.path:
            entry["responses"]["404"] = {"description": "not found", "content": {"application/json": {"schema": ERROR}}}
        paths.setdefault(oapi_path, {})[o.method.lower()] = entry
    return {
        "openapi": "3.1.0",
        "info": {"title": "Lanloom API", "version": API_VERSION, "x-lanloom-version": __version__,
                 "description": ("Public API of Lanloom. Authenticate with an API token (Settings > API) as "
                                 "'Authorization: Bearer llt_...'. Fields are only added within v1."),
                 "license": {"name": "Lanloom Source-Available License (free for private, non-commercial use with up to 100 devices; commercial use requires a paid license)", "url": "https://github.com/robeertm/lanloom/blob/main/LICENSE"}},
        "servers": [{"url": "/"}],
        "tags": [{"name": t} for t in dict.fromkeys(o.tag for o in ops)],
        "components": {"securitySchemes": {
            "token": {"type": "http", "scheme": "bearer", "bearerFormat": "llt_...", "description": "API token from Settings > API"},
            "session": {"type": "apiKey", "in": "cookie", "name": "ll_session", "description": "signed-in web page"}}},
        "paths": paths,
        "x-webhooks": {"events": sorted(webhooks_mod.EVENTS), "signature": "X-Lanloom-Signature: v1=HMAC-SHA256(secret, timestamp + '.' + body)",
                       "timestamp_header": "X-Lanloom-Timestamp", "tolerance_s": webhooks_mod.TOLERANCE_S},
    }


# ---------------------------------------------------------------- shaping of answers
def device_v1(d: dict) -> dict:
    merged = d.get("merged_into")
    return {"id": d["id"], "title": d.get("title") or "", "kind": d.get("kind") or "unknown", "ip": d.get("ip") or "",
            "mac": d.get("mac") or None, "ipv6": [str(x) for x in d.get("ipv6") or []], "vendor": d.get("vendor") or None,
            "model": d.get("model") or None, "firmware": d.get("firmware") or None,
            "status": d.get("status") if d.get("status") in ("up", "down", "off") else "unknown", "hidden": bool(d.get("hidden")),
            "gateway": bool(d.get("is_gateway")), "hostnames": [str(x) for x in d.get("hostnames") or []],
            "services": [str(x) for x in d.get("services") or []], "sources": [str(x) for x in d.get("sources") or []],
            "zone": (d.get("zone") or {}).get("name") if isinstance(d.get("zone"), dict) else (d.get("zone") or None),
            "merged_into": merged.get("id") if isinstance(merged, dict) else (merged or None),
            "first_seen": d.get("first_seen"), "last_seen": d.get("last_seen")}


def register(app: FastAPI, state, err: Callable, read_limited: Callable) -> Registry:
    api = Registry(app)
    app.state.api_v1 = api

    def lang_of(lang: str) -> str:
        return lang if lang in render_mod.languages() else "en"

    @api.op("GET", BASE, tag="general", summary="API index: versions and where the description is",
            response=s_obj({"api": s_str("API version"), "app": s_str("Lanloom version"), "openapi": s_str("path of the OpenAPI description"),
                            "you": s_obj({"username": s_str(), "role": s_str(), "via": s_str(enum=("token", "session")),
                                          "token": s_str("name of the token", nullable=True), "scope": s_str(nullable=True)})}),
            example={"api": "1", "app": __version__, "openapi": BASE + "/openapi.json",
                     "you": {"username": "ha-bridge", "role": "viewer", "via": "token", "token": "Home automation", "scope": "read"}})
    async def index(request: Request):
        user = request.state.user
        tok = user.get("token") or {}
        return {"api": API_VERSION, "app": __version__, "openapi": BASE + "/openapi.json",
                "you": {"username": user.get("username") or "", "role": user.get("role") or "", "via": "token" if tok else "session",
                        "token": tok.get("name"), "scope": tok.get("scope")}}

    @api.op("GET", BASE + "/openapi.json", tag="general", summary="This description (OpenAPI 3.1)",
            response=s_obj({"openapi": s_str(), "info": s_obj({}, required=(), extra=True), "paths": s_map({})}, required=("openapi", "info", "paths"), extra=True))
    async def openapi_json():
        return openapi(api.ops)

    @api.op("GET", BASE + "/status", tag="status", summary="Short state of the network: devices, internet line, connections, latest report",
            response=s_obj({
                "app": s_str(), "t": s_num(TS),
                "devices": s_obj({"total": s_int(), "online": s_int(), "offline": s_int(), "new": s_int("first seen in the last 24 hours"), "hidden": s_int()}),
                "wan": s_obj({"state": s_str("up, down or unknown"), "last_outage": s_num(TS, nullable=True)}),
                "sources": s_obj({"total": s_int(), "fresh": s_int(), "stale": s_int(), "overall": s_str()}),
                "report": s_obj({"latest": s_str("day of the latest nightly report", nullable=True), "level": s_str(nullable=True),
                                 "unread": s_int()})}))
    async def status():
        s = await asyncio.to_thread(state.core.overview.summary)
        dev, wan, src, rep = s.get("devices") or {}, s.get("wan") or {}, s.get("sources") or {}, s.get("report") or {}
        latest = rep.get("latest") or {}
        return {"app": __version__, "t": s.get("t"),
                "devices": {k: int(dev.get(k) or 0) for k in ("total", "online", "offline", "new", "hidden")},
                "wan": {"state": wan.get("state") or "unknown", "last_outage": wan.get("last_outage")},
                "sources": {"total": int(src.get("total") or 0), "fresh": int(src.get("fresh") or 0), "stale": int(src.get("stale") or 0),
                            "overall": src.get("overall") or "unknown"},
                "report": {"latest": latest.get("day"), "level": latest.get("level"), "unread": int(rep.get("unread") or 0)}}

    @api.op("GET", BASE + "/devices", tag="devices", summary="All devices",
            params=(P("hidden", "query", s_bool(), desc="also list devices hidden from the map (default false)"),
                    P("kind", "query", s_str(), desc="only this kind, e.g. printer"),
                    P("status", "query", s_str(enum=("up", "down", "off", "unknown")), desc="only this status")),
            response=s_obj({"count": s_int(), "devices": s_arr(DEVICE)}))
    async def devices(request: Request, hidden: bool = False, kind: str = "", status: str = ""):
        rows = [device_v1(d) for d in await asyncio.to_thread(state.core.devices)]
        rows = [d for d in rows if (hidden or not d["hidden"]) and (not kind or d["kind"] == kind[:40]) and (not status or d["status"] == status)]
        rows = access.redact_admin_fields(rows, request.state.user.get("role"))
        return {"count": len(rows), "devices": rows}

    @api.op("GET", BASE + "/devices/{device_id}", tag="devices", summary="One device",
            params=(P("device_id", "path", s_str(), True, "id of the device"),), response=DEVICE)
    async def device(request: Request, device_id: str):
        hit = next((d for d in await asyncio.to_thread(state.core.devices) if d["id"] == device_id[:80]), None)
        if hit is None:
            return err("api.err.not_found", 404)
        return access.redact_admin_fields(device_v1(hit), request.state.user.get("role"))

    @api.op("GET", BASE + "/topology", tag="topology", summary="Who hangs on whom: the links between devices",
            response=s_obj({"links": s_arr(s_obj({"child": s_str("device id"), "parent": s_str("device id"), "port": s_str("port on the parent", nullable=True),
                                                  "medium": s_str("wired, wireless, ...", nullable=True), "band": s_str(nullable=True),
                                                  "ssid": s_str(nullable=True), "source": s_str("where the link is known from", nullable=True)})),
                            "infra": s_arr(s_str(), "ids of infrastructure devices"), "unknown": s_arr(s_str(), "ids without a known place")}))
    async def topology():
        topo = await asyncio.to_thread(state.core.topology)
        links = []
        for child, ln in sorted((topo.get("links") or {}).items()):
            links.append({"child": ln.get("child") or child, "parent": ln.get("parent"),
                          "port": None if ln.get("port") is None else str(ln.get("port")), "medium": ln.get("medium"),
                          "band": ln.get("band"), "ssid": ln.get("ssid"), "source": ln.get("source")})
        return {"links": links, "infra": list(topo.get("infra") or []), "unknown": list(topo.get("unknown") or [])}

    @api.op("GET", BASE + "/availability", tag="availability", summary="Availability of every device and connection (35 days)",
            response=s_obj({"days": s_int(), "summary": s_obj({"subjects": s_int(), "open": s_int(), "wobbly": s_int(), "incidents7": s_int(),
                                                               "mttr_s": s_int(nullable=True), "stable": s_int()}),
                            "subjects": s_arr(AVAIL_ROW)}))
    async def availability():
        from .web_history import subject_names
        core = state.core
        st = await asyncio.to_thread(lambda: core.availability.stability(subject_names(core)))
        summ = dict(st.get("summary") or {})
        days = summ.pop("days", 35)
        return {"days": days, "summary": {k: summ.get(k) for k in ("subjects", "open", "wobbly", "incidents7", "mttr_s", "stable")},
                "subjects": [{k: r.get(k) for k in AVAIL_ROW["properties"]} for r in st.get("rows") or []]}

    @api.op("GET", BASE + "/availability/{subject:path}", tag="availability", summary="One subject: 24 hours in 5-minute slots, 35 daily shares, outages",
            params=(P("subject", "path", s_str(), True, "dev:<device id> or src:int:<connection id>"),),
            response=s_obj(dict(AVAIL_ROW["properties"], state=s_str("up, down or unknown"), slot_s=s_int("length of one slot in seconds"),
                                slots24=s_arr(s_int(), "the last 24 hours, oldest first: 0 = no data, 1 = up, 2 = down, 3 = partly"),
                                bars=s_arr(s_num(nullable=True), "daily shares, oldest first"), incidents=s_arr(INCIDENT, "newest first, at most 20"))))
    async def availability_one(subject: str):
        from .web_history import subject_names
        core = state.core
        names = subject_names(core)
        if subject not in names:
            return err("api.err.not_found", 404)
        d = await asyncio.to_thread(core.availability.detail, subject)
        out = {k: d.get(k) for k in AVAIL_ROW["properties"] if k not in ("title", "kind")}
        out.update(id=subject, title=names[subject]["title"], kind=names[subject]["kind"], state=d.get("state") or "unknown",
                   slot_s=d.get("slot_s"), slots24=d.get("slots24") or [], bars=d.get("bars") or [], incidents=d.get("list") or [])
        return out

    @api.op("GET", BASE + "/paths", tag="data-paths", summary="Data paths: bytes per country and per device over a window",
            params=(P("window", "query", s_str(enum=tuple(datapaths.PERIODS)), desc="24h (default), 7d or 30d"),),
            response=s_obj({"window": s_str(), "t": s_num(TS), "home": s_str("country of the internet line", nullable=True),
                            "totals": s_obj({"tx": s_int("bytes sent"), "rx": s_int("bytes received"), "outside_eu_share": s_num("0..1", nullable=True),
                                             "countries": s_int(), "devices": s_int()}),
                            "countries": s_arr(s_obj({"cc": s_str("ISO 3166 code"), "eu": s_bool(), "tx": s_int(), "rx": s_int(), "devices": s_int()})),
                            "devices": s_arr(s_obj({"id": s_str(), "title": s_str(), "tx": s_int(), "rx": s_int()}))}))
    async def paths(window: str = "24h"):
        if window not in datapaths.PERIODS:
            return err("api.err.window")
        s = await asyncio.to_thread(state.core.paths_summary, window)
        tot = s.get("totals") or {}
        return {"window": window, "t": s.get("t"), "home": s.get("home") or None,
                "totals": {"tx": int(tot.get("tx") or 0), "rx": int(tot.get("rx") or 0), "outside_eu_share": tot.get("outside_eu_share"),
                           "countries": int(tot.get("countries") or 0), "devices": int(tot.get("devices") or 0)},
                "countries": [{"cc": c.get("cc") or "", "eu": bool(c.get("eu")), "tx": int(c.get("tx") or 0), "rx": int(c.get("rx") or 0),
                               "devices": int(c.get("devices") or 0)} for c in s.get("countries") or []],
                "devices": [{"id": d.get("id") or "", "title": d.get("title") or "", "tx": int(d.get("tx") or 0), "rx": int(d.get("rx") or 0)}
                            for d in s.get("devices") or []]}

    @api.op("GET", BASE + "/reports", tag="reports", summary="The nightly reports that are kept",
            response=s_obj({"reports": s_arr(REPORT_ROW), "next_at": s_num("when the next one is made (" + TS + ")", nullable=True)}))
    async def reports():
        lst = state.core.reports.listing()
        return {"reports": [{k: r.get(k) for k in REPORT_ROW["properties"]} for r in lst.get("reports") or []], "next_at": lst.get("next_at")}

    @api.op("GET", BASE + "/reports/{day}", tag="reports", summary="One nightly report, with every line in the language you ask for",
            params=(P("day", "path", s_str(), True, "YYYY-MM-DD"), P("lang", "query", s_str(enum=tuple(render_mod.languages())), desc="language of the texts (default en)")),
            response=s_obj({"day": s_str(), "since": s_num(TS), "until": s_num(TS), "created": s_num(TS), "level": s_str(enum=("ok", "info", "warn", "danger")),
                            "points": s_int(), "open": s_int(), "headline": s_str(), "sections": s_arr(SECTION)}))
    async def report(request: Request, day: str, lang: str = "en"):
        rep = state.core.reports.get(day)
        if not rep:
            return err("api.err.not_found", 404)
        from .sites_report import filter_report
        rep = filter_report(rep, state.sites.allowed(getattr(request.state, "user", None)))   # lines of other sites (M1)
        r = render_mod.Renderer(lang_of(lang), state.core.reports.tz(), rep["day"])
        sections = []
        for s in rep.get("sections") or []:
            items = [dict(i, text=r.line(i)) for i in s.get("items") or []]
            sections.append({"id": s.get("id"), "title": r.tr(s.get("title") or ""), "level": s.get("level") or "ok",
                             "summary": r.line(s["summary"]) if isinstance(s.get("summary"), dict) else "", "items": items, "more": s.get("more")})
        return {"day": rep["day"], "since": rep.get("since"), "until": rep.get("until"), "created": rep.get("created"), "level": rep.get("level") or "ok",
                "points": int(rep.get("points") or 0), "open": int(rep.get("open") or 0), "headline": render_mod.headline(rep, r), "sections": sections}

    @api.op("GET", BASE + "/events", tag="webhooks", summary="The events a webhook can subscribe to",
            response=s_obj({"events": s_arr(s_obj({"id": s_str(), "group": s_str(), "available": s_bool("the source of the event exists in this installation")}))}))
    async def events():
        return {"events": state.webhooks.events()}

    @api.op("GET", BASE + "/ingest", tag="ingest", summary="External reports delivered recently",
            params=(P("limit", "query", s_int(), desc="at most this many (default 50, at most 200)"),),
            response=s_obj({"reports": s_arr(INGESTED)}))
    async def ingest_list(limit: int = 50):
        return {"reports": state.ingest.list(limit, site=state.core.site_id)}       # the reports of the request's site (M1T)

    CHAIN = s_obj({"id": s_str("short name for declared chains (the run route takes it); other chains have a prefixed id"), "name": s_str(), "kind": s_str(enum=("hypervisor", "hub", "agent", "manual")),
                   "status": s_str("ok, late, failed, running, none or unknown (unknown is never shown as ok)", ("ok", "late", "failed", "running", "none", "unknown")),
                   "level": s_str(enum=("ok", "warn", "danger", "unknown")), "last_ok_t": s_num(TS, nullable=True),
                   "expect_s": s_num("expected seconds between two backups"), "restore_checked_t": s_num("a person noted a restore check (" + TS + ")", nullable=True),
                   "progress": s_num("percent while running, when known", nullable=True)}, required=("id", "name", "kind", "status", "level"), extra=True)

    @api.op("GET", BASE + "/backups", tag="backups", summary="Backup chains with their state (control room)",
            response=s_obj({"summary": s_map(s_int()), "chains": s_arr(CHAIN), "ignored": s_arr(s_str(), "ids of automatically found chains a person set to not used; they are in no total")}),
            example={"summary": {"ok": 1}, "chains": [{"id": "offsite", "name": "Off-site copy", "kind": "manual", "status": "ok", "level": "ok",
                                                      "last_ok_t": 1795032261.2, "expect_s": 86400.0, "restore_checked_t": None, "progress": None}], "ignored": []})
    async def backups_list():
        v = await asyncio.to_thread(state.core.controlroom.backups.view)
        rows = [dict({k: c.get(k) for k in ("id", "name", "kind", "status", "level", "last_ok_t", "expect_s", "restore_checked_t")},
                     progress=(c.get("progress") or {}).get("pct")) for c in v["chains"]]
        for r in rows:
            if r["kind"] == "manual":
                r["id"] = r["id"].split(":", 1)[1]            # the short name the run route takes
        return {"summary": v["summary"], "chains": rows, "ignored": [c["id"] for c in v["ignored"]]}

    @api.op("POST", BASE + "/backups/{chain_id}/run", tag="backups", summary="A backup script reports that a declared chain just finished",
            description="Needs a token with the scope operate or admin. The chain has to be declared first (control room > Backups). With size and "
                        "duration Lanloom learns the throughput and can show progress for the next run.",
            params=(P("chain_id", "path", s_str(), True, "short name of the declared chain"),),
            body=s_obj({"ok": s_bool("true when the run succeeded"), "size": s_num("bytes copied", nullable=True), "duration_s": s_num("seconds it took", nullable=True)},
                       required=("ok",)),
            response=s_obj({"ok": s_bool()}), example={"ok": True})
    async def backups_run(request: Request, chain_id: str):
        data, _size, problem = await read_limited(request, 4096)
        if problem:
            return problem
        request.state.audit = {"target": chain_id[:48], "detail": {"ok": bool(data.get("ok"))}}
        if not state.core.controlroom.backups.report_run(chain_id[:48], bool(data.get("ok")), data.get("size"), data.get("duration_s")):
            return err("api.err.not_found", 404)
        return {"ok": True}

    INGEST_BODY = s_obj({"source": s_str("short name of the sender, e.g. nightly-backup (max 60)"), "title": s_str("headline (max 120)"),
                         "level": s_str("default info", ingest_mod.LEVELS), "summary": s_str("one line (max 500)"),
                         "items": s_arr(s_obj({"text": s_str("max 300"), "level": s_str(enum=ingest_mod.LEVELS)}, required=("text",)),
                                        "at most %d lines" % ingest_mod.MAX_ITEMS),
                         "site": s_str("the site the report is for - required when Lanloom keeps several sites (or the header X-Lanloom-Site); "
                                       "a report never lands in a site it did not name")}, required=("source", "title"))

    @api.op("POST", BASE + "/ingest", tag="ingest", summary="Deliver an external report (shown in Settings > API and in the next nightly report)",
            description="Needs a token with the scope ingest, operate or admin. At most %d KiB per report and %d reports a day."
                        % (ingest_mod.MAX_BYTES // 1024, ingest_mod.MAX_PER_DAY),
            body=INGEST_BODY, response=s_obj({"id": s_int(), "accepted": s_bool(), "t": s_num(TS), "items": s_int()}),
            example={"id": 17, "accepted": True, "t": 1795032261.2, "items": 1})
    async def ingest_add(request: Request):
        data, size, problem = await read_limited(request, ingest_mod.MAX_BYTES)
        if problem:
            return problem
        user = request.state.user
        # M1T: a report belongs to the site it names (header, address or the field "site"); with several sites a report that
        # names none is refused - it never lands in the default site
        named = getattr(request.state, "site_named", None)
        sid = sites_mod.ACTIVE.get() or sites_mod.HOME
        field = data.get("site") if isinstance(data, dict) else None
        if field is not None:
            if named in ("header", "address") and field != sid:
                return err("site.err.mismatch", 409)
            if not isinstance(field, str) or field not in state.sites.allowed(user):
                return err("site.err.unknown", 404)
            sid = field
        elif named not in ("header", "address") and len(state.sites.ids()) > 1:
            return err("site.err.required", 400)
        try:
            out = state.ingest.add(data, size, token=user.get("token"), actor=user.get("username") or "", site=sid)
        except OverflowError as exc:
            return err(str(exc), 429)
        except ValueError as exc:
            return err(str(exc))
        request.state.audit = {"target": str(data.get("source") or "")[:60], "detail": {"bytes": size, "items": out["items"]}}
        return out

    return api


async def read_json_limited(request: Request, limit: int) -> tuple:
    """(data, size, None) or (None, 0, error response): the body as JSON, refused above ``limit`` bytes before it is read."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        return None, 0, JSONResponse({"error": "api.err.too_large", "limit": limit}, status_code=413)
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            return None, 0, JSONResponse({"error": "api.err.too_large", "limit": limit}, status_code=413)
    import json
    try:
        data = json.loads(bytes(buf).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, 0, JSONResponse({"error": "request.err.json"}, status_code=400)
    if not isinstance(data, dict):
        return None, 0, JSONResponse({"error": "request.err.json"}, status_code=400)
    return data, len(buf), None
