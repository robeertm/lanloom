"""A second, fictional site for the demo (M1): a small branch office, reached over its controller (one site of a
multi-site controller) and a host agent on its server. Lanloom never scans it - exactly like a real remote site.

Its own network (10.44.7.0/24, private) with nine devices, one of them quiet (a desk that is switched off), so the
site overview and the switcher have something to show. Nothing is contacted; the data is written once into the
site's own store and kept alive by a small loop."""
from __future__ import annotations

import asyncio
import json
import math
import time

from . import agents as agents_mod, live

NAME = "Branch office"
SEEDED = "demo_sites_seeded"
AGENT = "ag-demo0branch"
OFFLINE = ("desk-02.office",)


def _mac(n: int) -> str:
    return "02:4c:4c:01:00:%02x" % n          # locally administered, not a real vendor prefix


def _port(port, service, title=None):
    return {"port": port, "proto": "tcp", "service": service, "banner": {"title": title, "server": None} if title else None}


HOSTS = [
    {"ip": "10.44.7.1", "n": 1, "names": ["branch-gw.office"], "ports": [_port(53, "dns"), _port(443, "https", "Gateway")]},
    {"ip": "10.44.7.2", "n": 2, "names": ["branch-switch.office"], "ports": [_port(443, "https", "Switch management")],
     "snmp": {"descr": "Managed switch SW-16, firmware 3.1.0", "object_id": "1.3.6.1.4.1.32473.1.16", "name": "branch-switch",
              "enterprise": 32473}},
    {"ip": "10.44.7.3", "n": 3, "names": ["ap-reception.office"], "ports": [_port(443, "https", "Access point")]},
    {"ip": "10.44.7.10", "n": 10, "names": ["printer-frontdesk.office"], "ports": [_port(631, "ipp"), _port(9100, "jetdirect")],
     "mdns": [{"type": "_ipp._tcp.local", "name": "Front desk printer", "txt": {}}]},
    {"ip": "10.44.7.20", "n": 20, "names": ["files-branch.office"], "ports": [_port(445, "smb"), _port(5001, "https", "File server")]},
    {"ip": "10.44.7.30", "n": 30, "names": ["camera-entrance.office"], "ports": [_port(554, "rtsp"), _port(80, "http", "Camera")]},
    {"ip": "10.44.7.40", "n": 40, "names": ["desk-01.office"], "ports": []},
    {"ip": "10.44.7.41", "n": 41, "names": ["desk-02.office"], "ports": []},
    {"ip": "10.44.7.50", "n": 50, "names": ["branch-server.office"], "ports": [_port(22, "ssh")]},
]
CONTROLLER_SITES = [{"ref": "default", "name": "Default"}, {"ref": "branch", "name": NAME}, {"ref": "storage", "name": "Storage unit"}]


def _scan_host(h: dict) -> dict:
    return {"ip": h["ip"], "mac": _mac(h["n"]), "hostnames": list(h["names"]), "sources": ["arp"] + (["tcp"] if h["ports"] else []),
            "ports": list(h["ports"]), "mdns": list(h.get("mdns") or []), "ssdp": [], "ipv6": [], "wsd": [], "netbios": None,
            "snmp": h.get("snmp")}


def seed_sites(app) -> None:
    """Add the sample branch office to the site list once (its core starts with the app)."""
    store = app.store
    if store.get_setting(SEEDED, None):
        return
    sid = "s-b7a2d3e0"
    if not store.one("SELECT id FROM sites WHERE id=?", (sid,)):
        store.execute("INSERT INTO sites(id, name, kind, created, pos) VALUES(?,?,?,?,?)", (sid, NAME, "remote", time.time() - 30 * 86400, 1))
    store.set_setting(SEEDED, [sid])


class BranchDemo:
    """The demo face of a further site: seeds the sample branch office (only the site seeded by ``seed_sites``),
    keeps its devices and sources alive, and never sends a packet."""

    def __init__(self):
        self.t0 = time.time()

    # what the core asks a demo for
    def networks(self) -> list:
        return []                                         # a remote site has no interface of this host

    def test_details(self, kind: str) -> dict:
        return {"controller": {"clients": 7, "devices": 3}}.get(kind, {})

    def controller_sites(self) -> list:
        return [dict(s) for s in CONTROLLER_SITES]

    async def scan(self, plan: dict, progress, preview=None) -> dict:
        raise ValueError("site.err.scan_consent")

    def _mine(self, core) -> bool:
        hub = getattr(core, "site_hub", None)
        seeded = hub.store.get_setting(SEEDED, []) if hub is not None else []
        return getattr(core, "site_id", "") in (seeded or [])

    def seed(self, core) -> None:
        if core.store.one("SELECT id FROM devices LIMIT 1"):
            return
        now = time.time()
        scan_id = core.store.execute("INSERT INTO scans(started, finished, mode, cidrs, status, hosts) VALUES(?,?,?,?,?,?)",
                                     (now - 9 * 86400, now - 9 * 86400 + 20, "gentle", '["10.44.7.0/24"]', "done", len(HOSTS))).lastrowid
        core.ingest({"hosts": [_scan_host(h) for h in HOSTS]}, scan_id)
        core.store.execute("UPDATE devices SET first_seen = first_seen - ?", (9 * 86400,))
        core.store.execute("UPDATE devices SET first_seen = ? WHERE ip = ?", (now - 3 * 3600, "10.44.7.30"))   # new today
        core.store.execute("UPDATE devices SET last_seen = ? WHERE ip = ?", (now - 2 * 86400, "10.44.7.41"))   # switched off
        for row in core.store.query("SELECT id FROM devices WHERE ip = ?", ("10.44.7.41",)):
            core.presence.pop(row["id"], None)
        core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                           ("controller", "Branch controller", json.dumps({"url": "https://10.44.7.2:8443", "username": "map-viewer",
                                                                           "site": "branch"}), now - 9 * 86400))
        facts = {"name": "branch-server", "ips": ["10.44.7.50"], "macs": [_mac(50)], "os": "Server OS 12", "kernel": "6.6.0",
                 "arch": "x86_64", "cpus": 2, "uptime": 11 * 86400}
        core.store.execute("INSERT INTO agent_hosts(id, kind, name, token_hash, url, created, last_seen, version, facts)"
                           " VALUES(?,?,?,?,?,?,?,?,?)",
                           (AGENT, "agent", "branch-server", None, None, now - 9 * 86400, now, agents_mod.__version__, json.dumps(facts)))
        core.agents.live[AGENT] = {"known_names": [], "t": 0}
        core.agents._resolve_device(AGENT, facts)
        core.structure_version += 1

    async def run(self, core) -> None:
        while True:
            await asyncio.sleep(2.0)
            if not self._mine(core):
                continue
            try:
                self.seed(core)
                self.tick(core, time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging
                logging.getLogger("lanloom.demo_sites").exception("sample branch office round failed")

    def tick(self, core, now: float) -> None:
        if now - getattr(self, "_seen_t", 0) < 10 and getattr(self, "_seen", None) is not None:
            for dev_id in self._seen:                       # the list of devices that answer changes only with the data
                core.presence[dev_id] = now
        else:
            self._seen_t, self._seen = now, []
            for d in core.store.query("SELECT id, data FROM devices"):
                data = json.loads(d["data"] or "{}") if isinstance(d.get("data"), str) else (d.get("data") or {})
                if not any(n in OFFLINE for n in data.get("hostnames") or []):
                    self._seen.append(d["id"])
                    core.presence[d["id"]] = now
        for row in core.store.query("SELECT id FROM integrations"):
            key = "int:%d" % row["id"]
            core.source_status[key] = {"ok": True, "key": "source.ok", "t": now}
            cad = core.cadence.setdefault(key, live.Cadence())
            if not cad.last or now - cad.last >= 30:            # the controller is read every 30 s
                cad.tick()
        watch = getattr(getattr(core, "reports", None), "watch", None)
        if watch is not None and isinstance(getattr(watch, "wan", None), dict):
            watch.wan["branch-gw"] = "up"                       # the controller reports the internet line of the office
        el = now - self.t0
        wave = 0.5 + 0.5 * math.sin(el / 31.0)
        rep = {"t": now, "interval": 10, "flows": [],
               "system": {"load": [round(0.3 + 0.6 * wave, 2), 0.5, 0.5], "mem_total": 4 << 30, "mem_avail": int((4 << 30) * 0.55),
                          "swap_total": 1 << 30, "swap_free": 1 << 30,
                          "disks": [{"mount": "/", "total": 64 << 30, "free": 39 << 30}]},
               "units": {"available": True, "failed": [], "timers": [], "total": 140},
               "containers": {"available": False, "reason": "no_socket"}, "overlay": {"available": False}, "listen": []}
        if AGENT in core.agents.live and now - getattr(self, "_agent_t", 0) >= 10:      # the agent reports every 10 s
            self._agent_t = now
            core.agents.ingest_report(AGENT, rep, now)
            core.agents.touch(AGENT, now)
