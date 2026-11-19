"""Nightly report section "Sites" (M1): with one combined report for the whole instance, the home report names every
further site with its state - devices online, new devices, internet, what stands out - and the points of its own
latest report. Only in the combined mode and only with more than one site.

The section is filtered for each reader when a report is shown (``filter_report``): a person who may not see a site
never gets its line, so names of other sites never reach an answer."""
from .nightly.contrib import Contribution, item, register, section

LEVEL_RANK = {"danger": 0, "warn": 1, "info": 2, "ok": 3}


def _hub(ctx):
    return getattr(ctx.core, "site_hub", None)


def _available(ctx) -> bool:
    hub = _hub(ctx)
    return (hub is not None and hub.several() and not getattr(ctx.core, "remote", False)
            and hub.report_mode() == "combined")


def _build(ctx) -> dict:
    hub = _hub(ctx)
    items = []
    for row in hub.rows():
        sid = row["id"]
        if sid == getattr(ctx.core, "site_id", "home"):
            continue
        core = hub.core(sid)
        if core is None:
            continue
        try:
            s = core.overview.summary(ctx.until)
        except Exception:
            s = {}
        devs = (s or {}).get("devices") or {}
        level = ((s or {}).get("health") or {}).get("level", "ok")
        wan = ((s or {}).get("wan") or {}).get("state", "unknown")
        try:
            latest = core.reports.latest()
        except Exception:
            latest = None
        points = int((latest or {}).get("points") or 0)
        lvl = "danger" if level == "danger" or wan == "down" else ("warn" if level == "warn" or points else "info")
        items.append(item("site:%s" % sid, "site.report.item", lvl, point=lvl in ("danger", "warn"), site=sid,
                          name=row["name"] or sid, online=int(devs.get("online") or 0), total=int(devs.get("total") or 0),
                          new=int(devs.get("new") or 0), wan_key="site.wan." + wan, points=points))
    bad = sum(1 for i in items if i["point"])
    key = "site.report.summary_bad" if bad else "site.report.summary"
    return section("sites", "site.report.title", key, items, n=len(items), bad=bad)


register(Contribution("sites", 2, "site.report.title", _available, _build))


def filter_report(report, allowed) -> dict:
    """The report as one reader may see it: lines of sites outside ``allowed`` (a list of site ids) are taken out."""
    if not isinstance(report, dict) or not report.get("sections"):
        return report
    allowed = set(allowed or ())
    out = dict(report)
    secs = []
    for sec in report["sections"]:
        if sec.get("id") != "sites":
            secs.append(sec)
            continue
        keep = [i for i in sec.get("items") or [] if (i.get("vars") or {}).get("site") in allowed]
        if keep:
            secs.append(dict(sec, items=keep, summary=dict(sec.get("summary") or {},
                                                          vars=dict((sec.get("summary") or {}).get("vars") or {}, n=len(keep),
                                                                    bad=sum(1 for i in keep if i.get("point"))))))
    out["sections"] = secs
    return out
