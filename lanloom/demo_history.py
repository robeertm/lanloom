"""Sample history for the demo: 35 days of availability and 8 days of traffic per connection.

Everything is invented and relative to "now": most devices were always there, a tablet and a phone wobble, one camera had a long
night, one connection dropped for a quarter of an hour, and Lanloom itself was not running for two hours eleven days ago
(those slots stay *unknown*, not "down"). The curves follow a day rhythm with a calm night. Only runs on an empty history."""
import hashlib
import math
import random
import time
import zlib

from . import availability, mapmodel, trafficlog
from .demo import _hmac

# (device number, hours ago the outage started, minutes it lasted)
OUTAGES = [
    (23, 150, 25), (23, 131, 12), (23, 104, 40), (23, 79, 15), (23, 52, 30), (23, 27, 20), (23, 9, 35), (23, 600, 18), (23, 410, 22),
    (22, 70, 150), (22, 300, 90), (22, 780, 120),
    (51, 100, 14), (51, 380, 18), (51, 700, 11),
    (50, 6, 330),
    (15, 290, 40),
    (40, 560, 55), (21, 220, 25),
    # incident files (S8): the core switch took eleven parts with it three days ago (a firmware jump the evening before);
    # the same switch inside its maintenance window this morning; the kitchen speaker flapped; two devices behind the
    # upstairs access point dropped together (the access point itself kept answering: it is the probable root); the garden
    # access point is down right now and its wireless clients went with it (they found another access point later)
    (2, 74, 12), (3, 74, 12), (12, 74, 12), (10, 74, 14), (30, 74, 12), (40, 74, 16), (15, 74, 12), (31, 74, 13), (32, 74, 12),
    (60, 74, 12), (63, 74, 12), (61, 74, 12),
    (2, 8.5, 6), (3, 8.5, 6), (30, 8.5, 6),
    (60, 46, 20), (61, 46, 20),
    (4, 1.0, 70), (51, 1.0, 25), (64, 1.0, 25), (65, 1.0, 25),
]
FLAP_DEVICE, FLAP_START, FLAP_OFFSETS = 31, (30.0, 5), (0, 2, 4)      # three one-slot dropouts with one good slot between each
FIRMWARE = [(2, 75, "4.2.1", "4.2.2"), (1, 400, "7.57", "7.58"), (3, 900, "6.6.55", "6.6.77"), (4, 900, "6.6.55", "6.6.77"),
            (10, 1300, "7.2.1", "7.2.2"), (2, 1700, "4.1.9", "4.2.1")]      # (device number, hours ago, from, to)
OLD_DAYS = (36, 90)              # incident rows older than the slots (the calendar shows 90 days)
LATE = {65: 12, 66: 20}          # device number -> days after the start of the history it first answered
GAP = (264, 130)                 # Lanloom itself did not run: hours ago, minutes
SOURCES = {"mqtt": [(50, 15)], "hub": [(22, 7)], "controller": [(500, 9)]}      # connection kind -> (hours ago, minutes)


def _slots(hours_ago: float, minutes: float, now: float) -> range:
    start = int((now - hours_ago * 3600) // availability.SLOT_S)
    return range(start, start + max(1, int(math.ceil(minutes / 5.0))))


def seed(core, now: float = None) -> None:
    now = now or time.time()
    if core.availability.has_data() or core.store.one("SELECT 1 AS x FROM traffic_min LIMIT 1"):
        return
    seed_availability(core, now)
    seed_traffic(core, now)
    seed_incidents(core, now)
    seed_chronicle(core, now)
    seed_firewall_changes(core, now)
    core.availability.version += 1                 # the rows were written directly: readers must not keep an older answer
    core.availability._cache.clear()


def seed_availability(core, now: float) -> None:
    cur = int(now // availability.SLOT_S)
    start = cur - core.availability.keep_days * availability.PER_DAY + 1
    ids = {"dev:d-" + _hmac(n).replace(":", ""): n for n in (10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 30, 31, 32, 40, 50, 51, 60, 61, 62, 63, 64, 65, 66, 1, 2, 3, 4)}
    present = {"dev:" + d["id"] for d in core.store.devices(include_hidden=False) if not d["data"].get("is_self")}
    bad: dict = {}
    for n, ago, mins in OUTAGES:
        for sid in [k for k, v in ids.items() if v == n]:
            bad.setdefault(sid, set()).update(_slots(ago, mins, now))
    gap = set(_slots(*GAP, now))
    base = _slots(*FLAP_START, now).start             # the flapping speaker: whole slots, so the wall clock cannot join two runs
    for sid in [k for k, v in ids.items() if v == FLAP_DEVICE]:
        bad.setdefault(sid, set()).update(base + off for off in FLAP_OFFSETS)
    rng = random.Random(7)
    subjects = {sid for sid in present if sid in ids and ids[sid] not in (71, 72, 73)}
    kinds = {row["kind"]: row["id"] for row in core.store.query("SELECT id, kind FROM integrations")}
    for kind, runs in SOURCES.items():
        if kind in kinds:
            sid = "src:int:%d" % kinds[kind]
            subjects.add(sid)
            for ago, mins in runs:
                bad.setdefault(sid, set()).update(_slots(ago, mins, now))
    first = {sid: start + LATE.get(ids.get(sid), 0) * availability.PER_DAY for sid in subjects}      # devices that joined later
    rows = {}
    for sid in subjects:
        for slot in range(max(start, first[sid]), cur):
            if slot in gap:
                continue
            day, idx = divmod(slot, availability.PER_DAY)
            cells = rows.setdefault((sid, day), bytearray(availability.PER_DAY))
            if slot in bad.get(sid, ()):
                cells[idx] = availability.pack(0, 5) if rng.random() > 0.15 else availability.pack(2, 3)
            else:
                cells[idx] = availability.pack(5, 0)
    with core.store.transaction() as tx:
        tx._db.executemany("INSERT INTO avail_day(subject, day, slots) VALUES(?,?,?)",
                           [(s, d, zlib.compress(bytes(c), 6)) for (s, d), c in rows.items()])


def _diurnal(minute_of_day: float) -> float:
    h = minute_of_day / 60.0
    return 0.12 + 0.88 * max(0.0, math.sin((h - 6.0) / 17.0 * math.pi)) ** 1.5 if 6 <= h <= 23 else 0.12 + 0.05 * math.sin(h)


def seed_traffic(core, now: float) -> None:
    traces = []
    for view in ("overview", "network"):
        try:
            traces += [t["id"] for t in mapmodel.cached_build(core, view)["traces"]]
        except Exception:
            continue
    end = int(now // 60)
    start = end - 8 * trafficlog.PER_DAY
    rows = []
    for trace in sorted(set(traces)):
        h = int(hashlib.sha256(trace.encode()).hexdigest()[:8], 16)
        rng = random.Random(h)
        base = 2_000 + (h % 400) * 2_500                   # bytes per second at the day's peak
        ratio = 0.05 + (h % 7) / 14.0                      # sent share relative to received
        shift = (h % 180) - 90
        days = {}
        for minute in range(start, end):
            day, idx = divmod(minute, trafficlog.PER_DAY)
            d = days.get(day)
            if d is None:
                d = days[day] = trafficlog._Day(day)
            if minute < end - 24 * 60 and (minute - start) % 5:
                src = (minute - ((minute - start) % 5))
                sd, si = divmod(src, trafficlog.PER_DAY)
                d.tx[idx], d.rx[idx] = days[sd].tx[si], days[sd].rx[si]
                continue
            f = _diurnal((idx + shift) % 1440) * (0.75 + 0.5 * rng.random())
            if rng.random() < 0.012:
                f *= 6.0                                      # a burst now and then
            rx = int(base * f * 60)
            d.rx[idx] = min(rx, 0xFFFFFFFF)
            d.tx[idx] = min(int(rx * ratio), 0xFFFFFFFF)
        for day, d in days.items():
            rows.append((trace, day, trafficlog._pack(d)))
    with core.store.transaction() as tx:
        tx._db.executemany("INSERT INTO traffic_min(trace, day, data) VALUES(?,?,?)", rows)


def _title(core, n: int) -> tuple:
    d = core.store.device("d-" + _hmac(n).replace(":", ""))
    return (d["id"], d.get("title") or d["ip"], d.get("kind") or "unknown") if d else (None, "", "unknown")


def seed_incidents(core, now: float) -> None:
    """Incident rows from 36 to 90 days ago (older than the slots): a few faults a week, the weekly backup window."""
    from . import incidents
    rng = random.Random(29)
    pick = [(23, "blip"), (51, "outage"), (50, "outage"), (40, "blip"), (21, "outage"), (31, "flapping"), (1, "blip"), (15, "outage")]
    rows = []
    for day in range(OLD_DAYS[0], OLD_DAYS[1] + 1):
        base = now - day * 86400
        for _ in range(rng.choice((0, 0, 0, 1, 1, 2, 3))):
            n, kind = rng.choice(pick)
            did, title, dkind = _title(core, n)
            if did is None:
                continue
            start = float(int((base + rng.uniform(0, 86000)) // availability.SLOT_S) * availability.SLOT_S)
            dur = {"blip": 300, "flapping": 1500, "outage": rng.choice((900, 1800, 3600, 7200))}[kind]
            rows.append(_row(incidents, did, title, dkind, start, dur, kind))
        if time.localtime(base).tm_wday == 6:            # the weekly backup window of the sample notifications
            did, title, dkind = _title(core, 10)
            if did:
                lt = time.localtime(base)
                start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 2, 20, 0, 0, 0, -1))
                row = _row(incidents, did, title, dkind, start, 1200, "maintenance")
                row[5]["why_kind"], row[5]["window"] = "window", {"id": "w-demo-weekly", "name": "Weekly backups"}
                rows.append(row)
    import json as _json
    with core.store.transaction() as tx:
        tx._db.executemany('INSERT OR REPLACE INTO incident(id, start, "end", kind, root, data) VALUES(?,?,?,?,?,?)',
                           [(r[0], r[1], r[2], r[3], r[4], _json.dumps(r[5], separators=(",", ":"))) for r in rows])


def _row(incidents, did, title, dkind, start, dur, kind) -> list:
    iid = incidents.incident_id(did, start)
    inc = {"id": iid, "start": start, "end": start + dur, "open": False, "dur": dur, "kind": kind, "runs": 3 if kind == "flapping" else 1,
           "root": {"id": did, "kind": dkind, "title": title, "ip": "", "subject": "dev:" + did, "cert": "sure", "observed": True, "why": None,
                    "tiles": [did], "from": start, "to": start + dur},
           "members": [], "counts": {"members": 0, "sure": 0, "likely": 0}}
    return [iid, start, start + dur, kind, did, inc]


# rule changes made outside Lanloom (W3): (hours ago, level, sub, names). Three of them fall into the day before the core switch's
# outage (74 hours ago) (the demo's change list adds a recent one of its own, see ``Demo.overview_changes``)
FW_CHANGES = [
    (92.0, "danger", "pair_open_in", {"from": "Internet", "to": "Internal"}),
    (91.6, "warn", "rule_new", {"name": "Temporary access", "action": "allow", "from": "Internet", "to": "Internal"}),
    (88.0, "warn", "fwd_on", {"name": "Photo sharing", "port": "8443"}),
    (86.5, "info", "rule_off", {"name": "Old test rule", "action": "allow", "from": "Work", "to": "Cameras"}),
]


def seed_firewall_changes(core, now: float) -> None:
    """Rule changes of the sample gateway in the journal (kind ``fw_drift``): the incident file of the core switch lists them under
    "changed before", the change list of the overview shows the recent one."""
    for ago, level, sub, data in FW_CHANGES:
        core.reports.journal.add("fw_drift", dict(data, sub=sub, level=level, controller="Gateway", integ="int:demo", family="demo"),
                                 t=now - ago * 3600)


def seed_chronicle(core, now: float) -> None:
    """Firmware jumps of the infrastructure over the last weeks (the chronicle remembers them; "What happened?" finds the
    switch's jump the evening before its outage)."""
    rows = []
    for n, ago, old, new in FIRMWARE:
        did, title, dkind = _title(core, n)
        if did:
            rows.append((now - ago * 3600, "firmware", did, {"title": title, "from": old, "to": new, "kind": dkind}))
    core.chronicle._add(rows)
