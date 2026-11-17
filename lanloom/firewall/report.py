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


def _executor_configured(core) -> bool:
    return os.environ.get("LANLOOM_FW_EXECUTOR", "").strip().lower() == "external"


# Notification event (rules per person): a change on a real controller that was put back on its own (warning) or
# could not be put back (critical). Finished changes are not announced; they are in the nightly report.
notify_events.register(notify_events.make("fw_change", "changes", 45, "warning", available=_executor_configured))
