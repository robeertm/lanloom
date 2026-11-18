"""Pi-hole answers -> the neutral DNS filter reading (pure, no I/O). See ``lanloom/dnsfilter.py`` for the shape.

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import re

HISTORY_SLOT_S = 600
_ADDRESS = re.compile(r"\A[a-z][a-z0-9+.-]*://(?:[^/@?#]*@)?([^/:?#]+)(?::\d+)?(/[^?#]*)?")


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v >= 0 else default


def _list(d, key) -> list:
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, list) else []


def _hours(history) -> list:
    """The 10-minute slots of the last day summed per hour: [[hour, queries, blocked], ...]."""
    acc = {}
    for slot in _list(history, "history"):
        if not isinstance(slot, dict):
            continue
        ts = _num(slot.get("timestamp"), None)
        if ts is None:
            continue
        h = int(ts // 3600)
        q, b = acc.get(h, (0, 0))
        acc[h] = (q + int(_num(slot.get("total"))), b + int(_num(slot.get("blocked"))))
    return [[h, q, min(b, q)] for h, (q, b) in sorted(acc.items())]


def _clients(all_clients, blocked_clients) -> list:
    blocked = {}
    for c in _list(blocked_clients, "clients"):
        if isinstance(c, dict) and c.get("ip"):
            blocked[str(c["ip"])] = int(_num(c.get("count")))
    out = []
    for c in _list(all_clients, "clients"):
        if not isinstance(c, dict) or not c.get("ip"):
            continue
        ip = str(c["ip"])
        out.append({"ip": ip, "name": c.get("name") or "", "q": int(_num(c.get("count"))), "b": blocked.get(ip, 0)})
    return out


def _upstreams(data):
    """(list, ok): the real resolvers only (the answer also names the cache and the block list as pseudo-upstreams)."""
    out = []
    for u in _list(data, "upstreams"):
        if not isinstance(u, dict):
            continue
        port = u.get("port")
        if isinstance(port, int) and port < 0:
            continue
        name = str(u.get("name") or u.get("ip") or "")
        if str(u.get("ip") or "") in ("blocklist", "cache"):
            continue
        resp = ((u.get("statistics") or {}).get("response") if isinstance(u.get("statistics"), dict) else None)
        out.append({"name": name or str(u.get("ip") or ""), "n": int(_num(u.get("count"))),
                    "ms": round(_num(resp, None) * 1000, 1) if _num(resp, None) is not None else None})
    return out


def _lists(data) -> list:
    out = []
    for item in _list(data, "lists"):
        if not isinstance(item, dict) or item.get("type") not in (None, "block"):
            continue
        addr = str(item.get("address") or "")
        m = _ADDRESS.match(addr)                 # host and path only: never a user name, a query or a token
        name = (m.group(1) + (m.group(2)[-40:] if m.group(2) not in ("", "/") else "")) if m else addr[:60]
        out.append({"name": name[:80], "enabled": bool(item.get("enabled", True)), "entries": int(_num(item.get("number"))),
                    "updated": int(_num(item.get("date_updated"), 0)) or None})
    return out


def _version(data) -> str:
    core = (((data or {}).get("version") or {}).get("core") or {}).get("local") if isinstance(data, dict) else None
    return str((core or {}).get("version") or "")[:32]


def to_neutral(raw: dict) -> dict:
    summary = raw.get("summary") or {}
    q = summary.get("queries") or {}
    gravity = summary.get("gravity") or {}
    clients = summary.get("clients") or {}
    ups = _upstreams(raw.get("upstreams"))
    forwarded = int(_num(q.get("forwarded")))
    blocking = (raw.get("blocking") or {}).get("blocking") if isinstance(raw.get("blocking"), dict) else None
    lists = _lists(raw.get("lists"))
    upstream_ok = None                         # judged only when something was forwarded in the window
    if raw.get("upstreams") is not None and forwarded:
        servfail = int(_num((q.get("replies") or {}).get("SERVFAIL") if isinstance(q.get("replies"), dict) else 0))
        upstream_ok = any(u["ms"] for u in ups) and not (forwarded >= 50 and servfail > forwarded // 2)
    return {"v": 1, "family": "pihole", "version": _version(raw.get("version")), "running": True,
            "blocking": None if blocking in (None, "unknown") else blocking == "enabled", "window_s": 86400,
            "totals": {"queries": int(_num(q.get("total"))), "blocked": int(_num(q.get("blocked"))),
                       "clients": int(_num(clients.get("active"))), "list_entries": int(_num(gravity.get("domains_being_blocked")))},
            "hours": _hours(raw.get("history")),
            "top_blocked": [{"domain": d.get("domain"), "n": int(_num(d.get("count")))}
                            for d in _list(raw.get("top_blocked"), "domains") if isinstance(d, dict)],
            "top_clients": _clients(raw.get("clients"), raw.get("clients_blocked")),
            "upstreams": ups, "upstream_ok": upstream_ok, "lists": lists,
            "lists_updated": int(_num(gravity.get("last_update"), 0)) or None}
