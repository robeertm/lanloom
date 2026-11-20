"""Nightly report section "Energy": yesterday's consumption, cost, CO2 and solar share, only when there is a total for that day."""
from .nightly.contrib import Contribution, item, register, section


def _book(ctx):
    return getattr(ctx.core, "energy", None)


def _totals(ctx):
    book = _book(ctx)
    return book.day_totals(book.yesterday_key(ctx.until)) if book is not None else None


def _available(ctx) -> bool:
    return _totals(ctx) is not None


def _build(ctx) -> dict:
    t = _totals(ctx)
    items = [item("energy-kwh", "energy.report.kwh", "info", kwh=round(t["kwh"], 1))]
    if t.get("cost") is not None:
        book = _book(ctx)
        cur = (book.view(ctx.until)["price"] or {}).get("currency") or book.settings()["currency"]
        items.append(item("energy-cost", "energy.report.cost", "info", cost=round(t["cost"], 2), currency=cur))
    if t.get("co2_kg") is not None:
        items.append(item("energy-co2", "energy.report.co2", "info", kg=round(t["co2_kg"], 1)))
    if t.get("solar_kwh"):
        own = max(0.0, t["solar_kwh"] - (t.get("export_kwh") or 0.0))
        items.append(item("energy-solar", "energy.report.solar", "info", kwh=round(t["solar_kwh"], 1),
                          pct=round(100.0 * min(1.0, own / t["kwh"])) if t["kwh"] else 0))
    return section("energy", "energy.report.title", "energy.report.summary", items, kwh=round(t["kwh"], 1))


register(Contribution("energy", 29, "energy.report.title", _available, _build))
