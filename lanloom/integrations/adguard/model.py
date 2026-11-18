"""AdGuard Home answers -> the neutral DNS filter reading (pure, no I/O). See ``lanloom/dnsfilter.py`` for the shape.

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import ipaddress
from datetime import datetime


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v >= 0 else default


def _series(stats: dict, key: str) -> list:
    v = stats.get(key)
    return [_num(x) for x in v] if isinstance(v, list) else []


def _pairs(stats: dict, key: str) -> list:
    """AdGuard's top lists are lists of one-entry objects: [{"name": count}, ...]."""
    out = []
    for item in stats.get(key) or []:
        if isinstance(item, dict):
            for k, v in item.items():
                out.append((str(k), _num(v)))
    return out


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _buckets(stats: dict, now: float):
    """(hours, days): the counters of the statistics window as [[number, queries, blocked], ...]."""
    unit = 3600 if stats.get("time_units") != "days" else 86400
    q = _series(stats, "dns_queries")
    b = _series(stats, "blocked_filtering")
    extra = [_series(stats, k) for k in ("replaced_safebrowsing", "replaced_parental")]
    if not q:
        return [], []
    cur = int(now // unit)
    rows = []
    for i, n in enumerate(q):
        blocked = (b[i] if i < len(b) else 0) + sum(e[i] for e in extra if i < len(e))
        rows.append([cur - (len(q) - 1 - i), int(n), int(min(blocked, n))])
    return (rows, []) if unit == 3600 else ([], rows)


def _updated(text) -> int:
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def to_neutral(raw: dict, now: float) -> dict:
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    filt = raw.get("filtering") if isinstance(raw.get("filtering"), dict) else {}
    hours, days = _buckets(stats, now)
    queries = int(_num(stats.get("num_dns_queries")))
    blocked = int(_num(stats.get("num_blocked_filtering")) + _num(stats.get("num_replaced_safebrowsing"))
                  + _num(stats.get("num_replaced_parental")))
    clients = []
    for key, n in _pairs(stats, "top_clients"):
        ip = key if _is_ip(key) else ""
        clients.append({"ip": ip, "name": "" if ip else key, "q": int(n), "b": None})
    resp = dict(_pairs(stats, "top_upstreams_responses"))
    avg = dict(_pairs(stats, "top_upstreams_avg_time"))
    ups = [{"name": name, "n": int(n), "ms": round(avg[name] * 1000, 1) if name in avg else None} for name, n in resp.items()]
    forwarded = max(0, queries - blocked)
    lists, newest = [], 0
    for f in filt.get("filters") or []:
        if not isinstance(f, dict):
            continue
        upd = _updated(f.get("last_updated"))
        enabled = bool(f.get("enabled", True))
        if enabled:
            newest = max(newest, upd)
        lists.append({"name": str(f.get("name") or "list")[:80], "enabled": enabled, "entries": int(_num(f.get("rules_count"))),
                      "updated": upd or None})
    window = (len(_series(stats, "dns_queries")) or 24) * (3600 if stats.get("time_units") != "days" else 86400)
    return {"v": 1, "family": "adguard", "version": str(status.get("version") or "")[:32],
            "running": status.get("running") if isinstance(status.get("running"), bool) else None,
            "blocking": status.get("protection_enabled") if isinstance(status.get("protection_enabled"), bool) else None,
            "window_s": min(window, 400 * 86400), "totals": {"queries": queries, "blocked": blocked, "clients": len(clients) or None,
                                                          "list_entries": sum(l["entries"] for l in lists if l["enabled"]) or None},
            "hours": hours, "days": days,
            "top_blocked": [{"domain": k, "n": int(n)} for k, n in _pairs(stats, "top_blocked_domains")],
            "top_clients": clients, "upstreams": ups,
            "upstream_ok": (bool(sum(u["n"] for u in ups)) if forwarded >= 20 and "top_upstreams_responses" in stats else None),
            "lists": lists, "lists_updated": newest or None}
