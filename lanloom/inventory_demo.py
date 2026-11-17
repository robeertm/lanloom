"""Sample data of the inventory for the demo: purposes, rooms, purchase dates, warranties, a fictional end-of-updates table,
one device that has not been seen for a week, one that is probably gone, one that was decommissioned (and replaced by the
storage), and a few night-report events. All names are sample data; dates are relative to the morning of the latest sample
report so the report shows them as news that night."""
import datetime
import json
import time

from . import eol as eol_mod
from .demo import _hmac, _mac

DAY = 86400.0
# extra devices only the demo has (they are not part of demo.HOSTS): number -> (title, kind, ip, vendor)
EXTRA = {
    71: ("Old garden camera", "camera", "192.168.50.71"),
    72: ("Upstairs access point (old)", "ap", "192.168.50.72"),
    73: ("Old storage", "nas", "192.168.50.73"),
}


def _id(n: int) -> str:
    return "d-" + _mac(n).replace(":", "")


def _dev(n: int) -> str:
    return "d-" + _hmac(n).replace(":", "")


def install(core) -> None:
    """What lives in memory only: the devices that never answer and the fictional table; the dates come from the saved seed."""
    dates = core.store.get_setting("inventory_demo_eol", None)
    if not dates:
        return
    core.demo.absent = frozenset(_id(n) for n in EXTRA)
    core.demo.eol = eol_mod.EolTable.from_raw([
        {"id": "demo-sw8", "name": "Example SW-8 firmware line", "match": ["sw-8"], "until": dates["sw8"]},
        {"id": "demo-st4", "name": "Example storage OS 6", "match": ["st-4 bay"], "until": dates["st4"]},
    ], as_of=dates["as_of"])


def seed(core, now: float = None) -> None:
    inv = getattr(core, "inventory", None)
    if inv is None:
        return
    if core.store.get_setting("inventory_demo_seeded", False):
        install(core)
        return
    if not core.store.one("SELECT id FROM devices LIMIT 1"):
        return
    core.store.set_setting("inventory_demo_seeded", True)
    now = time.time() if now is None else now
    rep = core.reports
    d0 = rep.due_day(now)
    p = rep.planned_at(d0)                         # the morning of the latest sample report
    iso = lambda d: d.isoformat()
    day0 = d0
    plus = lambda n: iso(day0 + datetime.timedelta(days=n))

    # fictional end-of-updates table: the switch's firmware line ends in 30 days, the storage's operating system is over
    core.store.set_setting("inventory_demo_eol", {"sw8": plus(30), "st4": plus(-120), "as_of": iso(day0)})
    install(core)

    for n, (title, kind, ip) in EXTRA.items():
        did = _id(n)
        core.store.upsert_device({"id": did, "ip": ip, "mac": _mac(n), "data": {"hostnames": [], "ports": [], "sources": ["arp"]},
                                  "kind": kind, "confidence": 0.8, "title": title}, None)
    life = {71: (p - 7 * DAY - 3 * 3600, 300 * DAY),      # missing since last night
            72: (p - 30 * DAY - 2 * 3600, 700 * DAY),     # away for a month: probably gone
            73: (p - 61 * DAY, 1400 * DAY)}               # decommissioned two months ago
    for n, (last, span) in life.items():
        core.store.execute("UPDATE devices SET first_seen=?, last_seen=?, last_scan=NULL WHERE id=?", (last - span, last, _id(n)))

    def rec(did, **kw):
        r = inv.record(did)
        r.update(kw)
        inv._save(did, r)

    rec(_dev(2), purpose="Core switch", room="Hall cabinet", purchase_date=plus(-1100), shop="Example Shop", owner="admin", confirmed=True,
        warranty_until=plus(-400))
    rec(_dev(10), purpose="Family files and backups", room="Basement", purchase_date=plus(-700), warranty_until=plus(7), shop="Example Shop",
        owner="admin", confirmed=True, note="Replaced the old storage; disks swapped once")
    rec(_dev(40), purpose="Documents", room="Office", purchase_date=plus(-760), warranty_until=plus(-1), owner="night-shift", confirmed=True)
    rec(_dev(4), purpose="Garden coverage", room="Garden shed", purchase_date=plus(-200), warranty_until=plus(530), confirmed=True)
    rec(_dev(50), purpose="Front door", room="Entrance", purchase_date=plus(-300), warranty_until=plus(430), shop="Example Shop", confirmed=True)
    rec(_dev(20), purpose="Work laptop", room="Office", owner="night-shift", confirmed=True)
    rec(_dev(30), purpose="Living room TV", room="Living room", purchase_date=plus(-500), confirmed=True)
    rec(_id(71), purpose="Garden, left of the shed", room="Garden", purchase_date=plus(-900), warranty_until=plus(-170), confirmed=True)
    rec(_id(72), purpose="Covered the upper floor", room="Upstairs", confirmed=True)
    rec(_id(73), purpose="First storage", room="Basement", purchase_date=plus(-1900), confirmed=True, state="decommissioned",
        decom_at=iso(day0 - datetime.timedelta(days=60)), replaced_by=_dev(10), decom_note="Disks worn out, data moved", hid=True)
    core.store.execute("UPDATE devices SET hidden=1 WHERE id=?", (_id(73),))
    # one automatic entry that was never confirmed and left the list by itself during the night
    core.reports.journal.add("inv_expired", {"id": "d-demo-guest", "title": "Guest phone", "kind": "phone", "ip": "192.168.50.88",
                                             "since": p - 8 * DAY}, p - 5 * 3600)
    core._refresh_index()
