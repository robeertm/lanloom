"""Nightly report section "Hosts and containers" of the host agents and container sources (E4).

Shown when at least one agent or container source is set up. Judged at the end of the report window from what the
agents last reported (nothing is healed or changed):

* an agent that has not reported for more than the offline time (a point);
* failed services and timers whose last run failed (a point each, names bundled per host);
* unhealthy or restarting containers (a point);
* an agent whose version differs from Lanloom's (a note with the image to pull);
* a disk of an agent's host that is fuller than 90 % (a point), memory above 95 % (a note).

The summary has counts only.
"""
from __future__ import annotations

from .. import agents as agents_mod
from .contrib import Contribution, Ctx, _short, item, register, section

ID = "hosts"
MAX_NAMES = 3
DISK_FULL = 90.0
MEM_FULL = 95.0


def _available(ctx: Ctx) -> bool:
    hub = getattr(ctx.core, "agents", None)
    return bool(hub and hub.entries())


def _names(names) -> str:
    names = [_short(n, 40) for n in names]
    return ", ".join(names[:MAX_NAMES]) + (" …" if len(names) > MAX_NAMES else "")


def _build(ctx: Ctx) -> list:
    hub = ctx.core.agents
    run = hub.running(ctx.until)
    items = []
    for h in run["hosts"]:
        hid, name = h["id"], h["name"]
        if h["kind"] == "agent" and not h["online"]:
            items.append(item("hosts:%s:offline" % hid, "report.hosts.offline", "warn", True, host=name,
                              last_at=h.get("last_seen") or 0))
            continue
        failed = [u["name"] for u in h["units"]["failed"]]
        timers = [x["name"] for x in h["units"]["timers"] if x.get("verdict") == "failed" and x.get("unit") not in failed]
        if failed:
            items.append(item("hosts:%s:failed" % hid, "report.hosts.failed", "warn", True, host=name, n=len(failed),
                              names=_names(failed)))
        if timers:
            items.append(item("hosts:%s:timers" % hid, "report.hosts.timers", "warn", True, host=name, n=len(timers),
                              names=_names(timers)))
        bad = [c["name"] for c in h["containers"]["items"] if c.get("health") == "unhealthy" or c.get("state") == "restarting"]
        if bad:
            items.append(item("hosts:%s:unhealthy" % hid, "report.hosts.unhealthy", "warn", True, host=name, n=len(bad),
                              names=_names(bad)))
        for d in (h.get("system") or {}).get("disks") or []:
            if d.get("total"):
                used = 100.0 * (1 - (d.get("free") or 0) / d["total"])
                if used >= DISK_FULL:
                    items.append(item("hosts:%s:disk:%s" % (hid, d.get("mount")), "report.hosts.disk", "warn", True, host=name,
                                      mount=d.get("mount") or "/", pct=round(used)))
        sys_ = h.get("system") or {}
        if sys_.get("mem_total") and sys_.get("mem_avail") is not None:
            mem = 100.0 * (1 - sys_["mem_avail"] / sys_["mem_total"])
            if mem >= MEM_FULL:
                items.append(item("hosts:%s:mem" % hid, "report.hosts.mem", "info", False, host=name, pct=round(mem)))
        if h.get("update"):
            items.append(item("hosts:%s:update" % hid, "report.hosts.update", "info", False, host=name,
                              version=h["update"]["agent"], target=h["update"]["version"], image=h["update"]["image"]))
    s = hub.summary(ctx.until)
    parts = [{"key": "report.hosts.part." + k, "vars": {"n": v}} for k, v in (
        ("containers", s["containers"]), ("unhealthy", s["unhealthy"]), ("failed", s["failed_units"] + s["failed_timers"])) if v]
    return [section(ID, "report.sec.hosts", "report.hosts.summary", items, parts=parts, n=s["agents"] + s["proxies"],
                    online=s["online"])]


register(Contribution(ID, 48, "report.sec.hosts", _available, _build))
