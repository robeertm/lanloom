"""Nightly report section "Control room": what happened to the internet line, the battery backup and the backups since the last report.

One section, only when at least one source is there. Points (things that need a look): an internet outage or flapping inside the
window, the reserve line in use, a UPS on battery, a backup chain that failed, is late or unknown. A calm night is one line.
Prüfdaten run with a fixed clock (``ctx.until``), so the text of a night is the same every time it is built."""
from ..nightly.contrib import Contribution, item, register, section


WAN_KEYS = {"failover": "cr.report.wan_failover", "failback": "cr.report.wan_failback"}
BACKUP_KEYS = {"failed": ("cr.report.backup_failed", "cr.report.backup_failed_since"), "late": ("cr.report.backup_late", "cr.report.backup_late_since"),
               "unknown": ("cr.report.backup_unknown", "cr.report.backup_unknown_since"), "none": ("cr.report.backup_none", "cr.report.backup_none_since")}


def _room(ctx):
    return getattr(ctx.core, "controlroom", None)


def _available(ctx) -> bool:
    cr = _room(ctx)
    if cr is None:
        return False
    return bool(cr.ups.units or cr.backups.chains(ctx.until) or cr.wan.confirmed is not None or cr.wan.history(ctx.until)["events"])


def _build(ctx) -> dict:
    cr = _room(ctx)
    items = []
    for ep in cr.wan.episodes(ctx.since, ctx.until):
        if ep["kind"] == "flapping":
            items.append(item("cr-wan-flap:%d" % ep["from_t"], "cr.report.wan_flapping", "warn", point=True, from_at=ep["from_t"], to_at=ep["to_t"]))
        else:
            key = "cr.report.wan_down_open" if ep["open"] else "cr.report.wan_down"
            items.append(item("cr-wan-down:%d" % ep["from_t"], key, "danger", point=True, from_at=ep["from_t"], to_at=ep["to_t"], len_dur=int(ep["to_t"] - ep["from_t"])))
    rows = cr.wan.store.query("SELECT t, kind, line FROM wan_log WHERE t >= ? AND t <= ? AND kind IN ('failover','failback') ORDER BY t", (ctx.since, ctx.until))
    for r in rows:
        items.append(item("cr-wan-%s:%d" % (r["kind"], r["t"]), WAN_KEYS[r["kind"]], "warn" if r["kind"] == "failover" else "info",
                          point=r["kind"] == "failover", when_at=r["t"], name=r["line"]))
    for ep in cr.ups.episodes(ctx.since, ctx.until):
        lvl = "danger" if ep["state"] == "low_battery" else "warn"
        items.append(item("cr-ups:%d" % ep["from_t"], "cr.report.ups_battery_open" if ep["open"] else "cr.report.ups_battery", lvl, point=True,
                          from_at=ep["from_t"], to_at=ep["to_t"], len_dur=int(ep["to_t"] - ep["from_t"])))
    bv = cr.backups.view(ctx.until)
    for ch in bv["chains"]:
        if ch["status"] in ("failed", "late", "unknown", "none"):
            lvl = {"failed": "danger", "late": "warn", "none": "warn", "unknown": "info"}[ch["status"]]
            vars_ = {"name": ch["name"]}
            if ch["last_ok_t"]:
                vars_["last_at"] = ch["last_ok_t"]
            items.append(item("cr-bk:%s:%s" % (ch["id"], ch["status"]), BACKUP_KEYS[ch["status"]][1 if ch["last_ok_t"] else 0], lvl,
                              point=ch["status"] != "unknown", **vars_))
    n_points = sum(1 for i in items if i["point"])
    if not n_points:
        return section("controlroom", "cr.report.title", "cr.report.calm", items, chains=len(bv["chains"]))
    return section("controlroom", "cr.report.title", "cr.report.summary", items, n=n_points)


register(Contribution("controlroom", 28, "cr.report.title", _available, _build))
