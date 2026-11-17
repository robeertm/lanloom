"""Nightly report section "Notifications" and the start-page note for an active maintenance window.

The section counts what the notification rules did overnight: messages sent, bundled, kept for the report by quiet
hours, suppressed during planned maintenance (with its names), flaps that never became news, the daily limit, and
failed deliveries. Failed deliveries and a heartbeat that does not get through are points that need a look. The
section appears only once somebody set up a rule, a push device, a maintenance window or the heartbeat."""
from ..nightly.contrib import Contribution, item, register, section
from ..overview import register_note


def _available(ctx) -> bool:
    n = getattr(ctx.core, "notify", None)
    if n is None:
        return False
    if n.maint_list() or n.heartbeat()["enabled"] or n.push_subs():
        return True
    return ctx.core.store.one("SELECT 1 AS x FROM settings WHERE key LIKE 'notify_user:%' LIMIT 1") is not None


def _build(ctx) -> dict:
    n = ctx.core.notify
    s = n.summary(ctx.since, ctx.until)
    c = s["count"]
    items = []
    if c["failed"]:
        items.append(item("nt-failed:%d" % int(ctx.until), "notify.report.failed", "warn", True, n=c["failed"]))
    hb = n.heartbeat()
    if hb["enabled"] and hb["ok"] is False:
        items.append(item("nt-hb:%d" % int(hb["last_ok"] or 0), "notify.report.heartbeat", "warn", True,
                          since_at=hb["last_ok"] or hb["last_t"], reason_key=hb["key"] or "report.send.error"))
    if c["sent"] or c["bundled"]:
        items.append(item("nt-sent", "notify.report.sent", "ok", n=c["sent"] + c["bundled"]))
    if c["quiet"]:
        items.append(item("nt-quiet", "notify.report.quiet", "info", n=c["quiet"]))
    if c["maintenance"]:
        names = ", ".join(x for x in s["maint_names"] if x) or "–"
        items.append(item("nt-maint", "notify.report.maintenance", "info", n=c["maintenance"], names=names))
    if c["flap"]:
        items.append(item("nt-flap", "notify.report.flap", "info", n=c["flap"]))
    if c["cooldown"]:
        items.append(item("nt-cool", "notify.report.cooldown", "info", n=c["cooldown"]))
    if c["capped"]:
        items.append(item("nt-cap", "notify.report.capped", "info", n=c["capped"]))
    if not items:
        return section("notifications", "notify.report.sec", "notify.report.calm", [])
    return section("notifications", "notify.report.sec", "notify.report.summary", items, n=s["n"])


register(Contribution("notifications", 92, "notify.report.sec", _available, _build))


@register_note
def _maintenance_note(core, now):
    n = getattr(core, "notify", None)
    w = n.maintenance_at(now) if n is not None else None
    if not w:
        return []
    return [{"id": "maint:" + w["id"], "level": "info", "key": "notify.note.maintenance", "sub": "notify.note.maintenance.sub",
             "vars": {"title": w.get("title") or "", "n": len(w.get("devices") or [])}, "link": "#/settings/notifications"}]
