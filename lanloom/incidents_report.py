"""Nightly report section "Incidents of the night": the incident files that started or were still open in the report window.

Rule based (lanloom/incidents.py): one line per incident with its probable root, how many parts hung on it, when it
started and how long it lasted. An incident that is still open is a point to look at; an outage that ended is a point
when it took longer than ``POINT_S`` or took others with it, otherwise a note; blips, flapping and maintenance (own
updates, a maintenance window, marked by a person) are notes. The section is left out when the slots hold nothing yet or
there was no incident in the window. Texts are i18n keys."""
from .nightly.contrib import Contribution, _short, item, register, section

ID = "incidents"
POINT_S = 900


def _items(ctx) -> list:
    inc = getattr(ctx.core, "incidents", None)
    if inc is None or not ctx.core.availability.has_data():
        return []
    try:
        return inc.night(ctx.since, ctx.until)
    except Exception:
        return []


def _available(ctx) -> bool:
    return bool(_items(ctx))


def _name(i: dict) -> str:
    r = i["root"]
    if r["id"] == "lanloom":
        return "Lanloom"
    return _short(r.get("title") or r.get("ip") or r["id"])


def _build(ctx) -> dict:
    rows = _items(ctx)
    items, faults, maint = [], 0, 0
    for i in rows:
        k = i["kind"]
        n = i["counts"]["members"]
        if k == "maintenance":
            maint += 1
            why = i.get("why_kind") or "window"
            if why == "update":
                v = i["root"]["why"]["vars"]
                items.append(item("inc:" + i["id"], "inc.report.update", "info", False, from_at=i["start"], dur_dur=i["dur"],
                                  was=v.get("from") or "", to=v.get("to") or ""))
            else:
                items.append(item("inc:" + i["id"], "inc.report.maintenance", "info", False, name=_name(i), from_at=i["start"], dur_dur=i["dur"],
                                  why_key="inc.why_kind." + why))
            continue
        faults += 1
        guess = not i["root"].get("observed", True)
        if i["open"]:
            key = "inc.report.open_guess" if guess else "inc.report.open"
            items.append(item("inc:" + i["id"], key, "danger" if n else "warn", True, name=_name(i), n=n, from_at=i["start"], dur_dur=i["dur"]))
        elif k == "flapping":
            items.append(item("inc:" + i["id"], "inc.report.flapping", "info", False, name=_name(i), runs=i.get("runs", 3), from_at=i["start"],
                              dur_dur=i["dur"]))
        elif k == "blip":
            items.append(item("inc:" + i["id"], "inc.report.blip", "info", False, name=_name(i), from_at=i["start"], dur_dur=i["dur"]))
        else:
            big = i["dur"] >= POINT_S or n > 0
            key = "inc.report.outage_guess" if guess else ("inc.report.outage_n" if n else "inc.report.outage")
            items.append(item("inc:" + i["id"], key, "warn" if big else "info", big, name=_name(i), n=n, from_at=i["start"], to_at=i["end"],
                              dur_dur=i["dur"]))
    if not faults:
        return section(ID, "inc.report.title", "inc.report.summary_maint", items, n=maint)
    return section(ID, "inc.report.title", "inc.report.summary", items, n=faults, m=maint)


register(Contribution(ID, 25, "inc.report.title", _available, _build))
