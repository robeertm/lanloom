"""Sample services for the demo: every case of the Links view and the certificate clock appears once, nothing is probed.

The answers are invented but stable (they follow the order of the services, not chance): most interfaces answer 200 or a
redirect, two want a login (401: alive), one answers 503 (red), one is dead (not clickable), one service is "Removed?",
and the certificates run from "fine" through "hint", "yellow" and "red" to "expired"."""
import time

from . import services as svc_mod

REMOVED_DAYS = 2


def simulate(services, now: float = None) -> int:
    now = time.time() if now is None else now
    eps = [ep for ep in services.endpoints() if ep["probe"]]
    web = [ep for ep in eps if ep["svc"]["web"] or ep["svc"]["candidate"]]
    other = [ep for ep in eps if ep not in web]
    results, special = [], {}
    tls_i = 0
    for ep in eps:
        svc = ep["svc"]
        is_web = ep in web
        i = web.index(ep) if is_web else other.index(ep)
        kind, status = "ok", 200
        if is_web:
            if i % 9 == 3:
                kind, status = "error", 503
            elif i % 9 in (1, 6):
                kind, status = "auth", 401
            elif i % 4 == 2:
                status = 302
            if i % 9 == 7:
                kind, status = "miss", 0
                special[ep["key"]] = "dead"
        elif i == 2:
            kind, status = "miss", 0
            special[ep["key"]] = "removed"
        res = {"kind": kind, "status": status, "ms": 6 + (i * 7) % 48}
        if svc["tls"] and kind != "miss":
            days = [11, -3, 25, 2][tls_i] if tls_i < 4 else 90 + (tls_i * 37) % 260
            res["cert"] = {"days": days, "not_after": int(now + days * 86400), "issuer": "Home CA" if tls_i % 2 else "",
                           "subject": ep["title"], "self_signed": tls_i % 2 == 0}
            if not res["cert"]["issuer"]:
                res["cert"]["issuer"] = ep["title"]
            tls_i += 1
        results.append(res)
    services.apply(eps, results, now, book=False)
    for key, how in special.items():
        rec = services.state.get(key) or {}
        rec.update(fails=5, miss_s=svc_mod.REMOVED_AFTER_S + 3600 if how == "removed" else 600.0, t=now,
                   state="removed" if how == "removed" else "dead")
    services.version += 1
    return len(eps)
