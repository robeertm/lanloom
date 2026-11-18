"""Nightly report section "Web interfaces and services": what stopped answering, what answers with an error, what vanished.

Reads the state the service probe (services.py) keeps; nothing is probed for the report. A web interface that is dead or
answers 5xx is a point to look at; a service that is "Removed?" is a plain note (probably switched off on purpose). The
certificates have their own section ("Certificates"), fed by the same probe. Texts are i18n keys."""
from .nightly.contrib import Contribution, item, register, section


def _rows(ctx) -> list:
    services = getattr(ctx.core, "services", None)
    return services.listing() if services is not None else []


def _available(ctx) -> bool:
    return any(r["state"] not in ("unprobed", "unknown") for r in _rows(ctx))


def _build(ctx) -> dict:
    rows = [r for r in _rows(ctx) if r["state"] not in ("unprobed", "unknown")]
    items = []
    for r in rows:
        name = "%s · %s :%d" % (r["title"], r["name"], r["port"])
        if r["state"] == "dead":
            items.append(item("svc-dead:" + r["key"], "svc.report.dead", "warn", point=True, name=name))
        elif r["state"] == "error":
            items.append(item("svc-err:" + r["key"], "svc.report.error", "warn", point=True, name=name, code=r["status"] or 500))
        elif r["state"] == "removed":
            items.append(item("svc-rm:" + r["key"], "svc.report.removed", "info", point=False, name=name))
    bad = sum(1 for i in items if i["point"])
    if not items:
        return section("services", "svc.report.title", "svc.report.summary_calm", items, total=len(rows))
    return section("services", "svc.report.title", "svc.report.summary", items, total=len(rows), n=bad)


register(Contribution("services", 36, "svc.report.title", _available, _build))
