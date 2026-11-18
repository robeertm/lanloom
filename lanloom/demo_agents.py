"""Sample host agents of the demo network (E4): what the Running view, the host cards and the map show.

Three entries, filled through the same path real reports take (``AgentHub.ingest_report``):

* the storage box (192.168.50.10) with an agent: media, photo and file containers, a short-lived job container the
  noise filter hides, sharing to the office laptop and the TV, a backup stream to the backup server;
* the backup server (192.168.50.12) with an agent: no containers, a failed off-site copy (service and timer), overlay
  VPN peers;
* this host (192.168.50.11) through a container source (socket proxy): Lanloom itself, a restarting container.

Only the sample network's addresses; nothing is contacted. Seven days of 5-minute host samples are written once.
"""
from __future__ import annotations

import json
import math
import time

from . import agents as agents_mod

STORAGE, BACKUP, HOME = "ag-demo0storage", "ag-demo0backup", "px-demo0home"
ENTRIES = (
    (STORAGE, "agent", "storage", "192.168.50.10", "02:4c:4c:00:00:0a", "Storage OS 7.2", 4, 16 << 30),
    (BACKUP, "agent", "backup", "192.168.50.12", "02:4c:4c:00:00:0c", "Server OS 12", 2, 4 << 30),
    (HOME, "proxy", "homeserver", "192.168.50.11", "", "", 4, 8 << 30),
)
_START = {}


def _containers(aid: str, now: float, el: float) -> list:
    day = 86400
    if aid == STORAGE:
        rows = [
            ("media-server", "example/media-server:10.9", "running", "healthy", now - 21 * day, [(8096, 8096)], "media", 2_400_000),
            ("photo-library", "example/photo-library:1.118", "running", "healthy", now - 9 * day, [(2283, 2283)], "photos", 90_000),
            ("photo-ml", "example/photo-ml:1.118", "running", None, now - 9 * day, [], "photos", 4_000),
            ("file-sync", "example/file-sync:1.27", "running", None, now - 30 * day, [(8384, 8384)], "sync", 160_000),
            ("reverse-proxy", "example/reverse-proxy:2.11", "running", "healthy", now - 40 * day, [(443, 443), (80, 80)], "edge", 300_000),
            ("db-photos", "example/database:16", "running", "healthy", now - 9 * day, [], "photos", 20_000),
            ("old-wiki", "example/wiki:2.5", "exited", None, now - 60 * day, [], "", 0),
            ("backup-job-7f3a", "example/backup-job:3", "exited", None, now - 140, [], "", 0),          # hidden: lived 2 minutes
        ]
    else:
        rows = [
            ("lanloom", "ghcr.io/robeertm/lanloom:%s" % agents_mod.__version__, "running", "healthy", now - 2 * day, [(7380, None)], "lanloom", 60_000),
            ("lanloom-updater", "ghcr.io/robeertm/lanloom:%s" % agents_mod.__version__, "running", None, now - 2 * day, [], "lanloom", 200),
            ("message-broker", "example/broker:2.0", "running", None, now - 12 * day, [(1883, 1883)], "home", 25_000),
            ("dns-filter", "example/dns-filter:5.18", "running", "healthy", now - 15 * day, [(53, 53), (8053, 80)], "dns", 12_000),
            ("metrics-db", "example/metrics-db:2.7", "running", None, now - 15 * day, [(8086, 8086)], "metrics", 45_000),
            ("camera-recorder", "example/recorder:0.14", "restarting", None, now - 3 * day, [(5000, 5000)], "cams", 0),
            ("dashboard", "example/dashboard:11.3", "running", "starting" if el < 120 else "healthy", now - 15 * day, [(3000, 3000)], "metrics", 8_000),
        ]
    out = []
    base = _START.setdefault(aid, now - 3600)
    for i, (name, image, state, health, created, ports, project, rate) in enumerate(rows):
        c = {"id": "%012x" % (0xdec0de000000 + i + (100 if aid == HOME else 0)), "name": name, "image": image, "state": state,
             "created": round(created), "started": round(created + 5) if state != "exited" else round(created + 5)}
        if state == "exited":
            c["finished"] = round(created + (120 if name.startswith("backup-job") else 20 * 86400))
            c["exit_code"] = 0
        if state == "restarting":
            c["restarts"] = 37
            c["exit_code"] = 1
        if health:
            c["health"] = health
        if ports:
            c["ports"] = [{"target": p, "port": q, "proto": "tcp"} for p, q in ports]
        if project:
            c["project"], c["service"] = project, name
        if state == "running" and rate:
            wave = 0.6 + 0.4 * math.sin((now - base) / 37.0 + i)
            c["tx"] = int(rate * (now - base) * wave * 0.8)
            c["rx"] = int(rate * (now - base) * wave * 0.25)
            c["networks"] = [{"name": (project or "default") + "_default", "ip": "172.20.%d.%d" % (i, 2)}]
        out.append(c)
    return out


def _system(aid: str, now: float, el: float) -> dict:
    cpus, mem = (4, 16 << 30) if aid == STORAGE else (2, 4 << 30) if aid == BACKUP else (4, 8 << 30)
    wave = 0.5 + 0.5 * math.sin(el / 41.0)
    disks = {STORAGE: [{"mount": "/", "total": 64 << 30, "free": 41 << 30}, {"mount": "/volume1", "total": 16 << 40, "free": 3 << 40}],
             BACKUP: [{"mount": "/", "total": 32 << 30, "free": 18 << 30}, {"mount": "/srv/backup", "total": 8 << 40, "free": int(0.6 * (1 << 40))}],
             HOME: [{"mount": "/", "total": 256 << 30, "free": 170 << 30}]}[aid]
    return {"load": [round(0.4 + cpus * 0.3 * wave, 2), round(0.5 + cpus * 0.2, 2), round(0.6 + cpus * 0.15, 2)],
            "mem_total": mem, "mem_avail": int(mem * (0.35 + 0.1 * wave)), "swap_total": 2 << 30, "swap_free": 2 << 30, "disks": disks}


def _units(aid: str, now: float) -> dict:
    if aid == STORAGE:
        timers = [{"name": "scrub.timer", "unit": "scrub.service", "active": True, "last": now - 5 * 86400, "next": now + 2 * 86400,
                   "result": "success", "unit_state": "inactive", "unit_result": "success"},
                  {"name": "snapshot.timer", "unit": "snapshot.service", "active": True, "last": now - 1800, "next": now + 1800,
                   "result": "success", "unit_state": "inactive", "unit_result": "success"}]
        return {"available": True, "failed": [], "timers": timers, "total": 184}
    if aid == BACKUP:
        failed = [{"name": "offsite-copy.service", "sub": "failed", "since": now - 4 * 3600, "result": "exit-code"}]
        timers = [{"name": "offsite-copy.timer", "unit": "offsite-copy.service", "active": True, "last": now - 4 * 3600,
                   "next": now + 20 * 3600, "result": "success", "unit_state": "failed", "unit_result": "exit-code"},
                  {"name": "nightly-backup.timer", "unit": "nightly-backup.service", "active": True, "last": now - 6 * 3600,
                   "next": now + 18 * 3600, "result": "success", "unit_state": "inactive", "unit_result": "success"},
                  {"name": "logrotate.timer", "unit": "logrotate.service", "active": True, "last": now - 3 * 3600,
                   "next": now + 21 * 3600, "result": "success", "unit_state": "inactive", "unit_result": "success"},
                  {"name": "apt-daily.timer", "unit": "apt-daily.service", "active": True, "last": now - 9 * 3600,
                   "next": now + 3 * 3600, "result": "success", "unit_state": "inactive", "unit_result": "success"}]
        return {"available": True, "failed": failed, "timers": timers, "total": 212}
    return {"available": False, "reason": "no_bus"}


LISTEN = {
    STORAGE: [("tcp", 22, "sshd"), ("tcp", 80, "docker-proxy"), ("tcp", 443, "docker-proxy"), ("tcp", 445, "smbd"),
              ("tcp", 2049, "nfsd"), ("tcp", 2283, "docker-proxy"), ("tcp", 8096, "docker-proxy"), ("tcp", 8384, "docker-proxy"),
              ("udp", 5353, "avahi-daemon"), ("udp", 137, "nmbd")],
    BACKUP: [("tcp", 22, "sshd"), ("tcp", 873, "rsync"), ("udp", 41641, "vpn-daemon")],
}


def _flows(aid: str, el: float, rng) -> list:
    wave = 0.5 + 0.5 * math.sin(el / 23.0)
    burst = 1.0 if (el % 90) < 40 else 0.08
    if aid == STORAGE:
        return [{"lip": "192.168.50.10", "rip": "192.168.50.12", "port": 873, "dir": "out", "tx": int(9_000_000 * burst), "rx": 60_000},
                {"lip": "192.168.50.10", "rip": "192.168.50.20", "port": 445, "dir": "in", "tx": int(2_500_000 * wave), "rx": 300_000},
                {"lip": "192.168.50.10", "rip": "192.168.50.30", "port": 8096, "dir": "in", "tx": int(18_000_000 * (0.7 + 0.3 * wave)),
                 "rx": 200_000},
                {"lip": "192.168.50.10", "rip": "192.168.50.21", "port": 2283, "dir": "in", "tx": int(400_000 * rng.random()), "rx": 900_000},
                {"lip": "192.168.50.10", "rip": "203.0.113.20", "port": 443, "dir": "out", "tx": 40_000, "rx": 120_000}]
    if aid == BACKUP:
        return [{"lip": "192.168.50.12", "rip": "192.168.50.10", "port": 873, "dir": "in", "tx": 60_000, "rx": int(9_000_000 * burst)},
                {"lip": "192.168.50.12", "rip": "192.168.50.15", "port": 22, "dir": "out", "tx": int(700_000 * wave), "rx": 50_000}]
    return []


def _overlay(aid: str, now: float) -> dict:
    if aid != BACKUP:
        return {"available": False}
    return {"available": True, "self": {"name": "backup", "ips": ["192.0.2.112"]},
            "peers": [{"name": "travel-laptop", "ips": ["192.0.2.120"], "online": True, "last_seen": round(now), "os": "desktop"},
                      {"name": "phone-1", "ips": ["192.0.2.122"], "online": False, "last_seen": round(now - 7200), "os": "mobile"},
                      {"name": "storage", "ips": ["192.0.2.110"], "online": True, "last_seen": round(now), "os": "server"}]}


def seed(core, now: float | None = None) -> None:
    """Create the three sample entries once (and seven days of host samples)."""
    hub = core.agents
    now = time.time() if now is None else now
    have = {r["id"] for r in hub._rows()}
    for aid, kind, name, ip, mac, os_name, cpus, _mem in ENTRIES:
        if aid in have:
            continue
        facts = {"name": name, "ips": [ip], "macs": [mac] if mac else [], "os": os_name, "kernel": "6.6.0" if kind == "agent" else "",
                 "arch": "x86_64", "cpus": cpus, "uptime": 23 * 86400}
        version = agents_mod.__version__ if aid != BACKUP else "1.0.0"         # one agent waits for its update (sample)
        core.store.execute("INSERT INTO agent_hosts(id, kind, name, token_hash, url, created, last_seen, version, facts)"
                           " VALUES(?,?,?,?,?,?,?,?,?)",
                           (aid, kind, name, None, "http://192.168.50.11:2375" if kind == "proxy" else None, now - 20 * 86400,
                            now, version if kind == "agent" else "", json.dumps(facts)))
        hub.live[aid] = {"known_names": [], "t": 0}
        hub._resolve_device(aid, facts)
        _history(core, aid, now)
    hub.agents_cache = None


def _history(core, aid: str, now: float) -> None:
    step = agents_mod.SAMPLE_S
    first = int((now - 7 * 86400) // step)
    rows = []
    for slot in range(first, int(now // step)):
        t = slot * step
        hour = time.localtime(t).tm_hour
        day = 0.5 + 0.5 * math.sin((hour - 8) / 24 * 2 * math.pi)
        cpus = 4 if aid != BACKUP else 2
        disk = {STORAGE: 80.0, BACKUP: 88.0, HOME: 33.0}[aid] + (now - t) / 86400 * -0.15
        failed = 1 if aid == BACKUP and now - t < 4 * 3600 else 0
        rows.append((slot, aid, round(0.3 + cpus * 0.4 * day, 2), cpus, round(45 + 20 * day, 1), round(disk, 1), failed,
                     6 if aid == STORAGE else 0 if aid == BACKUP else 6, 0))
    with core.store.transaction() as st:
        st._db.executemany("INSERT OR IGNORE INTO agent_samples(slot, agent, load, cpus, mem, disk, failed, containers, unhealthy)"
                           " VALUES(?,?,?,?,?,?,?,?,?)", rows)


def tick(core, now: float, el: float, rng) -> None:
    """One round (every 10 s): reports of the two agents, one reading of the container source."""
    hub = core.agents
    for aid, kind, *_ in ENTRIES:
        if aid not in hub.live:
            continue
        rep = {"t": now, "interval": 10, "flows": _flows(aid, el, rng)}
        if kind == "agent":
            rep.update(system=_system(aid, now, el), units=_units(aid, now), overlay=_overlay(aid, now),
                       listen=[{"proto": p, "ip": "0.0.0.0", "port": port, "local": False, "proc": proc} for p, port, proc in LISTEN[aid]])
            if aid == STORAGE:
                rep["containers"] = {"available": True, "runtime": "docker", "version": "27.3.1", "items": _containers(aid, now, el)}
            else:
                rep["containers"] = {"available": False, "reason": "no_socket"}
            hub.ingest_report(aid, rep, now)
            core.store.execute("UPDATE agent_hosts SET last_seen=? WHERE id=?", (now, aid))
        else:
            st = hub.live[aid]
            st["t"] = st["seen"] = now
            st["system"] = _system(aid, now, el)
            hub._apply_containers(aid, st, {"available": True, "runtime": "docker", "version": "27.3.1",
                                            "items": _containers(aid, now, el)}, now)
            core.store.execute("UPDATE agent_hosts SET last_seen=? WHERE id=?", (now, aid))
