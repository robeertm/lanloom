"""Nightly report section "API": tokens that expire soon or expired overnight, webhook deliveries that failed, webhooks
that keep failing, and the external reports that were delivered through ``POST /api/v1/ingest`` (their own lines,
as text). Shown only when the installation uses the API at all (a token, a webhook or a delivered report)."""
from .nightly.contrib import Contribution, item, register, section

EXPIRY_WARN_S = 14 * 86400
SHOW_REPORTS = 6


def _parts(core):
    return getattr(core, "tokens", None), getattr(core, "webhooks", None), getattr(core, "api_ingest", None)


def _available(ctx) -> bool:
    tokens, hooks, ingest = _parts(ctx.core)
    if ingest is None:
        return False
    # keys and webhooks belong to the instance (the home site's report names them); every site lists its own external reports
    return bool((tokens is not None and tokens.count()) or (hooks is not None and hooks.list()) or ingest.between(ctx.since, ctx.until))


def _build(ctx) -> dict:
    tokens, hooks, ingest = _parts(ctx.core)
    items = []
    for t in (tokens.expiring(EXPIRY_WARN_S, now=ctx.until) if tokens is not None else []):
        items.append(item("api-expiring:%d" % t["id"], "api.report.expiring", "warn", point=True, name=t["name"], user=t["username"],
                          expires_at=t["expires"]))
    for t in (tokens.expired_since(ctx.since, ctx.until) if tokens is not None else []):
        items.append(item("api-expired:%d" % t["id"], "api.report.expired", "info", name=t["name"], user=t["username"]))
    s = hooks.summary(ctx.since, ctx.until) if hooks is not None else {"failed": 0, "failed_hooks": [], "failing_now": [], "sent": 0}
    if s["failed"]:
        items.append(item("api-hook-failed:%d" % int(ctx.until), "api.report.hook_failed", "warn", point=True, n=s["failed"],
                          names=", ".join(s["failed_hooks"][:3])))
    for name in s["failing_now"]:
        items.append(item("api-hook-down:%s" % name, "api.report.hook_down", "warn", point=True, name=name))
    received = ingest.between(ctx.since, ctx.until)
    level_of = {"ok": "info", "info": "info", "warn": "warn", "danger": "danger"}
    for r in received[-SHOW_REPORTS:]:
        bad = r["level"] in ("warn", "danger")
        items.append(item("api-in:%d" % r["id"], "api.report.ingest", level_of[r["level"]], point=bad, source=r["source"], title=r["title"],
                          summary=r["summary"] or "-", at_at=r["t"]))
    if len(received) > SHOW_REPORTS:
        items.append(item("api-in-more", "api.report.ingest_more", "info", n=len(received) - SHOW_REPORTS))
    key = "api.report.summary" if any(i["point"] for i in items) else "api.report.calm"
    return section("api", "api.report.title", key, items, sent=s["sent"], received=len(received), tokens=tokens.count() if tokens is not None else 0,
                   points=sum(1 for i in items if i["point"]))


register(Contribution("api", 39, "api.report.title", _available, _build))
