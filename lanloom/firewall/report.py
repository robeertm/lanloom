"""Nightly report section "Changes": what the executor wrote to the controller since the last report.

The source is the event journal (``fw_change``, written by the firewall editor when a change on a real controller
finished). Rehearsals on the sample network are not changes and never appear. The section is there when an external
executor is configured or when the window holds a change; otherwise it is left out. A change that was rolled back on
its own or failed is a point; a finished change and a refused plan are plain notes. Names only - no addresses.
"""
import os

from ..nightly.contrib import Contribution, item, register, section
from ..notify import events as notify_events

POINT = {"rolled_back": "warn", "failed": "danger"}


def _available(ctx) -> bool:
    if os.environ.get("LANLOOM_FW_EXECUTOR", "").strip().lower() == "external":
        return True
    return bool(ctx.events("fw_change"))


def _what(data: dict) -> str:
    names = [str(n) for n in data.get("names") or []][:3]
    more = max(0, int(data.get("n") or 0) - len(names))
    return ", ".join(names) + (" +%d" % more if more else "")


def _build(ctx) -> dict:
    items = []
    done = 0
    for ev in ctx.events("fw_change"):
        d = ev.get("data") or {}
        status = d.get("status")
        common = {"what": _what(d) or "?", "who": str(d.get("user") or "?"), "at_at": ev["t"], "n": int(d.get("n") or 0)}
        iid = "fwc:%s" % str(d.get("plan") or ev["t"])[:48]
        if status == "done":
            done += 1
            items.append(item(iid, "report.changes.undone" if d.get("undo_of") else "report.changes.done", "ok", **common))
        elif status in POINT:
            reason = str(d.get("reason") or "")
            items.append(item(iid, "report.changes." + status, POINT[status], True,
                              reason_key=reason if reason.startswith("fw.res.") else "fw.res.apply_failed", **common))
        elif status == "refused":
            items.append(item(iid, "report.changes.refused", "info", **common))
    trouble = len([i for i in items if i["point"]])
    if not items:
        return section("changes", "report.sec.changes", "report.changes.calm", items)
    key = "report.changes.summary_trouble" if trouble else "report.changes.summary"
    return section("changes", "report.sec.changes", key, items, n=done, bad=trouble)


register(Contribution("changes", 52, "report.sec.changes", _available, _build))


# Section "Network plan": how far the segmentation plan has come (Lanloom's own note of the target network per device;
# the editor keeps its last progress). There only when a plan exists. Names of target networks only.
def _plan_available(ctx) -> bool:
    from . import segplan
    return segplan.progress(ctx.core.store) is not None


def _plan_build(ctx) -> dict:
    from . import segplan
    p = segplan.progress(ctx.core.store) or {}
    total, moved = int(p.get("total") or 0), int(p.get("moved") or 0)
    items = []
    before = None
    for sec in (ctx.prev or {}).get("sections") or []:
        if sec.get("id") == "netplan":
            before = ((sec.get("summary") or {}).get("vars") or {}).get("moved")
    if isinstance(before, int) and moved > before:
        items.append(item("np:progress", "report.netplan.progress", "ok", n=moved - before))
    for g in p.get("groups") or []:
        done = g.get("moved") == g.get("total")
        items.append(item("np:" + str(g.get("label") or "?")[:40].lower(), "report.netplan.group_done" if done else "report.netplan.group",
                          "ok" if done else "info", label=str(g.get("label") or "?")[:32], moved=int(g.get("moved") or 0),
                          total=int(g.get("total") or 0)))
    key = "report.netplan.summary_done" if moved >= total else "report.netplan.summary"
    return section("netplan", "report.sec.netplan", key, items, moved=moved, total=total)


register(Contribution("netplan", 53, "report.sec.netplan", _plan_available, _plan_build))


# Section "Zone matrix": which zone pairs of a zone firewall opened or closed since the last report (whole zones, any
# traffic, the same evaluation as "May A reach B?"), and whole zones that take traffic from the internet. There only when a
# connection reads zones and rules. Zone names only - no addresses.
ZM_MAX_PAIRS = 400


def _zm_sources(ctx) -> list:
    out = []
    try:
        live = ctx.core.live_configs() or []
    except Exception:
        live = []
    for cfg in live:
        st = cfg.get("state") or {}
        if st.get("zones") and st.get("policies"):
            out.append(cfg)
    return out


def _zm_available(ctx) -> bool:
    return bool(_zm_sources(ctx))


def _zm_build(ctx) -> dict:
    from . import zonefw
    before = ((ctx.prev or {}).get("keep") or {}).get("zonematrix") or {}
    now, items, opened, inbound = {}, [], 0, 0
    first = not before
    for cfg in _zm_sources(ctx):
        st = cfg["state"]
        names = {z["id"]: str(z.get("name") or "?")[:40] for z in st.get("zones") or []}
        allowed = sorted("%s>%s" % k for k, v in zonefw.zone_matrix(st).items() if v == "allow")[:ZM_MAX_PAIRS]
        src = str(cfg.get("name") or "?")[:40]
        now[cfg["key"]] = {"allowed": allowed, "names": names}
        old = before.get(cfg["key"])
        if old is not None:
            was = set(old.get("allowed") or [])
            for pair in sorted(set(allowed) - was):
                a, b = pair.split(">", 1)
                opened += 1
                items.append(item("zm:o:%s:%s" % (cfg["key"], pair), "report.zm.opened", "warn", True, source=src,
                                  **{"from": names.get(a, "?"), "to": names.get(b, "?")}))
            for pair in sorted(was - set(allowed)):
                a, b = pair.split(">", 1)
                old_names = old.get("names") or {}
                items.append(item("zm:c:%s:%s" % (cfg["key"], pair), "report.zm.closed", "info", False, source=src,
                                  **{"from": names.get(a) or old_names.get(a) or "?", "to": names.get(b) or old_names.get(b) or "?"}))
        ext = zonefw.system_zone(st, "external")
        for pair in allowed:
            a, b = pair.split(">", 1)
            if ext and a == ext and b != ext:
                inbound += 1
                items.append(item("zm:in:%s:%s" % (cfg["key"], pair), "report.zm.inbound", "danger", True, source=src,
                                  zone=names.get(b, "?")))
    ctx.keep["zonematrix"] = now
    n_allowed = sum(len(v["allowed"]) for v in now.values())
    if first and not inbound:
        return section("zonematrix", "report.sec.zonematrix", "report.zm.baseline", items, n=n_allowed)
    if not items:
        return section("zonematrix", "report.sec.zonematrix", "report.zm.calm", items, n=n_allowed)
    if inbound:
        return section("zonematrix", "report.sec.zonematrix", "report.zm.summary_bad", items, n=opened, bad=inbound)
    return section("zonematrix", "report.sec.zonematrix", "report.zm.summary", items, n=opened)


register(Contribution("zonematrix", 54, "report.sec.zonematrix", _zm_available, _zm_build))


def _executor_configured(core) -> bool:
    return os.environ.get("LANLOOM_FW_EXECUTOR", "").strip().lower() == "external"


# Notification event (rules per person): a change on a real controller that was put back on its own (warning) or
# could not be put back (critical). Finished changes are not announced; they are in the nightly report.
notify_events.register(notify_events.make("fw_change", "changes", 45, "warning", available=_executor_configured))
