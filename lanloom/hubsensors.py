"""Sensor views of the hub bridge (core side): the sensors list, one entity's course and the live ring.

* The **live ring** keeps the state changes of the last hour per entity in memory (``RING_S``, at most ``RING_MAX``
  per entity and ``RING_ENTITIES`` entities). It feeds the "1 h" range of the sensor page without asking the hub.
* The ranges **24 h / 7 days / 30 days** come from the hub's recorder (state changes) or its long-term statistics
  (min/mean/max per hour), asked on request only and thinned out here, at the server: numbers into at most
  ``BUCKETS`` buckets with minimum, mean and maximum (the band of the chart), states into at most ``MAX_SEGMENTS``
  segments (the state bar; when a bucket holds several states, the one that lasted longest wins).
"""
from __future__ import annotations

import math
import time

from . import hub_privacy as privacy

RING_S = 3600.0
RING_MAX = 240
RING_ENTITIES = 4000
BUCKETS = 240
MAX_SEGMENTS = 400
RANGES = ("1h", "24h", "7d", "30d")
SPAN_S = {"1h": 3600.0, "24h": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0}
NO_VALUE = ("unknown", "unavailable", "", None)
MAX_LIST = 20000


def num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def is_numeric(entity: dict, state) -> bool:
    if (entity or {}).get("domain") in ("sensor", "number", "input_number") and (entity.get("unit") or num(state) is not None):
        return num(state) is not None or state in NO_VALUE
    return False


# ---------------------------------------------------------------------- live ring
def ring_push(entry: dict, entity_id: str, state, t: float) -> None:
    ring = entry.setdefault("ring", {})
    lst = ring.get(entity_id)
    if lst is None:
        if len(ring) >= RING_ENTITIES:
            return
        lst = ring[entity_id] = []
    lst.append((float(t), state))
    cut = t - RING_S
    while lst and (lst[0][0] < cut or len(lst) > RING_MAX):
        lst.pop(0)


def ring_of(entry: dict, entity_id: str, now: float) -> list:
    return [(t, s) for t, s in (entry.get("ring") or {}).get(entity_id, []) if t >= now - RING_S]


# ---------------------------------------------------------------------- thinning out
def compress_numeric(points: list, t0: float, t1: float, n: int = BUCKETS) -> list:
    """[(t, value)] -> [[t, min, mean, max]] in at most ``n`` buckets (values outside [t0, t1] dropped)."""
    if t1 <= t0:
        return []
    width = (t1 - t0) / n
    acc = {}
    for t, v in points:
        v = num(v)
        if v is None or t < t0 or t > t1:
            continue
        b = min(n - 1, int((t - t0) / width))
        a = acc.get(b)
        if a is None:
            acc[b] = [v, v, v, 1]
        else:
            a[0] = min(a[0], v)
            a[1] += v
            a[2] = max(a[2], v)
            a[3] += 1
    return [[round(t0 + (b + 0.5) * width, 1), a[0], round(a[1] / a[3], 4), a[2]] for b, a in sorted(acc.items())]


def compress_stats(rows: list, t0: float, t1: float, n: int = BUCKETS) -> list:
    """[[t, min, mean, max]] (hourly statistics) -> at most ``n`` buckets, mean weighted by the hours inside."""
    if t1 <= t0:
        return []
    width = (t1 - t0) / n
    acc = {}
    for t, lo, mean, hi in rows:
        if t < t0 - 3600 or t > t1:
            continue
        b = max(0, min(n - 1, int((t - t0) / width)))
        a = acc.get(b)
        if a is None:
            acc[b] = [lo, mean, hi, 1]
        else:
            a[0] = min(a[0], lo)
            a[1] += mean
            a[2] = max(a[2], hi)
            a[3] += 1
    return [[round(t0 + (b + 0.5) * width, 1), a[0], round(a[1] / a[3], 4), a[2]] for b, a in sorted(acc.items())]


def segments(changes: list, t0: float, t1: float, n: int = MAX_SEGMENTS) -> list:
    """[(t, state)] (sorted) -> [[start, end, state]] covering [t0, t1]; at most ``n`` segments."""
    if t1 <= t0:
        return []
    raw = []
    prev = None
    for t, s in changes:
        if t > t1:
            break
        if t <= t0:
            prev = s
            continue
        if not raw and prev is not None:
            raw.append([t0, t, prev])
        elif raw:
            raw[-1][1] = t
        raw.append([t, t1, s])
    if not raw and prev is not None:
        raw = [[t0, t1, prev]]
    if raw:
        raw[-1][1] = t1
    merged = []
    for seg in raw:
        if merged and merged[-1][2] == seg[2]:
            merged[-1][1] = seg[1]
        else:
            merged.append(list(seg))
    if len(merged) <= n:
        return [[round(a, 1), round(b, 1), s] for a, b, s in merged]
    width = (t1 - t0) / n
    out = []
    for b in range(n):
        lo, hi = t0 + b * width, t0 + (b + 1) * width
        share = {}
        for a, e, s in merged:
            if e <= lo or a >= hi:
                continue
            share[s] = share.get(s, 0.0) + min(e, hi) - max(a, lo)
        if not share:
            continue
        s = max(share.items(), key=lambda kv: (kv[1], str(kv[0])))[0]
        if out and out[-1][2] == s:
            out[-1][1] = round(hi, 1)
        else:
            out.append([round(lo, 1), round(hi, 1), s])
    return out


def summary(kind: str, data: list, last) -> dict:
    if kind == "numeric":
        if not data:
            return {"min": None, "max": None, "mean": None, "last": num(last)}
        return {"min": min(p[1] for p in data), "max": max(p[3] for p in data),
                "mean": round(sum(p[2] for p in data) / len(data), 4), "last": num(last)}
    share = {}
    for a, b, s in data:
        share[s] = share.get(s, 0.0) + (b - a)
    total = sum(share.values()) or 1.0
    return {"share": {s: round(v / total, 4) for s, v in sorted(share.items(), key=lambda kv: -kv[1])[:12]}, "last": last,
            "changes": max(0, len(data) - 1)}


def series(entity: dict, rng: str, state, ring: list, recorded: dict | None, now: float) -> dict:
    """The answer of the sensor page for one range: numbers as buckets (min/mean/max) or states as segments."""
    t1 = now
    t0 = now - SPAN_S[rng]
    numeric = is_numeric(entity, state)
    kind = "numeric" if numeric else "state"
    source = "live"
    if rng == "1h":
        pts = list(ring)
        data = compress_numeric(pts, t0, t1) if numeric else segments(pts or [(t0, state)], t0, t1)
        if numeric and state not in NO_VALUE and not data:
            v = num(state)
            data = [[round(t1, 1), v, v, v]] if v is not None else []
    else:
        recorded = recorded or {}
        if numeric and recorded.get("stats"):
            source = "statistics"
            data = compress_stats(recorded["stats"], t0, t1)
        else:
            source = "history"
            changes = recorded.get("states") or []
            if numeric:
                data = compress_numeric(changes, t0, t1)
            else:
                data = segments(changes, t0, t1)
    out = {"entity": entity, "range": rng, "kind": kind, "source": source, "start": round(t0, 1), "end": round(t1, 1),
           "summary": summary(kind, data, state)}
    out["points" if numeric else "segments"] = data
    return out


def entity_rows(snap: dict, states: dict, now: float | None = None) -> list:
    """The sensors list: every entity the bridge keeps (private domains never reach it), compact rows."""
    now = time.time() if now is None else now
    devs = {d["id"]: d for d in (snap or {}).get("devices") or []}
    out = []
    for e in (snap or {}).get("entities") or []:
        dom = e.get("domain")
        if dom in ("automation", "script") or privacy.domain_of(e.get("entity_id")) in privacy.PRIVATE_DOMAINS:
            continue
        st = states.get(e["entity_id"])
        d = devs.get(e.get("device_id")) or {}
        out.append({"id": e["entity_id"], "name": e.get("name") or e["entity_id"], "state": st[0] if st else e.get("state"),
                    "unit": e.get("unit"), "domain": dom, "dc": e.get("dc"), "area": e.get("area") or d.get("area"),
                    "device": e.get("device_id"), "platform": e.get("platform"), "cat": e.get("cat"),
                    "changed_s": round(now - st[1], 1) if st else None})
        if len(out) >= MAX_LIST:
            break
    out.sort(key=lambda r: (str(r["name"]).lower(), r["id"]))
    return out
