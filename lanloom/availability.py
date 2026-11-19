"""Availability over time: was a device (or a connection) there, slot by slot, for up to 35 days.

The nightly watcher (``nightly/journal.py``) looks at every device once a minute and says "answers" or "does not answer";
this module books those looks into **5-minute slots**. One slot is one byte: the high nibble counts the looks that found
the subject up, the low nibble the looks that found it down (capped at 15; five looks fit into a slot). A slot without a
look is *unknown*, never "up" and never "down": while Lanloom itself is not running, or while its own network is gone, no
look is booked, so a gap in Lanloom's own history cannot turn into a fault of a device.

Storage: one row per subject and day (288 slots, zlib), kept ``keep_days`` days (Settings > Storage, default 35). The
current day lives in memory and is written every five minutes and when Lanloom stops. Nothing here talks to the network.

Reading (all pure functions of the slot bytes, so the tests can feed fixed series):

* ``analyse``: availability share per window, **incidents** (runs of slots with at least one failed look; two runs only
  one good slot apart count as one flapping incident), MTTR (mean length of the incidents that ended), the longest incident.
* ``stability``: one row per subject for the "Stability" list: 35 daily bars, shares for 24 h / 7 d / 35 d, incidents of the
  last 7 days, MTTR, and the **wobbly** flag (three or more incidents in 7 days) - the candidates to look at first.
"""
import logging
import threading
import time
import zlib
from typing import Callable, Optional

log = logging.getLogger("lanloom")

SLOT_S = 300
PER_DAY = 288                     # slots per day
DAY_S = 86400
DEFAULT_KEEP_DAYS = 35
WOBBLY_INCIDENTS = 3              # incidents in 7 days from which a subject counts as wobbly
FLAP_GAP_SLOTS = 1                # up to this many good slots between two bad runs: still one incident
FLUSH_EVERY = 300.0
FLUSH_BATCH = 40                  # rows written per call (the live loop calls once a second)
CACHE_S = 30.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS avail_day (subject TEXT NOT NULL, day INTEGER NOT NULL, slots BLOB NOT NULL,
    PRIMARY KEY (subject, day));
"""


def pack(ups: int, downs: int) -> int:
    return (min(15, ups) << 4) | min(15, downs)


def ups_of(b: int) -> int:
    return b >> 4


def downs_of(b: int) -> int:
    return b & 15


def slot_state(b: int) -> str:
    """unknown | up | down | partial (both seen inside the slot)."""
    u, d = b >> 4, b & 15
    if not u and not d:
        return "unknown"
    if not d:
        return "up"
    return "down" if not u else "partial"


def share_of(cells) -> Optional[float]:
    """Availability share 0..1 over the slots that have looks (None when there is none)."""
    got = known = 0.0
    for b in cells:
        u, d = b >> 4, b & 15
        if u or d:
            known += 1
            got += u / (u + d)
    return None if not known else got / known


def incidents(cells, start_slot: int, now: float) -> list:
    """[{"from", "to", "dur", "open"}] oldest first. ``from``/``to`` are timestamps, ``dur`` seconds, ``open`` = still going."""
    runs, i, n = [], 0, len(cells)
    while i < n:
        if cells[i] & 15:
            j = i
            while j < n and cells[j] & 15:
                j += 1
            if runs and i - runs[-1][1] <= FLAP_GAP_SLOTS and all(cells[k] >> 4 for k in range(runs[-1][1], i)):
                runs[-1][1] = j                      # one good slot between two bad runs: the same flapping incident
            else:
                runs.append([i, j])
            i = j
        else:
            i += 1
    now_slot = int(now // SLOT_S)
    out = []
    for a, b in runs:
        t0, t1 = (start_slot + a) * SLOT_S, (start_slot + b) * SLOT_S
        is_open = start_slot + b >= now_slot
        out.append({"from": t0, "to": None if is_open else t1, "dur": int(max(SLOT_S, (now if is_open else t1) - t0)), "open": is_open})
    return out


def mttr(items: list) -> Optional[int]:
    done = [i["dur"] for i in items if not i["open"]]
    return int(sum(done) / len(done)) if done else None


class Availability:
    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        self.keep_days = DEFAULT_KEEP_DAYS
        self.cur: dict = {}                # (subject, day) -> bytearray(288), today's (and yesterday's late) slots
        self.dirty: set = set()
        self.queue: list = []
        self.last_flush = clock()
        self.version = 0                   # rises with every booked look (cache key for readers)
        self._lock = threading.RLock()
        self._cache: dict = {}
        with store.transaction() as tx:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    tx._db.execute(stmt)

    # ------------------------------------------------------------------ writing
    def _day_cells(self, subject: str, day: int) -> bytearray:
        cells = self.cur.get((subject, day))
        if cells is None:
            row = self.store.one("SELECT slots FROM avail_day WHERE subject=? AND day=?", (subject, day))
            cells = _unpack(row["slots"]) if row else bytearray(PER_DAY)
            self.cur[(subject, day)] = cells
        return cells

    def sample(self, subject: str, up: bool, now: Optional[float] = None) -> None:
        """Book one look: the subject answered (``up``) or did not."""
        now = self.clock() if now is None else now
        slot = int(now // SLOT_S)
        day, idx = divmod(slot, PER_DAY)
        with self._lock:
            cells = self._day_cells(subject, day)
            b = cells[idx]
            u, d = b >> 4, b & 15
            if up:
                u = min(15, u + 1)
            else:
                d = min(15, d + 1)
            cells[idx] = (u << 4) | d
            self.dirty.add((subject, day))
            self.version += 1

    def maybe_flush(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            if now - self.last_flush >= FLUSH_EVERY and not self.queue:
                self.queue = sorted(self.dirty)
                self.dirty = set()
                self.last_flush = now
            if self.queue:
                batch, self.queue = self.queue[:FLUSH_BATCH], self.queue[FLUSH_BATCH:]
                self._write(batch, now)

    def flush(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            keys = sorted(set(self.queue) | self.dirty)
            self.queue, self.dirty = [], set()
            self.last_flush = now
            self._write(keys, now)

    def _write(self, keys: list, now: float) -> None:
        if not keys:
            return
        with self.store.transaction() as tx:
            for subject, day in keys:
                cells = self.cur.get((subject, day))
                if cells is not None:
                    tx._db.execute("INSERT INTO avail_day(subject, day, slots) VALUES(?,?,?) ON CONFLICT(subject, day) DO UPDATE SET slots=excluded.slots",
                                   (subject, day, zlib.compress(bytes(cells), 6)))
            tx._db.execute("DELETE FROM avail_day WHERE day < ?", (int(now // DAY_S) - int(self.keep_days),))
        today = int(now // DAY_S)
        for k in [k for k in self.cur if k[1] < today - 1 and k not in self.dirty]:
            del self.cur[k]                  # finished days are read from the database again when needed

    def forget(self, subject: str) -> None:
        with self._lock:
            for k in [k for k in self.cur if k[0] == subject]:
                del self.cur[k]
            self.dirty = {k for k in self.dirty if k[0] != subject}
            self.queue = [k for k in self.queue if k[0] != subject]
        self.store.execute("DELETE FROM avail_day WHERE subject=?", (subject,))
        self._cache.clear()

    # ------------------------------------------------------------------ reading
    def cells(self, subject: str, from_slot: int, to_slot: int) -> bytearray:
        """The slots from_slot..to_slot (inclusive); slots without a look are 0."""
        n = to_slot - from_slot + 1
        out = bytearray(max(0, n))
        with self._lock:
            for day in range(from_slot // PER_DAY, to_slot // PER_DAY + 1):
                cells = self.cur.get((subject, day))
                if cells is None:
                    row = self.store.one("SELECT slots FROM avail_day WHERE subject=? AND day=?", (subject, day))
                    if row is None:
                        continue
                    cells = _unpack(row["slots"])
                base = day * PER_DAY
                lo, hi = max(from_slot, base), min(to_slot, base + PER_DAY - 1)
                out[lo - from_slot:hi - from_slot + 1] = cells[lo - base:hi - base + 1]
        return out

    def cells_all(self, from_slot: int, to_slot: int) -> dict:
        """{subject: slots from_slot..to_slot} for every subject with data in that range, read in one query (readers of all
        subjects at once: incidents, rewind)."""
        n = to_slot - from_slot + 1
        out: dict = {}
        d0, d1 = from_slot // PER_DAY, to_slot // PER_DAY

        def place(subject, day, cells):
            buf = out.get(subject)
            if buf is None:
                buf = out[subject] = bytearray(max(0, n))
            base = day * PER_DAY
            lo, hi = max(from_slot, base), min(to_slot, base + PER_DAY - 1)
            buf[lo - from_slot:hi - from_slot + 1] = cells[lo - base:hi - base + 1]
        with self._lock:
            mem = {k: v for k, v in self.cur.items() if d0 <= k[1] <= d1}
            for r in self.store.query("SELECT subject, day, slots FROM avail_day WHERE day >= ? AND day <= ?", (d0, d1)):
                if (r["subject"], r["day"]) not in mem:
                    place(r["subject"], r["day"], _unpack(r["slots"]))
            for (subject, day), cells in mem.items():
                place(subject, day, cells)
        return out

    def subjects(self) -> list:
        with self._lock:
            mem = {k[0] for k in self.cur}
        return sorted(mem | {r["subject"] for r in self.store.query("SELECT DISTINCT subject FROM avail_day")})

    def has_data(self) -> bool:
        return bool(self.cur) or self.store.one("SELECT 1 AS x FROM avail_day LIMIT 1") is not None

    def first_seen(self, subject: str) -> Optional[float]:
        """Time of the oldest slot with a look (None without data)."""
        days = [r["day"] for r in self.store.query("SELECT day FROM avail_day WHERE subject=?", (subject,))]
        days += [k[1] for k in self.cur if k[0] == subject]
        if not days:
            return None
        day = min(days)
        cells = self.cells(subject, day * PER_DAY, day * PER_DAY + PER_DAY - 1)
        for i, b in enumerate(cells):
            if b:
                return (day * PER_DAY + i) * SLOT_S
        return None

    def detail(self, subject: str, now: Optional[float] = None) -> dict:
        """Everything the card of one subject shows."""
        now = self.clock() if now is None else now
        cur = int(now // SLOT_S)
        days = int(self.keep_days)
        start = cur - days * PER_DAY + 1
        cells = self.cells(subject, start, cur)
        return _detail(subject, cells, start, now, self.first_seen(subject))

    def stability(self, names: dict, now: Optional[float] = None) -> dict:
        """The "Stability" list. ``names`` = {subject: {"title", "kind"?}}; only these subjects are listed."""
        now = self.clock() if now is None else now
        key = (int(now // 30), self.version, tuple(sorted(names)))
        hit = self._cache.get("stability")
        if hit and hit[0] == key:
            return hit[1]
        cur = int(now // SLOT_S)
        days = int(self.keep_days)
        start = cur - days * PER_DAY + 1
        first_day = start // PER_DAY
        loaded: dict = {}
        with self._lock:
            for r in self.store.query("SELECT subject, day, slots FROM avail_day WHERE day >= ?", (first_day,)):
                if r["subject"] in names:
                    loaded.setdefault(r["subject"], {})[r["day"]] = r["slots"]
            mem = {k: v for k, v in self.cur.items() if k[0] in names and k[1] >= first_day}
        rows = []
        for subject, meta in names.items():
            cells = bytearray(days * PER_DAY)
            have = False
            for day, blob in (loaded.get(subject) or {}).items():
                if (subject, day) in mem:
                    continue
                have |= _place(cells, _unpack(blob), day, start, cur)
            for (s, day), arr in mem.items():
                if s == subject:
                    have |= _place(cells, arr, day, start, cur)
            if not have:
                continue
            d = _detail(subject, cells, start, now, None)
            rows.append({"id": subject, "title": meta.get("title") or subject, "kind": meta.get("kind") or "", **{k: d[k] for k in (
                "share24", "share7", "share35", "bars", "incidents7", "mttr_s", "longest_s", "open", "wobbly", "slots24")}})
        rank = lambda r: (0 if r["open"] else 1 if r["wobbly"] else 2, -(r["incidents7"]), r["share7"] if r["share7"] is not None else 2.0, r["title"].lower())
        rows.sort(key=rank)
        done = [r["mttr_s"] for r in rows if r["mttr_s"]]
        out = {"rows": rows, "summary": {"subjects": len(rows), "open": sum(1 for r in rows if r["open"]), "wobbly": sum(1 for r in rows if r["wobbly"]),
                                         "incidents7": sum(r["incidents7"] for r in rows), "mttr_s": int(sum(done) / len(done)) if done else None,
                                         "stable": sum(1 for r in rows if not r["incidents7"] and r["share35"] is not None),
                                         "days": days},
               "generated": now}
        self._cache["stability"] = (key, out)
        return out


def _place(cells: bytearray, day_cells: bytearray, day: int, start: int, cur: int) -> bool:
    base = day * PER_DAY
    lo, hi = max(start, base), min(cur, base + PER_DAY - 1)
    if hi < lo:
        return False
    cells[lo - start:hi - start + 1] = day_cells[lo - base:hi - base + 1]
    return True


def _detail(subject: str, cells, start: int, now: float, first_seen: Optional[float]) -> dict:
    """Pure: cells are the slots start..start+len-1 (the last one is the current slot)."""
    n = len(cells)
    last24 = cells[max(0, n - PER_DAY):]
    last7 = cells[max(0, n - 7 * PER_DAY):]
    bars = []
    for k in range(n // PER_DAY - 1, -1, -1):        # oldest first; the last bar ends at the current slot
        seg = cells[n - (k + 1) * PER_DAY:n - k * PER_DAY]
        share = share_of(seg)
        bars.append(None if share is None else round(share, 4))
    inc7 = incidents(last7, start + n - len(last7), now)
    all_inc = incidents(cells, start, now)
    cur_state = slot_state(cells[-1]) if n else "unknown"
    s24, s7, s35 = share_of(last24), share_of(last7), share_of(cells)
    return {"id": subject, "share24": None if s24 is None else round(s24, 4), "share7": None if s7 is None else round(s7, 4),
            "share35": None if s35 is None else round(s35, 4), "bars": bars,
            "incidents7": len(inc7), "mttr_s": mttr(inc7), "longest_s": max([i["dur"] for i in inc7], default=0),
            "open": any(i["open"] for i in inc7), "wobbly": len(inc7) >= WOBBLY_INCIDENTS, "state": cur_state,
            "slots24": [int(b) for b in last24] if len(last24) == PER_DAY else [0] * (PER_DAY - len(last24)) + [int(b) for b in last24],
            "list": [i for i in reversed(all_inc)][:20], "first": first_seen, "slot_s": SLOT_S, "t": now}


def _unpack(blob) -> bytearray:
    try:
        raw = zlib.decompress(blob)
    except zlib.error:
        raw = b""
    out = bytearray(PER_DAY)
    out[:min(PER_DAY, len(raw))] = raw[:PER_DAY]
    return out
