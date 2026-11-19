"""Nightly report section "Resilience": only when there is something to tell - the score moved by five points or more since the
last report day, or a part is at the highest risk level (with what would help most). A calm night leaves the section out.
Rule based (lanloom/resilience.py); texts are i18n keys with variables."""
from datetime import datetime

from .nightly.contrib import Contribution, _short, item, register, section

ID = "resilience"
MOVE = 5


def _rows(ctx) -> list:
    rs = getattr(ctx.core, "resilience", None)
    if rs is None:
        return []
    try:
        res = rs.result(ctx.until)
    except Exception:
        return []
    sc = res.get("score")
    if not sc:
        return []
    items = []
    today = datetime.fromtimestamp(ctx.until).strftime("%Y-%m-%d")
    before = [h for h in rs.history() if h[0] < today]
    if before and abs(sc["score"] - before[-1][1]) >= MOVE:
        down = sc["score"] < before[-1][1]
        items.append(item("rs-score:%s" % today, "rs.report.score_down" if down else "rs.report.score_up", "warn" if down else "info", down,
                          score=sc["score"], grade=sc["grade"], old=int(before[-1][1])))
    for w in res.get("weak_spots") or []:
        if w["level"] != "critical":
            continue
        rec = (w.get("rec") or {}).get("key") or ""
        items.append(item("rs-crit:%s" % w["id"], "rs.report.critical", "warn", True, name=_short(w["title"] or w["id"]), risk=int(round(w["risk"])),
                          rec_key="rs.rec." + rec if rec else "rs.rec.none"))
    return items


def _available(ctx) -> bool:
    return bool(_rows(ctx))


def _build(ctx) -> dict:
    items = _rows(ctx)
    sc = ctx.core.resilience.result(ctx.until)["score"]
    return section(ID, "rs.report.title", "rs.report.summary", items, n=sum(1 for i in items if i["point"]), score=sc["score"], grade=sc["grade"])


register(Contribution(ID, 27, "rs.report.title", _available, _build))
