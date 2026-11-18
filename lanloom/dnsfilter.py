"""DNS filters (stage A, read only): the neutral reading, sums per hour and day, the views and the findings.

A DNS filter is a local name server that blocks the names of trackers and advertising for the whole network. Its read
half (one package per product under ``integrations/``) only asks for **totals** - never for single queries - and hands
the core one neutral reading per poll::

    {"v": 1, "family": "<id>", "version": "1.0", "t": 1700000000.0, "running": True, "blocking": True,
     "window_s": 86400, "totals": {"queries": 12000, "blocked": 3100, "clients": 14, "list_entries": 91000},
     "hours": [[hour_number, queries, blocked], ...],      # hour_number = unix time // 3600
     "days": [[day_number, queries, blocked], ...],         # only when the filter reports no hours
     "top_blocked": [{"domain": "tracker.example", "n": 120}, ...],
     "top_clients": [{"ip": "192.0.2.10", "name": "laptop", "q": 900, "b": 210}, ...],   # b may be None
     "upstreams": [{"name": "192.0.2.53", "n": 5000, "ms": 12.5}, ...], "upstream_ok": True,
     "lists": [{"name": "list.example/hosts", "enabled": True, "entries": 91000, "updated": 1699000000}, ...],
     "lists_updated": 1699000000}

What is kept (privacy): per filter and hour the number of queries and blocked queries (35 days), per day the same sums
(400 days). Nothing per client, nothing per name: the top lists and the per-client counts live in memory only and are
the filter's own 24 hour figures as of the last poll. No query log is ever read (the read halves' allow-lists hold no
path of one; a static test checks it).
"""
from __future__ import annotations

import ipaddress
import re
import statistics
import time
from typing import Optional

HOUR_S = 3600
DAY_S = 86400
KEEP_HOUR_DAYS = 35
KEEP_DAYS = 400
MAX_TOP = 10
MAX_CLIENTS = 50
MAX_UPSTREAMS = 8
MAX_LISTS = 60
MAX_HOURS = 24 * 8
MAX_DAYS = 400
STALE_S = 900.0                    # no reading for 15 minutes: the filter is not answering
SHARE_MIN_QUERIES = 200            # below this a share says nothing
SHARE_MIN_DAYS = 5
SHARE_DIFF = 0.15
SILENT_HOURS = 3
SILENT_MIN_AVG = 20                # queries per hour on average before "no queries at all" is remarkable
LISTS_OLD_DAYS = 21
SCHEMA = """
CREATE TABLE IF NOT EXISTS dns_sum (integ INTEGER NOT NULL, hour INTEGER NOT NULL, q INTEGER NOT NULL, b INTEGER NOT NULL,
    PRIMARY KEY (integ, hour));
CREATE TABLE IF NOT EXISTS dns_day (integ INTEGER NOT NULL, day INTEGER NOT NULL, q INTEGER NOT NULL, b INTEGER NOT NULL,
    PRIMARY KEY (integ, day));
"""
_NAME_RX = re.compile(r"[^a-z0-9._\-]")


class ModelError(ValueError):
    pass


def _int(v, hi: int = 10 ** 12) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ModelError("number")
    if v != v or v < 0 or v > hi:
        raise ModelError("number range")
    return int(v)


def _opt_int(v):
    return None if v is None else _int(v)


def _text(v, n: int = 64) -> str:
    s = "".join(c for c in str(v or "") if ord(c) >= 32 and c != "\x7f").strip()
    return s[:n]


def _domain(v) -> str:
    s = _NAME_RX.sub("", str(v or "").strip().lower())
    return s[:253]


def _ip(v) -> str:
    try:
        return str(ipaddress.ip_address(str(v or "").strip()))
    except ValueError:
        return ""


def clean(raw) -> dict:
    """Checked, size-limited copy of a reading. Raises ModelError (the core then marks the source as a protocol error)."""
    if not isinstance(raw, dict) or raw.get("v") != 1:
        raise ModelError("shape")
    out = {"v": 1, "family": _text(raw.get("family"), 32), "version": _text(raw.get("version"), 32),
           "t": float(raw.get("t") or time.time()),
           "running": raw["running"] if isinstance(raw.get("running"), bool) else None,
           "blocking": raw["blocking"] if isinstance(raw.get("blocking"), bool) else None,
           "window_s": _int(raw.get("window_s") or DAY_S, 400 * DAY_S),
           "upstream_ok": raw["upstream_ok"] if isinstance(raw.get("upstream_ok"), bool) else None,
           "lists_updated": _opt_int(raw.get("lists_updated"))}
    tot = raw.get("totals") or {}
    out["totals"] = {k: _opt_int(tot.get(k)) for k in ("queries", "blocked", "clients", "list_entries")}
    for key, limit in (("hours", MAX_HOURS), ("days", MAX_DAYS)):
        rows = raw.get(key) or []
        if not isinstance(rows, list) or len(rows) > limit:
            raise ModelError(key)
        seen = {}
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) != 3:
                raise ModelError(key)
            n, q, b = _int(r[0], 10 ** 8), _int(r[1]), _int(r[2])
            seen[n] = (n, q, min(b, q))
        out[key] = [list(seen[n]) for n in sorted(seen)]
    out["top_blocked"] = [{"domain": _domain(r.get("domain")), "n": _int(r.get("n"))}
                          for r in (raw.get("top_blocked") or [])[:MAX_TOP] if isinstance(r, dict) and _domain(r.get("domain"))]
    clients = []
    for r in (raw.get("top_clients") or [])[:MAX_CLIENTS]:
        if not isinstance(r, dict) or not (_ip(r.get("ip")) or _text(r.get("name"))):
            continue
        clients.append({"ip": _ip(r.get("ip")), "name": _text(r.get("name"), 64), "q": _int(r.get("q")), "b": _opt_int(r.get("b"))})
    out["top_clients"] = clients
    out["upstreams"] = [{"name": _text(r.get("name"), 80), "n": _int(r.get("n")),
                         "ms": None if r.get("ms") is None else round(min(60000.0, max(0.0, float(r["ms"]))), 1)}
                        for r in (raw.get("upstreams") or [])[:MAX_UPSTREAMS] if isinstance(r, dict) and _text(r.get("name"), 80)]
    out["lists"] = [{"name": _text(r.get("name"), 80), "enabled": bool(r.get("enabled", True)), "entries": _opt_int(r.get("entries")),
                     "updated": _opt_int(r.get("updated"))}
                    for r in (raw.get("lists") or [])[:MAX_LISTS] if isinstance(r, dict) and _text(r.get("name"), 80)]
    return out


# ---------------------------------------------------------------------- sums per hour and day
class DnsLog:
    """Hourly and daily sums per filter (``dns_sum``, ``dns_day``). Idempotent: the filter's own hour buckets are
    written with MAX, so a poll that sees the same hour again, or a restart of the filter's counters, changes nothing
    wrongly."""

    def __init__(self, store):
        self.store = store
        with store.transaction() as tx:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    tx._db.execute(stmt)

    def record(self, integ: int, state: dict, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        days = set()
        with self.store.transaction() as tx:
            for h, q, b in state.get("hours") or []:
                tx._db.execute("INSERT INTO dns_sum(integ, hour, q, b) VALUES(?,?,?,?) ON CONFLICT(integ, hour) DO UPDATE SET "
                               "q=MAX(q, excluded.q), b=MAX(b, excluded.b)", (integ, h, q, b))
                days.add(h * HOUR_S // DAY_S)
            for d in sorted(days):
                tx._db.execute("INSERT OR REPLACE INTO dns_day(integ, day, q, b) SELECT ?, ?, COALESCE(SUM(q),0), COALESCE(SUM(b),0) "
                               "FROM dns_sum WHERE integ=? AND hour>=? AND hour<?",
                               (integ, d, integ, d * 24, d * 24 + 24))
            for d, q, b in state.get("days") or []:
                tx._db.execute("INSERT INTO dns_day(integ, day, q, b) VALUES(?,?,?,?) ON CONFLICT(integ, day) DO UPDATE SET "
                               "q=MAX(q, excluded.q), b=MAX(b, excluded.b)", (integ, d, q, b))
            tx._db.execute("DELETE FROM dns_sum WHERE integ=? AND hour < ?", (integ, int(now // HOUR_S) - KEEP_HOUR_DAYS * 24))
            tx._db.execute("DELETE FROM dns_day WHERE integ=? AND day < ?", (integ, int(now // DAY_S) - KEEP_DAYS))

    def hours(self, integ: int, n: int = 24, now: Optional[float] = None) -> list:
        """The last ``n`` hours up to and including the current one, oldest first, as [hour, queries, blocked]
        (hours without a row are zero)."""
        now = time.time() if now is None else now
        cur = int(now // HOUR_S)
        rows = {r["hour"]: (r["q"], r["b"]) for r in self.store.query(
            "SELECT hour, q, b FROM dns_sum WHERE integ=? AND hour>? AND hour<=?", (integ, cur - n, cur))}
        return [[h, *rows.get(h, (0, 0))] for h in range(cur - n + 1, cur + 1)]

    def days(self, integ: int, n: int = 14, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        cur = int(now // DAY_S)
        rows = {r["day"]: (r["q"], r["b"]) for r in self.store.query(
            "SELECT day, q, b FROM dns_day WHERE integ=? AND day>? AND day<=?", (integ, cur - n, cur))}
        return [[d, *rows.get(d, (0, 0))] for d in range(cur - n + 1, cur + 1)]

    def has_data(self, integ: int) -> bool:
        return self.store.one("SELECT 1 AS x FROM dns_sum WHERE integ=? LIMIT 1", (integ,)) is not None

    def forget(self, integ: int) -> None:
        with self.store.transaction() as tx:
            tx._db.execute("DELETE FROM dns_sum WHERE integ=?", (integ,))
            tx._db.execute("DELETE FROM dns_day WHERE integ=?", (integ,))


# ---------------------------------------------------------------------- findings (pure)
def share(q: int, b: int) -> Optional[float]:
    return None if not q else b / q


def share_shift(days: list, q: int, b: int) -> Optional[dict]:
    """The blocked share of the last 24 hours against the median share of the complete days before today.
    ``days`` is [[day, q, b], ...] of the complete days before today. None when nothing stands out or the data is thin."""
    ref = [b0 / q0 for _d, q0, b0 in days if q0 >= SHARE_MIN_QUERIES][-14:]
    if q < SHARE_MIN_QUERIES or len(ref) < SHARE_MIN_DAYS:
        return None
    usual, now_share = statistics.median(ref), b / q
    if abs(now_share - usual) < SHARE_DIFF:
        return None
    if now_share >= usual * 1.5:
        return {"dir": "high", "now": round(now_share * 100), "usual": round(usual * 100)}
    if now_share <= usual * 0.5:
        return {"dir": "low", "now": round(now_share * 100), "usual": round(usual * 100)}
    return None


def silent(hours: list, days: list) -> bool:
    """No query at all in the last complete hours although the filter usually answers some."""
    if len(hours) <= SILENT_HOURS:
        return False
    last = hours[-1 - SILENT_HOURS:-1]                       # complete hours; the current one is still filling
    usual = [q for _d, q, _b in days if q > 0][-7:]
    if not usual or sum(usual) / len(usual) / 24 < SILENT_MIN_AVG:
        return False
    return all(q == 0 for _h, q, _b in last)


def _finding(level: str, kind: str, **params) -> dict:
    """One finding: ``key`` is the text of the card, ``report`` the text of the night report (both written out, so a missing
    translation is found by the key check)."""
    texts = {"not_running": ("dns.finding.not_running", "report.dns.finding.not_running"),
             "protection_off": ("dns.finding.protection_off", "report.dns.finding.protection_off"),
             "upstream_down": ("dns.finding.upstream_down", "report.dns.finding.upstream_down"),
             "silent": ("dns.finding.silent", "report.dns.finding.silent"),
             "share_high": ("dns.finding.share_high", "report.dns.finding.share_high"),
             "share_low": ("dns.finding.share_low", "report.dns.finding.share_low"),
             "lists_old": ("dns.finding.lists_old", "report.dns.finding.lists_old")}
    key, report = texts[kind]
    return {"level": level, "kind": kind, "key": key, "report": report, "params": params}


def findings(state: dict, hours: list, days: list, now: Optional[float] = None) -> list:
    """What deserves a look, from one reading plus the stored sums. [{level, kind, key, report, params}], most important
    first. ``days`` holds the complete days before today."""
    now = time.time() if now is None else now
    out = []
    if state.get("running") is False:
        out.append(_finding("alert", "not_running"))
    elif state.get("blocking") is False:
        out.append(_finding("warn", "protection_off"))
    if state.get("upstream_ok") is False:
        out.append(_finding("warn", "upstream_down"))
    if silent(hours, days):
        out.append(_finding("warn", "silent", hours=SILENT_HOURS))
    tot = state.get("totals") or {}
    shift = share_shift(days, tot.get("queries") or 0, tot.get("blocked") or 0)
    if shift:
        out.append(_finding("warn", "share_high" if shift["dir"] == "high" else "share_low", now=shift["now"], usual=shift["usual"]))
    upd = state.get("lists_updated")
    if upd and now - upd > LISTS_OLD_DAYS * DAY_S:
        out.append(_finding("info", "lists_old", days=int((now - upd) // DAY_S)))
    return out


# ---------------------------------------------------------------------- views
def _devices_by_ip(core) -> dict:
    out = {}
    for ip, did in core.ip_to_device.items():
        out[ip] = did
    return out


def health(core, integ_id: int, entry: Optional[dict], now: float) -> str:
    """ok | stale (a reading exists but is old) | down (source reports a failure) | waiting (nothing yet)."""
    st = core.source_status.get("int:%d" % integ_id) or {}
    if entry is None:
        return "down" if st and not st.get("ok") else "waiting"
    if now - entry["t"] > STALE_S or (st and not st.get("ok")):
        return "down" if st and not st.get("ok") else "stale"
    return "ok"


def filter_view(core, integ_id: int, now: Optional[float] = None) -> Optional[dict]:
    """One filter for the map card, the leitstand and the nightly report. None when ``integ_id`` is no DNS filter."""
    now = time.time() if now is None else now
    integ = next((i for i in core.integrations() if i["id"] == integ_id), None)
    if integ is None or integ["kind"] not in core.dns_kinds():
        return None
    entry = core.dns.get(integ_id)
    state = entry["state"] if entry else None
    hrs = core.dns_log.hours(integ_id, 24, now)
    days = core.dns_log.days(integ_id, 15, now)
    before = [d for d in days if d[0] < int(now // DAY_S)]
    out = {"integ": integ_id, "name": integ["name"], "family": integ["kind"], "health": health(core, integ_id, entry, now),
           "age_s": round(now - entry["t"]) if entry else None, "hours": hrs, "days": days}
    if not state:
        out.update({"totals": None, "findings": [], "top_blocked": [], "top_clients": [], "upstreams": [], "lists": []})
        return out
    tot = state["totals"]
    by_ip = _devices_by_ip(core)
    titles = {d["id"]: d.get("title") or d.get("ip") or d["id"] for d in core.devices()} if state["top_clients"] else {}
    clients = []
    for c in state["top_clients"][:MAX_CLIENTS]:
        did = by_ip.get(c["ip"])
        clients.append({"ip": c["ip"], "name": c["name"], "q": c["q"], "b": c["b"], "device": did,
                        "title": titles.get(did) if did else (c["name"] or c["ip"])})
    out.update({"version": state["version"], "running": state["running"], "blocking": state["blocking"],
                "window_s": state["window_s"], "totals": dict(tot, share=share(tot.get("queries") or 0, tot.get("blocked") or 0)),
                "top_blocked": state["top_blocked"], "top_clients": clients, "upstreams": state["upstreams"],
                "upstream_ok": state["upstream_ok"], "lists": state["lists"], "lists_updated": state["lists_updated"],
                "findings": findings(state, hrs, before, now)})
    return out


def client_view(core, device_id: str, now: Optional[float] = None) -> Optional[dict]:
    """The DNS section of a device's card: how much of the filter's traffic it causes and how much of it is blocked.
    None when no filter has seen the device among its top clients (or the device runs the filter itself)."""
    now = time.time() if now is None else now
    rows = []
    for integ_id, entry in core.dns.items():
        state = entry["state"]
        for c in state["top_clients"]:
            if c["ip"] and core.ip_to_device.get(c["ip"]) == device_id:
                tot = state["totals"].get("queries") or 0
                rows.append({"integ": integ_id, "name": next((i["name"] for i in core.integrations() if i["id"] == integ_id), ""),
                             "q": c["q"], "b": c["b"], "share_of_all": round(c["q"] / tot, 3) if tot else None,
                             "blocked_share": share(c["q"], c["b"]) if c["b"] is not None else None,
                             "window_s": state["window_s"], "age_s": round(now - entry["t"])})
    return {"filters": rows} if rows else None


def overview(core, now: Optional[float] = None) -> list:
    """All filters in short, for the leitstand's DNS card and the palette. ``component`` is the map component whose card
    shows the filter (the device it runs on, or a component of its own)."""
    from . import mapmodel
    now = time.time() if now is None else now
    comps = mapmodel._special_components(core, mapmodel._visible_devices(core))["dns"]
    out = []
    for integ in core.integrations():
        if integ["kind"] not in core.dns_kinds():
            continue
        v = filter_view(core, integ["id"], now)
        if v is None:
            continue
        tot = v.get("totals") or {}
        out.append({"integ": v["integ"], "name": v["name"], "family": v["family"], "health": v["health"], "component": comps.get(integ["id"]),
                    "queries": tot.get("queries"), "blocked": tot.get("blocked"), "share": tot.get("share"),
                    "clients": tot.get("clients"), "findings": [f["key"] for f in v["findings"]],
                    "hours": [[h, q, b] for h, q, b in v["hours"]]})
    return out


def metric_of(entry: Optional[dict]) -> Optional[str]:
    """The short figure under the filter's component on the map: its blocked share over 24 hours."""
    if not entry:
        return None
    tot = entry["state"]["totals"]
    s = share(tot.get("queries") or 0, tot.get("blocked") or 0)
    return None if s is None else "%d %%" % round(s * 100)
