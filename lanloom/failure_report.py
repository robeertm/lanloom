"""Nightly report section "Failure chains": what hung on the devices that went offline during the night, and manual
dependencies that point at something Lanloom no longer knows.

Rule based (lanloom/failure.py), from today's dependency graph: for every device that went offline in the report window
it names how many devices, hub devices and automations depended on it (sure or probably). A device that is still down
is a point to look at; one that came back is a note. A manual dependency whose other end is gone is a point as well
(the card lets you remove it). The section is left out when there is nothing of the kind. Texts are i18n keys."""
from .nightly.contrib import Contribution, _short, item, register, section

ID = "failure"


def _offline(ctx) -> list:
    """[(device id, title, time)] of the devices that went offline in the window (once each, the last time)."""
    seen = {}
    for ev in ctx.events("dev_offline"):
        d = ev.get("data") or {}
        if d.get("id"):
            seen[d["id"]] = (d.get("title") or "", ev["t"])
    return [(did, title, t) for did, (title, t) in sorted(seen.items(), key=lambda kv: kv[1][1])]


def _rows(ctx) -> tuple:
    fl = getattr(ctx.core, "failure", None)
    if fl is None:
        return [], []
    status = {d["id"]: d.get("status") for d in ctx.core.devices()}
    chains = []
    for did, title, t in _offline(ctx):
        res = fl.chain(did, "down")
        if res and res["counts"]["total"]:
            chains.append({"id": did, "title": res["root"]["title"] or title or res["root"].get("ip") or did, "t": t,
                           "counts": res["counts"], "down": status.get(did) == "down"})
    return chains, fl.graph().stale


def _available(ctx) -> bool:
    chains, stale = _rows(ctx)
    return bool(chains or stale)


def _build(ctx) -> dict:
    chains, stale = _rows(ctx)
    items = []
    for c in chains:
        n = c["counts"]
        key = "fail.report.down_now" if c["down"] else "fail.report.was_down"
        items.append(item("fc:%s:%d" % (c["id"], c["t"]), key, "warn" if c["down"] else "info", c["down"],
                          name=_short(c["title"]), n=n["total"], devices=n["devices"] + n["hub_devices"], autos=n["automations"],
                          at_at=c["t"]))
    for s in stale:
        items.append(item("fcs:%s:%s" % (s["node"], s["target"]), "fail.report.stale", "info", True, id=_short(s["missing"], 40)))
    if not chains:
        return section(ID, "fail.report.title", "fail.report.summary_stale", items, n=len(stale))
    return section(ID, "fail.report.title", "fail.report.summary", items, n=len(chains))


register(Contribution(ID, 26, "fail.report.title", _available, _build))
