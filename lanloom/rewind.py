"""Rewind: play the last 24 hours (or 7 days) back on the board.

One answer holds everything the browser needs to replay a range without asking again:

* ``traffic``: per trace of the board the average bytes per second in each step, sent and received, as a string of
  level characters (one character per step, ``ALPHA[level]``; level 0 = nothing, level L = about 2^((L-0.5)/2) bytes/s, so a
  step is exact to about 19 %). 24 hours come in 1-minute steps from the minute log (lanloom/trafficlog.py), 7 days in
  5-minute sums of the same minutes. Traces without any byte in the range are left out.
* ``states``: per tile of the board that had trouble, one character per 5-minute availability slot (``u`` up, ``d`` down,
  ``p`` both seen, ``.`` no look); tiles that were always up are left out. ``gap`` marks the slots in which Lanloom
  looked at nothing at all (it was not running): the board shows them as unknown, never as down.
* ``marks``: the incidents of the range (lanloom/incidents.py) and a few events (new devices, the internet line, own
  updates) for the time line.
* ``totals``: per step the level of the busiest trace (the activity curve under the slider).

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
_LOOKED = bytes([0] + [1] * 255)                      # slot byte -> 1 when it holds any look
_DOWN = bytes(1 if b & 15 else 0 for b in range(256))  # slot byte -> 1 when a look failed
_GAP = bytes.maketrans(b"\x00\x01", b".u")
_cache: dict = {}
_lock = threading.Lock()


# level of a rate r (bytes per second, rounded up to a whole byte): the bit length of r², i.e. 2^(L-1) <= r² < 2^L; decoded as
# the geometric middle of that band, 2^((L-0.5)/2), so a step is exact to about 19 %. Computed with integers and translated to
# characters in one go (bytes.translate), because a range holds hundreds of thousands of steps.
_TABLE = bytes.maketrans(bytes(range(64)), ALPHA.encode("ascii"))


def level(rate: float) -> int:
    if rate <= 0:
        return 0
    r = int(math.ceil(rate))
    return min(63, (r * r).bit_length())


def rate_of(lv: int) -> float:
    return 0.0 if lv <= 0 else 2 ** ((lv - 0.5) / 2.0)


def encode(rates) -> str:
    return bytes(level(r) for r in rates).translate(_TABLE).decode("ascii")


def encode_sums(values, step: int) -> str:
    """Byte sums per step -> level characters (the fast path of ``build``). A sum fits 32 bits, so the rate stays below
    2^32 / 60 and the level below 54: no clamp needed."""
    s1 = step - 1
    return bytes([(((v + s1) // step) ** 2).bit_length() for v in values]).translate(_TABLE).decode("ascii")


def decode(text: str) -> list:
    return [rate_of(ALPHA.index(c)) for c in text]


_days: dict = {}                 # (trace, day, step) -> (sent, received) level strings of a finished day (they never change)
DAYS_MAX = 6000


_running: dict = {}              # (trace, day, step) -> [steps done, sent, received] of the running day (only new steps are encoded)


def _sums(arr, lo: int, hi: int, per: int) -> list:
    """Byte sums of the steps lo..hi-1 (``per`` minutes each) from a day's minute array."""
    part = arr[lo * per:hi * per]
    if per == 1:
        return part
    return [a + b + c + d + e for a, b, c, d, e in zip(*(part[k::per] for k in range(per)))]


def _day_levels(log: trafficlog.TrafficLog, trace: str, day: int, step: int, today: int, upto: int = 0):
    """Level strings of one trace and day (per minute, or per five minutes), None without traffic. Finished days are kept as
    they are; the running day keeps what it encoded and adds the steps since (the last one again: it was still filling)."""
    key = (trace, day, step)
    per = step // 60
    if day < today:
        hit = _days.get(key)
        if hit is not None:
            return hit or None
    ent = log._day(trace, day)
    if ent is None or not (any(ent.tx) or any(ent.rx)):
        res = ()
    elif day < today:
        n = trafficlog.PER_DAY // per
        res = (encode_sums(_sums(ent.tx, 0, n, per), step), encode_sums(_sums(ent.rx, 0, n, per), step))
    else:
        old = _running.get(key)
        done = max(0, old[0] - 1) if old else 0
        a = encode_sums(_sums(ent.tx, done, upto, per), step)
        b = encode_sums(_sums(ent.rx, done, upto, per), step)
        res = ((old[1][:done] if old else "") + a, (old[2][:done] if old else "") + b)
        if len(_running) > DAYS_MAX:
            _running.clear()
        _running[key] = [upto, res[0], res[1]]
    if day < today:
        if len(_days) >= DAYS_MAX:
            for k in list(_days)[:DAYS_MAX // 4]:
                del _days[k]
        _days[key] = res
    return res or None


def _levels(log, trace: str, start_min: int, end_min: int, step: int, today: int):
    """Level strings for the minutes start_min..end_min-1 of one trace (steps of ``step`` seconds), None without traffic."""
    per = step // 60
    a_out, b_out, got = [], [], False
    for day in range(start_min // trafficlog.PER_DAY, (end_min - 1) // trafficlog.PER_DAY + 1):
        base = day * trafficlog.PER_DAY
        lo, hi = (max(start_min, base) - base) // per, (min(end_min, base + trafficlog.PER_DAY) - base) // per
        lv = _day_levels(log, trace, day, step, today, hi)
        if lv is None:
            a_out.append("0" * (hi - lo)); b_out.append("0" * (hi - lo))
            continue
        got = True
        a_out.append(lv[0][lo:hi].ljust(hi - lo, "0")); b_out.append(lv[1][lo:hi].ljust(hi - lo, "0"))
    if not got:
        return None
    return "".join(a_out), "".join(b_out)


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
    end_min = (int(now // 60) // per + 1) * per         # whole steps (the 5-minute sums start on a 5-minute edge)
    start_min = end_min - length // 60
    n = length // step
    model = mapmodel.cached_build(core, view)
    traffic = {}
    today = int(now // 86400)
    for tr in model["traces"]:
        got = _levels(core.traffic_log, tr["id"], start_min, end_min, step, today)
        if got is not None:
            traffic[tr["id"]] = [got[0], got[1]]
    # the activity curve: per step the level of the busiest trace and direction (characters sort like levels up to 53, the
    # highest a 32-bit byte count reaches), taken column by column in C
    totals = "".join(max(col) for col in zip(*[x for pair in traffic.values() for x in pair])) if traffic else "0" * n
    # availability of the tiles (5-minute slots, the same for both ranges)
    cur = int(now // av.SLOT_S)
    first = cur - length // av.SLOT_S + 1
    states, seen = {}, 0
    tiles = {c["id"] for c in model["components"]}
    every = core.availability.cells_all(first, cur)
    for subject, cells in every.items():
        seen |= int.from_bytes(cells.translate(_LOOKED), "big")        # slots in which anything was looked at (0/1 per byte)
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
        if tile is None or tile not in tiles:
            continue
        if not any(cells.translate(_DOWN)):
            continue
        states[tile] = "".join(STATE_CH[av.slot_state(b)] for b in cells)
    gap = seen.to_bytes(cur - first + 1, "big").translate(_GAP).decode("ascii")
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
           "slot_t0": first * av.SLOT_S, "traffic": traffic, "totals": totals, "states": states, "gap": gap,
           "marks": marks, "events": events, "q": getattr(getattr(core, "quant", None), "q", None),
           "ms": round((time.perf_counter() - t0) * 1000, 1)}
    with _lock:
        if len(_cache) > 16:
            _cache.clear()
        _cache[key] = res
    return res
