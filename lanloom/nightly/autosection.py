"""Nightly report section "Automations" of the hub bridge (one per home-automation hub connection).

Shown only when the hub is connected and there is something to judge: flows of automations (admin mode), the states of
the hub's integrations, or unavailable entities. From the rules of the Automations tab (``hubauto``), judged at the
end of the report window:

* automations that name an entity the hub does not have (a point);
* two automations that set the same entity to opposite states within a minute, during the night (a point);
* automations and scripts with runs that ended with an error during the night (admin mode; a point each, five at most
  by name, the rest counted; the short error text has entity ids replaced by display names, see
  ``hub_privacy.error_text``; one summary read from the hub per report, never per automation);
* integrations that fail to start or keep retrying (a point);
* unavailable entities, bundled per domain (a point from three on, otherwise a note);
* automations that switch what starts them, and ``single`` automations that drop starts while they wait (notes);
* automations that have not run for 30 days (one note).

Only news is a point: a finding of the rules (dead references, self triggers, ``single`` with waiting, unavailable
bundles, integrations with errors) is a point the first time it shows. When the next report has it again it is only
counted in one calm note "still open since <date>". A finding that was gone for 36 hours counts as new when it comes
back. The first report after the update (and the first one of a hub) takes everything it finds as known and says so in
one note. The small register (finding key, first and last seen) lives in the report's ``keep`` per site and goes with
the archive. Conflicts of the night, runaways (hub section) and failed runs are events of the night, not states: they
stay points every night.

Nothing is healed or changed: the section only says what the hub shows. The summary has counts only - the AI features
see this section only as its summary (``PRIVATE_SECTIONS``), because names of automations may name people.
"""
from __future__ import annotations

import copy
import hashlib
from urllib.parse import quote

from .. import hubauto, hubbridge
from .contrib import Contribution, Ctx, _short, item, register, section

ID = "auto"
MAX_NAMES = 3
MAX_RUN_NAMED = 5               # automations named with failed runs; the rest is counted
MAX_NOTES = 4                   # notes of new self triggers / waiting singles per hub
KNOWN_FOR_S = 36 * 3600         # a finding that was not seen for this long is new again
FRESH_S = 1800                  # the summary of failed runs must have been read this close to the end of the window
KEEP_ID = "auto"                # report["keep"]["auto"] = {hub id: {finding key: [first seen, last seen]}}


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


def _href(iid, eid) -> str:
    return "#/automations/%s/item/%s" % (iid, quote(str(eid), safe=""))


def _finding_items(a, rows, health, iid) -> tuple[list, list]:
    """The findings of the rules as ``[(key, item)]`` (candidates for "new") and the names of never-run automations."""
    cands, never = [], []
    for r in rows:
        for f in r["findings"]:
            if f["code"] == "dead":
                it = item("auto:%d:dead:%s" % (iid, r["id"]), "report.auto.dead", "warn", True,
                          name=_short(r["name"]), names=_names(f["ids"]), n=f["n"])
                digest = hashlib.sha1(",".join(sorted(f["ids"])).encode()).hexdigest()[:10]
                cands.append(("dead:%s:%s" % (r["id"], digest), it, r["id"]))
            elif f["code"] in ("self_trigger", "single_wait"):
                it = item("auto:%d:%s:%s" % (iid, f["code"], r["id"]), "report.auto." + f["code"], "info", False,
                          name=_short(r["name"]))
                cands.append(("%s:%s" % (f["code"], r["id"]), it, r["id"]))
            elif f["code"] == "never":
                never.append(r["name"])
    for p in health["integrations"]["problems"]:
        it = item("auto:%d:integration:%s:%s" % (iid, p["domain"], p["state"]), "report.auto.integration", "warn", True,
                  domain=p["domain"], state_key="report.auto.entry." + p["state"], n=p["n"])
        cands.append(("integration:%s:%s" % (p["domain"], p["state"]), it, None))
    for g in health["unavailable"]:
        it = item("auto:%d:unavailable:%s" % (iid, g["domain"]), "report.auto.unavailable", "warn" if g["n"] >= 3 else "info",
                  g["n"] >= 3, domain=g["domain"], n=g["n"], names=_names(i["name"] for i in g["items"]))
        cands.append(("unavailable:%s" % g["domain"], it, None))
    return cands, never


def _only_news(ctx: Ctx, iid: int, cands: list) -> tuple[list, int, float | None, int]:
    """Split the candidates into news (items) and what stays open. Updates the register in ``ctx.keep`` (the new state
    of this report; the previous report's register is never touched). Returns (new items, ongoing count, oldest first-seen
    of the ongoing ones, count taken as known in the baseline)."""
    prev = ((ctx.prev or {}).get("keep") or {}).get(KEEP_ID)
    reg = ctx.keep.get(KEEP_ID)
    if reg is None:
        reg = ctx.keep[KEEP_ID] = copy.deepcopy(prev) if isinstance(prev, dict) else {}
    mine = reg.get(str(iid)) if isinstance(reg.get(str(iid)), dict) else {}
    # the first report after the update has no register yet: everything found counts as known (a report that is the very
    # first one, or the first one of a newly added hub, lists what it finds as new)
    baseline = bool(ctx.prev) and not isinstance(prev, dict)
    seen = {}
    fresh, ongoing, since, notes = [], 0, None, 0
    for key, it, eid in cands:
        rec = mine.get(key)
        if baseline:
            seen[key] = [ctx.until, ctx.until]
        elif isinstance(rec, list) and len(rec) == 2 and ctx.until - rec[1] <= KNOWN_FOR_S:
            seen[key] = [rec[0], ctx.until]
            ongoing += 1
            since = rec[0] if since is None else min(since, rec[0])
        else:
            seen[key] = [ctx.until, ctx.until]
            if not it["point"]:
                notes += 1
                if notes > MAX_NOTES:
                    continue
            if eid:
                it["href"] = _href(iid, eid)
            fresh.append(it)
    for key, rec in mine.items():       # gone for a while but not yet 36 h: keep the record (it may come back tonight)
        if key not in seen and isinstance(rec, list) and len(rec) == 2 and ctx.until - rec[1] <= KNOWN_FOR_S:
            seen[key] = rec
    reg[str(iid)] = seen
    return fresh, ongoing, since, len(cands) if baseline else 0


def _run_items(ctx: Ctx, a, entry: dict, iid: int, rows: list) -> tuple[list, int]:
    """Failed runs of the night: a point per automation (five by name), the rest in one point; or one calm note when the
    summary could not be read. Returns (items, number of automations with failed runs)."""
    status = entry.get("run_status") or {}
    summary = entry.get("run_summary") or {}
    fresh = abs((summary.get("t") or 0) - ctx.until) <= FRESH_S
    items = []
    if status and status.get("key") not in (None, "ok") and abs((status.get("t") or 0) - ctx.until) <= FRESH_S \
            and not (fresh and not summary.get("error")):
        items.append(item("auto:%d:runs_unavailable" % iid, "report.auto.runs_unavailable", "info", False))
        return items, 0
    if not fresh:
        return items, 0
    names = {r["id"]: r["name"] for r in rows}
    failed = hubbridge.failed_runs(entry, ctx.since, ctx.until)
    order = sorted(failed, key=lambda e: (-len(failed[e]), -failed[e][0]["at"], e))
    for eid in order[:MAX_RUN_NAMED]:
        runs = failed[eid]
        text = next((r["text"] for r in runs if r.get("text")), None)
        name = _short(names.get(eid) or eid)
        it = item("auto:%d:runfail:%s" % (iid, eid), "report.auto.run_failed" if text else "report.auto.run_failed_notext",
                  "warn", True, name=name, n=len(runs), last_at=runs[0]["at"], **({"text": text} if text else {}))
        it["href"] = _href(iid, eid)
        items.append(it)
    rest = order[MAX_RUN_NAMED:]
    if rest:
        items.append(item("auto:%d:runfail_more" % iid, "report.auto.run_failed_more", "warn", True, n=len(rest),
                          names=_names(names.get(e) or e for e in rest)))
    return items, len(order)


def _section(ctx: Ctx, integ: dict) -> dict | None:
    a = _ctx(ctx, integ)
    if not _has_source(a):
        return None
    iid = integ["id"]
    entry = ctx.core.hub.get(iid) or {}
    rows = hubauto.rows(a)
    health = hubauto.health(a)
    cands, never = _finding_items(a, rows, health, iid)
    items, ongoing, since, baseline = _only_news(ctx, iid, cands)
    conflicts = hubauto.conflicts(a, since=ctx.since)
    for c in conflicts:
        items.append(item("auto:%d:conflict:%s:%s:%s" % (iid, c["entity"], c["a"], c["b"]), "report.auto.conflict", "warn", True,
                          entity=_short(c["name"]), a=_short(c["a_name"]), b=_short(c["b_name"]), n=c["n"], last_at=c["last"]))
    run_items, run_autos = _run_items(ctx, a, entry, iid, rows)
    items.extend(run_items)
    if never:
        items.append(item("auto:%d:never" % iid, "report.auto.never", "info", False, n=len(never), names=_names(never),
                          days=hubauto.NEVER_DAYS))
    if ongoing:
        items.append(item("auto:%d:ongoing" % iid, "report.auto.ongoing", "info", False, n=ongoing, since_at=since))
    if baseline:
        items.append(item("auto:%d:baseline" % iid, "report.auto.baseline", "info", False, n=baseline))
    parts = [{"key": "report.auto.part." + k, "vars": {"n": v}} for k, v in (
        ("findings", sum(1 for r in rows if r["findings"])), ("conflicts", len(conflicts)), ("failed_runs", run_autos),
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
