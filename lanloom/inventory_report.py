"""Nightly report section "Inventory": devices that went missing or are probably gone, warranties and end-of-updates dates
that crossed a threshold in the report window, provisional entries that were removed, and retired devices that showed up
again. A threshold is announced once (the night it is crossed), never every night. Texts are i18n keys."""
import datetime

from . import eol as eol_mod
from .inventory import DAY
from .nightly.contrib import Contribution, item, register, section

WARRANTY_MARKS = (None, 7, 0)       # the configured warning time, one week, the day it ends
EOL_MARKS = (None, 30, 0)


def _midnight(date_str):
    d = eol_mod.parse_date(date_str)
    return datetime.datetime.combine(d, datetime.time(0), datetime.timezone.utc).timestamp() if d else None


def _crossings(until_str, marks, base, since, until):
    """The marks (days before the end date) whose moment lies inside (since, until]; 0 = the end date has passed (end of that day)."""
    t = _midnight(until_str)
    if t is None:
        return []
    out = []
    for m in marks:
        days = base if m is None else m
        at = t + DAY if days == 0 else t - days * DAY
        if since < at <= until:
            out.append(days)
    return out


def _entries(ctx):
    inv = ctx.core.inventory
    data = inv.list({}, now=ctx.until)
    return data, [i for i in data["items"]]


def _build_items(ctx) -> list:
    data, entries = _entries(ctx)
    cfg = data["settings"]
    items = []
    for ev in ctx.events("inv_expired"):
        d = ev["data"]
        items.append(item("inv-exp:%s:%d" % (d.get("id"), int(ev["t"])), "inventory.report.expired", "info", False,
                          name=d.get("title") or d.get("ip") or "?", kind_key="kind." + str(d.get("kind") or "unknown"), since_at=d.get("since") or ev["t"]))
    for i in entries:
        name = i["title"] or i["ip"]
        if i["state"] == "decommissioned":
            dc = i["decommissioned"] or {}
            if dc.get("seen_again") and i["alive_at"] and ctx.since < i["alive_at"] <= ctx.until:
                items.append(item("inv-back:%s" % i["id"], "inventory.report.seen_again", "warn", True, name=name))
            continue
        if i["state"] in ("missing", "removed") and i["alive_at"]:
            started = i["alive_at"] + cfg["missing_days"] * DAY
            gone_at = i["alive_at"] + cfg["removed_days"] * DAY
            key = "inventory.report.removed" if i["state"] == "removed" else "inventory.report.missing"
            if ctx.since < started <= ctx.until:
                items.append(item("inv-miss:%s:%d" % (i["id"], int(i["alive_at"])), key, "warn", True,
                                  name=name, days=cfg["missing_days"], since_at=i["alive_at"]))
            elif i["state"] == "removed" and ctx.since < gone_at <= ctx.until:       # it was only "missing" until the long absence tipped it
                items.append(item("inv-gone:%s:%d" % (i["id"], int(i["alive_at"])), "inventory.report.removed", "warn", True,
                                  name=name, days=cfg["removed_days"], since_at=i["alive_at"]))
        if i["warranty_until"]:
            for days in _crossings(i["warranty_until"], WARRANTY_MARKS, cfg["warranty_warn_days"], ctx.since, ctx.until):
                key = "inventory.report.warranty_over" if days == 0 else "inventory.report.warranty_soon"
                items.append(item("inv-war:%s:%d" % (i["id"], days), key, "warn", True, name=name, days=days, date=i["warranty_until"]))
        if i["eol"]:
            for days in _crossings(i["eol"]["until"], EOL_MARKS, cfg["eol_warn_days"], ctx.since, ctx.until):
                key = "inventory.report.eol_over" if days == 0 else "inventory.report.eol_soon"
                items.append(item("inv-eol:%s:%d" % (i["id"], days), key, "danger" if days == 0 else "warn", True,
                                  name=name, days=days, date=i["eol"]["until"]))
    return items


def _available(ctx) -> bool:
    try:
        return bool(_build_items(ctx))
    except Exception:
        return False


def _build(ctx) -> dict:
    items = _build_items(ctx)
    data, _ = _entries(ctx)
    s = data["summary"]
    return section("inventory", "inventory.report.title", "inventory.report.summary", items, total=s["total"] - s["decommissioned"],
                   missing=s["missing"] + s["removed"], warranty=s["warranty_soon"], eol=s["eol_past"] + s["eol_soon"])


register(Contribution("inventory", 25, "inventory.report.title", _available, _build))
