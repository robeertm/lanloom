"""Nightly report section "Vulnerability hints": hints that are new in the report window (first seen that night), the
open total, and a note when the bundled feed has grown old. A hint is announced once (the night it first appears), never
every night; it is phrased as a hint with something to do, never as an alarm. Texts are i18n keys."""
import datetime

from . import eol as eol_mod
from .nightly.contrib import Contribution, item, register, section
from .vulns import action_key

FEED_OLD_DAYS = 180


def _new_items(ctx) -> list:
    v = getattr(ctx.core, "vulns", None)
    if v is None:
        return []
    seen = v.first_seen()
    items = []
    for dev, f in v.open_findings(ctx.until):
        first = seen.get("%s|%s" % (dev["id"], f["id"]), ctx.until)
        if not (ctx.since < first <= ctx.until):
            continue
        act = action_key(f).split(".")[-1]                       # update_to | update | replace
        key = "vulns.report.%s%s" % ("x." if f["exploited"] else "", act)
        serious = f["exploited"] or f["certainty"] == "certain"
        items.append(item("vuln:%s:%s" % (dev["id"], f["id"]), key, "warn" if serious else "info", serious,
                          name=dev["title"] or dev["ip"] or "?", id=f["id"], severity_key="vulns.sev." + f["severity"],
                          certainty_key="vulns.certainty." + f["certainty"], fixed=f["fixed"] or ""))
    return items


def _feed_item(ctx):
    v = getattr(ctx.core, "vulns", None)
    info = v.feed_info() if v is not None else {}
    d = eol_mod.parse_date(info.get("as_of"))
    if not d or info.get("demo"):
        return None
    t = datetime.datetime.combine(d, datetime.time(0), datetime.timezone.utc).timestamp() + FEED_OLD_DAYS * 86400
    if ctx.since < t <= ctx.until:
        return item("vuln-feed-old:%s" % info["as_of"], "vulns.report.feed_old", "info", False, date=info["as_of"], days=FEED_OLD_DAYS)
    return None


def _available(ctx) -> bool:
    try:
        v = getattr(ctx.core, "vulns", None)
        return bool(v is not None and (v.open_findings(ctx.until) or _feed_item(ctx)))
    except Exception:
        return False


def _build(ctx) -> dict:
    items = _new_items(ctx)
    fi = _feed_item(ctx)
    if fi:
        items.append(fi)
    s = ctx.core.vulns.list({}, now=ctx.until)["summary"]
    return section("vulns", "vulns.report.title", "vulns.report.summary", items, open=s["open"], devices=s["devices"],
                   exploited=s["exploited"])


register(Contribution("vulns", 26, "vulns.report.title", _available, _build))
