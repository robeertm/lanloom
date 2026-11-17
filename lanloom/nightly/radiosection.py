"""Nightly report section "radio and batteries" of the hub bridge (one section per home-automation hub connection).

Shown only when a hub connection exists and its inventory names radio devices or batteries (no source, no section).
Everything is judged at the time of the report (``ctx.until``) with the rules of ``hubmerge``:

* silent radio devices - unavailable at the hub, or "last seen" older than ``hubmerge.SILENT_S`` (a point each);
* batteries to change now (at most ``BATTERY_NOW`` % or reported low: a point) and soon (at most ``BATTERY_SOON`` %);
* silent radio receivers and border routers (a point), a disturbed network (coordinator silent or half of it silent);
* the summary counts radio devices, networks and hub devices drawn together with their network device.

Names are device names from the hub; nothing of people, places or cameras reaches this section (the privacy filter
drops it before). Long lists end with "and N more".
"""
from __future__ import annotations

from .. import hubmerge
from .contrib import Contribution, Ctx, _short, item, register, section

ID = "hub_radio"
MAX_EACH = 8


def _hubs(ctx: Ctx) -> list:
    return [i for i in ctx.core.integrations() if i.get("enabled") and i["kind"] == "hub"]


def _relevant(snap: dict) -> bool:
    return any(d.get("radio") for d in snap.get("devices") or []) or any(e.get("dc") == "battery" for e in snap.get("entities") or [])


def _available(ctx: Ctx) -> bool:
    return any(_relevant((ctx.core.hub.get(i["id"]) or {}).get("snapshot") or {}) for i in _hubs(ctx))


def _section(ctx: Ctx, integ: dict) -> dict | None:
    iid = integ["id"]
    entry = ctx.core.hub.get(iid) or {}
    snap = entry.get("snapshot") or {}
    if not _relevant(snap):
        return None
    states = entry.get("states") or {}
    radio = hubmerge.radio_view(snap, states, ctx.until)
    site = hubmerge.onsite(snap, states, radio, ctx.until)
    health = hubmerge.health(snap, radio)
    items = []

    def capped(key, rows, level, point, make):
        for r in rows[:MAX_EACH]:
            items.append(make(r))
        if len(rows) > MAX_EACH:
            items.append(item("radio:%d:more:%s" % (iid, key), "report.radio.more", "info", False, n=len(rows) - MAX_EACH))

    for net in radio["networks"]:
        if net["health"] == "bad":
            if net["coordinator_name"]:
                items.append(item("radio:%d:net:%s" % (iid, net["key"]), "report.radio.net_bad", "danger", True,
                                  name=_short(net["coordinator_name"]), silent=net["silent"], n=net["devices"]))
            else:
                items.append(item("radio:%d:net:%s" % (iid, net["key"]), "report.radio.net_bad_plain", "danger", True,
                                  silent=net["silent"], n=net["devices"]))
    anchors = [r for r in health["receivers"] + health["border_routers"] if r["fresh"] == "silent"]
    capped("anchor", anchors, "warn", True, lambda r: item("radio:%d:anchor:%s" % (iid, r["device_id"]), "report.radio.anchor_silent",
                                                          "warn", True, name=_short(r["name"])))
    silent = [i for i in site["items"] if i["cat"] == "silent" or (i["cat"] == "now" and "silent" in i["reasons"])]
    capped("silent", silent, "warn", True, lambda r: item("radio:%d:silent:%s" % (iid, r["device_id"]), "report.radio.silent", "warn", True,
                                                          name=_short(r["name"])))
    now = [i for i in site["items"] if i["cat"] == "now" and "silent" not in i["reasons"]]
    capped("now", now, "warn", True, lambda r: item("radio:%d:now:%s" % (iid, r["device_id"]),
                                                    "report.radio.battery_low" if r["battery"] is None else "report.radio.battery_now",
                                                    "warn", True, name=_short(r["name"]), pct=r["battery"] if r["battery"] is not None else 0))
    soon = [i for i in site["items"] if i["cat"] == "soon"]
    capped("soon", soon, "info", False, lambda r: item("radio:%d:soon:%s" % (iid, r["device_id"]), "report.radio.battery_soon", "info",
                                                       False, name=_short(r["name"]), pct=r["battery"]))
    merged = sum(1 for k in ctx.core.hub_merge()["by_hub"] if k.startswith("%d:" % iid))
    devices = sum(n["devices"] for n in radio["networks"])
    parts = [{"key": "report.radio.part." + k, "vars": {"n": v}} for k, v in (
        ("silent", len(silent)), ("now", len(now)), ("soon", len(soon)), ("merged", merged)) if v]
    return section(ID, "report.sec.hub_radio", "report.radio.summary", items, parts=parts, name=integ["name"],
                   n=devices, nets=len(radio["networks"]))


def _build(ctx: Ctx) -> list:
    return [s for s in (_section(ctx, i) for i in _hubs(ctx)) if s]


register(Contribution(ID, 46, "report.sec.hub_radio", _available, _build))
