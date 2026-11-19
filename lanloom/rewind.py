"""Rewind: play the last 24 hours (or 7 days) back on the board.

One answer holds everything the browser needs to replay a range without asking again:

* ``traffic``: per trace of the board the average bytes per second in each step, sent and received, as a string of
  level characters (one character per step, ``ALPHA[level]``; level 0 = nothing, level L = 2^((L-1)/2) bytes/s, so a
  step is exact to about 19 %). 24 hours come in 1-minute steps from the minute log (lanloom/trafficlog.py), 7 days in
  5-minute sums of the same minutes. Traces without any byte in the range are left out.
* ``states``: per tile of the board that had trouble, one character per 5-minute availability slot (``u`` up, ``d`` down,
  ``p`` both seen, ``.`` no look); tiles that were always up are left out. ``gap`` marks the slots in which Lanloom
  looked at nothing at all (it was not running): the board shows them as unknown, never as down.
* ``marks``: the incidents of the range (lanloom/incidents.py) and a few events (new devices, the internet line, own
  updates) for the time line.
* ``totals``: all traces together per step (the activity curve under the slider).

Only byte counts per trace, slot states and titles - no addresses beyond what the board already shows. The answer is
cached per range, board and minute.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

from . import availability as av, mapmodel, trafficlog

ALPHA = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
RANGES = {"24h": (86400, 60), "7d": (7 * 86400, 300)}           # length, step (seconds)
STATE_CH = {"unknown": ".", "up": "u", "down": "d", "partial": "p"}
EVENT_KINDS = ("dev_new", "wan_down", "wan_up", "app_start")
CACHE_S = 30.0
MAX_EVENTS = 300
_cache: dict = {}
_lock = threading.Lock()


def level(rate: float) -> int:
    if rate <= 0:
        return 0
    return max(1, min(63, 1 + int(round(2 * math.log2(max(1.0, rate))))))


def rate_of(lv: int) -> float:
    return 0.0 if lv <= 0 else 2 ** ((lv - 1) / 2.0)


def encode(rates) -> str:
    return "".join(ALPHA[level(r)] for r in rates)


def decode(text: str) -> list:
    return [rate_of(ALPHA.index(c)) for c in text]


def _minutes(log: trafficlog.TrafficLog, trace: str, start: int, end: int) -> tuple:
    """Bytes per minute (sent, received) for the minutes start..end-1 of one trace, or None when there is nothing."""
    n = end - start
    tx, rx, any_ = [0] * n, [0] * n, False
    for day in range(start // trafficlog.PER_DAY, (end - 1) // trafficlog.PER_DAY + 1):
        ent = log._day(trace, day)
        if ent is None:
            continue
        base = day * trafficlog.PER_DAY
        lo, hi = max(start, base), min(end, base + trafficlog.PER_DAY)
        a, b = ent.tx[lo - base:hi - base], ent.rx[lo - base:hi - base]
        if any(a) or any(b):
            any_ = True
            tx[lo - start:hi - start] = a
            rx[lo - start:hi - start] = b
    return (tx, rx) if any_ else None


def build(core, rng: str = "24h", view: str = "overview", now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    rng = rng if rng in RANGES else "24h"
    view = view if view in ("overview", "network") else "overview"
    key = (id(core), rng, view, int(now // CACHE_S), core.availability.version // 50)
    with _lock:
        hit = _cache.get(key)
    if hit:
        return hit
    t0 = time.perf_counter()
    length, step = RANGES[rng]
    per = step // 60
    end_min = int(now // 60) + 1
    start_min = end_min - length // 60
    n = length // step
    model = mapmodel.cached_build(core, view)
    traffic, totals = {}, [0.0] * n
    for tr in model["traces"]:
        got = _minutes(core.traffic_log, tr["id"], start_min, end_min)
        if got is None:
            continue
        tx, rx = got
        if per > 1:
            tx = [sum(tx[i:i + per]) for i in range(0, len(tx), per)]
            rx = [sum(rx[i:i + per]) for i in range(0, len(rx), per)]
        stx = [v / step for v in tx]
        srx = [v / step for v in rx]
        for i in range(n):
            totals[i] += stx[i] + srx[i]
        traffic[tr["id"]] = [encode(stx), encode(srx)]
    # availability of the tiles (5-minute slots, the same for both ranges)
    cur = int(now // av.SLOT_S)
    first = cur - length // av.SLOT_S + 1
    states, seen_any = {}, bytearray(cur - first + 1)
    tiles = {c["id"] for c in model["components"]}
    for subject in core.availability.subjects():
        if subject.startswith("dev:"):
            tile = subject[4:]
        elif subject.startswith("src:int:"):
            tile = None
            iid = subject[8:]
            for pre in ("hub:", "broker:", "snmp:"):
                if pre + iid in tiles:
                    tile = pre + iid
                    break
        else:
            tile = None
        cells = core.availability.cells(subject, first, cur)
        for i, b in enumerate(cells):
            if b:
                seen_any[i] = 1
        if tile is None or tile not in tiles:
            continue
        if not any(b & 15 for b in cells):
            continue
        states[tile] = "".join(STATE_CH[av.slot_state(b)] for b in cells)
    gap = "".join("." if not s else "u" for s in seen_any)
    if "u" not in gap:
        gap = ""                                     # no availability history at all: nothing is marked as a gap
    marks = []
    inc = getattr(core, "incidents", None)
    if inc is not None:
        try:
            marks = inc.marks_for_range(start_min * 60, now)
        except Exception:
            marks = []
    events = []
    j = getattr(getattr(core, "reports", None), "journal", None)
    if j is not None:
        for ev in j.between(start_min * 60, now, EVENT_KINDS):
            d = ev.get("data") or {}
            if ev["kind"] == "app_start" and (not d.get("prev") or d.get("prev") == d.get("version")):
                continue
            events.append({"t": ev["t"], "kind": ev["kind"], "id": d.get("id") or "", "name": str(d.get("title") or d.get("name") or "")[:60],
                           "version": str(d.get("version") or "")[:20] if ev["kind"] == "app_start" else None})
        events = events[-MAX_EVENTS:]
    res = {"range": rng, "view": view, "t0": start_min * 60, "step": step, "n": n, "now": now, "slot": av.SLOT_S,
           "slot_t0": first * av.SLOT_S, "traffic": traffic, "totals": encode(totals), "states": states, "gap": gap,
           "marks": marks, "events": events, "q": getattr(getattr(core, "quant", None), "q", None),
           "ms": round((time.perf_counter() - t0) * 1000, 1)}
    with _lock:
        if len(_cache) > 16:
            _cache.clear()
        _cache[key] = res
    return res
