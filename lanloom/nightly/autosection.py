"""Nightly report section "Automations" of the hub bridge (one per home-automation hub connection).

Shown only when the hub is connected and there is something to judge: flows of automations (admin mode), the states of
the hub's integrations, or unavailable entities. From the rules of the Automations tab (``hubauto``), judged at the
end of the report window:

* automations that name an entity the hub does not have (a point);
* two automations that set the same entity to opposite states within a minute, during the night (a point);
* integrations that fail to start or keep retrying (a point);
* unavailable entities, bundled per domain (a point from three on, otherwise a note);
* automations that switch what starts them, and ``single`` automations that drop starts while they wait (notes);
* automations that have not run for 30 days (one note).

Nothing is healed or changed: the section only says what the hub shows. The summary has counts only - the AI features
see this section only as its summary (``PRIVATE_SECTIONS``), because names of automations may name people.
"""
from __future__ import annotations

from .. import hubauto
from .contrib import Contribution, Ctx, _short, item, register, section

ID = "auto"
MAX_NAMES = 3


def _hubs(ctx: Ctx) -> list:
    return [i for i in ctx.core.integrations() if i.get("enabled") and i["kind"] == "hub"]


def _ctx(ctx: Ctx, integ: dict):
    entry = ctx.core.hub.get(integ["id"]) or {}
    if not entry.get("snapshot"):
        return None
    return hubauto.Ctx(entry, ctx.until)


def _has_source(a) -> bool:
    return bool(a and (a.flows or a.snap.get("entries") is not None or
                       any(a.state(e) == "unavailable" for e in a.ents)))


def _available(ctx: Ctx) -> bool:
    return any(_has_source(_ctx(ctx, i)) for i in _hubs(ctx))


def _names(names) -> str:
    names = [_short(n, 40) for n in names]
    return ", ".join(names[:MAX_NAMES]) + (" …" if len(names) > MAX_NAMES else "")


def _section(ctx: Ctx, integ: dict) -> dict | None:
    a = _ctx(ctx, integ)
    if not _has_source(a):
        return None
    iid = integ["id"]
    items = []
    rows = hubauto.rows(a)
    never, notes = [], 0
    for r in rows:
        for f in r["findings"]:
            if f["code"] == "dead":
                items.append(item("auto:%d:dead:%s" % (iid, r["id"]), "report.auto.dead", "warn", True,
                                  name=_short(r["name"]), names=_names(f["ids"]), n=f["n"]))
            elif f["code"] in ("self_trigger", "single_wait") and notes < 4:
                notes += 1
                items.append(item("auto:%d:%s:%s" % (iid, f["code"], r["id"]), "report.auto." + f["code"], "info", False,
                                  name=_short(r["name"])))
            elif f["code"] == "never":
                never.append(r["name"])
    for c in hubauto.conflicts(a, since=ctx.since):
        items.append(item("auto:%d:conflict:%s:%s:%s" % (iid, c["entity"], c["a"], c["b"]), "report.auto.conflict", "warn", True,
                          entity=_short(c["name"]), a=_short(c["a_name"]), b=_short(c["b_name"]), n=c["n"], last_at=c["last"]))
    health = hubauto.health(a)
    for p in health["integrations"]["problems"]:
        items.append(item("auto:%d:integration:%s:%s" % (iid, p["domain"], p["state"]), "report.auto.integration", "warn", True,
                          domain=p["domain"], state_key="report.auto.entry." + p["state"], n=p["n"]))
    for g in health["unavailable"]:
        items.append(item("auto:%d:unavailable:%s" % (iid, g["domain"]), "report.auto.unavailable", "warn" if g["n"] >= 3 else "info",
                          g["n"] >= 3, domain=g["domain"], n=g["n"], names=_names(i["name"] for i in g["items"])))
    if never:
        items.append(item("auto:%d:never" % iid, "report.auto.never", "info", False, n=len(never), names=_names(never),
                          days=hubauto.NEVER_DAYS))
    conflicts = len(hubauto.conflicts(a, since=ctx.since))
    parts = [{"key": "report.auto.part." + k, "vars": {"n": v}} for k, v in (
        ("findings", sum(1 for r in rows if r["findings"])), ("conflicts", conflicts),
        ("integrations", len(health["integrations"]["problems"])), ("unavailable", health["unavailable_total"])) if v]
    autos = sum(1 for r in rows if r["kind"] == "automation")
    if not a.flows:
        return section(ID, "report.sec.auto", "report.auto.summary_basic", items, parts=parts, name=integ["name"],
                       n=autos, entities=len(a.ents))
    return section(ID, "report.sec.auto", "report.auto.summary", items, parts=parts, name=integ["name"],
                   n=autos, exact=len(a.flows))


def _build(ctx: Ctx) -> list:
    return [s for s in (_section(ctx, i) for i in _hubs(ctx)) if s]


register(Contribution(ID, 47, "report.sec.auto", _available, _build))
