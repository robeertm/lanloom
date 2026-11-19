"""Host health of the demo network (W1): seven days of host samples with something to see.

* the storage box (agent): the big volume fills up steadily - the disk reaches 95 % within about two days (an early
  warning on its card, in the overview, the nightly report and as a notification);
* the backup server (agent): swap grows slowly (a calm "info" trend, no warning);
* this host: an SNMP reading with a daily rhythm in memory (must not raise a warning) that stopped 40 minutes before the
  sample started - its card shows the source dimmed as "stale", never red.

The live values of the sample agents (``demo_agents``) continue the same curves (``volume_pct``, ``swap_pct``), so the
card, the samples and the forecast agree. Only the sample network's addresses; nothing is contacted.
"""
from __future__ import annotations

import math
import time

from . import hosthealth

ANCHOR: dict = {}           # the moment the curves pass their reference value (set once per process)
DAY = 86400.0


def anchor(now: float) -> float:
    return ANCHOR.setdefault("t", now)


def volume_pct(t: float) -> float:
    """Used share of the storage box's big volume: 87 % at the anchor, +4 % a day, never above 94.5 %."""
    a = ANCHOR.get("t", t)
    return round(min(94.5, 87.0 + 4.0 * (t - a) / DAY), 2)


def swap_pct(t: float) -> float:
    """Swap of the backup server: 22 % at the anchor, +3 % a day (a calm trend, the limit is weeks away)."""
    a = ANCHOR.get("t", t)
    return round(max(2.0, min(60.0, 22.0 + 3.0 * (t - a) / DAY)), 2)


def _day(t: float) -> float:
    hour = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60.0
    return 0.5 + 0.5 * math.sin((hour - 8) / 24 * 2 * math.pi)


def seed(core, now: float | None = None) -> None:
    """Write seven days of samples once (when the table holds none of the sample devices yet)."""
    from . import demo_agents
    now = time.time() if now is None else now
    anchor(now)
    hub = core.agents
    devs = {aid: hub.device_of(aid) for aid in (demo_agents.STORAGE, demo_agents.BACKUP, demo_agents.HOME)}
    if not all(devs.values()):
        return
    if core.store.one("SELECT 1 AS x FROM host_samples WHERE device=? LIMIT 1", (devs[demo_agents.STORAGE],)):
        return
    step = hosthealth.SAMPLE_S
    rows = []
    for slot in range(int((now - 7 * DAY) // step), int(now // step)):
        t = slot * step
        d = _day(t)
        wob = math.sin(slot * 1.7) * 0.6
        rows.append((slot, devs[demo_agents.STORAGE], "agent", round(18 + 30 * d + wob * 4, 1), round(52 + 14 * d + wob, 1), 3.0,
                     round(0.6 + 1.4 * d, 2), 4, volume_pct(t), round(44 + 9 * d + wob, 1), round(72 + 25 * d, 1), 0))
        rows.append((slot, devs[demo_agents.BACKUP], "agent", round(6 + 10 * d + wob, 1), round(61 + 5 * d, 1), swap_pct(t),
                     round(0.3 + 0.5 * d, 2), 2, 92.5, round(39 + 4 * d, 1), None, None))
        if now - t > 2400:         # this host's SNMP reading stopped 40 minutes before the sample started
            rows.append((slot, devs[demo_agents.HOME], "snmp", round(12 + 22 * d + wob * 3, 1), round(48 + 22 * d + wob, 1), 0.0,
                         round(0.5 + 1.1 * d, 2), 4, round(33.0 + 0.002 * (slot % 50), 2), round(51 + 8 * d + wob, 1), None, None))
    with core.store.transaction() as st:
        st._db.executemany("INSERT OR IGNORE INTO host_samples(slot, device, source, %s) VALUES(?,?,?,%s)"
                           % (", ".join(hosthealth.COLUMNS), ",".join("?" * len(hosthealth.COLUMNS))), rows)
    core.hosthealth.invalidate()
    # the sample's battery to replace has been waiting for two days: it shows on the "Waiting on someone" line
    core.overview.waiting.backdate = lambda note: now - 50 * 3600 if str(note.get("id", "")).startswith("onsite:") else None

