"""Demo mode (LANLOOM_DEMO=1): a fictional home network with synthetic live data.

Nothing is scanned and nothing is contacted. Every name, address and value here is invented; the scan in the
setup wizard is simulated and the discovered hosts still go through the real classifier. Useful to try the
product and for documentation screenshots.
"""
import asyncio
import math
import random
import time

NET = "192.168.50.0/24"


def _mac(n: int) -> str:
    return "02:4c:4c:00:00:%02x" % n          # locally administered, not a real vendor prefix


def _port(port, service, title=None, server=None):
    banner = {"title": title, "server": server} if (title or server) else None
    return {"port": port, "proto": "tcp", "service": service, "banner": banner}


HOSTS = [
    {"ip": "192.168.50.1", "n": 1, "gw": True, "names": ["gateway.lan"],
     "ports": [_port(53, "dns"), _port(80, "http", "Router"), _port(443, "https", "Router")],
     "ssdp": [{"st": "urn:schemas-upnp-org:device:InternetGatewayDevice:1", "friendly_name": "Router", "model": "Home gateway",
               "manufacturer": "Example Networks", "device_type": "urn:schemas-upnp-org:device:InternetGatewayDevice:1", "server": None}]},
    {"ip": "192.168.50.2", "n": 2, "names": ["core-switch.lan"],
     "ports": [_port(80, "http", "Switch management"), _port(443, "https", "Switch management")]},
    {"ip": "192.168.50.3", "n": 3, "names": ["ap-upstairs.lan"],
     "ports": [_port(443, "https", "Access point")],
     "ssdp": [{"st": "urn:schemas-wifialliance-org:device:WLANAccessPointDevice:1", "friendly_name": "Access point upstairs",  # identifier: UPnP device type
               "model": None, "manufacturer": None, "device_type": None, "server": None}]},
    {"ip": "192.168.50.4", "n": 4, "names": ["ap-garden.lan"],
     "ports": [_port(443, "https", "Access point")],
     "ssdp": [{"st": "urn:schemas-wifialliance-org:device:WLANAccessPointDevice:1", "friendly_name": "Access point garden",  # identifier: UPnP device type
               "model": None, "manufacturer": None, "device_type": None, "server": None}]},
    {"ip": "192.168.50.10", "n": 10, "names": ["storage.local"],
     "ports": [_port(22, "ssh"), _port(80, "http"), _port(443, "https"), _port(445, "smb"), _port(5000, "web-alt", "Storage"),
               _port(5001, "web-alt-tls", "Storage")],
     "mdns": [{"type": "_smb._tcp", "name": "Storage", "port": 445, "txt": {}}, {"type": "_nfs._tcp", "name": "Storage", "port": 2049, "txt": {}}]},
    {"ip": "192.168.50.11", "n": 11, "self": True, "names": ["homeserver.lan"],
     "ports": [_port(22, "ssh"), _port(80, "http"), _port(443, "https"), _port(8080, "http-alt")]},
    {"ip": "192.168.50.12", "n": 12, "names": ["backup.lan"],
     "ports": [_port(22, "ssh"), _port(873, "rsync")], "title": "Backup server"},
    {"ip": "192.168.50.13", "n": 13, "names": ["broker.lan"],
     "ports": [_port(1883, "mqtt"), _port(8883, "mqtts")],
     "mdns": [{"type": "_mqtt._tcp", "name": "Message broker", "port": 1883, "txt": {}}]},
    {"ip": "192.168.50.14", "n": 14, "names": ["automation.local"],
     "ports": [_port(8123, "web-alt", "Automation")],
     "mdns": [{"type": "_home-assistant._tcp", "name": "Automation hub", "port": 8123, "txt": {}}]},  # identifier: demo DNS-SD type
    {"ip": "192.168.50.20", "n": 20, "names": ["office-laptop.lan"],
     "ports": [_port(22, "ssh")],
     "mdns": [{"type": "_workstation._tcp", "name": "Office laptop", "port": 9, "txt": {}}]},
    {"ip": "192.168.50.21", "n": 21, "names": ["studio-desktop.lan"],
     "ports": [_port(139, "netbios"), _port(445, "smb"), _port(3389, "remote-desktop")], "title": "Studio desktop"},
    {"ip": "192.168.50.22", "n": 22, "names": ["phone-1.lan"],
     "mdns": [{"type": "_companion-link._tcp", "name": "Phone 1", "port": 49152, "txt": {}}], "ports": []},  # identifier: demo DNS-SD type
    {"ip": "192.168.50.23", "n": 23, "names": ["tablet.lan"],
     "mdns": [{"type": "_companion-link._tcp", "name": "Kitchen tablet", "port": 49153, "txt": {}}], "ports": []},  # identifier: demo DNS-SD type
    {"ip": "192.168.50.30", "n": 30, "names": ["living-room-tv.lan"],
     "ports": [_port(8008, "web-alt"), _port(8009, "web-alt")],
     "ssdp": [{"st": "urn:dial-multiscreen-org:service:dial:1", "friendly_name": "Living room TV", "model": "TV-55",
               "manufacturer": "Example Corp", "device_type": "urn:schemas-upnp-org:device:MediaRenderer:1", "server": None}]},
    {"ip": "192.168.50.31", "n": 31, "names": ["kitchen-speaker.lan"],
     "ports": [_port(1400, "media-ctl")],
     "ssdp": [{"st": "urn:schemas-upnp-org:device:MediaRenderer:1", "friendly_name": "Kitchen speaker", "model": "SP-1",
               "manufacturer": "Example Audio", "device_type": "urn:schemas-upnp-org:device:MediaRenderer:1", "server": None}]},
    {"ip": "192.168.50.32", "n": 32, "names": ["media-player.lan"],
     "ssdp": [{"st": "urn:schemas-upnp-org:device:MediaRenderer:1", "friendly_name": "Bedroom media player", "model": "MP-100",
               "manufacturer": "Example Corp", "device_type": "urn:schemas-upnp-org:device:MediaRenderer:1", "server": None}], "ports": []},
    {"ip": "192.168.50.40", "n": 40, "names": ["office-printer.lan"],
     "ports": [_port(80, "http", "Printer status"), _port(631, "ipp"), _port(9100, "printer-raw")],
     "mdns": [{"type": "_ipp._tcp", "name": "Office printer", "port": 631, "txt": {}}]},
    {"ip": "192.168.50.50", "n": 50, "names": ["front-door-cam.lan"],
     "ports": [_port(80, "http", "Camera"), _port(554, "rtsp")], "title": "Front door camera"},
    {"ip": "192.168.50.51", "n": 51, "names": ["garage-cam.lan"],
     "ports": [_port(80, "http", "Camera"), _port(554, "rtsp")], "title": "Garage camera"},
    {"ip": "192.168.50.60", "n": 60, "names": ["plug-washer.lan"], "ports": [_port(80, "http", "Smart plug")], "title": "Washer plug"},
    {"ip": "192.168.50.61", "n": 61, "names": ["thermostat-hall.lan"], "ports": [_port(80, "http", "Thermostat")], "title": "Hall thermostat"},
    {"ip": "192.168.50.62", "n": 62, "names": ["energy-meter.lan"], "ports": [_port(80, "http", "Energy meter sensor")], "title": "Energy meter"},
    {"ip": "192.168.50.63", "n": 63, "names": ["light-strip.lan"], "ports": [_port(80, "http", "Light strip")], "title": "Desk light strip"},
    {"ip": "192.168.50.64", "n": 64, "names": ["air-quality.lan"], "ports": [_port(80, "http", "Air quality sensor")], "title": "Air quality sensor"},
    {"ip": "192.168.50.65", "n": 65, "names": ["vacuum.lan"], "ports": [_port(80, "http", "Robot vacuum")], "title": "Robot vacuum"},
    {"ip": "192.168.50.66", "n": 66, "names": ["inverter.lan"], "ports": [_port(80, "http", "Solar inverter sensor")], "title": "Solar inverter"},
    {"ip": "192.168.50.99", "n": 99, "names": [], "ports": [_port(49152, "upnp")]},
]

AREAS = [("living_room", "Living room", 6, 31), ("kitchen", "Kitchen", 5, 24), ("office", "Office", 4, 19),
         ("bedroom", "Bedroom", 3, 12), ("garden", "Garden", 4, 15), ("garage", "Garage", 2, 9), ("hallway", "Hallway", 3, 11)]
TOPICS = [("sensors/climate", 6.0, 180), ("energy/meter", 4.0, 220), ("home/presence", 0.5, 90), ("garden/irrigation", 0.2, 140),
          ("solar/inverter", 1.0, 400), ("radio/bridge", 9.0, 160), ("lights/state", 1.5, 120)]


def scan_host(h: dict) -> dict:
    title = h.get("title")
    ports = list(h.get("ports") or [])
    if title and ports:
        p0 = dict(ports[0])
        p0["banner"] = {"title": title, "server": None}
        ports[0] = p0
    return {"ip": h["ip"], "mac": _mac(h["n"]), "hostnames": list(h.get("names") or []),
            "sources": ["arp"] + (["mdns"] if h.get("mdns") else []) + (["ssdp"] if h.get("ssdp") else []) + (["tcp"] if ports else []),
            "ports": ports, "mdns": list(h.get("mdns") or []), "ssdp": list(h.get("ssdp") or []),
            "is_gateway": bool(h.get("gw")), "is_self": bool(h.get("self")), "rtt_ms": round(random.uniform(0.4, 6.0), 2)}


class Demo:
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)

    def networks(self) -> list:
        return [{"cidr": NET, "iface": "eth0", "self_ip": "192.168.50.11", "gateway": "192.168.50.1", "private": True,
                 "size": 256, "host_mode": True}]

    def test_details(self, kind: str) -> dict:
        return {"hub": {"version": "demo", "entities": 121, "devices": 27}, "mqtt": {"topics": len(TOPICS)},
                "snmp": {"name": "router", "descr": "Demo router", "ifaces": 4}}.get(kind, {})

    async def scan(self, plan: dict, progress) -> dict:
        hosts = [scan_host(h) for h in HOSTS]
        total = 254
        phases = [("neighbors", 0.35), ("mdns", 0.2), ("ssdp", 0.15), ("ports", 0.2), ("banners", 0.1)]
        done, found = 0, 0
        for phase, share in phases:
            steps = 12
            for _ in range(steps):
                await asyncio.sleep(0.25)
                done = min(total, done + int(total * share / steps) + 1)
                found = min(len(hosts), found + (2 if phase in ("neighbors", "mdns") else 1))
                progress({"phase": phase, "done": done, "total": total, "found": found})
        progress({"phase": "banners", "done": total, "total": total, "found": len(hosts)})
        now = time.time()
        return {"started": now - 15, "finished": now, "mode": "deep" if plan.get("deep") else "gentle",
                "networks": self.networks(), "hosts": hosts, "stats": {"probes": 1660, "rate_per_s": 38.5, "duration_s": 15.0}}

    def seed(self, core) -> None:
        """Fill an empty demo instance: one finished scan and three integrations (no secrets, nothing contacted)."""
        if core.store.one("SELECT id FROM devices LIMIT 1"):
            return
        scan_id = core.store.execute("INSERT INTO scans(started, finished, mode, cidrs, status, hosts) VALUES(?,?,?,?,?,?)",
                                     (time.time() - 60, time.time() - 45, "gentle", '["%s"]' % NET, "done", len(HOSTS))).lastrowid
        core.ingest({"hosts": [scan_host(h) for h in HOSTS]}, scan_id)
        core.store.set_setting("rescan_cidrs", [NET])

    def seed_integrations(self, core) -> None:
        if core.store.one("SELECT id FROM integrations LIMIT 1"):
            return
        import json as _json
        for kind, name, cfg in (("hub", "Automation hub", {"url": "http://192.168.50.14:8123"}),
                                ("mqtt", "Message broker", {"host": "192.168.50.13", "port": 1883, "tls": False}),
                                ("snmp", "Router counters", {"host": "192.168.50.1", "version": "2c"})):
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (kind, name, _json.dumps(cfg), time.time()))

    async def run(self, core) -> None:
        """Synthetic live data: flows of this host, router counters, broker topics, hub state changes."""
        rng = self.rng
        t0 = time.time()
        hub_sent = 0
        while True:
            await asyncio.sleep(1.0)
            if not core.store.one("SELECT id FROM devices LIMIT 1"):
                continue
            now = time.time()
            el = now - t0
            ids = {row["kind"]: row["id"] for row in core.store.query("SELECT id, kind FROM integrations")}
            for key in ["host"] + ["int:%d" % i for i in ids.values()]:
                core.source_status[key] = {"ok": True, "key": "source.ok", "t": now}
            wave = 0.5 + 0.5 * math.sin(el / 23.0)
            burst = 1.0 if (el % 90) < 40 else 0.08
            flows = [
                ("192.168.50.10", 445, 6_000_000 * burst * (0.6 + 0.4 * rng.random()), 90_000),     # backup to storage
                ("192.168.50.30", 8009, 2_200_000 * (0.7 + 0.3 * wave), 40_000),                    # streaming to the TV
                ("192.168.50.13", 1883, 9_000 + 4_000 * rng.random(), 22_000 + 6_000 * rng.random()),
                ("192.168.50.14", 8123, 30_000 * (0.5 + rng.random()), 140_000 * (0.5 + rng.random())),
                ("192.168.50.20", 22, 380_000 * wave, 1_200_000 * wave),
                ("192.168.50.12", 873, 900_000 * (1 - burst + 0.1), 30_000),
                ("192.168.50.31", 1400, 160_000 * (0.8 + 0.2 * rng.random()), 4_000),
                # documentation addresses (RFC 5737): peers beyond the LAN, shown as end marks at the internet tile
                ("203.0.113.20", 443, 250_000 * (0.5 + rng.random()), 1_900_000 * (0.5 + rng.random())),
                ("198.51.100.7", 443, 40_000 * (0.5 + rng.random()), 260_000 * (0.5 + rng.random())),
                ("192.0.2.53", 853, 3_000 + 2_000 * rng.random(), 6_000 + 3_000 * rng.random()),
            ]
            core.on_event("host", None, {"type": "bytes", "source": "host", "t": now, "flows": [
                {"local_ip": "192.168.50.11", "local_port": 40000 + i, "peer_ip": ip, "peer_port": port, "tx": int(tx), "rx": int(rx)}
                for i, (ip, port, tx, rx) in enumerate(flows)]})
            core.on_event("host", None, {"type": "iface", "t": now, "ifaces": [{"name": "eth0", "tx": int(sum(f[2] for f in flows)),
                                                                               "rx": int(sum(f[3] for f in flows))}]})
            if "snmp" in ids and int(el) % 10 == 0:
                down = int((1_200_000 + 4_500_000 * wave) * 10)
                up = int((250_000 + 300_000 * rng.random()) * 10)
                core.on_event("int:%d" % ids["snmp"], ids["snmp"], {
                    "type": "snmp", "host": "192.168.50.1", "t": now, "sys": {"name": "router", "descr": "Demo router", "uptime": int(el)},
                    "ifaces": [{"index": 1, "name": "wan", "oper": "up", "speed": 1_000_000_000, "in": down, "out": up},
                               {"index": 2, "name": "lan1", "oper": "up", "speed": 1_000_000_000, "in": up, "out": down},
                               {"index": 3, "name": "lan2", "oper": "up", "speed": 1_000_000_000, "in": 0, "out": 0},
                               {"index": 4, "name": "wlan", "oper": "up", "speed": 600_000_000, "in": up // 3, "out": down // 3}]})
            if "mqtt" in ids:
                topics = []
                for root, rate, size in TOPICS:
                    n = sum(1 for _ in range(int(rate * 2)) if rng.random() < 0.5) if rate >= 1 else (1 if rng.random() < rate else 0)
                    topics.append([root, n, n * size])
                core.on_event("int:%d" % ids["mqtt"], ids["mqtt"], {"type": "mqtt", "t": now, "dt": 1.0, "topics": topics,
                                                                   "broker": {"clients": 14}})
            if "hub" in ids:
                hid = ids["hub"]
                if hub_sent == 0 or now - hub_sent > 600:
                    core.on_event("int:%d" % hid, hid, self._snapshot(now))
                    hub_sent = now
                for _ in range(rng.randint(1, 4)):
                    area = rng.choice(AREAS)
                    core.on_event("int:%d" % hid, hid, {"type": "hub_state", "entity_id": "sensor.%s_%d" % (area[0], rng.randint(1, area[3])),
                                                       "state": "%.1f" % rng.uniform(18, 24), "t": now})

    def _snapshot(self, now: float) -> dict:
        devices, entities = [], []
        for area_id, name, n_dev, n_ent in AREAS:
            for i in range(n_dev):
                devices.append({"id": "%s-dev-%d" % (area_id, i), "name": "%s device %d" % (name, i + 1), "model": None,
                                "manufacturer": None, "area": area_id, "via": None})
            for i in range(n_ent):
                domain = ("sensor", "light", "switch", "binary_sensor")[i % 4]
                state = {"sensor": "%.1f" % (19 + (i % 7) * 0.6), "light": "on" if i % 3 else "off",
                         "switch": "off", "binary_sensor": "off"}[domain]
                unit = "°C" if domain == "sensor" else None
                label = {"sensor": "temperature", "light": "light", "switch": "switch", "binary_sensor": "window"}[domain]
                entities.append({"entity_id": "%s.%s_%d" % (domain, area_id, i + 1), "device_id": "%s-dev-%d" % (area_id, i % n_dev),
                                 "name": "%s %s %d" % (name, label, i // 4 + 1), "state": state, "unit": unit, "domain": domain,
                                 "area": area_id})
        return {"type": "hub_snapshot", "t": now, "devices": devices, "entities": entities,
                "areas": [{"id": a[0], "name": a[1]} for a in AREAS]}
