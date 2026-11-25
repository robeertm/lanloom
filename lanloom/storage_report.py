"""Nightly report sections "Storage" (database size, growth, the nightly clean-up, space running out) and "Counter
check" (hours in which two meters of the same traffic disagreed, see counters.py). Texts are i18n keys."""
from .nightly.contrib import Contribution, item, register, section
from .storage import LOW_DAYS_LEFT


def _storage_available(ctx) -> bool:
    """Only with something to say: a clean-up ran in the report window, or there is a growth history."""
    s = getattr(ctx.core, "storage", None)
    if s is None:
        return False
    t = (s.store.get_setting("storage_state", None) or {}).get("t")
    return (isinstance(t, (int, float)) and ctx.since < t <= ctx.until) or s.growth()["per_day"] is not None


def _storage(ctx) -> dict:
    s = ctx.core.storage
    files = s.db_file_bytes()
    items = []
    st = s.store.get_setting("storage_state", None) or {}
    t = st.get("t")
    if isinstance(t, (int, float)) and ctx.since < t <= ctx.until:
        removed = sum(int(v or 0) for v in (st.get("removed") or {}).values())
        items.append(item("st-run:%d" % int(t), "storage.report.ran", "ok", at_at=t, n=removed, freed_bytes=int(st.get("freed") or 0)))
        if st.get("skipped") == "storage.vacuum.no_room":
            items.append(item("st-noroom:%d" % int(t), "storage.report.no_room", "warn", point=True, size_bytes=files["db"]))
    left = s.days_left()
    if left is not None and left < LOW_DAYS_LEFT:
        items.append(item("st-full", "storage.report.full_soon", "danger" if left < 7 else "warn", point=True, days=left,
                          growth_bytes=s.growth()["per_day"] or 0))
    g = s.growth()["per_day"]
    if g:
        return section("storage", "storage.report.title", "storage.report.summary_growth", items, size_bytes=files["db"], growth_bytes=g)
    return section("storage", "storage.report.title", "storage.report.summary", items, size_bytes=files["db"])


def _counters_available(ctx) -> bool:
    c = getattr(ctx.core, "counters", None)
    return c is not None and any(r for r in c.results() if ctx.since < (int(r.get("hour") or 0) + 1) * 3600 <= ctx.until)


def _counters(ctx) -> dict:
    from . import counters
    c = ctx.core.counters
    window = [r for r in c.results() if ctx.since < (int(r.get("hour") or 0) + 1) * 3600 <= ctx.until]
    groups: dict = {}
    items = []
    for r in counters.judged(window):
        state, factor = counters.effective(r)
        if r.get("pair") in counters.DAY_PAIRS and "wstate" in r:
            # judged on the rolling day: one point with the day ratio, not a count of hours
            items.append(item("cc:%s:day_%s" % (r["pair"], state), "counters.report.day_%s" % state, "warn", point=True,
                              pair_key="counters.pair." + r["pair"], pct=round((factor or 0) * 100),
                              at_at=(int(r["hour"]) + 1) * 3600))
            continue
        groups.setdefault((r["pair"], state), []).append(r)
    for (pair, state), rows in sorted(groups.items()):
        worst = max(rows, key=lambda r: abs((r.get("factor") or 0) - 1))
        items.append(item("cc:%s:%s" % (pair, state), "counters.report.%s" % state, "warn", point=True,
                          pair_key="counters.pair." + pair, n=len(rows), pct=round((worst.get("factor") or 0) * 100),
                          at_at=(int(rows[-1]["hour"]) + 1) * 3600))
    judged = len(window)
    return section("counters", "counters.report.title", "counters.report.summary_off" if items else "counters.report.summary_ok",
                   items, n=judged, bad=len(items))


register(Contribution("storage", 85, "storage.report.title", _storage_available, _storage))
register(Contribution("counters", 33, "counters.report.title", _counters_available, _counters))
