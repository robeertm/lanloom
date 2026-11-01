"""Live engine: measured bytes -> dots (byte conservation with carry), smoothed rates and live grades.

Byte conservation: for every trace direction a frame brings B bytes. With the carry C we emit
n = floor((B + C) / Q) dots and keep C = (B + C) - n*Q. No measured byte is lost: over any period
sum(n*Q) + C_end == sum(B) + C_start, even when Q changes (the carry stays in bytes).
Q is a power of two (1 KiB ... 64 MiB, factor 4), chosen so that the 75th percentile of active traces
yields about 6 dots per second; it changes at most every 30 s and only with clear distance (hysteresis).
Only what is measured is animated.

Live grade of a source from its cadence c (typical gap between updates) and the age a of the last update:
  a > max(2.5 c, c + 10 s)   -> interrupted
  c <= 2 s -> realtime, c <= 15 s -> near, c <= 10 min -> periodic, else sluggish.
"""
import math
import statistics
import time
from collections import deque

STEPS = [1024 * 4 ** i for i in range(9)]   # 1 KiB ... 64 MiB
Q_START = 64 * 1024
TARGET_DOTS = 6.0
Q_INTERVAL = 30.0
TAU = 20.0
ACTIVE_FROM = 256.0
GRADES = ("realtime", "near", "periodic", "sluggish", "interrupted", "unknown")
_GRADE_RANK = {g: i for i, g in enumerate(GRADES)}


def step_for(target: float) -> int:
    target = max(STEPS[0], min(STEPS[-1], target))
    return min(STEPS, key=lambda s: abs(math.log(s / target)))


def human_bytes(n: float) -> str:
    n = float(n)
    for unit, f in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= f:
            v = n / f
            return ("%.0f" % v if v >= 10 else "%.1f" % v) + " " + unit
    return "%d B" % n


def human_rate(bps: float) -> str:
    return human_bytes(bps) + "/s"


class Quantizer:
    def __init__(self, q: int = Q_START):
        self.q = q
        self.q_since = 0.0
        self.carry = {}      # (trace, dir) -> bytes
        self.rate = {}       # (trace, dir) -> smoothed B/s
        self.rate_t = {}
        self.ledger = {}     # trace -> [bytes0, bytes1, dotbytes0, dotbytes1]

    def book(self, trace: str, b0: int, b1: int, now: float, dt: float = 1.0) -> tuple:
        out = []
        led = self.ledger.setdefault(trace, [0, 0, 0, 0])
        for direction, b in ((0, max(0, int(b0))), (1, max(0, int(b1)))):
            key = (trace, direction)
            c = self.carry.get(key, 0) + b
            n = c // self.q
            c -= n * self.q
            if c:
                self.carry[key] = c
            else:
                self.carry.pop(key, None)
            led[direction] += b
            led[2 + direction] += n * self.q
            self._smooth(key, b, now, dt)
            out.append(int(n))
        return out[0], out[1]

    def _smooth(self, key, b, now, dt):
        old, t = self.rate.get(key), self.rate_t.get(key)
        dt = max(0.2, min(10.0, (now - t) if t else dt))
        a = 1 - math.exp(-dt / TAU)
        r = b / dt
        self.rate[key] = r if old is None else old + a * (r - old)
        self.rate_t[key] = now

    def rates(self, now: float) -> dict:
        out = {}
        for key, r in self.rate.items():
            age = max(0.0, now - self.rate_t.get(key, now))
            out[key] = r * math.exp(-age / TAU)
        return out

    def check_q(self, now: float) -> bool:
        if now - self.q_since < Q_INTERVAL:
            return False
        rates = self.rates(now)
        for key in [k for k, r in rates.items() if r < 1 and k not in self.carry]:
            self.rate.pop(key, None)
            self.rate_t.pop(key, None)
        active = sorted(r for r in rates.values() if r >= ACTIVE_FROM)
        self.q_since = now
        if not active:
            return False
        r75 = active[min(len(active) - 1, int(round(0.75 * (len(active) - 1))))]
        target = r75 / TARGET_DOTS
        new = step_for(target)
        if new != self.q and abs(math.log(target / self.q, 4)) > 0.75:
            self.q = new
            return True
        return False

    def forget(self, trace: str) -> None:
        for d in (0, 1):
            self.carry.pop((trace, d), None)
            self.rate.pop((trace, d), None)
            self.rate_t.pop((trace, d), None)
        self.ledger.pop(trace, None)


class Cadence:
    """Typical gap between updates of one source (median of the last 20 gaps)."""

    def __init__(self, nominal: float = None):
        self.nominal = nominal
        self.times = deque(maxlen=21)

    def tick(self, t: float = None) -> None:
        t = t or time.time()
        if not self.times or t - self.times[-1] > 0.05:
            self.times.append(t)

    @property
    def last(self):
        return self.times[-1] if self.times else None

    def cadence(self):
        if len(self.times) >= 3:
            gaps = [b - a for a, b in zip(self.times, list(self.times)[1:])]
            return max(0.1, statistics.median(gaps))
        return self.nominal

    def grade(self, now: float = None) -> str:
        return grade_for(self.cadence(), (now or time.time()) - self.last if self.last else None)


def grade_for(cadence, age) -> str:
    if cadence is None or age is None:
        return "unknown"
    if age > max(2.5 * cadence, cadence + 10.0):
        return "interrupted"
    if cadence <= 2.0:
        return "realtime"
    if cadence <= 15.0:
        return "near"
    if cadence <= 600.0:
        return "periodic"
    return "sluggish"


def worst(grades) -> str:
    grades = [g for g in grades if g and g != "unknown"]
    return max(grades, key=lambda g: _GRADE_RANK[g]) if grades else "unknown"


def typical(grades) -> str:
    grades = sorted((g for g in grades if g and g != "unknown"), key=lambda g: _GRADE_RANK[g])
    return grades[len(grades) // 2] if grades else "unknown"


class Ring:
    """Per-key ring of the last N one-second samples (pair of numbers)."""

    def __init__(self, size: int = 600):
        self.size = size
        self.data = {}

    def push(self, key: str, t: float, a: float, b: float) -> None:
        ring = self.data.get(key)
        if ring is None:
            ring = self.data[key] = deque(maxlen=self.size)
        ring.append((round(t, 1), round(a), round(b)))

    def series(self, key: str) -> list:
        return list(self.data.get(key, ()))
