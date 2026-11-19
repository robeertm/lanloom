#!/usr/bin/env python3
"""Lanloom bench: server time of the main APIs, the live loop and memory for 50 / 250 / 1000 devices.

Test data are generated (sample names, documentation and private addresses; nothing is contacted): N devices modelled
on the demo's device types, two days of hourly and 30 days of daily data paths for a third of them, a month of events,
and live traffic on every device's trace. Measured in this process:

    start         building the core over the generated database (what a container start does before serving)
    map           GET /api/map (overview and network view) incl. JSON encoding
    devices       GET /api/devices (devices_cold: the first read after a start or a scan, when every device view is
                  built; afterwards views are reused until a device row changes)
    component     GET /api/component/<id> of a device: first opening, and the refresh of an open card (every 5 s)
    paths         GET /api/paths for 24 h and 30 days
    storage       GET /api/storage
    search        GET /api/search over devices, services and guests (a word several devices match, then a word with a typo)
    tick          one second of the live loop (frame building), CPU share = tick time / 1 s
    rss           resident memory after everything (process, from /proc) and Python heap of the core (tracemalloc)

Times are medians of several runs in milliseconds. CPU speed differs between hosts: compare results only on the same
machine, or as a ratio to ``calib_ms`` (a fixed pure-Python workload measured in the same run; the budget tests in
tests/test_budget.py use that ratio, so they hold on a fast laptop and on a small single-board computer alike).

    python3 scripts/benchmark.py                      # 50, 250, 1000 devices, table on stdout
    python3 scripts/benchmark.py --devices 250 --json # one size, JSON
    python3 scripts/benchmark.py --write-data DIR --devices 1000   # leave a data directory for a container test
"""
import argparse
import gc
import json
import os
import random
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanloom import core as core_mod  # noqa: E402
from lanloom import demo as demo_mod  # noqa: E402
from lanloom import mapmodel  # noqa: E402
from lanloom.crypto import KEY_BYTES, Vault  # noqa: E402
from lanloom.store import Store  # noqa: E402

SIZES = (50, 250, 1000)
BEST = False          # True: the fastest run instead of the median (the budget test: robust against a busy machine)
ON_PROGRESS = None    # called after each timed run; the budget test hands in the hang guard's progress report (tests/conftest.py)
COUNTRIES = ("DE", "NL", "US", "IE", "FR", "SE", "~own")


def calib(rounds: int = 3) -> float:
    """Fixed pure-Python workload (dicts, sorting, JSON), median milliseconds - the yardstick for relative budgets."""
    times = []
    data = [{"id": "d%05d" % i, "ip": "10.0.%d.%d" % (i // 250, i % 250), "v": [i, i * 2.5, str(i)]} for i in range(4000)]
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _k in range(3):
            s = json.dumps(sorted(data, key=lambda d: d["ip"]), separators=(",", ":"))
            back = json.loads(s)
            idx = {d["id"]: d for d in back}
            sum(len(v["ip"]) for v in idx.values())
        times.append((time.perf_counter() - t0) * 1000)
    return min(times) if BEST else statistics.median(times)


def hosts(n: int, seed: int = 7) -> list:
    """N scan results modelled on the demo's device types (gateway and this host once, the rest repeated)."""
    rng = random.Random(seed)
    templates = [h for h in demo_mod.HOSTS if not h.get("gw") and not h.get("self") and not h.get("second_nic")]
    gw = next(h for h in demo_mod.HOSTS if h.get("gw"))
    me = next(h for h in demo_mod.HOSTS if h.get("self"))
    out = [dict(gw, ip="10.40.0.1", n=1), dict(me, ip="10.40.0.2", n=2)]
    for i in range(n - 2):
        tpl = templates[i % len(templates)]
        k = i + 3
        ip = "10.40.%d.%d" % (k // 250, k % 250 + 1)
        h = json.loads(json.dumps(tpl))
        h.update(ip=ip, n=k, names=["%s-%d.lan" % ((tpl.get("names") or ["host"])[0].split(".")[0], k)])
        h.pop("ipv6", None)
        for s in h.get("ssdp") or []:
            if s.get("udn"):
                s["udn"] = "%08x-0000-4000-8000-%012x" % (rng.getrandbits(32), k)
        out.append(h)
    return out


def build(n: int, data_dir: Path) -> tuple:
    store = Store(str(data_dir / "lanloom.db"))
    cfg = SimpleNamespace(data_dir=data_dir, host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"b" * KEY_BYTES))
    now = time.time()
    scan_id = store.execute("INSERT INTO scans(started, finished, mode, cidrs, status, hosts) VALUES(?,?,?,?,?,?)",
                            (now - 60, now - 30, "gentle", '["10.40.0.0/22"]', "done", n)).lastrowid
    c.ingest({"hosts": [demo_mod.scan_host(h) for h in hosts(n)]}, scan_id)
    rng = random.Random(3)
    ids = [d["id"] for d in store.query("SELECT id FROM devices ORDER BY ip")]
    talkers = ids[::3]
    now_h = int(now // 3600)
    with store.transaction() as tx:
        for dev in talkers:
            for cc in rng.sample(COUNTRIES, 3):
                for h in range(now_h - 48, now_h + 1):
                    tx._db.execute("INSERT OR IGNORE INTO paths_hour(hour, device, cc, tx, rx) VALUES(?,?,?,?,?)",
                                   (h, dev, cc, rng.randint(1, 9) * 10**6, rng.randint(1, 30) * 10**6))
                for d in range(now_h // 24 - 30, now_h // 24 + 1):
                    tx._db.execute("INSERT OR IGNORE INTO paths_day(day, device, cc, tx, rx) VALUES(?,?,?,?,?)",
                                   (d, dev, cc, rng.randint(10, 90) * 10**6, rng.randint(10, 300) * 10**6))
                if not cc.startswith("~"):
                    tx._db.execute("INSERT OR IGNORE INTO paths_seen(device, cc, first, last) VALUES(?,?,?,?)", (dev, cc, now - 20 * 86400, now))
        for k in range(n * 6):
            dev = rng.choice(ids)
            tx._db.execute("INSERT INTO events(t, kind, data) VALUES(?,?,?)",
                           (now - rng.random() * 30 * 86400, rng.choice(("dev_offline", "dev_back")), json.dumps({"id": dev, "title": dev})))
    store.close()
    return cfg


def timed(fn, runs: int) -> float:
    out = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
        if ON_PROGRESS:
            ON_PROGRESS()
    return round(min(out) if BEST else statistics.median(out), 2)


def rss() -> int:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def hub_entry(n: int, now: float) -> dict:
    """A hub connection with n automations (each with a flow), 4n entities and 20000 state changes with their cause (the
    Automations tab, H3)."""
    from lanloom import hubbridge
    from lanloom.sources import hub_flow
    entry = hubbridge.new_entry(None)
    ents = [{"entity_id": "%s.e%d" % (("light", "switch", "sensor", "binary_sensor")[i % 4], i), "device_id": "d%d" % (i // 4),
             "name": "Entity %d" % i, "state": "on", "unit": None, "domain": "light", "area": "a%d" % (i % 12), "dc": None, "cat": None,
             "platform": "sample", "ls": False} for i in range(4 * n)]
    autos = [{"entity_id": "automation.a%d" % i, "id": str(i), "mode": "single", "last": now - i * 60, "state": "on"} for i in range(n)]
    snap = {"type": "hub_snapshot", "t": now, "admin": True, "areas": [{"id": "a%d" % i, "name": "Room %d" % i} for i in range(12)],
            "devices": [{"id": "d%d" % i, "name": "Device %d" % i, "area": "a%d" % (i % 12), "service": False} for i in range(n)],
            "entities": ents + [{"entity_id": a["entity_id"], "name": "Automation %s" % a["id"], "state": "on", "domain": "automation"} for a in autos],
            "automations": autos, "scripts": [], "trackers": [], "entries": [{"domain": "sample", "state": "loaded", "n": 3}]}
    hubbridge.book(entry, snap, now)
    flows = {}
    for i in range(n):
        cfg = {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.e%d" % (4 * i + 3), "to": "on"}, {"trigger": "time", "at": "%02d:%02d" % (i % 24, i % 60)}],
               "conditions": [{"condition": "numeric_state", "entity_id": "sensor.e%d" % (4 * i + 2), "below": 20}],
               "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.e%d" % (4 * ((i + 1) % n))}}, {"delay": 30},
                           {"action": "switch.turn_off", "target": {"entity_id": "switch.e%d" % (4 * i + 1)}}]}
        flows["automation.a%d" % i] = {"trigger": [], "condition": [], "action": [], "text": [], "related": [], "flow": hub_flow.flow_from_config(cfg)}
    hubbridge.book(entry, {"type": "hub_deps", "t": now, "automations": flows, "scripts": {}, "cycles": [], "blocked": []}, now)
    for k in range(20000):
        hubbridge.book(entry, {"type": "hub_state", "entity_id": "light.e%d" % (4 * (k % n)), "state": "on" if k % 2 else "off",
                               "t": now - 86000 + k * 4, "by": "automation.a%d" % (k % n)}, now)
    return entry


def measure(n: int, runs: int = 5, data_dir: Path = None) -> dict:
    keep = data_dir is not None
    tmp = None
    if data_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="lanloom-bench-")
        data_dir = Path(tmp.name)
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = build(n, data_dir)
    gc.collect()
    tracemalloc.start()
    base_heap = tracemalloc.get_traced_memory()[0]
    t0 = time.perf_counter()
    store = Store(str(data_dir / "lanloom.db"))
    c = core_mod.Core(cfg, store, Vault(store, b"b" * KEY_BYTES))
    start_ms = (time.perf_counter() - t0) * 1000
    dumps = lambda o: json.dumps(o, separators=(",", ":"))  # noqa: E731
    ids = [d["id"] for d in store.query("SELECT id FROM devices ORDER BY ip")]
    self_id = c.ip_to_device.get(c.self_ip) or "self"
    router = c.ip_to_device.get(c.gateway_ip)
    now = time.time()
    for dev in ids:                                   # every device talked to this host lately: one live trace each
        c.flow_seen[dev] = now
        c.presence[dev] = now
    # live traffic: every device's flow trace and the LAN trace carry bytes every second (worst case for the loop)
    def feed():
        for dev in ids:
            c._add_bytes(core_mod.flow_trace(self_id, dev), 40_000, 120_000)
        if router:
            c._add_bytes(core_mod.lan_trace(router, self_id), 10**6, 10**6)
    t = now
    for _ in range(c.ring.size):                      # fill the live rings to their full length
        feed()
        t += 1
        c.quant.check_q(t)
        for trace, (b0, b1) in list(c.pending_bytes.items()):
            c.ring.push(trace, t, b0, b1)
        c.pending_bytes.clear()
    res = {"devices": n, "start_ms": round(start_ms, 1)}

    def tick():
        nonlocal t
        feed()
        t += 1
        dumps(c.tick(t))
    tick()
    res["tick_ms"] = timed(tick, runs * 4)
    res["cpu_live_pct"] = round(res["tick_ms"] / 10.0, 2)             # share of one core: tick time per 1000 ms
    res["devices_cold_ms"] = timed(lambda: c.devices(), 1)       # first read after a start or a scan (views built)
    res["map_overview_ms"] = timed(lambda: dumps(mapmodel.build(c, "overview")), runs)
    res["map_network_ms"] = timed(lambda: dumps(mapmodel.build(c, "network")), runs)
    res["devices_ms"] = timed(lambda: dumps(c.devices()), runs)
    probe = ids[len(ids) // 2]
    res["component_first_ms"] = round(timed(lambda: dumps(mapmodel.component_detail(c, probe)), 1), 2)   # opening the card
    res["component_ms"] = timed(lambda: dumps(mapmodel.component_detail(c, probe)), runs)
    res["paths_24h_ms"] = timed(lambda: (c.paths._cache.clear(), dumps(c.paths_summary("24h"))), runs)
    res["paths_30d_ms"] = timed(lambda: (c.paths._cache.clear(), dumps(c.paths_summary("30d"))), runs)
    res["storage_ms"] = timed(lambda: dumps(c.storage.overview()), max(2, runs // 2))
    from lanloom import palette
    res["search_ms"] = timed(lambda: (palette.search(c, "sample"), palette.search(c, "swtich")), runs)    # a broad word, then a typo
    # start page and "Right now" rail (O3): both from scratch (the 2 s cache emptied every run; data paths stay a minute)
    res["overview_ms"] = timed(lambda: ([c.overview._cache.pop(k, None) for k in ("summary", "now", "devices")], dumps(c.overview.summary()), dumps(c.overview.right_now())), runs)
    res["inventory_ms"] = timed(lambda: dumps(c.inventory.list({"sort": "room"})), runs)        # inventory list (L1), computed per request
    # failure chain (S5): the graph built from scratch, the chain of the gateway (the largest) and the outage automatic
    res["failure_ms"] = timed(lambda: (c.failure.invalidate(), dumps(c.failure.chain(router or ids[0], "down")), dumps(c.failure.outages())), runs)
    # rewind and incidents (S7S8): a whole day of minutes on every flow trace and of 5-minute slots on every device (one in ten
    # had a short outage behind the router), then the rewind of 24 hours from scratch and the incidents computed again
    from lanloom import availability as av_mod, rewind as rewind_mod, trafficlog as tl_mod
    day0, minute0 = int(now // 86400), int(now // 60)
    rows = []
    for k, dev in enumerate(ids):
        d = tl_mod._Day(day0)
        for m in range(0, 1440, 3):
            d.rx[m], d.tx[m] = 60_000 + (m * 37 + k) % 9000, 9_000 + (m * 11 + k) % 700
        rows.append((core_mod.flow_trace(self_id, dev), day0, tl_mod._pack(d)))
        rows.append((core_mod.flow_trace(self_id, dev), day0 - 1, tl_mod._pack(d)))
        cells = bytearray([av_mod.pack(5, 0)] * av_mod.PER_DAY)
        if k % 10 == 0:
            for i in range(100, 103):
                cells[i] = av_mod.pack(0, 5)
        for day in (day0 - 1, day0):
            store.execute("INSERT OR REPLACE INTO avail_day(subject, day, slots) VALUES(?,?,?)", ("dev:" + dev, day, __import__("zlib").compress(bytes(cells))))
    with store.transaction() as tx:
        tx._db.executemany("INSERT OR REPLACE INTO traffic_min(trace, day, data) VALUES(?,?,?)", rows)
    c.traffic_log.cur.clear() if hasattr(c.traffic_log, "cur") else None
    c.availability.version += 1
    # the first rewind encodes every finished day once; later ones (another minute, the other board) only the running day
    res["rewind_cold_ms"] = timed(lambda: (rewind_mod._cache.clear(), rewind_mod._days.clear(), rewind_mod._running.clear(), dumps(rewind_mod.build(c, "24h", "network", now))), 1)
    res["rewind_ms"] = timed(lambda: (rewind_mod._cache.clear(), dumps(rewind_mod.build(c, "24h", "network", now))), max(2, runs // 2))
    res["incidents_ms"] = timed(lambda: (c.incidents.collect(now, force=True), dumps(c.incidents.list(7, now=now))), max(2, runs // 2))
    res["map_kb"] = round(len(dumps(mapmodel.build(c, "overview"))) / 1024, 1)
    res["ring_samples"] = c.ring.samples()
    res["heap_mb"] = round((tracemalloc.get_traced_memory()[0] - base_heap) / 1e6, 1)
    tracemalloc.stop()
    res["rss_mb"] = round(rss() / 1e6, 1)
    from lanloom import hubauto
    hub = hub_entry(n, now)       # Automations tab (H3): list, who switches what, timeline, day plan (outside the heap measure)

    def auto_tab():
        ctx = hubauto.Ctx(hub, now)
        dumps(hubauto.tab(ctx)), dumps(hubauto.who(ctx)), dumps(hubauto.timeline(ctx)), dumps(hubauto.dayplan(ctx))
    res["automations_ms"] = timed(auto_tab, max(2, runs // 2))
    res["db_mb"] = round(os.stat(data_dir / "lanloom.db").st_size / 1e6, 2)
    store.close()
    if not keep and tmp is not None:
        tmp.cleanup()
    return res


COLS = ("devices", "start_ms", "tick_ms", "cpu_live_pct", "map_overview_ms", "map_network_ms", "devices_ms", "devices_cold_ms", "component_first_ms", "component_ms",
        "paths_24h_ms", "paths_30d_ms", "storage_ms", "search_ms", "overview_ms", "inventory_ms", "failure_ms", "automations_ms", "rewind_cold_ms", "rewind_ms", "incidents_ms", "map_kb", "heap_mb", "rss_mb", "db_mb")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--devices", type=int, action="append", help="device count (repeatable); default 50, 250, 1000")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write-data", metavar="DIR", help="keep the generated data directory here (one size only)")
    a = ap.parse_args(argv)
    sizes = a.devices or list(SIZES)
    out = {"calib_ms": round(calib(), 2), "python": sys.version.split()[0], "results": []}
    for n in sizes:
        out["results"].append(measure(n, a.runs, Path(a.write_data) if a.write_data else None))
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        print("calibration %.1f ms (python %s)" % (out["calib_ms"], out["python"]))
        print(" | ".join(COLS))
        for r in out["results"]:
            print(" | ".join(str(r.get(c, "")) for c in COLS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
