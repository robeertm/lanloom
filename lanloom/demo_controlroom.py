"""Sample data of the control room (S9-S12): two internet lines with a mobile reserve, a UPS, backup chains and a week of history.

Only the sample network's addresses (documentation ranges); nothing is contacted - the internet probe is replaced by a
synthetic one. The history is written once (a short outage two days ago, a night on the reserve line with the UPS carrying the load
for seven minutes, one overdue and one unreported backup, one backup running)."""
from __future__ import annotations

import math
import time

from .controlroom import wan as wan_mod

LINE_MAIN, LINE_RESERVE = "n-demo-fibre", "n-demo-mobile"
UPS_HOST = "192.168.50.10"
PROBE = {"enabled": True, "dns": "", "tcp": ["192.0.2.53:53"]}


def wans(now: float) -> list:
    wave = 0.5 + 0.5 * math.sin(now / 41.0)
    return [{"id": LINE_MAIN, "name": "Fibre", "type": "dhcp", "failover": "failover-only", "enabled": True, "kind": "cable", "role": "primary",
             "up": True, "active": True, "rx_bps": 18e6 + 30e6 * wave, "tx_bps": 4e6 + 3e6 * wave, "latency_ms": 9.0 + 3 * wave},
            {"id": LINE_RESERVE, "name": "Mobile backup", "type": "lte", "failover": "failover-only", "enabled": True, "kind": "cellular", "role": "backup",
             "up": True, "active": False, "rx_bps": 0.0, "tx_bps": 0.0, "latency_ms": 38.0}]


def cellular(now: float) -> list:
    wave = math.sin(now / 90.0)
    return [{"id": "cell-demo-1", "line": LINE_RESERVE, "name": "5G router", "tech": "5G NSA", "bands": "B20+B1+B3+n78", "carrier": "Sample Mobile",
             "rsrp": round(-96 + 3 * wave), "rsrq": round(-12 + wave), "sinr": round(11 + 3 * wave), "rssi": round(-68 + 2 * wave),
             "data_used": 38.4e9, "data_limit": 100e9, "period_end_t": now + 9 * 86400, "cell_id": "0x1A2B3C4", "tac": "4711", "location": "Sample hill"}]


def extend_gateway(st: dict, now: float) -> dict:
    """The sample gateway state with the internet lines, the mobile router and the intrusion detection counters."""
    st["wans"], st["cellular"], st["ids"] = wans(now), cellular(now), {"enabled": True, "blocked_24h": 214}
    return st


def nut_event(now: float, el: float) -> dict:
    wave = 0.5 + 0.5 * math.sin(el / 53.0)
    return {"type": "nut", "source": "nut", "host": UPS_HOST, "t": now, "units": [
        {"name": "office", "description": "Rack UPS", "status": "OL", "charge": 100.0, "charge_low": 20.0, "runtime_s": 2280.0 - 60 * wave,
         "runtime_low_s": 300.0, "load": round(31 + 8 * wave, 1), "input_v": round(231 + 2 * wave, 1), "output_v": 230.0, "battery_v": 27.3,
         "temp": 29.0, "power_w": round(210 + 40 * wave), "test": "Done and passed", "model": "UPS-1500"}]}


async def synthetic_probe() -> dict:
    wave = 0.5 + 0.5 * math.sin(time.time() / 30.0)
    return {"ok": True, "ms": round(11 + 6 * wave, 1), "dns_ok": None, "tcp_ok": True}


def seed(core, now: float) -> None:
    """History of a week and the declared backup chains; only when the control room is empty."""
    cr = core.controlroom
    if core.store.get_setting("demo_controlroom_seeded", False):
        return
    core.store.set_setting("demo_controlroom_seeded", True)
    cr.wan.set_probe(PROBE)
    cr.wan.probe_once = synthetic_probe                      # the demo never opens a connection
    cur = int(now // wan_mod.SLOT_S)
    day = 86400
    out_from, out_to = now - 2 * day - 3 * 3600, now - 2 * day - 3 * 3600 + 14 * 60
    res_from, res_to = now - 9 * 3600, now - 9 * 3600 + 14 * 60
    rows = []
    for s in range(cur - 7 * day // wan_mod.SLOT_S, cur + 1):
        t = s * wan_mod.SLOT_S
        state = "u"
        if out_from - wan_mod.SLOT_S < t <= out_to:
            state = "d"
        elif res_from - wan_mod.SLOT_S < t <= res_to:
            state = "r"
        rows.append((s, state))
    for r in rows:
        core.store.execute("INSERT OR REPLACE INTO wan_slots(slot, state) VALUES (?,?)", r)
    for t, kind, line, basis in ((out_from, "down", "", "measured"), (out_to, "up", "", "measured"), (res_from, "failover", "Mobile backup", "gateway"),
                                 (res_to, "failback", "Fibre", "gateway")):
        cr.wan._log(t, kind, line, basis, "")
    cr.wan.confirmed, cr.wan.since = "up", now - 3600
    for t, state, charge, runtime in ((res_from, "battery", 96.0, 2200.0), (res_from + 7 * 60, "online", 88.0, 2000.0)):
        core.store.execute("INSERT INTO ups_log(t, unit, state, charge, runtime_s) VALUES (?,?,?,?,?)", (t, "nut:demo:office", state, charge, runtime))
    bk = cr.backups
    bk.save_chain("offsite-copy", {"name": "Off-site copy", "source": "Storage box", "target": "Remote vault", "expect_hours": 24}, now)
    bk.save_chain("photo-archive", {"name": "Photo archive", "source": "Storage box", "target": "Cold storage", "expect_hours": 168}, now)
    bk.save_chain("laptop-image", {"name": "Laptop image", "source": "Office laptop", "target": "Storage box", "expect_hours": 72}, now)
    bk.save_chain("nightly-sync", {"name": "Nightly sync", "source": "Home server", "target": "Backup server", "expect_hours": 24}, now)
    for n in range(1, 5):                                        # a few finished runs: the learned throughput
        bk.report_run("offsite-copy", True, size=(41 + n) * 2 ** 30, duration_s=3300 + 120 * n, now=now - (n + 0.25) * day)
        bk.report_run("nightly-sync", True, size=(6 + n % 2) * 2 ** 30, duration_s=900 + 40 * n, now=now - (n + 0.1) * day)
    bk.report_run("offsite-copy", True, size=43 * 2 ** 30, duration_s=3500, now=now - 6 * 3600)
    bk.report_run("photo-archive", True, size=380 * 2 ** 30, duration_s=26000, now=now - 12 * day)
    bk.report_progress("nightly-sync", 62.0, now=now - 600)
    bk.set_restore_checked("offsite-copy", now - 40 * day, now)


def tick(core, now: float, el: float) -> None:
    """Every 10 s: a reading of the UPS and of the probe, and one evaluation of the internet watch."""
    cr = core.controlroom
    ids = {row["kind"]: row["id"] for row in core.store.query("SELECT id, kind FROM integrations")}
    if "nut" in ids:
        core.on_event("int:%d" % ids["nut"], ids["nut"], nut_event(now, el))
    wv = cr.wan
    wv.feed_probe({"ok": True, "ms": round(11 + 6 * (0.5 + 0.5 * math.sin(now / 30.0)), 1), "dns_ok": None, "tcp_ok": True}, now)
    wv.step(now)
