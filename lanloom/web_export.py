"""HTTP routes of the exports (lanloom/export.py, lanloom/export_plan.py).

Everybody who is signed in may download (viewer and up: they read the same data on screen); every download is written to the audit log
with what was fetched and whether it was redacted (never the content). The import changes own fields and connections, so it needs the
operator role, like editing them one by one; a trial run (``dry_run``) changes nothing.
"""
import asyncio
import datetime
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response

from . import __version__, export, export_plan, mapmodel, pdfmini, textfmt

WHAT = {"list.csv": "text/csv; charset=utf-8", "list.json": "application/json; charset=utf-8",
        "plan.svg": "image/svg+xml", "plan.drawio": "application/vnd.jgraph.mxfile+xml", "plan.pdf": "application/pdf"}
VIEWS = ("network", "overview")


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable
    load_i18n: Callable


def _bool(v) -> bool:
    return v is True or str(v).lower() in ("1", "true", "yes")


def make_tr(texts: dict, lang: str) -> Callable:
    def tr(key: str, vars_: dict = None) -> str:
        raw = texts.get(key)
        return textfmt.fill(raw, vars_ or {}, lang) if raw else ""
    return tr


def render(core, what: str, opts: dict, texts: dict, lang: str, now: float) -> tuple:
    """(bytes, media type, file name). Raises ValueError(i18n key) for a wrong request."""
    if what not in WHAT:
        raise ValueError("export.err.what")
    redact = _bool(opts.get("redact"))
    view = opts.get("view") if opts.get("view") in VIEWS else "network"
    look = opts.get("look") if opts.get("look") in export_plan.LOOKS else "light"
    size = opts.get("size") if opts.get("size") in export_plan.PAGES else "A4"
    tr = make_tr(texts, lang)
    day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y%m%d")
    tag = "-redacted" if redact else ""
    if what.startswith("list."):
        data = export.prepare(core, redact, now)
        if what == "list.csv":
            return export.to_csv(data).encode("utf-8"), WHAT[what], "lanloom-inventory-%s%s.csv" % (day, tag)
        return export.to_json(data, redact, __version__, now).encode("utf-8"), WHAT[what], "lanloom-inventory-%s%s.json" % (day, tag)
    rd = export.Redactor(export._safe_networks(core)) if redact else None
    stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def scene_of(v: str) -> dict:
        model = mapmodel.cached_build(core, v)
        sub = tr("export.plan.sub", {"date": stamp, "version": __version__})
        if redact:
            sub += " · " + tr("export.plan.redacted")
        return export_plan.build_scene(model, tr, rd, tr("export.plan.title." + v), sub)

    if what == "plan.svg":
        return export_plan.to_svg(scene_of(view), look, tr).encode("utf-8"), WHAT[what], "lanloom-plan-%s-%s%s.svg" % (view, day, tag)
    if what == "plan.drawio":
        return export_plan.to_drawio(scene_of(view), look, tr).encode("utf-8"), WHAT[what], "lanloom-plan-%s-%s%s.drawio" % (view, day, tag)
    pdf = pdfmini.Pdf(tr("export.plan.pdf_title"), "Lanloom " + __version__, datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y%m%d%H%M%SZ"))
    for v in VIEWS:
        export_plan.plan_pages(pdf, scene_of(v), look, size, tr)
    data = export.prepare(core, redact, now, rd)
    export_plan.inventory_pages(pdf, data, look, size, tr, tr("export.inventory.title"))
    export_plan.footers(pdf, look, "Lanloom %s · %s%s" % (__version__, stamp, " · " + tr("export.plan.redacted") if redact else ""))
    return pdf.render(), WHAT[what], "lanloom-plan-%s-%s%s.pdf" % (size.lower(), day, tag)


def register(app: FastAPI, c: Ctx) -> None:
    st = c.state
    gate = asyncio.Semaphore(2)                      # a plan of a big network takes a moment; never more than two at once

    @app.post("/api/export")
    async def export_download(request: Request):
        data = await c.body(request)
        what = str(data.get("what") or "")[:24]
        lang = data.get("lang") if data.get("lang") in textfmt.languages() else "en"
        c.note(request, target=what, detail={"redact": _bool(data.get("redact")), "view": str(data.get("view") or "")[:10],
                                             "size": str(data.get("size") or "")[:4]})
        texts = c.load_i18n(lang)
        async with gate:
            try:
                body, mime, name = await asyncio.to_thread(render, st.core, what, data, texts, lang, time.time())
            except ValueError as exc:
                return c.err(str(exc))
        return Response(body, media_type=mime, headers={"Content-Disposition": 'attachment; filename="%s"' % name, "X-Lanloom-File": name,
                                                        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/export/import")
    async def export_import(request: Request):
        data = await c.body(request)
        fmt = str(data.get("format") or "")[:4]
        dry = data.get("dry_run") is not False            # anything but an explicit false is a trial run
        c.note(request, detail={"format": fmt, "dry_run": dry})
        try:
            records = export.parse(fmt, data.get("text"))
            report = await asyncio.to_thread(export.apply, st.core, records, dry)
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, detail={"format": fmt, "dry_run": dry, "rows": report["rows"], "changed": report["changed"], "errors": len(report["errors"])})
        return report
