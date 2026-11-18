"""Sample data of the vulnerability hints for the demo: a fictional feed (every id, maker and model is invented), the
firmware two devices' owners typed into the inventory, one hint set aside with a note, and one notification. The
sample network then shows every case: a certain hint exploited in the wild with a fixed firmware (firmware typed in by
the owner), a certain hint with the firmware the hub knows, a possible hint for a model whose every version is
affected, a hint set aside, a model the feed lists but whose firmware is unknown, and devices that could not be checked."""
import json
import time

from .demo import _hmac
from .vulns import Feed

GATEWAY, PRINTER, SPEAKER = 1, 40, 31


def _dev(n: int) -> str:
    return "d-" + _hmac(n).replace(":", "")


FEED = [
    {"id": "DEMO-2026-0101", "sev": "critical", "score": 9.8, "kev": "2026-10-02", "ransom": False, "action": "update",
     "title": "Example Networks home gateway: command injection in the web management",
     "match": [{"v": "examplenetworks", "p": "homegatewayhg1", "r": [{"lt": "3.2.0"}]}]},
    {"id": "DEMO-2026-0102", "sev": "high", "score": 8.1, "kev": "", "ransom": False, "action": "update",
     "title": "Example Networks home gateway: a crafted request restarts the device",
     "match": [{"v": "examplenetworks", "p": "homegatewayhg1", "r": [{"ge": "3.0.0", "lt": "3.1.2"}]}]},     # 3.1.4 is outside
    {"id": "DEMO-2025-0201", "sev": "high", "score": 8.8, "kev": "", "ransom": False, "action": "replace",
     "title": "Example Imaging P-200: authentication can be bypassed (no update planned, end of support)",
     "match": [{"v": "exampleimaging", "p": "officelaserp200", "r": []}]},
    {"id": "DEMO-2026-0301", "sev": "medium", "score": 6.5, "kev": "", "ransom": False, "action": "update",
     "title": "Example Audio SP-1: the pairing service accepts unsigned updates",
     "match": [{"v": "exampleaudio", "p": "sp1", "r": [{"lt": "2.0.1"}]}]},
    {"id": "DEMO-2026-0401", "sev": "critical", "score": 9.1, "kev": "", "ransom": False, "action": "update",
     "title": "Example Corp TV-55: remote code execution through the media service",
     "match": [{"v": "examplecorp", "p": "tv55", "r": [{"lt": "5.4.0"}]}]},
    {"id": "DEMO-2026-0402", "sev": "high", "score": 7.5, "kev": "", "ransom": False, "action": "update",
     "title": "Example Corp MP-100: the media service can be crashed from the network",
     "match": [{"v": "examplecorp", "p": "mp100", "r": [{"lt": "2.8.0"}]}]},                              # firmware unknown: not checked
]


def install(core) -> None:
    if core.store.get_setting("vulns_demo_seeded", False):
        core.demo.vulnfeed = Feed.from_raw(FEED, as_of=core.store.get_setting("vulns_demo_as_of", "2026-11-01"), sources=["sample feed"])
        if getattr(core, "vulns", None):
            core.vulns.invalidate()


def seed(core, now: float = None) -> None:
    if getattr(core, "vulns", None) is None or getattr(core, "inventory", None) is None:
        return
    if core.store.get_setting("vulns_demo_seeded", False):
        install(core)
        return
    if not core.store.one("SELECT id FROM devices LIMIT 1"):
        return
    now = time.time() if now is None else now
    rep = core.reports
    d0 = rep.due_day(now)
    p = rep.planned_at(d0)                       # the morning of the latest sample report
    core.store.set_setting("vulns_demo_seeded", True)
    core.store.set_setting("vulns_demo_as_of", d0.isoformat())
    install(core)
    inv = core.inventory

    def fw(did, value):
        r = inv.record(did)
        r["firmware"] = value
        r["confirmed"] = True
        inv._save(did, r)

    # the firmware of two devices, as their owners typed it from the devices' own status pages
    fw(_dev(GATEWAY), "3.1.4")
    fw(_dev(SPEAKER), "1.8.0")
    core.store.set_setting("vulns_dismissed", {
        "%s|DEMO-2026-0301" % _dev(SPEAKER): {"at": p - 3 * 86400, "by": "admin", "note": "Pairing is switched off; only in the guest network"}})
    # first sightings: the gateway hint appeared during the night of the latest report, the others earlier
    core.store.set_setting("vulns_seen", {
        "%s|DEMO-2026-0101" % _dev(GATEWAY): p - 4 * 3600,
        "%s|DEMO-2025-0201" % _dev(PRINTER): p - 9 * 86400,
        "%s|DEMO-2026-0301" % _dev(SPEAKER): p - 12 * 86400})
    n = getattr(core, "notify", None)
    if n is not None:
        core.store.execute("INSERT INTO notify_log(t, event, key, severity, data, outcome, maint) VALUES(?,?,?,?,?,?,?)",
                           (p - 4 * 3600, "vulnerability", "vuln:%s:DEMO-2026-0101" % _dev(GATEWAY), "warning",
                            json.dumps({"title": "Router", "ip": "192.168.50.1", "name": "DEMO-2026-0101", "device": _dev(GATEWAY),
                                        "certainty_key": "vulns.certainty.certain", "action_key": "vulns.do.update"}),
                            json.dumps({"*": "nightly"}), None))
    core._refresh_index()
    core.vulns.invalidate()
