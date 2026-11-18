"""Switch ports as a notification event and a nightly report section (S4, read only).

* Event ``port_unstable``: a port had three or more link drops or error episodes in 24 hours (the red level of the
  faceplate). Announced once while the condition lasts, with an all-clear when the port is calm again.
* Nightly section "Switch ports": the ports with events in the period of the report (three or more is a point to look
  at, one or two is a plain note) and how often wireless clients moved between access points.
"""
from . import portfront
from .nightly.contrib import Contribution, Ctx, item, register, section
from .notify import events as ev

ID = "ports"
MAX_PORTS = 8

ev.register(ev.make("port_unstable", "connections", 33, "warning", recovery=True,
                    available=lambda core: bool(getattr(getattr(core, "portwatch", None), "last", None))))


def names(core) -> dict:
    """{port id: "<equipment> port <n>"} for every switch the controller adapters read."""
    out = {}
    try:
        entries = core.live_configs()
    except Exception:
        return out
    for entry in entries:
        st = entry["state"]
        eq = {e["id"]: e.get("name") or "?" for e in st.get("equipment") or []}
        for p in st.get("ports") or []:
            if p.get("device") in eq:
                out[p["id"]] = (eq[p["device"]], p.get("name") or str(p.get("idx")))
    return out


def check(core, now: float) -> None:
    """Announces ports that turned red and clears the ones that calmed down (called after every port reading)."""
    watch, notify = core.portwatch, getattr(core, "notify", None)
    if notify is None:
        return
    label = None
    bad = set()
    for pid, flaps, errors in watch.problems(now):
        if portfront.level_for(flaps + errors) != "bad":
            continue
        bad.add(pid)
        if label is None:
            label = names(core)
        eq, port = label.get(pid, ("?", "?"))
        notify.emit("port_unstable", "port:%s" % pid, {"name": "%s · %s" % (eq, port), "n": flaps + errors}, t=now)
    for pid in list(getattr(core, "_port_open", set()) - bad):
        notify.emit("port_unstable", "port:%s" % pid, resolve=True, t=now)
    core._port_open = bad


def _available(ctx: Ctx) -> bool:
    w = ctx.core.portwatch
    return bool(w.last or w.events)


def _build(ctx: Ctx) -> dict:
    w, roam = ctx.core.portwatch, ctx.core.roamwatch
    label = names(ctx.core)
    rows = []
    for pid, evs in w.events.items():
        mine = [e for e in evs if ctx.since <= e[0] <= ctx.until]
        if mine:
            rows.append((pid, sum(1 for e in mine if e[1] == "flap"), sum(1 for e in mine if e[1] == "err")))
    rows.sort(key=lambda r: -(r[1] + r[2]))
    items = []
    for pid, flaps, errors in rows[:MAX_PORTS]:
        bad = portfront.level_for(flaps + errors) == "bad"
        eq, port = label.get(pid, ("?", "?"))
        items.append(item("port:%s" % pid, "report.ports.port", "danger" if bad else "warn", bad, name=eq, port=port, flaps=flaps, errors=errors))
    if len(rows) > MAX_PORTS:
        items.append(item("ports:more", "report.ports.more", "info", False, n=len(rows) - MAX_PORTS))
    moves = roam.total(ctx.until)
    if moves:
        items.append(item("ports:roam", "report.ports.roam", "info", False, n=moves))
    if not rows:
        return section(ID, "report.sec.ports", "report.ports.none", items)
    return section(ID, "report.sec.ports", "report.ports.summary", items, n=len(rows))


register(Contribution(ID, 49, "report.sec.ports", _available, _build))
