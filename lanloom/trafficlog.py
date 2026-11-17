"""Traffic over time per connection: bytes per minute, so a node's plot can look back 1 h, 6 h, 24 h and 7 days.

The live ring (``live.py``) keeps the last minutes at one-second resolution. This log keeps what the board carried
**per trace** in one-minute buckets (bytes sent and received) for 8 days and answers the plot ranges:

    1h   60 points of 1 minute      6h   360 points of 1 minute
    24h  288 points of 5 minutes    7d   336 points of 30 minutes

Values are average bytes per second inside a bucket. Storage: one row per trace and day (1440 minutes x 2 counters,
zlib - most minutes are empty and compress to a few hundred bytes), the current day in memory, written every five
minutes and when Lanloom stops. Only byte counts per trace id are kept: no addresses, no peers.
"""
import sys
import threading
import time
import zlib
from array import array
from typing import Callable, Optional

MIN_S = 60
PER_DAY = 1440
DAY_S = 86400
KEEP_DAYS = 8
FLUSH_EVERY = 300.0
FLUSH_BATCH = 40
CAP = 0xFFFFFFFF
RANGES = {"1h": (60, 1), "6h": (360, 1), "24h": (1440, 5), "7d": (10080, 30)}      # minutes, minutes per point
SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic_min (trace TEXT NOT NULL, day INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY (trace, day));
"""
_BIG = sys.byteorder == "big"


class _Day:
    __slots__ = ("day", "tx", "rx")

    def __init__(self, day: int, tx=None, rx=None):
        self.day = day
        self.tx = tx if tx is not None else array("I", bytes(4 * PER_DAY))
        self.rx = rx if rx is not None else array("I", bytes(4 * PER_DAY))


def _pack(d: _Day) -> bytes:
    a, b = array("I", d.tx), array("I", d.rx)
    if _BIG:
        a.byteswap(); b.byteswap()
    return zlib.compress(a.tobytes() + b.tobytes(), 6)


def _unpack(day: int, blob) -> _Day:
    try:
        raw = zlib.decompress(blob)
    except zlib.error:
        raw = b""
    if len(raw) != 8 * PER_DAY:
        return _Day(day)
    a, b = array("I"), array("I")
    a.frombytes(raw[:4 * PER_DAY]); b.frombytes(raw[4 * PER_DAY:])
    if _BIG:
        a.byteswap(); b.byteswap()
    return _Day(day, a, b)


class TrafficLog:
    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        self.cur: dict = {}                # trace -> _Day (the day being written)
        self.old: dict = {}                # (trace, day) -> _Day, a late minute of yesterday or a read-back
        self.dirty: set = set()
        self.queue: list = []
        self.last_flush = clock()
        self._lock = threading.Lock()
        with store.transaction() as tx:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    tx._db.execute(stmt)

    # ------------------------------------------------------------------ writing
    def book(self, trace: str, tx: int, rx: int, now: Optional[float] = None) -> None:
        if not tx and not rx:
            return
        now = self.clock() if now is None else now
        minute = int(now // MIN_S)
        day, idx = divmod(minute, PER_DAY)
        ent = self.cur.get(trace)
        if ent is None or ent.day != day:
            with self._lock:
                if ent is not None:
                    self.old[(trace, ent.day)] = ent
                row = self.store.one("SELECT data FROM traffic_min WHERE trace=? AND day=?", (trace, day))
                ent = self.cur[trace] = _unpack(day, row["data"]) if row else _Day(day)
        if tx:
            ent.tx[idx] = min(CAP, ent.tx[idx] + int(tx))
        if rx:
            ent.rx[idx] = min(CAP, ent.rx[idx] + int(rx))
        self.dirty.add((trace, day))

    def maybe_flush(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            if now - self.last_flush >= FLUSH_EVERY and not self.queue:
                self.queue = sorted(self.dirty)
                self.dirty = set()
                self.last_flush = now
            batch, self.queue = self.queue[:FLUSH_BATCH], self.queue[FLUSH_BATCH:]
        if batch:
            self._write(batch, now)

    def flush(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            keys = sorted(set(self.queue) | self.dirty)
            self.queue, self.dirty = [], set()
            self.last_flush = now
        self._write(keys, now)

    def _find(self, trace: str, day: int) -> Optional[_Day]:
        ent = self.cur.get(trace)
        if ent is not None and ent.day == day:
            return ent
        return self.old.get((trace, day))

    def _write(self, keys: list, now: float) -> None:
        with self.store.transaction() as tx:
            for trace, day in keys:
                ent = self._find(trace, day)
                if ent is not None:
                    tx._db.execute("INSERT INTO traffic_min(trace, day, data) VALUES(?,?,?) ON CONFLICT(trace, day) DO UPDATE SET data=excluded.data",
                                   (trace, day, _pack(ent)))
            tx._db.execute("DELETE FROM traffic_min WHERE day < ?", (int(now // DAY_S) - KEEP_DAYS,))
        with self._lock:
            for k in [k for k in self.old if k not in self.dirty and k not in self.queue]:
                del self.old[k]
            gone = [t for t, e in self.cur.items() if e.day < int(now // DAY_S) - 1 and (t, e.day) not in self.dirty]
            for t in gone:
                del self.cur[t]

    def forget(self, trace_ids) -> None:
        ids = set(trace_ids)
        with self._lock:
            for t in ids:
                self.cur.pop(t, None)
            for k in [k for k in self.old if k[0] in ids]:
                del self.old[k]
            self.dirty = {k for k in self.dirty if k[0] not in ids}
        for t in ids:
            self.store.execute("DELETE FROM traffic_min WHERE trace=?", (t,))

    # ------------------------------------------------------------------ reading
    def _day(self, trace: str, day: int) -> Optional[_Day]:
        ent = self._find(trace, day)
        if ent is not None:
            return ent
        row = self.store.one("SELECT data FROM traffic_min WHERE trace=? AND day=?", (trace, day))
        return _unpack(day, row["data"]) if row else None

    def series(self, traces: list, rng: str, now: Optional[float] = None) -> dict:
        """``traces`` = [(trace id, flip)]; flip swaps the two directions (a trace is stored from its first end).

        {"range", "step", "t0", "points": [[tx_per_s, rx_per_s], ...], "peak": [tx, rx], "total": [tx_bytes, rx_bytes]}"""
        now = self.clock() if now is None else now
        minutes, per = RANGES[rng]
        end = int(now // MIN_S)
        start = end - minutes + 1
        tx, rx = [0] * minutes, [0] * minutes
        for trace, flip in traces:
            for day in range(start // PER_DAY, end // PER_DAY + 1):
                ent = self._day(trace, day)
                if ent is None:
                    continue
                a, b = (ent.rx, ent.tx) if flip else (ent.tx, ent.rx)
                base = day * PER_DAY
                lo, hi = max(start, base), min(end, base + PER_DAY - 1)
                for m in range(lo, hi + 1):
                    tx[m - start] += a[m - base]
                    rx[m - start] += b[m - base]
        step = per * MIN_S
        points, peak, total = [], [0, 0], [0, 0]
        for i in range(0, minutes, per):
            s, r = sum(tx[i:i + per]), sum(rx[i:i + per])
            total[0] += s; total[1] += r
            p = [round(s / step), round(r / step)]
            peak = [max(peak[0], p[0]), max(peak[1], p[1])]
            points.append(p)
        return {"range": rng, "step": step, "t0": start * MIN_S, "points": points, "peak": peak, "total": total}
