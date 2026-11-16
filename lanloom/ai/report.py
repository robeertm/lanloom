"""Nightly report section "AI": how often a model was asked, and what needs a look (daily limit reached,
requests failing, the local service not answering) and new identification suggestions waiting for a decision.
Only present when AI is set up."""
from ..nightly.contrib import Contribution, item, register, section


def _ai(ctx):
    return getattr(ctx.core, "ai", None)


def _available(ctx) -> bool:
    ai = _ai(ctx)
    if ai is None:
        return False
    s = ai.settings()
    return bool(s["local"]["enabled"] or s["public"]["enabled"])


def _build(ctx) -> dict:
    ai = _ai(ctx)
    u = ai.usage_between(ctx.since, ctx.until)
    items = []
    if u["limited"]:
        items.append(item("ai-limit", "ai.report.limit", "warn", point=True, n=u["limited"], limit=ai.settings()["daily_limit"]))
    if u["failed"] and not (u["local"] or u["public"]):
        items.append(item("ai-failed", "ai.report.failed", "warn", point=True, n=u["failed"]))
    elif u["failed"]:
        items.append(item("ai-failed-some", "ai.report.failed_some", "info", n=u["failed"]))
    if u["local"] or u["public"]:
        items.append(item("ai-usage", "ai.report.usage", "ok", local=u["local"], public=u["public"]))
    hints = getattr(ai, "hints", None)
    if hints is not None:
        new = sum(1 for v in hints.all().values() if v.get("state") == "open" and ctx.since <= float(v.get("t") or 0) <= ctx.until)
        if new:
            items.append(item("ai-hints", "ai.report.hints", "info", n=new))
    key = "ai.report.summary" if (u["local"] or u["public"] or u["failed"]) else "ai.report.quiet"
    return section("ai", "ai.report.title", key, items, local=u["local"], public=u["public"])


register(Contribution("ai", 80, "ai.report.title", _available, _build))
