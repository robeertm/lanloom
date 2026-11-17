"""Nightly report section "Availability": how steady the devices and connections were over the last seven days.

It names the wobbly ones (three or more gaps in 7 days, see availability.py) as points to look at and gives the typical
repair time; a calm week is one line. The single outages of the night are in the "Devices" section already, this one is
the trend behind them. Texts are i18n keys."""
from .nightly.contrib import Contribution, item, register, section
from .web_history import subject_names


def _stability(ctx) -> dict:
    core = ctx.core
    return core.availability.stability(subject_names(core), ctx.until)


def _available(ctx) -> bool:
    a = getattr(ctx.core, "availability", None)
    return a is not None and a.has_data() and bool(_stability(ctx)["rows"])


def _build(ctx) -> dict:
    stab = _stability(ctx)
    rows, sm = stab["rows"], stab["summary"]
    items = []
    for r in rows:
        if r["wobbly"]:
            items.append(item("av-wob:" + r["id"], "avail.report.wobbly", "warn", point=True, name=r["title"], n=r["incidents7"],
                              mttr_dur=r["mttr_s"] or 0))
    total = len(rows)
    if not sm["incidents7"]:
        return section("availability", "avail.report.title", "avail.report.summary_calm", items, total=total)
    if sm["mttr_s"]:
        return section("availability", "avail.report.title", "avail.report.summary", items, total=total, stable=sm["stable"],
                       n=sm["incidents7"], mttr_dur=sm["mttr_s"])
    return section("availability", "avail.report.title", "avail.report.summary_open", items, total=total, stable=sm["stable"], n=sm["incidents7"])


register(Contribution("availability", 25, "avail.report.title", _available, _build))
