"""Trend and forecast of one measured series: will it reach its limit soon? (early warnings, pure functions)

The aim is to see a problem coming (memory filling up, a disk running full, a host getting warmer week by week) instead
of reporting it when it is too late - and never to cry wolf over a daily rhythm or a sawtooth (a cache that fills and
is emptied, a backup that runs every night).

The calculation for one series and one window (``judge``):

1. **Support points.** The window is split into a fixed number of equal parts (6 hours: 12 parts of 30 minutes, 7 days:
   14 parts of 12 hours); the median of the samples in a part is its support point. A part counts only when at least
   half of its expected samples are there (a gap is a gap, not a flat line).
2. **Trailing monotone run.** The longest run of the newest support points (at least 3, and at least two thirds of the
   points there are) whose Kendall tau is at least 0.7, with at least half of the steps in its direction and no step
   back larger than 20 % of its span. A sawtooth never passes; a rise after one restart does, once it carries two
   thirds of the window.
3. **Slope.** Theil-Sen (the median of all pairwise slopes) over that run. It counts only when the change within the run
   is at least 1 % of the limit (0.2 % when the limit would be reached within a year: a big disk filling slowly).
4. **Time to the limit** = (limit - current value) / slope; the current value is the median of the last 30 minutes.

Levels: ``warn`` - rising, monotone, meaningful, at least 3 support points and the limit reached in less than
``WARN_H`` (72) hours; a warning from the short window needs the long window's consent (the value is above the long
window's highest point, or the long window rises as well), otherwise it is a daily rhythm. ``info`` - any other
monotone, meaningful trend (also falling, also without a limit); a short-window trend only while the long window has no
data yet or leans the same way, otherwise it is the daily rhythm (``calm``, shape ``daily``). ``calm`` - flat or wobbling. ``nodata`` - fewer
than 3 support points. Nothing sticks: every round calculates again from the samples.

All functions take the clock as an argument (tests use fixed series with a fixed clock).
"""
from __future__ import annotations

import math
from typing import Optional

WARN_H = 72.0
LEVELS = ("nodata", "calm", "info", "warn")
TAU_MIN = 0.7
STEP_BACK = 0.2
MIN_POINTS = 3
MEANINGFUL = 0.01
MEANINGFUL_NEAR = 0.002
MEANINGFUL_NO_LIMIT = 0.05
YEAR_H = 365 * 24.0
NOW_S = 1800                # "the value now": median of the samples of the last 30 minutes

# window -> (length in seconds, support points)
SPANS = {
    "6h": (6 * 3600, 12),
    "7d": (7 * 86400, 14),
}


def num(x) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def median(values) -> Optional[float]:
    s = sorted(values)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def support(points, start: float, end: float, n: int, step: float) -> list:
    """Samples ``[(t, value|None)]`` (one every ``step`` seconds, gaps allowed) -> ``[(t_middle, median)]`` of ``n`` equal
    parts of ``[start, end)``; a part needs at least half of its expected samples."""
    if not points or n < 1 or end <= start or step <= 0:
        return []
    width = (end - start) / float(n)
    expect = max(1, int(round(width / step)))
    parts = [[] for _ in range(n)]
    for t, v in points:
        v = num(v)
        if v is None or t is None or not (start <= t < end):
            continue
        parts[min(n - 1, int((t - start) // width))].append(v)
    return [(start + (k + 0.5) * width, median(parts[k])) for k in range(n) if len(parts[k]) * 2 >= expect]


def kendall_tau(values) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    s = 0
    for i in range(n):
        for j in range(i + 1, n):
            d = values[j] - values[i]
            s += (d > 0) - (d < 0)
    return s / (n * (n - 1) / 2.0)


def theil_sen(points) -> Optional[float]:
    """Median of all pairwise slopes of ``[(t, v)]`` -> unit per second."""
    p = list(points)
    s = [(p[j][1] - p[i][1]) / (p[j][0] - p[i][0]) for i in range(len(p)) for j in range(i + 1, len(p)) if p[j][0] != p[i][0]]
    return median(s) if s else None


def monotone(values, direction: int) -> bool:
    if len(values) < MIN_POINTS:
        return False
    span = max(values) - min(values)
    if span <= 0:
        return False
    if direction * kendall_tau(values) < TAU_MIN:
        return False
    if sum(1 for i in range(len(values) - 1) if direction * (values[i + 1] - values[i]) > 0) * 2 < len(values) - 1:
        return False                  # one single step is no trend
    return all(direction * (values[i + 1] - values[i]) >= -STEP_BACK * span for i in range(len(values) - 1))


def trailing_run(points) -> tuple:
    """Longest trailing monotone run -> (direction, [(t, v)]) or (0, [])."""
    least = max(MIN_POINTS, int(math.ceil(len(points) * 2 / 3.0)))
    for length in range(len(points), least - 1, -1):
        part = points[-length:]
        values = [v for _t, v in part]
        for direction in (1, -1):
            if monotone(values, direction):
                return direction, part
    return 0, []


def _round(x, digits: int = 4):
    if x is None:
        return None
    if x == 0:
        return 0.0
    return round(x, max(0, digits - int(math.floor(math.log10(abs(x)))) - 1))


def judge(points, now: float, window: str = "6h", limit: Optional[float] = None, current: Optional[float] = None,
          step: float = 300.0) -> dict:
    """One series in one window -> ``{"window", "n", "points", "high", "level", "shape", "direction", "slope_h", "eta_h"}``.
    ``points``: samples ``[(t, value)]``; ``limit``: the value that counts as full (None: no warning possible)."""
    length, n = SPANS[window]
    sp = support(points, now - length, now, n, step)
    inside = [num(v) for t, v in points if num(v) is not None and now - length <= t <= now]
    out = {"window": window, "n": len(sp), "points": [[round(t), _round(v)] for t, v in sp],
           "high": _round(max(inside)) if inside else None}
    if len(sp) < MIN_POINTS:
        return dict(out, level="nodata", shape="few", direction=0, slope_h=None, eta_h=None)
    values = [v for _t, v in sp]
    direction, run = trailing_run(sp)
    recent = [num(v) for t, v in points if num(v) is not None and now - NOW_S <= t <= now]
    now_value = current if current is not None else (median(recent) if recent else values[-1])
    if not direction:
        span = max(values) - min(values)
        ref = limit if limit else (abs(sum(values) / len(values)) or 1.0)
        return dict(out, level="calm", shape="flat" if span < MEANINGFUL * 2 * ref else "wobbly", direction=0, slope_h=None, eta_h=None)
    s = theil_sen(run)
    if s is None or s * direction <= 0:
        return dict(out, level="calm", shape="flat", direction=0, slope_h=None, eta_h=None)
    change = abs(s * (run[-1][0] - run[0][0]))
    slope_h = s * 3600.0
    eta = None
    if direction > 0 and limit is not None:
        eta = 0.0 if now_value >= limit else (limit - now_value) / slope_h
    if limit:
        ref = limit * (MEANINGFUL_NEAR if (eta is not None and eta <= YEAR_H) else MEANINGFUL)
    else:
        ref = abs(sum(values) / len(values)) * MEANINGFUL_NO_LIMIT
    if change < ref or change <= 0:
        return dict(out, level="calm", shape="flat", direction=0, slope_h=_round(slope_h), eta_h=None)
    level = "warn" if (eta is not None and eta < WARN_H) else "info"
    return dict(out, level=level, shape="rising" if direction > 0 else "falling", direction=direction, slope_h=_round(slope_h),
                eta_h=_round(eta) if eta is not None else None, run=len(run))


def combine(short: dict, long: dict) -> dict:
    """The verdict of one series from its short (6 h) and long (7 days) window. A short warning without the long
    window's consent is a daily rhythm and becomes ``info``; a long warning stands on its own."""
    if long.get("level") == "warn":
        best = dict(long)
    elif short.get("level") == "warn":
        consent = long.get("direction", 0) > 0 or (long.get("high") is not None and short["points"]
                                                     and short["points"][-1][1] is not None and short["points"][-1][1] > long["high"])
        best = dict(short) if consent else dict(short, level="info", shape="daily")
    elif long.get("level") == "info":
        best = dict(long)
    elif short.get("level") == "info" and (long.get("level") == "nodata" or long.get("direction", 0) == short.get("direction")):
        best = dict(short)
    elif short.get("level") == "info":
        best = dict(short, level="calm", shape="daily", direction=0, eta_h=None)     # a short trend the long window does not share
    elif long.get("level") == "calm" or short.get("level") == "calm":
        best = dict(long if long.get("level") == "calm" else short)
    else:
        best = dict(long if long.get("n", 0) >= short.get("n", 0) else short)
    best["spans"] = {"6h": {k: short.get(k) for k in ("level", "n", "shape", "eta_h")},
                       "7d": {k: long.get(k) for k in ("level", "n", "shape", "eta_h")}}
    return best
